"""Encode page previews with Qwen3-VL-Embedding-2B on the WORKSTATION GPU into the visual artifact (agent VIS).

Input: the list of pages still to encode — the ``--missing-out`` JSON of ``vkm-corpus search build-page-vectors
--plan-only`` on CORE (page ids and preview content addresses only) — and the producer's artifact store
(``VKM_STAGING_ARTIFACTS``; a preview lives at ``previews/<hh>/<hh>/<sha256>.jpg`` and must hash to its id).
Same encoder settings as V2 (sentence-transformers 6.1, bf16, SDPA, the model's default document instruction on the
image alone, batch 4), so the rows join the same config directory (same signature) as a new writer's parts; pages
already encoded with the same preview hash are skipped (§46). Bounded memory: one part of ``--part`` pages at a time.
Run it in ``systemd-run --user --scope -p MemoryMax=16G -p MemorySwapMax=0`` and one GPU job at a time.

    python encode_pages.py --todo todo.json --out-root <data-root-like dir> [--part 64] [--limit N]
    # env: VKM_MODELS_DIR, VKM_STAGING_ARTIFACTS
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from vkm_corpus.embeddings.artifacts import ArtifactWriter  # noqa: E402
from vkm_corpus.embeddings.page_images import encode_pages, page_image_config, plan, read_todo  # noqa: E402
from vkm_corpus.embeddings.specs import get  # noqa: E402
from vkm_corpus.embeddings.tokenize import file_sha256  # noqa: E402

WRITER = "rtx5070ti-enc"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--todo", required=True)
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--part", type=int, default=64)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--vram-fraction", type=float, default=0.5)
    a = ap.parse_args()
    import torch
    from PIL import Image
    from sentence_transformers import SentenceTransformer

    spec = get("qwen3-vl-emb-2b")
    hf = Path(os.environ["VKM_MODELS_DIR"]) / spec.model_id.replace("/", "__") / spec.model_revision
    meta, pages = read_todo(Path(a.todo))
    gpu = torch.cuda.get_device_name(0)
    config = page_image_config(spec, weights_sha256=file_sha256(hf / "model.safetensors"),
                               tokenizer_sha256=file_sha256(hf / "tokenizer.json"),
                               preprocessor_sha256=file_sha256(hf / "preprocessor_config.json"),
                               backend=f"sentence-transformers-cuda-bf16 ({gpu})")
    if meta.get("config_signature") and meta["config_signature"] != config.signature():
        raise SystemExit(f"the todo list was planned for signature {meta['config_signature'][:12]}…, this encoder "
                         f"writes {config.signature()[:12]}… (other weights, GPU class or settings)")
    writer = ArtifactWriter(Path(a.out_root), "visual", config, writer_id=WRITER)
    todo = plan(pages, writer.dir)[: a.limit or None]
    print(json.dumps({"planned": len(pages), "to_encode": len(todo), "dir": str(writer.dir)}), flush=True)
    if not todo:
        return
    torch.cuda.set_per_process_memory_fraction(a.vram_fraction)
    model = SentenceTransformer(str(hf), device="cuda", local_files_only=True,
                                model_kwargs={"dtype": torch.bfloat16, "attn_implementation": "sdpa"})

    def encode(images):
        return model.encode(images, prompt=spec.doc_prefix, batch_size=a.batch, convert_to_numpy=True,
                            show_progress_bar=False)

    def load(path: Path):
        with Image.open(path) as im:
            return im.convert("RGB")

    t0 = time.time()

    def progress(done: int, total: int) -> None:
        rate = done / max(time.time() - t0, 1e-9)
        print(f"{done}/{total} pages, {rate:.2f} p/s, peak MiB {torch.cuda.max_memory_allocated() >> 20}",
              flush=True)

    rep = encode_pages(todo, lambda imgs: np.asarray(encode(imgs), dtype=np.float32), writer,
                       artifacts_root=Path(os.environ["VKM_STAGING_ARTIFACTS"]), load_image=load, batch=a.part,
                       worker=WRITER, progress=progress)
    rep.update({"seconds": round(time.time() - t0, 1), "config_signature": config.signature(), "gpu": gpu})
    (writer.dir / f"encode_receipt_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json").write_text(
        json.dumps(rep, indent=1), encoding="utf-8")
    print(json.dumps(rep), flush=True)


if __name__ == "__main__":
    main()
