"""CLI outcome accounting only; no native operator or runtime is constructed."""
import json
from types import SimpleNamespace

import pytest

from vkm_corpus.update import operator


@pytest.mark.parametrize(("action", "status", "exit_code"), [
    ("status", "CLOSED_BASELINE_QUALIFIED", 0),
    ("recover", "RESTORED_CLOSED", 0),
    ("status", "READY", 0),
    ("recover", "RESTORED", 0),
    ("switch", "PASS", 0),
    ("accept", "PASS", 0),
    ("drill", "PASS", 0),
    ("plan", "READY", 0),
    ("drill-plan", "READY", 0),
    ("recovery-plan", "READY", 0),
    ("status", "CLOSED", 2),
    ("recover", "CLOSED", 2),
    ("status", "MAINTENANCE", 2),
    ("status", "UNAVAILABLE", 2),
    ("recover", "READY", 2),
    ("accept", "RESTORED_CLOSED", 2),
    ("switch", "CLOSED_BASELINE_QUALIFIED", 2),
    ("drill", "READY", 2),
    ("status", "PASS", 2),
])
def test_terminal_status_is_success_only_for_its_own_action(monkeypatch, capsys, action, status, exit_code):
    result = {"status": status, "scope": "SHADOW_PRODUCTION",
              "serving_admission": False, "scientific_admission": False}
    calls = []

    class FixedOperator:
        def __init__(self, config, *, config_ref, recovery_only):
            assert recovery_only is (action in {"recover", "recovery-plan"})

        def close(self):
            calls.append("closed")

        def __getattr__(self, name):
            assert name == action.replace("-", "_")
            def invoke(*args):
                calls.append(name)
                return result
            return invoke

    monkeypatch.setattr(operator, "CoreOperator", FixedOperator)
    monkeypatch.setattr(operator, "CoreOperatorConfig", SimpleNamespace(model_validate=lambda raw: raw))
    monkeypatch.setattr(operator, "bound_json", lambda ref: {})
    args = SimpleNamespace(operator_config="unused-synthetic.json", config_sha256="a" * 64,
        deployment_command=action, request_id="synthetic-request", confirm_plan="b" * 64)

    assert operator.command(args) == exit_code
    # Successful command accounting must not promote a CLOSED state to READY,
    # serving admission, or scientific qualification.
    assert json.loads(capsys.readouterr().out) == result
    assert calls == [action.replace("-", "_"), "closed"]
