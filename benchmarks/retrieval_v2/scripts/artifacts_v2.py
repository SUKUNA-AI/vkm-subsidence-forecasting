"""Deployable dense vectors of one V2 model in the existing artifact format (``vkm_corpus.embeddings.artifacts``):
``<data root>/derived/embeddings/dense/<model>/<revision>/<config-hash>/{config.json, part-<writer>-NNNNN.parquet,
_manifest-<writer>.json}`` for the units of the CURRENT snapshot (``units_final.jsonl``: 205 457 units incl.
BIB_ENTRY — the unit set ``search build-vectors`` expects), then the §64 checks (``validate``).

    python artifacts_v2.py <key>        # needs $V2_WORK/vec/<key>/final/ from ``encode_v2.py text <key> final``

The weights are the pinned safetensors run in bf16 on the RTX 5070 Ti, so the signature records ``quantization BF16``,
``weights_file model.safetensors`` and ``backend`` (a different signature from any Q8_0 GGUF artifact, by design of
``EmbeddingConfig``). The model key the service checks (``model_key_of``) is the same as for the Q8_0 GGUF query encoder.
Output root: ``$V2_WORK/artifacts`` (outside git). Writes ``artifact_receipt.json`` next to the parts.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from vkm_corpus.embeddings import specs as ES  # noqa: E402
from vkm_corpus.embeddings.artifacts import ArtifactWriter, EmbeddingRow, validate  # noqa: E402
from vkm_corpus.embeddings.signature import EmbeddingConfig  # noqa: E402
from vkm_corpus.search.vectors import model_key_of  # noqa: E402

WORK = Path(os.environ["V2_WORK"])
V1 = Path(os.environ["J_V1"])
CFG = json.loads((REPO / "benchmarks/retrieval_v2/configs/models_v2.json").read_text(encoding="utf-8"))
PART_ROWS = 512


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    key = sys.argv[1]
    m = next(x for x in CFG["text"] if x["key"] == key)
    spec = ES.get(m["rx580_key"])
    src = WORK / "vec" / key / "final"
    meta = json.loads((src / "meta.json").read_text(encoding="utf-8"))
    vecs = np.load(src / "docs.f32.npy")
    ids = json.loads((src / "docs_ids.json").read_text(encoding="utf-8"))
    units = {}
    with open(V1 / "units_final.jsonl", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            units[r["unit_id"]] = r
    if ids != list(units):
        raise SystemExit("vector order differs from units_final.jsonl")
    hf = Path(os.environ["VKM_MODELS_DIR"]) / spec.model_id.replace("/", "__") / spec.model_revision
    weights = hf / "model.safetensors"
    config = EmbeddingConfig(
        model_id=spec.model_id, model_revision=spec.model_revision, weights_file=weights.name,
        weights_sha256=sha256_file(weights), quantization="BF16", mode="dense", dimension=int(vecs.shape[1]),
        pooling=spec.pooling, normalization="l2", output_transform=spec.output_transform,
        document_instruction=spec.doc_prefix,
        text_rule="vkm-units-v1/A", max_len=int(CFG["doc_max_tokens"]),
        tokenizer_sha256=sha256_file(hf / spec.tokenizer_file), storage_precision="float32",
        backend=f"sentence-transformers-cuda-bf16 ({meta['env'].get('gpu')})")
    root = WORK / "artifacts"
    writer = ArtifactWriter(root, "dense", config, writer_id="rtx5070ti-v2")
    if writer.manifest()["parts"]:
        raise SystemExit(f"{writer.dir} already has parts (immutable): remove the directory to rewrite")
    t0 = time.time()
    for a in range(0, len(ids), PART_ROWS):
        rows = []
        for i in range(a, min(a + PART_ROWS, len(ids))):
            u = units[ids[i]]
            rows.append(EmbeddingRow(object_id=u["unit_id"], text_hash=u["text_hash"], source_id=u["source_id"],
                                     page_id=u["page_id"], object_type=u["kind"], vector=vecs[i],
                                     worker="rtx5070ti-v2", backend=config.backend))
        writer.write_part(rows)
    write_s = time.time() - t0
    expected = {u: r["text_hash"] for u, r in units.items()}
    rep = validate(writer.dir, config, expected)
    receipt = {"key": key, "directory": str(writer.dir.relative_to(root)), "config_signature": config.signature(),
               "config": config.as_dict(), "model_key": model_key_of(config), "units": len(ids),
               "parts": len(writer.manifest()["parts"]), "write_s": round(write_s, 1),
               "encode_s": meta.get("encode_s"), "units_per_s": meta.get("units_per_s"),
               "validate": rep.as_dict(), "bytes": sum(p.stat().st_size for p in writer.dir.glob("*.parquet"))}
    (writer.dir / "artifact_receipt.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False),
                                                      encoding="utf-8")
    print(json.dumps({k: v for k, v in receipt.items() if k != "config"}, ensure_ascii=False, default=str)[:3000])
    if not rep.ok:
        raise SystemExit("artifact validation failed")


if __name__ == "__main__":
    main()
