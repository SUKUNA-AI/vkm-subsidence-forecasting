#!/usr/bin/env python3
"""Bounded one-shot OpenRouter pilot. No shell/tools, retries, or credential files."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import math
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
MAX_TOKENS_BY_PROFILE = MappingProxyType({
    "cheap_code": 4096,
    "vision_documents": 4096,
    "engineering_research": 32768,
})
DEFAULT_SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"], "additionalProperties": False}
MAX_RESPONSE = 64 * 1024 * 1024  # transport RAM guard, not a model context limit
MAX_DECODED_IMAGE_BYTES = 256 * 1024 * 1024  # decoder RAM guard, not model resolution
POLICY_PATH = Path(__file__).resolve().parents[1] / "references" / "DELEGATION_POLICY_V2.json"


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


def load_json(path, limit=64 * 1024 * 1024):
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


def is_v2(job):
    return isinstance(job, dict) and "task_kind" in job


def delegation_policy():
    policy = load_json(POLICY_PATH)
    if policy.get("schema") != "vkm.delegate-policy/2":
        fail("INVALID_PROJECT_POLICY")
    for profile, spec in policy.get("profiles", {}).items():
        pinned = profile_spec(profile)
        if (spec.get("model"), spec.get("provider_only"), spec.get("per_call_usd")) != (pinned[0], pinned[1], pinned[3]):
            fail("INVALID_PROJECT_POLICY")
    if set(policy.get("profiles", {})) != set(PROFILES):
        fail("INVALID_PROJECT_POLICY")
    return policy


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def artifact_locator(locator):
    if not isinstance(locator, dict) or set(locator) != {"kind", "value"}:
        fail("INVALID_PROVENANCE")
    if not isinstance(locator["kind"], str) or locator["kind"] not in {"LINES", "PAGE", "EXCERPT", "FULL_FILE", "CROP"}:
        fail("INVALID_PROVENANCE")
    if not isinstance(locator["value"], str) or not locator["value"].strip() or re.search(r"[\x00-\x1f]|://|\$\{|%[A-Za-z_]+%", locator["value"]):
        fail("INVALID_PROVENANCE")


def validate_artifact_package(package, image_hashes):
    """Verify inline fragments; full original hashes/authorization are assertions."""
    if not isinstance(package, dict) or set(package) != {"schema", "authorization_scope", "selection_rationale", "artifacts"}:
        fail("INVALID_PROVENANCE")
    if package["schema"] != "vkm.delegate-artifact-package/2" or package["authorization_scope"] != "VKM-MULTIAGENT-DELEGATION-V2":
        fail("UNAUTHORIZED_SOURCE_PACKAGE")
    if not isinstance(package["selection_rationale"], str) or not package["selection_rationale"].strip():
        fail("INVALID_PROVENANCE")
    if not isinstance(package["artifacts"], list) or not package["artifacts"]:
        fail("INVALID_PROVENANCE")
    used_images, ids = set(), set()
    for artifact in package["artifacts"]:
        fields = {"artifact_id", "kind", "source_id", "work_id", "logical_name", "full_sha256", "fragments", "images"}
        if not isinstance(artifact, dict) or not fields <= artifact.keys() or set(artifact) - fields - {"registry_reference"}:
            fail("INVALID_PROVENANCE")
        aid = artifact["artifact_id"]
        if not isinstance(aid, str) or not re.fullmatch(r"[\w.:-]+", aid) or aid in ids:
            fail("INVALID_PROVENANCE")
        ids.add(aid)
        if not isinstance(artifact["kind"], str) or artifact["kind"] not in {"SOURCE", "PUBLIC_CODE", "PRIVATE_CODE", "PUBLIC_TEXT", "PRIVATE_TEXT"}:
            fail("INVALID_PROVENANCE")
        for key, pattern in (("source_id", r"VKM-SRC-\d{3}"), ("work_id", r"VKM-WRK-\d{3,}")):
            value = artifact[key]
            if value is not None and (not isinstance(value, str) or not re.fullmatch(pattern, value)):
                fail("INVALID_PROVENANCE")
            if artifact["kind"] == "SOURCE" and value is None:
                fail("INVALID_PROVENANCE")
        logical = artifact["logical_name"]
        if not isinstance(logical, str) or not logical.strip() or logical.startswith(("/", "\\", "~")) or re.search(r"[\x00-\x1f\\$%]|://|^[A-Za-z]:", logical):
            fail("INVALID_PROVENANCE")
        parts = logical.lower().split("/")
        if ".." in parts or any(part.startswith(".env") or part in {".ssh", ".aws", ".codex", ".git"} for part in parts):
            fail("FORBIDDEN_INPUT")
        if not valid_hash(artifact["full_sha256"]) or not isinstance(artifact["fragments"], list) or not isinstance(artifact["images"], list):
            fail("INVALID_PROVENANCE")
        if not artifact["fragments"] and not artifact["images"]:
            fail("INVALID_PROVENANCE")
        for fragment in artifact["fragments"]:
            if not isinstance(fragment, dict) or set(fragment) != {"text", "sha256", "locator"} or not isinstance(fragment["text"], str) or not fragment["text"]:
                fail("INVALID_PROVENANCE")
            artifact_locator(fragment["locator"])
            if not valid_hash(fragment["sha256"]) or hashlib.sha256(fragment["text"].encode("utf-8")).hexdigest() != fragment["sha256"]:
                fail("FRAGMENT_PROVENANCE_MISMATCH")
            if fragment["locator"]["kind"] == "FULL_FILE" and fragment["sha256"] != artifact["full_sha256"]:
                fail("FRAGMENT_PROVENANCE_MISMATCH")
        for image in artifact["images"]:
            if not isinstance(image, dict) or set(image) != {"image_index", "sha256", "locator"}:
                fail("INVALID_PROVENANCE")
            index = image["image_index"]
            if type(index) is not int or not 0 <= index < len(image_hashes) or index in used_images:
                fail("INVALID_PROVENANCE")
            artifact_locator(image["locator"])
            if not valid_hash(image["sha256"]) or image["sha256"] != image_hashes[index]:
                fail("IMAGE_PROVENANCE_MISMATCH")
            used_images.add(index)
    if used_images != set(range(len(image_hashes))):
        fail("UNREFERENCED_IMAGE")


def reasoning_configuration(job, row, ep):
    policy = delegation_policy()
    if job.get("task_kind") not in policy["task_kinds"]:
        fail("UNKNOWN_TASK_KIND")
    spec = policy["profiles"][job["profile"]]
    explicit = job.get("reasoning_effort")
    effort = explicit if explicit is not None else spec.get("task_efforts", {}).get(job["task_kind"], spec["default_effort"])
    if not isinstance(effort, str) or effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        fail("UNSUPPORTED_REASONING_MODE")
    capability = row.get("model_capabilities")
    if not isinstance(capability, dict) or not isinstance(capability.get("model"), dict):
        fail("REASONING_CAPABILITIES_UNKNOWN")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(capability["checked_at"].replace("Z", "+00:00"))).total_seconds()
        model = capability["model"]
        alias = profile_spec(job["profile"])[0]
        if not -60 <= age <= 86400 or model["id"] != alias or model["canonical_slug"] != CANONICAL_MODEL_PINS[alias]:
            fail("STALE_OR_WRONG_REASONING_CAPABILITIES")
        reasoning = model["reasoning"]
        supported = reasoning["supported_efforts"]
        if not isinstance(supported, list) or not supported or not all(isinstance(x, str) for x in supported) or not valid_hash(capability["catalogue_sha256"]):
            fail("REASONING_CAPABILITIES_UNKNOWN")
        if effort not in supported or (effort == "none" and reasoning.get("mandatory") is True):
            fail("UNSUPPORTED_REASONING_MODE")
        if not {"reasoning", "reasoning_effort"} <= set(ep["supported_parameters"]) or not {"reasoning", "reasoning_effort"} <= set(model["supported_parameters"]):
            fail("UNSUPPORTED_REASONING_MODE")
    except (KeyError, ValueError, TypeError):
        fail("REASONING_CAPABILITIES_UNKNOWN")
    if effort in spec["hard_efforts"] and (not isinstance(job.get("reasoning_justification"), str) or not job["reasoning_justification"].strip()):
        fail("HARD_REASONING_JUSTIFICATION_REQUIRED")
    wire = {"enabled": effort != "none", "effort": effort}
    return wire, {"project_policy_sha256": digest(policy), "task_kind": job["task_kind"],
        "caller_requested_effort": explicit, "selected_effort": effort,
        "selection_basis": "CALLER_EXPLICIT" if explicit is not None else "PROJECT_TASK_DEFAULT",
        "wire": wire, "model_default_effort": reasoning.get("default_effort"),
        "supported_efforts": supported, "mandatory": reasoning.get("mandatory"),
        "capability_checked_at": capability["checked_at"], "catalogue_sha256": capability["catalogue_sha256"],
        "capability_scope": "MODEL_CATALOGUE_PLUS_ENDPOINT_PARAMETER_SUPPORT_NOT_EFFECTIVE_PROVIDER_EFFORT",
        "effective_effort": None, "effective_confirmation": "UNCONFIRMED"}


def reasoning_observation(response, generation, configured):
    expected = configured["selected_effort"]
    provider_reports, echoed = [], []
    for label, obj in (("response", response), ("generation", generation)):
        provider = obj.get("provider_metadata")
        for origin, data, sink in ((label, obj, echoed), (label + ".provider_metadata", provider, provider_reports)):
            if not isinstance(data, dict):
                continue
            values = [data.get("reasoning_effort")]
            if isinstance(data.get("reasoning"), dict):
                values.append(data["reasoning"].get("effort"))
            for value in values:
                if value is not None:
                    if value != expected:
                        fail("REASONING_MODE_MISMATCH")
                    sink.append({"source": origin, "effort": value})
    return {"requested_effort": expected, "wire": configured["wire"],
            "echo_observations": echoed, "provider_reports": provider_reports,
            "effective_effort": expected if provider_reports else None,
            "effective_confirmation": "PROVIDER_METADATA_REPORTED_NOT_INDEPENDENTLY_MEASURED" if provider_reports else "UNCONFIRMED",
            "inference": {"intended_effort": expected, "actual_effort_inferred": None,
                          "basis": "REQUEST_AND_SUPPORTED_CAPABILITY_DO_NOT_PROVE_EFFECTIVE_EFFORT"}}


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
    v2 = is_v2(job)
    if v2:
        allowed |= {"task_kind", "reasoning_effort", "reasoning_justification"}
    if not isinstance(job, dict) or set(job) - allowed or type(job.get("synthetic")) is not bool:
        fail("FORBIDDEN_INPUT")
    profile_spec(job.get("profile"))
    if v2 and job["task_kind"] not in delegation_policy()["task_kinds"]:
        fail("UNKNOWN_TASK_KIND")
    artifact_package = isinstance(job.get("input_package"), dict) and job["input_package"].get("schema") == "vkm.delegate-artifact-package/2"
    if artifact_package and not v2:
        fail("V2_TASK_KIND_REQUIRED")
    if job["synthetic"] is True and "input_package" in job:
        fail("FORBIDDEN_INPUT")
    if job["synthetic"] is False and ("input_package" not in job or (not artifact_package and job["profile"] != "vision_documents")):
        fail("FORBIDDEN_INPUT")
    if not isinstance(job.get("task_id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", job["task_id"]):
        fail("FORBIDDEN_INPUT")
    prompt = job.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or (not v2 and len(prompt.encode("utf-8")) > 16000):
        fail("INPUT_LIMIT")
    text = encoded(job).decode("utf-8")
    if len(text.encode("utf-8")) > MAX_RESPONSE:
        fail("TRANSPORT_RAM_GUARD")
    if (secret and secret in text) or re.search(r"sk-or-[A-Za-z0-9_-]+|(?:api[_ -]?key|password|authorization)\s*[:=]\s*\S+", text, re.I):
        fail("FORBIDDEN_INPUT")
    if v2 and re.search(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?):/{2}[^\s/]*@|Bearer\s+sk-[A-Za-z0-9_-]+)", text, re.I):
        fail("FORBIDDEN_INPUT")
    if type(job.get("max_tokens")) is not int or job["max_tokens"] < (1 if v2 else 64) or (not v2 and job["max_tokens"] > MAX_TOKENS_BY_PROFILE[job["profile"]]):
        fail("INPUT_LIMIT")
    schema = job.get("schema", DEFAULT_SCHEMA)
    if not v2 and len(encoded(schema)) > 8192:
        fail("INPUT_LIMIT")
    schema_check(schema)
    images = job.get("images", [])
    if not isinstance(images, list) or (not v2 and len(images) > 2):
        fail("INPUT_LIMIT")
    if job["synthetic"] is False and not artifact_package and len(images) != 1:
        fail("INVALID_PROVENANCE")
    pixels = 0
    image_hashes = []
    for img in images:
        if not isinstance(img, dict) or set(img) != {"mime", "data_base64", "width", "height", "bbox"}:
            fail("INVALID_IMAGE")
        if img["mime"] not in {"image/png", "image/jpeg"}:
            fail("INVALID_IMAGE")
        w, h = img["width"], img["height"]
        if type(w) is not int or type(h) is not int or w < 1 or h < 1 or (not v2 and (w > 2048 or h > 2048)):
            fail("INVALID_IMAGE")
        if v2 and (pixels + w * h) * 4 > MAX_DECODED_IMAGE_BYTES:
            fail("IMAGE_DECODER_RAM_GUARD")
        bbox = img["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4 or any(type(x) is not int for x in bbox) or not (0 <= bbox[0] < bbox[2] <= w and 0 <= bbox[1] < bbox[3] <= h):
            fail("INVALID_IMAGE")
        try:
            raw = base64.b64decode(img["data_base64"], validate=True)
            if not 1 <= len(raw) <= (MAX_RESPONSE if v2 else 1024 * 1024):
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
    if not v2 and pixels > 4_000_000:
        fail("INPUT_LIMIT")
    if not v2 and job["profile"] == "vision_documents" and not images:
        fail("INVALID_IMAGE")
    if job["synthetic"] is False:
        if artifact_package:
            validate_artifact_package(job["input_package"], image_hashes)
        else:
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
    endpoint_max = ep.get("max_completion_tokens")
    if type(endpoint_max) is not int or endpoint_max < 1 or job["max_tokens"] > endpoint_max:
        fail("UNSUPPORTED_PARAMETERS")
    model, tag, _, cap = PROFILES[job["profile"]]
    content = [{"type": "text", "text": job["prompt"]}]
    if is_v2(job) and isinstance(job.get("input_package"), dict) and job["input_package"].get("schema") == "vkm.delegate-artifact-package/2":
        for artifact in job["input_package"]["artifacts"]:
            for fragment in artifact["fragments"]:
                # Separate JSON text part labels untrusted content as a bounded
                # source artifact; it grants neither file reads nor execution.
                content.append({"type": "text", "text": encoded({"artifact_id": artifact["artifact_id"], "logical_name": artifact["logical_name"], "locator": fragment["locator"], "sha256": fragment["sha256"], "untrusted_artifact_text": fragment["text"]}).decode("utf-8")})
    for img in job.get("images", []):
        content.append({"type": "image_url", "image_url": {"url": "data:" + img["mime"] + ";base64," + img["data_base64"]}})
    request = {"model": model, "messages": [{"role": "user", "content": content}], "provider": {"only": [tag], "allow_fallbacks": False, "require_parameters": True}, "reasoning": {"enabled": True}, "max_tokens": job["max_tokens"], "usage": {"include": True}, "response_format": {"type": "json_schema", "json_schema": {"name": "delegated_result", "strict": True, "schema": schema}}}
    reasoning_evidence = None
    if is_v2(job):
        request["reasoning"], reasoning_evidence = reasoning_configuration(job, row, ep)
    pricing = ep["pricing"]
    # Reject newly advertised charge categories that this bounded client cannot price.
    known = {"prompt", "completion", "internal_reasoning", "image", "audio", "web_search", "input_audio_cache", "input_cache_read", "input_cache_write", "discount", "request"}
    if set(pricing) - known:
        fail("UNKNOWN_PRICING")
    prompt_rate = money(pricing.get("prompt"), "UNKNOWN_PRICING")
    output_rate = (money(pricing.get("completion"), "UNKNOWN_PRICING") + money(pricing.get("internal_reasoning", 0))) if is_v2(job) else max(money(pricing.get("completion"), "UNKNOWN_PRICING"), money(pricing.get("internal_reasoning", 0)))
    textual_request = request
    if is_v2(job):
        textual_request = {**request, "messages": [{"role": "user", "content": [part for part in content if part["type"] == "text"]}]}
    input_bound = len(encoded(textual_request)) + 4096 + pixels * 4
    if type(ep.get("context_length")) is not int or ep["context_length"] < 1 or input_bound + job["max_tokens"] > ep["context_length"]:
        fail("INPUT_LIMIT")
    estimate = (prompt_rate * input_bound + output_rate * job["max_tokens"] + money(pricing.get("request", 0))) * Decimal("1.25")
    if pixels:
        estimate += money(pricing.get("image", 0)) * (len(job.get("images", [])) if is_v2(job) else pixels * 4)
    if estimate > Decimal(cap):
        fail("CALL_COST_CAP")
    evidence = {"snapshot_sha256": digest(metadata), "checked_at": row["checked_at"], "catalog_model": data.get("id", model), "endpoint": ep, "provider_endpoints": [endpoint_identity(e) for e in data["endpoints"] if e.get("provider_name") == PROFILES[job["profile"]][2]], "policy": {k: ep.get(k, "UNKNOWN") for k in ("data_policy", "data_collection", "retention", "training", "zdr")}}
    if reasoning_evidence is not None:
        evidence["reasoning_policy"] = reasoning_evidence
        evidence["accounting"] = {"method": "CONSERVATIVE_TEXT_UTF8_BYTE_BOUND_PLUS_4096_PROTOCOL_GUARD_PLUS_4_PER_IMAGE_PIXEL_ESTIMATE",
            "quantity_status": "CONSERVATIVE_ESTIMATE_NOT_MODEL_TOKENIZATION_OR_ACTUAL_USAGE",
            "image_method_status": "LEGACY_SAFETY_OVERESTIMATE_PENDING_EXACT_PROVIDER_ACCOUNTING" if pixels else "NO_IMAGES",
            "estimated_input_token_bound": input_bound, "completion_token_cap_includes_reasoning": job["max_tokens"],
            "image_tariff_method": "USD_PER_INPUT_IMAGE_TIMES_IMAGE_COUNT_PLUS_CONSERVATIVE_PROMPT_TOKEN_ESTIMATE_POSSIBLE_OVERCOUNT",
            "image_tariff_usd_per_image": str(money(pricing.get("image", 0))),
            "image_tariff_specification": "https://openrouter.ai/openapi.json (pricing.image: USD per input image)",
            "output_rate_bound": str(output_rate), "output_rate_method": "COMPLETION_PLUS_INTERNAL_REASONING_CONSERVATIVE",
            "endpoint_context_length": ep["context_length"], "endpoint_max_completion_tokens": endpoint_max,
            "technical_ram_guard_bytes": MAX_RESPONSE, "model_input_bytes_limit": None}
    return request, estimate, evidence


class ReplayTransport:
    """Frozen protocol responses only; this class has no HTTP implementation.

    Scratch protocol-test subclasses may implement deterministic failure cases.
    It is never exposed as a CLI live override and does not read key/source files.
    """
    offline_replay = True

    def __init__(self, transcript):
        if not isinstance(transcript, list) or len(encoded(transcript)) > MAX_RESPONSE:
            fail("INVALID_REPLAY_TRANSCRIPT")
        self.transcript = json.loads(encoded(transcript))
        self.position = 0

    def request(self, method, path, body=None):
        if self.position >= len(self.transcript):
            fail("REPLAY_EXHAUSTED")
        item = self.transcript[self.position]
        self.position += 1
        if not isinstance(item, dict) or set(item) != {"method", "path", "response"} or (item["method"], item["path"]) != (method, path) or not isinstance(item["response"], dict):
            fail("REPLAY_TRANSCRIPT_MISMATCH")
        return json.loads(encoded(item["response"]))


class Transport:
    def __init__(self, key=None, connect_timeout=10, read_timeout=330):
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
            conn.sock.settimeout(self.read_timeout if method == "POST" else min(self.read_timeout, 30))
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


def discover(transport, profiles=None, *, include_reasoning=False):
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
    if include_reasoning:
        catalogue = transport.request("GET", "models")
        models = catalogue.get("data")
        if not isinstance(models, list):
            fail("REASONING_CAPABILITIES_UNKNOWN")
        matches = [entry for entry in models if isinstance(entry, dict) and entry.get("id") == model]
        if len(matches) != 1:
            fail("REASONING_CAPABILITIES_UNKNOWN")
        selected = {key: matches[0].get(key) for key in ("id", "canonical_slug", "reasoning", "supported_parameters", "architecture", "top_provider")}
        rows[0]["model_capabilities"] = {"checked_at": now(), "catalogue_sha256": digest(catalogue), "model": selected}
    return rows


def _lease_host_identity():
    """Stable local host/kernel identity; unconfirmed identity never enables spend."""
    import socket
    try:
        if sys.platform == "win32":
            import winreg
            flags = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, flags) as key:
                machine, _ = winreg.QueryValueEx(key, "MachineGuid")
            if not isinstance(machine, str) or not machine:
                return None
            raw = socket.gethostname().casefold() + "|" + machine
            return "windows:" + hashlib.sha256(raw.encode()).hexdigest()
        if sys.platform.startswith("linux"):
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            namespace = os.readlink("/proc/self/ns/pid")
            if not boot or not namespace.startswith("pid:["):
                return None
            raw = socket.gethostname() + "|" + boot + "|" + namespace
            return "linux:" + hashlib.sha256(raw.encode()).hexdigest()
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _process_birth_identity(pid):
    """Birth of a currently live process, not just existence of a reusable PID."""
    if type(pid) is not int or not 0 < pid < 2 ** 32:
        return None
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
            kernel.GetProcessTimes.restype = wintypes.BOOL
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                if kernel.WaitForSingleObject(handle, 0) != 0x00000102:  # WAIT_TIMEOUT means live
                    return None
                creation, exited, system, user = (wintypes.FILETIME() for _ in range(4))
                if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
                    return None
                birth = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                return "windows-filetime:" + str(birth) if birth > 0 else None
            finally:
                kernel.CloseHandle(handle)
        if sys.platform.startswith("linux"):
            raw = Path(f"/proc/{pid}/stat").read_text()
            fields = raw.rsplit(")", 1)[1].split()  # comm may itself contain spaces/parentheses
            if fields[0] in {"Z", "X", "x"} or len(fields) <= 19 or not fields[19].isdigit():
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if not boot:
                return None
            return "linux-startticks:" + boot + ":" + fields[19]
    except (OSError, ValueError, AttributeError, IndexError, OverflowError):
        return None
    return None


def _new_reservation_lease(lease_seconds=900):
    import math
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 1800:
        fail("INVALID_RESERVATION_LEASE")
    host, pid = _lease_host_identity(), os.getpid()
    birth = _process_birth_identity(pid)
    wall, monotonic = time.time(), time.monotonic()
    if not host or not birth or not all(math.isfinite(value) for value in (wall, monotonic)):
        fail("PROCESS_IDENTITY_UNCONFIRMED")
    return {"state": "ACTIVE", "pid": pid, "process_birth": birth, "host": host,
            "created_unix": wall, "deadline_unix": wall + lease_seconds,
            "created_monotonic": monotonic, "deadline_monotonic": monotonic + lease_seconds}


def _lease_is_confirmed_active(lease, host=None):
    import math
    if not isinstance(lease, dict) or lease.get("state") != "ACTIVE":
        return False
    host = host or _lease_host_identity()
    if not host or lease.get("host") != host:
        return False
    keys = ("created_unix", "deadline_unix", "created_monotonic", "deadline_monotonic")
    if any(type(lease.get(key)) not in (int, float) for key in keys):
        return False
    try:
        if not all(math.isfinite(lease[key]) for key in keys):
            return False
        wall_duration = lease["deadline_unix"] - lease["created_unix"]
        mono_duration = lease["deadline_monotonic"] - lease["created_monotonic"]
        if not (1 <= wall_duration <= 1800 and 1 <= mono_duration <= 1800) or abs(wall_duration - mono_duration) > 0.01:
            return False
        wall, monotonic = time.time(), time.monotonic()
        if not (lease["created_unix"] <= wall < lease["deadline_unix"] and
                lease["created_monotonic"] <= monotonic < lease["deadline_monotonic"]):
            return False
        birth = _process_birth_identity(lease.get("pid"))
        return bool(birth and birth == lease.get("process_birth"))
    except (ValueError, TypeError, OverflowError):
        return False


def _ledger_money_sum(values):
    """Exact decimal addition even for a caller's unusually precise finite costs."""
    from decimal import localcontext
    values = [money(value) for value in values]
    nonzero = [value for value in values if value]
    precision = 28 if not nonzero else max(28, max(value.adjusted() for value in nonzero)
        - min(value.as_tuple().exponent for value in nonzero) + len(str(len(values))) + 3)
    if precision > 10000:
        fail("INVALID_COST")
    with localcontext() as context:
        context.prec = precision
        return sum(values, Decimal(0))


