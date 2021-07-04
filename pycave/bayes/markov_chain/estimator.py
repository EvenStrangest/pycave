from __future__ import annotations
from typing import cast, List, Optional
import numpy as np
import pytorch_lightning as pl
import torch
from torch.nn.utils.rnn import PackedSequence
from torch.utils.data import DataLoader
from pycave.core.estimator import Estimator
from pycave.data.sequences import collate_sequences, sequence_dataset_from_data, SequenceData
from .model import MarkovChainModel, MarkovChainModelConfig
from .module import MarkovChainLightningModule


class MarkovChain(Estimator[MarkovChainModel]):
    """
    A Markov chain can be used to learn the initial probabilities of a set of states and the
    transition probabilities between them. It is similar to a hidden Markov model, only that the
    hidden states are known. More information is available
    `here <https://en.wikipedia.org/wiki/Markov_chain>`_.
    """

    def __init__(
        self,
        num_states: Optional[int] = None,
        symmetric: bool = False,
        batch_size: Optional[int] = None,
        num_workers: int = 0,
        trainer: Optional[pl.Trainer] = None,
    ):
        """
        Args:
            num_states: The number of states that the Markov chain has. If not provided, it will
                be derived automatically when calling :meth:`fit`. Note that this requires a pass
                through the data. Consider setting this option explicitly if you're fitting a lot
                of data.
            symmetric: Whether the transitions between states should be considered symmetric.
            batch_size: The batch size to use when fitting the model. If not provided, all data
                will be used as a single batch. You should consider setting this option if your
                data does not fit into memory.
            num_workers: The number of workers to use for loading the data. By default, it loads
                data on the main process.
            trainer: The PyTorch Lightning trainer to use for fitting the model. Consider setting
                it explicitly if you e.g. have a GPU available but want to train on the CPU. Make
                sure that you set :code:`max_epochs = 1` on the trainer since training for a Markov
                chain requires only a single pass through the data.
        """
        super().__init__()

        self.num_states = num_states
        self.symmetric = symmetric
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.trainer = trainer or pl.Trainer(max_epochs=1)
        self.model_: MarkovChainModel

    def fit(self, sequences: SequenceData) -> MarkovChain:
        """
        Fits the Markov chain on the provided data and returns the fitted estimator.

        Args:
            sequences: The sequences to fit the Markov chain on. May be a two-dimensional NumPy
                array or PyTorch tensor, or a PyTorch dataset yielding individual sequences.

        Returns:
            The fitted Markov chain.
        """
        config = MarkovChainModelConfig(num_states=self.num_states or _get_num_states(sequences))
        self.model_ = MarkovChainModel(config)

        loader = self._get_data_loader(sequences)
        module = MarkovChainLightningModule(self.model_, self.symmetric)
        self.trainer.fit(module, loader)
        return self

    def sample(self, num_sequences: int, sequence_length: int) -> torch.Tensor:
        """
        Samples state sequences from the fitted Markov chain.

        Args:
            num_sequences: The number of sequences to sample.
            sequence_length: The length of the sequences to sample.

        Returns:
            The sampled sequences as a tensor of shape `[num_sequences, sequence_length]`.
        """
        return self.model_.sample(num_sequences, sequence_length)

    def score(self, sequences: SequenceData) -> float:
        """
        Computes the average log-probability of all the provided sequences. If you want to have
        log-probabilities for each individual sequence, use :meth:`score_samples` instead.

        Args:
            sequences: The sequences for which to compute the average log-probability.

        Returns:
            The average log-probability for all sequences.

        Note:
            Other than :meth:`score_samples`, this method can also be run across multiple
            processes.
        """
        module = MarkovChainLightningModule(self.model_)
        loader = self._get_data_loader(sequences)
        result = self.trainer.test(module, loader, verbose=False)
        return result[0]["log_prob"]

    def score_samples(self, sequences: SequenceData) -> torch.Tensor:
        """
        Computes the log-probability of observing each of the sequences provided.

        Args:
            sequences: The sequences for which to compute the log-probabilities.

        Returns:
            The log-probability for each individual sequence.

        Attention:
            This method cannot be used in a multi-process setting.
        """
        module = MarkovChainLightningModule(self.model_)
        loader = self._get_data_loader(sequences)
        result = self.trainer.predict(module, loader, return_predictions=True)
        return torch.stack(cast(List[torch.Tensor], result))

    def _get_data_loader(self, sequences: SequenceData) -> DataLoader[PackedSequence]:
        dataset = sequence_dataset_from_data(sequences)
        assert self.batch_size is not None or hasattr(
            dataset, "__len__"
        ), "batch size must be set for iterable datasets"
        return DataLoader(
            dataset,
            batch_size=self.batch_size or len(dataset),  # type: ignore
            collate_fn=collate_sequences,  # type: ignore
            num_workers=self.num_workers,
        )


def _get_num_states(data: SequenceData) -> int:
    if isinstance(data, np.ndarray):
        assert data.dtype == np.int64, "array states must have type `np.int64`"
        return int(data.max() + 1)
    if isinstance(data, torch.Tensor):
        assert data.dtype == torch.long, "tensor states must have type `torch.long`"
        return int(data.max().item() + 1)
    return max(_get_num_states(entry) for entry in data)
