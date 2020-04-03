from abc import ABC, abstractmethod
import torch
import torch.nn as nn
import torch.distributions as dist
from sklearn.cluster import KMeans
from .utils import to_one_hot, max_likeli_means, max_likeli_covars, normalize, log_normal

class OutputHead(ABC):
    """
    Output head is a base class that all modules conform to that model the output of a hidden Markov
    model.
    """

    @abstractmethod
    def reset_parameters(self, **kwargs):
        """
        Resets the parameters of the output head randomly or according to some data if the concrete
        type supports this.

        Parameters
        ----------
        kwargs: keyword arguments
            Additional parameters that some output heads can work with.
        """

    @abstractmethod
    def evaluate(self, data):
        """
        Computes the responsibilities of each hidden state for the given data.

        Parameters
        ----------
        data: torch.Tensor [..., ?]
            The data to evaluate for. The shape is arbitrary, the existence of an additional
            dimension depends on the conrete output type.

        Returns
        -------
        torch.Tensor [..., K]
            The responsibility of all input types and each hidden state (number of hidden states K).
        """

    @abstractmethod
    def sample(self, states):
        """
        Samples from the output given a set of hidden states. The shape of the states is arbitrary.

        Parameters
        ----------
        states: torch.Tensor [...]
            The hidden states obtained from the Markov model. The shape of the tensor is arbitrary.

        Returns
        -------
        torch.Tensor [..., ?]
            The samples for each of the state. Might be a multi-dimensional output for each state
            depending on the concrete output head.
        """

    @abstractmethod
    def maximize(self, data, gamma):
        """
        Returns an intermediate representation to update its parameters to maximize the probability
        of outputting the given sequences with the given gamma values.

        Parameters
        ----------
        data: torch.Tensor [N, ?]
            Data from the observed sequences. Contents are shaped either by concatenating the
            sequences or by concatenating the timesteps of the sequences ("packing"). The layout
            should, however, be irrelevant to this method (number of datapoints N).
        gamma: torch.Tensor [N, K]
            The responsibilities of all data items for the hidden states of the HMM. The layout is
            equal to the layout of `data` (number of hidden states K).

        Returns
        -------
        dict
            Dictionary mapping keys to values. The results can be used in the `update` and `apply`
            methods.
        """

    @abstractmethod
    def update(self, current, previous=None):
        """
        Updates an update object with a new one received from the `maximize` method.

        Parameters
        ----------
        current: dict
            The update values outputted by the most recent call of the `maximize` method.
        previous: dict, default: None
            The values from a previous call to `maximize` or `update`.
        """

    @abstractmethod
    def apply(self, update):
        """
        Applies the update received by continuous calls to the `maximize` and `update` methods. It
        changes the parameters of this model.

        Parameters
        ----------
        update: dict
            The update object.
        """


# pylint: disable=abstract-method,missing-function-docstring
class Discrete(OutputHead, nn.Module):
    """
    Represents a module that models a discrete output head of a hidden Markov model.
    """

    def __init__(self, num_states, num_outputs):
        super().__init__()

        self.probabilities = nn.Parameter(
            torch.empty(num_states, num_outputs), requires_grad=False
        )
        self.reset_parameters()

    @property
    def num_states(self):
        return self.probabilities.size(0)

    def reset_parameters(self, **kwargs):
        self.probabilities.uniform_()
        # Must use in-place operation to keep as parameters instead of tensor
        self.probabilities /= self.probabilities.sum(-1, keepdim=True)

    def evaluate(self, data):
        probabilities = self.probabilities.t()[data]
        return normalize(probabilities)

    def sample(self, states):
        generator = dist.Categorical(self.probabilities[states])
        return generator.sample()

    def maximize(self, sequences, gamma):
        gamma_ = gamma.t()
        sequences_ = sequences.view(1, -1).expand(self.num_states, -1)

        num = torch.zeros_like(self.probabilities)
        num.scatter_add_(1, sequences_, gamma_)

        denom = gamma.sum(0)

        return {'num': num, 'denom': denom}

    def update(self, current, previous=None):
        num = current['num']
        denom = current['denom']

        if previous is not None:
            num += previous['num']
            denom += previous['denom']

        return {'num': num, 'denom': denom}

    def apply(self, update):
        self.probabilities.set_(update['num'] / update['denom'])

    def __repr__(self):
        return f'{self.__class__.__name__}(num_outputs={self.probabilities.size(1)})'


