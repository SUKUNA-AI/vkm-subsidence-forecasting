"""Smoke Q5 in V1: does an explicit figure number («рис. 3.1 …») bring the captioned figure up?

Read-only probe against the VKM API: ``/v1/search`` (kinds FIGURE — the smoke Q5 request — and PAGE) and
``/v1/search/hybrid`` (PAGE, the served scheme with its default late stage). For each probe query the rank of the
target (a figure with the given label on the given page, or the page itself) is recorded with and without the number.
Targets are VERIFIED visual-track pages whose figure captions carry the number (IDs only; no corpus text is stored).

Environment: ``J_V1`` (output ``$J_V1/out/figure_number_probe.json``), ``VKM_API_TOKEN_FILE``.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_v1 import api_client, post_retry  # noqa: E402

V1 = Path(os.environ["J_V1"])
# (query with number, query without number, figure label, target page)
PROBES = [
    ("рис. 3.1 мульда сдвижения", "мульда сдвижения", "3.1", None),                   # smoke Q5 as defined
    ("рис. 1.4 геологический разрез Соликамской впадины", "геологический разрез Соликамской впадины", "1.4",
     "VKM-SRC-047:p0012"),
    ("рисунок 2.10 оседания по профильной линии № 8", "оседания по профильной линии № 8", "2.10", "VKM-SRC-012:p0052"),
    ("рисунок 4.12 расчётные и замеренные оседания земной поверхности", "расчётные и замеренные оседания земной поверхности",
     "4.12", "VKM-SRC-012:p0107"),
    ("рисунок 3.3 конечно-элементная схема камер", "конечно-элементная схема камер", "3.3", "VKM-SRC-045:p0064"),
    ("рис. 4.1 технология отработки пластов АБ и Красный-II на руднике СКРУ-1",
     "технология отработки пластов АБ и Красный-II на руднике СКРУ-1", "4.1", "VKM-SRC-014:p0114"),
    ("рис. 6.3 кривые ползучести соляных пород при различных уровнях нагружения",
     "кривые ползучести соляных пород при различных уровнях нагружения", "6.3", "VKM-SRC-202:p0155"),
    ("рис. 1.4 структурный план Верхнекамского месторождения", "структурный план Верхнекамского месторождения", "1.4",
     "VKM-SRC-037:p0007"),
]


def page_of(obj_id: str | None) -> str | None:
    if not obj_id:
        return None
    parts = obj_id.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else obj_id


LABEL_RE = re.compile(r"(?:рис(?:унок)?|fig(?:ure)?)\.?\s*(\d+(?:\.\d+)*)", re.IGNORECASE)


def items(resp: dict) -> list[dict]:
    """IDs, type, page and the figure number: the record's ``object_number`` (API with object labels, agent L), else
    parsed from the caption (the caption itself is not kept)."""
    out = []
    for it in resp.get("items") or []:
        env, rec = it.get("envelope") or {}, it.get("record") or {}
        label = rec.get("object_number")
        if label is None:
            m = LABEL_RE.search(rec.get("title_or_caption") or "")
            label = m.group(1) if m else None
        out.append({"id": env.get("object_id"), "type": rec.get("object_type"), "label": label,
                    "page": env.get("page_id") or page_of(env.get("object_id"))})
    return out


def rank(hits: list[dict], pred) -> int | None:
    for i, h in enumerate(hits, 1):
        if pred(h):
            return i
    return None


def main() -> None:
    client = api_client()
    res = []
    first_item_keys = None
    for with_no, without_no, label, page in PROBES:
        row = {"query": with_no, "label": label, "target_page": page}
        for tag, q in (("with_number", with_no), ("without_number", without_no)):
            r = post_retry(client, "/v1/search", {"query": q, "kinds": ["FIGURE"], "limit": 20})
            hits = items(r.json()) if r.status_code == 200 else []
            if first_item_keys is None and r.status_code == 200 and (r.json().get("items") or []):
                it = r.json()["items"][0]
                first_item_keys = {"envelope": sorted((it.get("envelope") or {}).keys()),
                                   "record": sorted((it.get("record") or {}).keys())}
            lab_rank = rank(hits, lambda h: str(h.get("label")) == label)
            tgt_rank = rank(hits, lambda h: page and h.get("page") == page) if page else None
            tgt_lab = rank(hits, lambda h: page and h.get("page") == page and str(h.get("label")) == label) if page else None
            pr = post_retry(client, "/v1/search", {"query": q, "kinds": ["PAGE"], "limit": 20})
            ph = [h.get("id") for h in items(pr.json())] if pr.status_code == 200 else []
            hr = post_retry(client, "/v1/search/hybrid", {"query": q, "kinds": ["PAGE"], "limit": 20, "candidates": 100})
            hh = [h.get("id") for h in items(hr.json())] if hr.status_code == 200 else []
            row[tag] = {"figure_status": r.status_code, "first_label_rank": lab_rank, "target_page_figure_rank": tgt_rank,
                        "target_figure_with_label_rank": tgt_lab,
                        "top5_labels": [h.get("label") for h in hits[:5]],
                        "page_bm25_rank": (ph.index(page) + 1) if page in ph else None,
                        "page_hybrid_late_rank": (hh.index(page) + 1) if page in hh else None}
        res.append(row)
        print(json.dumps(row, ensure_ascii=False))
    out = {"api_item_keys": first_item_keys, "probes": res}
    (V1 / "out" / "figure_number_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("keys", first_item_keys)


if __name__ == "__main__":
    main()
