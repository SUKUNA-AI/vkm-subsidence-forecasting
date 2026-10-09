#!/usr/bin/env python3
"""World passport extraction, step 2a: build packets of selected pages (``pages.csv`` of step 1) from the NAV page
text and tables, plus the task text and the answer schema of the producer.

Writes into ``--out``: ``packets/<packet_id>.json`` (manifest, header, page texts, context), ``packets.jsonl``
(manifests), ``task.txt``, ``schema.json`` and ``packets_receipt.json``.

Usage:  python scripts/extract_build_packets.py --nav <nav_pages.duckdb> --pages <pages.csv> --out <dir>
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkm_world.extraction import packets as P            # noqa: E402
from vkm_world.extraction import schema as S             # noqa: E402

RULE_VERSION = "extract_build_packets_v1"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nav", required=True, type=Path)
    ap.add_argument("--pages", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pin", type=Path, help="JSON list of packet ids kept byte-identical from --out/packets "
                                              "(packets already given to a run or to the double entry)")
    a = ap.parse_args()
    import duckdb
    c = duckdb.connect(str(a.nav), read_only=True)
    rows = list(csv.DictReader(open(a.pages, encoding="utf-8")))
    pinned = {}
    if a.pin:
        for pid in json.load(open(a.pin, encoding="utf-8")):
            pinned[pid] = json.loads((a.out / "packets" / f"{pid}.json").read_text(encoding="utf-8"))
    pinned_pages = {x for pk in pinned.values() for x in pk["manifest"]["page_ids"]}
    rows = [r for r in rows if r["page_id"] not in pinned_pages]
    sel = {r["page_id"] for r in rows}
    srcs = {r["source_id"] for r in rows}
    q = ",".join("?" * len(srcs))
    pages = {pid: (idx, txt, pp) for pid, idx, txt, pp in c.execute(
        f"select page_id, page_index, normalized_text, printed_page_raw from pages where source_id in ({q})",
        list(srcs)).fetchall()}
    tabs = defaultdict(list)
    for pid, lab, cap, txt in c.execute(
            f"select page_id, table_label, caption, normalized_text from tables where is_primary_layer and "
            f"source_id in ({q}) order by page_id, object_id", list(srcs)).fetchall():
        tabs[pid].append((lab, cap, txt))
    figcap = defaultdict(list)
    for pid, lab, cap in c.execute(
            f"select page_id, figure_label, caption from figures where source_id in ({q}) and caption is not null "
            f"order by page_id, object_id", list(srcs)).fetchall():
        figcap[pid].append(" ".join(x for x in (lab, cap) if x)[:200])
    for pid, lst in tabs.items():
        for lab, cap, _ in lst:
            if lab or cap:
                figcap[pid].append(" ".join(x for x in (lab, cap) if x)[:200])
    meta = {}
    for sid, scope, cls, title, authors, year in c.execute(
            f"""select s.source_id, s.site_scope, s.source_class_raw, w.title, w.authors_display, w.publication_year
                from sources s left join work_sources ws on ws.source_id = s.source_id and ws.is_primary
                left join works w on w.work_id = ws.work_id where s.source_id in ({q})""", list(srcs)).fetchall():
        meta.setdefault(sid, (scope, cls, title, authors, year))
    snap = c.execute("select snapshot_id, manifest_sha256 from snapshot").fetchone()
    by_idx = {(pid.split(":")[0], idx): pid for pid, (idx, _t, _p) in pages.items()}

    texts = {pid: P.page_text(pages[pid][1], tabs.get(pid, [])) for pid in sel}
    tier_of = {r["page_id"]: r["tier"] for r in rows}
    groups = P.group_pages(rows, texts)
    out = a.out
    (out / "packets").mkdir(parents=True, exist_ok=True)
    keep = {f"{pid}.{ext}" for pid in pinned for ext in ("json", "txt")}
    for f in (out / "packets").iterdir():
        if f.name not in keep:
            f.unlink()
    manifests = [pk["manifest"] for pk in pinned.values()]
    mixed_no = 0
    for g in groups:
        ids = [r["page_id"] for r in g]
        sids = list(dict.fromkeys(r["source_id"] for r in g))
        context, headers = {}, {}
        for r in g:
            sid, i = r["source_id"], int(r["page_index"])
            for j in (i - 1, i + 1):
                pid = by_idx.get((sid, j))
                if pid and pid not in sel:
                    context[pid] = P.context_lines(pages[pid][1], figcap.get(pid, []))
        for sid in sids:
            scope, cls, title, authors, year = meta.get(sid, ("", "", None, None, None))
            scope = ", ".join(re.findall(r"[A-Z0-9_]+", scope or ""))
            printed = "; ".join(f"{pid} → печ. {pages[pid][2]}" for pid in ids
                                if pid.startswith(sid + ":") and pages[pid][2])
            headers[sid] = ((f"{authors}" if authors else "") + (f", «{title}»" if title else "")
                            + (f" ({year})" if year else "") + (f"; тип: {cls}" if cls else "") + "\n"
                            + f"Площадка источника по реестру корпуса: {scope or 'не указана'} — это свойство файла, "
                              f"а не доказательство площадки конкретного числа."
                            + (f"\nПечатные номера страниц: {printed}" if printed else ""))
        tier = min(tier_of[x] for x in ids)
        if len(sids) > 1:
            mixed_no += 1
        pk = P.Packet(P.packet_id(g, mixed_no if len(sids) > 1 else None), tier, ids, {x: texts[x] for x in ids},
                      context, headers)
        (out / "packets" / f"{pk.packet_id}.json").write_text(P.to_json(pk) + "\n", encoding="utf-8")
        (out / "packets" / f"{pk.packet_id}.txt").write_bytes(pk.render().encode("utf-8"))
        manifests.append(pk.manifest())
    manifests.sort(key=lambda m: ("ABC".index(m["tier"]), m["packet_id"]))
    with open(out / "packets.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for m in manifests:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    (out / "task.txt").write_bytes(S.task_bytes())
    (out / "schema.json").write_bytes(S.schema_bytes())
    sizes = sorted(m["n_pages"] for m in manifests)
    receipt = {"rule_version": RULE_VERSION, "schema_version": S.SCHEMA_VERSION, "nav_snapshot": list(snap),
               "pages_csv_sha256": hashlib.sha256(a.pages.read_bytes()).hexdigest(),
               "task_sha256": S.sha256(S.task_bytes()), "schema_sha256": S.sha256(S.schema_bytes()),
               "n_packets": len(manifests), "n_pages": sum(sizes), "pinned_packets": sorted(pinned),
               "pinned_pages_not_in_selection": sorted(pinned_pages - {r["page_id"] for r in csv.DictReader(
                   open(a.pages, encoding="utf-8"))}),
               "by_tier": {t: sum(1 for m in manifests if m["tier"] == t) for t in "ABC"},
               "pages_per_packet": {"min": sizes[0], "median": sizes[len(sizes) // 2], "max": sizes[-1]},
               "chars_total": sum(m["chars"] for m in manifests),
               "limits": {"MAX_PAGES": P.MAX_PAGES, "MIN_PAGES": P.MIN_PAGES, "MAX_CHARS": P.MAX_CHARS},
               "mixed_packets": mixed_no}
    (out / "packets_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n",
                                              encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
