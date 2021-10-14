from abc import ABC, abstractmethod
from typing import List
import pytorch_lightning as pl
import torch
from torch import nn


class NonparametricLightningModule(pl.LightningModule, ABC):
    """
    A lightning module which sets some defaults for training models with no parameters (i.e. only
    buffers that are optimized differently than via gradient descent).
    """

    def __init__(self):
        super().__init__()
        self.automatic_optimization = False

        # Required parameter to make DDP training work
        self.register_parameter("__ddp_dummy__", nn.Parameter(torch.empty(1)))

    def configure_optimizers(self) -> None:
        return None

    def training_step(self, batch: torch.Tensor, batch_idx: int) -> torch.Tensor:
        self.nonparametric_training_step(batch, batch_idx)
        # Dummy value to make lightning call `training_epoch_end`
        return torch.empty(1)

    def training_epoch_end(self, outputs: List[torch.Tensor]) -> None:
        self.nonparametric_training_epoch_end()

    @abstractmethod
    def nonparametric_training_step(self, batch: torch.Tensor, batch_idx: int) -> None:
        """
        Training step that is not allowed to return any value.
        """

    @abstractmethod
    def nonparametric_training_epoch_end(self) -> None:
        """
        Training epoch end that is not passed any outputs.
        """
