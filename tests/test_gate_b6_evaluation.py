from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from skru1.b6_evaluation import native_probabilistic_metrics, probabilistic_metric_table
from skru1.b6_models import _full_rank_columns
from skru1.b6_worker import _failure_class, _traceback_frames, ensemble_prediction_frame
from skru1.b6_probabilistic import quantile_crps_approximation
from skru1.data_contracts import ContractViolation
from skru1.gate_b6 import _dataframes_equivalent, build_internal_shortlist


ROOT = Path(__file__).resolve().parents[1]


def test_full_rank_column_selection_is_deterministic() -> None:
    frame = pd.DataFrame(
        {
            "const": np.ones(6),
            "a": np.arange(6, dtype=float),
            "duplicate_a": 2.0 * np.arange(6, dtype=float),
            "b": np.asarray([0, 1, 0, 1, 0, 1], dtype=float),
        }
    )
    first = _full_rank_columns(frame)
    second = _full_rank_columns(frame)
    assert first == second
    assert np.linalg.matrix_rank(frame.loc[:, first].to_numpy(float)) == len(first) == 3


def test_quantile_crps_and_conditional_native_metrics_are_published() -> None:
    rows = 60
    truth = np.linspace(-5.0, 5.0, rows)
    mean = truth + 0.25
    frame = pd.DataFrame(
        {
            "design": "rolling_origin",
            "model_id": "quantile_fixture",
            "profile_id": np.where(np.arange(rows) < 30, "P1", "P2"),
            "zone_id": np.where(np.arange(rows) % 2 == 0, "Z1", "Z2"),
            "transition_segment": np.where(np.arange(rows) % 3 == 0, "accelerating", "stable"),
            "missing_campaigns_since_previous": np.where(np.arange(rows) % 4 == 0, 1, 0),
            "y_true": truth,
            "y_pred": mean,
            "q025": mean - 3.0,
            "q10": mean - 2.0,
            "q25": mean - 1.0,
            "q50": mean,
            "q75": mean + 1.0,
            "q90": mean + 2.0,
            "q975": mean + 3.0,
            "distribution_family": "empirical_quantiles",
        }
    )
    for tag, half_width in (("50", 1.0), ("80", 2.0), ("95", 3.0)):
        frame[f"conformal_lower_{tag}"] = mean - half_width
        frame[f"conformal_upper_{tag}"] = mean + half_width
    native = native_probabilistic_metrics(frame)
    assert native is not None
    assert np.isfinite(native["crps"])
    assert native["nll"] != native["nll"]
    assert native["quantile_crossing_rate"] == 0.0
    metrics = probabilistic_metric_table(frame)
    native_rows = metrics.loc[metrics["interval_source"].eq("native")]
    assert {"overall", "profile", "zone", "transition", "gap"}.issubset(
        set(native_rows["dimension"])
    )


def test_quantile_crps_uses_interval_width_weights() -> None:
    levels = np.asarray([0.025, 0.10, 0.25, 0.50, 0.75, 0.90, 0.975])
    degenerate = np.zeros((2, len(levels)))
    result = quantile_crps_approximation([0.0, 1.0], levels, degenerate)
    assert np.allclose(result, [0.0, 1.0])
    boundaries = np.concatenate(([0.0], (levels[:-1] + levels[1:]) / 2.0, [1.0]))
    assert np.isclose(np.diff(boundaries).sum(), 1.0)


def test_native_quantile_crossing_is_rejected_without_crashing_gate() -> None:
    frame = pd.DataFrame(
        {
            "y_true": [0.0, 1.0],
            "y_pred": [0.0, 1.0],
            "q025": [-2.0, -1.0],
            "q10": [-1.0, 0.0],
            "q25": [1.0, 2.0],
            "q50": [0.0, 1.0],
            "q75": [2.0, 3.0],
            "q90": [3.0, 4.0],
            "q975": [4.0, 5.0],
            "distribution_family": "crossing_fixture",
        }
    )
    metrics = native_probabilistic_metrics(frame)
    assert metrics is not None
    assert metrics["native_interval_status"] == "INVALID_QUANTILE_CROSSING"
    assert metrics["quantile_crossing_rate"] == 1.0
    assert np.isnan(metrics["weighted_interval_score"])
    assert np.isfinite(metrics["crps"])


