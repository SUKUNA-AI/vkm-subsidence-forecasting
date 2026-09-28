"""``vkm-rerank-gateway`` — the only entry point of the EDGE rerankers for VKM (CP-18, H-13, H-41, H-45).

FastAPI application (created by :func:`create_app`) with:

* ``POST /v1/rerank/text`` → the existing v3.5 service (never modified): count check (≤ 24 → else 413), exact
  listwise-prompt token count with the service's ``tokenizer.json`` (≤ 4096 → else 413), then ``POST /v1/rerank`` with
  ``token_budget = 4096`` and ``expected_runtime`` from ``/readyz`` (the service re-checks the budget itself);
* ``POST /v1/rerank/visual`` → pinned llama-server with jina-reranker-m0: ≤ 8 images (else 413), strict decoding and
  normalisation (:mod:`images`), last-token hidden states from ``/embedding``, score head in the gateway;
* ``GET /health`` (liveness + backend states, no auth) and ``GET /status`` (roles, pins, limits; token required;
  never host addresses).

Every request needs ``X-VKM-Rerank-Token`` (compared in constant time); the token comes from
``VKM_RERANK_TOKEN_FILE`` via :func:`vkm_corpus.config.load_settings`. Admission control is per backend
(:class:`backends.BackendGate`); there is no lock shared by text and visual. One JSON log line per request; texts and
image bytes are never logged. FastAPI/uvicorn/numpy/Pillow/tokenizers are imported lazily.

No ``from __future__ import annotations`` here: the route functions are defined inside :func:`create_app` next to the
lazily imported ``fastapi.Request``, and FastAPI must see the evaluated annotation, not a string.
"""
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vkm_corpus.config import load_settings
from vkm_corpus.logs import get_logger
from vkm_corpus.retrieval import models as M
from vkm_corpus.retrieval import pins
from vkm_corpus.retrieval.backends import BackendGate, RerankError, TextBackend, VisualBackend

