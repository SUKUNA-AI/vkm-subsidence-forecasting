"""Canonical data-path and schema contracts for Gate A1.

The module is intentionally the only place where model code discovers the
canonical feature and target tables.  Historical ``next_cycle_*`` files are
registered for comparison, but are never returned as trainable data.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd
import yaml


class ContractViolation(ValueError):
    """Raised when a canonical path, schema, or target contract is violated."""


REQUIRED_CANONICAL_KEYS = {
    "features",
    "operational_targets",
    "feature_contract",
    "target_contract",
    "early_warning_labels",
}

FEATURE_DATE_COLUMNS = ("current_date", "target_date")
T1_DATE_COLUMNS = ("current_date", "target_date")
T5_DATE_COLUMNS = ("current_date", "label_horizon_end", "first_onset_date")


def discover_project_root(start: str | Path | None = None) -> Path:
    """Find the repository root without embedding a host-specific path."""

    candidate = Path(start or Path.cwd()).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for path in (candidate, *candidate.parents):
        if (path / "pyproject.toml").is_file() and (path / "configs" / "gate_a1.yaml").is_file():
            return path
    raise FileNotFoundError("Could not locate project root containing pyproject.toml and configs/gate_a1.yaml")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_relative(root: Path, value: str) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        raise ContractViolation(f"Gate A1 paths must be repository-relative: {value}")
    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractViolation(f"Path escapes repository root: {value}") from exc
    return resolved


def load_gate_config(root: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    config_path = project_root / "configs" / "gate_a1.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractViolation("configs/gate_a1.yaml must contain a mapping")
    missing = REQUIRED_CANONICAL_KEYS - set(config.get("canonical", {}))
    if missing:
        raise ContractViolation(f"Missing canonical path keys: {sorted(missing)}")
    return project_root, config


@dataclass(frozen=True)
class FeatureContract:
    """Executable view of ``formal_feature_contract.csv``."""

    table: pd.DataFrame
    source_path: Path
    source_sha256: str

    @classmethod
    def from_csv(cls, path: Path) -> "FeatureContract":
        table = pd.read_csv(path)
        expected = {"field", "role", "allowed", "reason"}
        if set(table.columns) != expected:
            raise ContractViolation(
                f"Feature contract columns must be {sorted(expected)}, got {list(table.columns)}"
            )
        if table["field"].isna().any() or table["field"].duplicated().any():
            raise ContractViolation("Feature contract field names must be non-null and unique")
        table = table.copy()
        table["allowed"] = table["allowed"].map(_parse_bool)
        if table["allowed"].isna().any():
            raise ContractViolation("Feature contract allowed column contains non-boolean values")
        allowed_with_wrong_role = table[table["allowed"] & table["role"].ne("MODEL_FEATURE")]
        if not allowed_with_wrong_role.empty:
            raise ContractViolation(
                "Only MODEL_FEATURE rows may be allowed: "
                + ", ".join(allowed_with_wrong_role["field"].astype(str))
            )
        return cls(table=table, source_path=path, source_sha256=sha256_file(path))

    @property
    def allowed_features(self) -> tuple[str, ...]:
        rows = self.table[self.table["allowed"]]
        return tuple(rows["field"].astype(str))

    @property
    def metadata_fields(self) -> tuple[str, ...]:
        rows = self.table[self.table["role"].eq("METADATA")]
        return tuple(rows["field"].astype(str))

    @property
    def excluded_fields(self) -> tuple[str, ...]:
        rows = self.table[~self.table["allowed"]]
        return tuple(rows["field"].astype(str))

    def role_of(self, field: str) -> str:
        match = self.table.loc[self.table["field"].eq(field), "role"]
        if match.empty:
            raise ContractViolation(f"Field is absent from feature contract: {field}")
        return str(match.iloc[0])

    def assert_covers_feature_table(self, columns: Iterable[str]) -> None:
        columns_set = set(columns)
        contract_set = set(self.table["field"].astype(str))
        missing = columns_set - contract_set
        if missing:
            raise ContractViolation(f"Feature table has fields absent from allowlist contract: {sorted(missing)}")
        absent_allowed = set(self.allowed_features) - columns_set
        if absent_allowed:
            raise ContractViolation(f"Allowed features are absent from canonical feature table: {sorted(absent_allowed)}")

    def assert_exact_estimator_columns(self, columns: Iterable[str]) -> None:
        actual = tuple(columns)
        expected = self.allowed_features
        missing = [name for name in expected if name not in actual]
        extra = [name for name in actual if name not in expected]
        if missing or extra:
            raise ContractViolation(
                f"Estimator matrix must use the exact allowlist; missing={missing}, extra={extra}"
            )


@dataclass(frozen=True)
class TargetContract:
    payload: Mapping[str, Any]
    source_path: Path
    source_sha256: str

    @classmethod
    def from_json(cls, path: Path) -> "TargetContract":
        payload = json.loads(path.read_text(encoding="utf-8"))
        required = {
            "dataset_version",
            "sign_convention",
            "origin",
            "primary_target",
            "split_protocol",
            "uncertainty",
            "metrics",
        }
        missing = required - set(payload)
        if missing:
            raise ContractViolation(f"Target contract is missing keys: {sorted(missing)}")
        split_protocol = payload["split_protocol"]
        if split_protocol.get("regression") != "by target_date":
            raise ContractViolation("Regression split must be governed by target_date")
        if split_protocol.get("early_warning") != "by label_horizon_end":
            raise ContractViolation("T5 split must be governed by label_horizon_end")
        if "random row split" not in str(split_protocol.get("forbidden", "")).lower():
            raise ContractViolation("Target contract must explicitly forbid random row split")
        return cls(payload=payload, source_path=path, source_sha256=sha256_file(path))


@dataclass(frozen=True)
class CanonicalBundle:
    root: Path
    config: Mapping[str, Any]
    paths: Mapping[str, Path]
    historical_paths: Mapping[str, Path]
    supporting_paths: Mapping[str, Path]
    features: pd.DataFrame
    operational_targets: pd.DataFrame
    early_warning_labels: pd.DataFrame
    feature_contract: FeatureContract
    target_contract: TargetContract

    @property
    def canonical_hashes(self) -> dict[str, str]:
        return {name: sha256_file(path) for name, path in self.paths.items()}


def load_canonical_bundle(root: str | Path | None = None) -> CanonicalBundle:
    """Load the five governed Gate A1 inputs and validate their join universe."""

    project_root, config = load_gate_config(root)
    canonical_config = config["canonical"]
    paths = {name: _resolve_relative(project_root, value) for name, value in canonical_config.items()}
    historical_paths = {
        name: _resolve_relative(project_root, value)
        for name, value in config.get("historical_only", {}).items()
    }
    supporting_paths = {
        name: _resolve_relative(project_root, value)
        for name, value in config.get("supporting", {}).items()
    }
    all_paths = {**paths, **historical_paths, **supporting_paths}
    missing_paths = [str(path.relative_to(project_root)) for path in all_paths.values() if not path.is_file()]
    if missing_paths:
        raise FileNotFoundError(f"Gate A1 inputs are missing: {missing_paths}")

    canonical_lower = {path.as_posix().lower() for path in paths.values()}
    historical_lower = {path.as_posix().lower() for path in historical_paths.values()}
    if canonical_lower & historical_lower:
        raise ContractViolation("Canonical and historical-only registries overlap")
    for name, path in paths.items():
        lowered = path.as_posix().lower()
        if "/private_generation/" in lowered or "/evaluation_only/" in lowered:
            raise ContractViolation(f"Canonical input {name} points to a forbidden data layer: {path}")

    feature_contract = FeatureContract.from_csv(paths["feature_contract"])
    target_contract = TargetContract.from_json(paths["target_contract"])
    features = _read_dates(paths["features"], FEATURE_DATE_COLUMNS)
    operational_targets = _read_dates(paths["operational_targets"], T1_DATE_COLUMNS)
    early_warning_labels = _read_dates(paths["early_warning_labels"], T5_DATE_COLUMNS)

    feature_contract.assert_covers_feature_table(features.columns)
    _assert_unique_sample_ids("features", features)
    _assert_unique_sample_ids("operational_targets", operational_targets)
    _assert_unique_sample_ids("early_warning_labels", early_warning_labels)
    feature_ids = set(features["sample_id"].astype(str))
    for name, frame in (
        ("operational_targets", operational_targets),
        ("early_warning_labels", early_warning_labels),
    ):
        other_ids = set(frame["sample_id"].astype(str))
        if other_ids != feature_ids:
            raise ContractViolation(
                f"Canonical {name} sample universe differs from features by {len(feature_ids ^ other_ids)} IDs"
            )

    return CanonicalBundle(
        root=project_root,
        config=config,
        paths=paths,
        historical_paths=historical_paths,
        supporting_paths=supporting_paths,
        features=features,
        operational_targets=operational_targets,
        early_warning_labels=early_warning_labels,
        feature_contract=feature_contract,
        target_contract=target_contract,
    )


def _read_dates(path: Path, date_columns: Iterable[str]) -> pd.DataFrame:
    frame = pd.read_csv(path)
    for column in date_columns:
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame


def _assert_unique_sample_ids(name: str, frame: pd.DataFrame) -> None:
    if "sample_id" not in frame:
        raise ContractViolation(f"{name} has no sample_id column")
    if frame["sample_id"].isna().any():
        raise ContractViolation(f"{name} contains null sample_id values")
    duplicated = int(frame["sample_id"].duplicated().sum())
    if duplicated:
        raise ContractViolation(f"{name} contains {duplicated} duplicate sample_id values")


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    return None
