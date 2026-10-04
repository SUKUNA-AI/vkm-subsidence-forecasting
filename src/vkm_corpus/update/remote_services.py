"""Native service observer bound to the API's actual serving backends.

Only authenticated identity endpoints are called; no model inference, health
fallback, configurable command, alternate client/URL or expected-JSON proof.
All three services are required by the complete production READ contract.
"""
from __future__ import annotations

import asyncio

from vkm_corpus.update.contracts import ServiceIdentity
from vkm_corpus.update.remote_control import ControlServiceLease, ControlSpec
from vkm_corpus.update.remote_models import NativeModelProof, native_identity_json
from vkm_corpus.update.service_identity import RerankNativeProfile
from vkm_evidence.contracts import record_hash


def _endpoint(client):
    url = client.base_url
    if url.scheme not in {"http", "https"} or not url.host or url.username or url.password or url.query or url.fragment:
        raise ValueError("unsafe native service endpoint")
    return record_hash({"scheme": url.scheme, "host": url.host, "port": url.port, "path": url.path})


def _body(response):
    if response.status_code != 200 or len(response.content) > 262144:
        raise ValueError("native serving identity unavailable")
    body = native_identity_json(response.content)
    if (not isinstance(body, dict) or body.get("status") != "READY"
            or body.get("functional_qualification") != "NOT_RUN"):
        raise ValueError("native identity must not substitute for functional qualification")
    return body


def retrieval_identity(body, endpoint):
    if (set(body) != {"schema", "status", "scope", "code_sha256", "dependencies_sha256", "pack", "models", "functional_qualification"}
            or body["schema"] != "vkm-retrieval-native-identity/1" or body["scope"] != "NATIVE_LOADED_IDENTITY"):
        raise ValueError("unsupported retrieval native identity")
    models, pack = body["models"], body["pack"]
    if not isinstance(models, dict) or set(models) != {"dense", "late", "visual"}:
        raise ValueError("complete production retrieval requires dense, late and visual owned models")
    required = {"role", "model_id", "model_revision", "query_signature", "query_config_sha256",
                "weights_sha256", "tokenizer_sha256", "resources_sha256", "process"}
    for role, model in models.items():
        if not isinstance(model, dict) or set(model) != required or model["role"] != role:
            raise ValueError("incomplete native query model identity")
        process = model["process"]
        if (not isinstance(process, dict) or set(process) != {"pid", "start_ticks", "command_sha256", "executable_signature", "endpoint_sha256"}
                or type(process["pid"]) is not int or process["pid"] <= 0
                or type(process["start_ticks"]) is not int or process["start_ticks"] <= 0):
            raise ValueError("missing native owned process proof")
    if not isinstance(pack, dict) or pack.get("schema") != "vkm-loaded-pack/1":
        raise ValueError("missing loaded pack proof")
    required_pack = {"schema", "pack_id", "snapshot_id", "manifest_sha256", "config_signature", "config_sha256",
                     "files", "count", "total_tokens"}
    if (set(pack) != required_pack or not pack["pack_id"] or not pack["snapshot_id"]
            or set(pack["files"]) != {"tokens.f16", "index.parquet"}
            or type(pack["count"]) is not int or pack["count"] <= 0
            or type(pack["total_tokens"]) is not int or pack["total_tokens"] < pack["count"]):
        raise ValueError("incomplete loaded pack inventory")
    from pydantic import TypeAdapter
    from vkm_evidence.contracts import Sha256
    sha = TypeAdapter(Sha256)
    for digest in (pack["manifest_sha256"], pack["config_sha256"], pack["config_signature"], *pack["files"].values()):
        sha.validate_python(digest)
    resources = {"LATE_PACK": pack["manifest_sha256"]}
    for role, model in models.items():
        for name in ("weights_sha256", "tokenizer_sha256", "resources_sha256", "query_config_sha256"):
            resources[role + "_" + name.removesuffix("_sha256")] = model[name]
    return ServiceIdentity(service="RETRIEVAL",
        instance_sha256=record_hash({k: v["process"] for k, v in models.items()}),
        code_sha256=body["code_sha256"], dependencies_sha256=body["dependencies_sha256"],
        config_sha256=record_hash({k: v["query_config_sha256"] for k, v in models.items()}),
        endpoint_sha256=endpoint, runtime_sha256=record_hash({"pack": pack, "models": models}),
        resources=resources, capabilities=("dense_query", "late_query", "late_scores", "visual_query"))


