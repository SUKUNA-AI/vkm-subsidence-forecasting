"""Scenario B of CP-22: quality of embedded (foreign) OCR layers.

* sample: ≈5 % of the embedded-layer pages of each source, evenly spread over the source (deterministic);
* metric ``cer_v1``: character error rate of the layer against GLM-OCR on the same page (both through
  ``normalize_text_v1``, case-folded, ё→е, whitespace removed); plus the letter share of the layer;
* decision per source: re-OCR the whole source when the sample CER > 10 % or the layer's letter share < 0.6, within
  the scenario-B GPU budget; the decision and the numbers go to the receipt. When both layers exist, the primary one
  is chosen by this rule (``is_primary_layer``); their disagreement is the metric ``native_ocr_cer``, not a status.
"""
from __future__ import annotations

import unicodedata
from typing import Any

CER_RULE = "cer_v1"


def sample_pages(pages: list[int], share: float, min_pages: int = 1) -> list[int]:
    """Evenly spaced sample (the centre of each of k equal strata)."""
    n = len(pages)
    if n == 0:
        return []
    k = min(n, max(min_pages, round(n * share)))
    ordered = sorted(pages)
    return sorted({ordered[min(n - 1, int((i + 0.5) * n / k))] for i in range(k)})


def cer_text(text: str | None) -> str:
    from vkm_corpus.contracts.text_rules import normalize_text_v1

    s = normalize_text_v1(text or "")
    s = unicodedata.normalize("NFC", s).casefold().replace("ё", "е")
    return "".join(s.split())


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance with a vectorised row update (numpy)."""
    import numpy as np

    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    bb = np.frombuffer(b.encode("utf-32-le"), dtype=np.uint32)
    prev = np.arange(len(b) + 1, dtype=np.int64)
    ar = np.arange(len(b) + 1, dtype=np.int64)
    for ch in a:
        c = ord(ch)
        sub = prev[:-1] + (bb != c)
        dele = prev[1:] + 1
        t = np.empty_like(prev)
        t[0] = prev[0] + 1
        t[1:] = np.minimum(sub, dele)
        cur = np.minimum.accumulate(t - ar) + ar
        prev = cur
    return int(prev[-1])


def cer(hyp: str | None, ref: str | None) -> tuple[float | None, int, int]:
    """(CER of ``hyp`` against ``ref``, edit distance, reference length) on ``cer_text`` forms."""
    h, r = cer_text(hyp), cer_text(ref)
    if not r:
        return None, len(h), 0
    d = edit_distance(h, r)
    return d / len(r), d, len(r)


def decide(sample_rows: list[dict[str, Any]], cer_threshold: float, letter_min: float) -> dict[str, Any]:
    """Source decision from sample rows ``{page_index, distance, ref_len, layer_letter_share}``."""
    usable = [r for r in sample_rows if r.get("ref_len")]
    dist = sum(r["distance"] for r in usable)
    ref = sum(r["ref_len"] for r in usable)
    agg = dist / ref if ref else None
    letters = [r["layer_letter_share"] for r in sample_rows if r.get("layer_letter_share") is not None]
    letter_share = sum(letters) / len(letters) if letters else None
    reasons = []
    if agg is not None and agg > cer_threshold:
        reasons.append("CER_ABOVE_THRESHOLD")
    if letter_share is not None and letter_share < letter_min:
        reasons.append("LETTER_SHARE_BELOW_MIN")
    return {"rule": CER_RULE, "sample_pages": len(sample_rows), "usable_pages": len(usable), "cer": agg,
            "layer_letter_share": letter_share, "cer_threshold": cer_threshold, "letter_share_min": letter_min,
            "decision": "REOCR_SOURCE" if reasons else ("KEEP_EMBEDDED_LAYER" if usable else "UNDECIDED"),
            "reasons": reasons}
