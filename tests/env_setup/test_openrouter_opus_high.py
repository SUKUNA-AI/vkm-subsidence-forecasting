"""New user-selected Opus profile: offline/fake tests only, no keys or HTTP."""
import json
from decimal import Decimal

import pytest

from test_openrouter_delegate_v2 import d, metadata as all_metadata, real_job, response


def metadata():
    row = next(row for row in all_metadata() if row["model"] == "anthropic/claude-opus-5.5")
    model = row["model_capabilities"]["model"]
    model["reasoning"] = {"mandatory": True, "default_effort": "high", "supported_efforts": ["max", "xhigh", "high", "medium", "low"]}
    ep = row["metadata"]["data"]["endpoints"][0]
    ep.update(max_completion_tokens=128000, supports_implicit_caching=False,
              pricing={"prompt": "0.000004", "completion": "0.00002", "input_cache_read": "0.0000002",
                       "input_cache_write": "0.000005", "input_cache_write_1h": "0.000008"})
    return [row]


def job():
    value = real_job("critical_review")
    value.update(task_kind="code_review", reasoning_effort="high", max_tokens=12000)
    return value


def test_opus_exact_pin_and_high_wire_without_changing_other_profiles():
    assert d.PROFILES == {
        "cheap_code": ("deepseek/deepseek-v4.1-flash-1017", "stepfun/fp8", "StepFun", "0.10"),
        "vision_documents": ("google/gemini-4-hydrogen", "google-ai-studio/flex", "Google AI Studio", "0.30"),
        "engineering_research": ("moonshotai/kimi-k3.1-pro", "decart/mxfp4", "Decart", "1.00"),
        "critical_review": ("anthropic/claude-opus-5.5", "google-vertex/global", "Google", "1.00"),
    }
    j = job(); del j["reasoning_effort"]
    request, estimate, evidence = d.prepare(j, metadata())
    assert request["model"] == "anthropic/claude-opus-5.5"
    assert request["provider"] == {"only": ["google-vertex/global"], "allow_fallbacks": False, "require_parameters": True}
    assert request["reasoning"] == {"enabled": True, "effort": "high", "exclude": True}
    assert estimate <= Decimal("1.00")
    assert evidence["reasoning_policy"]["effective_effort"] is None
    assert d.CANONICAL_MODEL_PINS[request["model"]] == "anthropic/claude-opus-5.5-20260921"


def test_opus_cannot_use_legacy_wire_without_high_capability_check():
    j = {"task_id": "legacy-opus", "synthetic": True, "profile": "critical_review", "prompt": "Offline legacy boundary test", "max_tokens": 256}
    with pytest.raises(d.DelegateError, match="V2_TASK_KIND_REQUIRED"): d.prepare(j, metadata())


def test_budget_is_checked_without_shrinking_output_or_effort():
    j = job(); request, estimate, evidence = d.prepare(j, metadata())
    assert request["max_tokens"] == 12000 and request["reasoning"]["effort"] == "high"
    assert evidence["accounting"]["input_rate_bound"] == "0.000004"
    assert evidence["accounting"]["input_rate_method"] == "PROMPT_RATE_NO_REQUESTED_OR_IMPLICIT_CACHING"
    rows = metadata(); rows[0]["metadata"]["data"]["endpoints"][0]["supports_implicit_caching"] = True
    _, conservative, evidence = d.prepare(j, rows)
    assert conservative > estimate and evidence["accounting"]["input_rate_bound"] == "0.000008"
    j["max_tokens"] = 128000
    with pytest.raises(d.DelegateError, match="CALL_COST_CAP"): d.prepare(j, metadata())
    assert j["max_tokens"] == 128000 and j["reasoning_effort"] == "high"


@pytest.mark.parametrize("change,error", [("endpoint", "ENDPOINT_UNAVAILABLE"), ("canonical", "STALE_OR_WRONG_REASONING_CAPABILITIES"),
                                          ("effort", "UNSUPPORTED_REASONING_MODE"), ("price", "UNKNOWN_PRICING")])
def test_opus_rejects_wrong_or_unknown_capabilities_instead_of_fallback(change, error):
    rows = metadata(); row = rows[0]; ep = row["metadata"]["data"]["endpoints"][0]
    if change == "endpoint": ep["tag"] = "google-vertex/us"
    elif change == "canonical": row["model_capabilities"]["model"]["canonical_slug"] = "anthropic/claude-opus-5.5-latest"
    elif change == "effort": row["model_capabilities"]["model"]["reasoning"]["supported_efforts"] = ["medium", "low"]
    else: ep["pricing"]["unknown_charge"] = "0.01"
    with pytest.raises(d.DelegateError, match=error): d.prepare(job(), rows)


@pytest.mark.parametrize("model", ["google/gemini-4-carbon", "google/gemini-4-carbon-20261110"])
def test_carbon_response_cannot_be_used_as_opus_fallback(model):
    r = response("critical_review"); r["model"] = model
    with pytest.raises(d.DelegateError, match="BLOCKED_403"): d.verify_route(r, {}, "critical_review")


def test_google_name_alone_does_not_confirm_global_endpoint_or_effective_high():
    r = response("critical_review"); del r["provider_endpoint"]
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.verify_route(r, {"provider_name": "Google"}, "critical_review")
    request, _, evidence = d.prepare(job(), metadata())
    observed = d.reasoning_observation({"reasoning": {"effort": "high"}}, {}, evidence["reasoning_policy"])
    assert observed["effective_effort"] is None and observed["effective_confirmation"] == "UNCONFIRMED"
    assert request["reasoning"]["exclude"] is True