def rerank_identity(body, endpoint, *, text_backend=None, retrieval: ServiceIdentity | None = None):
    fields = {"schema", "status", "instance_sha256", "code_sha256", "dependencies_sha256", "config_sha256",
              "resources", "models", "functional_qualification"}
    optional = isinstance(body, dict) and body.get("schema") == "vkm-rerank-native-identity/2"
    kinds = ("visual",) if optional else ("text", "visual")
    resources_required = {"visual_tokenizer", "visual_head"} | (set() if optional else {"text_tokenizer"})
    if (not isinstance(body, dict) or set(body) != fields | ({"profile", "text_route"} if optional else set())
            or body["status"] != "READY" or body["functional_qualification"] != "NOT_RUN"
            or body["schema"] != ("vkm-rerank-native-identity/2" if optional else "vkm-rerank-native-identity/1")
            or not isinstance(body["models"], dict) or set(body["models"]) != set(kinds)
            or not isinstance(body["resources"], dict) or set(body["resources"]) != resources_required):
        raise ValueError("incomplete native rerank gateway proof")
    fallback = None
    if optional:
        if (body["profile"] != RerankNativeProfile.VISUAL_ONLY_TEXT_DISABLED_V2.value
                or body["text_route"] != "DISABLED_UNAVAILABLE" or text_backend != "late"
                or type(retrieval) is not ServiceIdentity or retrieval.service != "RETRIEVAL"
                or "late_scores" not in retrieval.capabilities
                or not {"LATE_PACK", "late_weights", "late_tokenizer", "late_resources", "late_query_config"} <= retrieval.resources.keys()):
            raise ValueError("disabled text requires actual API late route and same qualified retrieval")
        fallback = {"selected_text_backend": text_backend, "retrieval": retrieval.model_dump(mode="json")}
    resources = dict(body["resources"])
    for kind in kinds:
        proof = NativeModelProof.model_validate(body["models"][kind])
        if proof.kind != kind:
            raise ValueError("native downstream model differs from gateway route")
        if kind == "text" and (proof.tokenizer_binding != "STANDALONE_LOADED"
                or proof.resources["tokenizer"] != resources["text_tokenizer"]):
            raise ValueError("native text tokenizer differs from gateway route")
        if kind == "visual" and proof.tokenizer_binding != "EMBEDDED_WEIGHTS_VOCAB":
            raise ValueError("native downstream model differs from gateway route")
        resources.update({kind + "_model_" + k: v for k, v in proof.resources.items()})
    if fallback is not None:
        resources["text_fallback_late"] = record_hash(fallback)
    return ServiceIdentity(service="RERANK", instance_sha256=body["instance_sha256"],
        code_sha256=body["code_sha256"], dependencies_sha256=body["dependencies_sha256"],
        config_sha256=body["config_sha256"], endpoint_sha256=endpoint,
        runtime_sha256=record_hash({"models": body["models"], "profile": body["profile"], "text_fallback": fallback})
            if optional else record_hash(body["models"]), resources=resources,
        capabilities=("rerank_visual",) if optional else ("rerank_text", "rerank_visual"))