# pylint: disable=abstract-method,missing-function-docstring
class Gaussian(OutputHead, nn.Module):
    """
    Represents a module that models a set of Gaussian distributions. It is *not* a full mixture
    model since the component weights are missing. This way, however, this class can be used in both
    GMMs and HMMs.
    """

    def __init__(self, num_components, num_features, covariance):
        super().__init__()

        self.covariance = covariance

        self.means = nn.Parameter(
            torch.empty(num_components, num_features),
            requires_grad=False
        )

        if covariance == 'diag':
            self.covars = nn.Parameter(
                torch.empty(num_components, num_features),
                requires_grad=False
            )
        elif covariance == 'spherical':
            self.covars = nn.Parameter(
                torch.empty(num_components), requires_grad=False
            )
        else:
            self.covars = nn.Parameter(
                torch.empty(num_features), requires_grad=False
            )

        self.reset_parameters()

    @property
    def num_components(self):
        return self.means.size(0)

    @property
    def num_features(self):
        return self.means.size(1)

    def reset_parameters(self, data=None, max_iter=100):
        # 1) Means
        if data is not None:
            model = KMeans(
                self.num_components, n_init=1, max_iter=max_iter, n_jobs=-1
            )
            model.fit(data.cpu().numpy())
            labels = torch.as_tensor(model.labels_, dtype=torch.long, device=data.device)
            one_hot_labels = to_one_hot(labels, self.num_components)
            self.means.set_(max_likeli_means(
                data, one_hot_labels, one_hot_labels.sum(0)
            ))
        else:
            labels = None
            self.means.normal_()

        # 2) Covariance is estimated via the labels from kmeans if they exist, otherwise all
        # covariances are set to 1.
        if labels is None:
            self.covars.fill_(1)
        else:
            self.covars.set_(
                max_likeli_covars(
                    data, one_hot_labels, one_hot_labels.sum(0),
                    self.means, self.covariance
                )
            )

        return labels

    def evaluate(self, data, log=False):
        shape = data.size()
        probabilities = log_normal(data.view(-1, shape[-1]), self.means, self.covars)
        result = probabilities.view(*shape[:-1], self.num_components)

        if log:
            return result
        return result.exp()

    def sample(self, states):
        shape = states.size()
        if states.dim() > 1:
            states = states.view(-1)

        samples = torch.empty(
            states.size(0), self.num_features,
            device=states.device, dtype=torch.float
        )

        unique_states, component_counts = torch.unique(
            states, return_counts=True
        )

        for i in range(unique_states.size(0)):
            c = unique_states[i]
            samples[states == c] = \
                dist.MultivariateNormal(
                    self.means[c], self._get_full_covariance_matrix(c)
                ).sample((component_counts[i],))

        return samples.view(*shape, -1)

    def maximize(self, sequences, gamma):
        denom = gamma.sum(0)
        means = max_likeli_means(sequences, gamma, denom)
        covars = max_likeli_covars(sequences, gamma, denom, means, self.covariance)
        return {'means': means, 'covars': covars}

    def update(self, current, previous=None):
        pass

    def apply(self, update):
        pass

    def _get_full_covariance_matrix(self, i):
        if self.covariance == 'diag':
            return torch.diag(self.covars[i])
        if self.covariance == 'diag-shared':
            return torch.diag(self.covars)
        return self.covars[i] * torch.diag(torch.ones(self.num_features))

    def __repr__(self):
        return f'{self.__class__.__name__}(dim={self.num_features})'