def test_validator_frame_comparison_allows_only_numeric_roundoff() -> None:
    saved = pd.DataFrame({"model_id": ["m"], "fold_id": ["f"], "mae": [1.0], "status": ["PASS"]})
    recomputed = saved.copy()
    recomputed.loc[0, "mae"] += 1e-11
    assert _dataframes_equivalent(saved, recomputed, keys=("model_id", "fold_id"))
    recomputed.loc[0, "status"] = "FAIL"
    assert not _dataframes_equivalent(saved, recomputed, keys=("model_id", "fold_id"))


def test_model_failure_is_not_misreported_as_protocol_failure() -> None:
    assert _failure_class(RuntimeError("numerical convergence failed")) == "MODEL_EXECUTION"
    assert _failure_class(ContractViolation("sample hash mismatch")) == "PROTOCOL"


def test_fixed_seed_ensemble_averages_predictions_but_sums_compute_time() -> None:
    base = pd.DataFrame(
        {
            "sample_id": ["s1", "s2"],
            "y_pred": [1.0, 3.0],
            "fit_seconds": [2.0, 2.0],
            "inference_seconds": [0.2, 0.2],
            "peak_ram_mb": [100.0, 100.0],
            "peak_vram_mb": [50.0, 50.0],
            "artifact_size_bytes": [10, 10],
            "seed": [1, 1],
            "ensemble_member_count": [1, 1],
            "aggregation": ["single_seed", "single_seed"],
        }
    )
    second = base.copy()
    second["y_pred"] = [3.0, 5.0]
    second["fit_seconds"] = 4.0
    second["inference_seconds"] = 0.4
    second["peak_ram_mb"] = 120.0
    second["peak_vram_mb"] = 60.0
    second["artifact_size_bytes"] = 20
    ensemble = ensemble_prediction_frame([base, second])
    assert np.allclose(ensemble["y_pred"], [2.0, 4.0])
    assert ensemble["fit_seconds"].iloc[0] == 6.0
    assert np.isclose(ensemble["inference_seconds"].iloc[0], 0.6)
    assert ensemble["peak_ram_mb"].iloc[0] == 120.0
    assert ensemble["peak_vram_mb"].iloc[0] == 60.0
    assert ensemble["artifact_size_bytes"].iloc[0] == 30


def test_internal_shortlist_excludes_models_without_spatial_audit() -> None:
    eligibility = pd.DataFrame(
        {
            "model_id": ["Z01_elastic_net", "Z02_huber"],
            "leave_profile_macro_mae": [6.0, np.nan],
            "leave_zone_macro_mae": [6.1, np.nan],
        }
    )
    aggregate = pd.DataFrame(
        {"model_id": ["Z01_elastic_net", "Z02_huber"], "mae": [6.2, 1.0]}
    )
    probabilistic = pd.DataFrame(
        columns=["model_id", "design", "dimension", "weighted_interval_score"]
    )
    shortlist = build_internal_shortlist(
        "B7_two_regime_imm",
        "PASS_NO_NEW_PRIMARY",
        eligibility,
        aggregate,
        probabilistic,
    )
    assert shortlist["context_only_models"] == ["Z01_elastic_net"]


def test_worker_traceback_provenance_does_not_expose_absolute_paths() -> None:
    try:
        raise RuntimeError("fixture")
    except RuntimeError as exc:
        frames = _traceback_frames(exc, ROOT)
    assert frames
    assert all(":" not in frame["file"] for frame in frames)
    assert all(frame["origin"] in {"repository", "external_dependency"} for frame in frames)