def _ledger_fields(ledger):
    if (not isinstance(ledger, dict) or not isinstance(ledger.get("key_id"), str) or not ledger["key_id"] or
            type(ledger.get("blocked")) is not bool or type(ledger.get("owner_local_cap_only", False)) is not bool or
            type(ledger.get("reservation_count")) is not int or ledger["reservation_count"] < 0 or
            not isinstance(ledger.get("reservations"), dict) or not isinstance(ledger.get("reservation_leases", {}), dict)):
        fail("LEDGER_CONTRACT_MISMATCH")
    try:
        budget, spent = money(ledger["budget_usd"]), money(ledger["spent_usd"])
    except (KeyError, DelegateError):
        fail("LEDGER_CONTRACT_MISMATCH")
    if budget <= 0:
        fail("LEDGER_CONTRACT_MISMATCH")
    for token, amount in ledger["reservations"].items():
        if not isinstance(token, str) or not token or not isinstance(amount, str) or money(amount) <= 0:
            fail("LEDGER_CONTRACT_MISMATCH")
    return budget, spent, ledger["reservations"], ledger.get("reservation_leases", {}), ledger["reservation_count"]


def _unknown_reservation_lease(previous, reason):
    lease = dict(previous) if isinstance(previous, dict) else {
        "pid": None, "process_birth": None, "host": None, "created_unix": None,
        "deadline_unix": None, "created_monotonic": None, "deadline_monotonic": None}
    lease.update(state="UNKNOWN", reason=reason)
    return lease


