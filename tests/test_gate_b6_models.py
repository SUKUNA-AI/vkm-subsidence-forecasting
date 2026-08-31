from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from skru1.b6_models import create_adapter
from skru1.b6_registry import build_model_registry
from skru1.data_contracts import ContractViolation, load_canonical_bundle
from skru1.evaluation import derived_dataset
from skru1.gate_b6 import load_gate_b6_config
from skru1.splits import load_split_dataset


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def fixture_data():
    config = load_gate_b6_config(ROOT)[1]
    registry = {spec.model_id: spec for spec in build_model_registry(ROOT, config)}
    bundle = load_canonical_bundle(ROOT)
    source = load_split_dataset("t1", "train", root=ROOT)
    dates = source.frame["target_date"]
    unique = sorted(dates.unique())
    train_ids = tuple(source.frame.loc[dates.isin(unique[:8]), "sample_id"].astype(str))
    validation_ids = tuple(source.frame.loc[dates.eq(unique[8]), "sample_id"].astype(str))
    train = derived_dataset(source, train_ids, split="train", label="b6_cpu_fixture_train")
    validation = derived_dataset(source, validation_ids, split="validation", label="b6_cpu_fixture_validation")
    return registry, bundle, train, validation


@pytest.mark.parametrize(
    "model_id",
    ["Z01_elastic_net", "Z02_huber", "Z03_rbf_svr", "Z06_hist_gradient_boosting"],
)
def test_dependency_light_cpu_adapters_fit_and_predict(model_id: str, fixture_data) -> None:
    registry, bundle, train, validation = fixture_data
    spec = registry[model_id]
    adapter = create_adapter(
        spec,
        spec.parameter_grid[0],
        contract=bundle.feature_contract,
        seed=42117,
    )
    adapter.fit(train, validation=validation)
    first = adapter.predict(validation).mean
    second = adapter.predict(validation).mean
    assert first.shape == (len(validation.frame),)
    assert np.isfinite(first).all()
    assert np.allclose(first, second, rtol=0, atol=1e-12)


def test_quantile_hgb_outputs_all_preregistered_quantiles(fixture_data) -> None:
    registry, bundle, train, validation = fixture_data
    spec = registry["Z07_quantile_hist_gradient_boosting"]
    adapter = create_adapter(
        spec,
        spec.parameter_grid[0],
        contract=bundle.feature_contract,
        seed=42117,
    )
    adapter.fit(train, validation=validation)
    prediction = adapter.predict(validation)
    assert set(prediction.quantiles) == {0.025, 0.10, 0.25, 0.50, 0.75, 0.90, 0.975}
    assert np.allclose(prediction.mean, prediction.quantiles[0.5])


def test_gee_ar1_serialization_roundtrip_preserves_predictions(fixture_data) -> None:
    joblib = pytest.importorskip("joblib")
    pytest.importorskip("statsmodels")
    registry, bundle, train, validation = fixture_data
    spec = registry["Z05_gaussian_gee"]
    parameters = next(
        item for item in spec.parameter_grid if item["working_correlation"] == "AR1"
    )
    adapter = create_adapter(
        spec,
        parameters,
        contract=bundle.feature_contract,
        seed=42117,
    )
    adapter.fit(train)
    expected = adapter.predict(validation).mean
    path = ROOT / "work" / "tests" / "gee_serialization" / "gee.joblib"
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(adapter, path)
    restored = joblib.load(path)
    observed = restored.predict(validation).mean
    assert np.allclose(observed, expected, rtol=0, atol=1e-12)
    assert restored.state_dict()["model_state"]["parameter_count"] > 0


def test_catboost_without_eval_set_uses_frozen_iteration_count(monkeypatch, fixture_data) -> None:
    registry, bundle, train, validation = fixture_data
    del validation

    class FakeCatBoostRegressor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def fit(self, **kwargs):
            self.fit_kwargs = kwargs

        def get_best_iteration(self):
            return None

        def predict(self, matrix):
            return np.zeros(len(matrix), dtype=float)

    monkeypatch.setitem(
        sys.modules,
        "catboost",
        SimpleNamespace(CatBoostRegressor=FakeCatBoostRegressor),
    )
    spec = registry["Z10_catboost"]
    parameters = dict(spec.parameter_grid[0])
    parameters["frozen_iterations"] = 37
    adapter = create_adapter(spec, parameters, contract=bundle.feature_contract, seed=42117)
    adapter.fit(train)
    assert adapter.effective_iterations_ == 37


def test_tabpfn_adapter_is_removed_by_governance_amendment(fixture_data) -> None:
    registry, bundle, train, validation = fixture_data
    del train, validation
    spec = registry["Z15_tabpfn_v2_6"]
    with pytest.raises(ContractViolation, match="B6-GOV-001"):
        create_adapter(
            spec,
            spec.fixed_parameters,
            contract=bundle.feature_contract,
            seed=42117,
        )


def test_enfs_implements_pi_membership_not_bell_approximation() -> None:
    source = (ROOT / "src" / "skru1" / "b6_models.py").read_text(encoding="utf-8")
    enfs_source = source[source.index("def _build_enfs") : source.index("def _seed_torch")]
    assert "first_shoulder" in enfs_source
    assert "second_shoulder" in enfs_source
    assert "torch.zeros_like(normalized)" in enfs_source
    assert "1.0 / (1.0 + normalized**2) ** 2" not in enfs_source