def test_mock_receipt_keeps_requested_high_without_reasoning_text_or_network(tmp_path):
    class NoNetwork:
        def request(self, *args, **kwargs): raise AssertionError("mock must not call HTTP")
    result = d.run(job(), metadata(), tmp_path / "mock", transport=NoNetwork(), mock=True)
    assert result["verification"] == "MOCK_PASS"
    assert result["requested_model"] == "anthropic/claude-opus-5.5"
    assert result["reasoning"] == {"enabled": True, "effort": "high", "exclude": True}
    assert result["reasoning_verification"]["effective_effort"] is None
    clean = d.redact({"reasoning": result["reasoning"], "message": {"reasoning": "PRIVATE_THINKING", "reasoning_details": ["HIDDEN"], "reasoning_content": "HIDDEN"}}, None)
    assert clean["reasoning"] == result["reasoning"] and clean["message"] == {}
    assert "PRIVATE_THINKING" not in json.dumps(clean)


def test_new_profile_uses_same_ledger_history_and_twenty_dollar_total(tmp_path):
    ledger = tmp_path / "same-ledger.json"
    d.write_json(ledger, {"key_id": "same-existing-key", "budget_usd": "20", "spent_usd": "19.5",
                          "reservations": {}, "blocked": False, "reservation_count": 17})
    before = ledger.read_bytes()
    with pytest.raises(d.DelegateError, match="BUDGET_EXCEEDED"):
        d.reserve(ledger, "20", "same-existing-key", "100", "1", profile="critical_review")
    assert ledger.read_bytes() == before


def test_explicit_complex_mock_output_checks_same_schema_but_never_live(tmp_path):
    j = job(); j["schema"] = {"type": "object", "properties": {"patch": {"type": "string"}, "findings": {"type": "array", "items": {"type": "string"}}}, "required": ["patch", "findings"], "additionalProperties": False}
    fixture = {"patch": "OFFLINE_FIXTURE_NOT_MODEL_CODE", "findings": []}
    result = d.run(j, metadata(), tmp_path / "complex", mock=True, mock_output=fixture)
    assert result["verification"] == "MOCK_PASS" and result["acceptance"]["API_SUCCESS"] == "MOCK_ONLY"
    assert result["mock_output_source"] == "EXPLICIT_OFFLINE_FIXTURE_NOT_MODEL_OUTPUT"
    assert json.loads((tmp_path / "complex/output.json").read_text()) == fixture
    invalid = d.run(j, metadata(), tmp_path / "invalid", mock=True, mock_output={"result": "wrong schema"})
    assert invalid["verification"] == "SCHEMA_MISMATCH"
    with pytest.raises(d.DelegateError, match="MOCK_OUTPUT_ONLY"):
        d.run(j, metadata(), tmp_path / "live", mock_output=fixture)
    assert not (tmp_path / "live").exists()


@pytest.mark.parametrize("contradiction", [
    {"is_byok": True}, {"is_byok": 1}, {"is_byok": "false"},
    {"service_tier": "flex"}, {"provider_endpoint": "google-vertex/us"},
])
def test_opus_router_metadata_cannot_override_exact_endpoint(contradiction):
    observed = response("critical_review")
    observed["openrouter_metadata"] = {"requested": observed["model"], **contradiction}
    with pytest.raises(d.DelegateError, match="ROUTING_MISMATCH"):
        d.verify_route(observed, {}, "critical_review")


def test_opus_router_metadata_explicit_non_byok_control():
    observed = response("critical_review")
    observed["openrouter_metadata"] = {"requested": observed["model"], "is_byok": False}
    assert d.verify_route(observed, {}, "critical_review")["basis"] == "EXPLICIT_ENDPOINT_TIER_OR_QUANTIZATION"


@pytest.mark.parametrize("payload", [
    {"text": "PRIVATE_REASONING_SENTINEL"},
    {"enabled": True, "effort": "high", "text": "PRIVATE_REASONING_SENTINEL"},
    {"enabled": True, "effort": "high", "vendor": {"summary": "PRIVATE_REASONING_SENTINEL"}},
    {"enabled": "PRIVATE_REASONING_SENTINEL", "exclude": {}, "mandatory": [],
     "effort": "PRIVATE_REASONING_SENTINEL", "default_effort": "PRIVATE_REASONING_SENTINEL",
     "supported_efforts": ["high", "PRIVATE_REASONING_SENTINEL"], "max_tokens": True},
])
def test_redact_reasoning_dict_rejects_unknown_fields_and_wrong_types(payload):
    cleaned = d.redact({"message": {"reasoning": payload, "content": "public answer"}}, None)
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(cleaned)
    assert cleaned["message"]["content"] == "public answer"


def test_redact_retains_typed_wire_and_catalogue_reasoning():
    wire = {"enabled": True, "exclude": True, "effort": "high", "max_tokens": 12000}
    capability = {"mandatory": True, "default_effort": "high", "supported_efforts": ["high", "medium", "low"]}
    assert d.redact({"reasoning": wire}, None) == {"reasoning": wire}
    assert d.redact({"reasoning": capability}, None) == {"reasoning": capability}
    # Live discovery is sanitized BEFORE prepare: retain capabilities for H/K/D/O.
    for profile in d.PROFILES:
        d.prepare(real_job(profile), d.redact(all_metadata(), None))