@contextmanager
def locked(path):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    until = time.monotonic() + 5
    marker = uuid.uuid4().hex.encode("ascii")
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() > until:
                fail("LEDGER_LOCKED")
            time.sleep(0.02)
    try:
        os.write(fd, marker)
        os.fsync(fd)
        identity = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        yield
    finally:
        # Never remove a stale/foreign/replaced lock. Only this acquisition's
        # exact lock may be released; process death deliberately leaves it.
        try:
            current = lock.lstat()
            owns_lock = ((current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)
                         and lock.read_bytes() == marker)
        except OSError:
            owns_lock = False
        if not owns_lock:
            fail("LEDGER_LOCK_OWNERSHIP_LOST")
        lock.unlink()


def reserve(path, budget, key_id, key_remaining, amount, *, owner_local_cap_only=False,
            profile=None, lease_seconds=900):
    budget, amount = money(budget, "LIVE_BLOCKED_BUDGET"), money(amount)
    if owner_local_cap_only and (budget <= 0 or budget > 1):
        fail("OWNER_LOCAL_CAP_REJECTED")
    if budget <= 0 or budget > 20:
        fail("LIVE_BLOCKED_BUDGET")
    if amount <= 0:
        fail("INVALID_RESERVATION_AMOUNT")
    cap = money(profile_spec(profile)[3]) if profile is not None else max(money(spec[3]) for spec in PROFILES.values())
    if amount > cap:
        fail("PER_CALL_CAP_EXCEEDED")
    if key_remaining is None:
        if not owner_local_cap_only:
            fail("LIVE_BLOCKED_KEY_LIMIT")
    else:
        key_remaining = money(key_remaining, "LIVE_BLOCKED_KEY_LIMIT")
        if key_remaining <= 0:
            fail("LIVE_BLOCKED_KEY_LIMIT")
    if type(owner_local_cap_only) is not bool or not isinstance(key_id, str) or not key_id:
        fail("LEDGER_CONTRACT_MISMATCH")
    with locked(path):
        ledger = load_json(path) if Path(path).exists() else {"budget_usd": str(budget), "key_id": key_id,
            "spent_usd": "0", "reservations": {}, "reservation_leases": {}, "blocked": False,
            "owner_local_cap_only": owner_local_cap_only, "reservation_count": 0}
        actual_budget, spent, reservations, leases, count = _ledger_fields(ledger)
        if ledger["key_id"] != key_id or actual_budget != budget or ledger.get("owner_local_cap_only", False) is not owner_local_cap_only:
            fail("LEDGER_CONTRACT_MISMATCH")
        if ledger["blocked"]:
            fail("UNKNOWN_EXECUTION_COST")
        if owner_local_cap_only and count >= 3:
            fail("OWNER_SMOKE_LIMIT_REACHED")
        if set(leases) - set(reservations):
            fail("PENDING_EXECUTION_COST")
        if reservations:
            # The old three-smoke exception stays serial and does not inherit V2.
            if owner_local_cap_only:
                fail("PENDING_EXECUTION_COST")
            host = _lease_host_identity()
            if not host or any(not _lease_is_confirmed_active(leases.get(token), host) for token in reservations):
                fail("PENDING_EXECUTION_COST")
        held = _ledger_money_sum(reservations.values())
        if (_ledger_money_sum((spent, held, amount)) > budget or
                (key_remaining is not None and _ledger_money_sum((held, amount)) > key_remaining)):
            fail("BUDGET_EXCEEDED")
        lease = _new_reservation_lease(lease_seconds)
        token = uuid.uuid4().hex
        ledger["reservations"][token] = str(amount)  # retain the existing journal shape
        ledger.setdefault("reservation_leases", {})[token] = lease
        ledger["reservation_count"] = count + 1  # lifetime counter: never reset/decrement
        write_json(path, ledger)
        return token


