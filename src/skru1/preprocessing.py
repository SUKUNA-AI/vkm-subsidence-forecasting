"""Train-only preprocessing for manifest-governed model frames."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from .data_contracts import FeatureContract
from .leakage import LeakageViolation, assert_estimator_feature_safety
from .splits import ManifestDataset


MISSING_CATEGORY = "__MISSING__"
UNKNOWN_CATEGORY = "__UNKNOWN__"


@dataclass
class TrainOnlyPreprocessor:
    """Median/scale numeric fields and one-hot encode categoricals.

    Fitting requires a :class:`~skru1.splits.ManifestDataset` whose provenance
    says ``train``. This makes the train-only rule a runtime invariant instead
    of a comment in a notebook.
    """

    contract: FeatureContract
    numeric_columns_: tuple[str, ...] = field(default_factory=tuple, init=False)
    categorical_columns_: tuple[str, ...] = field(default_factory=tuple, init=False)
    numeric_medians_: dict[str, float] = field(default_factory=dict, init=False)
    numeric_means_: dict[str, float] = field(default_factory=dict, init=False)
    numeric_scales_: dict[str, float] = field(default_factory=dict, init=False)
    numeric_missing_indicators_: tuple[str, ...] = field(default_factory=tuple, init=False)
    categorical_levels_: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    feature_names_out_: tuple[str, ...] = field(default_factory=tuple, init=False)
    fitted_train_sample_hash_: str | None = field(default=None, init=False)

    @property
    def is_fitted(self) -> bool:
        return self.fitted_train_sample_hash_ is not None

    def fit(self, dataset: ManifestDataset) -> "TrainOnlyPreprocessor":
        if not isinstance(dataset, ManifestDataset):
            raise TypeError("Preprocessing fit requires a ManifestDataset loaded from a frozen manifest")
        if dataset.provenance.split != "train":
            raise LeakageViolation(
                f"Preprocessing may be fit only on train, got {dataset.provenance.task}/{dataset.provenance.split}"
            )
        assert_estimator_feature_safety(dataset.feature_columns, self.contract)
        features = dataset.frame.loc[:, dataset.feature_columns].copy()
        numeric = tuple(column for column in features if is_numeric_dtype(features[column]))
        categorical = tuple(column for column in features if column not in numeric)

        medians: dict[str, float] = {}
        means: dict[str, float] = {}
        scales: dict[str, float] = {}
        missing_indicators: list[str] = []
        for column in numeric:
            values = pd.to_numeric(features[column], errors="coerce")
            median = float(values.median()) if values.notna().any() else 0.0
            imputed = values.fillna(median).astype(float)
            mean = float(imputed.mean())
            scale = float(imputed.std(ddof=0))
            if not np.isfinite(scale) or scale == 0.0:
                scale = 1.0
            medians[column] = median
            means[column] = mean
            scales[column] = scale
            if values.isna().any():
                missing_indicators.append(column)

        levels: dict[str, tuple[str, ...]] = {}
        for column in categorical:
            values = features[column].astype("string").fillna(MISSING_CATEGORY)
            observed = sorted(set(values.astype(str)))
            if UNKNOWN_CATEGORY not in observed:
                observed.append(UNKNOWN_CATEGORY)
            levels[column] = tuple(observed)

        names: list[str] = []
        for column in numeric:
            names.append(column)
            if column in missing_indicators:
                names.append(f"{column}__missing")
        for column in categorical:
            names.extend(f"{column}=={level}" for level in levels[column])

        self.numeric_columns_ = numeric
        self.categorical_columns_ = categorical
        self.numeric_medians_ = medians
        self.numeric_means_ = means
        self.numeric_scales_ = scales
        self.numeric_missing_indicators_ = tuple(missing_indicators)
        self.categorical_levels_ = levels
        self.feature_names_out_ = tuple(names)
        self.fitted_train_sample_hash_ = dataset.provenance.sample_ids_sha256
        return self

    def transform(self, dataset: ManifestDataset) -> pd.DataFrame:
        if not self.is_fitted:
            raise RuntimeError("Preprocessor must be fit on a train manifest before transform")
        if not isinstance(dataset, ManifestDataset):
            raise TypeError("Preprocessing transform requires a ManifestDataset")
        assert_estimator_feature_safety(dataset.feature_columns, self.contract)
        features = dataset.frame.loc[:, dataset.feature_columns]
        output: dict[str, pd.Series] = {}

        for column in self.numeric_columns_:
            values = pd.to_numeric(features[column], errors="coerce")
            missing = values.isna()
            imputed = values.fillna(self.numeric_medians_[column]).astype(float)
            output[column] = (imputed - self.numeric_means_[column]) / self.numeric_scales_[column]
            if column in self.numeric_missing_indicators_:
                output[f"{column}__missing"] = missing.astype(float)

        for column in self.categorical_columns_:
            values = features[column].astype("string").fillna(MISSING_CATEGORY).astype(str)
            known = set(self.categorical_levels_[column]) - {UNKNOWN_CATEGORY}
            normalized = values.where(values.isin(known), UNKNOWN_CATEGORY)
            for level in self.categorical_levels_[column]:
                output[f"{column}=={level}"] = normalized.eq(level).astype(float)

        transformed = pd.DataFrame(output, index=dataset.frame.index)
        transformed.insert(0, "sample_id", dataset.frame["sample_id"].astype(str).to_numpy())
        actual = tuple(column for column in transformed.columns if column != "sample_id")
        if actual != self.feature_names_out_:
            raise RuntimeError("Preprocessor output schema changed after fit")
        return transformed

    def fit_transform(self, dataset: ManifestDataset) -> pd.DataFrame:
        return self.fit(dataset).transform(dataset)

    def state_dict(self) -> dict[str, Any]:
        if not self.is_fitted:
            raise RuntimeError("Cannot serialize an unfitted preprocessor")
        return {
            "train_sample_ids_sha256": self.fitted_train_sample_hash_,
            "feature_contract_sha256": self.contract.source_sha256,
            "numeric_columns": list(self.numeric_columns_),
            "categorical_columns": list(self.categorical_columns_),
            "numeric_medians": self.numeric_medians_,
            "numeric_means": self.numeric_means_,
            "numeric_scales": self.numeric_scales_,
            "numeric_missing_indicators": list(self.numeric_missing_indicators_),
            "categorical_levels": {key: list(value) for key, value in self.categorical_levels_.items()},
            "feature_names_out": list(self.feature_names_out_),
        }
