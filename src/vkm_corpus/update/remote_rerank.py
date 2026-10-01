"""Qualified gateway identity, including both actual downstream model clients."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.remote_models import own_process, read_backend_model_identity
from vkm_corpus.update.remote_retrieval import file_signature
from vkm_evidence.contracts import record_hash


def config_identity(cfg):
    values = asdict(cfg)
    for key in ("token", "text_native_token", "visual_native_token"):
        values[key] = bool(values.get(key))
    return record_hash(values)


class RerankServiceLease:
    @classmethod
    async def bind(cls, cfg, res):
        from vkm_corpus.update.service_identity import (RERANK, require_rerank_profile,
            service_code_identity, service_dependencies_identity)
        if not cfg.auth_required or not cfg.token or not cfg.qualified_identity:
            raise ValueError("qualified rerank identity requires explicit authenticated profile")
        captures = getattr(res, "load_files", None)
        if not captures:
            raise ValueError("gateway load-time resource identity unavailable")
        if res.load_watch is None:
            raise ValueError("gateway load-time mutation watch unavailable")
        require_rerank_profile(res)
        obj = cls()
        obj.cfg, obj.res, obj.captures = cfg, res, captures
        obj.objects = (res.text, res.visual, res.v35_tokens, res.m0_tokens, res.head)
        obj.clients = (res.text.client(), res.visual.client())
        obj.process, obj.config = own_process(), config_identity(cfg)
        obj.code, obj.dependencies = service_code_identity(RERANK), service_dependencies_identity(RERANK)
        obj._local_fence()
        hashes = {p: sha256_of(Path(p)) for p in captures}
        obj.resources = {"text_tokenizer": hashes[str(Path(cfg.v35_tokenizer).absolute())],
                         "visual_tokenizer": hashes[str(Path(cfg.m0_tokenizer).absolute())],
                         "visual_head": hashes[str(Path(cfg.m0_head).absolute())]}
        if obj.resources != {"text_tokenizer": res.v35_tokens.sha256,
                             "visual_tokenizer": res.m0_tokens.sha256, "visual_head": res.head.sha256}:
            raise ValueError("gateway loaded objects differ from native resource bytes")
        obj.models = await obj._models()
        obj._local_fence()
        return obj

    def _local_fence(self):
        from vkm_corpus.update.service_identity import (RERANK, require_rerank_profile,
            service_code_identity, service_dependencies_identity)
        require_rerank_profile(self.res)
        self.res.load_watch.check()
        objects = (self.res.text, self.res.visual, self.res.v35_tokens, self.res.m0_tokens, self.res.head)
        if (any(a is not b for a, b in zip(objects, self.objects))
                or any(file_signature(p) != sig for p, sig in self.captures.items())
                or own_process() != self.process or config_identity(self.cfg) != self.config
                or service_code_identity(RERANK) != self.code or service_dependencies_identity(RERANK) != self.dependencies
                or self.res.text.client() is not self.clients[0] or self.res.visual.client() is not self.clients[1]):
            raise ValueError("qualified rerank gateway instance/resources/configuration changed")
        import httpx
        if (str(self.clients[0].base_url).rstrip('/') != str(httpx.URL(self.cfg.text_url)).rstrip('/')
                or str(self.clients[1].base_url).rstrip('/') != str(httpx.URL(self.cfg.visual_url)).rstrip('/')):
            raise ValueError("native model clients differ from actual configured inference endpoints")

    async def _models(self):
        text = await read_backend_model_identity(self.res.text, "text", self.cfg.text_native_token)
        visual = await read_backend_model_identity(self.res.visual, "visual", self.cfg.visual_native_token)
        from vkm_corpus.retrieval import pins
        if (text.resources["weights"] != pins.TEXT["weights_sha256"]
                or text.resources["tokenizer"] != self.res.v35_tokens.sha256
                or visual.resources["weights"] != self.cfg.m0_weights_sha256
                or visual.resources["mmproj"] != self.cfg.m0_mmproj_sha256
                or visual.resources["tokenizer"] != self.res.m0_tokens.sha256):
            raise ValueError("native loaded rerank model differs from actual gateway inputs")
        return {"text": text.model_dump(mode="json"), "visual": visual.model_dump(mode="json")}

    async def observe(self):
        self._local_fence()
        models = await self._models()
        if models != self.models:
            raise ValueError("qualified downstream rerank model changed")
        self._local_fence()
        return {"schema": "vkm-rerank-native-identity/1", "status": "READY", "instance_sha256": record_hash(self.process),
            "code_sha256": self.code, "dependencies_sha256": self.dependencies,
            "config_sha256": self.config, "resources": dict(self.resources), "models": models,
            "functional_qualification": "NOT_RUN"}
