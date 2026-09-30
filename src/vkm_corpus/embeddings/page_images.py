"""Page-image vectors of the visual route (agent VIS): the *visual* derived artifact, one row per page.

The embedded input of a page is its stored ``PAGE_PREVIEW`` (JPEG, long side 1024 px) exactly as the producer stored
it; the preview's content address (``sha256:<hex>``, the page's ``preview_artifact_id``) is the row's ``text_hash``
and ``artifact_sha`` — a changed preview is a new hash and is encoded again (§46), an unchanged one never. The encoder
itself (Qwen3-VL-Embedding-2B through sentence-transformers, bf16, on the WORKSTATION GPU) lives outside the platform
(``infra/workstation/visual_route/``); this module is torch-free: configuration, the list of pages to encode, preview
paths and hash checks, and the writer loop around any ``encode(images) -> [n, dim]`` callable.

Layout: ``derived/embeddings/visual/<model>/<revision>/<config-hash>/`` (``vkm_corpus.embeddings.artifacts``);
OpenSearch projection: ``vkm_corpus.search.page_vectors``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from vkm_corpus.embeddings.artifacts import ArtifactWriter, EmbeddingRow, existing_hashes_in
from vkm_corpus.embeddings.signature import EmbeddingConfig
from vkm_corpus.embeddings.specs import EncoderSpec, get

PAGE_IMAGE_RULE = "vkm-page-preview-v1"          # text rule of a visual artifact: the stored PAGE_PREVIEW, as is
PAGE_IMAGE_INPUT = "PAGE_PREVIEW artifact as stored (JPEG, long side 1024 px), decoded to RGB"
TODO_SCHEMA = "vkm.page_vectors_todo/1"


def preview_hex(artifact_id: str | None) -> str | None:
    """``sha256:<hex>`` → ``<hex>``; anything else → None."""
    a = str(artifact_id or "")
    if not a.startswith("sha256:") or len(a) != 71:
        return None
    h = a[7:].lower()
    return h if all(c in "0123456789abcdef" for c in h) else None


def preview_relpath(artifact_id: str, media_type: str = "image/jpeg") -> str:
    """Stored path of a PAGE_PREVIEW under an artifact root (the rule of ``ArtifactStore.relpath``)."""
    from vkm_corpus.artifacts.store import ArtifactStore

    h = preview_hex(artifact_id)
    if h is None:
        raise ValueError(f"not a content-addressed artifact id: {artifact_id!r}")
    return ArtifactStore.relpath("PAGE_PREVIEW", h, media_type)


def page_image_config(spec: EncoderSpec | None = None, *, weights_file: str = "model.safetensors",
                      weights_sha256: str, tokenizer_sha256: str, preprocessor_sha256: str,
                      backend: str, quantization: str = "BF16",
                      storage_precision: str = "float32") -> EmbeddingConfig:
    """Document signature of the page vectors: the model's default document instruction on the page image alone,
    last-token pooling, L2; no text truncation (the image processor bounds the image tokens)."""
    spec = spec or get("qwen3-vl-emb-2b")
    if spec.family != "visual":
        raise ValueError(f"{spec.key} is not a visual encoder")
    return EmbeddingConfig(
        model_id=spec.model_id, model_revision=spec.model_revision, weights_file=weights_file,
        weights_sha256=weights_sha256, quantization=quantization, mode="visual", dimension=spec.output_dim,
        pooling=spec.pooling, normalization="l2" if spec.normalize else "none", document_instruction=spec.doc_prefix,
        text_rule=PAGE_IMAGE_RULE, max_len=0, tokenizer_sha256=tokenizer_sha256,
        storage_precision=storage_precision,  # type: ignore[arg-type]
        backend=backend, image={"input": PAGE_IMAGE_INPUT, "preprocessor_config_sha256": preprocessor_sha256})


@dataclass(frozen=True)
class PageTodo:
    page_id: str
    source_id: str | None
    preview_artifact_id: str

    @property
    def text_hash(self) -> str:
        h = preview_hex(self.preview_artifact_id)
        assert h is not None
        return h


def read_todo(path: Path) -> tuple[dict[str, Any], list[PageTodo]]:
    """Pages to encode: the ``--missing-out`` JSON of ``search build-page-vectors --plan-only`` (ids only)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != TODO_SCHEMA:
        raise ValueError(f"todo schema {raw.get('schema')!r} != {TODO_SCHEMA}")
    out = []
    for r in raw.get("pages") or []:
        if preview_hex(r.get("preview_artifact_id")) is None:
            raise ValueError(f"{r.get('page_id')}: no content-addressed preview")
        out.append(PageTodo(str(r["page_id"]), r.get("source_id"), str(r["preview_artifact_id"])))
    return {k: v for k, v in raw.items() if k != "pages"}, out


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan(pages: Sequence[PageTodo], artifact_dir: Path | None) -> list[PageTodo]:
    """Pages whose (page id, preview hash) is not yet in the artifact (§46: same hash + same signature = no work)."""
    have = existing_hashes_in(artifact_dir) if artifact_dir is not None and artifact_dir.is_dir() else {}
    return [p for p in pages if p.text_hash not in have.get(p.page_id, set())]


def encode_pages(pages: Sequence[PageTodo], encode: Callable[[list[Any]], np.ndarray], writer: ArtifactWriter, *,
                 artifacts_root: Path, load_image: Callable[[Path], Any], batch: int = 64, verify_sha: bool = True,
                 worker: str = "", progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Encode ``pages`` in parts of ``batch`` and append them to ``writer`` (one immutable part per batch).

    Every preview file must hash to its artifact id (a wrong or missing file stops the run before anything of its batch
    is written); vectors must be finite with the configured dimension; they are L2-normalised before writing."""
    done = 0
    dim = writer.config.dimension
    for a in range(0, len(pages), batch):
        chunk = pages[a:a + batch]
        images = []
        for p in chunk:
            path = Path(artifacts_root) / preview_relpath(p.preview_artifact_id)
            if not path.is_file():
                raise FileNotFoundError(f"{p.page_id}: preview {p.preview_artifact_id} is not in the artifact store")
            if verify_sha and file_sha256(path) != p.text_hash:
                raise ValueError(f"{p.page_id}: preview file does not hash to {p.preview_artifact_id}")
            images.append(load_image(path))
        vecs = np.asarray(encode(images), dtype=np.float32)
        if vecs.shape != (len(chunk), dim) or not np.all(np.isfinite(vecs)):
            raise ValueError(f"encoder returned {vecs.shape}, finite={bool(np.all(np.isfinite(vecs)))}; "
                             f"expected ({len(chunk)}, {dim})")
        vecs = vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12)
        write_rows(writer, [(p.page_id, p.source_id, p.text_hash) for p in chunk], vecs, worker=worker)
        done += len(chunk)
        if progress:
            progress(done, len(pages))
    return {"encoded": done, "parts": len(writer.manifest()["parts"])}


def write_rows(writer: ArtifactWriter, keys: Iterable[tuple[str, str | None, str]], vectors: np.ndarray, *,
               worker: str = "") -> dict[str, Any]:
    """One part: (page id, source id, preview hash) + vector per row."""
    rows = [EmbeddingRow(object_id=pid, text_hash=h, source_id=src, page_id=pid, object_type="PAGE",
                         content_sha256=h, vector=np.asarray(v, dtype=np.float32), artifact_sha=h,
                         worker=worker, backend=writer.config.backend)
            for (pid, src, h), v in zip(keys, vectors)]
    return writer.write_part(rows)
