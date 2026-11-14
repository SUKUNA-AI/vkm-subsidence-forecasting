#!/usr/bin/env python3
"""Bounded one-shot OpenRouter pilot. No shell/tools, retries, or credential files."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
import uuid
from types import MappingProxyType

PROFILES = {
    "cheap_code": ("deepseek/deepseek-v4.1-flash-1017", "stepfun/fp8", "StepFun", "0.10"),
    "vision_documents": ("google/gemini-4-hydrogen", "google-ai-studio/flex", "Google AI Studio", "0.30"),
    "engineering_research": ("moonshotai/kimi-k3.1-pro", "decart/mxfp4", "Decart", "1.00"),
}
BLOCKED_PROFILES = {"critical_review": {"model": "google/gemini-4-carbon", "status": "BLOCKED_403"}}
BLOCKED_MODEL_IDS = frozenset({"google/gemini-4-carbon", "google/gemini-4-carbon-20261110"})
CANONICAL_MODEL_PINS = MappingProxyType({
    "google/gemini-4-hydrogen": "google/gemini-4-hydrogen-20261112",
    "deepseek/deepseek-v4.1-flash-1017": "deepseek/deepseek-v4.1-flash-20261017",
    "moonshotai/kimi-k3.1-pro": "moonshotai/kimi-k3.1-pro-20261023",
})
DEFAULT_SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"], "additionalProperties": False}
MAX_RESPONSE = 2 * 1024 * 1024


class DelegateError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # never carry upstream bodies, prompts, or credentials


def fail(code):
    raise DelegateError(code)


def profile_spec(profile):
    if not isinstance(profile, str):
        fail("UNKNOWN_PROFILE")
    if profile in BLOCKED_PROFILES or profile in BLOCKED_MODEL_IDS:
        fail("BLOCKED_403")
    if profile not in PROFILES:
        fail("UNKNOWN_PROFILE")
    return PROFILES[profile]


def allowed_model_ids(alias):
    return frozenset({alias, CANONICAL_MODEL_PINS[alias]})


def check_observed_model(value, alias):
    if isinstance(value, str) and value in BLOCKED_MODEL_IDS:
        fail("BLOCKED_403")
    if not isinstance(value, str) or value not in allowed_model_ids(alias):
        fail("ROUTING_MISMATCH")


def endpoint_identity(endpoint):
    return {k: endpoint.get(k) for k in ("model_id", "provider_name", "tag", "quantization", "service_tier", "status")}


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(obj):
    return hashlib.sha256(encoded(obj)).hexdigest()


def money(value, code="INVALID_COST"):
    if isinstance(value, bool) or value is None:
        fail(code)
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        fail(code)
    if not d.is_finite() or d < 0:
        fail(code)
    return d


def load_json(path, limit=4 * 1024 * 1024):
    if Path(path).stat().st_size > limit:
        fail("INPUT_LIMIT")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=lambda _: fail("INVALID_JSON"))
    except (ValueError, UnicodeError):
        fail("INVALID_JSON")


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("xb") as f:
        f.write(encoded(obj))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def schema_check(schema, depth=0):
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum", "minItems", "maxItems", "minLength", "maxLength", "description"}
    if depth > 8 or not isinstance(schema, dict) or set(schema) - allowed:
        fail("INVALID_SCHEMA")
    if schema.get("type") not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        fail("INVALID_SCHEMA")
    if schema["type"] == "object":
        if not isinstance(schema.get("properties"), dict) or schema.get("additionalProperties") is not False:
            fail("INVALID_SCHEMA")
        if set(schema.get("required", [])) != set(schema["properties"]):
            fail("INVALID_SCHEMA")
        for child in schema["properties"].values():
            schema_check(child, depth + 1)
    if schema["type"] == "array":
        schema_check(schema.get("items"), depth + 1)
    try:
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
    except ImportError:
        fail("DEPENDENCY_JSONSCHEMA")
    except Exception:
        fail("INVALID_SCHEMA")


def validate_source_package(package, image_sha256):
    """Validate the caller's explicit scope/locator assertion; never read sources."""
    fields = {"work_id", "source_id", "page", "figure_id", "panel_id", "frame", "image_sha256", "authorization_scope"}
    if not isinstance(package, dict) or set(package) != fields:
        fail("INVALID_PROVENANCE")
    scope = package["authorization_scope"]
    if scope not in ("VKM-GEO-01", "VKM-GEO-01-MA") or package["source_id"] != "VKM-SRC-011":
        fail("UNAUTHORIZED_SOURCE_PACKAGE")
    for value in (package["work_id"], package["panel_id"]):
        if not isinstance(value, str) or not re.fullmatch(r"\w[\w.:-]{0,127}", value):
            fail("INVALID_PROVENANCE")
    if not isinstance(package["figure_id"], str):
        fail("INVALID_PROVENANCE")
    if not re.fullmatch(r"1\.[0-9]{2}", package["figure_id"]) or not 25 <= int(package["figure_id"].split(".")[1]) <= (63 if scope == "VKM-GEO-01-MA" else 31):
        fail("UNAUTHORIZED_SOURCE_PACKAGE")
    if scope == "VKM-GEO-01-MA" and package["work_id"] != "VKM-WRK-011":
        fail("UNAUTHORIZED_SOURCE_PACKAGE")
    page = package["page"]
    if not isinstance(page, dict) or set(page) != {"number", "scheme"} or type(page["number"]) is not int or not 1 <= page["number"] <= 10000 or not isinstance(page["scheme"], str) or page["scheme"] not in {"PDF_1_BASED", "PRINTED"}:
        fail("INVALID_PROVENANCE")
    frame = package["frame"]
    if not isinstance(frame, dict) or set(frame) != {"id", "units", "crs"}:
        fail("INVALID_PROVENANCE")
    if not isinstance(frame["id"], str) or not re.fullmatch(r"\w[\w.:-]{0,127}", frame["id"]) or not isinstance(frame["units"], str) or frame["units"] not in {"px", "pixel"} or frame["crs"] != "UNKNOWN":
        fail("INVALID_PROVENANCE")
    if not isinstance(package["image_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", package["image_sha256"]):
        fail("INVALID_PROVENANCE")
    if package["image_sha256"] != image_sha256:
        fail("IMAGE_PROVENANCE_MISMATCH")


def validate_job(job, secret=None):
    allowed = {"task_id", "synthetic", "profile", "prompt", "max_tokens", "schema", "images", "input_package"}
    if not isinstance(job, dict) or set(job) - allowed or type(job.get("synthetic")) is not bool:
        fail("FORBIDDEN_INPUT")
    profile_spec(job.get("profile"))
    if job["synthetic"] is True and "input_package" in job:
        fail("FORBIDDEN_INPUT")
    if job["synthetic"] is False and (job["profile"] != "vision_documents" or "input_package" not in job):
        fail("FORBIDDEN_INPUT")
    if not isinstance(job.get("task_id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job["task_id"]):
        fail("FORBIDDEN_INPUT")
    prompt = job.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt.encode("utf-8")) <= 16000:
        fail("INPUT_LIMIT")
    text = encoded(job).decode("utf-8")
    if (secret and secret in text) or re.search(r"sk-or-[A-Za-z0-9_-]+|(?:api[_ -]?key|password|authorization)\s*[:=]\s*\S+", text, re.I):
        fail("FORBIDDEN_INPUT")
    if type(job.get("max_tokens")) is not int or not 64 <= job["max_tokens"] <= 4096:
        fail("INPUT_LIMIT")
    schema = job.get("schema", DEFAULT_SCHEMA)
    if len(encoded(schema)) > 8192:
        fail("INPUT_LIMIT")
    schema_check(schema)
    images = job.get("images", [])
    if not isinstance(images, list) or len(images) > 2:
        fail("INPUT_LIMIT")
    if job["synthetic"] is False and len(images) != 1:
        fail("INVALID_PROVENANCE")
    pixels = 0
    image_hashes = []
    for img in images:
        if not isinstance(img, dict) or set(img) != {"mime", "data_base64", "width", "height", "bbox"}:
            fail("INVALID_IMAGE")
        if img["mime"] not in {"image/png", "image/jpeg"}:
            fail("INVALID_IMAGE")
        w, h = img["width"], img["height"]
        if type(w) is not int or type(h) is not int or not (1 <= w <= 2048 and 1 <= h <= 2048):
            fail("INVALID_IMAGE")
        bbox = img["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4 or any(type(x) is not int for x in bbox) or not (0 <= bbox[0] < bbox[2] <= w and 0 <= bbox[1] < bbox[3] <= h):
            fail("INVALID_IMAGE")
        try:
            raw = base64.b64decode(img["data_base64"], validate=True)
            if not 1 <= len(raw) <= 1024 * 1024:
                fail("INVALID_IMAGE")
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as im:
                if im.format != {"image/png": "PNG", "image/jpeg": "JPEG"}[img["mime"]] or im.size != (w, h) or getattr(im, "n_frames", 1) != 1:
                    fail("INVALID_IMAGE")
                im.verify()
            with Image.open(io.BytesIO(raw)) as im:
                im.load()  # verify CRC/header alone does not validate decoded pixels
        except DelegateError:
            raise
        except Exception:
            fail("INVALID_IMAGE")
        image_hashes.append(hashlib.sha256(raw).hexdigest())
        pixels += w * h
    if pixels > 4_000_000:
        fail("INPUT_LIMIT")
    if job["profile"] == "vision_documents" and not images:
        fail("INVALID_IMAGE")
    if job["synthetic"] is False:
        validate_source_package(job["input_package"], image_hashes[0])
    return schema, pixels


def select_endpoint(metadata, profile, fresh=True):
    model, tag, provider, _ = profile_spec(profile)
    try:
        row = next(x for x in metadata if x["model"] == model and x["http_status"] == 200)
        data = row["metadata"]["data"]
        ep = next(e for e in data["endpoints"] if e.get("tag") == tag and e.get("model_id") in allowed_model_ids(model) and e.get("status") == 0)
        if data.get("id", model) not in allowed_model_ids(model) or ep.get("provider_name") != provider:
            fail("ENDPOINT_UNAVAILABLE")
        stamp = datetime.fromisoformat(row["checked_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if fresh and not -60 <= age <= 86400:
            fail("STALE_METADATA")
        if tag.endswith("/flex") and ep.get("service_tier") != "flex":
            fail("ENDPOINT_UNAVAILABLE")
        if tag.endswith("/fp8") and ep.get("quantization") != "fp8":
            fail("ENDPOINT_UNAVAILABLE")
        if tag.endswith("/mxfp4") and ep.get("quantization") != "mxfp4":
            fail("ENDPOINT_UNAVAILABLE")
        if not {"reasoning", "max_tokens", "response_format", "structured_outputs"} <= set(ep["supported_parameters"]):
            fail("UNSUPPORTED_PARAMETERS")
        return row, data, ep
    except (KeyError, TypeError, StopIteration, ValueError):
        fail("ENDPOINT_UNAVAILABLE")


def prepare(job, metadata, secret=None):
    schema, pixels = validate_job(job, secret)
    row, data, ep = select_endpoint(metadata, job["profile"])
    if pixels and "image" not in data.get("architecture", {}).get("input_modalities", []):
        fail("UNSUPPORTED_PARAMETERS")
    if job["max_tokens"] > ep.get("max_completion_tokens", 0):
        fail("UNSUPPORTED_PARAMETERS")
    model, tag, _, cap = PROFILES[job["profile"]]
    content = [{"type": "text", "text": job["prompt"]}]
    for img in job.get("images", []):
        content.append({"type": "image_url", "image_url": {"url": "data:" + img["mime"] + ";base64," + img["data_base64"]}})
    request = {"model": model, "messages": [{"role": "user", "content": content}], "provider": {"only": [tag], "allow_fallbacks": False, "require_parameters": True}, "reasoning": {"enabled": True}, "max_tokens": job["max_tokens"], "usage": {"include": True}, "response_format": {"type": "json_schema", "json_schema": {"name": "delegated_result", "strict": True, "schema": schema}}}
    pricing = ep["pricing"]
    # Reject newly advertised charge categories that this bounded client cannot price.
    known = {"prompt", "completion", "internal_reasoning", "image", "audio", "web_search", "input_audio_cache", "input_cache_read", "input_cache_write", "discount", "request"}
    if set(pricing) - known:
        fail("UNKNOWN_PRICING")
    prompt_rate = money(pricing.get("prompt"), "UNKNOWN_PRICING")
    output_rate = max(money(pricing.get("completion"), "UNKNOWN_PRICING"), money(pricing.get("internal_reasoning", 0)))
    input_bound = len(encoded(request)) + 4096 + pixels * 4
    if input_bound + job["max_tokens"] > ep["context_length"]:
        fail("INPUT_LIMIT")
    estimate = (prompt_rate * input_bound + output_rate * job["max_tokens"] + money(pricing.get("request", 0))) * Decimal("1.25")
    if pixels:
        estimate += money(pricing.get("image", 0)) * pixels * 4
    if estimate > Decimal(cap):
        fail("CALL_COST_CAP")
    return request, estimate, {"snapshot_sha256": digest(metadata), "checked_at": row["checked_at"], "catalog_model": data.get("id", model), "endpoint": ep, "provider_endpoints": [endpoint_identity(e) for e in data["endpoints"] if e.get("provider_name") == PROFILES[job["profile"]][2]], "policy": {k: ep.get(k, "UNKNOWN") for k in ("data_policy", "data_collection", "retention", "training", "zdr")}}


class Transport:
    def __init__(self, key=None, connect_timeout=10, read_timeout=120):
        self.key = key
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    def request(self, method, path, body=None):
        conn = http.client.HTTPSConnection("openrouter.ai", timeout=self.connect_timeout, context=ssl.create_default_context())
        headers = {"Content-Type": "application/json"}
        if method == "POST":
            headers["X-OpenRouter-Metadata"] = "enabled"
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        try:
            conn.connect()
            conn.sock.settimeout(self.read_timeout)
            conn.request(method, "/api/v1/" + path, body=encoded(body) if body is not None else None, headers=headers)
            res = conn.getresponse()
            raw = res.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                fail("RESPONSE_LIMIT")
            status = res.status
            try:
                obj = json.loads(raw, parse_constant=lambda _: fail("INVALID_JSON"))
            except (ValueError, UnicodeError):
                fail("INVALID_JSON")
            if status != 200:
                fail("HTTP_" + str(status))
            if not isinstance(obj, dict) or obj.get("error") is not None:
                fail("API_ERROR")
            return obj
        except DelegateError:
            raise
        except Exception:
            fail("UNKNOWN_EXECUTION_COST" if method == "POST" else "GET_FAILED")
        finally:
            conn.close()


def discover(transport, profiles=None):
    selected = [profiles] if isinstance(profiles, str) else profiles
    if not isinstance(selected, (list, tuple)) or len(selected) != 1:
        fail("SINGLE_PROFILE_REQUIRED")
    model = profile_spec(selected[0])[0]  # validate before any network operation
    rows = []
    for model in [model]:
        row = {"model": model, "url": "https://openrouter.ai/api/v1/models/" + model + "/endpoints", "checked_at": now()}
        try:
            row.update(http_status=200, metadata=transport.request("GET", "models/" + model + "/endpoints"))
        except DelegateError as exc:
            row.update(http_status=None, error=exc.code)
        rows.append(row)
    return rows


@contextmanager
def locked(path):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    until = time.monotonic() + 5
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() > until:
                fail("LEDGER_LOCKED")
            time.sleep(0.02)
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink()


def reserve(path, budget, key_id, key_remaining, amount, *, owner_local_cap_only=False):
    budget, amount = money(budget, "LIVE_BLOCKED_BUDGET"), money(amount)
    if owner_local_cap_only and (budget <= 0 or budget > 1):
        fail("OWNER_LOCAL_CAP_REJECTED")
    if key_remaining is None:
        if not owner_local_cap_only:
            fail("LIVE_BLOCKED_KEY_LIMIT")
    else:
        key_remaining = money(key_remaining, "LIVE_BLOCKED_KEY_LIMIT")
    if budget <= 0 or (key_remaining is not None and key_remaining <= 0):
        fail("LIVE_BLOCKED_BUDGET")
    with locked(path):
        ledger = load_json(path) if Path(path).exists() else {"budget_usd": str(budget), "key_id": key_id, "spent_usd": "0", "reservations": {}, "blocked": False, "owner_local_cap_only": owner_local_cap_only, "reservation_count": 0}
        if ledger["key_id"] != key_id or money(ledger["budget_usd"]) != budget or ledger.get("owner_local_cap_only", False) is not owner_local_cap_only:
            fail("LEDGER_CONTRACT_MISMATCH")
        if ledger["blocked"]:
            fail("UNKNOWN_EXECUTION_COST")
        count = ledger.get("reservation_count", 0)
        if type(count) is not int or count < 0:
            fail("LEDGER_CONTRACT_MISMATCH")
        if owner_local_cap_only and count >= 3:
            fail("OWNER_SMOKE_LIMIT_REACHED")
        # A pending request can be an interrupted process. Do not assume it is safe to retry.
        if ledger["reservations"]:
            fail("PENDING_EXECUTION_COST")
        held = sum((money(x) for x in ledger["reservations"].values()), Decimal(0))
        local_remaining = budget - money(ledger["spent_usd"]) - held
        if amount > local_remaining or (key_remaining is not None and held + amount > key_remaining):
            fail("BUDGET_EXCEEDED")
        token = uuid.uuid4().hex
        ledger["reservations"][token] = str(amount)
        ledger["reservation_count"] = count + 1  # never decremented, including known failures
        write_json(path, ledger)
        return token


def settle(path, token, cost):
    with locked(path):
        ledger = load_json(path)
        reserved = money(ledger["reservations"][token])
        if cost is None:
            ledger["blocked"] = True
        else:
            cost = money(cost)
            del ledger["reservations"][token]
            ledger["spent_usd"] = str(money(ledger["spent_usd"]) + cost)
            if cost > reserved:
                ledger["blocked"] = True
        write_json(path, ledger)


def usage_cost(response, generation):
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
        fail("UNKNOWN_EXECUTION_COST")
    if usage["total_tokens"] < usage["prompt_tokens"] + usage["completion_tokens"]:
        fail("INVALID_USAGE")
    value = usage.get("cost", generation.get("total_cost"))
    cost = money(value, "UNKNOWN_EXECUTION_COST")
    if "cost" in usage and "total_cost" in generation and abs(cost - money(generation["total_cost"])) > Decimal("0.000001"):
        fail("COST_MISMATCH")
    compact = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    details = usage.get("completion_tokens_details", {})
    reasoning_count = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if reasoning_count is None:
        reasoning_count = generation.get("native_tokens_reasoning")
    if reasoning_count is not None:
        if type(reasoning_count) is not int or reasoning_count < 0:
            fail("INVALID_USAGE")
        compact["reasoning_tokens"] = reasoning_count
    return compact, cost


def infer_unique_kimi_route(response, endpoint_evidence):
    """Infer a catalog route, never measured physical execution precision."""
    model, tag, provider, _ = PROFILES["engineering_research"]
    try:
        ev = endpoint_evidence
        stamp = datetime.fromisoformat(ev["checked_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if not -60 <= age <= 86400 or not re.fullmatch(r"[0-9a-f]{64}", ev["snapshot_sha256"]):
            return None
        if ev["catalog_model"] not in allowed_model_ids(model):
            return None
        candidates = ev["provider_endpoints"]
        ep = endpoint_identity(ev["endpoint"])
        if not isinstance(candidates, list) or len(candidates) != 1 or candidates[0] != ep:
            return None
        if ep["model_id"] not in allowed_model_ids(model) or ep["provider_name"] != provider or ep["tag"] != tag or ep["quantization"] != "mxfp4" or ep["service_tier"] is not None or type(ep["status"]) is not int or ep["status"] != 0:
            return None
        routing = response["openrouter_metadata"]
        if routing["requested"] != model or routing["strategy"] != "direct" or type(routing["attempt"]) is not int or routing["attempt"] != 1 or routing["is_byok"] is not False:
            return None
        available = routing["endpoints"]["available"]
        if not isinstance(available, list) or len(available) != 1:
            return None
        selected = available[0]
        if selected["selected"] is not True or selected["provider"] != provider or selected["model"] not in allowed_model_ids(model):
            return None
        return {"basis": "UNIQUE_PINNED_ENDPOINT_AND_ROUTER_SELECTION", "snapshot_sha256": ev["snapshot_sha256"], "catalog_checked_at": ev["checked_at"], "catalog_model": ev["catalog_model"], "catalog_provider_endpoint_count": 1, "catalog_endpoint": ep, "router_selection": {"requested": model, "strategy": "direct", "attempt": 1, "available_count": 1, "selected": True, "provider": provider, "model": selected["model"], "is_byok": False}, "physical_precision": "NOT_MEASURED"}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def verify_route(response, generation, profile, endpoint_evidence=None):
    model, tag, provider, _ = profile_spec(profile)
    if response.get("error") is not None:
        fail("API_ERROR")
    check_observed_model(response.get("model"), model)
    if "model" in generation:
        check_observed_model(generation["model"], model)
    providers = [response.get("provider"), generation.get("provider_name")]
    if not any(p == provider for p in providers) or any(p not in (None, provider) for p in providers):
        fail("ROUTING_MISMATCH")
    if generation.get("id", response.get("id")) != response.get("id"):
        fail("GENERATION_MISMATCH")
    observations = [response, generation]
    provider_responses = generation.get("provider_responses")
    if provider_responses is not None:
        if not isinstance(provider_responses, list) or any(not isinstance(x, dict) for x in provider_responses):
            fail("ROUTING_MISMATCH")
        for item in provider_responses:
            if item.get("provider_name") not in (None, provider):
                fail("ROUTING_MISMATCH")
            for key in ("model", "model_permaslug"):
                if item.get(key) is not None:
                    check_observed_model(item[key], model)
            observations.append(item)
    routing = response.get("openrouter_metadata")
    if isinstance(routing, dict):
        if isinstance(routing.get("requested"), str) and routing["requested"] in BLOCKED_MODEL_IDS:
            fail("BLOCKED_403")
        if routing.get("requested") not in (None, model):
            fail("ROUTING_MISMATCH")
        available = routing.get("endpoints", {}).get("available", []) if isinstance(routing.get("endpoints", {}), dict) else []
        if isinstance(available, list):
            for selected in available:
                if isinstance(selected, dict) and selected.get("selected") is True:
                    if selected.get("provider") not in (None, provider):
                        fail("ROUTING_MISMATCH")
                    if selected.get("model") is not None:
                        check_observed_model(selected["model"], model)
                    observations.append(selected)
    exacts = [obj["provider_endpoint"] for obj in observations if obj.get("provider_endpoint") is not None]
    tiers = [obj["service_tier"] for obj in observations if obj.get("service_tier") is not None]
    quants = [obj["quantization"] for obj in observations if obj.get("quantization") is not None]
    if any(exact != tag for exact in exacts):
        fail("ROUTING_MISMATCH")
    if tag.endswith("/flex") and any(tier != "flex" for tier in tiers):
        fail("ROUTING_MISMATCH")
    if tag.endswith("/fp8") and any(quant != "fp8" for quant in quants):
        fail("ROUTING_MISMATCH")
    if tag.endswith("/mxfp4") and any(quant != "mxfp4" for quant in quants):
        fail("ROUTING_MISMATCH")
    if profile == "engineering_research":
        if tiers or any(obj.get("is_byok") is not None and obj["is_byok"] is not False for obj in observations):
            fail("ROUTING_MISMATCH")
    if tag in exacts or (tag.endswith("/flex") and "flex" in tiers) or (tag.endswith("/fp8") and "fp8" in quants) or (tag.endswith("/mxfp4") and "mxfp4" in quants):
        return {"basis": "EXPLICIT_ENDPOINT_TIER_OR_QUANTIZATION", "observed_provider_endpoints": exacts, "observed_service_tiers": tiers, "observed_quantizations": quants, "physical_precision": "NOT_MEASURED"}
    if profile == "engineering_research":
        inferred = infer_unique_kimi_route(response, endpoint_evidence)
        if inferred is not None:
            return inferred
    fail("PROVENANCE_UNCONFIRMED")


def validate_response(response, generation, job, secret=None, *, endpoint_evidence=None, verification_evidence=None):
    route = verify_route(response, generation, job["profile"], endpoint_evidence)
    if verification_evidence is not None:
        verification_evidence.update(route)
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        fail("INVALID_CHOICES")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        fail("INCOMPLETE_RESULT")
    msg = choice.get("message", {})
    content = msg.get("content")
    if msg.get("tool_calls") or not isinstance(content, str) or not content.strip():
        fail("INVALID_CONTENT")
    if (secret and secret in content) or re.search(r"sk-or-[A-Za-z0-9_-]+", content):
        fail("SECRET_IN_RESULT")
    try:
        output = json.loads(content, parse_constant=lambda _: fail("INVALID_JSON"))
    except (ValueError, UnicodeError):
        fail("INVALID_JSON")
    # JSON unicode escapes must be decoded before testing for credential leakage.
    if contains_secret(output, secret):
        fail("SECRET_IN_RESULT")
    from jsonschema import Draft202012Validator
    if not Draft202012Validator(job.get("schema", DEFAULT_SCHEMA)).is_valid(output):
        fail("SCHEMA_MISMATCH")
    return output


def contains_secret(value, secret):
    if isinstance(value, str):
        return bool((secret and secret in value) or re.search(r"sk-or-[A-Za-z0-9_-]+", value))
    if isinstance(value, dict):
        return any(contains_secret(k, secret) or contains_secret(v, secret) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_secret(v, secret) for v in value)
    return False


def redact(value, secret):
    """Final defense at the only persisted API metadata boundary."""
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"sk-or-[A-Za-z0-9_-]+", "[REDACTED]", value)
    if isinstance(value, dict):
        return {redact(k, secret): redact(v, secret) for k, v in value.items() if k not in {"reasoning_details", "reasoning_content"}}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    return value


def run(job, metadata, run_dir, ledger=None, budget=None, transport=None, mock=False, owner_local_cap_only=False):
    secret = None if mock else os.environ.get("OPENROUTER_API_KEY")
    request, estimate, evidence = prepare(job, metadata, secret)
    receipt = {"task_id": job["task_id"], "profile": job["profile"], "input_sha256": digest(job), "prompt_sha256": digest(job["prompt"]), "config_sha256": digest({k: v for k, v in request.items() if k != "messages"}), "started_at": now(), "requested_model": request["model"], "requested_provider": request["provider"], "endpoint_evidence": evidence, "reasoning": {"enabled": True}, "reservation_usd": str(estimate), "currency": "USD", "mode": "mock" if mock else "live", "cost_usd": None, "verification": "NOT_RUN"}
    receipt["input_kind"] = "SYNTHETIC" if job["synthetic"] else "AUTHORIZED_SOURCE_VISUAL"
    receipt["synthetic"] = job["synthetic"]
    if job["synthetic"] is False:
        receipt["input_provenance"] = json.loads(encoded(job["input_package"]))
        receipt["input_authorization_basis"] = "EXPLICIT_CALLER_ASSERTION"
    target = Path(run_dir)
    target.mkdir(parents=True, exist_ok=False)  # each attempt is immutable
    start = time.monotonic()
    token = None
    cost = None
    try:
        if mock:
            response = {"id": "mock-generation", "model": request["model"], "provider": PROFILES[job["profile"]][2], "provider_endpoint": PROFILES[job["profile"]][1], "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"result": "synthetic mock; no API request"})}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0}}
            generation = {}
        else:
            if not secret or not secret.strip():
                fail("LIVE_BLOCKED_SECRET")
            if budget is None or ledger is None:
                fail("LIVE_BLOCKED_BUDGET")
            if owner_local_cap_only and (job.get("synthetic") is not True or not 0 < money(budget, "OWNER_LOCAL_CAP_REJECTED") <= 1):
                fail("OWNER_LOCAL_CAP_REJECTED")
            if owner_local_cap_only:
                receipt["local_batch_budget_usd"] = str(money(budget))
                receipt["max_live_reservations"] = 3
            transport = transport or Transport(secret)
            fresh = redact(discover(transport, [job["profile"]]), secret)
            request, estimate, evidence = prepare(job, fresh, secret)
            write_json(target / "endpoint_metadata.json", fresh)
            receipt.update(endpoint_evidence=evidence, reservation_usd=str(estimate))
            key_data = transport.request("GET", "key").get("data", {})
            unlimited = "limit" in key_data and "limit_remaining" in key_data and key_data["limit"] is None and key_data["limit_remaining"] is None
            if unlimited:
                receipt["server_key_limit_status"] = "UNLIMITED_OBSERVED"
                receipt["observed_key_limits"] = {"limit": None, "limit_remaining": None}
                if not owner_local_cap_only:
                    fail("LIVE_BLOCKED_KEY_LIMIT")
                receipt["override"] = "EXPLICIT_OWNER_LOCAL_CAP_FOR_THREE_SMOKES"
                remaining = None  # no invented server cap; reserve checks actual local remainder
            else:
                limit = money(key_data.get("limit"), "LIVE_BLOCKED_KEY_LIMIT")
                remaining = money(key_data.get("limit_remaining"), "LIVE_BLOCKED_KEY_LIMIT")
                if limit <= 0 or remaining > limit:
                    fail("LIVE_BLOCKED_KEY_LIMIT")
                receipt["server_key_limit_status"] = "FINITE_VERIFIED"
                receipt["verified_key_limit_usd"] = str(limit)
                receipt["verified_key_remaining_usd"] = str(remaining)
            token = reserve(ledger, budget, hashlib.sha256(secret.encode()).hexdigest(), remaining, estimate, owner_local_cap_only=owner_local_cap_only)
            response = transport.request("POST", "chat/completions", request)
            generation = {}
            gid = response.get("id")
            if isinstance(gid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", gid):
                receipt["generation_id"] = gid
                try:
                    generation = transport.request("GET", "generation?id=" + quote(gid, safe="")).get("data", {})
                except DelegateError:
                    receipt["generation_lookup"] = "UNAVAILABLE"
            else:
                fail("INVALID_GENERATION_ID")
        receipt["http_status"] = 200
        receipt["api_status"] = "ERROR" if response.get("error") is not None else "OK"
        usage, cost = usage_cost(response, generation)
        receipt.update(usage=usage, cost_usd=str(cost), actual_model=response.get("model"), actual_generation_model=generation.get("model"), model_identity={"requested_alias": request["model"], "canonical_pin": CANONICAL_MODEL_PINS[request["model"]], "response_observed": response.get("model"), "generation_observed": generation.get("model")}, actual_provider=response.get("provider"), observed_endpoint={k: generation.get(k, response.get(k)) for k in ("provider_endpoint", "service_tier", "quantization")})
        if cost > estimate:
            fail("COST_BOUND_EXCEEDED")
        receipt["route_verification"] = {}
        output = validate_response(response, generation, job, secret, endpoint_evidence=evidence, verification_evidence=receipt["route_verification"])
        receipt["output_sha256"] = digest(output)
        write_json(target / "output.json", output)
        receipt["verification"] = "MOCK_PASS" if mock else "PASS"
    except DelegateError as exc:
        receipt["verification"] = exc.code
        if exc.code.startswith("HTTP_"):
            receipt["http_status"] = int(exc.code[5:])
        if exc.code == "API_ERROR":
            receipt.update(http_status=200, api_status="ERROR")
    except Exception:
        receipt["verification"] = "INTERNAL_ERROR"
    finally:
        if token:
            try:
                settle(ledger, token, cost)
            except Exception:
                receipt["verification"] = "LEDGER_SETTLEMENT_ERROR"
        receipt["execution_cost_status"] = "KNOWN" if cost is not None else ("UNKNOWN" if token else "NOT_SENT")
        receipt.update(finished_at=now(), latency_seconds=round(time.monotonic() - start, 3))
        receipt = redact(receipt, secret)
        write_json(target / "receipt.json", receipt)
    return receipt


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--output", required=True)
    d.add_argument("--profile", required=True, help="One active profile; blocked profiles never trigger discovery")
    for name in ("check", "call", "mock"):
        s = sub.add_parser(name)
        s.add_argument("--job", required=True)
        s.add_argument("--metadata", required=True)
        if name != "check":
            s.add_argument("--run-dir", required=True)
        if name == "call":
            s.add_argument("--ledger")
            s.add_argument("--budget-usd")
            s.add_argument("--owner-local-cap-only", action="store_true", help="Explicit owner exception: synthetic only, budget <= USD1, at most 3 reservations in one shared ledger")
    args = p.parse_args(argv)
    try:
        if args.command == "discover":
            result = redact(discover(Transport(), args.profile), None)
            write_json(args.output, result)
            select_endpoint(result, args.profile)
            print(json.dumps({"status": "DISCOVERED", "profile": args.profile, "model": profile_spec(args.profile)[0]}))
            return 0
        job, metadata = load_json(args.job), load_json(args.metadata)
        if args.command == "check":
            _, estimate, _ = prepare(job, metadata)
            print(json.dumps({"status": "CHECK_PASS", "reservation_usd": str(estimate)}))
            return 0
        receipt = run(job, metadata, args.run_dir, ledger=getattr(args, "ledger", None), budget=getattr(args, "budget_usd", None), mock=args.command == "mock", owner_local_cap_only=getattr(args, "owner_local_cap_only", False))
        print(json.dumps({"status": receipt["verification"], "cost_usd": receipt["cost_usd"]}))
        return 0 if receipt["verification"] in ("PASS", "MOCK_PASS") else 2
    except DelegateError as exc:
        print(json.dumps({"status": exc.code}))
        return 2
    except Exception:
        print(json.dumps({"status": "LOCAL_INPUT_ERROR"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
