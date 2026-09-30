"""Visual artifact (page-image vectors, Qwen3-VL-Embedding-2B) from the page vectors V2 encoded on the RTX 5070 Ti.

V2 (``benchmarks/retrieval_v2``, ``encode_v2.py visual qwen3-vl-2b``) encoded every page with a stored PAGE_PREVIEW
of its snapshot (26 092 pages, bf16, sentence-transformers, the model's default document instruction) into
``$V2_WORK/vis/qwen3-vl-2b/{docs.f32.npy, docs_ids.json, meta.json}``, with the page → preview list in
``$V2_WORK/pages_visual.json``. This script writes them — unchanged, L2-normalised float32 — as the derived *visual*
artifact of ``vkm_corpus.embeddings.page_images`` (text hash = preview hash), validates it (§64) and writes
``artifact_receipt.json``. Whether they are the vectors of the CURRENT snapshot is decided on CORE:
``search build-page-vectors --plan-only`` compares every row with the snapshot's page → preview map and lists what is
missing or changed (``--missing-out``) for ``encode_pages.py``.

    python page_artifact_from_v2.py --out-root <data-root-like dir>     # env: V2_WORK, VKM_MODELS_DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from vkm_corpus.embeddings.artifacts import ArtifactWriter, validate  # noqa: E402
from vkm_corpus.embeddings.page_images import page_image_config, preview_hex, write_rows  # noqa: E402
from vkm_corpus.embeddings.specs import get  # noqa: E402
from vkm_corpus.embeddings.tokenize import file_sha256  # noqa: E402

PART_ROWS = 512
WRITER = "rtx5070ti-v2"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root", required=True, help="root under which derived/embeddings/visual/… is written")
    a = ap.parse_args()
    work = Path(os.environ["V2_WORK"])
    spec = get("qwen3-vl-emb-2b")
    src = work / "vis" / "qwen3-vl-2b"
    meta = json.loads((src / "meta.json").read_text(encoding="utf-8"))
    if meta["model_id"] != spec.model_id or meta["revision"] != spec.model_revision or meta.get("probe"):
        raise SystemExit("V2 vectors are not the pinned Qwen3-VL-Embedding-2B run")
    ids = json.loads((src / "docs_ids.json").read_text(encoding="utf-8"))
    if hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest() != meta["ids_sha256"]:
        raise SystemExit("docs_ids.json does not match meta.json")
    pv = json.loads((work / "pages_visual.json").read_text(encoding="utf-8"))
    preview = {p["page_id"]: p["artifact_id"] for p in pv["pages"]}
    if [p["page_id"] for p in pv["pages"]] != ids:
        raise SystemExit("page order differs from pages_visual.json")
    vecs = np.load(src / "docs.f32.npy", mmap_mode="r")
    if vecs.shape != (len(ids), spec.output_dim):
        raise SystemExit(f"vectors {vecs.shape}")
    hf = Path(os.environ["VKM_MODELS_DIR"]) / spec.model_id.replace("/", "__") / spec.model_revision
    weights = meta["weights"]["files"]
    tok_sha = file_sha256(hf / "tokenizer.json")
    if weights.get("tokenizer.json") and weights["tokenizer.json"] != tok_sha:
        raise SystemExit("tokenizer.json differs from the download receipt")
    config = page_image_config(spec, weights_sha256=weights["model.safetensors"], tokenizer_sha256=tok_sha,
                               preprocessor_sha256=file_sha256(hf / "preprocessor_config.json"),
                               backend=f"sentence-transformers-cuda-bf16 ({meta['env'].get('gpu')})")
    writer = ArtifactWriter(Path(a.out_root), "visual", config, writer_id=WRITER)
    if writer.manifest()["parts"]:
        raise SystemExit(f"{writer.dir} already has parts of writer {WRITER} (parts are immutable)")
    t0 = time.time()
    expected = {}
    for s in range(0, len(ids), PART_ROWS):
        keys, block = [], np.asarray(vecs[s:s + PART_ROWS], dtype=np.float32)
        for pid in ids[s:s + PART_ROWS]:
            h = preview_hex(preview[pid])
            if h is None:
                raise SystemExit(f"{pid}: preview id is not content-addressed")
            keys.append((pid, pid.split(":")[0], h))
            expected[pid] = h
        norms = np.linalg.norm(block, axis=1)
        if not np.all(np.isfinite(block)) or np.max(np.abs(norms - 1.0)) > 1e-3:
            raise SystemExit(f"rows {s}..: vectors are not finite unit vectors")
        write_rows(writer, keys, block, worker=WRITER)
    write_s = time.time() - t0
    rep = validate(writer.dir, config, expected)
    receipt = {"schema": "vkm.visual_artifact_receipt/1", "directory": str(writer.dir.relative_to(Path(a.out_root))),
               "config_signature": config.signature(), "config": config.as_dict(), "pages": len(ids),
               "source": {"v2_snapshot_id": pv.get("snapshot_id"),
                          "v2_pages_with_preview": pv.get("pages_with_preview"),
                          "v2_pages_total": pv.get("pages_total"), "ids_sha256": meta["ids_sha256"],
                          "encode_s": meta.get("encode_s"), "pages_per_s": meta.get("pages_per_s"),
                          "peak_vram_mib": meta.get("peak_vram_mib"), "env": meta.get("env"),
                          "finished_at": meta.get("finished_at")},
               "expected_sha256": hashlib.sha256("".join(f"{k}\t{v}\n" for k, v in sorted(expected.items()))
                                                 .encode()).hexdigest(),
               "parts": len(writer.manifest()["parts"]), "write_s": round(write_s, 1), "validate": rep.as_dict(),
               "bytes": sum(p.stat().st_size for p in writer.dir.glob("*.parquet"))}
    (writer.dir / "artifact_receipt.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False),
                                                      encoding="utf-8")
    print(json.dumps({k: v for k, v in receipt.items() if k != "config"}, ensure_ascii=False, default=str)[:3000])
    if not rep.ok:
        raise SystemExit("artifact validation failed")


if __name__ == "__main__":
    main()