def settle(path, token, cost):
    invalid = False
    with locked(path):
        ledger = load_json(path)
        _, spent, reservations, leases, _ = _ledger_fields(ledger)
        if token not in reservations:
            fail("RESERVATION_NOT_FOUND")
        reserved = money(reservations[token])
        if cost is not None:
            try:
                cost = money(cost)
            except DelegateError:
                cost, invalid = None, True
        if cost is None:
            ledger["blocked"] = True
            ledger.setdefault("reservation_leases", {})[token] = _unknown_reservation_lease(
                leases.get(token), "INVALID_COST" if invalid else "UNKNOWN_EXECUTION_COST")
        else:
            del ledger["reservations"][token]
            ledger.get("reservation_leases", {}).pop(token, None)
            ledger["spent_usd"] = str(_ledger_money_sum((spent, cost)))
            if cost > reserved:
                ledger["blocked"] = True
            # Paying one known bill never forgives any other unknown/pending bill.
            host = _lease_host_identity()
            if any(not _lease_is_confirmed_active(ledger.get("reservation_leases", {}).get(other), host)
                   for other in ledger["reservations"]):
                ledger["blocked"] = True
        write_json(path, ledger)
    if invalid:
        fail("INVALID_COST")


def reservation_metadata(path, token):
    """One sanitized lease for a private receipt; no key ID or credential values."""
    with locked(path):
        ledger = load_json(path)
        _, _, reservations, leases, _ = _ledger_fields(ledger)
        if token not in reservations:
            fail("RESERVATION_NOT_FOUND")
        lease = leases.get(token)
        return {"reservation_usd": reservations[token], "lease": dict(lease) if isinstance(lease, dict) else None,
                "confirmed_active": _lease_is_confirmed_active(lease)}




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
    if is_v2(job):
        if not isinstance(endpoint_evidence, dict) or "reasoning_policy" not in endpoint_evidence:
            fail("REASONING_CAPABILITIES_UNKNOWN")
        observed = reasoning_observation(response, generation, endpoint_evidence["reasoning_policy"])
        if verification_evidence is not None:
            verification_evidence["reasoning"] = observed
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
    replay = not mock and isinstance(transport, ReplayTransport) and type(transport) is not Transport
    if not mock and not is_v2(job) and not replay:
        fail("V2_TASK_KIND_REQUIRED")
    receipt = {"task_id": job["task_id"], "profile": job["profile"], "input_sha256": digest(job), "prompt_sha256": digest(job["prompt"]), "config_sha256": digest({k: v for k, v in request.items() if k != "messages"}), "started_at": now(), "requested_model": request["model"], "requested_provider": request["provider"], "endpoint_evidence": evidence, "reasoning": {"enabled": True}, "reservation_usd": str(estimate), "currency": "USD", "mode": "mock" if mock else "live", "cost_usd": None, "verification": "NOT_RUN"}
    receipt["input_kind"] = "SYNTHETIC" if job["synthetic"] else "AUTHORIZED_SOURCE_VISUAL"
    receipt["synthetic"] = job["synthetic"]
    if replay:
        receipt["mode"] = "offline_replay"
        receipt["live_authorization"] = "NONE_OFFLINE_TRANSPORT_DECLARATION_ONLY"
    if is_v2(job):
        receipt["delegation_version"] = 2
        receipt["task_kind"] = job["task_kind"]
        receipt["reasoning"] = dict(request["reasoning"])
        receipt["reasoning_policy"] = evidence["reasoning_policy"]
        receipt["reasoning_verification"] = {"effective_effort": None, "effective_confirmation": "UNCONFIRMED"}
        receipt["token_accounting"] = evidence["accounting"]
        receipt["acceptance"] = {"API_SUCCESS": "NOT_RUN", "OUTPUT_SCHEMA_VALID": "NOT_RUN", "TESTS_PASS": "NOT_RUN", "SOURCE_CONSISTENT": "NOT_RUN", "AGENT_REVIEWED": "NOT_RUN", "HUMAN_REVIEWED": "NOT_RUN", "SCIENTIFICALLY_ACCEPTED": "NOT_RUN"}
    if job["synthetic"] is False:
        provenance = json.loads(encoded(job["input_package"]))
        if provenance.get("schema") == "vkm.delegate-artifact-package/2":
            for artifact in provenance["artifacts"]:
                for fragment in artifact["fragments"]:
                    fragment.pop("text", None)
                artifact["full_file_hash_verification"] = "CALLER_ASSERTED_NOT_SOURCE_FILE_READ_BY_CLIENT"
            receipt["input_kind"] = "AUTHORIZED_INLINE_ARTIFACTS"
        receipt["input_provenance"] = provenance
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
            fresh = redact(discover(transport, [job["profile"]], include_reasoning=is_v2(job)), secret)
            request, estimate, evidence = prepare(job, fresh, secret)
            write_json(target / "endpoint_metadata.json", fresh)
            receipt.update(endpoint_evidence=evidence, reservation_usd=str(estimate))
            if is_v2(job):
                receipt.update(reasoning_policy=evidence["reasoning_policy"], token_accounting=evidence["accounting"], reasoning=dict(request["reasoning"]))
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
            reserve_options = {"owner_local_cap_only": owner_local_cap_only}
            if is_v2(job):
                reserve_options["profile"] = job["profile"]
                read_timeout = getattr(transport, "read_timeout", 330)
                if not isinstance(read_timeout, (int, float)) or isinstance(read_timeout, bool) or not 0 < read_timeout <= 1500:
                    fail("UNBOUNDED_TRANSPORT_TIMEOUT")
                reserve_options["lease_seconds"] = max(900, math.ceil(read_timeout + 150))
            token = reserve(ledger, budget, hashlib.sha256(secret.encode()).hexdigest(), remaining, estimate, **reserve_options)
            if is_v2(job):
                lease_reader = globals().get("reservation_metadata")
                receipt["reservation_lease"] = lease_reader(ledger, token) if callable(lease_reader) else {"status": "LEDGER_HELPER_NOT_AVAILABLE_IN_ISOLATED_CANDIDATE"}
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
        if is_v2(job):
            receipt["acceptance"]["API_SUCCESS"] = "MOCK_ONLY" if mock or replay else ("PASS" if receipt["api_status"] == "OK" else "FAIL")
        usage, cost = usage_cost(response, generation)
        receipt.update(usage=usage, cost_usd=str(cost), actual_model=response.get("model"), actual_generation_model=generation.get("model"), model_identity={"requested_alias": request["model"], "canonical_pin": CANONICAL_MODEL_PINS[request["model"]], "response_observed": response.get("model"), "generation_observed": generation.get("model")}, actual_provider=response.get("provider"), observed_endpoint={k: generation.get(k, response.get(k)) for k in ("provider_endpoint", "service_tier", "quantization")})
        if cost > estimate:
            fail("COST_BOUND_EXCEEDED")
        receipt["route_verification"] = {}
        output = validate_response(response, generation, job, secret, endpoint_evidence=evidence, verification_evidence=receipt["route_verification"])
        if is_v2(job):
            receipt["reasoning_verification"] = receipt["route_verification"]["reasoning"]
            receipt["acceptance"]["API_SUCCESS"] = "MOCK_ONLY" if mock or replay else "PASS"
            receipt["acceptance"]["OUTPUT_SCHEMA_VALID"] = "PASS"
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
    d.add_argument("--task-kind", help="V2 task kind; enables fresh model reasoning-capability discovery")
    for name in ("check", "call", "mock"):
        s = sub.add_parser(name)
        s.add_argument("--job", required=True)
        s.add_argument("--metadata", required=True)
        s.add_argument("--task-kind", help="Explicit V2 task kind, must match any task_kind in job")
        s.add_argument("--reasoning-effort", help="Explicit supported effort; no automatic fallback")
        s.add_argument("--reasoning-justification", help="Specific necessity for max/xhigh")
        if name != "check":
            s.add_argument("--run-dir", required=True)
        if name == "call":
            s.add_argument("--ledger")
            s.add_argument("--budget-usd")
            s.add_argument("--owner-local-cap-only", action="store_true", help="Explicit owner exception: synthetic only, budget <= USD1, at most 3 reservations in one shared ledger")
    args = p.parse_args(argv)
    try:
        if args.command == "discover":
            if args.task_kind is not None and args.task_kind not in delegation_policy()["task_kinds"]:
                fail("UNKNOWN_TASK_KIND")
            result = redact(discover(Transport(), args.profile, include_reasoning=args.task_kind is not None), None)
            write_json(args.output, result)
            select_endpoint(result, args.profile)
            print(json.dumps({"status": "DISCOVERED", "profile": args.profile, "model": profile_spec(args.profile)[0]}))
            return 0
        job, metadata = load_json(args.job), load_json(args.metadata)
        for name in ("task_kind", "reasoning_effort", "reasoning_justification"):
            value = getattr(args, name, None)
            if value is not None:
                if name in job and job[name] != value:
                    fail("CLI_JOB_POLICY_CONFLICT")
                job[name] = value
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
