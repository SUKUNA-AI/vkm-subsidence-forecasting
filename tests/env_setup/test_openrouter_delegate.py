"""Synthetic contract tests. Every transport is fake; no API calls or keys read."""
import base64
import copy
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents/skills/openrouter-delegate/scripts/openrouter_call.py"
spec = importlib.util.spec_from_file_location("vkm_openrouter_delegate", SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def metadata():
    rows = []
    for model, tag, provider, _ in d.PROFILES.values():
        rows.append({"model": model, "checked_at": d.now(), "http_status": 200, "metadata": {"data": {"architecture": {"input_modalities": ["text", "image"]}, "endpoints": [{"model_id": model, "provider_name": provider, "tag": tag, "service_tier": "flex" if tag.endswith("flex") else None, "quantization": "fp8" if tag.endswith("fp8") else "unknown", "status": 0, "context_length": 1000000, "max_completion_tokens": 10000, "supported_parameters": ["reasoning", "max_tokens", "response_format", "structured_outputs"], "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}}]}}})
    return rows


def job(profile="cheap_code"):
    result = {"task_id": "synthetic-unit", "synthetic": True, "profile": profile, "prompt": "Return JSON with result containing Python add(a,b).", "max_tokens": 256, "schema": copy.deepcopy(d.DEFAULT_SCHEMA)}
    if profile == "vision_documents":
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (32, 16), "white").save(buf, format="PNG")
        result["images"] = [{"mime": "image/png", "width": 32, "height": 16, "bbox": [0, 0, 32, 16], "data_base64": base64.b64encode(buf.getvalue()).decode()}]
    return result


def response(profile="cheap_code"):
    model, tag, provider, _ = d.PROFILES[profile]
    return {"id": "gen-synthetic", "model": model, "provider": provider, "provider_endpoint": tag, "choices": [{"finish_reason": "stop", "message": {"content": '{"result":"def add(a,b): return a+b"}', "reasoning_details": [{"text": "MUST_NOT_PERSIST"}]}}], "usage": {"prompt_tokens": 8, "completion_tokens": 12, "total_tokens": 20, "cost": 0.00001}}


class FakeTransport:
    def __init__(self, reply=None, timeout=False, limit=1):
        self.reply = response() if reply is None else reply
        self.timeout = timeout
        self.posts = 0
        self.limit = limit

    def request(self, method, path, body=None):
        if path.startswith("models/"):
            model = path[len("models/"):-len("/endpoints")]
            return next(r["metadata"] for r in metadata() if r["model"] == model)
        if path == "key":
            return {"data": {"limit": self.limit, "limit_remaining": self.limit}}
        if path.startswith("generation?"):
            return {"data": {"id": self.reply["id"], "model": self.reply["model"], "provider_name": self.reply.get("provider"), "provider_endpoint": self.reply.get("provider_endpoint")}}
        if method == "POST":
            self.posts += 1
            assert body["provider"]["allow_fallbacks"] is False
            assert body["provider"]["require_parameters"] is True
            if self.timeout:
                d.fail("UNKNOWN_EXECUTION_COST")
            return copy.deepcopy(self.reply)
        raise AssertionError("unexpected transport request")


def invoke(tmp_path, monkeypatch, reply=None, transport=None, **kwargs):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")
    return d.run(job(), metadata(), tmp_path / "run", ledger=tmp_path / "ledger.json", budget="0.01", transport=transport or FakeTransport(reply), **kwargs)


@pytest.mark.parametrize("profile", list(d.PROFILES))
def test_exact_profiles_and_structured_response(profile):
    j = job(profile)
    req, estimate, _ = d.prepare(j, metadata())
    assert req["provider"] == {"only": [d.PROFILES[profile][1]], "allow_fallbacks": False, "require_parameters": True}
    assert req["reasoning"] == {"enabled": True}
    assert estimate > 0
    assert d.validate_response(response(profile), {}, j)["result"].startswith("def add")


def test_01_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    t = FakeTransport()
    r = d.run(job(), metadata(), tmp_path / "run", transport=t)
    assert r["verification"] == "LIVE_BLOCKED_SECRET" and t.posts == 0


def test_02_unknown_profile():
    j = job()
    j["profile"] = "other"
    with pytest.raises(d.DelegateError, match="UNKNOWN_PROFILE"):
        d.prepare(j, metadata())


def test_03_missing_exact_endpoint_does_not_block_others():
    m = metadata()
    m[0]["metadata"]["data"]["endpoints"][0]["tag"] = "stepfun"
    with pytest.raises(d.DelegateError, match="ENDPOINT_UNAVAILABLE"):
        d.prepare(job(), m)
    d.prepare(job("critical_review"), m)


@pytest.mark.parametrize("field,value", [("provider", {"allow_fallbacks": True}), ("models", ["other"]), ("plugins", [{"id": "web"}]), ("tools", [{}])])
def test_04_fallback_and_paid_extensions_rejected(field, value):
    j = job()
    j[field] = value
    with pytest.raises(d.DelegateError, match="FORBIDDEN_INPUT"):
        d.prepare(j, metadata())


def test_05_unsupported_parameters():
    m = metadata()
    m[0]["metadata"]["data"]["endpoints"][0]["supported_parameters"].remove("reasoning")
    with pytest.raises(d.DelegateError, match="UNSUPPORTED_PARAMETERS"):
        d.prepare(job(), m)


def test_06_budget_and_verified_key_limit(tmp_path, monkeypatch):
    with pytest.raises(d.DelegateError, match="BUDGET_EXCEEDED"):
        d.reserve(tmp_path / "small.json", ".001", "key", ".1", ".002")
    t = FakeTransport(limit=None)
    r = invoke(tmp_path, monkeypatch, transport=t)
    assert r["verification"] == "LIVE_BLOCKED_KEY_LIMIT" and t.posts == 0


def race_reserve(script, ledger, barrier, queue):
    spec = importlib.util.spec_from_file_location("race_delegate", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    barrier.wait(timeout=10)
    try:
        module.reserve(ledger, ".01", "same-key", ".01", ".01")
        queue.put("RESERVED")
    except module.DelegateError as exc:
        queue.put(exc.code)


def test_07_multiprocessing_last_budget_race(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    workers = [ctx.Process(target=race_reserve, args=(str(SCRIPT), str(tmp_path / "race.json"), barrier, queue)) for _ in range(2)]
    for proc in workers:
        proc.start()
    results = [queue.get(timeout=20) for _ in workers]
    for proc in workers:
        proc.join(timeout=20)
        assert proc.exitcode == 0
    assert results.count("RESERVED") == 1
    assert set(results) <= {"RESERVED", "PENDING_EXECUTION_COST", "BUDGET_EXCEEDED"}
    ledger = json.loads((tmp_path / "race.json").read_text())
    assert len(ledger["reservations"]) == 1


def test_08_missing_cost_blocks_batch(tmp_path, monkeypatch):
    reply = response()
    del reply["usage"]["cost"]
    r = invoke(tmp_path, monkeypatch, reply=reply)
    assert r["verification"] == "UNKNOWN_EXECUTION_COST" and r["cost_usd"] is None
    assert json.loads((tmp_path / "ledger.json").read_text())["blocked"] is True
    with pytest.raises(d.DelegateError, match="UNKNOWN_EXECUTION_COST"):
        d.reserve(tmp_path / "ledger.json", ".01", __import__("hashlib").sha256(b"sk-or-synthetic-test-only").hexdigest(), 1, ".001")


def test_09_timeout_post_never_retried_and_blocks(tmp_path, monkeypatch):
    t = FakeTransport(timeout=True)
    r = invoke(tmp_path, monkeypatch, transport=t)
    assert r["verification"] == "UNKNOWN_EXECUTION_COST" and t.posts == 1
    assert json.loads((tmp_path / "ledger.json").read_text())["blocked"] is True


def test_10_truncated_response_is_failure_but_cost_settled(tmp_path, monkeypatch):
    reply = response()
    reply["choices"][0]["finish_reason"] = "length"
    r = invoke(tmp_path, monkeypatch, reply=reply)
    assert r["verification"] == "INCOMPLETE_RESULT"
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["spent_usd"] == "0.00001" and not ledger["reservations"]


def test_11_api_error_http_200(tmp_path, monkeypatch):
    reply = response()
    reply["error"] = {"message": "bad upstream"}
    r = invoke(tmp_path, monkeypatch, reply=reply)
    assert r["verification"] == "API_ERROR"


@pytest.mark.parametrize("content,status", [("{bad", "INVALID_JSON"), ('{"wrong":1}', "SCHEMA_MISMATCH"), ("", "INVALID_CONTENT")])
def test_12_invalid_json_and_schema(tmp_path, monkeypatch, content, status):
    reply = response()
    reply["choices"][0]["message"]["content"] = content
    assert invoke(tmp_path, monkeypatch, reply=reply)["verification"] == status


@pytest.mark.parametrize("mutate", [lambda x: x.update(mime="application/pdf"), lambda x: x.update(data_base64="aGVsbG8="), lambda x: x.update(width=999), lambda x: x.update(bbox=[0, 0, 99, 99])])
def test_13_bad_images(mutate):
    j = job("vision_documents")
    mutate(j["images"][0])
    with pytest.raises(d.DelegateError, match="INVALID_IMAGE"):
        d.prepare(j, metadata())


@pytest.mark.parametrize("change", [{"synthetic": False}, {"files": ["../../.env"]}, {"prompt": "api_key=sk-or-private"}, {"schema": {"$ref": "file:///etc/passwd"}}])
def test_14_forbidden_input(change):
    j = job()
    j.update(change)
    with pytest.raises(d.DelegateError, match="FORBIDDEN_INPUT|INVALID_SCHEMA"):
        d.prepare(j, metadata())


def test_15_secret_reasoning_absent_and_error_exit(tmp_path, monkeypatch):
    secret = "sk-or-synthetic-test-only"
    reply = response()
    reply["provider"] = secret
    reply["choices"][0]["message"]["content"] = secret
    r = invoke(tmp_path, monkeypatch, reply=reply)
    assert r["verification"] == "ROUTING_MISMATCH"
    text = (tmp_path / "run/receipt.json").read_text()
    assert secret not in text and "MUST_NOT_PERSIST" not in text and "reasoning_details" not in text
    j = tmp_path / "job.json"
    j.write_text(json.dumps(job()))
    m = tmp_path / "metadata.json"
    m.write_text(json.dumps(metadata()))
    env = dict(os.environ)
    env.pop("OPENROUTER_API_KEY", None)
    proc = subprocess.run([sys.executable, str(SCRIPT), "call", "--job", str(j), "--metadata", str(m), "--run-dir", str(tmp_path / "cli")], env=env, capture_output=True, text=True)
    assert proc.returncode == 2 and "LIVE_BLOCKED_SECRET" in proc.stdout
    assert secret not in proc.stdout + proc.stderr


@pytest.mark.parametrize("change,code", [({"model": "other"}, "ROUTING_MISMATCH"), ({"provider_endpoint": "stepfun/standard"}, "ROUTING_MISMATCH"), ({"provider_endpoint": None}, "PROVENANCE_UNCONFIRMED"), ({"choices": []}, "INVALID_CHOICES")])
def test_response_identity_and_choices(change, code):
    reply = response()
    reply.update(change)
    with pytest.raises(d.DelegateError, match=code):
        d.validate_response(reply, {}, job())


def test_google_provider_alone_cannot_prove_flex():
    reply = response("critical_review")
    del reply["provider_endpoint"]
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.validate_response(reply, {}, job("critical_review"))
    assert d.validate_response(reply, {"service_tier": "flex"}, job("critical_review"))


def test_mock_saves_sanitized_success(tmp_path):
    r = d.run(job(), metadata(), tmp_path / "mock", mock=True)
    assert r["verification"] == "MOCK_PASS" and r["cost_usd"] == "0"
    assert (tmp_path / "mock/output.json").exists()


@pytest.mark.parametrize("status,payload,expected", [(200, b'{"error":{"message":"secret upstream details"}}', "API_ERROR"), (500, b'{"error":{"message":"secret upstream details"}}', "HTTP_500"), (200, b'bad-json', "INVALID_JSON")])
def test_transport_errors_and_authorization_each_call(monkeypatch, status, payload, expected):
    seen = []

    class FakeConnection:
        def __init__(self, host, **kwargs):
            assert host == "openrouter.ai" and kwargs["timeout"] == 10
            self.sock = self

        def connect(self):
            pass

        def settimeout(self, value):
            assert value == 120

        def request(self, method, path, **kwargs):
            seen.append((method, path, kwargs["headers"]))

        def getresponse(self):
            self.status = status
            return self

        def read(self, limit):
            return payload

        def close(self):
            pass

    monkeypatch.setattr(d.http.client, "HTTPSConnection", FakeConnection)
    t = d.Transport("sk-or-synthetic-test-only")
    for method in ("GET", "POST"):
        with pytest.raises(d.DelegateError, match=expected) as exc:
            t.request(method, "key", {} if method == "POST" else None)
        assert "secret upstream details" not in str(exc.value)
    assert len(seen) == 2
    assert all(headers["Authorization"] == "Bearer sk-or-synthetic-test-only" for _, _, headers in seen)


def test_post_unknown_failure_retains_reservation_and_prevents_second_post(tmp_path, monkeypatch):
    t = FakeTransport(timeout=True)
    invoke(tmp_path, monkeypatch, transport=t)
    r = d.run(job(), metadata(), tmp_path / "run-2", ledger=tmp_path / "ledger.json", budget=".01", transport=t)
    assert r["verification"] == "UNKNOWN_EXECUTION_COST" and t.posts == 1


def test_stale_metadata_and_cost_overrun(tmp_path, monkeypatch):
    m = metadata()
    m[0]["checked_at"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(d.DelegateError, match="STALE_METADATA"):
        d.prepare(job(), m)
    reply = response()
    reply["usage"]["cost"] = .1
    r = invoke(tmp_path, monkeypatch, reply=reply)
    assert r["verification"] == "COST_BOUND_EXCEEDED"
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["blocked"] and ledger["spent_usd"] == "0.1"


def test_unicode_escaped_secret_is_rejected_before_output_write(tmp_path, monkeypatch):
    reply = response()
    secret = "sk-or-synthetic-test-only"
    escaped = "".join("\\u%04x" % ord(c) for c in secret)
    reply["choices"][0]["message"]["content"] = '{"result":"' + escaped + '"}'
    receipt = invoke(tmp_path, monkeypatch, reply=reply)
    assert receipt["verification"] == "SECRET_IN_RESULT"
    assert not (tmp_path / "run/output.json").exists()
    assert secret not in (tmp_path / "run/receipt.json").read_text()


@pytest.mark.parametrize("profile,field,conflict", [("critical_review", "provider_endpoint", "google-ai-studio/standard"), ("critical_review", "service_tier", "standard"), ("cheap_code", "quantization", "fp16")])
def test_conflicting_generation_observation_cannot_be_masked(profile, field, conflict):
    reply = response(profile)
    reply["service_tier"] = "flex" if profile == "critical_review" else None
    reply["quantization"] = "fp8" if profile == "cheap_code" else "unknown"
    with pytest.raises(d.DelegateError, match="ROUTING_MISMATCH"):
        d.validate_response(reply, {field: conflict}, job(profile))


def test_owner_optin_unlimited_metadata_truthful_and_default_stays_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")
    blocked = FakeTransport(limit=None)
    r = d.run(job(), metadata(), tmp_path / "default", ledger=tmp_path / "default-ledger.json", budget="1", transport=blocked)
    assert r["verification"] == "LIVE_BLOCKED_KEY_LIMIT" and blocked.posts == 0
    assert r["server_key_limit_status"] == "UNLIMITED_OBSERVED"
    assert not (tmp_path / "default-ledger.json").exists()
    allowed = FakeTransport(limit=None)
    r = d.run(job(), metadata(), tmp_path / "optin", ledger=tmp_path / "owner-ledger.json", budget="1", transport=allowed, owner_local_cap_only=True)
    assert r["verification"] == "PASS" and allowed.posts == 1
    assert r["server_key_limit_status"] == "UNLIMITED_OBSERVED"
    assert r["override"] == "EXPLICIT_OWNER_LOCAL_CAP_FOR_THREE_SMOKES"
    assert r["observed_key_limits"] == {"limit": None, "limit_remaining": None}
    assert "verified_key_limit_usd" not in r and "verified_key_remaining_usd" not in r


@pytest.mark.parametrize("budget", ["1.00001", "2", "0", "-1", "NaN"])
def test_owner_optin_budget_cannot_exceed_one_dollar(tmp_path, monkeypatch, budget):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")
    t = FakeTransport(limit=None)
    r = d.run(job(), metadata(), tmp_path / "optin", ledger=tmp_path / "ledger.json", budget=budget, transport=t, owner_local_cap_only=True)
    assert r["verification"] == "OWNER_LOCAL_CAP_REJECTED" and t.posts == 0
    assert not (tmp_path / "ledger.json").exists()


def test_owner_optin_rejects_non_synthetic_and_uses_remaining_local_budget(tmp_path):
    j = job()
    j["synthetic"] = False
    with pytest.raises(d.DelegateError, match="FORBIDDEN_INPUT"):
        d.run(j, metadata(), tmp_path / "never", budget="1", owner_local_cap_only=True)
    ledger = tmp_path / "remaining.json"
    token = d.reserve(ledger, "1", "key", None, ".6", owner_local_cap_only=True)
    d.settle(ledger, token, ".6")
    with pytest.raises(d.DelegateError, match="BUDGET_EXCEEDED"):
        d.reserve(ledger, "1", "key", None, ".41", owner_local_cap_only=True)


def test_owner_optin_persistent_three_reservation_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")
    t = FakeTransport(limit=None)
    ledger = tmp_path / "ledger.json"
    for i in range(4):
        r = d.run(job(), metadata(), tmp_path / str(i), ledger=ledger, budget="1", transport=t, owner_local_cap_only=True)
        assert r["verification"] == ("PASS" if i < 3 else "OWNER_SMOKE_LIMIT_REACHED")
    data = json.loads(ledger.read_text())
    assert t.posts == 3 and data["reservation_count"] == 3 and not data["reservations"]
    with pytest.raises(d.DelegateError, match="LEDGER_CONTRACT_MISMATCH"):
        d.reserve(ledger, "1", data["key_id"], "1", ".01")


def owner_race_reserve(script, ledger, barrier, queue):
    spec = importlib.util.spec_from_file_location("owner_race_delegate", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    barrier.wait(timeout=10)
    try:
        token = module.reserve(ledger, "1", "same-key", None, ".01", owner_local_cap_only=True)
        module.settle(ledger, token, ".001")
        queue.put("RESERVED")
    except module.DelegateError as exc:
        queue.put(exc.code)


def test_owner_optin_third_reservation_multiprocessing_race(tmp_path):
    ledger = tmp_path / "owner-race.json"
    for _ in range(2):
        token = d.reserve(ledger, "1", "same-key", None, ".01", owner_local_cap_only=True)
        d.settle(ledger, token, ".001")
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    workers = [ctx.Process(target=owner_race_reserve, args=(str(SCRIPT), str(ledger), barrier, queue)) for _ in range(2)]
    for proc in workers:
        proc.start()
    results = [queue.get(timeout=20) for _ in workers]
    for proc in workers:
        proc.join(timeout=20)
        assert proc.exitcode == 0
    assert results.count("RESERVED") == 1
    assert set(results) <= {"RESERVED", "OWNER_SMOKE_LIMIT_REACHED", "PENDING_EXECUTION_COST"}
    assert json.loads(ledger.read_text())["reservation_count"] == 3


def test_owner_optin_unknown_blocks_further_reservations(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")
    t = FakeTransport(limit=None, timeout=True)
    ledger = tmp_path / "ledger.json"
    for i in range(2):
        r = d.run(job(), metadata(), tmp_path / str(i), ledger=ledger, budget="1", transport=t, owner_local_cap_only=True)
        assert r["verification"] == "UNKNOWN_EXECUTION_COST"
    assert t.posts == 1
    data = json.loads(ledger.read_text())
    assert data["blocked"] and data["reservation_count"] == 1 and len(data["reservations"]) == 1


def test_cli_owner_optin_switch_is_explicit(monkeypatch, tmp_path, capsys):
    j, m = tmp_path / "job.json", tmp_path / "metadata.json"
    j.write_text(json.dumps(job()))
    m.write_text(json.dumps(metadata()))
    seen = []

    def capture(*args, **kwargs):
        seen.append(kwargs["owner_local_cap_only"])
        return {"verification": "PASS", "cost_usd": "0"}

    monkeypatch.setattr(d, "run", capture)
    argv = ["call", "--job", str(j), "--metadata", str(m), "--run-dir", str(tmp_path / "unused"), "--budget-usd", "1", "--ledger", str(tmp_path / "ledger.json")]
    assert d.main(argv) == 0
    assert d.main(argv + ["--owner-local-cap-only"]) == 0
    assert seen == [False, True]


def test_public_metadata_artifact_is_sanitized_including_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")

    class InjectedMetadata(FakeTransport):
        def request(self, method, path, body=None):
            obj = super().request(method, path, body)
            if path.startswith("models/"):
                obj["data"]["audit_note"] = "sk-or-synthetic-test-only"
                obj["data"]["sk-or-synthetic-test-only"] = "sensitive key-name"
                obj["data"]["reasoning_details"] = [{"text": "OPAQUE_REASONING_MUST_NOT_PERSIST"}]
            return obj

    r = d.run(job(), metadata(), tmp_path / "run", ledger=tmp_path / "ledger.json", budget="1", transport=InjectedMetadata(limit=None), owner_local_cap_only=True)
    assert r["verification"] == "PASS"
    raw = (tmp_path / "run/endpoint_metadata.json").read_text(encoding="utf8")
    assert "sk-or-synthetic-test-only" not in raw and "OPAQUE_REASONING_MUST_NOT_PERSIST" not in raw
    assert d.digest(json.loads(raw)) == r["endpoint_evidence"]["snapshot_sha256"]
