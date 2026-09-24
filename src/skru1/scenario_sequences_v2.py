"""Lazy tensorization of authoritative frozen history-ID windows, without labels."""
from __future__ import annotations

from dataclasses import dataclass
import json
import numpy as np
import pandas as pd

from .scenario_adapter_v2 import ModelDataBundle, FEATURES

CHANNELS = ("last_settlement_mm", "last_rate_mm_y", "current_standard_uncertainty_mm",
            "days_since_previous_observation", "missing_campaigns_since_previous")
MAX_LENGTH = 16


@dataclass(frozen=True)
class SequenceBatch:
    sample_ids: tuple[str, ...]  # lookup only, never part of values
    values: np.ndarray
    lengths: np.ndarray
    padding_mask: np.ndarray
    observation_mask: np.ndarray
    missing_campaign_mask: np.ndarray
    value_valid_mask: np.ndarray


class SequenceTensorizer:
    def __init__(self, bundle: ModelDataBundle):
        if not set(CHANNELS).issubset(FEATURES):
            raise ValueError("Network channel outside estimator allowlist")
        self.windows = bundle.windows.set_index("sample_id", drop=False)
        self.history = bundle.history.frame.sort_values(["point_id", "current_date"], kind="stable").copy()
        self.history["days_since_previous_observation"] = self.history.groupby("point_id").current_date.diff().dt.days
        self.index = pd.Index(self.history.history_id)
        self.values = self.history.loc[:, CHANNELS].to_numpy(dtype=np.float64)
        self.points = self.history.point_id.to_numpy()
        self.dates = self.history.current_date.to_numpy(dtype="datetime64[ns]")

    def raw(self, sample_ids) -> SequenceBatch:
        ids = tuple(sample_ids)
        if len(ids) != len(set(ids)) or not ids:
            raise ValueError("Empty/duplicate sequence request")
        windows = self.windows.loc[list(ids)]
        index = np.full((len(ids), MAX_LENGTH), -1, dtype=np.int64)
        lengths = windows.sequence_length.to_numpy(dtype=np.int64)
        for i, row in enumerate(windows.itertuples(index=False)):
            history_ids = json.loads(row.history_ids_json)
            n = len(history_ids)
            if not 1 <= n <= MAX_LENGTH or row.sequence_length != n or row.left_padding != MAX_LENGTH-n:
                raise ValueError("Invalid frozen sequence length/padding")
            positions = self.index.get_indexer(history_ids)
            if (positions < 0).any() or len(set(history_ids)) != n:
                raise ValueError("Unknown/duplicate frozen history ID")
            dates = self.dates[positions]
            if not (self.points[positions] == row.point_id).all() or not (np.diff(dates) > np.timedelta64(0, "ns")).all():
                raise ValueError("Sequence point/order mismatch")
            if dates[-1] != np.datetime64(row.current_date, "ns"):
                raise ValueError("Sequence does not end at current observation")
            index[i, -n:] = positions
        padding = index < 0
        values = self.values[index.clip(min=0)].copy()
        values[padding] = 0.0
        observed = ~padding
        valid = np.isfinite(values) & observed[:, :, None]
        missing = (values[:, :, CHANNELS.index("missing_campaigns_since_previous")] > 0) & observed
        return SequenceBatch(ids, values, lengths, padding, observed, missing, valid)

    def batches(self, sample_ids, batch_size=4096):
        ids = tuple(sample_ids)
        for start in range(0, len(ids), batch_size):
            yield self.raw(ids[start:start+batch_size])
