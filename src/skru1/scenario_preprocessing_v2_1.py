"""Preprocessing only; fit scope is selected internally from a frozen fold record."""
from __future__ import annotations

from dataclasses import replace
import numpy as np
import pandas as pd

from .scenario_adapter_v2_1 import FEATURES, estimator_matrix
from .scenario_boundary_v2_1 import json_sha256, DATA_SHA
from .scenario_sequences_v2_1 import CHANNELS, SequenceBatch
from .scenario_splits_v2_1 import fold_datasets, validate_fold
from .splits import sample_id_list_sha256

CATEGORICAL = ("current_campaign_type", "target_campaign_type")


class FoldPreprocessor:
    def __init__(self, kind: str):
        if kind not in {"tabular", "sequence"}:
            raise ValueError("Unknown preprocessing kind")
        self.kind = kind
        self.state = None

    def fit(self, bundle, groups, fold, *, frozen_record: dict, tensorizer=None):
        if self.state is not None:
            raise ValueError("Preprocessor state is single-fit; use a new instance per fold")
        if frozen_record != fold.record():
            raise ValueError("Fold differs from frozen membership/record")
        validate_fold(fold, bundle, groups)
        if self.kind == "tabular":
            fit, _ = fold_datasets(bundle, groups, fold)
            frame = estimator_matrix(fit)
            columns = FEATURES
            numeric = tuple(c for c in columns if c not in CATEGORICAL)
            categorical = CATEGORICAL
        else:
            if tensorizer is None:
                raise ValueError("Sequence preprocessor requires the v2 tensorizer")
            # Bounded per-fold working array, never a serialized all-origin tensor.
            pieces = [batch.values[batch.observation_mask]
                      for batch in tensorizer.batches(fold.fit_ids)]
            frame = pd.DataFrame(np.concatenate(pieces), columns=CHANNELS)
            columns = numeric = CHANNELS
            categorical = ()
        parameters = {}
        for name in numeric:
            values = pd.to_numeric(frame[name], errors="raise")
            if np.isinf(values.to_numpy(float)).any():
                raise ValueError("Infinite preprocessing input")
            median = float(values.median()) if values.notna().any() else 0.0
            filled = values.fillna(median)
            mean = float(filled.mean())
            std = float(filled.std(ddof=0))
            parameters[name] = {"median": median, "mean": mean,
                                "scale": std if np.isfinite(std) and std > 0 else 1.0}
        categories = {name: sorted(frame[name].astype("string").fillna("<MISSING>").unique().tolist())
                      for name in categorical}
        schema = {"authority": bundle.provenance["feature_schema_sha256"], "kind": self.kind,
                  "columns": list(columns), "normalization": "median_then_population_zscore",
                  "categories": "fit_only_unknown_zero"}
        self.state = {
            "creation_identity": {"representation_id": "SKRU1_SCENARIO_REPRESENTATION_V2_1_R1",
                                  "dataset_manifest_sha256": DATA_SHA},
            "kind": self.kind, "fold_id": fold.fold_id,
            "ordered_train_sample_sha256": sample_id_list_sha256(fold.fit_ids),
            "feature_schema_sha256": json_sha256(schema), "schema": schema,
            "fold_record_sha256": json_sha256(frozen_record),
            "fit_origins": len(fold.fit_ids), "fit_rows_or_real_tokens": len(frame),
            "parameters": parameters, "categories": categories,
            "category_mappings": {name: {value: i+1 for i, value in enumerate(values)}
                                  for name, values in categories.items()},
            "unknown_category_code": 0,
        }
        return self

    def _transform_values(self, frame):
        if self.state is None:
            raise ValueError("Unfitted preprocessor")
        columns = self.state["schema"]["columns"]
        if list(frame.columns) != columns:
            raise ValueError("Unexpected preprocessing schema/order")
        output = np.zeros((len(frame), len(columns)), dtype=np.float32)
        for i, name in enumerate(columns):
            if name in self.state["parameters"]:
                p = self.state["parameters"][name]
                values = pd.to_numeric(frame[name], errors="raise").fillna(p["median"])
                output[:, i] = ((values - p["mean"]) / p["scale"]).to_numpy(np.float32)
            else:
                mapping = {value: j+1 for j, value in enumerate(self.state["categories"][name])}
                output[:, i] = frame[name].astype("string").fillna("<MISSING>").map(mapping).fillna(0).to_numpy(np.float32)
        if not np.isfinite(output).all():
            raise ValueError("Nonfinite normalized values")
        return output

    def transform_tabular(self, dataset):
        if self.kind != "tabular":
            raise ValueError("Wrong preprocessing kind")
        return self._transform_values(estimator_matrix(dataset))

    def transform_sequence(self, batch: SequenceBatch) -> SequenceBatch:
        if self.kind != "sequence":
            raise ValueError("Wrong preprocessing kind")
        values = np.zeros(batch.values.shape, dtype=np.float32)
        frame = pd.DataFrame(batch.values[batch.observation_mask], columns=CHANNELS)
        values[batch.observation_mask] = self._transform_values(frame)
        return replace(batch, values=values)

    def state_dict(self):
        if self.state is None:
            raise ValueError("Unfitted preprocessor")
        import copy
        return copy.deepcopy(self.state)