GATEWAY_VERSION = "0.1.0"
LOG = get_logger("rerank.gateway")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8")).hexdigest()


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class GatewayConfig:
    """Gateway settings from ``VKM_RERANK_*`` variables (secrets only through ``load_settings``)."""

    text_url: str = "http://127.0.0.1:18082"
    visual_url: str = "http://127.0.0.1:18083"
    v35_tokenizer: str | None = None
    v35_tokenizer_sha256: str = pins.TEXT["tokenizer_sha256"]
    m0_tokenizer: str | None = None
    m0_tokenizer_sha256: str | None = pins.VISUAL["tokenizer_sha256"]
    m0_head: str | None = None
    m0_head_sha256: str = pins.VISUAL["head_sha256"]
    m0_ngl: int = 20
    m0_layers: int = pins.VISUAL["n_layers"]
    m0_quant: str = pins.VISUAL["quant"]
    m0_weights_sha256: str = pins.VISUAL["weights_sha256"]
    m0_mmproj_sha256: str = pins.VISUAL["mmproj_sha256"]
    m0_deviation_note: str | None = None
    llama_cpp_commit: str = pins.LLAMA_CPP_COMMIT
    llama_image: str | None = None
    text_max_inflight: int = 0          # H-41: the service serialises inference itself → no gateway limit
    text_max_queue: int = 0
    visual_max_inflight: int = 1        # = llama-server -np 1
    visual_max_queue: int = 8
    image_max_side: int = M.IMAGE_MAX_SIDE_PX
    text_token_budget: int = M.TEXT_TOKEN_BUDGET
    auth_required: bool = True
    warmup: bool = True                 # max-size visual warmup after every (re)start of llama-server
    warmup_interval_s: float = 30.0
    token: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GatewayConfig":
        env = os.environ if env is None else env
        settings = load_settings(env)
        auth = env.get("VKM_RERANK_AUTH", "required").strip().lower() or "required"
        if auth not in ("required", "disabled"):
            raise ValueError("VKM_RERANK_AUTH must be 'required' or 'disabled'")
        return cls(
            text_url=env.get("VKM_RERANK_TEXT_URL", cls.text_url).strip() or cls.text_url,
            visual_url=env.get("VKM_RERANK_VISUAL_URL", cls.visual_url).strip() or cls.visual_url,
            v35_tokenizer=env.get("VKM_RERANK_V35_TOKENIZER", "").strip() or None,
            m0_tokenizer=env.get("VKM_RERANK_M0_TOKENIZER", "").strip() or None,
            m0_tokenizer_sha256=env.get("VKM_RERANK_M0_TOKENIZER_SHA256", "").strip() or cls.m0_tokenizer_sha256,
            m0_head=env.get("VKM_RERANK_M0_HEAD", "").strip() or None,
            m0_ngl=_env_int(env, "VKM_RERANK_M0_NGL", cls.m0_ngl),
            m0_quant=env.get("VKM_RERANK_M0_QUANT", "").strip() or cls.m0_quant,
            m0_weights_sha256=env.get("VKM_RERANK_M0_WEIGHTS_SHA256", "").strip() or cls.m0_weights_sha256,
            m0_mmproj_sha256=env.get("VKM_RERANK_M0_MMPROJ_SHA256", "").strip() or cls.m0_mmproj_sha256,
            m0_deviation_note=env.get("VKM_RERANK_M0_DEVIATION", "").strip() or None,
            llama_image=env.get("VKM_RERANK_LLAMA_IMAGE", "").strip() or None,
            text_max_inflight=_env_int(env, "VKM_RERANK_TEXT_MAX_INFLIGHT", 0),
            text_max_queue=_env_int(env, "VKM_RERANK_TEXT_MAX_QUEUE", 0),
            visual_max_inflight=_env_int(env, "VKM_RERANK_VISUAL_MAX_INFLIGHT", 1),
            visual_max_queue=_env_int(env, "VKM_RERANK_VISUAL_MAX_QUEUE", 8),
            image_max_side=_env_int(env, "VKM_RERANK_IMAGE_MAX_SIDE", M.IMAGE_MAX_SIDE_PX),
            auth_required=auth == "required",
            warmup=env.get("VKM_RERANK_WARMUP", "1").strip() not in ("0", "false", "no"),
            token=settings.rerank_token,
        )

    @property
    def visual_placement(self) -> str:
        return f"mmproj=GPU; llm_layers_gpu={self.m0_ngl}/{self.m0_layers}; output=CPU"

    def text_model_config(self, runtime: dict[str, str] | None) -> dict[str, Any]:
        return {"kind": "text", "runtime": runtime or {}, "weights_sha256": pins.TEXT["weights_sha256"],
                "tokenizer_sha256": self.v35_tokenizer_sha256, "token_budget": self.text_token_budget,
                "placement": pins.TEXT["placement"], "max_candidates": M.MAX_TEXT_CANDIDATES}

    def visual_model_config(self) -> dict[str, Any]:
        return {"kind": "visual", "model_id": pins.VISUAL["model_id"], "revision": pins.VISUAL["model_revision"],
                "weights_sha256": self.m0_weights_sha256, "mmproj_sha256": self.m0_mmproj_sha256,
                "head_sha256": self.m0_head_sha256, "quant": self.m0_quant, "placement": self.visual_placement,
                "llama_cpp_commit": self.llama_cpp_commit, "llama_image": self.llama_image,
                "image_max_side": self.image_max_side, "min_pixels": M.M0_MIN_PIXELS, "max_pixels": M.M0_MAX_PIXELS}


@dataclass
class Resources:
    """Loaded at startup; injectable in tests."""

    text: Any
    visual: Any
    v35_tokens: Any = None          # tokens.TokenCounter
    m0_tokens: Any = None           # tokens.TokenCounter
    head: Any = None                # m0_head.M0Head
    gates: dict[str, BackendGate] = field(default_factory=dict)
    started_at: str = field(default_factory=_now)


