"""Leakage-safe T1 baselines for Gate B0/B1.

Every estimator consumes :class:`~skru1.splits.ManifestDataset` objects and
therefore inherits the executable feature allowlist and manifest provenance.
Identifiers are used only for causal state lookup by the fixed Kalman filter;
they never enter a learned estimator matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.linear_model import Ridge

from .data_contracts import FeatureContract
from .leakage import LeakageViolation, assert_estimator_feature_safety
from .preprocessing import TrainOnlyPreprocessor
from .splits import ManifestDataset


TARGET_COLUMN = "observed_rate_mm_y"
SIGMA_COLUMN = "sigma_rate_mm_y"


class T1Model(Protocol):
    """Common interface used by the controlled evaluation runner."""

    model_id: str
    family: str

    def fit(self, dataset: ManifestDataset) -> "T1Model": ...

    def predict(
        self,
        dataset: ManifestDataset,
        *,
        history_frame: pd.DataFrame | None = None,
    ) -> np.ndarray: ...

    def state_dict(self) -> dict[str, Any]: ...


def train_only_precision_weights(
    frame: pd.DataFrame,
    *,
    lower: float = 0.25,
    upper: float = 4.0,
) -> np.ndarray:
    """Recompute inverse-variance weights inside the supplied fit scope.

    The canonical ``training_weight`` field is deliberately ignored because
    it was normalized before the frozen temporal split. Recomputing from the
    uncertainty column prevents even distribution-level information from a
    later split entering model fit.
    """

    if not 0 < lower <= upper:
        raise ValueError("Weight clipping bounds must satisfy 0 < lower <= upper")
    if SIGMA_COLUMN not in frame:
        return np.ones(len(frame), dtype=float)
    sigma = pd.to_numeric(frame[SIGMA_COLUMN], errors="coerce").to_numpy(float)
    valid = np.isfinite(sigma) & (sigma > 0)
    if not valid.any():
        return np.ones(len(frame), dtype=float)
    fallback = float(np.median(sigma[valid]))
    sigma = np.where(valid, sigma, fallback)
    variance = np.square(sigma)
    reference = float(np.median(variance))
    weights = np.clip(reference / variance, lower, upper)
    return weights / float(np.mean(weights))


@dataclass
class PersistenceLastRate:
    model_id: str
    parameters: Mapping[str, Any]
    family: str = "persistence"
    fallback_rate_: float | None = None

    def fit(self, dataset: ManifestDataset) -> "PersistenceLastRate":
        _require_training_dataset(dataset)
        self.fallback_rate_ = _target_median(dataset.frame)
        return self

    def predict(
        self,
        dataset: ManifestDataset,
        *,
        history_frame: pd.DataFrame | None = None,
    ) -> np.ndarray:
        del history_frame
        _require_fitted(self.fallback_rate_, self.model_id)
        values = pd.to_numeric(dataset.frame["last_rate_mm_y"], errors="coerce").to_numpy(float)
        return np.where(np.isfinite(values), values, float(self.fallback_rate_))

    def state_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "family": self.family,
            "parameters": dict(self.parameters),
            "fallback_rate": self.fallback_rate_,
            "parameter_count": 1,
        }


@dataclass
class ProfileAwareRobustTrend:
    model_id: str
    parameters: Mapping[str, Any]
    family: str = "profile_robust_trend"
    fallback_rate_: float | None = None
    acceleration_lower_: float | None = None
    acceleration_upper_: float | None = None

    def fit(self, dataset: ManifestDataset) -> "ProfileAwareRobustTrend":
        _require_training_dataset(dataset)
        self.fallback_rate_ = _target_median(dataset.frame)
        quantiles = tuple(float(value) for value in self.parameters["acceleration_clip_quantiles"])
        if len(quantiles) != 2 or not 0 <= quantiles[0] < quantiles[1] <= 1:
            raise ValueError("acceleration_clip_quantiles must contain two ordered probabilities")
        acceleration = pd.to_numeric(
            dataset.frame["recent_acceleration_mm_y2"], errors="coerce"
        ).dropna()
        if acceleration.empty:
            self.acceleration_lower_ = 0.0
            self.acceleration_upper_ = 0.0
        else:
            self.acceleration_lower_ = float(acceleration.quantile(quantiles[0]))
            self.acceleration_upper_ = float(acceleration.quantile(quantiles[1]))
        return self

    def predict(
        self,
        dataset: ManifestDataset,
        *,
        history_frame: pd.DataFrame | None = None,
    ) -> np.ndarray:
        del history_frame
        _require_fitted(self.fallback_rate_, self.model_id)
        frame = dataset.frame
        last_rate = pd.to_numeric(frame["last_rate_mm_y"], errors="coerce").to_numpy(float)
        mean_last_three = pd.to_numeric(
            frame["mean_last_3_rates_mm_y"], errors="coerce"
        ).to_numpy(float)
        profile_mean = pd.to_numeric(
            frame["profile_mean_rate_mm_y"], errors="coerce"
        ).to_numpy(float)
        acceleration = pd.to_numeric(
            frame["recent_acceleration_mm_y2"], errors="coerce"
        ).to_numpy(float)
        acceleration = np.clip(
            acceleration,
            float(self.acceleration_lower_),
            float(self.acceleration_upper_),
        )
        horizon_years = (
            pd.to_numeric(frame["forecast_horizon_days"], errors="coerce").to_numpy(float)
            / 365.25
        )
        trend = last_rate + (
            float(self.parameters["acceleration_integral_factor"])
            * acceleration
            * horizon_years
        )
        values = np.column_stack([trend, mean_last_three, profile_mean])
        weights = np.asarray(
            [
                self.parameters["instantaneous_weight"],
                self.parameters["mean_last_3_weight"],
                self.parameters["profile_mean_weight"],
            ],
            dtype=float,
        )
        if (weights < 0).any() or not np.isclose(weights.sum(), 1.0):
            raise ValueError("Robust-trend component weights must be non-negative and sum to one")
        finite = np.isfinite(values)
        numerator = np.nansum(values * weights, axis=1)
        denominator = np.sum(finite * weights, axis=1)
        prediction = np.divide(
            numerator,
            denominator,
            out=np.full(len(frame), float(self.fallback_rate_)),
            where=denominator > 0,
        )
        return prediction

    def state_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "family": self.family,
            "parameters": dict(self.parameters),
            "fallback_rate": self.fallback_rate_,
            "acceleration_lower": self.acceleration_lower_,
            "acceleration_upper": self.acceleration_upper_,
            "parameter_count": 3,
        }


@dataclass
class FixedKalmanRate:
    model_id: str
    parameters: Mapping[str, Any]
    family: str = "fixed_kalman"
    fallback_rate_: float | None = None

    def fit(self, dataset: ManifestDataset) -> "FixedKalmanRate":
        _require_training_dataset(dataset)
        self.fallback_rate_ = _target_median(dataset.frame)
        return self

    def predict(
        self,
        dataset: ManifestDataset,
        *,
        history_frame: pd.DataFrame | None = None,
    ) -> np.ndarray:
        _require_fitted(self.fallback_rate_, self.model_id)
        if history_frame is None:
            raise ValueError("Fixed Kalman prediction requires a non-test causal feature history")
        required = {
            "point_id",
            "current_date",
            "last_settlement_mm",
            "current_standard_uncertainty_mm",
        }
        missing = required - set(history_frame)
        if missing:
            raise ValueError(f"Kalman history is missing columns: {sorted(missing)}")
        history = history_frame.loc[:, sorted(required)].copy()
        history["current_date"] = pd.to_datetime(history["current_date"], errors="coerce")
        if history["current_date"].isna().any():
            raise ValueError("Kalman history contains invalid current_date values")
        histories = {
            str(point_id): group.sort_values(["current_date"], kind="mergesort")
            for point_id, group in history.groupby("point_id", sort=False)
        }
        predictions: list[float] = []
        for row in dataset.frame.itertuples(index=False):
            point_history = histories.get(str(row.point_id))
            fallback = _finite_or(row.last_rate_mm_y, float(self.fallback_rate_))
            if point_history is None:
                predictions.append(fallback)
                continue
            causal = point_history.loc[
                point_history["current_date"].le(pd.Timestamp(row.current_date))
            ]
            predictions.append(self._filter_rate(causal, fallback))
        return np.asarray(predictions, dtype=float)

    def _filter_rate(self, history: pd.DataFrame, fallback_rate: float) -> float:
        history = history.copy()
        history["settlement"] = pd.to_numeric(
            history["last_settlement_mm"], errors="coerce"
        )
        history["sigma"] = pd.to_numeric(
            history["current_standard_uncertainty_mm"], errors="coerce"
        )
        history = history.loc[history["settlement"].notna()].drop_duplicates(
            "current_date", keep="last"
        )
        if len(history) < 2:
            return fallback_rate
        q = float(self.parameters["process_variance_q"])
        velocity_variance = float(self.parameters["initial_velocity_variance"])
        minimum_r = float(self.parameters["minimum_measurement_variance"])
        if q < 0 or velocity_variance <= 0 or minimum_r <= 0:
            raise ValueError("Kalman variances must be positive (q may be zero)")
        first = history.iloc[0]
        first_sigma = _finite_or(first["sigma"], minimum_r**0.5)
        state = np.asarray([float(first["settlement"]), float(fallback_rate)], dtype=float)
        covariance = np.diag([max(first_sigma**2, minimum_r), velocity_variance])
        previous_date = pd.Timestamp(first["current_date"])
        observation_matrix = np.asarray([[1.0, 0.0]])
        identity = np.eye(2)
        for current in history.iloc[1:].itertuples(index=False):
            current_date = pd.Timestamp(current.current_date)
            delta_years = max((current_date - previous_date).days / 365.25, 1.0 / 365.25)
            transition = np.asarray([[1.0, delta_years], [0.0, 1.0]])
            process = q * np.asarray(
                [
                    [delta_years**3 / 3.0, delta_years**2 / 2.0],
                    [delta_years**2 / 2.0, delta_years],
                ]
            )
            state = transition @ state
            covariance = transition @ covariance @ transition.T + process
            sigma = _finite_or(current.sigma, minimum_r**0.5)
            measurement_variance = max(sigma**2, minimum_r)
            innovation = float(current.settlement) - float((observation_matrix @ state)[0])
            innovation_variance = float(
                (observation_matrix @ covariance @ observation_matrix.T)[0, 0]
                + measurement_variance
            )
            gain = (covariance @ observation_matrix.T / innovation_variance).ravel()
            state = state + gain * innovation
            covariance = (identity - np.outer(gain, observation_matrix.ravel())) @ covariance
            previous_date = current_date
        return float(state[1]) if np.isfinite(state[1]) else fallback_rate

    def state_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "family": self.family,
            "parameters": dict(self.parameters),
            "fallback_rate": self.fallback_rate_,
            "parameter_count": 0,
        }


@dataclass
class RidgeRateRegressor:
    model_id: str
    parameters: Mapping[str, Any]
    contract: FeatureContract
    weight_clip: tuple[float, float]
    family: str = "ridge"
    preprocessor_: TrainOnlyPreprocessor | None = None
    estimator_: Ridge | None = None

    def fit(self, dataset: ManifestDataset) -> "RidgeRateRegressor":
        _require_training_dataset(dataset)
        preprocessor = TrainOnlyPreprocessor(self.contract)
        matrix = preprocessor.fit_transform(dataset).drop(columns="sample_id").to_numpy(float)
        target = _target(dataset.frame)
        weights = train_only_precision_weights(
            dataset.frame, lower=self.weight_clip[0], upper=self.weight_clip[1]
        )
        estimator = Ridge(
            alpha=float(self.parameters["alpha"]),
            solver=str(self.parameters["solver"]),
            tol=float(self.parameters["tolerance"]),
        )
        estimator.fit(matrix, target, sample_weight=weights)
        self.preprocessor_ = preprocessor
        self.estimator_ = estimator
        return self

    def predict(
        self,
        dataset: ManifestDataset,
        *,
        history_frame: pd.DataFrame | None = None,
    ) -> np.ndarray:
        del history_frame
        if self.preprocessor_ is None or self.estimator_ is None:
            raise RuntimeError(f"Model is not fitted: {self.model_id}")
        matrix = self.preprocessor_.transform(dataset).drop(columns="sample_id").to_numpy(float)
        return np.asarray(self.estimator_.predict(matrix), dtype=float)

    def state_dict(self) -> dict[str, Any]:
        if self.preprocessor_ is None or self.estimator_ is None:
            raise RuntimeError(f"Model is not fitted: {self.model_id}")
        return {
            "model_id": self.model_id,
            "family": self.family,
            "parameters": dict(self.parameters),
            "parameter_count": int(np.asarray(self.estimator_.coef_).size + 1),
            "feature_count": len(self.preprocessor_.feature_names_out_),
            "train_sample_ids_sha256": self.preprocessor_.fitted_train_sample_hash_,
        }


@dataclass
class ExtraTreesRateRegressor:
    model_id: str
    parameters: Mapping[str, Any]
    contract: FeatureContract
    weight_clip: tuple[float, float]
    random_seed: int
    family: str = "extra_trees"
    preprocessor_: TrainOnlyPreprocessor | None = None
    estimator_: ExtraTreesRegressor | None = None

    def fit(self, dataset: ManifestDataset) -> "ExtraTreesRateRegressor":
        _require_training_dataset(dataset)
        preprocessor = TrainOnlyPreprocessor(self.contract)
        matrix = preprocessor.fit_transform(dataset).drop(columns="sample_id").to_numpy(float)
        target = _target(dataset.frame)
        weights = train_only_precision_weights(
            dataset.frame, lower=self.weight_clip[0], upper=self.weight_clip[1]
        )
        estimator = ExtraTreesRegressor(
            n_estimators=int(self.parameters["n_estimators"]),
            max_depth=int(self.parameters["max_depth"]),
            min_samples_leaf=int(self.parameters["min_samples_leaf"]),
            max_features=float(self.parameters["max_features"]),
            bootstrap=bool(self.parameters["bootstrap"]),
            n_jobs=int(self.parameters["n_jobs"]),
            random_state=self.random_seed,
        )
        estimator.fit(matrix, target, sample_weight=weights)
        self.preprocessor_ = preprocessor
        self.estimator_ = estimator
        return self

    def predict(
        self,
        dataset: ManifestDataset,
        *,
        history_frame: pd.DataFrame | None = None,
    ) -> np.ndarray:
        del history_frame
        if self.preprocessor_ is None or self.estimator_ is None:
            raise RuntimeError(f"Model is not fitted: {self.model_id}")
        matrix = self.preprocessor_.transform(dataset).drop(columns="sample_id").to_numpy(float)
        return np.asarray(self.estimator_.predict(matrix), dtype=float)

    def state_dict(self) -> dict[str, Any]:
        if self.preprocessor_ is None or self.estimator_ is None:
            raise RuntimeError(f"Model is not fitted: {self.model_id}")
        node_count = int(sum(tree.tree_.node_count for tree in self.estimator_.estimators_))
        return {
            "model_id": self.model_id,
            "family": self.family,
            "parameters": dict(self.parameters),
            "parameter_count": node_count,
            "tree_count": len(self.estimator_.estimators_),
            "feature_count": len(self.preprocessor_.feature_names_out_),
            "train_sample_ids_sha256": self.preprocessor_.fitted_train_sample_hash_,
        }


def build_model(
    model_spec: Mapping[str, Any],
    *,
    contract: FeatureContract,
    random_seed: int,
    weight_clip: tuple[float, float],
) -> T1Model:
    """Build one model only from a governed, serializable specification."""

    model_id = str(model_spec["model_id"])
    family = str(model_spec["family"])
    parameters = dict(model_spec.get("parameters", {}))
    if family == "persistence":
        return PersistenceLastRate(model_id=model_id, parameters=parameters)
    if family == "profile_robust_trend":
        return ProfileAwareRobustTrend(model_id=model_id, parameters=parameters)
    if family == "fixed_kalman":
        return FixedKalmanRate(model_id=model_id, parameters=parameters)
    if family == "ridge":
        return RidgeRateRegressor(
            model_id=model_id,
            parameters=parameters,
            contract=contract,
            weight_clip=weight_clip,
        )
    if family == "extra_trees":
        return ExtraTreesRateRegressor(
            model_id=model_id,
            parameters=parameters,
            contract=contract,
            weight_clip=weight_clip,
            random_seed=random_seed,
        )
    raise KeyError(f"Unknown Gate B0/B1 model family: {family}")


def _require_training_dataset(dataset: ManifestDataset) -> None:
    if not isinstance(dataset, ManifestDataset):
        raise TypeError("Model fit requires a ManifestDataset")
    if dataset.provenance.split != "train":
        raise LeakageViolation(
            f"Model fit requires train provenance, got {dataset.provenance.task}/{dataset.provenance.split}"
        )
    assert_estimator_feature_safety(dataset.feature_columns, _contract_from_dataset(dataset))
    _target(dataset.frame)


def _contract_from_dataset(dataset: ManifestDataset) -> FeatureContract:
    """Return a minimal contract proxy only for exact-column guard reuse.

    The model builders already hold the real contract for learned estimators.
    Baselines need an equivalent exact-allowlist assertion without carrying a
    second serialized contract object.
    """

    table = pd.DataFrame(
        {
            "field": list(dataset.feature_columns),
            "role": ["MODEL_FEATURE"] * len(dataset.feature_columns),
            "allowed": [True] * len(dataset.feature_columns),
            "reason": ["manifest dataset allowlist"] * len(dataset.feature_columns),
        }
    )
    return FeatureContract(table=table, source_path=dataset.provenance.manifest_path, source_sha256="runtime")


def _target(frame: pd.DataFrame) -> np.ndarray:
    if TARGET_COLUMN not in frame:
        raise LeakageViolation(f"Training/evaluation frame has no T1 target: {TARGET_COLUMN}")
    target = pd.to_numeric(frame[TARGET_COLUMN], errors="coerce").to_numpy(float)
    if not np.isfinite(target).all():
        raise LeakageViolation("T1 training/evaluation target contains missing or non-finite values")
    return target


def _target_median(frame: pd.DataFrame) -> float:
    return float(np.median(_target(frame)))


def _require_fitted(value: float | None, model_id: str) -> None:
    if value is None or not np.isfinite(value):
        raise RuntimeError(f"Model is not fitted: {model_id}")


def _finite_or(value: Any, fallback: float) -> float:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return float(numeric) if pd.notna(numeric) and np.isfinite(float(numeric)) else float(fallback)

