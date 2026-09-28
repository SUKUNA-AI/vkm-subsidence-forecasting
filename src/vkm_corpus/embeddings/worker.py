"""Embedding job worker (постановка лаборатории §34–36, §63) — the interface K provides for the coordinator's
operational queue in PostgreSQL (``vkm_corpus.ops``; no Kafka/Redis/Celery).

Model: the corpus is split into jobs of ``max_objects`` canonical objects under ONE embedding config signature. Jobs
are ``PENDING`` → ``CLAIMED_RTX5070`` | ``CLAIMED_RX580`` → ``DONE`` | ``FAILED``; a claim is atomic (PostgreSQL:
``UPDATE … SET state = 'CLAIMED_RX580', worker = … WHERE job_id = (SELECT job_id … WHERE state = 'PENDING' ORDER BY
job_id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING …``). Scheduling is pull-based: each GPU worker claims the next small
job when it is free, so the faster device simply takes more jobs — wall-clock is minimised without a central
throughput model (§36). A job never embeds an object twice: before inference the worker drops objects whose
(text_hash, signature) is already in the artifact directory (§46), so a re-claimed job after a crash is idempotent.

This module defines the protocols (:class:`JobQueue`, :class:`TextSource`, :class:`DocumentEncoder`), an in-memory
queue with the same semantics for tests, the llama-server encoder used on the RX580 and :class:`EmbeddingWorker`.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence

import numpy as np

from vkm_corpus.embeddings import postprocess as pp
from vkm_corpus.embeddings.artifacts import ArtifactWriter, EmbeddingRow, Kind, existing_hashes_in
from vkm_corpus.embeddings.reembed import CanonObject
from vkm_corpus.embeddings.specs import EncoderSpec
from vkm_corpus.embeddings.tokenize import SpecTokenizer

JobState = Literal["PENDING", "CLAIMED_RTX5070", "CLAIMED_RX580", "DONE", "FAILED"]
Device = Literal["RTX5070", "RX580"]
CLAIMED: dict[str, JobState] = {"RTX5070": "CLAIMED_RTX5070", "RX580": "CLAIMED_RX580"}


@dataclass
class EmbedJob:
    job_id: str
    config_signature: str
    kind: Kind
    object_ids: list[str]
    state: JobState = "PENDING"
    worker: str | None = None
    attempts: int = 0
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    heartbeat_at: float | None = None


class JobQueue(Protocol):
    def claim(self, *, device: Device, worker: str, config_signature: str) -> EmbedJob | None: ...
    def heartbeat(self, job_id: str) -> None: ...
    def complete(self, job_id: str, result: dict[str, Any]) -> None: ...
    def fail(self, job_id: str, error: str, *, retryable: bool) -> None: ...


class TextSource(Protocol):
    def objects(self, object_ids: Sequence[str]) -> list[tuple[CanonObject, str]]:
        """Canonical objects with their embedded text (text rule of the config applied)."""


@dataclass
class Encoded:
    vector: np.ndarray | None = None
    int8: np.ndarray | None = None          # pplx native int8 (used when the config stores int8)
    vectors: np.ndarray | None = None
    token_ids: list[int] | None = None
    weights: list[float] | None = None


class DocumentEncoder(Protocol):
    backend: str

    def encode(self, texts: Sequence[str]) -> list[Encoded]: ...


class InMemoryJobQueue:
    """Test double with the PostgreSQL semantics: atomic claim, retry of retryable failures up to ``max_attempts``,
    reclaim of stale claims (no heartbeat for ``lease_s``)."""

    def __init__(self, *, max_attempts: int = 3, lease_s: float = 600.0) -> None:
        self._lock = threading.Lock()   # guards the dict only (stands in for the row lock of the database)
        self.jobs: dict[str, EmbedJob] = {}
        self.max_attempts = max_attempts
        self.lease_s = lease_s

    def add(self, config_signature: str, kind: Kind, object_ids: Sequence[str], job_size: int) -> list[str]:
        ids = []
        with self._lock:
            for i in range(0, len(object_ids), job_size):
                jid = f"EMB-{len(self.jobs):06d}-{uuid.uuid4().hex[:6]}"
                self.jobs[jid] = EmbedJob(jid, config_signature, kind, list(object_ids[i:i + job_size]))
                ids.append(jid)
        return ids

    def claim(self, *, device: Device, worker: str, config_signature: str) -> EmbedJob | None:
        now = time.time()
        with self._lock:
            for job in sorted(self.jobs.values(), key=lambda j: j.job_id):
                stale = job.state.startswith("CLAIMED") and job.heartbeat_at is not None and \
                    now - job.heartbeat_at > self.lease_s
                if job.config_signature == config_signature and (job.state == "PENDING" or stale):
                    job.state = CLAIMED[device]
                    job.worker = worker
                    job.attempts += 1
                    job.heartbeat_at = now
                    return job
        return None

    def heartbeat(self, job_id: str) -> None:
        with self._lock:
            self.jobs[job_id].heartbeat_at = time.time()

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job = self.jobs[job_id]
            if not job.state.startswith("CLAIMED"):
                raise ValueError(f"{job_id} is {job.state}, cannot complete")
            job.state, job.result = "DONE", dict(result)

    def fail(self, job_id: str, error: str, *, retryable: bool) -> None:
        with self._lock:
            job = self.jobs[job_id]
            job.error = error
            job.state = "PENDING" if (retryable and job.attempts < self.max_attempts) else "FAILED"

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for j in self.jobs.values():
            out[j.state] = out.get(j.state, 0) + 1
        return out


class LlamaDocumentEncoder:
    """Documents → vectors through a pinned llama-server (RX580 Vulkan or RTX CUDA: same GGUF, same signature)."""

    def __init__(self, spec: EncoderSpec, tokenizer: SpecTokenizer, client: Any, *, mode: str, max_len: int,
                 dimension: int | None = None, heads: dict[str, np.ndarray] | None = None,
                 backend: str = "llama.cpp-vulkan", batch: int = 8) -> None:
        self.spec, self.tok, self.client = spec, tokenizer, client
        self.mode, self.max_len, self.dimension = mode, max_len, dimension
        self.heads = heads or {}
        self.backend = backend
        self.batch = batch

    def encode(self, texts: Sequence[str]) -> list[Encoded]:
        enc = [self.tok.encode(t, "document", max_len=self.max_len) for t in texts]
        out: list[Encoded] = []
        for i in range(0, len(enc), self.batch):
            chunk = enc[i:i + self.batch]
            res = self.client.embed_ids([e.ids for e in chunk])
            for e, v in zip(chunk, res.vectors):
                if self.mode == "dense":
                    pooled = v if v.ndim == 1 else pp.pool(v, self.spec.pooling)
                    d = pp.finalize_dense(self.spec, pooled, dim=self.dimension)
                    out.append(Encoded(vector=d.vector, int8=d.int8))
                elif self.mode == "multivector":
                    m = pp.colbert_tokens(self.spec, v, e.keep, head=self.heads.get("colbert_w"),
                                          head_bias=self.heads.get("colbert_b"), dim=self.dimension)
                    kept = [tid for tid, k in zip(e.ids, e.keep or [True] * len(e.ids)) if k]
                    out.append(Encoded(vectors=m, token_ids=kept))
                elif self.mode == "sparse":
                    sp = pp.bge_m3_sparse(v, e.ids, self.heads["sparse_w"], self.heads["sparse_b"],
                                          unused_ids=(0, 1, 2, 3))
                    out.append(Encoded(token_ids=sp.token_ids.tolist(), weights=sp.weights.tolist()))
                else:
                    raise ValueError(f"unsupported mode {self.mode}")
        return out


class EmbeddingWorker:
    def __init__(self, queue: JobQueue, source: TextSource, encoder: DocumentEncoder, writer: ArtifactWriter, *,
                 device: Device, name: str) -> None:
        self.queue, self.source, self.encoder, self.writer = queue, source, encoder, writer
        self.device, self.name = device, name
        self._known: dict[str, set[str]] = {}
        self._seen_parts: set[str] = set()

    def _already(self) -> dict[str, set[str]]:
        # incremental and key columns only: re-reading every part (with vectors) per job is quadratic at corpus scale
        return existing_hashes_in(Path(self.writer.dir), known=self._known, seen_parts=self._seen_parts)

    def run_once(self) -> dict[str, Any] | None:
        sig = self.writer.config.signature()
        job = self.queue.claim(device=self.device, worker=self.name, config_signature=sig)
        if job is None:
            return None
        t0 = time.perf_counter()
        try:
            pairs = self.source.objects(job.object_ids)
            done = self._already()
            todo = [(o, t) for o, t in pairs if o.text_hash not in done.get(o.object_id, set())]
            rows: list[EmbeddingRow] = []
            if todo:
                vecs = self.encoder.encode([t for _, t in todo])
                self.queue.heartbeat(job.job_id)
                use_int8 = self.writer.config.storage_precision == "int8"
                for (o, _), v in zip(todo, vecs):
                    vector = v.int8 if (use_int8 and v.int8 is not None) else v.vector
                    rows.append(EmbeddingRow(object_id=o.object_id, text_hash=o.text_hash, source_id=o.source_id,
                                             page_id=o.page_id, object_type=o.object_type,
                                             content_sha256=o.content_sha256, vector=vector, vectors=v.vectors,
                                             token_ids=v.token_ids, weights=v.weights, worker=self.name,
                                             backend=self.encoder.backend))
            part = self.writer.write_part(rows)["file"] if rows else None
            result = {"objects": len(pairs), "embedded": len(rows), "skipped_existing": len(pairs) - len(todo),
                      "part": part, "seconds": round(time.perf_counter() - t0, 3), "device": self.device}
            self.queue.complete(job.job_id, result)
            return {"job_id": job.job_id, **result}
        except Exception as exc:
            retryable = getattr(exc, "retryable", False) or isinstance(exc, (ConnectionError, TimeoutError))
            self.queue.fail(job.job_id, f"{type(exc).__name__}: {exc}"[:500], retryable=retryable)
            raise

    def run(self, *, max_jobs: int | None = None, idle_exit: bool = True) -> list[dict[str, Any]]:
        results = []
        while max_jobs is None or len(results) < max_jobs:
            r = self.run_once()
            if r is None:
                if idle_exit:
                    break
                time.sleep(1.0)
                continue
            results.append(r)
        return results
