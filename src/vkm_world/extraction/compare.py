"""Agreement of two extractions of the same pages (Sol high vs medium, Sol vs the independent double entry).

Records are matched per page by their printed numbers (value_min, value_max) and unit; records without a number
(entities, citations, laws without values) by kind and name. Agreement is reported separately for the number and
for the attribution: rock / layer, scale, site, parameter code. Disagreements are listed for review, never averaged.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict

ATTRIBUTION = ("parameter_code", "scale", "site_norm", "material")


def _norm(s: str | None) -> str:
    t = unicodedata.normalize("NFKC", s or "").casefold().replace("ё", "е")
    return re.sub(r"[\s\-–—.,;:()«»\"']+", " ", t).strip()


def material_key(s: str | None) -> str:
    """Rock / layer as a comparable key: lower case, no punctuation, word stems of 4 letters."""
    words = [w[:4] for w in _norm(s).split() if len(w) > 2]
    return " ".join(sorted(set(words)))


def _same_num(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)


def _unit(s: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).casefold()


def _material_agree(a: str | None, b: str | None) -> bool:
    ka, kb = set(material_key(a).split()), set(material_key(b).split())
    if not ka and not kb:
        return True
    if not ka or not kb:
        return False
    return len(ka & kb) / min(len(ka), len(kb)) >= 0.5


def match(a: list[dict], b: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Greedy one-to-one matching per page: numeric records by (min, max, unit), then by (min, max) alone;
    records without numbers by (kind, entity/parameter name)."""
    pairs, used_b = [], set()
    by_page = defaultdict(list)
    for j, r in enumerate(b):
        by_page[r.get("page_id")].append(j)
    rest_a = []
    for strict in (True, False):
        nxt = []
        for r in (a if strict else rest_a):
            hit = None
            for j in by_page.get(r.get("page_id"), ()):
                if j in used_b:
                    continue
                s = b[j]
                if r.get("value_min") is not None or r.get("value_max") is not None:
                    if not (_same_num(r.get("value_min"), s.get("value_min")) and
                            _same_num(r.get("value_max"), s.get("value_max"))):
                        continue
                    if strict and _unit(r.get("unit_as_printed")) != _unit(s.get("unit_as_printed")):
                        continue
                else:
                    if s.get("value_min") is not None or s.get("value_max") is not None:
                        continue
                    if r.get("kind") != s.get("kind"):
                        continue
                    na = _norm(r.get("entity_name") or r.get("parameter") or r.get("time_as_printed"))
                    nb = _norm(s.get("entity_name") or s.get("parameter") or s.get("time_as_printed"))
                    if not na or na != nb:
                        continue
                hit = j
                break
            if hit is None:
                nxt.append(r)
            else:
                used_b.add(hit)
                pairs.append((r, b[hit]))
        rest_a = nxt
    only_b = [s for j, s in enumerate(b) if j not in used_b]
    return pairs, rest_a, only_b


def attribution_diff(r: dict, s: dict) -> list[str]:
    """Fields that differ; ``material_one_side`` — the rock / layer is given by one producer only (weaker than a
    contradiction ``material``)."""
    out = []
    for f in ("parameter_code", "scale", "site_norm"):
        if (r.get(f) or "") != (s.get(f) or ""):
            out.append(f)
    ka, kb = material_key(r.get("material_as_printed")), material_key(s.get("material_as_printed"))
    if bool(ka) != bool(kb):
        out.append("material_one_side")
    elif not _material_agree(r.get("material_as_printed"), s.get("material_as_printed")):
        out.append("material")
    return out


def agreement(a: list[dict], b: list[dict]) -> dict:
    """Summary of B against A (A = reference run)."""
    pairs, only_a, only_b = match(a, b)
    num_a = [r for r in a if r.get("value_min") is not None]
    num_b = [r for r in b if r.get("value_min") is not None]
    num_pairs = [(r, s) for r, s in pairs if r.get("value_min") is not None]
    diffs = defaultdict(int)
    full = strict = 0
    for r, s in num_pairs:
        d = attribution_diff(r, s)
        for f in d:
            diffs[f] += 1
        full += not d
        strict += not [f for f in d if f != "material_one_side"]
    n = len(num_pairs)
    return {"records_a": len(a), "records_b": len(b), "numeric_a": len(num_a), "numeric_b": len(num_b),
            "matched": len(pairs), "matched_numeric": n,
            "number_recall_b_vs_a": round(n / len(num_a), 3) if num_a else None,
            "number_precision_b_vs_a": round(n / len(num_b), 3) if num_b else None,
            "attribution_full_agreement": round(full / n, 3) if n else None,
            "attribution_no_contradiction": round(strict / n, 3) if n else None,
            "attribution_disagreements": dict(sorted(diffs.items())),
            "only_a": len(only_a), "only_b": len(only_b)}