class NativeServiceObserver:
    """Bind once before admission; every observe uses those actual backends."""
    @classmethod
    async def bind(cls, deps, *, control_spec: ControlSpec, api_service=None):
        from vkm_corpus.api.backends import GatewayRerankBackend, HybridBackend, PgControlPlane
        from vkm_corpus.api.service import ApiService
        if (not isinstance(deps.rerank, GatewayRerankBackend) or not isinstance(deps.hybrid, HybridBackend)
                or not isinstance(deps.control, PgControlPlane)):
            raise ValueError("complete production service identity requires actual rerank/retrieval/control backends")
        obj = cls()
        obj.deps = deps
        # Optional-text qualification needs the actual serving ApiService, never
        # an expected route, a new receiver, an injected callback or an env claim.
        if api_service is not None and (type(api_service) is not ApiService or api_service.deps is not deps):
            raise ValueError("actual API service/dependencies required for native route binding")
        obj.api_service = api_service
        obj.api_selector = ApiService.text_rerank_backend
        obj.text_backend = obj._api_text_backend()
        obj.backends = (deps.rerank, deps.hybrid, deps.control)
        obj.rerank = deps.rerank._client
        obj.embed = deps.hybrid._embed
        obj.clients = (obj.rerank._http, obj.embed._http)
        if any(c is None for c in obj.clients):
            raise ValueError("actual service HTTP clients unavailable")
        if not obj.rerank.token or not obj.embed._http.headers.get("Authorization", "").startswith("Bearer "):
            raise ValueError("authenticated native serving clients required")
        obj.endpoints = tuple(_endpoint(c) for c in obj.clients)
        obj.control = await asyncio.to_thread(ControlServiceLease, deps.control, control_spec)
        obj.identities = await obj._observe()
        return obj

    def _api_text_backend(self):
        if self.api_service is None:
            return None  # Strict v1 remains valid; v2 rejects absence below.
        from vkm_corpus.api.service import ApiService
        method = self.api_service.text_rerank_backend
        if (type(self.api_service) is not ApiService or self.api_service.deps is not self.deps
                or ApiService.text_rerank_backend is not self.api_selector
                or getattr(method, "__self__", None) is not self.api_service
                or getattr(method, "__func__", None) is not self.api_selector):
            raise ValueError("actual API text selector replaced")
        return method()

    def _fence(self):
        actual = (self.deps.rerank, self.deps.hybrid, self.deps.control)
        if (any(a is not b for a, b in zip(actual, self.backends))
                or self.deps.rerank._client is not self.rerank or self.deps.hybrid._embed is not self.embed
                or self.rerank._http is not self.clients[0] or self.embed._http is not self.clients[1]
                or tuple(_endpoint(c) for c in self.clients) != self.endpoints
                or self._api_text_backend() != self.text_backend):
            raise ValueError("actual serving backend/client/endpoint replaced")

    async def _observe(self):
        self._fence()
        import httpx
        def read_retrieval():
            return self.embed._http.get("/identity", headers={"Cache-Control": "no-cache"}, timeout=5.0)
        try:
            rerank = await self.rerank._http.get("/identity", headers={"X-VKM-Rerank-Token": self.rerank.token,
                "Cache-Control": "no-cache"}, timeout=5.0)
            retrieval = await asyncio.to_thread(read_retrieval)
        except httpx.HTTPError as exc:
            raise ValueError("native serving identity transport unavailable") from exc
        control = await asyncio.to_thread(self.control.observe)
        retrieval_body = _body(retrieval)
        retrieval_proof = retrieval_identity(retrieval_body, self.endpoints[1])
        identities = (rerank_identity(_body(rerank), self.endpoints[0], text_backend=self.text_backend,
                                     retrieval=retrieval_proof), retrieval_proof, control)
        self._fence()
        self.pack_proof = retrieval_body["pack"]
        return {i.service: i.model_dump(mode="json") for i in identities}

    async def observe(self):
        actual = await self._observe()
        if actual != self.identities:
            raise ValueError("qualified native service identity changed")
        return actual


async def observe_services(deps, *, control_spec: ControlSpec, api_service=None):
    """Factory: bind expensive proofs once, return an async native observer."""
    return await NativeServiceObserver.bind(deps, control_spec=control_spec, api_service=api_service)
