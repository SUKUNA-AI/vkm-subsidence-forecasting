#!/usr/bin/env python3
"""Construction only. Run after core representation acceptance; never fit/forward."""
import argparse
import inspect
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["baselines", "c01"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--core-receipt", required=True)
    args = parser.parse_args()
    receipt = json.loads((ROOT/args.core_receipt).read_text(encoding="utf-8"))
    if receipt["status"] != "PASS_REPRESENTATION_CORE":
        raise ValueError("Complete representation core acceptance before interface smoke")
    from skru1.scenario_boundary_v2_1 import DATA_SHA, model_worker_scope
    result = dict(status="PASS", dataset_manifest_sha256=DATA_SHA, models_fitted=0,
                  models_executed=0, model_scoring=0, model_forward_calls=0, legacy_holdout_labels_parsed=0)
    with model_worker_scope(ROOT):
        if args.kind == "baselines":
            from skru1.baselines import PersistenceLastRate, FixedKalmanRate
            from skru1.adaptive_kalman import AdaptiveKalmanRate, prepare_kalman_history
            from skru1.imm_kalman import TwoRegimeIMMRate
            from skru1.robust_imm import RobustInnovationIMMRate
            from skru1.scenario_adapter_v2_1 import load_model_data
            from skru1.scenario_worker_v2_1 import origin_inputs
            names = []
            for cls in (PersistenceLastRate, FixedKalmanRate, AdaptiveKalmanRate, TwoRegimeIMMRate, RobustInnovationIMMRate):
                instance = cls(model_id="construction_only", parameters={})
                assert instance.fallback_rate_ is None
                assert "history_frame" in inspect.signature(instance.predict).parameters
                names.append(cls.__name__)
            bundle = load_model_data(ROOT)
            inputs = origin_inputs(bundle, bundle.frames.sample_id.iloc[0])
            prepared = prepare_kalman_history(inputs.history)
            assert prepared.source_rows == len(inputs.history) and len(prepared.points) == 1
            result.update(constructors=names, history_rows=prepared.source_rows, prediction_called=False,
                          parameters_validated_for_fit=False, note="Interface compatibility only; benchmark parameters remain preregistration work")
        else:
            import torch
            from skru1.gate_c1_models import PackedRecurrentRegressor
            config = json.loads((ROOT/"configs/scenario_sequences_v2_1.json").read_text(encoding="utf-8"))
            spec = config["architecture_construction_spec"]
            torch.manual_seed(spec["seed"])
            instance = PackedRecurrentRegressor(**{name:spec[name] for name in ("input_size", "hidden_size", "layers", "dropout", "cell")})
            assert instance.recurrent.input_size == 5
            assert tuple(instance.recurrent.weight_ih_l0.shape) == (48, 5)
            assert {"x", "lengths", "padding_mask", "observation_mask", "missing_campaign_mask"}.issubset(inspect.signature(instance.forward).parameters)
            result.update(architecture=spec, torch_version=torch.__version__, checkpoint_loaded=False,
                          input_shape=["batch", 16, 5], weight_ih_l0_shape=list(instance.recurrent.weight_ih_l0.shape),
                          status="PASS_ARCHITECTURE_CONSTRUCTION_ONLY", historical_C01_reproduction=False,
                          missing_campaign_type_invented=False, forward_executed=False)
    path = (ROOT/args.output).resolve()
    if not path.is_relative_to(ROOT) or path.exists():
        raise ValueError("Use a new repository-relative receipt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
