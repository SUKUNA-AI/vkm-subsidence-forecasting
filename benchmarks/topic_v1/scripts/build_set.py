"""Build the frozen topic set of TOPIC_BENCHMARK_V1 from the human evidence catalogues.

Inputs (read only):
    catalogues/physics/physics_coverage_and_execution_matrix.csv   processes PC-xx: name, group
    catalogues/physics/process_evidence_links.csv                  evidence pages of each process (source_id, pdf_page)
    catalogues/mathematics/MATHEMATICAL_MODEL_REGISTRY.csv         model families MM-<FAMILY>: locators «VKM-SRC-xxx p.N»
    benchmarks/topic_v1/topic_queries_v1.tsv                       paraphrases (and family titles) written for the set
    --canon-db                                                     canonical DuckDB copy of the snapshot (pages, formats)
    $VKM_RESOURCES_ROOT/11_evidence_vnext                          PRIVATE evidence records: quotes for page anchoring

Page mapping rule (checked here, see page_mapping_v1.json): the catalogues' ``pdf_page`` is the physical page of the
file = ``canonical.pages.page_index`` (identity) for PDF and DjVu sources, 2-up spreads included (one canonical page per
scanned spread). The rule is checked by anchoring every PRIVATE quote on the canonical text of pages pdf_page−3…+3
(token containment). A confident anchor on another page (score ≥ 0.6 and ≥ 0.2 above the identity page) adds that page
as an acceptable alternate. DOCX VKM-SRC-023 is paginated by a pinned render (RENDER_DEPENDENT): its target page is the
anchored page, or — for quotes that cannot be anchored — the page shifted by the drift of the nearest anchored record.
Pages of a duplicate group (``duplicate_page_candidates``) are acceptable alternates too.

Outputs: topic_set_v1.jsonl (one topic per line: queries + targets, IDs only) and page_mapping_v1.json (counts).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

BENCH = Path(__file__).resolve().parents[1]
ROOT = BENCH.parents[1]
WINDOW = 3
ANCHOR_MIN = 0.6
ANCHOR_MARGIN = 0.2
EXCLUDED_FAMILIES = {"PDE": "textbook equations, no corpus locator", "DER": "derived by this project, not a corpus topic"}
LOCATOR = re.compile(r"(VKM-SRC-\d{3})\s+p\.\s?(\d+)")


def norm_tokens(s: str | None) -> list[str]:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"-\s*\n\s*", "", s)
    return re.sub(r"[^0-9a-zа-я]+", " ", s).split()


def containment(q: list[str], page: set[str]) -> float | None:
    toks = {t for t in q if len(t) >= 4}
    if len(toks) < 3:
        return None
    return sum(1 for t in toks if t in page) / len(toks)


def write_lf(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canon-db", required=True)
    ap.add_argument("--out-dir", default=str(BENCH))
    args = ap.parse_args()
    priv = Path(os.environ["VKM_RESOURCES_ROOT"]) / "11_evidence_vnext"
    cat = ROOT / "catalogues"

    matrix = list(csv.DictReader(open(cat / "physics" / "physics_coverage_and_execution_matrix.csv", encoding="utf-8")))
    links = list(csv.DictReader(open(cat / "physics" / "process_evidence_links.csv", encoding="utf-8")))
    registry = list(csv.DictReader(open(cat / "mathematics" / "MATHEMATICAL_MODEL_REGISTRY.csv", encoding="utf-8")))
    paras: dict[str, dict[str, str]] = defaultdict(dict)
    with open(BENCH / "topic_queries_v1.tsv", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            paras[row["topic_id"]][row["variant"]] = row["text"].strip()

    con = duckdb.connect(args.canon_db, read_only=True)
    snapshot = con.execute("SELECT snapshot_id FROM meta.snapshot").fetchone()[0]
    # --------------------------------------------------------------- page map of the evidence sources
    page_id: dict[tuple[str, int], str] = {}
    page_tokens: dict[tuple[str, int], set[str]] = {}
    fmt: dict[str, str] = {}
    spreads: Counter = Counter()
    evidence_sources = {r["source_id"] for r in links} | {s for m in registry for s, _ in LOCATOR.findall(m["locator"])}
    for s in sorted(evidence_sources):
        f = con.execute("SELECT format_detected FROM canonical.sources WHERE source_id = ?", [s]).fetchone()
        fmt[s] = f[0] if f else "MISSING"
        for pid, pidx, text, spread in con.execute(
                "SELECT page_id, page_index, normalized_text, is_spread FROM canonical.pages WHERE source_id = ?",
                [s]).fetchall():
            page_id[(s, pidx)] = pid
            page_tokens[(s, pidx)] = set(norm_tokens(text))
            spreads[s] += 1 if spread else 0
    dup_of: dict[str, list[str]] = defaultdict(list)
    for gid, pages in con.execute("SELECT dup_group_id, list(page_id ORDER BY page_id) FROM "
                                  "main.duplicate_page_candidates GROUP BY 1").fetchall():
        for p in pages:
            dup_of[p] = [x for x in pages if x != p]

    # --------------------------------------------------------------- quote anchoring (PRIVATE quotes, no output text)
    vn_index = {r["vn_id"]: r for r in csv.DictReader(open(priv / "merged" / "vn_index.csv", encoding="utf-8"))}
    cache: dict[str, list[str]] = {}

    def record(vn: str) -> dict | None:
        r = vn_index.get(vn)
        if not r:
            return None
        p = priv / "sweep_raw" / r["dir"] / "records.jsonl"
        key = str(p)
        if key not in cache:
            cache[key] = p.read_text(encoding="utf-8").splitlines()
        return json.loads(cache[key][int(r["line"]) - 1])

    vn_pages: dict[str, tuple[str, int]] = {}
    for l in links:
        vn_pages[l["vn_id"]] = (l["source_id"], int(l["pdf_page"]))
    for m in registry:
        for vn in (v.strip() for v in m["vn_ids"].split(";")):
            if vn.startswith("EV-VN-") and vn not in vn_pages and vn in vn_index:
                vn_pages[vn] = (vn_index[vn]["source_id"], int(vn_index[vn]["pdf_page"]))
    anchor: dict[str, dict] = {}
    for vn, (s, p) in sorted(vn_pages.items()):
        rec = record(vn)
        if rec is None:
            anchor[vn] = {"status": "NO_RECORD"}
            continue
        q = norm_tokens(rec.get("quote"))
        scores = {}
        for off in range(-WINDOW, WINDOW + 1):
            toks = page_tokens.get((s, p + off))
            if toks is not None:
                sc = containment(q, toks)
                if sc is not None:
                    scores[off] = sc
        if not scores:
            anchor[vn] = {"status": "NO_SCORE"}
            continue
        best = max(scores.values())
        off = sorted((o for o, v in scores.items() if v == best), key=lambda o: (abs(o), o))[0]
        s0 = scores.get(0, 0.0)
        if best >= ANCHOR_MIN and off != 0 and best - s0 >= ANCHOR_MARGIN:
            anchor[vn] = {"status": "ANCHORED", "offset": off}
        elif s0 >= ANCHOR_MIN and s0 == best:
            anchor[vn] = {"status": "MATCH0", "offset": 0}
        else:
            anchor[vn] = {"status": "WEAK"}
    by_page: dict[tuple[str, int], list[str]] = defaultdict(list)
    for vn, sp in vn_pages.items():
        by_page[sp].append(vn)
    located = defaultdict(list)            # source -> [(pdf_page, offset)] of confident locations (for the DOCX drift)
    for vn, a in anchor.items():
        if a["status"] in ("ANCHORED", "MATCH0"):
            s, p = vn_pages[vn]
            located[s].append((p, a["offset"]))

    def resolve(s: str, p: int) -> tuple[str, list[str], str] | None:
        """(primary page id, alternates, mapping) of a catalogue page, None when the page is not in the canon."""
        offs = sorted({anchor[vn]["offset"] for vn in by_page.get((s, p), []) if anchor.get(vn, {}).get("status") == "ANCHORED"})
        matched0 = any(anchor.get(vn, {}).get("status") == "MATCH0" for vn in by_page.get((s, p), []))
        if fmt.get(s) == "DOCX":
            if offs and not matched0:
                prim, alts, how = p + offs[0], [p + o for o in offs[1:]], "DOCX_ANCHOR"
            elif matched0:
                prim, alts, how = p, [p + o for o in offs], "IDENTITY"
            else:
                near = sorted(located.get(s, []), key=lambda x: (abs(x[0] - p), x[0]))
                d = near[0][1] if near else 0
                prim, alts, how = (p + d, [p], "DOCX_DRIFT") if d else (p, [], "IDENTITY")
        else:
            prim, alts, how = p, [p + o for o in offs], ("IDENTITY+ANCHOR_ALT" if offs else "IDENTITY")
        if (s, prim) not in page_id:
            if (s, p) not in page_id:
                return None
            prim, alts, how = p, [], "IDENTITY"
        alt_ids = [page_id[(s, a)] for a in alts if (s, a) in page_id and a != prim]
        pid = page_id[(s, prim)]
        for x in [pid, *alt_ids]:
            alt_ids += [d for d in dup_of.get(x, []) if d != pid and d not in alt_ids]
        return pid, alt_ids, how

    # --------------------------------------------------------------- topics
    topics: list[dict] = []
    dropped: list[dict] = []
    mapping_counts: Counter = Counter()

    def targets_of(topic_id: str, pages: list[tuple[str, int, int, list[str], list[str]]]) -> list[dict]:
        merged: dict[str, dict] = {}
        for s, p, grade, basis, kinds in pages:
            res = resolve(s, p)
            if res is None:
                dropped.append({"topic_id": topic_id, "source_id": s, "pdf_page": p, "reason": "page not in canon"})
                continue
            pid, alts, how = res
            t = merged.setdefault(pid, {"page_id": pid, "source_id": s, "alt_page_ids": [], "grade": grade,
                                        "mapping": how, "basis": [], "kinds": []})
            t["grade"] = max(t["grade"], grade)
            t["alt_page_ids"] += [a for a in alts if a not in t["alt_page_ids"]]
            t["basis"] += [b for b in basis if b not in t["basis"]]
            t["kinds"] += [k for k in kinds if k not in t["kinds"]]
        out = sorted(merged.values(), key=lambda t: t["page_id"])
        for i, t in enumerate(out, 1):
            t["target_id"] = f"{topic_id}/T{i:02d}"
            mapping_counts[t["mapping"]] += 1
        return [{k: t[k] for k in ("target_id", "page_id", "source_id", "alt_page_ids", "grade", "mapping", "basis",
                                   "kinds")} for t in out]

    by_process: dict[str, list] = defaultdict(list)
    for l in links:
        by_process[l["process_id"]].append((l["source_id"], int(l["pdf_page"]), 2 if l["role"] == "KEY" else 1,
                                            [l["vn_id"]], [f"{l['kind']}:{l['extraction_method']}"]))
    for r in matrix:
        tid = r["process_id"]
        texts = {"NAME": r["process"].strip(), **paras[tid]}
        topics.append({"topic_id": tid, "track": "PROCESS", "group": r["group"], "domain": r["domain"],
                       "title": r["process"].strip(),
                       "queries": [{"query_id": f"TQ-{tid}-{i}", "variant": v, "text": texts[v]}
                                   for i, v in enumerate(("NAME", "PARA1", "PARA2"))],
                       "targets": targets_of(tid, by_process[tid])})
    fam_pages: dict[str, list] = defaultdict(list)
    fam_order: list[str] = []
    for m in registry:
        fam = re.match(r"^MM-([A-Z]+)", m["model_id"]).group(1)
        if fam in EXCLUDED_FAMILIES:
            continue
        if fam not in fam_order:
            fam_order.append(fam)
        methods = [x for x in m["extraction_methods"].split(";") if x]
        for s, p in LOCATOR.findall(m["locator"]):
            fam_pages[fam].append((s, int(p), 1, [m["model_id"]], [f"formula:{x}" for x in methods] or ["formula:"]))
    for fam in sorted(fam_order):
        tid = f"MM-{fam}"
        if not fam_pages[fam]:
            dropped.append({"topic_id": tid, "reason": "no VKM-SRC locator"})
            continue
        texts = paras[tid]
        topics.append({"topic_id": tid, "track": "MODEL_FAMILY", "group": "MM", "domain": None,
                       "title": texts["NAME"],
                       "queries": [{"query_id": f"TQ-{tid}-{i}", "variant": v, "text": texts[v]}
                                   for i, v in enumerate(("NAME", "PARA1", "PARA2"))],
                       "targets": targets_of(tid, fam_pages[fam])})
    missing = [t["topic_id"] for t in topics if not all(q["text"] for q in t["queries"])]
    if missing:
        raise SystemExit(f"queries missing for {missing}")
    for t in topics:
        t["n_targets"] = len(t["targets"])
        t["sources"] = sorted({x["source_id"] for x in t["targets"]})
        t["set_version"] = "topic_v1"

    out = Path(args.out_dir)
    write_lf(out / "topic_set_v1.jsonl", "".join(json.dumps(t, ensure_ascii=False, sort_keys=True) + "\n"
                                                 for t in topics))
    per_source: dict[str, Counter] = defaultdict(Counter)
    offsets: dict[str, Counter] = defaultdict(Counter)
    for vn, a in anchor.items():
        s = vn_pages[vn][0]
        per_source[s][a["status"]] += 1
        if a["status"] == "ANCHORED":
            offsets[s][str(a["offset"])] += 1
    receipt = {
        "receipt": "vkm.topic_benchmark_page_mapping/1",
        "canonical_snapshot_id": snapshot,
        "rule": ("pdf_page = canonical page_index (identity) for PDF and DJVU sources, 2-up spreads included; "
                 f"quote anchoring on pages pdf_page-{WINDOW}..+{WINDOW} (token containment >= {ANCHOR_MIN}, margin "
                 f">= {ANCHOR_MARGIN} over the identity page) adds the anchored page as an alternate; DOCX (render-"
                 "dependent pagination): anchored page, else the drift of the nearest confidently located record; "
                 "duplicate-group pages are alternates"),
        "records_checked": len(anchor),
        "status_totals": dict(Counter(a["status"] for a in anchor.values())),
        "identity_share_of_scoreable": round(
            sum(1 for a in anchor.values() if a["status"] == "MATCH0")
            / max(1, sum(1 for a in anchor.values() if a["status"] in ("MATCH0", "ANCHORED", "WEAK"))), 4),
        "per_source": {s: {"format": fmt.get(s), "spread_pages": spreads[s], **dict(per_source[s]),
                           "anchored_offsets": dict(offsets[s])} for s in sorted(per_source)},
        "set": {"topics": len(topics), "queries": 3 * len(topics),
                "topics_by_track": dict(Counter(t["track"] for t in topics)),
                "targets": sum(t["n_targets"] for t in topics),
                "targets_by_mapping": dict(mapping_counts),
                "targets_with_alternates": sum(1 for t in topics for x in t["targets"] if x["alt_page_ids"]),
                "sources": len({s for t in topics for s in t["sources"]}),
                "excluded_families": EXCLUDED_FAMILIES, "dropped": dropped},
    }
    write_lf(out / "page_mapping_v1.json", json.dumps(receipt, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: receipt[k] for k in ("records_checked", "status_totals", "identity_share_of_scoreable", "set")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
