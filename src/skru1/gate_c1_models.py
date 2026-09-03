"""Compact sequence adapters preregistered for Gate C1.

This module is imported only inside the isolated ``gate_c_torch`` worker.  It
contains no data-loading code and therefore cannot open validation, test, or
holdout manifests.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import torch
from scipy import stats
from torch import Tensor, nn
from torch.nn import functional as F

from .data_contracts import ContractViolation


@dataclass(frozen=True)
class ModelOutput:
    point: Tensor
    loc: Tensor | None = None
    scale: Tensor | None = None
    df: Tensor | None = None


class PackedRecurrentRegressor(nn.Module):
    """GRU/LSTM regressor that repacks left-padded histories chronologically."""

    def __init__(
        self,
        *,
        input_size: int,
        hidden_size: int,
        layers: int,
        dropout: float,
        cell: str,
    ) -> None:
        super().__init__()
        recurrent_dropout = float(dropout) if int(layers) > 1 else 0.0
        recurrent_type = nn.GRU if cell == "gru" else nn.LSTM
        self.recurrent = recurrent_type(
            input_size=int(input_size),
            hidden_size=int(hidden_size),
            num_layers=int(layers),
            dropout=recurrent_dropout,
            batch_first=True,
        )
        self.head_dropout = nn.Dropout(float(dropout))
        self.head = nn.Linear(int(hidden_size), 1)

    @staticmethod
    def chronological_right_padded(x: Tensor, lengths: Tensor) -> Tensor:
        """Move valid tokens with one device-side gather and no host sync."""

        sequence_length = x.shape[1]
        positions = torch.arange(sequence_length, device=x.device).unsqueeze(0)
        lengths_device = lengths.to(device=x.device, dtype=torch.long, non_blocking=True)
        source = sequence_length - lengths_device.unsqueeze(1) + positions
        valid = positions < lengths_device.unsqueeze(1)
        source = source.clamp(min=0, max=sequence_length - 1)
        gathered = x.gather(1, source.unsqueeze(-1).expand(-1, -1, x.shape[2]))
        return gathered * valid.unsqueeze(-1).to(dtype=x.dtype)

    def encode(self, x: Tensor, lengths: Tensor) -> Tensor:
        right_padded = self.chronological_right_padded(x, lengths)
        sequence_output, _ = self.recurrent(right_padded)
        lengths_device = lengths.to(device=x.device, dtype=torch.long, non_blocking=True)
        row_index = torch.arange(x.shape[0], device=x.device)
        return sequence_output[row_index, lengths_device - 1]

    def forward(
        self,
        x: Tensor,
        lengths: Tensor,
        padding_mask: Tensor,
        observation_mask: Tensor,
        missing_campaign_mask: Tensor,
    ) -> ModelOutput:
        del padding_mask, observation_mask, missing_campaign_mask
        representation = self.head_dropout(self.encode(x, lengths))
        point = self.head(representation).squeeze(-1)
        return ModelOutput(point=point)


class StudentTPackedGRU(nn.Module):
    """GRU with a stable three-parameter Student-t head."""

    def __init__(
        self,
        *,
        input_size: int,
        hidden_size: int,
        layers: int,
        dropout: float,
    ) -> None:
        super().__init__()
        recurrent_dropout = float(dropout) if int(layers) > 1 else 0.0
        self.recurrent = nn.GRU(
            input_size=int(input_size),
            hidden_size=int(hidden_size),
            num_layers=int(layers),
            dropout=recurrent_dropout,
            batch_first=True,
        )
        self.head_dropout = nn.Dropout(float(dropout))
        self.distribution_head = nn.Linear(int(hidden_size), 3)

    def forward(
        self,
        x: Tensor,
        lengths: Tensor,
        padding_mask: Tensor,
        observation_mask: Tensor,
        missing_campaign_mask: Tensor,
    ) -> ModelOutput:
        del padding_mask, observation_mask, missing_campaign_mask
        right_padded = PackedRecurrentRegressor.chronological_right_padded(x, lengths)
        sequence_output, _ = self.recurrent(right_padded)
        lengths_device = lengths.to(device=x.device, dtype=torch.long, non_blocking=True)
        row_index = torch.arange(x.shape[0], device=x.device)
        representation = self.head_dropout(sequence_output[row_index, lengths_device - 1])
        raw = self.distribution_head(representation)
        loc = raw[:, 0]
        scale = F.softplus(raw[:, 1]) + 1.0e-3
        df = 2.01 + F.softplus(raw[:, 2])
        return ModelOutput(point=loc, loc=loc, scale=scale, df=df)


class CausalConv1d(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, dilation: int) -> None:
        super().__init__()
        self.left_padding = (int(kernel_size) - 1) * int(dilation)
        self.conv = nn.Conv1d(
            int(in_channels),
            int(out_channels),
            kernel_size=int(kernel_size),
            dilation=int(dilation),
            padding=0,
        )

    def forward(self, x: Tensor) -> Tensor:
        return self.conv(F.pad(x, (self.left_padding, 0)))


class ResidualCausalBlock(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        kernel_size: int,
        dilation: int,
        dropout: float,
    ) -> None:
        super().__init__()
        self.causal = CausalConv1d(in_channels, out_channels, kernel_size, dilation)
        self.dropout = nn.Dropout(float(dropout))
        self.residual = (
            nn.Identity()
            if int(in_channels) == int(out_channels)
            else nn.Conv1d(int(in_channels), int(out_channels), kernel_size=1)
        )

    def forward(self, x: Tensor, valid_mask: Tensor) -> Tensor:
        transformed = self.dropout(F.relu(self.causal(x)))
        output = F.relu(transformed + self.residual(x))
        return output * valid_mask


class CausalTCNRegressor(nn.Module):
    """Two-block masked causal TCN with fixed dilations 1 and 2."""

    def __init__(
        self,
        *,
        input_size: int,
        channels: list[int] | tuple[int, int],
        kernel_size: int,
        dropout: float,
    ) -> None:
        super().__init__()
        if len(channels) != 2:
            raise ContractViolation("Gate C1 TCN must contain exactly two residual blocks")
        self.blocks = nn.ModuleList(
            [
                ResidualCausalBlock(
                    int(input_size), int(channels[0]), kernel_size=kernel_size, dilation=1, dropout=dropout
                ),
                ResidualCausalBlock(
                    int(channels[0]), int(channels[1]), kernel_size=kernel_size, dilation=2, dropout=dropout
                ),
            ]
        )
        self.head = nn.Linear(int(channels[-1]), 1)

    def forward(
        self,
        x: Tensor,
        lengths: Tensor,
        padding_mask: Tensor,
        observation_mask: Tensor,
        missing_campaign_mask: Tensor,
    ) -> ModelOutput:
        del lengths, observation_mask, missing_campaign_mask
        valid_mask = (1.0 - padding_mask).unsqueeze(1)
        encoded = x.transpose(1, 2) * valid_mask
        for block in self.blocks:
            encoded = block(encoded, valid_mask)
        # Every normalized C0 sequence ends with its current observation.
        point = self.head(encoded[:, :, -1]).squeeze(-1)
        return ModelOutput(point=point)


def create_sequence_model(
    model_id: str,
    parameters: Mapping[str, Any],
    *,
    input_size: int,
) -> nn.Module:
    if model_id == "C01_compact_gru":
        return PackedRecurrentRegressor(
            input_size=input_size,
            hidden_size=int(parameters["hidden_size"]),
            layers=int(parameters["layers"]),
            dropout=float(parameters["dropout"]),
            cell="gru",
        )
    if model_id == "C02_compact_lstm":
        return PackedRecurrentRegressor(
            input_size=input_size,
            hidden_size=int(parameters["hidden_size"]),
            layers=int(parameters["layers"]),
            dropout=float(parameters["dropout"]),
            cell="lstm",
        )
    if model_id == "C03_causal_tcn":
        return CausalTCNRegressor(
            input_size=input_size,
            channels=parameters["channels"],
            kernel_size=int(parameters["kernel_size"]),
            dropout=float(parameters["dropout"]),
        )
    if model_id == "C04_probabilistic_gru_student_t":
        return StudentTPackedGRU(
            input_size=input_size,
            hidden_size=int(parameters["hidden_size"]),
            layers=int(parameters["layers"]),
            dropout=float(parameters["dropout"]),
        )
    raise KeyError(model_id)


def model_parameter_count(model: nn.Module) -> int:
    return int(sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad))


def state_dict_cpu(model: nn.Module) -> OrderedDict[str, Tensor]:
    return OrderedDict(
        (name, value.detach().cpu().clone()) for name, value in model.state_dict().items()
    )


def point_huber_loss(output: ModelOutput, target: Tensor) -> Tensor:
    return F.huber_loss(output.point, target, delta=1.0, reduction="mean")


def student_t_nll_loss(output: ModelOutput, target: Tensor) -> Tensor:
    if output.loc is None or output.scale is None or output.df is None:
        raise ContractViolation("Student-t objective requires loc, scale, and df")
    distribution = torch.distributions.StudentT(df=output.df, loc=output.loc, scale=output.scale)
    return -distribution.log_prob(target).mean()


def student_t_quantiles(
    loc: np.ndarray,
    scale: np.ndarray,
    df: np.ndarray,
    probabilities: np.ndarray,
) -> np.ndarray:
    loc = np.asarray(loc, dtype=float)
    scale = np.asarray(scale, dtype=float)
    df = np.asarray(df, dtype=float)
    probabilities = np.asarray(probabilities, dtype=float)
    if (scale <= 0).any() or (df <= 2.01).any():
        raise ContractViolation("Student-t quantiles require scale > 0 and df > 2.01")
    quantiles = stats.t.ppf(
        probabilities.reshape(1, -1),
        df=df.reshape(-1, 1),
        loc=loc.reshape(-1, 1),
        scale=scale.reshape(-1, 1),
    )
    if not np.isfinite(quantiles).all() or (np.diff(quantiles, axis=1) < 0).any():
        raise ContractViolation("Student-t quantile calculation failed")
    return quantiles


def quantile_grid_crps(
    truth: np.ndarray,
    loc: np.ndarray,
    scale: np.ndarray,
    df: np.ndarray,
    *,
    probabilities: np.ndarray | None = None,
) -> np.ndarray:
    """Fixed-grid approximation of CRPS via integrated pinball loss."""

    tau = np.asarray(
        probabilities if probabilities is not None else np.arange(0.01, 1.00, 0.01),
        dtype=float,
    )
    if tau.shape != (99,) or not np.allclose(tau, np.arange(0.01, 1.00, 0.01)):
        raise ContractViolation("Gate C1 CRPS grid must be exactly 0.01...0.99")
    quantiles = student_t_quantiles(loc, scale, df, tau)
    residual = np.asarray(truth, dtype=float).reshape(-1, 1) - quantiles
    pinball = np.maximum(tau.reshape(1, -1) * residual, (tau.reshape(1, -1) - 1.0) * residual)
    # Trapezoidal integration uses the preregistered internal grid only.
    return 2.0 * np.trapezoid(pinball, tau, axis=1)


def student_t_nll(
    truth: np.ndarray,
    loc: np.ndarray,
    scale: np.ndarray,
    df: np.ndarray,
) -> np.ndarray:
    values = -stats.t.logpdf(
        np.asarray(truth, dtype=float),
        df=np.asarray(df, dtype=float),
        loc=np.asarray(loc, dtype=float),
        scale=np.asarray(scale, dtype=float),
    )
    if not np.isfinite(values).all():
        raise ContractViolation("Student-t NLL is non-finite")
    return values