def load_resources(cfg: GatewayConfig) -> Resources:
    from vkm_corpus.retrieval.m0_head import M0Head
    from vkm_corpus.retrieval.tokens import TokenCounter

    missing = [n for n, v in (("VKM_RERANK_V35_TOKENIZER", cfg.v35_tokenizer),
                              ("VKM_RERANK_M0_TOKENIZER", cfg.m0_tokenizer), ("VKM_RERANK_M0_HEAD", cfg.m0_head)) if not v]
    if missing:
        raise ValueError(f"gateway configuration incomplete: {', '.join(missing)}")
    return Resources(
        text=TextBackend(cfg.text_url), visual=VisualBackend(cfg.visual_url),
        v35_tokens=TokenCounter.from_file(cfg.v35_tokenizer, cfg.v35_tokenizer_sha256),
        m0_tokens=TokenCounter.from_file(cfg.m0_tokenizer, cfg.m0_tokenizer_sha256),
        head=M0Head.from_npz(cfg.m0_head, cfg.m0_head_sha256),
    )


def _gates(cfg: GatewayConfig) -> dict[str, BackendGate]:
    return {"text": BackendGate("text", cfg.text_max_inflight, cfg.text_max_queue),
            "visual": BackendGate("visual", cfg.visual_max_inflight, cfg.visual_max_queue)}


# llama-server runs with --no-warmup (its built-in mtmd warmup would reserve the ViT buffer for 46×46 = 2116 tokens);
# the gateway warms it up with the largest image the contract admits: 896 × 672 = M0_MAX_PIXELS → 768 visual tokens.
WARMUP_SIZE_PX = (896, 672)


def warmup_image_base64() -> str:
    import base64
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", WARMUP_SIZE_PX, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for k in range(0, WARMUP_SIZE_PX[0], 56):
        draw.line((k, 0, WARMUP_SIZE_PX[0] - k, WARMUP_SIZE_PX[1]), fill=(k % 255, 80, 160), width=3)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


async def warmup_visual(res: Resources) -> float:
    async with res.gates["visual"].slot():
        t0 = time.perf_counter()
        await res.visual.embed_images("warmup", [warmup_image_base64()])
        return time.perf_counter() - t0


