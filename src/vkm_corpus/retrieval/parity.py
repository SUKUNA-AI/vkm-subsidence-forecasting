"""Parity of the visual reranker: llama.cpp (GGUF Q6_K + mmproj Q8_0 + MLP head) against the transformers reference
(``jinaai/jina-reranker-m0`` @ ``94bfe0ae…``, bf16) — a blocking gate before deployment (CP-18).

Thresholds (project F §7.6, fixed before the first run): for every query Spearman ρ ≥ 0.9 over the images and
``max |Δscore| ≤ 0.05``; top-1 must agree. Layout parity (the same GGUF with a different GPU/CPU split) must agree to
``|Δ| ≤ 1e-3``. Both sides receive the images already normalised by :func:`images.normalize_image`, so no resampling
differs between them. torch / transformers / httpx are imported lazily.
"""
from __future__ import annotations

import math
from typing import Any

PARITY_SPEARMAN_MIN = 0.9
PARITY_MAX_ABS_DIFF = 0.05
LAYOUT_MAX_ABS_DIFF = 1e-3


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        raise ValueError("need two aligned series of length ≥ 2")
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else float("nan")


def compare(reference: dict[str, dict[str, float]], candidate: dict[str, dict[str, float]], *,
            spearman_min: float = PARITY_SPEARMAN_MIN, max_abs_diff: float = PARITY_MAX_ABS_DIFF) -> dict[str, Any]:
    """``{query_id: {image: score}}`` on both sides → per-query metrics and an overall verdict."""
    per_query, all_diffs = {}, []
    ok = True
    for qid, ref_scores in reference.items():
        names = sorted(ref_scores)
        if sorted(candidate.get(qid, {})) != names:
            raise ValueError(f"query {qid}: image sets differ")
        a = [ref_scores[n] for n in names]
        b = [candidate[qid][n] for n in names]
        diffs = [abs(x - y) for x, y in zip(a, b)]
        rho = spearman(a, b)
        top_ref, top_cand = max(names, key=lambda n: ref_scores[n]), max(names, key=lambda n: candidate[qid][n])
        # pairs ranked differently although the reference separates them clearly (diagnostic)
        clear_swaps = sum(1 for i in range(len(names)) for j in range(i + 1, len(names))
                          if (a[i] - a[j]) * (b[i] - b[j]) < 0 and abs(a[i] - a[j]) > max_abs_diff)
        q_ok = rho >= spearman_min and max(diffs) <= max_abs_diff and top_ref == top_cand
        ok &= q_ok
        all_diffs.extend(diffs)
        per_query[qid] = {"spearman": round(rho, 4), "max_abs_diff": round(max(diffs), 5),
                          "mean_abs_diff": round(sum(diffs) / len(diffs), 5), "top1_reference": top_ref,
                          "top1_candidate": top_cand, "clear_swaps": clear_swaps, "pass": q_ok}
    return {"thresholds": {"spearman_min": spearman_min, "max_abs_diff": max_abs_diff, "top1": "equal"},
            "n_queries": len(per_query), "n_pairs": len(all_diffs),
            "max_abs_diff": round(max(all_diffs), 5) if all_diffs else None,
            "mean_abs_diff": round(sum(all_diffs) / len(all_diffs), 5) if all_diffs else None,
            "min_spearman": round(min(q["spearman"] for q in per_query.values()), 4) if per_query else None,
            "per_query": per_query, "verdict": "PASS" if ok else "FAIL"}


def reference_scores(model_dir: str, images: dict[str, bytes], queries: dict[str, str], *, device: str = "cuda",
                     dtype: str = "bfloat16", batch_size: int = 1) -> dict[str, dict[str, float]]:
    """transformers reference: ``JinaVLForRanking.compute_score`` with ``doc_type='image'`` on normalised PNGs.

    ``batch_size = 1`` avoids padding (the model reads the hidden state of the last position)."""
    import io

    import torch
    from PIL import Image
    from transformers import AutoModel, AutoProcessor

    model = AutoModel.from_pretrained(model_dir, trust_remote_code=True, dtype=getattr(torch, dtype))
    model.to(device).eval()
    # the processor the checkpoint was saved with (slow Qwen2VLImageProcessor), same pixel bounds as compute_score
    model._processor = AutoProcessor.from_pretrained(model_dir, max_pixels=602112, min_pixels=3136,
                                                     trust_remote_code=True, use_fast=False)
    pil = {name: Image.open(io.BytesIO(png)).convert("RGB") for name, png in images.items()}
    out: dict[str, dict[str, float]] = {}
    names = sorted(pil)
    for qid, query in queries.items():
        pairs = [[query, pil[n]] for n in names]
        scores = model.compute_score(pairs, doc_type="image", batch_size=batch_size)
        if not isinstance(scores, list):
            scores = [scores]
        out[qid] = {n: float(s) for n, s in zip(names, scores)}
    del model
    if device.startswith("cuda"):
        torch.cuda.empty_cache()
    return out


def llama_scores(base_url: str, images: dict[str, str], queries: dict[str, str], head, *,
                 timeout_s: float = 600.0) -> tuple[dict[str, dict[str, float]], dict[str, list[float]]]:
    """llama-server ``/embedding`` (multimodal, last pooling, no normalisation) + MLP head.

    ``images``: name → base64 PNG (normalised). Returns scores and the raw hidden states keyed ``qid/name``.
    """
    import httpx

    from vkm_corpus.retrieval.backends import m0_prompt

    names = sorted(images)
    scores: dict[str, dict[str, float]] = {}
    hidden: dict[str, list[float]] = {}
    with httpx.Client(base_url=base_url, timeout=timeout_s, trust_env=False) as client:
        props = client.get("/props")
        props.raise_for_status()
        marker = props.json()["media_marker"]
        for qid, query in queries.items():
            content = [{"prompt_string": m0_prompt(query, marker), "multimodal_data": [images[n]]} for n in names]
            resp = client.post("/embedding", json={"content": content, "embd_normalize": -1})
            resp.raise_for_status()
            vecs = {int(item["index"]): item["embedding"] for item in resp.json()}
            rows = []
            for i, n in enumerate(names):
                v = vecs[i]
                v = v[-1] if v and isinstance(v[0], list) else v
                rows.append(v)
                hidden[f"{qid}/{n}"] = v
            s = head.scores(rows)
            scores[qid] = {n: float(x) for n, x in zip(names, s)}
    return scores, hidden
