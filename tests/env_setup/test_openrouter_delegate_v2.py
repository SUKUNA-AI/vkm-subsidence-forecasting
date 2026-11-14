"""V2 contracts use fake transports only; never read API keys or make HTTP calls."""
import base64
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("vkm_delegate_v2_test", ROOT / ".agents/skills/openrouter-delegate/scripts/openrouter_call.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def metadata():
    result = []
    for profile, (model, tag, provider, _) in d.PROFILES.items():
        params = ["reasoning", "reasoning_effort", "max_tokens", "response_format", "structured_outputs"]
        reasoning = {"supported_efforts": ["xhigh", "high", "medium", "low"] if profile == "vision_documents" else ["max", "high", "low", "none"],
                     "default_effort": "medium" if profile == "vision_documents" else "max" if profile == "engineering_research" else "high",
                     "mandatory": profile == "vision_documents"}
        ep = {"model_id": model, "provider_name": provider, "tag": tag, "status": 0,
              "service_tier": "flex" if profile == "vision_documents" else None,
              "quantization": "fp8" if profile == "cheap_code" else "mxfp4" if profile == "engineering_research" else "unknown",
              "max_completion_tokens": 500000, "context_length": 1000000,
              "supported_parameters": params, "pricing": {"prompt": "0.0000001", "completion": "0.0000005", "image": "0"}}
        result.append({"model": model, "checked_at": d.now(), "http_status": 200,
                       "metadata": {"data": {"id": model, "architecture": {"input_modalities": ["text", "image"]}, "endpoints": [ep]}},
                       "model_capabilities": {"checked_at": d.now(), "catalogue_sha256": "a" * 64,
                         "model": {"id": model, "canonical_slug": d.CANONICAL_MODEL_PINS[model], "reasoning": reasoning,
                                   "supported_parameters": params}}})
    return result


def job(profile="cheap_code", task="code_implementation"):
    return {"task_id": "v2-contract", "synthetic": True, "profile": profile, "task_kind": task,
            "prompt": "Return one bounded implementation candidate in JSON result.", "max_tokens": 8192}


def real_job(profile="cheap_code", text="def useful(value):\n    return value + 1\n", kind="PUBLIC_CODE"):
    j = job(profile)
    j["synthetic"] = False
    digest = hashlib.sha256(text.encode()).hexdigest()
    artifact = {"artifact_id": "module", "kind": kind, "source_id": "VKM-SRC-064" if kind == "SOURCE" else None,
                "work_id": "VKM-WRK-064" if kind == "SOURCE" else None,
                "logical_name": "src/module.py", "full_sha256": digest,
                "fragments": [{"text": text, "sha256": digest, "locator": {"kind": "FULL_FILE", "value": "src/module.py"}}], "images": []}
    j["input_package"] = {"schema": "vkm.delegate-artifact-package/2", "authorization_scope": "VKM-MULTIAGENT-DELEGATION-V2",
                          "selection_rationale": "Only this necessary module, no whole repository", "artifacts": [artifact]}
    return j


def image(width=16, height=16):
    from PIL import Image
    stream = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(stream, "PNG")
    return {"mime": "image/png", "data_base64": base64.b64encode(stream.getvalue()).decode(),
            "width": width, "height": height, "bbox": [0, 0, width, height]}


def response(profile="cheap_code"):
    model, tag, provider, _ = d.PROFILES[profile]
    return {"id": "gen-v2-fake", "model": model, "provider": provider, "provider_endpoint": tag,
            "choices": [{"finish_reason": "stop", "message": {"content": '{"result":"candidate"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130, "cost": 0.00001}}


class FakeV2Transport:
    def __init__(self, profile="cheap_code", fresh=None):
        self.profile, self.rows, self.posts, self.bodies = profile, fresh or metadata(), 0, []

    def request(self, method, path, body=None):
        if path == "models":
            return {"data": [r["model_capabilities"]["model"] for r in self.rows]}
        if path.startswith("models/"):
            model = path[len("models/"):-len("/endpoints")]
            return next(r["metadata"] for r in self.rows if r["model"] == model)
        if path == "key":
            return {"data": {"limit": 20, "limit_remaining": 19.5}}
        if path.startswith("generation?"):
            r = response(self.profile)
            return {"data": {"id": r["id"], "model": r["model"], "provider_name": r["provider"], "provider_endpoint": r["provider_endpoint"]}}
        if method == "POST":
            self.posts += 1
            self.bodies.append(copy.deepcopy(body))
            return response(self.profile)
        raise AssertionError("Unexpected fake protocol call")


@pytest.mark.parametrize("profile", list(d.PROFILES))
def test_all_three_profiles_are_text_and_code_coexecutors_high_default(profile):
    req, _, evidence = d.prepare(real_job(profile), metadata())
    assert req["reasoning"] == {"enabled": True, "effort": "high"}
    assert req["provider"] == {"only": [d.PROFILES[profile][1]], "allow_fallbacks": False, "require_parameters": True}
    assert "def useful" in req["messages"][0]["content"][1]["text"]
    assert evidence["reasoning_policy"]["effective_confirmation"] == "UNCONFIRMED"


@pytest.mark.parametrize("task,effort", [("mechanical", "none"), ("data_transform", "low"), ("tests", "high"), ("code_review", "high")])
def test_deepseek_effort_adapts_to_explicit_task_kind(task, effort):
    req, _, _ = d.prepare(job(task=task), metadata())
    assert req["reasoning"] == {"enabled": effort != "none", "effort": effort}


@pytest.mark.parametrize("profile,effort", [("vision_documents", "max"), ("engineering_research", "xhigh"), ("cheap_code", "medium")])
def test_unsupported_effort_fails_before_post_and_never_downgrades(profile, effort):
    j = job(profile)
    j.update(reasoning_effort=effort, reasoning_justification="Specific difficult implementation")
    with pytest.raises(d.DelegateError, match="UNSUPPORTED_REASONING_MODE"):
        d.prepare(j, metadata())


@pytest.mark.parametrize("profile,effort", [("vision_documents", "xhigh"), ("engineering_research", "max"), ("cheap_code", "max")])
def test_hard_supported_effort_requires_specific_reason(profile, effort):
    j = job(profile)
    j["reasoning_effort"] = effort
    with pytest.raises(d.DelegateError, match="HARD_REASONING_JUSTIFICATION_REQUIRED"):
        d.prepare(j, metadata())
    j["reasoning_justification"] = "Check one difficult nonlinear transformation against a counterexample"
    assert d.prepare(j, metadata())[0]["reasoning"]["effort"] == effort


@pytest.mark.parametrize("mutation", ["missing", "empty", "stale", "wrong_model", "missing_parameter"])
def test_unknown_stale_or_wrong_capabilities_do_not_use_provider_default(mutation):
    rows = metadata()
    item = rows[0]
    if mutation == "missing":
        del item["model_capabilities"]
    elif mutation == "empty":
        item["model_capabilities"]["model"]["reasoning"]["supported_efforts"] = []
    elif mutation == "stale":
        item["model_capabilities"]["checked_at"] = "2000-01-01T00:00:00+00:00"
    elif mutation == "wrong_model":
        item["model_capabilities"]["model"]["id"] = "google/gemini-4-carbon"
    else:
        item["metadata"]["data"]["endpoints"][0]["supported_parameters"].remove("reasoning_effort")
    with pytest.raises(d.DelegateError):
        d.prepare(job(), rows)


def test_kimi_high_explicitly_overrides_catalogue_default_max_without_claiming_observation():
    request, _, evidence = d.prepare(job("engineering_research"), metadata())
    assert request["reasoning"]["effort"] == "high"
    assert evidence["reasoning_policy"]["model_default_effort"] == "max"
    assert evidence["reasoning_policy"]["effective_effort"] is None


@pytest.mark.parametrize("kind", ["PUBLIC_CODE", "PRIVATE_CODE", "PUBLIC_TEXT", "PRIVATE_TEXT", "SOURCE"])
def test_real_artifacts_not_mislabelled_synthetic_and_all_fields_bound(kind):
    j = real_job(kind=kind)
    d.prepare(j, metadata())
    assert j["synthetic"] is False


def test_source_needs_explicit_source_and_work_ids_but_code_null_is_valid():
    d.prepare(real_job(), metadata())
    j = real_job(kind="SOURCE")
    j["input_package"]["artifacts"][0]["work_id"] = None
    with pytest.raises(d.DelegateError, match="INVALID_PROVENANCE"):
        d.prepare(j, metadata())


@pytest.mark.parametrize("field,value", [("full_sha256", "fake"), ("logical_name", "https://invalid/source.py"),
    ("logical_name", "C:/private.py"), ("logical_name", "../private.py"), ("logical_name", ".env"),
    ("source_path", "/secrets"), ("source_url", "https://invalid")])
def test_artifacts_reject_fake_hashes_auto_paths_urls_and_env(field, value):
    j = real_job()
    j["input_package"]["artifacts"][0][field] = value
    with pytest.raises(d.DelegateError):
        d.prepare(j, metadata())


def test_partial_fragment_hash_is_verified_full_original_hash_remains_caller_assertion():
    j = real_job()
    artifact = j["input_package"]["artifacts"][0]
    artifact["fragments"][0]["locator"] = {"kind": "LINES", "value": "10-11"}
    artifact["full_sha256"] = "b" * 64
    d.prepare(j, metadata())
    artifact["fragments"][0]["text"] += "changed"
    with pytest.raises(d.DelegateError, match="FRAGMENT_PROVENANCE_MISMATCH"):
        d.prepare(j, metadata())


@pytest.mark.parametrize("text", ["sk-or-v1-private-secret", "ghp_" + "a" * 30, "AKIA" + "A" * 16,
                                "postgresql://owner:private@host/db", "-----BEGIN PRIVATE KEY-----"])
def test_real_inline_secrets_rejected(text):
    with pytest.raises(d.DelegateError, match="FORBIDDEN_INPUT"):
        d.prepare(real_job(text=text), metadata())


def test_no_artificial_64k_four_fragment_or_4k_output_limit():
    j = real_job(text="bounded module contents\n" * 3000)
    artifact = j["input_package"]["artifacts"][0]
    artifact["fragments"] = artifact["fragments"] * 5
    j["max_tokens"] = 65536
    request, _, _ = d.prepare(j, metadata())
    assert request["max_tokens"] == 65536 and len(request["messages"][0]["content"]) == 6


def test_image_count_and_resolution_not_smoke_caps_and_refs_must_cover_every_image():
    j = real_job(profile="vision_documents")
    j["images"] = [image(2100, 1), image(), image()]
    artifact = j["input_package"]["artifacts"][0]
    artifact["images"] = [{"image_index": i, "sha256": hashlib.sha256(base64.b64decode(img["data_base64"])).hexdigest(),
                           "locator": {"kind": "CROP", "value": "source crop with explicit input frame"}} for i, img in enumerate(j["images"])]
    d.prepare(j, metadata())
    artifact["images"].pop()
    with pytest.raises(d.DelegateError, match="UNREFERENCED_IMAGE"):
        d.prepare(j, metadata())


@pytest.mark.parametrize("profile", list(d.PROFILES))
def test_actual_endpoint_completion_limit_not_profile_smoke_ceiling(profile):
    j = job(profile)
    j["max_tokens"] = 65536
    assert d.prepare(j, metadata())[0]["max_tokens"] == 65536
    rows = metadata()
    next(row for row in rows if row["model"] == d.PROFILES[profile][0])["metadata"]["data"]["endpoints"][0]["max_completion_tokens"] = 65535
    with pytest.raises(d.DelegateError, match="UNSUPPORTED_PARAMETERS"):
        d.prepare(j, rows)


def test_unknown_tariff_and_context_overflow_fail_before_post():
    rows = metadata()
    ep = rows[0]["metadata"]["data"]["endpoints"][0]
    ep["pricing"]["new_paid_item"] = "0.01"
    with pytest.raises(d.DelegateError, match="UNKNOWN_PRICING"):
        d.prepare(job(), rows)
    del ep["pricing"]["new_paid_item"]
    ep["context_length"] = 100
    with pytest.raises(d.DelegateError, match="INPUT_LIMIT"):
        d.prepare(job(), rows)


def test_image_price_is_per_image_not_per_pixel_and_output_reasoning_reserve_conservative():
    j = job("vision_documents")
    j["images"] = [image()]
    rows = metadata()
    ep = rows[1]["metadata"]["data"]["endpoints"][0]
    ep["pricing"]["image"] = "0.001"
    ep["pricing"]["internal_reasoning"] = "0.0000001"
    _, amount, evidence = d.prepare(j, rows)
    assert evidence["accounting"]["image_tariff_usd_per_image"] == "0.001"
    assert evidence["accounting"]["output_rate_bound"] == "6E-7"
    assert amount < .1


def test_request_or_echo_does_not_confirm_provider_effective_effort():
    j = job()
    _, _, evidence = d.prepare(j, metadata())
    r = response()
    r["reasoning"] = {"effort": "high"}
    proof = {}
    d.validate_response(r, {}, j, endpoint_evidence=evidence, verification_evidence=proof)
    assert proof["reasoning"]["effective_confirmation"] == "UNCONFIRMED"
    assert proof["reasoning"]["effective_effort"] is None
    assert proof["reasoning"]["echo_observations"]


def test_explicit_provider_report_separate_and_mismatch_refused():
    j = job()
    _, _, evidence = d.prepare(j, metadata())
    r = response()
    r["provider_metadata"] = {"reasoning_effort": "high"}
    proof = {}
    d.validate_response(r, {}, j, endpoint_evidence=evidence, verification_evidence=proof)
    assert proof["reasoning"]["effective_confirmation"] == "PROVIDER_METADATA_REPORTED_NOT_INDEPENDENTLY_MEASURED"
    r["provider_metadata"]["reasoning_effort"] = "low"
    with pytest.raises(d.DelegateError, match="REASONING_MODE_MISMATCH"):
        d.validate_response(r, {}, j, endpoint_evidence=evidence)


def test_mock_receipt_has_hashes_not_real_artifact_text_and_acceptance_not_scientific(tmp_path):
    j = real_job(text="PRIVATE_ARTIFACT_TEXT_SHOULD_NOT_BE_IN_RECEIPT")
    receipt = d.run(j, metadata(), tmp_path / "mock", mock=True)
    assert receipt["verification"] == "MOCK_PASS"
    assert receipt["input_kind"] == "AUTHORIZED_INLINE_ARTIFACTS"
    assert "PRIVATE_ARTIFACT_TEXT" not in (tmp_path / "mock/receipt.json").read_text()
    assert receipt["acceptance"]["OUTPUT_SCHEMA_VALID"] == "PASS"
    assert receipt["acceptance"]["SCIENTIFICALLY_ACCEPTED"] == "NOT_RUN"
    assert receipt["reasoning_verification"]["effective_confirmation"] == "UNCONFIRMED"


def test_new_legacy_live_cannot_use_normal_or_fake_transport_without_explicit_offline_contract(tmp_path):
    j = job()
    del j["task_kind"]
    j["max_tokens"] = 256  # valid legacy fixture; assert the live-policy gate
    for t in (None, FakeV2Transport(), d.Transport()):
        with pytest.raises(d.DelegateError, match="V2_TASK_KIND_REQUIRED"):
            d.run(j, metadata(), tmp_path / "blocked", transport=t)
    assert not (tmp_path / "blocked").exists()


def test_builtin_replay_transport_is_frozen_and_never_creates_network(monkeypatch):
    events = [{"method": "GET", "path": "key", "response": {"data": {"limit": 1}}}]
    replay = d.ReplayTransport(events)
    events[0]["response"]["data"]["limit"] = 9
    assert replay.request("GET", "key")["data"]["limit"] == 1
    with pytest.raises(d.DelegateError, match="REPLAY_EXHAUSTED"):
        replay.request("POST", "chat/completions")


def test_discovery_fetches_exact_endpoint_and_catalogue_only_for_selected_model():
    t = FakeV2Transport()
    rows = d.discover(t, "cheap_code", include_reasoning=True)
    assert len(rows) == 1 and rows[0]["model"] == d.PROFILES["cheap_code"][0]
    assert rows[0]["model_capabilities"]["model"]["reasoning"]["default_effort"] == "high"
    assert t.posts == 0


def test_live_fake_has_fresh_effort_and_passes_exact_profile_to_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v2-fake-only")
    held, settled = [], []
    monkeypatch.setattr(d, "reserve", lambda *args, **kw: held.append(kw) or "test-token")
    monkeypatch.setattr(d, "settle", lambda *args: settled.append(args))
    monkeypatch.setattr(d, "reservation_metadata", lambda *args: {"confirmed_active": True, "scope": "fake scratch lease"}, raising=False)
    t = FakeV2Transport()
    t.read_timeout = 900.75
    result = d.run(real_job(), metadata(), tmp_path / "run", ledger=tmp_path / "scratch.json", budget="20", transport=t)
    assert result["verification"] == "PASS" and t.posts == 1
    assert held[0]["profile"] == "cheap_code"
    assert held[0]["lease_seconds"] == 1051
    assert t.bodies[0]["reasoning"]["effort"] == "high" and settled
    assert result["reasoning_verification"]["effective_confirmation"] == "UNCONFIRMED"


def test_fresh_capability_change_is_not_downgraded_or_paid(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v2-fake-only")
    fresh = metadata()
    fresh[0]["model_capabilities"]["model"]["reasoning"]["supported_efforts"] = ["low", "none"]
    t = FakeV2Transport(fresh=fresh)
    receipt = d.run(job(), metadata(), tmp_path / "run", transport=t, ledger=tmp_path / "scratch.json", budget="20")
    assert receipt["verification"] == "UNSUPPORTED_REASONING_MODE" and t.posts == 0
    assert receipt["execution_cost_status"] == "NOT_SENT"