async def _warmup_loop(res: Resources, cfg: GatewayConfig) -> None:
    """Warm llama-server up once per instance (its media marker is random per start)."""
    seen: str | None = None
    while True:
        try:
            if await res.visual.health() == "ready":
                marker = (await res.visual.props(refresh=True)).get("media_marker")
                if marker and marker != seen:
                    seconds = await warmup_visual(res)
                    seen = marker
                    LOG.info("visual warmup done", extra={"vkm": {"stage": "warmup", "status": "ok",
                                                                  "duration_ms": round(seconds * 1000, 1),
                                                                  "image_px": list(WARMUP_SIZE_PX)}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the loop alive, report
            LOG.warning("visual warmup failed", extra={"vkm": {"stage": "warmup", "status": "error",
                                                               "error_code": type(exc).__name__}})
        await asyncio.sleep(cfg.warmup_interval_s)


# ----------------------------------------------------------------------------------------------------------- flows
async def rerank_text(req: M.TextRerankRequest, res: Resources, cfg: GatewayConfig, request_id: str
                      ) -> M.RerankResponse:
    t0 = time.perf_counter()
    warnings: list[str] = []
    n = len(req.candidates)
    top_n = min(req.top_n or n, n)
    if req.top_n and req.top_n > n:
        warnings.append("TOP_N_CLAMPED")
    texts, per = [], []
    for c in req.candidates:
        if req.truncate_to_tokens:
            text, ntok, cut = res.v35_tokens.truncate(c.text, req.truncate_to_tokens)
        else:
            text, ntok, cut = c.text, res.v35_tokens.count(c.text), False
        texts.append(text)
        per.append({"n_tokens": ntok, "truncated": cut, "end": len(text)})
    if any(p["truncated"] for p in per):
        warnings.append("CANDIDATES_TRUNCATED")
    total = res.v35_tokens.v35_prompt_tokens(req.query, texts)
    if total > cfg.text_token_budget:
        raise RerankError(M.E_PAYLOAD_TOO_LARGE, f"listwise prompt needs {total} tokens > {cfg.text_token_budget}",
                          stage="text_tokens", details={"n_tokens_total": total, "budget": cfg.text_token_budget,
                                                        "candidate_tokens": {c.id: p["n_tokens"] for c, p in
                                                                             zip(req.candidates, per)}})
    t_pre = time.perf_counter()
    async with res.gates["text"].slot() as queued_s:
        tb = time.perf_counter()
        out = await res.text.rerank(req.query, texts, top_n, cfg.text_token_budget)
        backend_s = time.perf_counter() - tb
    if out["total_tokens"] != total:
        warnings.append(f"TOKEN_COUNT_DIFFERS:gateway={total},service={out['total_tokens']}")
    runtime = out["runtime"]
    results = []
    for rank, (idx, score) in enumerate(out["results"], start=1):
        c, p = req.candidates[idx], per[idx]
        results.append(M.RankedCandidate(
            id=c.id, rank=rank, score=score, input_index=idx, input_text_sha256=_sha256_text(c.text),
            text_chars=len(c.text), text_char_range=(0, p["end"]), truncated=p["truncated"], n_tokens=p["n_tokens"]))
    query_sha = _sha256_text(req.query)
    input_sha = _canonical_sha256({"kind": "text", "query_sha256": query_sha, "top_n": top_n,
                                   "truncate_to_tokens": req.truncate_to_tokens,
                                   "candidates": [[c.id, _sha256_text(c.text)] for c in req.candidates]})
    t_end = time.perf_counter()
    return M.RerankResponse(
        kind="text", request_id=request_id, model_id=runtime.get("model_id", pins.TEXT["model_id"]),
        model_revision=runtime.get("model_revision", pins.TEXT["model_revision"]),
        quant=f"{runtime.get('dtype_or_quantization', 'float16')} (no quantization)",
        placement=pins.TEXT["placement"], backend=pins.TEXT["backend"],
        backend_version=f"{runtime.get('runtime_backend', '?')}; torch {runtime.get('torch_version', '?')}; "
                        f"transformers {runtime.get('transformers_version', '?')}",
        weights_sha256=pins.TEXT["weights_sha256"],
        model_config_sha256=_canonical_sha256(cfg.text_model_config(runtime)),
        score_semantics=pins.TEXT["score_semantics"], license=pins.LICENSE,
        candidate_ids=[r.id for r in results], scores=[r.score for r in results], results=results,
        n_candidates=n, top_n=top_n, query_sha256=query_sha, input_sha256=input_sha, n_tokens_total=total,
        latency_ms=M.LatencyMs(total=round((t_end - t0) * 1000, 2), preprocess=round((t_pre - t0) * 1000, 2),
                               queue=round(queued_s * 1000, 2), backend=round(backend_s * 1000, 2)),
        gateway_version=GATEWAY_VERSION, created_at=_now(), warnings=warnings)


def _decode_all(req: M.VisualRerankRequest, cfg: GatewayConfig) -> list[Any]:
    from vkm_corpus.retrieval.images import ImageDecodeError, decode_base64, normalize_image

    out = []
    for c in req.candidates:
        try:
            out.append(normalize_image(decode_base64(c.image_base64), max_side=cfg.image_max_side))
        except ImageDecodeError as exc:
            raise RerankError(M.E_IMAGE_DECODE_FAILED, f"candidate {c.id}: {exc}", stage="image_decode",
                              details={"candidate_id": c.id}) from exc
        except M.RerankLimitError as exc:
            raise RerankError(M.E_PAYLOAD_TOO_LARGE, f"candidate {c.id}: {exc}", stage="image_decode",
                              details={"candidate_id": c.id, "limit": exc.limit, "actual": exc.actual}) from exc
    return out


async def rerank_visual(req: M.VisualRerankRequest, res: Resources, cfg: GatewayConfig, request_id: str
                        ) -> M.RerankResponse:
    from vkm_corpus.retrieval.tokens import strip_special_tokens

    t0 = time.perf_counter()
    warnings: list[str] = []
    n = len(req.candidates)
    top_n = min(req.top_n or n, n)
    if req.top_n and req.top_n > n:
        warnings.append("TOP_N_CLAMPED")
    query, stripped = strip_special_tokens(req.query)
    if stripped:
        warnings.append("QUERY_SPECIAL_TOKENS_STRIPPED")
    if not query.strip():
        raise RerankError(M.E_INPUT_INVALID, "query is empty after removing special tokens", stage="query")
    q_tokens = res.m0_tokens.count(query)
    if q_tokens > M.VISUAL_QUERY_MAX_TOKENS:
        raise RerankError(M.E_PAYLOAD_TOO_LARGE, f"visual query has {q_tokens} tokens > {M.VISUAL_QUERY_MAX_TOKENS}",
                          stage="query_tokens", details={"query_tokens": q_tokens,
                                                         "limit": M.VISUAL_QUERY_MAX_TOKENS})
    images = await asyncio.to_thread(_decode_all, req, cfg)
    t_pre = time.perf_counter()
    async with res.gates["visual"].slot() as queued_s:
        tb = time.perf_counter()
        hidden = await res.visual.embed_images(query, [img.png_base64() for img in images])
        backend_s = time.perf_counter() - tb
    scores = res.head.scores(hidden)
    order = sorted(range(n), key=lambda i: (-scores[i], i))[:top_n]
    results = []
    for rank, idx in enumerate(order, start=1):
        c, img = req.candidates[idx], images[idx]
        results.append(M.RankedCandidate(
            id=c.id, rank=rank, score=scores[idx], input_index=idx, source_image_sha256=img.source_sha256,
            image_sha256=img.sha256, pixel_sha256=img.pixel_sha256,
            source_image_size_px=(img.source_width, img.source_height), image_size_px=(img.width, img.height),
            image_tokens=img.image_tokens))
    query_sha = _sha256_text(req.query)
    input_sha = _canonical_sha256({"kind": "visual", "query_sha256": query_sha, "top_n": top_n,
                                   "image_max_side": cfg.image_max_side,
                                   "candidates": [[c.id, img.source_sha256] for c, img in zip(req.candidates, images)]})
    t_end = time.perf_counter()
    if cfg.m0_deviation_note:
        warnings.append("VISUAL_DEVIATION:" + cfg.m0_deviation_note)
    return M.RerankResponse(
        kind="visual", request_id=request_id, model_id=pins.VISUAL["model_id"],
        model_revision=pins.VISUAL["model_revision"], quant=f"{cfg.m0_quant} + mmproj {pins.VISUAL['mmproj_quant']}",
        placement=cfg.visual_placement, backend=pins.VISUAL["backend"],
        backend_version=f"llama.cpp {cfg.llama_cpp_commit[:12]}",
        weights_sha256=cfg.m0_weights_sha256, model_config_sha256=_canonical_sha256(cfg.visual_model_config()),
        score_semantics=pins.VISUAL["score_semantics"], license=pins.LICENSE,
        candidate_ids=[r.id for r in results], scores=[r.score for r in results], results=results,
        n_candidates=n, top_n=top_n, query_sha256=query_sha, input_sha256=input_sha, n_tokens_total=None,
        latency_ms=M.LatencyMs(total=round((t_end - t0) * 1000, 2), preprocess=round((t_pre - t0) * 1000, 2),
                               queue=round(queued_s * 1000, 2), backend=round(backend_s * 1000, 2)),
        gateway_version=GATEWAY_VERSION, created_at=_now(), warnings=warnings)


async def status_payload(res: Resources, cfg: GatewayConfig) -> M.StatusResponse:
    text_state, runtime, detail = "unavailable", None, None
    try:
        runtime = await res.text.identity(refresh=True)
        text_state = "ready"
    except RerankError as exc:
        detail = exc.message
    visual_state = await res.visual.health()
    vprops: dict[str, Any] = {}
    vdetail = None
    if visual_state == "ready":
        try:
            vprops = await res.visual.props(refresh=True)
            loaded = Path(str(vprops.get("model_path", ""))).name
            if loaded and loaded != pins.VISUAL["weights_file"]:
                visual_state, vdetail = "mismatch", f"llama-server serves {loaded}"
        except RerankError as exc:
            vdetail = exc.message
    text_cfg, vis_cfg = cfg.text_model_config(runtime), cfg.visual_model_config()
    return M.StatusResponse(
        gateway_version=GATEWAY_VERSION, started_at=res.started_at, limits=M.contract_limits(),
        backends={
            "text": M.BackendStatus(
                role="text-rerank (existing service, loopback)", kind="text", status=text_state,
                model_id=(runtime or {}).get("model_id"), model_revision=(runtime or {}).get("model_revision"),
                code_revision=(runtime or {}).get("model_code_revision"),
                tokenizer_revision=(runtime or {}).get("tokenizer_revision"),
                quant=f"{(runtime or {}).get('dtype_or_quantization', 'float16')} (no quantization)",
                placement=pins.TEXT["placement"], backend=pins.TEXT["backend"],
                backend_version=(f"torch {runtime.get('torch_version')}; transformers "
                                 f"{runtime.get('transformers_version')}") if runtime else None,
                weights_sha256=pins.TEXT["weights_sha256"],
                aux_artifacts=[{"role": "tokenizer (gateway token count)", "file": pins.TEXT["tokenizer_file"],
                                "sha256": cfg.v35_tokenizer_sha256}],
                score_semantics=pins.TEXT["score_semantics"], license=pins.LICENSE,
                limits={"max_candidates": M.MAX_TEXT_CANDIDATES, "token_budget": cfg.text_token_budget},
                concurrency=("service runs inference under one lock; gateway limit "
                             + (f"{cfg.text_max_inflight} in flight" if cfg.text_max_inflight else "none")),
                model_config_sha256=_canonical_sha256(text_cfg), detail=detail),
            "visual": M.BackendStatus(
                role="visual-rerank (llama-server, loopback)", kind="visual", status=visual_state,
                model_id=pins.VISUAL["model_id"], model_revision=pins.VISUAL["model_revision"],
                quant=f"{cfg.m0_quant} + mmproj {pins.VISUAL['mmproj_quant']}", placement=cfg.visual_placement,
                backend=pins.VISUAL["backend"], backend_version=f"llama.cpp {cfg.llama_cpp_commit}",
                weights_sha256=cfg.m0_weights_sha256,
                aux_artifacts=[
                    {"role": "mmproj (vision encoder)", "file": pins.VISUAL["mmproj_file"],
                     "sha256": cfg.m0_mmproj_sha256,
                     "derived_from": f"{pins.VISUAL['source_model_id']}@{pins.VISUAL['source_revision']}"},
                    {"role": "score head", "file": pins.VISUAL["head_file"], "sha256": cfg.m0_head_sha256},
                ] + ([{"role": "llama-server image", "ref": cfg.llama_image}] if cfg.llama_image else []),
                score_semantics=pins.VISUAL["score_semantics"], license=pins.LICENSE,
                limits={"max_candidates": M.MAX_VISUAL_CANDIDATES, "image_max_side_px": cfg.image_max_side,
                        "max_pixels": M.M0_MAX_PIXELS, "query_max_tokens": M.VISUAL_QUERY_MAX_TOKENS,
                        "slots": vprops.get("total_slots")},
                concurrency=f"gateway: {cfg.visual_max_inflight} in flight + queue {cfg.visual_max_queue}",
                deviation_note=cfg.m0_deviation_note, model_config_sha256=_canonical_sha256(vis_cfg),
                detail=vdetail),
        })


# ------------------------------------------------------------------------------------------------------------- app
def _error_json(exc: RerankError, request_id: str | None) -> dict[str, Any]:
    return M.ErrorResponse(error=M.ErrorBody(code=exc.code, message=exc.message, retryable=exc.retryable,
                                             stage=exc.stage, request_id=request_id,
                                             details=exc.details)).model_dump(mode="json")


def _validation_details(exc) -> list[dict[str, Any]]:
    """pydantic errors without the offending input (it may be a text or an image)."""
    return [{"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()][:20]


async def _read_body(request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise RerankError(M.E_PAYLOAD_TOO_LARGE, f"request body {declared} bytes > {limit}", stage="body",
                          details={"limit": limit})
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise RerankError(M.E_PAYLOAD_TOO_LARGE, f"request body > {limit} bytes", stage="body",
                              details={"limit": limit})
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(body: bytes, model_cls, max_candidates: int, what: str):
    from pydantic import ValidationError

    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RerankError(M.E_INPUT_INVALID, f"body is not valid JSON: {exc.__class__.__name__}", stage="parse") from exc
    if not isinstance(data, dict):
        raise RerankError(M.E_INPUT_INVALID, "body must be a JSON object", stage="parse")
    cands = data.get("candidates")
    if isinstance(cands, list) and len(cands) > max_candidates:   # H-13: 413, not 422, and no batching
        raise RerankError(M.E_PAYLOAD_TOO_LARGE, f"{what}: {len(cands)} candidates > {max_candidates}",
                          stage="parse", details={"limit": max_candidates, "actual": len(cands)})
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise RerankError(M.E_INPUT_INVALID, "request does not match the contract", stage="validate",
                          details={"errors": _validation_details(exc)}) from exc


def create_app(cfg: GatewayConfig, resources: Resources | None = None):
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    if cfg.auth_required and not cfg.token:
        raise ValueError("VKM_RERANK_TOKEN_FILE is required (or VKM_RERANK_AUTH=disabled for loopback tests)")

    @asynccontextmanager
    async def lifespan(app):
        res = resources or load_resources(cfg)
        if not res.gates:
            res.gates = _gates(cfg)
        app.state.res = res
        LOG.info("gateway started", extra={"vkm": {"status": "started", "gateway_version": GATEWAY_VERSION,
                                                   "visual_placement": cfg.visual_placement,
                                                   "text_max_inflight": cfg.text_max_inflight}})
        task = asyncio.create_task(_warmup_loop(res, cfg)) if cfg.warmup else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for backend in (res.text, res.visual):
                close = getattr(backend, "aclose", None)
                if close:
                    await close()

    app = FastAPI(title="vkm-rerank-gateway", version=GATEWAY_VERSION, lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)

    def _request_id(request: Request) -> str:
        rid = request.headers.get(M.HEADER_REQUEST_ID, "")
        return rid if _REQUEST_ID_RE.fullmatch(rid) else "rr-" + uuid.uuid4().hex[:16]

    def _authorized(request: Request) -> bool:
        if not cfg.auth_required:
            return True
        got = request.headers.get(M.HEADER_TOKEN, "")
        return bool(got) and hmac.compare_digest(got.encode("utf-8"), (cfg.token or "").encode("utf-8"))

    def _log(request_id: str, kind: str, status: int, t0: float, **fields: Any) -> None:
        payload = {"request_id": request_id, "stage": kind, "status": status,
                   "duration_ms": round((time.perf_counter() - t0) * 1000, 2)}
        payload.update({k: v for k, v in fields.items() if v is not None})
        LOG.info("rerank request", extra={"vkm": payload})

    def _error_response(exc: RerankError, rid: str) -> JSONResponse:
        headers = {M.HEADER_REQUEST_ID: rid}
        if exc.code == M.E_BACKEND_BUSY:
            headers["Retry-After"] = "5"
        return JSONResponse(_error_json(exc, rid), status_code=exc.status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        code = M.E_NOT_FOUND if exc.status_code == 404 else M.E_INPUT_INVALID
        err = RerankError(code, str(exc.detail), stage="route")
        return JSONResponse(_error_json(err, None), status_code=exc.status_code)

    async def _handle(request: Request, kind: str, model_cls, max_candidates: int, limit: int, flow):
        t0 = time.perf_counter()
        rid = _request_id(request)
        if not _authorized(request):
            _log(rid, kind, 401, t0, error_code=M.E_UNAUTHORIZED)
            return _error_response(RerankError(M.E_UNAUTHORIZED, f"missing or wrong {M.HEADER_TOKEN}",
                                               stage="auth"), rid)
        req = None
        try:
            body = await _read_body(request, limit)
            req = _parse(body, model_cls, max_candidates, kind)
            if req.request_id:
                rid = req.request_id
            resp = await flow(req, request.app.state.res, cfg, rid)
        except RerankError as exc:
            _log(rid, kind, exc.status, t0, error_code=exc.code,
                 n_candidates=len(req.candidates) if req else None)
            return _error_response(exc, rid)
        ids = resp.candidate_ids
        _log(rid, kind, 200, t0, n_candidates=resp.n_candidates, top_n=resp.top_n,
             candidate_ids=ids[:32], candidate_ids_sha256=_canonical_sha256(ids),
             n_tokens_total=resp.n_tokens_total, backend_ms=resp.latency_ms.backend,
             queue_ms=resp.latency_ms.queue, model_revision=resp.model_revision, quant=resp.quant)
        return JSONResponse(resp.model_dump(mode="json"), headers={M.HEADER_REQUEST_ID: rid})

    @app.post("/v1/rerank/text")
    async def post_text(request: Request):
        return await _handle(request, "text", M.TextRerankRequest, M.MAX_TEXT_CANDIDATES, M.MAX_BODY_BYTES_TEXT,
                             rerank_text)

    @app.post("/v1/rerank/visual")
    async def post_visual(request: Request):
        return await _handle(request, "visual", M.VisualRerankRequest, M.MAX_VISUAL_CANDIDATES,
                             M.MAX_BODY_BYTES_VISUAL, rerank_visual)

    @app.get("/health")
    async def get_health(request: Request):
        res = request.app.state.res
        text_state = await res.text.health()
        visual_state = await res.visual.health()
        states = {"text": text_state, "visual": visual_state}
        overall = "ok" if all(s == "ready" for s in states.values()) else "degraded"
        return JSONResponse(M.HealthResponse(status=overall, backends=states).model_dump(mode="json"))

    @app.get("/status")
    async def get_status(request: Request):
        rid = _request_id(request)
        if not _authorized(request):
            return _error_response(RerankError(M.E_UNAUTHORIZED, f"missing or wrong {M.HEADER_TOKEN}",
                                               stage="auth"), rid)
        payload = await status_payload(request.app.state.res, cfg)
        return JSONResponse(payload.model_dump(mode="json"), headers={M.HEADER_REQUEST_ID: rid})

    return app


def serve(hosts: list[str], port: int, cfg: GatewayConfig | None = None, log_dir: str | None = None) -> int:
    """Run the gateway on every address in ``hosts`` (e.g. the LAN address of EDGE and 127.0.0.1)."""
    import socket

    import uvicorn

    from vkm_corpus.logs import configure

    cfg = cfg or GatewayConfig.from_env()
    if not cfg.auth_required and any(h not in ("127.0.0.1", "::1", "localhost") for h in hosts):
        raise ValueError("VKM_RERANK_AUTH=disabled is allowed only on loopback")
    configure("vkm-rerank-gateway", Path(log_dir) if log_dir else None, level=load_settings().log_level)
    app = create_app(cfg)
    sockets = []
    for host in dict.fromkeys(hosts):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.set_inheritable(True)
        sockets.append(sock)
    config = uvicorn.Config(app, log_config=None, access_log=False, proxy_headers=False, server_header=False,
                            timeout_keep_alive=30, limit_concurrency=256)
    server = uvicorn.Server(config)
    asyncio.run(server.serve(sockets=sockets))
    return 0


def config_summary(cfg: GatewayConfig) -> dict[str, Any]:
    """Printable configuration (token redacted)."""
    data = asdict(cfg)
    data["token"] = "<set>" if cfg.token else None
    return data
