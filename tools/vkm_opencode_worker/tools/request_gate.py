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
import re
import secrets
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from vkm_worker_core import WorkerError, need, finite_money, json_bytes, sha256_file, write_json, parse_json, read_json, verify_file_record
from shared_budget import BudgetError
import denied_telemetry

PROFILE_NAMES = {"deepseek": "cheap_code", "kimi": "engineering_research",
                 "hydrogen": "vision_documents", "opus": "critical_review"}
INCLUSIVE_CAP_PROOF_SHA256 = "9fa36159effa948b24ad6fdd191625a6319c210dc58c31248e262999d29a9553"

KINDS = {"primary": "main", "title": "title", "compaction": "compaction", "generate": "summary"}


class WirePolicy:
    def __init__(self, profile: dict, endpoint: dict, *, target_url="https://openrouter.ai/api/v1/chat/completions", image_policy=None, output_rate_policy=None):
        self.profile = profile
        self.endpoint = endpoint
        self.target_url = target_url
        self.image_policy = image_policy
        self.output_rate_policy = None
        if output_rate_policy is not None:
            policy = read_json(verify_file_record(output_rate_policy))
            need(isinstance(policy, dict) and set(policy) == {'schema', 'status', 'model_id', 'provider_only', 'inclusive_output_cap', 'cap_fields', 'pricing_condition', 'proof'}, 'invalid output rate opt-in')
            need(policy['schema'] == 'vkm.opencode.output-cap-accounting/1' and policy['status'] == 'APPROVED_BY_MAIN_CODEX', 'output rate policy awaits main review')
            need(profile['model_id'] == policy['model_id'] == 'google/gemini-4-hydrogen' and profile['provider_only'] == policy['provider_only'] == ['google-ai-studio/flex'] and endpoint.get('tag') == 'google-ai-studio/flex', 'inclusive cap endpoint binding differs')
            need(policy['inclusive_output_cap'] is True and policy['cap_fields'] == ['max_tokens', 'max_completion_tokens'] and policy['pricing_condition'] == 'FRESH_COMPLETION_EQUALS_INTERNAL_REASONING', 'unproved output cap semantics')
            need(isinstance(policy['proof'], dict) and policy['proof'].get('sha256') == INCLUSIVE_CAP_PROOF_SHA256, 'output cap derivation proof differs')
            verify_file_record(policy['proof'])
            self.output_rate_policy = policy

    def inspect(self, payload: dict) -> tuple[dict, Decimal, dict]:
        need(set(payload) == {"body_text", "url", "kind"}, "unexpected hook request fields")
        need(payload["url"] == self.target_url, "provider URL differs from pinned endpoint")
        need(payload["kind"] in KINDS, "unknown native request kind")
        need(isinstance(payload["body_text"], str), "captured body text missing")
        raw = payload["body_text"].encode("utf-8")
        body = parse_json(payload["body_text"])
        need(isinstance(body, dict), "request body missing")
        p = self.profile
        need(p.get('model_id') != 'moonshotai/kimi-k3.1-pro', 'Kimi API revoked by owner; use subscription workflow')
        need(body.get("model") == p["model_id"], "request model differs from selected model")
        provider = body.get("provider")
        need(provider == {"only": p["provider_only"], "allow_fallbacks": False, "require_parameters": True}, "request provider policy differs")
        reason = body.get("reasoning")
        need(reason == {"enabled": True, "effort": p["reasoning_effort"], "exclude": True}, "request reasoning differs")
        limits = [body[k] for k in ("max_tokens", "max_completion_tokens", "max_output_tokens") if k in body]
        need(len(limits) == 1 and type(limits[0]) is int and 0 < limits[0] <= p["max_output_tokens"], "missing/ambiguous/oversized output cap")
        output_cap = limits[0]
        forbidden = sorted(set(body) & {"models", "route", "transforms", "plugins", "web_search_options", "image_config", "audio"})
        need(not forbidden, "unqualified body routing/extension: " + ",".join(forbidden))
        need(body.get('modalities',['text'])==['text'],'unqualified nontext output modality')
        # Bound text and tool schemas together. Multimodal inputs require a
        # separately qualified image/file accounting policy; never price as text.
        messages = body.get("messages")
        need(isinstance(messages, list) and messages, "missing messages")
        image_evidence=None
        if self.image_policy is not None:
            input_bound,image_evidence=self.image_policy.body_bound(body)
        else:
            for message in messages:
                need(isinstance(message, dict), "invalid message")
                content = message.get("content")
                if isinstance(content, list):
                    need(all(isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str) for x in content), "unqualified nontext accounting")
                else:
                    need(content is None or isinstance(content, str), "unqualified content")
            input_bound=len(raw)+4096
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
        completion_rate = finite_money(pricing.get("completion"), "output tariff")
        reasoning_rate = finite_money(pricing.get("internal_reasoning", 0), "reasoning tariff")
        output_rate = completion_rate + reasoning_rate
        output_evidence = {'method': 'SUM_UNVERIFIED_OUTPUT_CLASS_QUOTAS',
                           'completion_rate': str(completion_rate), 'reasoning_rate': str(reasoning_rate)}
        if self.output_rate_policy is not None:
            need('internal_reasoning' in pricing and completion_rate == reasoning_rate, 'inclusive cap equal fresh rates condition failed')
            cap_field = next(k for k in ['max_tokens', 'max_completion_tokens', 'max_output_tokens'] if k in body)
            need(cap_field in self.output_rate_policy['cap_fields'], 'unproved inclusive output cap field')
            output_rate = max(completion_rate, reasoning_rate)
            output_evidence.update(method='MAX_DISJOINT_VISIBLE_REASONING_ONE_INCLUSIVE_CAP',
                                   formula='V>=0,R>=0,V+R<=N => V*pc+R*pr<=N*max(pc,pr)',
                                   cap_field=cap_field, inclusive_output_cap=True,
                                   proof=self.output_rate_policy['proof'],
                                   actual_provider_partition='NOT_ASSUMED_OR_INFERRED',
                                   allowance='UNCHANGED_125_PERCENT_AND_CACHE_MAX')
        need(input_bound + output_cap <= ep["context_length"], "endpoint context bound exceeded")
        image_count=image_evidence['image_count'] if image_evidence else 0
        image_fee=finite_money(pricing.get('image',0),'image tariff')*image_count
        estimate = (prompt_rate * input_bound + output_rate * output_cap + finite_money(pricing.get("request", 0)) + image_fee) * Decimal("1.25")
        evidence = {"method": "EXACT_CAPTURED_BODY_UTF8_BYTES_PLUS_4096_PROTOCOL_GUARD_125_PERCENT",
                    "quantity_status": "CONSERVATIVE_ESTIMATE_NOT_PROVIDER_TOKENIZATION",
                    "request_bytes": len(raw), "input_bound": input_bound,
                    "max_tokens": output_cap, "input_rate_bound": str(prompt_rate),
                    "output_rate_bound": str(output_rate), "output_rate_accounting": output_evidence, "upper_usd": str(estimate)}
        if image_evidence:
            evidence.update(method='APPROVED_IMAGE_NATIVE_MAX_PLUS_CANONICAL_TEXT_BYTES_PROTOCOL_GUARDS_125_PERCENT',images=image_evidence,image_fee_bound_usd=str(image_fee))
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
        self.denied_sequence = 0
        self.denial_lock = threading.Lock()
        self._denial_ctx = threading.local()
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                ctx = outer._denial_ctx
                ctx.state = None
                try:
                    need(hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + outer.token), "hook authentication failed")
                    length = int(self.headers.get("Content-Length", "0"))
                    need(0 < length <= 32 * 1024 * 1024, "hook payload size limit")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = outer.dispatch(self.path, payload)
                    self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                    self.wfile.write(json_bytes(result))
                except BaseException as exc:
                    # dispatch() records its own phase exactly once. Only failures
                    # before dispatch (auth/size/JSON) are recorded here. Never
                    # persist exception text, args, headers, token or control body.
                    if getattr(ctx, "state", None) is None:
                        outer._record_denial("HOOK_HANDLER", outer._denial_reason(exc))
                    outer.denied += 1
                    outer.denials.append(outer._denial_reason(exc))
                    self.send_response(403); self.end_headers(); self.wfile.write(b'{"error":"ADMISSION_DENIED"}')
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    @property
    def url(self): return "http://127.0.0.1:" + str(self.server.server_port)

    def _denial_reason(self, exc):
        """Classify only exact library exception types; never str/repr/args text.

        Anything not an exact WorkerError static message or an exact allowlisted
        BudgetError code is UNCLASSIFIED. No prefix/regex normalization.
        """
        try:
            if type(exc) is WorkerError:
                return denied_telemetry.worker_reason(exc.args)
            if type(exc) is BudgetError:
                return denied_telemetry.budget_reason(exc.code)
        except Exception:
            return 'UNCLASSIFIED'
        return 'UNCLASSIFIED'

    @staticmethod
    def _denial_body(payload):
        """Trusted /before body_text only; never parse or store body content."""
        if isinstance(payload, dict):
            text = payload.get('body_text')
            if type(text) is str:
                try:
                    return text.encode('utf-8')
                except UnicodeError:
                    return None
        return None

    @staticmethod
    def _denial_metrics(evidence, bound):
        """Bounded numeric evidence already computed by the wire inspection."""
        metrics = {}
        if isinstance(evidence, dict):
            for source, name in (('input_bound', 'input_bound'), ('max_tokens', 'output_cap')):
                value = evidence.get(source)
                if type(value) is int and 0 <= value <= 2 ** 63 - 1:
                    metrics[name] = value
        try:
            metrics['estimated_upper_usd'] = denied_telemetry.money_text(bound)
        except Exception:
            pass
        return metrics

    def _endpoint_metrics(self):
        """Trusted endpoint config only, for refusals raised before evidence exists."""
        metrics = {}
        try:
            endpoint = getattr(self.policy, 'endpoint', None)
            value = endpoint.get('context_length') if isinstance(endpoint, dict) else None
        except Exception:
            return {}
        if type(value) is int and 0 <= value <= 2 ** 63 - 1:
            metrics['context_limit'] = value
        return metrics

    def _record_denial(self, stage, reason_code, *, body_bytes=None, metrics=None):
        """Persist one bounded record; failure is silent and never changes refusal."""
        try:
            record = denied_telemetry.build_record(stage=stage, reason_code=reason_code,
                                                   body_bytes=body_bytes, metrics=metrics)
        except Exception:
            return False
        with self.denial_lock:
            self.denied_sequence += 1
            sequence = self.denied_sequence
        try:
            write_json(self.run_dir / ("denied-" + str(sequence) + ".json"), record, exclusive=True)
        except Exception:
            return False
        return True

    def dispatch(self, path, payload):
        """Record one denial per logical refusal, then re-raise the original error."""
        ctx = self._denial_ctx
        ctx.state = 'PENDING'; ctx.stage = 'DISPATCH'; ctx.body = None; ctx.metrics = None
        try:
            result = self._dispatch(path, payload)
        except BaseException as exc:
            if ctx.state == 'PENDING':
                stage = ctx.stage if ctx.stage in denied_telemetry.STAGES else 'UNCLASSIFIED'
                ok = self._record_denial(stage, self._denial_reason(exc),
                                         body_bytes=ctx.body, metrics=ctx.metrics)
                ctx.state = 'RECORDED' if ok else 'FAILED'
            raise
        ctx.state = 'OK'
        return result

    def _dispatch(self, path, payload):
        ctx = self._denial_ctx
        with self.mutex:
            if path == '/ready':
                need(payload == {'protocol':2,'plugin_sha256':self.plugin_sha256},'native guard pin mismatch')
                self.plugin_ready=True
                return {'ready':True}
            if path == '/tool':
                ctx.stage = 'TOOL_SCOPE'
                need(self.plugin_ready and self.tool_scope is not None,'tool scope not configured')
                need(isinstance(payload,dict) and set(payload)=={'tool','input'},'invalid tool envelope')
                result=self.tool_scope.inspect(payload['tool'],payload['input'])
                self.tool_calls.append(result)
                write_json(self.run_dir / ('tool-'+str(len(self.tool_calls))+'.scope.json'),result,exclusive=True)
                return {'permit':True}
            if path == '/unresolved':
                ctx.stage = 'DISPATCH_AFTER'
                need(isinstance(payload,dict) and set(payload)=={'attempt_id','http_status','response_sha256','reason','generation_id','error_code','error_message'},'invalid unresolved evidence')
                need(self.inflight is not None and payload['attempt_id']==self.inflight,'unresolved attempt mismatch')
                need(payload['reason'] in {'HTTP_ERROR','NON_JSON_RESPONSE','GENERATION_COST_UNKNOWN'},'unknown unresolved reason')
                need(isinstance(payload['error_message'],str) and len(payload['error_message'])<=2000,'oversized error evidence')
                write_json(self.run_dir/(self.inflight+'.unresolved.json'),payload,exclusive=True)
                self.unresolved.append(payload)
                # Deliberately no settle: POSSIBLY_SENT remains until abort/
                # explicit main-Codex reconciliation, never assume zero cost.
                return {'recorded':True,'accounting':'UNKNOWN_HOLD_REQUIRED'}
            if path == '/response-metadata':
                ctx.stage = 'DISPATCH_AFTER'
                need(isinstance(payload,dict) and set(payload)=={'attempt_id','http_status','response_sha256','content_type','headers'},'invalid transport metadata')
                need(self.inflight is not None and payload['attempt_id']==self.inflight,'transport attempt mismatch')
                need(type(payload['http_status']) is int and 100<=payload['http_status']<=599,'invalid transport status')
                need(isinstance(payload['response_sha256'],str) and re.fullmatch('[a-f0-9]{64}',payload['response_sha256']),'invalid transport digest')
                need(isinstance(payload['content_type'],str) and re.fullmatch('[A-Za-z0-9.+/-]{0,128}',payload['content_type']),'invalid content type')
                headers=payload['headers']
                need(isinstance(headers,dict) and set(headers)<={'x-request-id','x-generation-id','x-openrouter-request-id','cf-ray'},'unapproved transport headers')
                need(all(isinstance(v,str) and re.fullmatch('[A-Za-z0-9._:/-]{1,256}',v) for v in headers.values()),'invalid transport identifier')
                write_json(self.run_dir/(self.inflight+'.transport.json'),{**payload,'billing_status':'IDENTIFIERS_ONLY_NOT_SETTLEMENT'},exclusive=True)
                return {'recorded':True,'accounting':'UNCHANGED'}
            if path == "/before":
                ctx.stage = 'WIRE_POLICY'
                ctx.body = self._denial_body(payload)
                need(self.plugin_ready,'native guard handshake missing')
                need(self.inflight is None, "concurrent request not admitted")
                pins, bound, evidence = self.policy.inspect(payload)
                ctx.metrics = self._denial_metrics(evidence, bound)
                ctx.stage = 'BUDGET_BEFORE'
                self.sequence += 1; ident = "http-" + str(self.sequence)
                # This call durably marks POSSIBLY_SENT before returning permit.
                admitted = self.budget.call("before_attempt", ident, str(bound), pins, request_body=payload["body_text"].encode("utf-8"))
                # Ledger state may already have changed; no denial is claimed past
                # this point, so a later file error cannot look like "not admitted".
                ctx.state = 'ADMITTED'
                self.inflight = ident
                write_json(self.run_dir / (ident + ".admission.json"), {"id": ident, "pins": pins, "accounting": evidence, "admission": admitted}, exclusive=True)
                return {"permit": True, "attempt_id": ident}
            ctx.stage = 'DISPATCH_AFTER'
            need(path == "/after" and isinstance(payload, dict), "unknown hook operation")
            need(set(payload) == {"attempt_id", "generation_id", "cost", "response_sha256", "http_status"}, "unexpected response evidence")
            need(payload["attempt_id"] == self.inflight and self.inflight is not None, "attempt response mismatch")
            need(type(payload["http_status"]) is int and 200 <= payload["http_status"] < 300, "provider HTTP failure remains unresolved")
            cost = finite_money(payload["cost"], "generation cost")
            result = self.budget.call("after_attempt", self.inflight, payload["generation_id"], str(cost), payload["response_sha256"], evidence_kind="response_usage")
            ctx.state = 'ADMITTED'
            write_json(self.run_dir / (self.inflight + ".response.json"), {**payload, "settlement": result}, exclusive=True)
            self.settled.append(self.inflight); self.inflight = None
            return {"recorded": True}

    def close(self, *, clean):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        if clean and self.inflight is None:
            return self.budget.call("finish", no_inflight=True)
        return self.budget.call("abort", "PROCESS_OR_HTTP_OUTCOME_UNRESOLVED")
