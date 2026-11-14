"""Native HTTP-hook admission, never a forwarding proxy.

Only the OpenCode process transports provider requests. This localhost control
endpoint checks their exact captured bodies and serializes ledger operations on
one owning thread. It never receives an API key or logs source/tool payloads.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import queue
import secrets
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from vkm_worker_core import WorkerError, need, finite_money, json_bytes, sha256_file, write_json, parse_json

PROFILE_NAMES = {"deepseek": "cheap_code", "kimi": "engineering_research",
                 "hydrogen": "vision_documents", "opus": "critical_review"}
KINDS = {"primary": "main", "title": "title", "compaction": "compaction", "generate": "summary"}


class WirePolicy:
    def __init__(self, profile: dict, endpoint: dict, *, target_url="https://openrouter.ai/api/v1/chat/completions"):
        self.profile = profile
        self.endpoint = endpoint
        self.target_url = target_url

    def inspect(self, payload: dict) -> tuple[dict, Decimal, dict]:
        need(set(payload) == {"body_text", "url", "kind"}, "unexpected hook request fields")
        need(payload["url"] == self.target_url, "provider URL differs from pinned endpoint")
        need(payload["kind"] in KINDS, "unknown native request kind")
        need(isinstance(payload["body_text"], str), "captured body text missing")
        raw = payload["body_text"].encode("utf-8")
        body = parse_json(payload["body_text"])
        need(isinstance(body, dict), "request body missing")
        p = self.profile
        need(body.get("model") == p["model_id"], "request model differs from selected model")
        provider = body.get("provider")
        need(provider == {"only": p["provider_only"], "allow_fallbacks": False, "require_parameters": True}, "request provider policy differs")
        reason = body.get("reasoning")
        need(reason == {"enabled": True, "effort": p["reasoning_effort"], "exclude": True}, "request reasoning differs")
        limits = [body[k] for k in ("max_tokens", "max_completion_tokens", "max_output_tokens") if k in body]
        need(len(limits) == 1 and type(limits[0]) is int and 0 < limits[0] <= p["max_output_tokens"], "missing/ambiguous/oversized output cap")
        output_cap = limits[0]
        forbidden = sorted(set(body) & {"models", "route", "transforms", "plugins"})
        need(not forbidden, "unqualified body routing/extension: " + ",".join(forbidden))
        # Bound text and tool schemas together. Multimodal inputs require a
        # separately qualified image/file accounting policy; never price as text.
        messages = body.get("messages")
        need(isinstance(messages, list) and messages, "missing messages")
        for message in messages:
            need(isinstance(message, dict), "invalid message")
            content = message.get("content")
            if isinstance(content, list):
                need(all(isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str) for x in content), "unqualified nontext accounting")
            else:
                need(content is None or isinstance(content, str), "unqualified content")
        ep = self.endpoint
        need(type(ep.get("context_length")) is int and type(ep.get("max_completion_tokens")) is int, "endpoint limits unknown")
        need(output_cap <= ep["max_completion_tokens"], "endpoint output limit exceeded")
        pricing = ep.get("pricing", {})
        known = {"prompt", "completion", "internal_reasoning", "image", "audio", "web_search", "input_audio_cache", "input_cache_read", "input_cache_write", "input_cache_write_1h", "discount", "request"}
        need(set(pricing) <= known, "unpriced endpoint category")
        prompt_rate = finite_money(pricing.get("prompt"), "prompt tariff")
        # Tool-use may trigger caching. Use worst advertised cache tariff for
        # every profile, rather than assuming a cheaper cached request.
        prompt_rate = max(prompt_rate, *(finite_money(pricing.get(k, 0)) for k in ("input_cache_read", "input_cache_write", "input_cache_write_1h")))
        output_rate = finite_money(pricing.get("completion"), "output tariff") + finite_money(pricing.get("internal_reasoning", 0))
        input_bound = len(raw) + 4096
        need(input_bound + output_cap <= ep["context_length"], "endpoint context bound exceeded")
        estimate = (prompt_rate * input_bound + output_rate * output_cap + finite_money(pricing.get("request", 0))) * Decimal("1.25")
        evidence = {"method": "EXACT_CAPTURED_BODY_UTF8_BYTES_PLUS_4096_PROTOCOL_GUARD_125_PERCENT",
                    "quantity_status": "CONSERVATIVE_ESTIMATE_NOT_PROVIDER_TOKENIZATION",
                    "request_bytes": len(raw), "input_bound": input_bound,
                    "max_tokens": output_cap, "input_rate_bound": str(prompt_rate),
                    "output_rate_bound": str(output_rate), "upper_usd": str(estimate)}
        pins = {"model": body["model"], "provider": provider, "reasoning": reason,
                "request_sha256": hashlib.sha256(raw).hexdigest(), "kind": KINDS[payload["kind"]], "retry_count": 0}
        return pins, estimate, evidence


class BudgetThread:
    """All SharedSession methods execute on the same ledger-lock-owning thread."""
    def __init__(self, factory):
        self.queue = queue.Queue(); self.ready = threading.Event(); self.error = None
        self.thread = threading.Thread(target=self._run, args=(factory,), daemon=True)
        self.thread.start(); need(self.ready.wait(20), "budget supervisor did not start")
        if self.error: raise self.error

    def _run(self, factory):
        try: session = factory()
        except BaseException as exc:
            self.error = exc; self.ready.set(); return
        self.ready.set()
        while True:
            method, args, kwargs, result, done = self.queue.get()
            try: result.append((True, getattr(session, method)(*args, **kwargs)))
            except BaseException as exc: result.append((False, exc))
            done.set()
            if method in {"finish", "abort"}: return

    def call(self, method, *args, **kwargs):
        need(self.thread.is_alive(), "budget supervisor stopped")
        result = []; done = threading.Event()
        self.queue.put((method, args, kwargs, result, done))
        need(done.wait(25), "budget supervisor response timed out")
        if not result[0][0]: raise result[0][1]
        return result[0][1]


class HookGate:
    def __init__(self, budget: BudgetThread, policy: WirePolicy, run_dir: Path, *, tool_scope=None):
        self.budget, self.policy, self.run_dir = budget, policy, run_dir
        self.token = secrets.token_hex(32); self.sequence = 0; self.inflight = None
        self.tool_scope = tool_scope; self.tool_calls = []
        self.unresolved = []
        self.plugin_ready = False
        self.plugin_sha256 = sha256_file(Path(__file__).with_name('budget_guard.js'))
        self.mutex = threading.Lock(); self.settled = []; self.denied = 0; self.denials = []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                try:
                    need(hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + outer.token), "hook authentication failed")
                    length = int(self.headers.get("Content-Length", "0"))
                    need(0 < length <= 32 * 1024 * 1024, "hook payload size limit")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = outer.dispatch(self.path, payload)
                    self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                    self.wfile.write(json_bytes(result))
                except BaseException as exc:
                    outer.denied += 1
                    outer.denials.append(str(exc) if isinstance(exc, WorkerError) else type(exc).__name__)
                    self.send_response(403); self.end_headers(); self.wfile.write(b'{"error":"ADMISSION_DENIED"}')
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    @property
    def url(self): return "http://127.0.0.1:" + str(self.server.server_port)

    def dispatch(self, path, payload):
        with self.mutex:
            if path == '/ready':
                need(payload == {'protocol':2,'plugin_sha256':self.plugin_sha256},'native guard pin mismatch')
                self.plugin_ready=True
                return {'ready':True}
            if path == '/tool':
                need(self.plugin_ready and self.tool_scope is not None,'tool scope not configured')
                need(isinstance(payload,dict) and set(payload)=={'tool','input'},'invalid tool envelope')
                result=self.tool_scope.inspect(payload['tool'],payload['input'])
                self.tool_calls.append(result)
                write_json(self.run_dir / ('tool-'+str(len(self.tool_calls))+'.scope.json'),result,exclusive=True)
                return {'permit':True}
            if path == '/unresolved':
                need(isinstance(payload,dict) and set(payload)=={'attempt_id','http_status','response_sha256','reason','generation_id','error_code','error_message'},'invalid unresolved evidence')
                need(self.inflight is not None and payload['attempt_id']==self.inflight,'unresolved attempt mismatch')
                need(payload['reason'] in {'HTTP_ERROR','NON_JSON_RESPONSE','GENERATION_COST_UNKNOWN'},'unknown unresolved reason')
                need(isinstance(payload['error_message'],str) and len(payload['error_message'])<=2000,'oversized error evidence')
                write_json(self.run_dir/(self.inflight+'.unresolved.json'),payload,exclusive=True)
                self.unresolved.append(payload)
                # Deliberately no settle: POSSIBLY_SENT remains until abort/
                # explicit main-Codex reconciliation, never assume zero cost.
                return {'recorded':True,'accounting':'UNKNOWN_HOLD_REQUIRED'}
            if path == "/before":
                need(self.plugin_ready,'native guard handshake missing')
                need(self.inflight is None, "concurrent request not admitted")
                pins, bound, evidence = self.policy.inspect(payload)
                self.sequence += 1; ident = "http-" + str(self.sequence)
                # This call durably marks POSSIBLY_SENT before returning permit.
                admitted = self.budget.call("before_attempt", ident, str(bound), pins, request_body=payload["body_text"].encode("utf-8"))
                self.inflight = ident
                write_json(self.run_dir / (ident + ".admission.json"), {"id": ident, "pins": pins, "accounting": evidence, "admission": admitted}, exclusive=True)
                return {"permit": True, "attempt_id": ident}
            need(path == "/after" and isinstance(payload, dict), "unknown hook operation")
            need(set(payload) == {"attempt_id", "generation_id", "cost", "response_sha256", "http_status"}, "unexpected response evidence")
            need(payload["attempt_id"] == self.inflight and self.inflight is not None, "attempt response mismatch")
            need(type(payload["http_status"]) is int and 200 <= payload["http_status"] < 300, "provider HTTP failure remains unresolved")
            cost = finite_money(payload["cost"], "generation cost")
            result = self.budget.call("after_attempt", self.inflight, payload["generation_id"], str(cost), payload["response_sha256"], evidence_kind="response_usage")
            write_json(self.run_dir / (self.inflight + ".response.json"), {**payload, "settlement": result}, exclusive=True)
            self.settled.append(self.inflight); self.inflight = None
            return {"recorded": True}

    def close(self, *, clean):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        if clean and self.inflight is None:
            return self.budget.call("finish", no_inflight=True)
        return self.budget.call("abort", "PROCESS_OR_HTTP_OUTCOME_UNRESOLVED")
