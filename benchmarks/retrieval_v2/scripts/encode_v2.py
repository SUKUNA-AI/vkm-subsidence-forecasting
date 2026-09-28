"""V2 encoding on the WORKSTATION GPU: dense text vectors of the deployed dense-index units and page-image vectors of
the stored page previews; the benchmark queries of every model. No evaluation happens here.

Environment: ``J_V1`` (V1 work dir, read only: ``units_dense.jsonl``, ``units_final.jsonl``, ``core/duckdb``),
``V2_WORK`` (V2 work dir, outside git), ``VKM_MODELS_DIR`` (local model snapshots), ``VKM_STAGING_ARTIFACTS`` (the
producer's artifact store, read only: ``previews/…``). Model settings: ``benchmarks/retrieval_v2/configs/models_v2.json``
(+ the V0 specs it points to).

    prepare                 — page previews of the canon (page → stored PAGE_PREVIEW) → ``$V2_WORK/pages_visual.json``
    text <key> [units]      — dense units (default ``dense`` = the 186 116 units of the deployed index; ``final`` = the
                              units of the CURRENT snapshot, for a deployable artifact) and the 189 queries
    visual <key>            — every page with a preview + the 189 queries through the text tower
    queue <k1,k2,…>         — the above one after another (``v:<key>`` = visual), a failure is logged and skipped

Outputs per model: ``$V2_WORK/vec/<key>[/<units>]/{docs.f32.npy, docs_ids.json, queries.f32.npy, queries_ids.json,
meta.json}`` (L2-normalised float32); chunks under ``chunks/`` make every run resumable. Progress:
``$V2_WORK/logs/encode_<key>.log``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import sys
import time
import traceback
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab.encoders import load_specs, make_encoder  # noqa: E402
from vkm_corpus.retrieval_lab.vectors import l2_normalize  # noqa: E402

V1 = Path(os.environ["J_V1"])
WORK = Path(os.environ["V2_WORK"])
MODELS_DIR = os.environ.get("VKM_MODELS_DIR", "")
CFG = json.loads((REPO / "benchmarks/retrieval_v2/configs/models_v2.json").read_text(encoding="utf-8"))
SPECS = load_specs(REPO / "benchmarks/retrieval_v0/configs/models.json")
CHUNK_UNITS = 8192
CHUNK_PAGES = 512
LOG = None


def log(*a) -> None:
    line = time.strftime("%Y-%m-%dT%H:%M:%S") + " " + " ".join(str(x) for x in a)
    print(line, flush=True)
    if LOG is not None:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def ids_sha256(ids: list[str]) -> str:
    return sha256_text("\n".join(ids))


def model_cfg(key: str) -> tuple[str, dict]:
    for track in ("text", "visual"):
        for m in CFG[track]:
            if m["key"] == key:
                return track, m
    raise SystemExit(f"unknown V2 model key {key!r}")


def receipt_entry(model_id: str, revision: str) -> dict:
    """Weights files and sha256 of the local snapshot from the download receipts (nothing is re-hashed here)."""
    for name in ("MODELS_RECEIPT_RETRIEVAL.json", "MODELS_RECEIPT.json"):
        p = Path(MODELS_DIR) / name
        if not p.is_file():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        models = data.get("models")
        items = models.values() if isinstance(models, dict) else (models or [])
        for m in items:
            rid = m.get("repo_id") or m.get("model_id")
            if rid == model_id and m.get("revision") == revision:
                files = m.get("files") or {}
                keep = {}
                if isinstance(files, dict):
                    keep = {k: (v.get("sha256") if isinstance(v, dict) else v) for k, v in files.items()
                            if str(k).endswith((".safetensors", ".bin", ".pt", "tokenizer.json"))}
                elif isinstance(files, list):
                    keep = {f.get("path") or f.get("name"): (f.get("local_sha256") or f.get("lfs_sha256")
                                                             or f.get("sha256")) for f in files
                            if str(f.get("path") or f.get("name") or "").endswith((".safetensors", ".bin", ".pt",
                                                                                    "tokenizer.json"))}
                return {"receipt": name, "complete_and_verified": m.get("complete_and_verified",
                                                                         m.get("all_lfs_match")), "files": keep}
    return {"receipt": None, "files": {}}


def env_versions() -> dict:
    import importlib.metadata as md

    out = {"python": platform.python_version()}
    for pkg in ("torch", "transformers", "sentence-transformers", "numpy", "pillow", "tokenizers"):
        try:
            out[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            out[pkg] = None
    try:
        import torch

        out["cuda"] = torch.version.cuda
        out["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except Exception:  # noqa: BLE001
        pass
    return out


def queries() -> list[tuple[str, str]]:
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    return [(q.query_id, q.text) for q in bench.queries]


# ------------------------------------------------------------------------------------------------------------ units
def read_units(which: str) -> tuple[list[str], list[str], list[str]]:
    """(ids, texts, source ids) of the dense-index units (``dense``) or of the CURRENT snapshot (``final``), in export
    order; every text must hash to its ``text_hash``."""
    name = {"dense": "units_dense.jsonl", "final": "units_final.jsonl"}[which]
    ids, texts, sources = [], [], []
    with open(V1 / name, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if sha256_text(r["text"]) != r["text_hash"]:
                raise SystemExit(f"{r['unit_id']}: text does not match text_hash")
            ids.append(r["unit_id"])
            texts.append(r["text"])
            sources.append(r["source_id"])
    return ids, texts, sources


# ------------------------------------------------------------------------------------------------------------ prepare
def prepare() -> None:
    import duckdb

    root = Path(os.environ["VKM_STAGING_ARTIFACTS"])
    con = duckdb.connect(str(V1 / "core" / "duckdb" / "vkm_corpus.duckdb"), read_only=True)
    # an artifact can be registered more than once (same id, same stored file): one row per artifact id
    rows = con.execute("""
        WITH a AS (SELECT artifact_id, any_value(storage_relpath) AS storage_relpath,
                          any_value(image_width_px) AS w, any_value(image_height_px) AS h,
                          any_value(materialization) AS materialization
                   FROM canonical.artifacts WHERE artifact_kind = 'PAGE_PREVIEW' GROUP BY artifact_id)
        SELECT p.page_id, p.page_kind, a.artifact_id, a.storage_relpath, a.w, a.h, a.materialization
        FROM canonical.pages p LEFT JOIN a ON a.artifact_id = p.preview_artifact_id
        ORDER BY p.page_id""").fetchall()
    snap = con.execute("SELECT snapshot_id, manifest_sha256 FROM meta.snapshot").fetchone()
    con.close()
    pages, missing_file, no_preview = [], [], []
    kinds_without = {}
    for page_id, kind, aid, rel, w, h, mat in rows:
        if not aid or not rel:
            no_preview.append(page_id)
            kinds_without[kind] = kinds_without.get(kind, 0) + 1
            continue
        if not (root / rel).is_file():
            missing_file.append(page_id)
            continue
        pages.append({"page_id": page_id, "artifact_id": aid, "relpath": rel, "w": w, "h": h})
    out = {"snapshot_id": snap[0], "manifest_sha256": snap[1], "pages_total": len(rows), "pages_with_preview": len(pages),
           "no_preview": len(no_preview), "no_preview_by_kind": kinds_without, "missing_file": missing_file,
           "pages": pages}
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "pages_visual.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "pages"}, ensure_ascii=False))


# ------------------------------------------------------------------------------------------------------------ text
def plan_batches(lengths: np.ndarray, budget: int, cap: int) -> list[np.ndarray]:
    """Length-sorted batches with at most ``budget`` padded tokens (the longest item of a batch sets its width)."""
    order = np.argsort(lengths, kind="stable")
    batches, cur = [], []
    for i in order:
        width = min(int(lengths[i]), cap)
        if cur and (len(cur) + 1) * width > budget:
            batches.append(np.array(cur))
            cur = []
        cur.append(int(i))
    if cur:
        batches.append(np.array(cur))
    return batches


def encode_text(key: str, which: str = "dense", limit: int | None = None) -> None:
    import torch

    _, m = model_cfg(key)
    spec = SPECS[m["spec"]]
    changes = {"max_query_tokens": CFG["query_max_tokens"], "max_doc_tokens": CFG["doc_max_tokens"]}
    if m.get("family_override"):
        changes["family"] = m["family_override"]
    spec = dataclasses.replace(spec, **changes)
    out = WORK / "vec" / key / which if which != "dense" else WORK / "vec" / key
    if limit:
        out = WORK / "probe" / key
    (out / "chunks").mkdir(parents=True, exist_ok=True)
    if (out / "meta.json").is_file() and not limit:
        log(key, which, "already encoded")
        return
    torch.cuda.set_per_process_memory_fraction(float(CFG["vram_cap_fraction"]))
    ids, texts, sources = read_units(which)
    if limit:
        step = max(1, len(ids) // limit)
        sel = list(range(0, len(ids), step))[:limit]
        ids, texts, sources = [ids[i] for i in sel], [texts[i] for i in sel], [sources[i] for i in sel]
    t_load = time.time()
    kw = {"device": "cuda", "precision": CFG["precision"]}
    if spec.family != "pplx_context":
        kw["batch_size"] = 32
    enc = make_encoder(spec, MODELS_DIR, **kw)
    load_s = time.time() - t_load
    torch.cuda.reset_peak_memory_stats()
    log(key, which, "loaded in", round(load_s, 1), "s; units", len(ids), "alloc MiB", torch.cuda.memory_allocated() >> 20)
    t0 = time.time()
    n_tokens = None
    if spec.family == "pplx_context":
        vecs, n_tokens = _encode_context_windows(enc, m, ids, texts, sources, out)
    else:
        tok = enc.model.tokenizer
        prefix = spec.doc_prefix or ""
        lens = np.array([len(x) for x in tok([prefix + t for t in texts], add_special_tokens=True,
                                            truncation=False)["input_ids"]], dtype=np.int64)
        n_tokens = int(np.minimum(lens, CFG["doc_max_tokens"]).sum())
        batches = plan_batches(lens, int(m["token_budget"]), CFG["doc_max_tokens"])
        vecs = np.zeros((len(ids), spec.dim), dtype=np.float32)
        chunk, chunk_no, done_units = [], 0, 0
        for bi, b in enumerate(batches):
            chunk.append(b)
            if sum(len(x) for x in chunk) >= CHUNK_UNITS or bi == len(batches) - 1:
                path = out / "chunks" / f"c{chunk_no:04d}.npz"
                idx = np.concatenate(chunk)
                if path.is_file():
                    z = np.load(path)
                    vecs[z["idx"]] = z["vec"]
                else:
                    parts = []
                    for bb in chunk:
                        enc.batch_size = len(bb)
                        parts.append(enc.encode_docs([texts[i] for i in bb]))
                    v = np.concatenate(parts).astype(np.float32)
                    if not np.isfinite(v).all():
                        raise RuntimeError(f"non-finite vectors in chunk {chunk_no}")
                    np.savez(path, idx=idx, vec=v)
                    vecs[idx] = v
                done_units += len(idx)
                el = time.time() - t0
                log(key, which, f"chunk {chunk_no}: {done_units}/{len(ids)} units, {done_units / max(el, 1e-9):.1f} u/s,"
                    f" eta {int((len(ids) - done_units) / max(done_units / max(el, 1e-9), 1e-9))} s,"
                    f" peak MiB {torch.cuda.max_memory_allocated() >> 20}")
                chunk, chunk_no = [], chunk_no + 1
    enc_s = time.time() - t0
    vecs = l2_normalize(vecs)
    qs = queries()
    tq = time.time()
    if spec.family == "pplx_context":
        qv = np.stack([w[0] for w in enc.encode_windows([[t] for _q, t in qs])])
    else:
        enc.batch_size = 16
        qv = enc.encode_queries([t for _q, t in qs])
    q_s = time.time() - tq
    qv = l2_normalize(np.asarray(qv, dtype=np.float32))
    np.save(out / "docs.f32.npy", vecs)
    (out / "docs_ids.json").write_text(json.dumps(ids), encoding="utf-8")
    np.save(out / "queries.f32.npy", qv)
    (out / "queries_ids.json").write_text(json.dumps([q for q, _t in qs]), encoding="utf-8")
    meta = {"key": key, "units": which, "n_units": len(ids), "ids_sha256": ids_sha256(ids), "dim": int(vecs.shape[1]),
            "model_id": spec.model_id, "revision": spec.revision, "license": m["license"], "spec": m["spec"],
            "family": spec.family, "precision": CFG["precision"], "attention": CFG["attention"],
            "doc_prefix": spec.doc_prefix, "query_prefix": spec.query_prefix, "doc_max_tokens": CFG["doc_max_tokens"],
            "query_max_tokens": CFG["query_max_tokens"], "load_s": round(load_s, 1), "encode_s": round(enc_s, 1),
            "units_per_s": round(len(ids) / max(enc_s, 1e-9), 1),
            "tokens": n_tokens, "tokens_per_s": round(n_tokens / max(enc_s, 1e-9)) if n_tokens else None,
            "queries": len(qs), "queries_s": round(q_s, 2), "peak_vram_mib": int(torch.cuda.max_memory_allocated() >> 20),
            "weights": receipt_entry(spec.model_id, spec.revision), "env": env_versions(), "probe": bool(limit),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    log(key, which, "done", json.dumps({k: meta[k] for k in ("n_units", "units_per_s", "tokens_per_s", "encode_s",
                                                               "peak_vram_mib")}))
    del enc
    torch.cuda.empty_cache()


def _encode_context_windows(enc, m: dict, ids, texts, sources, out: Path):
    """pplx-embed-context: windows of ≤ ``window_units`` consecutive units of one source (export order) and
    ≤ ``window_chars`` characters; one vector per unit (its chunk in the window)."""
    import torch

    windows, cur, size = [], [], 0
    for i in range(len(ids)):
        if cur and (sources[i] != sources[cur[-1]] or len(cur) >= m["window_units"] or
                    size + len(texts[i]) > m["window_chars"]):
            windows.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(texts[i])
    if cur:
        windows.append(cur)
    vecs = np.zeros((len(ids), SPECS[m["spec"]].dim), dtype=np.float32)
    per_chunk = max(1, CHUNK_UNITS // m["window_units"])
    t0 = time.time()
    done = 0
    for c0 in range(0, len(windows), per_chunk):
        path = out / "chunks" / f"w{c0 // per_chunk:04d}.npz"
        ws = windows[c0:c0 + per_chunk]
        idx = np.array([i for w in ws for i in w])
        if path.is_file():
            z = np.load(path)
            vecs[z["idx"]] = z["vec"]
        else:
            got = []
            order = sorted(range(len(ws)), key=lambda k: sum(len(texts[i]) for i in ws[k]))
            res = {}
            for b0 in range(0, len(order), m["window_batch"]):
                bw = [ws[k] for k in order[b0:b0 + m["window_batch"]]]
                outs = enc.encode_windows([[texts[i] for i in w] for w in bw])
                for k, o in zip(order[b0:b0 + m["window_batch"]], outs):
                    res[k] = o
            for k in range(len(ws)):
                got.append(np.asarray(res[k], dtype=np.float32).reshape(len(ws[k]), -1))
            v = np.concatenate(got)
            if not np.isfinite(v).all():
                raise RuntimeError("non-finite vectors")
            np.savez(path, idx=idx, vec=v)
            vecs[idx] = v
        done += len(idx)
        el = time.time() - t0
        log(m["key"], f"windows {min(c0 + per_chunk, len(windows))}/{len(windows)}: {done}/{len(ids)} units, "
                      f"{done / max(el, 1e-9):.1f} u/s, peak MiB {torch.cuda.max_memory_allocated() >> 20}")
    return vecs, None


# ------------------------------------------------------------------------------------------------------------ visual
def load_images(paths: list[Path]):
    from concurrent.futures import ThreadPoolExecutor

    from PIL import Image

    def one(p):
        with Image.open(p) as im:
            return im.convert("RGB")

    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, paths))


def encode_visual(key: str, limit: int | None = None) -> None:
    import torch
    from sentence_transformers import SentenceTransformer

    _, m = model_cfg(key)
    spec = SPECS[m["spec"]]
    out = WORK / "vis" / key if not limit else WORK / "probe" / key
    (out / "chunks").mkdir(parents=True, exist_ok=True)
    if (out / "meta.json").is_file() and not limit:
        log(key, "already encoded")
        return
    torch.cuda.set_per_process_memory_fraction(float(CFG["vram_cap_fraction"]))
    pv = json.loads((WORK / "pages_visual.json").read_text(encoding="utf-8"))
    pages = pv["pages"][: limit or None]
    root = Path(os.environ["VKM_STAGING_ARTIFACTS"])
    path = str(Path(MODELS_DIR) / spec.model_id.replace("/", "__") / spec.revision)
    t_load = time.time()
    mk = {"dtype": torch.bfloat16, "attn_implementation": CFG["attention"]}
    if m.get("modality"):
        mk = {"dtype": torch.bfloat16, "modality": m["modality"]}
    model = SentenceTransformer(path, device="cuda", local_files_only=True, trust_remote_code=spec.trust_remote_code,
                                model_kwargs=mk)
    load_s = time.time() - t_load
    torch.cuda.reset_peak_memory_stats()
    log(key, "loaded in", round(load_s, 1), "s; pages", len(pages))

    def enc_docs(images):
        if m.get("doc_prompt_name"):
            return model.encode(images, prompt_name=m["doc_prompt_name"], batch_size=m["batch"], convert_to_numpy=True,
                                show_progress_bar=False)
        return model.encode(images, prompt=m["doc_prompt"], batch_size=m["batch"], convert_to_numpy=True,
                            show_progress_bar=False)

    vecs = None
    t0 = time.time()
    done = 0
    for c0 in range(0, len(pages), CHUNK_PAGES):
        cpath = out / "chunks" / f"p{c0 // CHUNK_PAGES:04d}.npy"
        chunk = pages[c0:c0 + CHUNK_PAGES]
        if cpath.is_file():
            v = np.load(cpath)
        else:
            imgs = load_images([root / p["relpath"] for p in chunk])
            v = np.asarray(enc_docs(imgs), dtype=np.float32)
            if not np.isfinite(v).all():
                raise RuntimeError(f"non-finite page vectors in chunk {c0 // CHUNK_PAGES}")
            np.save(cpath, v)
        if vecs is None:
            vecs = np.zeros((len(pages), v.shape[1]), dtype=np.float32)
        vecs[c0:c0 + len(chunk)] = v
        done += len(chunk)
        el = time.time() - t0
        log(key, f"{done}/{len(pages)} pages, {done / max(el, 1e-9):.2f} p/s, "
                 f"eta {int((len(pages) - done) / max(done / max(el, 1e-9), 1e-9))} s, "
                 f"peak MiB {torch.cuda.max_memory_allocated() >> 20}")
    enc_s = time.time() - t0
    vecs = l2_normalize(vecs)
    qs = queries()
    tq = time.time()
    if m.get("query_prompt_name"):
        qv = model.encode([t for _q, t in qs], prompt_name=m["query_prompt_name"], batch_size=16, convert_to_numpy=True)
    else:
        qv = model.encode([t for _q, t in qs], prompt=m["query_prompt"], batch_size=16, convert_to_numpy=True)
    q_s = time.time() - tq
    qv = l2_normalize(np.asarray(qv, dtype=np.float32))
    np.save(out / "docs.f32.npy", vecs)
    ids = [p["page_id"] for p in pages]
    (out / "docs_ids.json").write_text(json.dumps(ids), encoding="utf-8")
    np.save(out / "queries.f32.npy", qv)
    (out / "queries_ids.json").write_text(json.dumps([q for q, _t in qs]), encoding="utf-8")
    meta = {"key": key, "n_pages": len(pages), "ids_sha256": ids_sha256(ids), "dim": int(vecs.shape[1]),
            "model_id": spec.model_id, "revision": spec.revision, "license": m["license"], "precision": CFG["precision"],
            "doc_prompt": m.get("doc_prompt") or m.get("doc_prompt_name"),
            "query_prompt": m.get("query_prompt") or m.get("query_prompt_name"), "batch": m["batch"],
            "input": "PAGE_PREVIEW JPEG (long side 1024 px) of the producer STAGING, as stored",
            "load_s": round(load_s, 1), "encode_s": round(enc_s, 1), "pages_per_s": round(len(pages) / max(enc_s, 1e-9), 2),
            "queries": len(qs), "queries_s": round(q_s, 2), "peak_vram_mib": int(torch.cuda.max_memory_allocated() >> 20),
            "weights": receipt_entry(spec.model_id, spec.revision), "env": env_versions(), "probe": bool(limit),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    log(key, "done", json.dumps({k: meta[k] for k in ("n_pages", "pages_per_s", "encode_s", "peak_vram_mib")}))
    del model
    torch.cuda.empty_cache()


# ------------------------------------------------------------------------------------------------------------ main
def main() -> None:
    global LOG
    cmd = sys.argv[1]
    (WORK / "logs").mkdir(parents=True, exist_ok=True)
    if cmd == "prepare":
        prepare()
    elif cmd == "text":
        LOG = WORK / "logs" / f"encode_{sys.argv[2]}.log"
        encode_text(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "dense",
                    int(os.environ["V2_LIMIT"]) if os.environ.get("V2_LIMIT") else None)
    elif cmd == "visual":
        LOG = WORK / "logs" / f"encode_{sys.argv[2]}.log"
        encode_visual(sys.argv[2], int(os.environ["V2_LIMIT"]) if os.environ.get("V2_LIMIT") else None)
    elif cmd == "queue":
        LOG = WORK / "logs" / "queue.log"
        for item in sys.argv[2].split(","):
            t0 = time.time()
            try:
                if item.startswith("v:"):
                    encode_visual(item[2:])
                else:
                    key, _, which = item.partition("@")
                    encode_text(key, which or "dense")
                log("QUEUE OK", item, round(time.time() - t0, 1), "s")
            except Exception:  # noqa: BLE001 - one model failing must not stop the queue
                log("QUEUE FAIL", item, traceback.format_exc()[-2000:])
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
