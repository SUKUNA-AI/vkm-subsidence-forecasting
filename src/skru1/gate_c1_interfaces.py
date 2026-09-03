"""Hash-stable, Torch-free public interfaces for Gate C1.

The aggregation and validation layers import this module without importing
PyTorch.  Targets are deliberately absent from the worker-facing prediction
schema; outer labels may only be attached by the independent scorer after a
shard hash has been frozen.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .data_contracts import ContractViolation
from .splits import sample_id_list_sha256


C1_SEEDS = (42117, 42118, 42119, 42120, 42121)
C1_REQUIRED_MODELS = (
    "C01_compact_gru",
    "C02_compact_lstm",
    "C03_causal_tcn",
    "C04_probabilistic_gru_student_t",
)
C1_NUMERIC_CHANNELS = (
    "last_settlement_mm",
    "last_rate_mm_y",
    "current_standard_uncertainty_mm",
    "days_since_previous_observation",
    "missing_campaigns_since_previous",
)
C1_CATEGORICAL_CHANNELS = ("current_campaign_type",)
C1_MASKS = ("padding_mask", "observation_mask", "missing_campaign_mask")
C1_IDENTIFIER_NAMES = frozenset(
    {
        "sample_id",
        "point_id",
        "profile_id",
        "zone_id",
        "campaign_id",
        "current_campaign_id",
        "target_campaign_id",
        "observation_id",
    }
)


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def assert_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ContractViolation(f"{name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class SequenceModelSpec:
    """One preregistered compact sequence architecture."""

    model_id: str
    family: str
    probabilistic: bool
    parameter_grid: tuple[Mapping[str, Any], ...]
    seeds: tuple[int, ...]
    numeric_channels: tuple[str, ...]
    categorical_channels: tuple[str, ...]
    masks: tuple[str, ...]
    training_objective: str
    selection_objective: str
    parameter_count_limit: int
    environment_id: str
    status: str = "REQUIRED_COMPACT_SCREEN"

    def __post_init__(self) -> None:
        if self.model_id not in C1_REQUIRED_MODELS:
            raise ContractViolation(f"Unregistered Gate C1 model: {self.model_id}")
        if tuple(map(int, self.seeds)) != C1_SEEDS:
            raise ContractViolation(f"Gate C1 seeds changed for {self.model_id}")
        if tuple(self.numeric_channels) != C1_NUMERIC_CHANNELS:
            raise ContractViolation("Gate C1 numeric channels differ from the frozen C0 view")
        if tuple(self.categorical_channels) != C1_CATEGORICAL_CHANNELS:
            raise ContractViolation("Gate C1 categorical channels differ from the frozen C0 view")
        if tuple(self.masks) != C1_MASKS:
            raise ContractViolation("Gate C1 masks differ from the frozen C0 view")
        if not self.parameter_grid:
            raise ContractViolation(f"Gate C1 grid is empty for {self.model_id}")
        if self.parameter_count_limit != 100_000:
            raise ContractViolation("Gate C1 parameter-count limit must remain 100000")
        if self.environment_id != "gate_c_torch":
            raise ContractViolation("Gate C1 model must use the gate_c_torch environment")
        if self.status != "REQUIRED_COMPACT_SCREEN":
            raise ContractViolation("Gate C1 registry may include only REQUIRED_COMPACT_SCREEN models")

    @property
    def spec_sha256(self) -> str:
        return canonical_json_sha256(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["spec_sha256"] = self.spec_sha256
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SequenceModelSpec":
        clean = {key: value for key, value in payload.items() if key != "spec_sha256"}
        clean["parameter_grid"] = tuple(clean["parameter_grid"])
        for key in ("seeds", "numeric_channels", "categorical_channels", "masks"):
            clean[key] = tuple(clean[key])
        spec = cls(**clean)
        supplied = payload.get("spec_sha256")
        if supplied is not None and supplied != spec.spec_sha256:
            raise ContractViolation(f"SequenceModelSpec hash mismatch for {spec.model_id}")
        return spec


@dataclass(frozen=True)
class C1FitContext:
    """Train-only provenance supplied to preprocessing and target scaling."""

    fold_id: str
    role: str
    source_split: str
    sample_ids_sha256: str
    sequence_pairs_sha256: str
    target_sha256: str
    seed: int

    def validate(self) -> None:
        if self.role != "train" or self.source_split != "t1_v1/train":
            raise ContractViolation("Gate C1 fit context must be role=train inside t1_v1/train")
        if not self.fold_id:
            raise ContractViolation("Gate C1 fit context requires fold_id")
        for name in ("sample_ids_sha256", "sequence_pairs_sha256", "target_sha256"):
            assert_sha256(str(getattr(self, name)), name)
        if int(self.seed) not in C1_SEEDS:
            raise ContractViolation("Gate C1 fit context uses an unregistered seed")

    @property
    def context_sha256(self) -> str:
        self.validate()
        return canonical_json_sha256(asdict(self))


@dataclass(frozen=True)
class SequenceTensorBatch:
    """Variable-history batch with IDs held outside the estimator tensor."""

    x: np.ndarray
    padding_mask: np.ndarray
    observation_mask: np.ndarray
    missing_campaign_mask: np.ndarray
    lengths: np.ndarray
    sample_ids: tuple[str, ...]
    feature_names: tuple[str, ...]
    preprocessing_state_sha256: str

    def validate(self, *, max_length: int = 16) -> "SequenceTensorBatch":
        if self.x.ndim != 3:
            raise ContractViolation("Sequence tensor X must be rank 3")
        n_rows, sequence_length, _ = self.x.shape
        if sequence_length != max_length:
            raise ContractViolation("Sequence tensor length differs from frozen C0 max length")
        expected_shape = (n_rows, sequence_length)
        for name, value in (
            ("padding_mask", self.padding_mask),
            ("observation_mask", self.observation_mask),
            ("missing_campaign_mask", self.missing_campaign_mask),
        ):
            if np.asarray(value).shape != expected_shape:
                raise ContractViolation(f"{name} shape differs from sequence tensor")
        if np.asarray(self.lengths).shape != (n_rows,):
            raise ContractViolation("Sequence lengths shape mismatch")
        if len(self.sample_ids) != n_rows or len(set(self.sample_ids)) != n_rows:
            raise ContractViolation("Sequence batch sample IDs must be exact and unique")
        if len(self.feature_names) != self.x.shape[2]:
            raise ContractViolation("Sequence feature names do not match tensor width")
        lowered = {name.lower() for name in self.feature_names}
        if lowered & C1_IDENTIFIER_NAMES:
            raise ContractViolation("Identifier leaked into Gate C1 tensor feature names")
        if not np.isfinite(self.x).all():
            raise ContractViolation("Sequence tensor contains non-finite values")
        lengths = np.asarray(self.lengths, dtype=int)
        if (lengths < 1).any() or (lengths > max_length).any():
            raise ContractViolation("Sequence lengths are outside the frozen range")
        padding = np.asarray(self.padding_mask, dtype=int)
        for row, length in zip(padding, lengths, strict=True):
            if row.tolist() != [1] * (max_length - int(length)) + [0] * int(length):
                raise ContractViolation("Gate C1 tensor is not deterministically left padded")
        if not np.equal(self.x[padding.astype(bool)], 0.0).all():
            raise ContractViolation("Padding values must be zero after preprocessing")
        assert_sha256(self.preprocessing_state_sha256, "preprocessing_state_sha256")
        return self


@dataclass
class C1SequencePreprocessor:
    """Fold-local imputer/scaler and one-hot encoder for the frozen C0 rows."""

    numeric_channels: tuple[str, ...] = C1_NUMERIC_CHANNELS
    categorical_channel: str = "current_campaign_type"
    medians_: dict[str, float] = field(default_factory=dict)
    means_: dict[str, float] = field(default_factory=dict)
    scales_: dict[str, float] = field(default_factory=dict)
    categories_: tuple[str, ...] = ()
    fit_context_: C1FitContext | None = None

    def fit(
        self,
        sequence_rows: pd.DataFrame,
        *,
        sample_ids: Sequence[str],
        context: C1FitContext,
    ) -> "C1SequencePreprocessor":
        context.validate()
        ids = tuple(map(str, sample_ids))
        if sample_id_list_sha256(ids) != context.sample_ids_sha256:
            raise ContractViolation("C1 preprocessing IDs do not match fit provenance")
        selected = _select_sequence_rows(sequence_rows, ids)
        observed = selected.loc[pd.to_numeric(selected["padding_mask"], errors="raise").eq(0)].copy()
        observed_ids = tuple(dict.fromkeys(observed["sample_id"].astype(str)))
        if observed_ids != ids:
            raise ContractViolation("C1 preprocessing rows do not preserve the train manifest order")
        required = set(self.numeric_channels) | {self.categorical_channel, *C1_MASKS}
        missing = required - set(observed.columns)
        if missing:
            raise ContractViolation(f"C1 sequence rows are missing channels: {sorted(missing)}")
        for channel in self.numeric_channels:
            values = pd.to_numeric(observed[channel], errors="coerce")
            median = float(values.median()) if values.notna().any() else 0.0
            filled = values.fillna(median)
            mean = float(filled.mean())
            scale = float(filled.std(ddof=0))
            self.medians_[channel] = median
            self.means_[channel] = mean
            self.scales_[channel] = scale if np.isfinite(scale) and scale > 0 else 1.0
        categories = observed[self.categorical_channel].astype("string").fillna("<MISSING>").astype(str)
        self.categories_ = tuple(sorted(categories.unique()))
        self.fit_context_ = context
        return self

    def transform(
        self,
        sequence_rows: pd.DataFrame,
        *,
        sample_ids: Sequence[str],
        max_length: int = 16,
    ) -> SequenceTensorBatch:
        if self.fit_context_ is None:
            raise ContractViolation("C1 sequence preprocessor must be fitted before transform")
        ids = tuple(map(str, sample_ids))
        selected = _select_sequence_rows(sequence_rows, ids)
        if len(selected) != len(ids) * max_length:
            raise ContractViolation("C1 sequence row count differs from manifest x max_length")
        numeric = np.zeros((len(ids), max_length, len(self.numeric_channels)), dtype=np.float32)
        categorical = np.zeros((len(ids), max_length, len(self.categories_) + 1), dtype=np.float32)
        padding = np.zeros((len(ids), max_length), dtype=np.float32)
        observation = np.zeros((len(ids), max_length), dtype=np.float32)
        missing_campaign = np.zeros((len(ids), max_length), dtype=np.float32)
        category_map = {value: index for index, value in enumerate(self.categories_, start=1)}
        for row_index, sample_id in enumerate(ids):
            group = selected.loc[selected["sample_id"].astype(str).eq(sample_id)].sort_values(
                "sequence_position", kind="mergesort"
            )
            if len(group) != max_length:
                raise ContractViolation(f"C1 sequence has wrong normalized length: {sample_id}")
            padding[row_index] = pd.to_numeric(group["padding_mask"], errors="raise").to_numpy(np.float32)
            observation[row_index] = pd.to_numeric(group["observation_mask"], errors="raise").to_numpy(
                np.float32
            )
            missing_campaign[row_index] = pd.to_numeric(
                group["missing_campaign_mask"], errors="raise"
            ).to_numpy(np.float32)
            for channel_index, channel in enumerate(self.numeric_channels):
                values = pd.to_numeric(group[channel], errors="coerce").fillna(self.medians_[channel])
                numeric[row_index, :, channel_index] = (
                    (values.to_numpy(float) - self.means_[channel]) / self.scales_[channel]
                ).astype(np.float32)
            values = group[self.categorical_channel].astype("string").fillna("<MISSING>").astype(str)
            indices = values.map(category_map).fillna(0).astype(int).to_numpy()
            categorical[row_index, np.arange(max_length), indices] = 1.0
        x = np.concatenate((numeric, categorical), axis=2)
        x[padding.astype(bool)] = 0.0
        lengths = observation.sum(axis=1).astype(np.int64)
        feature_names = (*self.numeric_channels, "current_campaign_type::<UNKNOWN>") + tuple(
            f"current_campaign_type::{value}" for value in self.categories_
        )
        return SequenceTensorBatch(
            x=x,
            padding_mask=padding,
            observation_mask=observation,
            missing_campaign_mask=missing_campaign,
            lengths=lengths,
            sample_ids=ids,
            feature_names=feature_names,
            preprocessing_state_sha256=self.state_sha256,
        ).validate(max_length=max_length)

    @property
    def state_sha256(self) -> str:
        return canonical_json_sha256(self.state_dict())

    def state_dict(self) -> dict[str, Any]:
        if self.fit_context_ is None:
            raise ContractViolation("Cannot serialize an unfitted C1 sequence preprocessor")
        return {
            "schema_version": 1,
            "numeric_channels": list(self.numeric_channels),
            "categorical_channel": self.categorical_channel,
            "medians": self.medians_,
            "means": self.means_,
            "scales": self.scales_,
            "categories": list(self.categories_),
            "unknown_bucket_index": 0,
            "fit_context": asdict(self.fit_context_),
        }


@dataclass
class SequenceTargetScaler:
    """Train-only target scaler with reversible distribution transforms."""

    mean_: float | None = None
    scale_: float | None = None
    fit_context_: C1FitContext | None = None

    def fit(self, values: Iterable[float], *, context: C1FitContext) -> "SequenceTargetScaler":
        context.validate()
        array = np.asarray(tuple(values), dtype=float)
        if not len(array) or not np.isfinite(array).all():
            raise ContractViolation("Gate C1 target scaler requires finite non-empty train targets")
        scale = float(np.std(array, ddof=0))
        if not np.isfinite(scale) or scale <= 0:
            raise ContractViolation("Gate C1 target scaler rejects zero-variance train targets")
        self.mean_ = float(np.mean(array))
        self.scale_ = scale
        self.fit_context_ = context
        return self

    def transform(self, values: Iterable[float]) -> np.ndarray:
        self._require_fit()
        return (np.asarray(tuple(values), dtype=float) - float(self.mean_)) / float(self.scale_)

    def inverse_transform(self, values: Iterable[float] | np.ndarray) -> np.ndarray:
        self._require_fit()
        return np.asarray(values, dtype=float) * float(self.scale_) + float(self.mean_)

    def inverse_scale(self, values: Iterable[float] | np.ndarray) -> np.ndarray:
        self._require_fit()
        return np.asarray(values, dtype=float) * float(self.scale_)

    @property
    def state_sha256(self) -> str:
        return canonical_json_sha256(self.state_dict())

    def state_dict(self) -> dict[str, Any]:
        self._require_fit()
        return {
            "schema_version": 1,
            "mean": float(self.mean_),
            "scale": float(self.scale_),
            "fit_context": asdict(self.fit_context_),
        }

    def _require_fit(self) -> None:
        if self.mean_ is None or self.scale_ is None or self.fit_context_ is None:
            raise ContractViolation("SequenceTargetScaler must be fitted before use")


C1_WORKER_REQUIRED_COLUMNS = (
    "model_id",
    "family",
    "fold_id",
    "seed",
    "sample_id",
    "y_pred",
    "environment_id",
    "model_spec_sha256",
    "config_sha256",
    "code_sha256",
    "environment_sha256",
    "expected_sample_ids_sha256",
    "selected_parameter_sha256",
    "selected_parameter_json",
    "epoch_count",
    "parameter_count",
    "fit_seconds",
    "inference_seconds",
    "peak_ram_mb",
    "peak_vram_mb",
    "aggregation",
)
C1_WORKER_OPTIONAL_COLUMNS = (
    "distribution_family",
    "distribution_loc",
    "distribution_scale",
    "distribution_df",
    "q025",
    "q10",
    "q25",
    "q50",
    "q75",
    "q90",
    "q975",
)
C1_WORKER_FORBIDDEN_COLUMNS = frozenset({"y_true", "observed_rate_mm_y", "target_value"})


@dataclass(frozen=True)
class SequencePredictionBundle:
    """Validated unlabeled worker shard."""

    frame: pd.DataFrame

    @classmethod
    def validate(
        cls,
        frame: pd.DataFrame,
        *,
        expected_sample_ids: Sequence[str],
        expected_model_id: str,
        expected_fold_id: str,
        expected_environment_id: str = "gate_c_torch",
        allow_ensemble: bool = False,
    ) -> "SequencePredictionBundle":
        forbidden = set(frame.columns) & C1_WORKER_FORBIDDEN_COLUMNS
        if forbidden:
            raise ContractViolation(f"Gate C1 worker shard exposes labels: {sorted(forbidden)}")
        missing = set(C1_WORKER_REQUIRED_COLUMNS) - set(frame.columns)
        if missing:
            raise ContractViolation(f"Gate C1 worker shard is missing columns: {sorted(missing)}")
        extra = set(frame.columns) - set(C1_WORKER_REQUIRED_COLUMNS) - set(C1_WORKER_OPTIONAL_COLUMNS)
        if extra:
            raise ContractViolation(f"Gate C1 worker shard has unregistered columns: {sorted(extra)}")
        if set(frame["model_id"].astype(str)) != {expected_model_id}:
            raise ContractViolation("Gate C1 worker model ID mismatch")
        if set(frame["fold_id"].astype(str)) != {expected_fold_id}:
            raise ContractViolation("Gate C1 worker fold ID mismatch")
        if set(frame["environment_id"].astype(str)) != {expected_environment_id}:
            raise ContractViolation("Gate C1 worker environment ID mismatch")
        aggregation = set(frame["aggregation"].astype(str))
        allowed_aggregation = {"single_seed", "mean_of_fixed_seeds"} if allow_ensemble else {"single_seed"}
        if not aggregation <= allowed_aggregation:
            raise ContractViolation("Gate C1 worker shard has invalid aggregation")
        keys = ["model_id", "fold_id", "seed", "sample_id", "aggregation"]
        if frame.duplicated(keys).any():
            raise ContractViolation("Gate C1 worker shard contains duplicate rows")
        for column in ("y_pred", "fit_seconds", "inference_seconds", "peak_ram_mb", "peak_vram_mb"):
            values = pd.to_numeric(frame[column], errors="coerce").to_numpy(float)
            if not np.isfinite(values).all():
                raise ContractViolation(f"Gate C1 worker shard contains non-finite {column}")
        expected = tuple(map(str, expected_sample_ids))
        expected_hash = sample_id_list_sha256(expected)
        seeds = tuple(sorted(pd.to_numeric(frame["seed"], errors="raise").astype(int).unique()))
        if aggregation == {"single_seed"}:
            if seeds != C1_SEEDS:
                raise ContractViolation("Gate C1 worker shard must contain all five fixed seeds")
            for _, seed_frame in frame.groupby("seed", sort=True):
                actual = tuple(seed_frame["sample_id"].astype(str))
                if actual != expected:
                    raise ContractViolation("Gate C1 seed does not cover exact ordered sample IDs")
        if set(frame["expected_sample_ids_sha256"].astype(str)) != {expected_hash}:
            raise ContractViolation("Gate C1 worker expected sample-ID hash mismatch")
        probabilistic = frame.get("distribution_family", pd.Series(dtype="object")).notna().any()
        if probabilistic:
            for column in ("distribution_loc", "distribution_scale", "distribution_df"):
                values = pd.to_numeric(frame[column], errors="coerce").to_numpy(float)
                if not np.isfinite(values).all():
                    raise ContractViolation(f"Gate C1 Student-t shard contains invalid {column}")
            if pd.to_numeric(frame["distribution_scale"]).le(0).any():
                raise ContractViolation("Gate C1 Student-t scale must be positive")
            if pd.to_numeric(frame["distribution_df"]).le(2.01).any():
                raise ContractViolation("Gate C1 Student-t df must be greater than 2.01")
            quantiles = frame[["q025", "q10", "q25", "q50", "q75", "q90", "q975"]].to_numpy(float)
            if not np.isfinite(quantiles).all() or (np.diff(quantiles, axis=1) < 0).any():
                raise ContractViolation("Gate C1 Student-t quantiles must be finite and monotone")
        return cls(frame=frame.copy())


@dataclass(frozen=True)
class C1BenchmarkPlan:
    source_split: str
    benchmark_plan_sha256: str
    sequence_contract_content_sha256: str
    sequence_contract_file_sha256: str
    outer_jobs: tuple[Mapping[str, Any], ...]
    expected_outer_folds: int
    inner_folds_per_outer: int
    logical_inner_fits: int
    physical_inner_fits: int

    def __post_init__(self) -> None:
        if self.source_split != "t1_v1/train":
            raise ContractViolation("C1 benchmark source must remain t1_v1/train")
        if self.expected_outer_folds != 11 or self.inner_folds_per_outer != 3:
            raise ContractViolation("C1 benchmark fold counts changed")
        if len(self.outer_jobs) != 44:
            raise ContractViolation("C1 benchmark must contain exactly 44 outer jobs")
        if self.logical_inner_fits != 9240 or self.physical_inner_fits != 3640:
            raise ContractViolation("C1 logical/physical fit inventory changed")
        for name in ("benchmark_plan_sha256", "sequence_contract_content_sha256", "sequence_contract_file_sha256"):
            assert_sha256(str(getattr(self, name)), name)

    @property
    def plan_sha256(self) -> str:
        return canonical_json_sha256(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["plan_sha256"] = self.plan_sha256
        return payload


@dataclass(frozen=True)
class TemporalAdmissionRecord:
    model_id: str
    status: str
    checks: Mapping[str, bool]
    observed: Mapping[str, Any]
    model_spec_sha256: str
    config_sha256: str
    code_sha256: str
    environment_sha256: str

    @property
    def admitted(self) -> bool:
        return self.status == "PASSED_TEMPORAL_SCREEN" and all(self.checks.values())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["admitted"] = self.admitted
        payload["record_sha256"] = canonical_json_sha256(payload)
        return payload


def assert_c2_model_admitted(model_id: str, admission_manifest: Mapping[str, Any]) -> None:
    """Reject deep models that did not pass the frozen Gate C1 screen."""

    admitted = tuple(map(str, admission_manifest.get("admitted_model_ids", ())))
    if model_id not in C1_REQUIRED_MODELS or model_id not in admitted:
        raise ContractViolation(f"Deep model is not admitted to Gate C2: {model_id}")
    records = {str(item["model_id"]): item for item in admission_manifest.get("records", ())}
    record = records.get(model_id)
    if record is None or record.get("status") != "PASSED_TEMPORAL_SCREEN":
        raise ContractViolation(f"Gate C2 admission record is missing or invalid: {model_id}")


C1_FORBIDDEN_JOB_KEYS = frozenset(
    {
        "validation_manifest",
        "test_manifest",
        "holdout_manifest",
        "historical_validation_manifest",
        "outer_validation_targets",
        "outer_labels",
        "y_true",
    }
)


def assert_train_only_c1_job(payload: Mapping[str, Any]) -> None:
    found: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized_key = str(key).lower()
                child_path = f"{path}.{normalized_key}" if path else normalized_key
                if normalized_key in C1_FORBIDDEN_JOB_KEYS:
                    found.append(child_path)
                visit(child, child_path)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
        elif isinstance(value, (str, Path)):
            normalized = str(value).replace("\\", "/").lower()
            if "t1_v1/validation" in normalized or "t1_v1/test" in normalized or "holdout" in normalized:
                found.append(f"{path}={value}")

    visit(payload, "")
    if found:
        raise ContractViolation(f"Gate C1 worker job exposes prohibited data: {sorted(found)}")
    if str(payload.get("source_split")) != "t1_v1/train":
        raise ContractViolation("Gate C1 worker source_split must be exactly t1_v1/train")


def ordered_sample_hash(sample_ids: Sequence[str]) -> str:
    return sample_id_list_sha256(tuple(map(str, sample_ids)))


def target_values_sha256(sample_ids: Sequence[str], values: Sequence[float]) -> str:
    if len(sample_ids) != len(values):
        raise ContractViolation("Target hash requires aligned sample IDs and values")
    payload = [
        {"sample_id": str(sample_id), "target": float(value)}
        for sample_id, value in zip(sample_ids, values, strict=True)
    ]
    return canonical_json_sha256(payload)


def _select_sequence_rows(sequence_rows: pd.DataFrame, sample_ids: Sequence[str]) -> pd.DataFrame:
    ids = tuple(map(str, sample_ids))
    if len(ids) != len(set(ids)):
        raise ContractViolation("C1 sequence selection has duplicate sample IDs")
    selected = sequence_rows.loc[sequence_rows["sample_id"].astype(str).isin(ids)].copy()
    actual = set(selected["sample_id"].astype(str))
    missing = set(ids) - actual
    if missing:
        raise ContractViolation(f"C1 sequence selection has unknown sample IDs: {len(missing)}")
    order = {sample_id: index for index, sample_id in enumerate(ids)}
    selected["__sample_order"] = selected["sample_id"].astype(str).map(order)
    selected = selected.sort_values(["__sample_order", "sequence_position"], kind="mergesort")
    return selected.drop(columns="__sample_order").reset_index(drop=True)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(type(value).__name__)
