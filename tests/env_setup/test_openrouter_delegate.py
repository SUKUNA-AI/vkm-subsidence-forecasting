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
        quantization = tag.rsplit("/", 1)[-1] if tag.endswith(("/fp8", "/mxfp4")) else "unknown"
        rows.append({"model": model, "checked_at": d.now(), "http_status": 200, "metadata": {"data": {"architecture": {"input_modalities": ["text", "image"]}, "endpoints": [{"model_id": model, "provider_name": provider, "tag": tag, "service_tier": "flex" if tag.endswith("flex") else None, "quantization": quantization, "status": 0, "context_length": 1000000, "max_completion_tokens": 10000, "supported_parameters": ["reasoning", "max_tokens", "response_format", "structured_outputs"], "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}}]}}})
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
    d.prepare(job("vision_documents"), m)


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
    reply = response("vision_documents")
    del reply["provider_endpoint"]
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.validate_response(reply, {}, job("vision_documents"))
    assert d.validate_response(reply, {"service_tier": "flex"}, job("vision_documents"))


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
    assert "X-OpenRouter-Metadata" not in seen[0][2]
    assert seen[1][2]["X-OpenRouter-Metadata"] == "enabled"


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


@pytest.mark.parametrize("profile,field,conflict", [("vision_documents", "provider_endpoint", "google-ai-studio/standard"), ("vision_documents", "service_tier", "standard"), ("cheap_code", "quantization", "fp16")])
def test_conflicting_generation_observation_cannot_be_masked(profile, field, conflict):
    reply = response(profile)
    reply["service_tier"] = "flex" if profile == "vision_documents" else None
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


@pytest.mark.parametrize("profile", ["critical_review", "google/gemini-4-carbon", "google/gemini-4-carbon-20261110"])
@pytest.mark.parametrize("mode", ["check", "mock", "call", "discover"])
def test_carbon_is_blocked_in_every_cli_mode_before_transport(tmp_path, monkeypatch, capsys, profile, mode):
    class NoNetwork:
        def request(self, *args, **kwargs):
            raise AssertionError("blocked model must never reach transport")

    monkeypatch.setattr(d, "Transport", lambda *args, **kwargs: NoNetwork())
    j = job()
    j["profile"] = profile
    path, meta = tmp_path / "job.json", tmp_path / "metadata.json"
    path.write_text(json.dumps(j))
    meta.write_text(json.dumps(metadata()))
    if mode == "discover":
        argv = [mode, "--profile", profile, "--output", str(tmp_path / "discovered.json")]
    else:
        argv = [mode, "--job", str(path), "--metadata", str(meta)]
        if mode != "check":
            argv += ["--run-dir", str(tmp_path / "blocked-run")]
    assert d.main(argv) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED_403"
    assert not (tmp_path / "discovered.json").exists()
    assert not (tmp_path / "blocked-run").exists()
    assert "critical_review" not in d.PROFILES


@pytest.mark.parametrize("profiles", [None, [], ["cheap_code", "vision_documents"], ["cheap_code", "cheap_code"]])
def test_discover_never_fans_out_without_a_single_selected_profile(profiles):
    class NoNetwork:
        def request(self, *args, **kwargs):
            raise AssertionError("invalid selection must not reach transport")

    with pytest.raises(d.DelegateError, match="SINGLE_PROFILE_REQUIRED"):
        d.discover(NoNetwork(), profiles)


@pytest.mark.parametrize("profile", list(d.PROFILES))
def test_discover_calls_only_selected_model(profile):
    class RecordingTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.paths = []

        def request(self, method, path, body=None):
            self.paths.append((method, path))
            return super().request(method, path, body)

    t = RecordingTransport()
    rows = d.discover(t, profile)
    assert t.paths == [("GET", "models/" + d.PROFILES[profile][0] + "/endpoints")]
    assert len(rows) == 1 and rows[0]["model"] == d.PROFILES[profile][0]


def test_active_profiles_match_owner_selection_without_carbon_fallback():
    assert d.PROFILES == {
        "cheap_code": ("deepseek/deepseek-v4.1-flash-1017", "stepfun/fp8", "StepFun", "0.10"),
        "vision_documents": ("google/gemini-4-hydrogen", "google-ai-studio/flex", "Google AI Studio", "0.30"),
        "engineering_research": ("moonshotai/kimi-k3.1-pro", "decart/mxfp4", "Decart", "1.00"),
    }


@pytest.mark.parametrize("quantization", [None, "unknown", "fp8", "fp4"])
def test_kimi_requires_exact_mxfp4_metadata(quantization):
    m = metadata()
    row = next(row for row in m if row["model"] == "moonshotai/kimi-k3.1-pro")
    row["metadata"]["data"]["endpoints"][0]["quantization"] = quantization
    with pytest.raises(d.DelegateError, match="ENDPOINT_UNAVAILABLE"):
        d.prepare(job("engineering_research"), m)


@pytest.mark.parametrize("parameter", ["reasoning", "max_tokens", "response_format", "structured_outputs"])
def test_kimi_mandatory_parameters_must_be_supported(parameter):
    m = metadata()
    row = next(row for row in m if row["model"] == "moonshotai/kimi-k3.1-pro")
    row["metadata"]["data"]["endpoints"][0]["supported_parameters"].remove(parameter)
    with pytest.raises(d.DelegateError, match="UNSUPPORTED_PARAMETERS"):
        d.prepare(job("engineering_research"), m)


def test_kimi_generic_provider_cannot_prove_mxfp4():
    reply = response("engineering_research")
    del reply["provider_endpoint"]
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.validate_response(reply, {}, job("engineering_research"))
    assert d.validate_response(reply, {"quantization": "mxfp4"}, job("engineering_research"))


@pytest.mark.parametrize("response_change,generation", [
    ({}, {"quantization": "fp8"}),
    ({"quantization": "mxfp4"}, {"provider_endpoint": "decart/fp4"}),
    ({"quantization": "fp4"}, {}),
    ({"provider_endpoint": "decart"}, {}),
    ({"model": "moonshotai/kimi-latest"}, {}),
])
def test_kimi_actual_endpoint_and_quantization_conflicts_fail(response_change, generation):
    reply = response("engineering_research")
    reply.update(response_change)
    with pytest.raises(d.DelegateError, match="ROUTING_MISMATCH"):
        d.validate_response(reply, generation, job("engineering_research"))


def test_kimi_call_fake_transport_and_mock_remain_single_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")
    t = FakeTransport(reply=response("engineering_research"))
    receipt = d.run(job("engineering_research"), metadata(), tmp_path / "fake-call", ledger=tmp_path / "ledger.json", budget=".01", transport=t)
    assert receipt["verification"] == "PASS" and t.posts == 1
    assert receipt["requested_model"] == "moonshotai/kimi-k3.1-pro"
    assert receipt["requested_provider"]["only"] == ["decart/mxfp4"]
    mock = d.run(job("engineering_research"), metadata(), tmp_path / "mock", transport=t, mock=True)
    assert mock["verification"] == "MOCK_PASS" and t.posts == 1


def test_cli_discover_reports_selected_endpoint_failure_nonzero(tmp_path, monkeypatch, capsys):
    class Unavailable:
        def request(self, *args, **kwargs):
            d.fail("HTTP_403")

    monkeypatch.setattr(d, "Transport", lambda: Unavailable())
    out = tmp_path / "discovery.json"
    assert d.main(["discover", "--profile", "engineering_research", "--output", str(out)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "ENDPOINT_UNAVAILABLE"
    rows = json.loads(out.read_text())
    assert len(rows) == 1 and rows[0]["model"] == "moonshotai/kimi-k3.1-pro"
    assert rows[0]["error"] == "HTTP_403"


@pytest.mark.parametrize("profile", list(d.PROFILES))
def test_cli_discover_success_names_only_selected_profile(tmp_path, monkeypatch, capsys, profile):
    monkeypatch.setattr(d, "Transport", FakeTransport)
    out = tmp_path / "discovery.json"
    assert d.main(["discover", "--profile", profile, "--output", str(out)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {"status": "DISCOVERED", "profile": profile, "model": d.PROFILES[profile][0]}
    assert len(json.loads(out.read_text())) == 1


def kimi_router_response():
    reply = response("engineering_research")
    del reply["provider_endpoint"]
    reply["openrouter_metadata"] = {"requested": "moonshotai/kimi-k3.1-pro", "strategy": "direct", "attempt": 1, "is_byok": False, "endpoints": {"total": 24, "available": [{"model": "moonshotai/kimi-k3.1-pro-20261023", "provider": "Decart", "selected": True}]}}
    return reply


def kimi_evidence():
    return d.prepare(job("engineering_research"), metadata())[2]


@pytest.mark.parametrize("profile", list(d.PROFILES))
def test_only_pinned_alias_canonical_pairs_are_valid(profile):
    reply = response(profile)
    canonical = d.CANONICAL_MODEL_PINS[d.PROFILES[profile][0]]
    reply["model"] = canonical
    assert d.validate_response(reply, {"model": canonical}, job(profile))
    reply["model"] = canonical + "-latest"
    with pytest.raises(d.DelegateError, match="ROUTING_MISMATCH"):
        d.validate_response(reply, {}, job(profile))


def test_canonical_mapping_is_immutable_and_carbon_canonical_blocked():
    with pytest.raises(TypeError):
        d.CANONICAL_MODEL_PINS["moonshotai/kimi-k3.1-pro"] = "moonshotai/other"
    reply = response("engineering_research")
    with pytest.raises(d.DelegateError, match="BLOCKED_403"):
        d.validate_response(reply, {"model": "google/gemini-4-carbon-20261110"}, job("engineering_research"))


def test_unique_catalog_and_router_selection_can_confirm_kimi_without_quant_field():
    reply, ev = kimi_router_response(), kimi_evidence()
    proof = {}
    assert d.validate_response(reply, {"model": "moonshotai/kimi-k3.1-pro-20261023"}, job("engineering_research"), endpoint_evidence=ev, verification_evidence=proof)
    assert proof["basis"] == "UNIQUE_PINNED_ENDPOINT_AND_ROUTER_SELECTION"
    assert proof["physical_precision"] == "NOT_MEASURED"
    assert proof["catalog_provider_endpoint_count"] == 1
    assert "provider_endpoint" not in reply and "quantization" not in reply
    assert len(ev["provider_endpoints"]) == 1
    assert set(ev["provider_endpoints"][0]) == {"model_id", "provider_name", "tag", "quantization", "service_tier", "status"}


@pytest.mark.parametrize("field,value", [("selected", False), ("selected", 1), ("provider", None), ("model", None)])
def test_inference_requires_complete_single_router_selection(field, value):
    reply = kimi_router_response()
    reply["openrouter_metadata"]["endpoints"]["available"][0][field] = value
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.validate_response(reply, {}, job("engineering_research"), endpoint_evidence=kimi_evidence())


@pytest.mark.parametrize("field,value", [("strategy", "fallback"), ("attempt", 2), ("attempt", True), ("is_byok", True), ("is_byok", 0)])
def test_inference_rejects_retries_fallback_and_missing_no_byok_proof(field, value):
    reply = kimi_router_response()
    reply["openrouter_metadata"][field] = value
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.validate_response(reply, {}, job("engineering_research"), endpoint_evidence=kimi_evidence())


@pytest.mark.parametrize("field", ["openrouter_metadata", "requested", "endpoints"])
def test_inference_requires_routing_metadata_fields(field):
    reply = kimi_router_response()
    if field == "openrouter_metadata":
        del reply[field]
    else:
        del reply["openrouter_metadata"][field]
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.validate_response(reply, {}, job("engineering_research"), endpoint_evidence=kimi_evidence())


def test_inference_rejects_ambiguous_catalog_or_router_candidates():
    reply = kimi_router_response()
    m = metadata()
    endpoints = next(r for r in m if r["model"] == "moonshotai/kimi-k3.1-pro")["metadata"]["data"]["endpoints"]
    other = copy.deepcopy(endpoints[0]); other["tag"] = "decart/fp8"; other["quantization"] = "fp8"
    endpoints.append(other)
    evidence = d.prepare(job("engineering_research"), m)[2]
    assert len(evidence["provider_endpoints"]) == 2
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.validate_response(reply, {}, job("engineering_research"), endpoint_evidence=evidence)
    reply["openrouter_metadata"]["endpoints"]["available"].append({"provider": "Other", "model": "other/model", "selected": False})
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.validate_response(reply, {}, job("engineering_research"), endpoint_evidence=kimi_evidence())


@pytest.mark.parametrize("variant", ["absent", "missing_catalog", "stale", "future", "wrong_model", "mismatched_endpoint"])
def test_inference_needs_fresh_complete_matching_endpoint_evidence(variant):
    evidence = kimi_evidence()
    if variant == "absent":
        evidence = None
    elif variant == "missing_catalog":
        del evidence["provider_endpoints"]
    elif variant == "stale":
        evidence["checked_at"] = "2020-01-01T00:00:00+00:00"
    elif variant == "future":
        evidence["checked_at"] = "2099-01-01T00:00:00+00:00"
    elif variant == "wrong_model":
        evidence["catalog_model"] = "moonshotai/other-model"
    else:
        evidence["provider_endpoints"][0]["quantization"] = "fp8"
    with pytest.raises(d.DelegateError, match="PROVENANCE_UNCONFIRMED"):
        d.validate_response(kimi_router_response(), {}, job("engineering_research"), endpoint_evidence=evidence)


@pytest.mark.parametrize("field,value", [("provider", "Other"), ("model", "moonshotai/kimi-k3.1-pro-20261024"), ("provider_endpoint", "decart/fp8"), ("quantization", "fp8"), ("service_tier", "flex")])
def test_inference_never_hides_explicit_selected_route_conflicts(field, value):
    reply = kimi_router_response()
    reply["openrouter_metadata"]["endpoints"]["available"][0][field] = value
    with pytest.raises(d.DelegateError, match="ROUTING_MISMATCH"):
        d.validate_response(reply, {}, job("engineering_research"), endpoint_evidence=kimi_evidence())


@pytest.mark.parametrize("generation", [{"model": "moonshotai/kimi-k3.1-pro-20261024"}, {"provider_endpoint": "decart/fp8"}, {"quantization": "fp8"}, {"service_tier": "priority"}, {"is_byok": True}, {"provider_responses": [{"provider_name": "Decart", "model_permaslug": "moonshotai/other"}]}])
def test_generation_conflicts_override_valid_inference(generation):
    with pytest.raises(d.DelegateError, match="ROUTING_MISMATCH"):
        d.validate_response(kimi_router_response(), generation, job("engineering_research"), endpoint_evidence=kimi_evidence())


def test_run_records_inferred_route_basis_and_preserves_observed_null_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")

    class CanonicalGeneration(FakeTransport):
        def request(self, method, path, body=None):
            obj = super().request(method, path, body)
            if path.startswith("generation?"):
                obj["data"]["model"] = "moonshotai/kimi-k3.1-pro-20261023"
            return obj

    t = CanonicalGeneration(reply=kimi_router_response())
    receipt = d.run(job("engineering_research"), metadata(), tmp_path / "run", ledger=tmp_path / "ledger.json", budget=".01", transport=t)
    assert receipt["verification"] == "PASS" and t.posts == 1
    assert receipt["route_verification"]["basis"] == "UNIQUE_PINNED_ENDPOINT_AND_ROUTER_SELECTION"
    assert receipt["observed_endpoint"] == {"provider_endpoint": None, "quantization": None, "service_tier": None}
    assert receipt["model_identity"]["requested_alias"] == "moonshotai/kimi-k3.1-pro"
    assert receipt["actual_generation_model"] == "moonshotai/kimi-k3.1-pro-20261023"
    assert receipt["model_identity"]["generation_observed"] == receipt["actual_generation_model"]


def source_visual_job():
    """Generated test pixels imitate a caller package; no real source is read."""
    j = job("vision_documents")
    j["task_id"] = "geo-provenance-fixture"
    j["synthetic"] = False
    j["input_package"] = {"work_id": "VKM-SRC-011", "source_id": "VKM-SRC-011", "page": {"number": 1, "scheme": "PDF_1_BASED"}, "figure_id": "1.25", "panel_id": "main", "frame": {"id": "source011_fig1_25_pixel_fixture", "units": "pixel", "crs": "UNKNOWN"}, "image_sha256": __import__("hashlib").sha256(base64.b64decode(j["images"][0]["data_base64"])).hexdigest(), "authorization_scope": "VKM-GEO-01"}
    return j


@pytest.mark.parametrize("units,scheme", [("pixel", "PDF_1_BASED"), ("px", "PRINTED")])
def test_explicit_real_visual_package_is_bounded_and_frame_preserved(units, scheme):
    j = source_visual_job()
    j["input_package"]["frame"]["units"] = units
    j["input_package"]["page"]["scheme"] = scheme
    original = copy.deepcopy(j)
    request, estimate, _ = d.prepare(j, metadata())
    assert request["model"] == "google/gemini-4-hydrogen" and estimate > 0
    assert request["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "input_package" not in request and j == original


@pytest.mark.parametrize("field", ["work_id", "source_id", "page", "figure_id", "panel_id", "frame", "image_sha256", "authorization_scope"])
def test_real_visual_package_requires_every_provenance_field(field):
    j = source_visual_job()
    del j["input_package"][field]
    with pytest.raises(d.DelegateError, match="INVALID_PROVENANCE"):
        d.prepare(j, metadata())


@pytest.mark.parametrize("field,value", [("authorization_scope", "OTHER-TASK"), ("source_id", "SRC-011"), ("source_id", "VKM-SRC-012"), ("figure_id", "1.24"), ("figure_id", "1.32")])
def test_real_visual_package_rejects_unauthorized_scope_source_or_figure(field, value):
    j = source_visual_job()
    j["input_package"][field] = value
    with pytest.raises(d.DelegateError, match="UNAUTHORIZED_SOURCE_PACKAGE"):
        d.prepare(j, metadata())


@pytest.mark.parametrize("mutation", [
    {"page": {"number": 0, "scheme": "PDF_1_BASED"}},
    {"page": {"number": True, "scheme": "PDF_1_BASED"}},
    {"page": {"number": 1, "scheme": "UNKNOWN"}},
    {"page": {"number": 1, "scheme": []}},
    {"work_id": ""}, {"panel_id": "../../private"},
    {"figure_id": []}, {"image_sha256": "not-a-hash"},
    {"frame": {"id": "frame", "units": "m", "crs": "UNKNOWN"}},
    {"frame": {"id": "frame", "units": "pixel", "crs": "EPSG:4326"}},
    {"frame": {"id": "frame", "units": "pixel", "crs": None}},
])
def test_real_visual_package_rejects_malformed_locators_frame_or_hash(mutation):
    j = source_visual_job()
    j["input_package"].update(mutation)
    with pytest.raises(d.DelegateError, match="INVALID_PROVENANCE"):
        d.prepare(j, metadata())


def test_real_visual_package_hash_matches_decoded_file_bytes_not_base64_text():
    j = source_visual_job()
    j["input_package"]["image_sha256"] = __import__("hashlib").sha256(j["images"][0]["data_base64"].encode()).hexdigest()
    with pytest.raises(d.DelegateError, match="IMAGE_PROVENANCE_MISMATCH"):
        d.prepare(j, metadata())


def test_real_visual_package_cannot_mislabel_input_as_synthetic_or_use_other_profile():
    j = source_visual_job()
    j["synthetic"] = True
    with pytest.raises(d.DelegateError, match="FORBIDDEN_INPUT"):
        d.prepare(j, metadata())
    j["synthetic"] = False
    j["profile"] = "engineering_research"
    with pytest.raises(d.DelegateError, match="FORBIDDEN_INPUT"):
        d.prepare(j, metadata())


@pytest.mark.parametrize("count", [0, 2])
def test_one_real_source_visual_package_binds_exactly_one_image(count):
    j = source_visual_job()
    j["images"] = j["images"] * count
    with pytest.raises(d.DelegateError, match="INVALID_PROVENANCE"):
        d.prepare(j, metadata())


@pytest.mark.parametrize("field,value", [("source_path", "../../PRIVATE/source.pdf"), ("image_url", "https://example.invalid/source.png")])
def test_source_visual_package_never_opens_source_paths_or_urls(field, value):
    j = source_visual_job()
    j["input_package"][field] = value
    with pytest.raises(d.DelegateError, match="INVALID_PROVENANCE"):
        d.prepare(j, metadata())


def test_real_visual_package_receipt_has_provenance_without_inline_image(tmp_path, monkeypatch):
    j = source_visual_job()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")
    t = FakeTransport(reply=response("vision_documents"))
    receipt = d.run(j, metadata(), tmp_path / "run", ledger=tmp_path / "ledger.json", budget=".01", transport=t)
    assert receipt["verification"] == "PASS" and t.posts == 1
    assert receipt["synthetic"] is False and receipt["input_kind"] == "AUTHORIZED_SOURCE_VISUAL"
    assert receipt["input_provenance"] == j["input_package"]
    assert receipt["input_provenance"]["frame"]["units"] == "pixel"
    assert receipt["input_authorization_basis"] == "EXPLICIT_CALLER_ASSERTION"
    persisted = (tmp_path / "run/receipt.json").read_text(encoding="utf8")
    assert j["images"][0]["data_base64"] not in persisted and "data_base64" not in persisted
    assert receipt["input_sha256"] == d.digest(j)


def test_real_visual_package_does_not_expand_synthetic_local_cap_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic-test-only")
    t = FakeTransport(reply=response("vision_documents"), limit=None)
    receipt = d.run(source_visual_job(), metadata(), tmp_path / "run", ledger=tmp_path / "ledger.json", budget="1", transport=t, owner_local_cap_only=True)
    assert receipt["verification"] == "OWNER_LOCAL_CAP_REJECTED" and t.posts == 0
    assert not (tmp_path / "ledger.json").exists()


def test_invalid_source_package_is_rejected_before_any_transport_or_attempt(tmp_path, monkeypatch):
    class NoNetwork:
        def request(self, *args, **kwargs):
            raise AssertionError("invalid package must not call transport")

    j = source_visual_job()
    j["input_package"]["authorization_scope"] = "NOT_AUTHORIZED"
    with pytest.raises(d.DelegateError, match="UNAUTHORIZED_SOURCE_PACKAGE"):
        d.run(j, metadata(), tmp_path / "run", transport=NoNetwork())
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("figure", ["1.25", "1.42", "1.52", "1.63"])
def test_ma_scope_allows_pinned_work_source_and_authorized_figure_range(figure):
    j = source_visual_job()
    j["input_package"].update(authorization_scope="VKM-GEO-01-MA", work_id="VKM-WRK-011", figure_id=figure)
    d.prepare(j, metadata())


@pytest.mark.parametrize("figure", ["1.24", "1.64", "1.025", "1.25-extra", "2.25"])
def test_ma_scope_rejects_figures_outside_exact_range(figure):
    j = source_visual_job()
    j["input_package"].update(authorization_scope="VKM-GEO-01-MA", work_id="VKM-WRK-011", figure_id=figure)
    with pytest.raises(d.DelegateError, match="UNAUTHORIZED_SOURCE_PACKAGE"):
        d.prepare(j, metadata())


def test_ma_scope_requires_actual_work_source_link():
    j = source_visual_job()
    j["input_package"]["authorization_scope"] = "VKM-GEO-01-MA"
    with pytest.raises(d.DelegateError, match="UNAUTHORIZED_SOURCE_PACKAGE"):
        d.prepare(j, metadata())


def test_authorized_jpeg_hash_binds_exact_decoded_container_bytes():
    from PIL import Image
    j = source_visual_job()
    buf = io.BytesIO()
    Image.new("RGB", (32, 16), "white").save(buf, format="JPEG")
    j["images"][0].update(mime="image/jpeg", data_base64=base64.b64encode(buf.getvalue()).decode())
    j["input_package"]["image_sha256"] = __import__("hashlib").sha256(buf.getvalue()).hexdigest()
    assert d.prepare(j, metadata())[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
