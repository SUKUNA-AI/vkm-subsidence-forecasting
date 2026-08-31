"""Protocol-guarded model adapters used by isolated Gate B6 workers."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from .baselines import TARGET_COLUMN, build_model, train_only_precision_weights
from .benchmarking import (
    ModelSpec,
    apply_feature_view,
    assert_structural_keys_not_in_x,
    feature_view_contract,
)
from .data_contracts import ContractViolation, FeatureContract
from .leakage import LeakageViolation, assert_estimator_feature_safety
from .preprocessing import MISSING_CATEGORY, UNKNOWN_CATEGORY, TrainOnlyPreprocessor
from .splits import ManifestDataset


@dataclass(frozen=True)
class AdapterPrediction:
    mean: np.ndarray
    predictive_std: np.ndarray | None = None
    quantiles: Mapping[float, np.ndarray] = field(default_factory=dict)
    distribution_family: str | None = None
    distribution_parameters: Mapping[str, np.ndarray] = field(default_factory=dict)

    def validate(self, rows: int) -> "AdapterPrediction":
        mean = np.asarray(self.mean, dtype=float)
        if mean.shape != (rows,) or not np.isfinite(mean).all():
            raise RuntimeError("Adapter returned invalid point predictions")
        if self.predictive_std is not None:
            std = np.asarray(self.predictive_std, dtype=float)
            if std.shape != (rows,) or not np.isfinite(std).all() or (std <= 0).any():
                raise RuntimeError("Adapter returned invalid predictive standard deviations")
        for level, values in self.quantiles.items():
            quantile = np.asarray(values, dtype=float)
            if not 0 < float(level) < 1 or quantile.shape != (rows,) or not np.isfinite(quantile).all():
                raise RuntimeError("Adapter returned invalid predictive quantiles")
        return self


@dataclass
class FrozenAdapterEnsemble:
    """Serializable full-train artifact for one or several frozen seeds."""

    model_id: str
    environment_id: str
    adapters: tuple[Any, ...]
    seeds: tuple[int, ...]

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        predictions = [adapter.predict(dataset) for adapter in self.adapters]
        mean_stack = np.vstack([prediction.mean for prediction in predictions])
        mean = np.mean(mean_stack, axis=0)
        std = None
        if all(prediction.predictive_std is not None for prediction in predictions):
            second_moment = np.mean(
                np.vstack(
                    [
                        np.asarray(prediction.predictive_std, float) ** 2
                        + np.asarray(prediction.mean, float) ** 2
                        for prediction in predictions
                    ]
                ),
                axis=0,
            )
            std = np.sqrt(np.maximum(second_moment - mean**2, 1e-16))
        common_levels = set(predictions[0].quantiles)
        for prediction in predictions[1:]:
            common_levels &= set(prediction.quantiles)
        quantiles = {
            level: np.mean(
                np.vstack([np.asarray(prediction.quantiles[level], float) for prediction in predictions]),
                axis=0,
            )
            for level in sorted(common_levels)
        }
        families = {prediction.distribution_family for prediction in predictions}
        family = families.pop() if len(families) == 1 else "fixed_seed_ensemble"
        return AdapterPrediction(
            mean=mean,
            predictive_std=std,
            quantiles=quantiles,
            distribution_family=family,
        ).validate(len(dataset.frame))


class B6Adapter:
    def __init__(
        self,
        spec: ModelSpec,
        parameters: Mapping[str, Any],
        *,
        contract: FeatureContract,
        seed: int,
    ) -> None:
        self.spec = spec
        self.parameters = dict(parameters)
        self.contract = contract
        self.view_contract = feature_view_contract(spec.feature_view, contract)
        self.seed = int(seed)
        self.effective_iterations_: int | None = None

    @property
    def model_id(self) -> str:
        return self.spec.model_id

    @property
    def family(self) -> str:
        return self.spec.family

    def fit(
        self,
        train: ManifestDataset,
        *,
        validation: ManifestDataset | None = None,
    ) -> "B6Adapter":
        raise NotImplementedError

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        raise NotImplementedError

    def state_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "family": self.family,
            "parameters": self.parameters,
            "seed": self.seed,
            "effective_iterations": self.effective_iterations_,
        }

    def _view(self, dataset: ManifestDataset) -> ManifestDataset:
        return apply_feature_view(dataset, view_name=self.spec.feature_view, contract=self.contract)


class FrozenComparatorAdapter(B6Adapter):
    """Adapter around immutable B1/B3/B5/B6/B7/B8/M1/M2 specifications."""

    def __init__(self, *args: Any, raw_history: pd.DataFrame, prepared_history: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.raw_history = raw_history
        self.prepared_history = prepared_history
        self.model: Any | None = None

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "FrozenComparatorAdapter":
        del validation
        from .adaptive_kalman import AdaptiveKalmanRate
        from .imm_kalman import TwoRegimeIMMRate
        from .robust_imm import RobustInnovationIMMRate

        family = self.family
        if family in {"persistence", "profile_robust_trend", "fixed_kalman", "ridge", "extra_trees"}:
            model = build_model(
                {"model_id": self.model_id, "family": family, "parameters": self.parameters},
                contract=self.contract,
                random_seed=self.seed,
                weight_clip=(0.25, 4.0),
            )
        elif family == "adaptive_kalman":
            model = AdaptiveKalmanRate(model_id=self.model_id, parameters=self.parameters)
        elif family == "imm_damped_acceleration":
            model = TwoRegimeIMMRate(model_id=self.model_id, parameters=self.parameters)
        elif family == "imm_student_t_robust_observation":
            model = RobustInnovationIMMRate(model_id=self.model_id, parameters=self.parameters)
        else:
            raise KeyError(family)
        self.model = model.fit(train)
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if self.model is None:
            raise RuntimeError("Frozen comparator is not fitted")
        if hasattr(self.model, "predict_distribution"):
            mean, std, _ = self.model.predict_distribution(dataset, history_frame=self.prepared_history)
            return AdapterPrediction(
                mean=np.asarray(mean, float),
                predictive_std=np.maximum(np.asarray(std, float), 1e-8),
                distribution_family="native_state_space_scale",
            ).validate(len(dataset.frame))
        mean = self.model.predict(dataset, history_frame=self.raw_history)
        return AdapterPrediction(mean=np.asarray(mean, float)).validate(len(dataset.frame))

    def state_dict(self) -> dict[str, Any]:
        state = super().state_dict()
        if self.model is not None and hasattr(self.model, "state_dict"):
            state["model_state"] = self.model.state_dict()
        return state


class OneHotSklearnAdapter(B6Adapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preprocessor: TrainOnlyPreprocessor | None = None
        self.estimator: Any | None = None

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "OneHotSklearnAdapter":
        from sklearn.ensemble import HistGradientBoostingRegressor
        from sklearn.linear_model import ElasticNet, HuberRegressor
        from sklearn.svm import SVR

        train_view = self._view(train)
        preprocessor = TrainOnlyPreprocessor(self.view_contract)
        matrix = preprocessor.fit_transform(train_view).drop(columns="sample_id").to_numpy(float)
        target = _target(train_view)
        weights = train_only_precision_weights(train_view.frame, lower=0.25, upper=4.0)
        if self.family == "elastic_net":
            estimator = ElasticNet(
                alpha=float(self.parameters["alpha"]),
                l1_ratio=float(self.parameters["l1_ratio"]),
                max_iter=20000,
                tol=1e-8,
                random_state=self.seed,
            )
            estimator.fit(matrix, target, sample_weight=weights)
        elif self.family == "huber":
            estimator = HuberRegressor(
                epsilon=float(self.parameters["epsilon"]),
                alpha=float(self.parameters["alpha"]),
                max_iter=2000,
                tol=1e-8,
            )
            estimator.fit(matrix, target, sample_weight=weights)
        elif self.family == "rbf_svr":
            estimator = SVR(
                kernel="rbf",
                C=float(self.parameters["C"]),
                gamma=self.parameters["gamma"],
                epsilon=float(self.parameters["epsilon"]),
            )
            estimator.fit(matrix, target, sample_weight=weights)
        elif self.family == "hist_gradient_boosting":
            from threadpoolctl import threadpool_limits

            frozen_iterations = self.parameters.get("frozen_iterations")
            estimator = HistGradientBoostingRegressor(
                loss=str(self.parameters["loss"]),
                max_leaf_nodes=int(self.parameters["max_leaf_nodes"]),
                learning_rate=float(self.parameters["learning_rate"]),
                l2_regularization=float(self.parameters["l2_regularization"]),
                max_iter=int(frozen_iterations or self.parameters["max_iter"]),
                early_stopping=validation is not None and frozen_iterations is None,
                validation_fraction=None,
                n_iter_no_change=int(self.parameters["early_stopping_rounds"]),
                random_state=self.seed,
            )
            fit_kwargs: dict[str, Any] = {"sample_weight": weights}
            if validation is not None and frozen_iterations is None:
                validation_view = self._view(validation)
                validation_matrix = preprocessor.transform(validation_view).drop(columns="sample_id").to_numpy(float)
                fit_kwargs.update(
                    {
                        "X_val": validation_matrix,
                        "y_val": _target(validation_view),
                        "sample_weight_val": _weights_from_train(
                            train_view.frame, validation_view.frame
                        ),
                    }
                )
            with threadpool_limits(limits=1):
                estimator.fit(matrix, target, **fit_kwargs)
            self.effective_iterations_ = int(estimator.n_iter_)
        else:
            raise KeyError(self.family)
        self.preprocessor = preprocessor
        self.estimator = estimator
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if self.preprocessor is None or self.estimator is None:
            raise RuntimeError("Adapter is not fitted")
        view = self._view(dataset)
        matrix = self.preprocessor.transform(view).drop(columns="sample_id").to_numpy(float)
        return AdapterPrediction(mean=np.asarray(self.estimator.predict(matrix), float)).validate(len(view.frame))


class GaussianProcessAdapter(B6Adapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preprocessor: TrainOnlyPreprocessor | None = None
        self.estimator: Any | None = None

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "GaussianProcessAdapter":
        del validation
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import ConstantKernel, Matern, RBF

        train_view = self._view(train)
        preprocessor = TrainOnlyPreprocessor(self.view_contract)
        matrix = preprocessor.fit_transform(train_view).drop(columns="sample_id").to_numpy(float)
        dimensions = matrix.shape[1]
        if self.parameters["kernel"] == "constant_times_rbf":
            base = RBF(length_scale=np.ones(dimensions), length_scale_bounds=(1e-2, 1e2))
        elif self.parameters["kernel"] == "constant_times_matern_1_5":
            base = Matern(length_scale=np.ones(dimensions), length_scale_bounds=(1e-2, 1e2), nu=1.5)
        else:
            raise KeyError(self.parameters["kernel"])
        kernel = ConstantKernel(1.0, (1e-2, 1e2)) * base
        sigma = pd.to_numeric(train_view.frame["sigma_rate_mm_y"], errors="coerce").to_numpy(float)
        finite = sigma[np.isfinite(sigma) & (sigma > 0)]
        fallback = float(np.median(finite)) if len(finite) else 1.0
        sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, fallback)
        alpha = float(self.parameters["alpha_scale"]) * sigma**2
        estimator = GaussianProcessRegressor(
            kernel=kernel,
            alpha=alpha,
            normalize_y=bool(self.parameters["normalize_y"]),
            n_restarts_optimizer=int(self.parameters["optimizer_restarts"]),
            random_state=self.seed,
        )
        estimator.fit(matrix, _target(train_view))
        self.preprocessor = preprocessor
        self.estimator = estimator
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if self.preprocessor is None or self.estimator is None:
            raise RuntimeError("GPR is not fitted")
        view = self._view(dataset)
        matrix = self.preprocessor.transform(view).drop(columns="sample_id").to_numpy(float)
        mean, std = self.estimator.predict(matrix, return_std=True)
        return AdapterPrediction(
            mean=np.asarray(mean, float),
            predictive_std=np.maximum(np.asarray(std, float), 1e-8),
            distribution_family="normal",
            distribution_parameters={"loc": np.asarray(mean, float), "scale": np.asarray(std, float)},
        ).validate(len(view.frame))


class GaussianGEEAdapter(B6Adapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preprocessor: TrainOnlyPreprocessor | None = None
        self.params_: np.ndarray | None = None
        self.residual_scale_: float | None = None
        self.exog_columns_: tuple[str, ...] = ()
        self.design_rank_: int | None = None
        self.converged_: bool | None = None

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "GaussianGEEAdapter":
        del validation
        try:
            import statsmodels.api as sm
            from statsmodels.genmod import cov_struct
        except ImportError as exc:
            raise RuntimeError("statsmodels is required for Gaussian GEE") from exc
        train_view = self._view(train)
        preprocessor = TrainOnlyPreprocessor(self.view_contract)
        matrix_frame = _gee_identifiable_matrix(
            preprocessor.fit_transform(train_view).drop(columns="sample_id"), preprocessor
        )
        assert_structural_keys_not_in_x(matrix_frame.columns, {"point_id": tuple(train.frame["point_id"])})
        exog_frame = sm.add_constant(matrix_frame, has_constant="add")
        exog_columns = _full_rank_columns(exog_frame)
        exog = exog_frame.loc[:, exog_columns].to_numpy(float)
        groups = train.frame["point_id"].astype(str).to_numpy()
        dates = pd.to_datetime(train.frame["current_date"], errors="raise")
        time = ((dates - dates.min()).dt.days.to_numpy(float) / 365.25).reshape(-1, 1)
        correlation = str(self.parameters["working_correlation"])
        covariance = {
            "Independence": cov_struct.Independence(),
            "Exchangeable": cov_struct.Exchangeable(),
            "AR1": cov_struct.Autoregressive(grid=True),
        }[correlation]
        model = sm.GEE(
            _target(train_view),
            exog,
            groups=groups,
            time=time,
            family=sm.families.Gaussian(sm.families.links.Identity()),
            cov_struct=covariance,
        )
        result = model.fit(maxiter=200)
        self.converged_ = bool(getattr(result, "converged", True))
        if not self.converged_:
            raise RuntimeError("Gaussian GEE did not converge within 200 iterations")
        self.params_ = np.asarray(result.params, dtype=float).copy()
        self.residual_scale_ = max(float(result.scale), 1e-8)
        self.preprocessor = preprocessor
        self.exog_columns_ = tuple(exog_columns)
        self.design_rank_ = int(np.linalg.matrix_rank(exog))
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if self.preprocessor is None or self.params_ is None or self.residual_scale_ is None:
            raise RuntimeError("GEE is not fitted")
        import statsmodels.api as sm

        view = self._view(dataset)
        matrix_frame = _gee_identifiable_matrix(
            self.preprocessor.transform(view).drop(columns="sample_id"), self.preprocessor
        )
        assert_structural_keys_not_in_x(matrix_frame.columns, {"point_id": tuple(dataset.frame["point_id"])})
        exog_frame = sm.add_constant(matrix_frame, has_constant="add")
        exog = exog_frame.loc[:, self.exog_columns_].to_numpy(float)
        mean = np.asarray(exog @ self.params_, float)
        std = np.full(len(mean), math.sqrt(self.residual_scale_), dtype=float)
        return AdapterPrediction(mean=mean, predictive_std=std, distribution_family="normal_marginal").validate(
            len(view.frame)
        )

    def state_dict(self) -> dict[str, Any]:
        state = super().state_dict()
        state["model_state"] = {
            "exog_columns": list(self.exog_columns_),
            "design_rank": self.design_rank_,
            "converged": self.converged_,
            "parameter_count": 0 if self.params_ is None else int(self.params_.size),
        }
        return state


class QuantileHGBAdapter(B6Adapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preprocessor: TrainOnlyPreprocessor | None = None
        self.estimators: dict[float, Any] = {}

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "QuantileHGBAdapter":
        from sklearn.ensemble import HistGradientBoostingRegressor
        from threadpoolctl import threadpool_limits

        train_view = self._view(train)
        preprocessor = TrainOnlyPreprocessor(self.view_contract)
        matrix = preprocessor.fit_transform(train_view).drop(columns="sample_id").to_numpy(float)
        target = _target(train_view)
        weights = train_only_precision_weights(train_view.frame, lower=0.25, upper=4.0)
        validation_view = self._view(validation) if validation is not None else None
        validation_matrix = (
            preprocessor.transform(validation_view).drop(columns="sample_id").to_numpy(float)
            if validation_view is not None
            else None
        )
        frozen_iterations = self.parameters.get("frozen_iterations")
        iterations: list[int] = []
        for quantile in map(float, self.parameters["quantiles"]):
            estimator = HistGradientBoostingRegressor(
                loss="quantile",
                quantile=quantile,
                max_leaf_nodes=int(self.parameters["max_leaf_nodes"]),
                learning_rate=float(self.parameters["learning_rate"]),
                l2_regularization=float(self.parameters["l2_regularization"]),
                max_iter=int(frozen_iterations or self.parameters["max_iter"]),
                early_stopping=validation_view is not None and frozen_iterations is None,
                validation_fraction=None,
                n_iter_no_change=int(self.parameters["early_stopping_rounds"]),
                random_state=self.seed,
            )
            fit_kwargs: dict[str, Any] = {"sample_weight": weights}
            if validation_view is not None and frozen_iterations is None:
                fit_kwargs.update(
                    {
                        "X_val": validation_matrix,
                        "y_val": _target(validation_view),
                        "sample_weight_val": _weights_from_train(
                            train_view.frame, validation_view.frame
                        ),
                    }
                )
            with threadpool_limits(limits=1):
                estimator.fit(matrix, target, **fit_kwargs)
            self.estimators[quantile] = estimator
            iterations.append(int(estimator.n_iter_))
        self.effective_iterations_ = int(np.median(iterations))
        self.preprocessor = preprocessor
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if self.preprocessor is None or not self.estimators:
            raise RuntimeError("Quantile HGB is not fitted")
        view = self._view(dataset)
        matrix = self.preprocessor.transform(view).drop(columns="sample_id").to_numpy(float)
        quantiles = {level: np.asarray(model.predict(matrix), float) for level, model in self.estimators.items()}
        mean = quantiles.get(0.5)
        if mean is None:
            raise RuntimeError("Quantile HGB has no median model")
        return AdapterPrediction(mean=mean, quantiles=quantiles, distribution_family="empirical_quantiles").validate(
            len(view.frame)
        )


class ExternalBoostingAdapter(B6Adapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preprocessor: TrainOnlyPreprocessor | None = None
        self.estimator: Any | None = None

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "ExternalBoostingAdapter":
        train_view = self._view(train)
        preprocessor = TrainOnlyPreprocessor(self.view_contract)
        train_matrix = preprocessor.fit_transform(train_view).drop(columns="sample_id").to_numpy(float)
        target = _target(train_view)
        weights = train_only_precision_weights(train_view.frame, lower=0.25, upper=4.0)
        validation_view = self._view(validation) if validation is not None else None
        validation_matrix = (
            preprocessor.transform(validation_view).drop(columns="sample_id").to_numpy(float)
            if validation_view is not None
            else None
        )
        frozen_iterations = self.parameters.get("frozen_iterations")
        if self.family == "xgboost":
            try:
                from xgboost import XGBRegressor
            except ImportError as exc:
                raise RuntimeError("xgboost is not installed in this worker environment") from exc
            kwargs = {
                "objective": self.parameters["objective"],
                "tree_method": self.parameters["tree_method"],
                "device": self.parameters["device"],
                "max_depth": int(self.parameters["max_depth"]),
                "min_child_weight": float(self.parameters["min_child_weight"]),
                "learning_rate": float(self.parameters["learning_rate"]),
                "subsample": float(self.parameters["subsample"]),
                "colsample_bytree": float(self.parameters["colsample_bytree"]),
                "reg_lambda": float(self.parameters["reg_lambda"]),
                "n_estimators": int(frozen_iterations or self.parameters["n_estimators"]),
                "n_jobs": int(self.parameters["n_jobs"]),
                "random_state": self.seed,
            }
            if validation_view is not None and frozen_iterations is None:
                kwargs["early_stopping_rounds"] = int(self.parameters["early_stopping_rounds"])
            estimator = XGBRegressor(**kwargs)
            fit_kwargs: dict[str, Any] = {"sample_weight": weights, "verbose": False}
            if validation_view is not None and frozen_iterations is None:
                fit_kwargs["eval_set"] = [(validation_matrix, _target(validation_view))]
            estimator.fit(train_matrix, target, **fit_kwargs)
            self.effective_iterations_ = int(
                getattr(estimator, "best_iteration", kwargs["n_estimators"] - 1) + 1
            )
        elif self.family == "lightgbm":
            try:
                import lightgbm as lgb
            except ImportError as exc:
                raise RuntimeError("lightgbm is not installed in this worker environment") from exc
            estimator = lgb.LGBMRegressor(
                objective=self.parameters["objective"],
                device_type=self.parameters["device_type"],
                deterministic=bool(self.parameters["deterministic"]),
                force_col_wise=bool(self.parameters["force_col_wise"]),
                num_leaves=int(self.parameters["num_leaves"]),
                min_child_samples=int(self.parameters["min_child_samples"]),
                reg_lambda=float(self.parameters["reg_lambda"]),
                learning_rate=float(self.parameters["learning_rate"]),
                n_estimators=int(frozen_iterations or self.parameters["n_estimators"]),
                n_jobs=int(self.parameters["n_jobs"]),
                random_state=self.seed,
                verbosity=-1,
            )
            fit_kwargs = {"sample_weight": weights}
            if validation_view is not None and frozen_iterations is None:
                fit_kwargs.update(
                    {
                        "eval_X": validation_matrix,
                        "eval_y": _target(validation_view),
                        "callbacks": [
                            lgb.early_stopping(int(self.parameters["early_stopping_rounds"]), verbose=False)
                        ],
                    }
                )
            estimator.fit(train_matrix, target, **fit_kwargs)
            self.effective_iterations_ = int(getattr(estimator, "best_iteration_", estimator.n_estimators_))
        else:
            raise KeyError(self.family)
        self.preprocessor = preprocessor
        self.estimator = estimator
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if self.preprocessor is None or self.estimator is None:
            raise RuntimeError("External booster is not fitted")
        view = self._view(dataset)
        matrix = self.preprocessor.transform(view).drop(columns="sample_id").to_numpy(float)
        return AdapterPrediction(mean=np.asarray(self.estimator.predict(matrix), float)).validate(len(view.frame))


@dataclass
class NativeCategoricalPreprocessor:
    contract: FeatureContract
    numeric_columns_: tuple[str, ...] = ()
    categorical_columns_: tuple[str, ...] = ()
    medians_: dict[str, float] = field(default_factory=dict)
    levels_: dict[str, tuple[str, ...]] = field(default_factory=dict)
    fitted_: bool = False

    def fit(self, dataset: ManifestDataset) -> "NativeCategoricalPreprocessor":
        if dataset.provenance.split != "train":
            raise LeakageViolation("Native categorical preprocessing may be fit only on train")
        assert_estimator_feature_safety(dataset.feature_columns, self.contract)
        features = dataset.frame.loc[:, dataset.feature_columns]
        self.numeric_columns_ = tuple(column for column in features if is_numeric_dtype(features[column]))
        self.categorical_columns_ = tuple(column for column in features if column not in self.numeric_columns_)
        self.medians_ = {
            column: float(pd.to_numeric(features[column], errors="coerce").median())
            if pd.to_numeric(features[column], errors="coerce").notna().any()
            else 0.0
            for column in self.numeric_columns_
        }
        self.levels_ = {}
        for column in self.categorical_columns_:
            values = features[column].astype("string").fillna(MISSING_CATEGORY).astype(str)
            levels = sorted(set(values))
            levels.append(UNKNOWN_CATEGORY)
            self.levels_[column] = tuple(dict.fromkeys(levels))
        self.fitted_ = True
        return self

    def transform(self, dataset: ManifestDataset) -> pd.DataFrame:
        if not self.fitted_:
            raise RuntimeError("Native categorical preprocessor is not fitted")
        assert_estimator_feature_safety(dataset.feature_columns, self.contract)
        output = pd.DataFrame(index=dataset.frame.index)
        for column in self.numeric_columns_:
            output[column] = pd.to_numeric(dataset.frame[column], errors="coerce").fillna(self.medians_[column]).astype(float)
        for column in self.categorical_columns_:
            values = dataset.frame[column].astype("string").fillna(MISSING_CATEGORY).astype(str)
            known = set(self.levels_[column]) - {UNKNOWN_CATEGORY}
            output[column] = values.where(values.isin(known), UNKNOWN_CATEGORY).astype(str)
        return output.loc[:, list(dataset.feature_columns)]


class CatBoostAdapter(B6Adapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preprocessor: NativeCategoricalPreprocessor | None = None
        self.estimator: Any | None = None

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "CatBoostAdapter":
        try:
            from catboost import CatBoostRegressor
        except ImportError as exc:
            raise RuntimeError("catboost is not installed in this worker environment") from exc
        train_view = self._view(train)
        preprocessor = NativeCategoricalPreprocessor(self.view_contract).fit(train_view)
        matrix = preprocessor.transform(train_view)
        categorical = [matrix.columns.get_loc(column) for column in preprocessor.categorical_columns_]
        frozen_iterations = self.parameters.get("frozen_iterations")
        estimator = CatBoostRegressor(
            loss_function=self.parameters["loss_function"],
            depth=int(self.parameters["depth"]),
            l2_leaf_reg=float(self.parameters["l2_leaf_reg"]),
            learning_rate=float(self.parameters["learning_rate"]),
            iterations=int(frozen_iterations or self.parameters["iterations"]),
            task_type=self.parameters["task_type"],
            thread_count=int(self.parameters["thread_count"]),
            allow_writing_files=bool(self.parameters["allow_writing_files"]),
            random_seed=self.seed,
            verbose=False,
        )
        fit_kwargs: dict[str, Any] = {
            "X": matrix,
            "y": _target(train_view),
            "cat_features": categorical,
            "sample_weight": train_only_precision_weights(train_view.frame, lower=0.25, upper=4.0),
        }
        if validation is not None and frozen_iterations is None:
            validation_view = self._view(validation)
            fit_kwargs["eval_set"] = (preprocessor.transform(validation_view), _target(validation_view))
            fit_kwargs["early_stopping_rounds"] = int(self.parameters["early_stopping_rounds"])
            fit_kwargs["use_best_model"] = True
        estimator.fit(**fit_kwargs)
        best_iteration = estimator.get_best_iteration()
        if best_iteration is None or int(best_iteration) < 0:
            self.effective_iterations_ = int(frozen_iterations or self.parameters["iterations"])
        else:
            self.effective_iterations_ = int(best_iteration) + 1
        self.preprocessor = preprocessor
        self.estimator = estimator
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if self.preprocessor is None or self.estimator is None:
            raise RuntimeError("CatBoost is not fitted")
        matrix = self.preprocessor.transform(self._view(dataset))
        return AdapterPrediction(mean=np.asarray(self.estimator.predict(matrix), float)).validate(len(matrix))


class EBMAdapter(B6Adapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preprocessor: NativeCategoricalPreprocessor | None = None
        self.estimator: Any | None = None

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "EBMAdapter":
        del validation
        try:
            from interpret.glassbox import ExplainableBoostingRegressor
        except ImportError as exc:
            raise RuntimeError("interpret-core is not installed in this worker environment") from exc
        train_view = self._view(train)
        preprocessor = NativeCategoricalPreprocessor(self.view_contract).fit(train_view)
        matrix = preprocessor.transform(train_view)
        feature_types = [
            "continuous" if column in preprocessor.numeric_columns_ else "nominal" for column in matrix.columns
        ]
        estimator = ExplainableBoostingRegressor(
            feature_names=list(matrix.columns),
            feature_types=feature_types,
            interactions=int(self.parameters["interactions"]),
            max_bins=int(self.parameters["max_bins"]),
            min_samples_leaf=int(self.parameters["min_samples_leaf"]),
            learning_rate=float(self.parameters["learning_rate"]),
            outer_bags=int(self.parameters["outer_bags"]),
            max_rounds=int(self.parameters["max_rounds"]),
            n_jobs=int(self.parameters["n_jobs"]),
            random_state=self.seed,
        )
        estimator.fit(
            matrix,
            _target(train_view),
            sample_weight=train_only_precision_weights(train_view.frame, lower=0.25, upper=4.0),
        )
        self.preprocessor = preprocessor
        self.estimator = estimator
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if self.preprocessor is None or self.estimator is None:
            raise RuntimeError("EBM is not fitted")
        matrix = self.preprocessor.transform(self._view(dataset))
        return AdapterPrediction(mean=np.asarray(self.estimator.predict(matrix), float)).validate(len(matrix))


class NGBoostAdapter(B6Adapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preprocessor: TrainOnlyPreprocessor | None = None
        self.estimator: Any | None = None
        self.target_mean_: float | None = None
        self.target_scale_: float | None = None

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "NGBoostAdapter":
        del validation
        try:
            from ngboost import NGBRegressor
            from ngboost.distns import Normal
            from ngboost.scores import CRPScore, LogScore
            from sklearn.tree import DecisionTreeRegressor
        except ImportError as exc:
            raise RuntimeError("ngboost is not installed in this worker environment") from exc
        train_view = self._view(train)
        preprocessor = TrainOnlyPreprocessor(self.view_contract)
        matrix = preprocessor.fit_transform(train_view).drop(columns="sample_id").to_numpy(float)
        target = _target(train_view)
        target_mean = float(np.mean(target))
        target_scale = float(np.std(target))
        if not np.isfinite(target_scale) or target_scale <= 0:
            target_scale = 1.0
        standardized_target = (target - target_mean) / target_scale
        score = {"LogScore": LogScore, "CRPScore": CRPScore}[str(self.parameters["score"])]
        estimator = NGBRegressor(
            Dist=Normal,
            Score=score,
            Base=DecisionTreeRegressor(max_depth=int(self.parameters["base_tree_depth"]), random_state=self.seed),
            learning_rate=float(self.parameters["learning_rate"]),
            n_estimators=int(self.parameters["n_estimators"]),
            random_state=self.seed,
            verbose=False,
        )
        estimator.fit(
            matrix,
            standardized_target,
            sample_weight=train_only_precision_weights(train_view.frame, lower=0.25, upper=4.0),
        )
        self.effective_iterations_ = int(self.parameters["n_estimators"])
        self.preprocessor = preprocessor
        self.estimator = estimator
        self.target_mean_ = target_mean
        self.target_scale_ = target_scale
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if (
            self.preprocessor is None
            or self.estimator is None
            or self.target_mean_ is None
            or self.target_scale_ is None
        ):
            raise RuntimeError("NGBoost is not fitted")
        view = self._view(dataset)
        matrix = self.preprocessor.transform(view).drop(columns="sample_id").to_numpy(float)
        distribution = self.estimator.pred_dist(matrix)
        mean = self.target_mean_ + self.target_scale_ * np.asarray(distribution.loc, float)
        std = self.target_scale_ * np.asarray(distribution.scale, float)
        return AdapterPrediction(
            mean=mean,
            predictive_std=np.maximum(std, 1e-8),
            distribution_family="normal",
            distribution_parameters={"loc": mean, "scale": std},
        ).validate(len(view.frame))

    def state_dict(self) -> dict[str, Any]:
        state = super().state_dict()
        state["target_mean"] = self.target_mean_
        state["target_scale"] = self.target_scale_
        return state


class TorchTabularAdapter(B6Adapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preprocessor: TrainOnlyPreprocessor | None = None
        self.network: Any | None = None
        self.target_scale_: float | None = None
        self.device_: str | None = None
        self.input_features_: int | None = None

    def fit(self, train: ManifestDataset, *, validation: ManifestDataset | None = None) -> "TorchTabularAdapter":
        try:
            import torch
            from torch import nn
        except ImportError as exc:
            raise RuntimeError("PyTorch is not installed in this worker environment") from exc
        _seed_torch(torch, self.seed)
        train_view = self._view(train)
        preprocessor = TrainOnlyPreprocessor(self.view_contract)
        x_train = preprocessor.fit_transform(train_view).drop(columns="sample_id").to_numpy(np.float32)
        baseline_train = pd.to_numeric(train_view.frame["last_rate_mm_y"], errors="coerce").to_numpy(float)
        fallback = float(np.nanmedian(_target(train_view)))
        baseline_train = np.where(np.isfinite(baseline_train), baseline_train, fallback)
        residual = _target(train_view) - baseline_train
        target_scale = float(np.std(residual))
        if not np.isfinite(target_scale) or target_scale <= 0:
            target_scale = 1.0
        y_train = (residual / target_scale).astype(np.float32)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.family == "residual_mlp":
            network = _build_mlp(nn, x_train.shape[1], self.parameters)
        elif self.family == "protocol_safe_enfs_replica":
            network = _build_enfs(nn, torch, x_train.shape[1], self.parameters)
        else:
            raise KeyError(self.family)
        network.to(device)
        optimizer = torch.optim.AdamW(
            network.parameters(),
            lr=float(self.parameters.get("learning_rate_start", self.parameters.get("learning_rate", 0.001))),
            weight_decay=float(self.parameters.get("weight_decay", 0.0)),
        )
        loss_function = nn.HuberLoss()
        x_tensor = torch.tensor(x_train, device=device)
        y_tensor = torch.tensor(y_train, device=device)
        validation_tensors = None
        if validation is not None:
            validation_view = self._view(validation)
            x_validation = preprocessor.transform(validation_view).drop(columns="sample_id").to_numpy(np.float32)
            baseline_validation = pd.to_numeric(
                validation_view.frame["last_rate_mm_y"], errors="coerce"
            ).to_numpy(float)
            baseline_validation = np.where(np.isfinite(baseline_validation), baseline_validation, fallback)
            y_validation = ((_target(validation_view) - baseline_validation) / target_scale).astype(np.float32)
            validation_tensors = (
                torch.tensor(x_validation, device=device),
                torch.tensor(y_validation, device=device),
            )
        max_epochs = int(self.parameters.get("frozen_epochs") or self.parameters["max_epochs"])
        patience = int(self.parameters["patience"] if self.family == "residual_mlp" else self.parameters["early_stopping_patience"])
        batch_size = int(self.parameters.get("batch_size", 32))
        generator = torch.Generator(device="cpu").manual_seed(self.seed)
        best_loss = float("inf")
        best_state = None
        stale = 0
        epochs_run = 0
        for epoch in range(max_epochs):
            network.train()
            order = torch.randperm(len(x_tensor), generator=generator).to(device)
            for start in range(0, len(order), batch_size):
                indices = order[start : start + batch_size]
                optimizer.zero_grad(set_to_none=True)
                loss = loss_function(network(x_tensor[indices]).squeeze(-1), y_tensor[indices])
                loss.backward()
                optimizer.step()
            epochs_run = epoch + 1
            if self.family == "protocol_safe_enfs_replica":
                learning_rate = max(
                    float(self.parameters["learning_rate_end"]),
                    float(self.parameters["learning_rate_start"])
                    * float(self.parameters["scheduler_gamma"]) ** epochs_run,
                )
                for group in optimizer.param_groups:
                    group["lr"] = learning_rate
            if validation_tensors is not None:
                network.eval()
                with torch.no_grad():
                    value = float(
                        loss_function(network(validation_tensors[0]).squeeze(-1), validation_tensors[1]).item()
                    )
                if value < best_loss - 1e-8:
                    best_loss = value
                    best_state = {key: tensor.detach().cpu().clone() for key, tensor in network.state_dict().items()}
                    stale = 0
                else:
                    stale += 1
                    if stale >= patience:
                        break
        if best_state is not None:
            network.load_state_dict(best_state)
        self.effective_iterations_ = epochs_run
        self.preprocessor = preprocessor
        self.network = network
        self.target_scale_ = target_scale
        self.device_ = device
        self.input_features_ = int(x_train.shape[1])
        return self

    def predict(self, dataset: ManifestDataset) -> AdapterPrediction:
        if self.preprocessor is None or self.network is None or self.target_scale_ is None:
            raise RuntimeError("Torch adapter is not fitted")
        import torch

        view = self._view(dataset)
        matrix = self.preprocessor.transform(view).drop(columns="sample_id").to_numpy(np.float32)
        baseline = pd.to_numeric(view.frame["last_rate_mm_y"], errors="coerce").to_numpy(float)
        baseline = np.where(np.isfinite(baseline), baseline, 0.0)
        self.network.eval()
        with torch.no_grad():
            residual = self.network(torch.tensor(matrix, device=self.device_)).squeeze(-1).cpu().numpy()
        mean = baseline + residual * self.target_scale_
        return AdapterPrediction(mean=np.asarray(mean, float)).validate(len(view.frame))

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        network = state.get("network")
        if network is not None:
            state["_serialized_network_state"] = {
                key: tensor.detach().cpu()
                for key, tensor in network.state_dict().items()
            }
            # The ENFS class is intentionally constructed inside the torch-only
            # factory, so persist architecture + tensors instead of pickling a
            # process-local class object.
            state["network"] = None
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        serialized = state.pop("_serialized_network_state", None)
        self.__dict__.update(state)
        if serialized is None:
            return
        if self.input_features_ is None:
            raise RuntimeError("Serialized torch adapter is missing its input width")
        import torch
        from torch import nn

        if self.family == "residual_mlp":
            network = _build_mlp(nn, self.input_features_, self.parameters)
        elif self.family == "protocol_safe_enfs_replica":
            network = _build_enfs(nn, torch, self.input_features_, self.parameters)
        else:
            raise KeyError(self.family)
        network.load_state_dict(serialized)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        network.to(device)
        self.network = network
        self.device_ = device


def create_adapter(
    spec: ModelSpec,
    parameters: Mapping[str, Any],
    *,
    contract: FeatureContract,
    seed: int,
    raw_history: pd.DataFrame | None = None,
    prepared_history: Any | None = None,
) -> B6Adapter:
    if spec.status == "FROZEN_COMPARATOR":
        if raw_history is None or prepared_history is None:
            raise ValueError("Frozen comparator requires causal raw and prepared histories")
        return FrozenComparatorAdapter(
            spec,
            parameters,
            contract=contract,
            seed=seed,
            raw_history=raw_history,
            prepared_history=prepared_history,
        )
    family = spec.family
    adapter_type: type[B6Adapter]
    if family in {"elastic_net", "huber", "rbf_svr", "hist_gradient_boosting"}:
        adapter_type = OneHotSklearnAdapter
    elif family == "gaussian_process":
        adapter_type = GaussianProcessAdapter
    elif family == "gaussian_gee":
        adapter_type = GaussianGEEAdapter
    elif family == "quantile_hist_gradient_boosting":
        adapter_type = QuantileHGBAdapter
    elif family in {"xgboost", "lightgbm"}:
        adapter_type = ExternalBoostingAdapter
    elif family == "catboost":
        adapter_type = CatBoostAdapter
    elif family == "explainable_boosting_machine":
        adapter_type = EBMAdapter
    elif family == "ngboost_normal":
        adapter_type = NGBoostAdapter
    elif family in {"residual_mlp", "protocol_safe_enfs_replica"}:
        adapter_type = TorchTabularAdapter
    elif family == "local_tabpfn":
        raise ContractViolation(
            "TabPFN support was removed by frozen governance amendment B6-GOV-001"
        )
    else:
        raise KeyError(f"Unknown B6 family: {family}")
    return adapter_type(spec, parameters, contract=contract, seed=seed)


def _target(dataset: ManifestDataset) -> np.ndarray:
    values = pd.to_numeric(dataset.frame[TARGET_COLUMN], errors="coerce").to_numpy(float)
    if not np.isfinite(values).all():
        raise LeakageViolation("Training or validation target contains non-finite values")
    return values


def _weights_from_train(train_frame: pd.DataFrame, evaluation_frame: pd.DataFrame) -> np.ndarray:
    train_sigma = pd.to_numeric(train_frame["sigma_rate_mm_y"], errors="coerce").to_numpy(float)
    finite_train = train_sigma[np.isfinite(train_sigma) & (train_sigma > 0)]
    if not len(finite_train):
        return np.ones(len(evaluation_frame), dtype=float)
    reference_variance = float(np.median(finite_train**2))
    sigma = pd.to_numeric(evaluation_frame["sigma_rate_mm_y"], errors="coerce").to_numpy(float)
    sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, math.sqrt(reference_variance))
    weights = np.clip(reference_variance / sigma**2, 0.25, 4.0)
    return weights / float(np.mean(weights))


def _gee_identifiable_matrix(
    matrix: pd.DataFrame,
    preprocessor: TrainOnlyPreprocessor,
) -> pd.DataFrame:
    """Drop reference levels and train-zero columns before adding a GEE intercept."""

    reference_columns = {
        f"{column}=={levels[0]}"
        for column, levels in preprocessor.categorical_levels_.items()
        if levels
    }
    keep = [column for column in matrix if column not in reference_columns]
    return matrix.loc[:, keep]


def _full_rank_columns(frame: pd.DataFrame) -> tuple[str, ...]:
    """Choose a deterministic maximal full-rank exogenous schema on train only."""

    from scipy.linalg import qr

    values = frame.to_numpy(float)
    _, triangular, pivots = qr(values, mode="economic", pivoting=True)
    diagonal = np.abs(np.diag(triangular))
    tolerance = max(values.shape) * np.finfo(float).eps * (float(diagonal.max()) if len(diagonal) else 0.0)
    rank = int(np.sum(diagonal > tolerance))
    selected = sorted(map(int, pivots[:rank]))
    if rank == 0:
        raise RuntimeError("Gaussian GEE train design has rank zero")
    return tuple(str(frame.columns[index]) for index in selected)


def _build_mlp(nn: Any, input_features: int, parameters: Mapping[str, Any]):
    layers: list[Any] = []
    current = input_features
    for hidden in map(int, parameters["hidden_layout"]):
        layers.extend([nn.Linear(current, hidden), nn.ReLU()])
        if float(parameters["dropout"]) > 0:
            layers.append(nn.Dropout(float(parameters["dropout"])))
        current = hidden
    layers.append(nn.Linear(current, 1))
    return nn.Sequential(*layers)


def _build_enfs(nn: Any, torch: Any, input_features: int, parameters: Mapping[str, Any]):
    rules = int(parameters["fuzzy_rules"])
    hidden = tuple(map(int, parameters["rule_network"]))

    class ENFS(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.centres = nn.Parameter(torch.randn(rules, input_features) * 0.25)
            self.log_widths = nn.Parameter(torch.zeros(rules, input_features))
            networks = []
            for _ in range(rules):
                layers: list[Any] = []
                width = input_features
                for units in hidden:
                    layers.extend([nn.Linear(width, units), nn.Tanh()])
                    width = units
                layers.append(nn.Linear(width, 1))
                networks.append(nn.Sequential(*layers))
            self.rule_networks = nn.ModuleList(networks)

        def forward(self, values):
            widths = torch.nn.functional.softplus(self.log_widths) + 1e-3
            normalized = torch.abs((values[:, None, :] - self.centres[None, :, :]) / widths[None, :, :])
            # Symmetric pi membership: a unit plateau inside one learned
            # half-width, quadratic S/Z shoulders, and zero beyond three
            # half-widths.  This is the actual preregistered pi-shaped channel,
            # not a bell-function approximation.
            midpoint = 2.0
            outer = 3.0
            first_shoulder = 1.0 - 2.0 * ((normalized - 1.0) / (outer - 1.0)) ** 2
            second_shoulder = 2.0 * ((normalized - outer) / (outer - 1.0)) ** 2
            membership = torch.where(
                normalized <= 1.0,
                torch.ones_like(normalized),
                torch.where(
                    normalized <= midpoint,
                    first_shoulder,
                    torch.where(
                        normalized < outer,
                        second_shoulder,
                        torch.zeros_like(normalized),
                    ),
                ),
            )
            log_fire = torch.mean(torch.log(membership + 1e-12), dim=2)
            weights = torch.softmax(log_fire, dim=1)
            consequents = torch.cat([network(values) for network in self.rule_networks], dim=1)
            return torch.sum(weights * consequents, dim=1, keepdim=True)

    return ENFS()


def _seed_torch(torch: Any, seed: int) -> None:
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(True)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
