"""TOPIC_BENCHMARK_V1 — delta pool T2 (protocol: ``benchmarks/topic_v1/POOL_LABELING_T2.md``).

T1 (``qrels_topic_v1_pooled.tsv``, ``LLM_AGENT_T1``) labelled the first 10 pages of five systems on the snapshot
``snap-20260928T160616Z-5d669f09``. CORE now serves ``snap-20260929T175107Z-574daaac``: 20 new sources
(VKM-SRC-252…271) and a drifted ranking. T2 labels the topic × page pairs that the systems bring now and that carry no
judgment yet. Unit, scale, reason codes, duplicate groups, packets, text windows and ingest checks are those of T1
(POOL_LABELING_V1.md §3–§6 and the deviations of RESULTS_POOL_T1.md §10); the code is imported from ``pool_t1.py``.
Label source ``LLM_AGENT_T2``. The T1 files are never written.

    pool     rankings of the re-run on 574daaac (``bm25``, ``hybrid_late``, ``hybrid_nolate``, ``nav``: the frozen code
             of score.py) and of the experiment variant ``human3`` → the first 10 pages of each of the topic's three
             queries → minus catalogue targets of the topic (V1 rule) and pairs judged in T1 (any grade; duplicate
             aliases count) → duplicate groups (T1 rule) → label units with provenance ``system@rank`` →
             ``$LB_WORK/pool_t2.json`` (IDs only); control: the rankings reproduce the recorded V means of the re-run
             and of the experiment
    packets  blind packets ``$LB_WORK/packets/pk-NNN.md`` (T1 format; about UNITS_TARGET units each; pages in page-id
             order; no system names, no ranks) + ``topics_handbook.md`` + ``packets_index.json``; the cross-check
             packet ``$LB_WORK/xcheck/xc-001.md`` (XCHECK_T2 T2 units at random + T1 units stratified by grade as in
             POOL_LABELING_V1.md §8, seeded; no grades, no T1/T2 marks) and its key ``$LB_WORK/xcheck_key.json``
    status   judged / missing per packet
    ingest   ``$LB_WORK/judgments/*.txt`` (lines ``<topic_id>|<cid>|<grade>|<CODE>``) → the T1 checks with
             ``LLM_AGENT_T2`` (+ no pair judged in T1) → ``qrels_topic_v1_pooled_t2.tsv`` and
             ``qrels_topic_v1_pooled_t2_groups.json``
    xscore   agreement of the second labeller's file (lines ``<topic_id>|<xid>|<grade>|<CODE>``) with the key: exact,
             ±1, Cohen's kappa (quadratic and linear weights), Krippendorff's alpha (ordinal); T1 part against the T1
             grades, T2 part against the ingested T2 grades

Environment (all outside git): ``LB_WORK`` (work dir), ``CANON_DB`` (canonical DuckDB of 574daaac, read only),
``TD_WORK`` (TERM_DICTIONARY_V1 stage_a.json: the D1 translations used for the text windows, as in T1),
``NAV_SECTIONS`` (optional sections.parquet of the 574daaac NAV build: section titles in the packets),
``T2_RUNS`` / ``T2_EXPERIMENTS`` (default: the re-run and the experiment file under ``work/``).

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/pool_t2.py {pool,packets,status,ingest,xscore} …
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool_t1 as PT  # noqa: E402  (also puts <repo>/src on sys.path)
import score_t1 as ST  # noqa: E402

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402

REPO, BENCH = PT.REPO, PT.BENCH
LABEL_SOURCE = "LLM_AGENT_T2"
OUT_TSV = BENCH / "qrels_topic_v1_pooled_t2.tsv"
OUT_GROUPS = BENCH / "qrels_topic_v1_pooled_t2_groups.json"
H_SAMPLE = BENCH / "H_REVIEW_SAMPLE_T1.md"
POOL_FILE = "pool_t2.json"                                         # in $LB_WORK (pool_t3.py reuses this code)
SNAPSHOT = "snap-20260929T175107Z-574daaac"
RUN_SYSTEMS = ("bm25", "hybrid_late", "hybrid_nolate", "nav")    # the four topic_v1 pool systems, re-run on SNAPSHOT
EXPERIMENT_SYSTEMS = ("human3",)                                  # experiment of 05.10: +2 human formulations, RRF
SYSTEMS = RUN_SYSTEMS + EXPERIMENT_SYSTEMS
DEPTH = 10                                                        # first 10 pages of each of the three queries
NEW_SOURCES = frozenset(f"VKM-SRC-{n:03d}" for n in range(252, 272))   # sources added after 5d669f09
CID_PREFIX = "d"                                                  # T2 unit ids d001…; T1 used c001…
UNITS_TARGET, UNITS_MAX = 90, 100                                 # units per packet (one fresh labeller each)
XCHECK_NAME = "xc-001"
XCHECK_SEED = 20261005
XCHECK_T2 = 50                                                    # T2 units at random
XCHECK_T1_PER_GRADE = {3: 13, 2: 13, 1: 12, 0: 12}                # T1 units: §8 strata, 50 in all
DEFAULT_RUNS = REPO / "work" / "topic_v1_rerun_20261005" / "runs_v1_20261005.jsonl"
DEFAULT_EXPERIMENTS = REPO / "work" / "topic_v1_experiments_20261005" / "experiments_v1.jsonl"
CONTROL_RUNS_FILE = "score_20261005.json"            # next to the re-run file: V means of score_rerun.py
CONTROL_EXPERIMENTS_FILE = "experiments_v1_scored.json"   # next to the experiment file: score_experiments.py
CONTROL_METRICS = ("page_recall@10", "page_recall@20", "page_recall@50", "mrr@50", "success@10", "source_recall@10")
_PROVENANCE = re.compile(r"\b(?:" + "|".join(SYSTEMS + ("D1", "D0")) + r")@\d")


def run_paths() -> tuple[Path, Path]:
    return (Path(os.environ.get("T2_RUNS") or DEFAULT_RUNS).expanduser(),
            Path(os.environ.get("T2_EXPERIMENTS") or DEFAULT_EXPERIMENTS).expanduser())


def sha256_big(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def is_new_source(page_id: str) -> bool:
    return page_id.split(":")[0] in NEW_SOURCES


def load_t1() -> tuple[list[dict[str, str]], list[dict]]:
    return B.load_pooled_qrels(PT.OUT_TSV), json.load(open(PT.OUT_GROUPS, encoding="utf-8"))["groups"]


def load_pool(work: Path | None = None) -> dict:
    return json.load(open((work or PT.env_path("LB_WORK")) / POOL_FILE, encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ rankings
def load_rankings(runs_path: Path, exp_path: Path, systems: tuple[str, ...] = SYSTEMS
                  ) -> tuple[dict[str, dict[str, TB.Ranking]], list[dict]]:
    """Rankings of the pool systems with the frozen ``score.py`` code: ``ranking_of`` with the section index of the
    outlines of the same file (``nav`` reads its pages from the sections and term units, not from ``hits``); an
    experiment variant is an unknown system name → ``ranking_from_hits``, as in score_experiments.py."""
    score = PT.load_module("topic_v1_score", BENCH / "scripts" / "score.py")
    out: dict[str, dict[str, TB.Ranking]] = defaultdict(dict)
    info = []
    for path, group in ((runs_path, RUN_SYSTEMS), (exp_path, EXPERIMENT_SYSTEMS)):
        wanted = [s for s in group if s in systems]
        if not wanted:
            continue
        runs, outlines, meta, files = score.load_runs([str(path)])
        snaps = sorted({m[k] for m in meta for k in ("canonical_snapshot_id", "nav_snapshot_id") if m.get(k)})
        if snaps != [SNAPSHOT]:
            raise SystemExit(f"{path.name}: snapshots {snaps}, expected {SNAPSHOT}")
        index = TB.SectionIndex.from_outlines(outlines)
        errors: Counter = Counter()
        for (qid, system), line in runs.items():
            if system in wanted:
                out[system][qid] = score.ranking_of(line, index)
                errors[system] += 1 if line.get("error") else 0
        info.append({"file": path.name, "sha256": files[0]["sha256"], "lines": files[0]["lines"],
                     "snapshot_id": SNAPSHOT, "systems": wanted, "outline_sources": len(outlines),
                     "errors": {s: errors[s] for s in wanted}})
    return dict(out), info


def as_lists(rankings: dict[str, dict[str, TB.Ranking]]) -> dict[str, dict[str, list[tuple[str, frozenset[str]]]]]:
    """The shape ``pool_t1.collect_pool`` reads: per system and query, ``(page, its duplicate aliases incl. itself)``."""
    return {s: {q: list(zip(r.pages, r.aliases)) for q, r in rs.items()} for s, rs in rankings.items()}


def control(topics: list[TB.Topic], rankings: dict[str, dict[str, TB.Ranking]], runs_path: Path, exp_path: Path
            ) -> dict:
    """The rankings must reproduce the recorded V means (4 decimals): the re-run against score_20261005.json
    (``statistics.mean``, score_rerun.py), the experiment against experiments_v1_scored.json (``topic_bench.aggregate``)."""
    qmap = {q.query_id: t for t in topics for q in t.queries}
    recorded: dict[str, dict[str, float]] = {}
    mean_of: dict[str, object] = {}
    f = runs_path.parent / CONTROL_RUNS_FILE
    if f.is_file():
        v = json.load(open(f, encoding="utf-8"))["truths"]["V"]
        for s in RUN_SYSTEMS:
            if s in v:
                recorded[s] = {m: v[s][m]["now"] for m in CONTROL_METRICS}
                mean_of[s] = statistics.mean
    f = exp_path.parent / CONTROL_EXPERIMENTS_FILE
    if f.is_file():
        v = json.load(open(f, encoding="utf-8"))["summary"]
        for s in EXPERIMENT_SYSTEMS:
            if s in v:
                recorded[s] = {m: v[s][m] for m in CONTROL_METRICS}
                mean_of[s] = TB.mean
    checked, mismatches = 0, {}
    for s, rs in sorted(rankings.items()):
        if s not in recorded:
            continue
        rows = [TB.query_metrics(qmap[q], r) for q, r in rs.items()]
        for m in CONTROL_METRICS:
            checked += 1
            now = round(mean_of[s]([x[m] for x in rows]), 4)
            if recorded[s][m] is None or abs(now - recorded[s][m]) > 1e-9:
                mismatches[f"{s}|{m}"] = [now, recorded[s][m]]
    return {"values_checked": checked, "mismatches": mismatches,
            "systems_without_record": sorted(set(rankings) - set(recorded))}


# ------------------------------------------------------------------------------------------------ pool
def t1_judged(topics: list[TB.Topic], rows: list[dict[str, str]], groups: list[dict]) -> dict[str, frozenset[str]]:
    """Pages that carry a T1 judgment for each topic: ``score_t1.judged_pages`` (every T1 row of any grade with the
    pages and aliases of its unit) without the catalogue target pages (those are the V1 rule of ``collect_pool``)."""
    p = ST.judged_pages(topics, rows, groups)
    v = ST.judged_pages(topics, None, groups)
    return {tid: p[tid] - v[tid] for tid in p}


def delta_candidates(topics: list[TB.Topic], rk: dict, depths: dict[str, int], dup: dict[str, frozenset[str]],
                     judged: dict[str, frozenset[str]]) -> dict[str, dict]:
    """T1's ``collect_pool`` (first ``depth`` pages of each system over the topic's three queries; a page that matches a
    catalogue target of the topic — primary, alternate or duplicate alias — is excluded), then the T2 rule: a candidate
    whose page or duplicate alias (API duplicates of the answer, canonical ``duplicate_page_candidates``) carries a T1
    judgment for the topic is not labelled again. A candidate matched only through an alias is kept as a link to the
    T1 pages (a scorer can add it to the T1 unit as an alternate)."""
    base = PT.collect_pool(topics, rk, depths, dup)
    out = {}
    for t in topics:
        b = base[t.topic_id]
        keep, excluded, links = {}, [], []
        for p, c in b["candidates"].items():
            hit = ({p} | c["aliases"]) & judged.get(t.topic_id, frozenset())
            if not hit:
                keep[p] = c
                continue
            excluded.append(p)
            if p not in hit:
                links.append({"page": p, "t1_pages": sorted(hit)})
        out[t.topic_id] = {"candidates": keep, "pairs_after_catalogue": len(b["candidates"]),
                           "excluded_catalogue_matches": b["excluded_catalogue_matches"],
                           "excluded_t1_judged": sorted(excluded),
                           "t1_alias_links": sorted(links, key=lambda x: x["page"])}
    return out


def make_units(topics: list[TB.Topic], delta: dict[str, dict], old: dict | None = None) -> dict[str, dict]:
    """One label unit per duplicate group among a topic's candidates (``pool_t1.group_candidates``); provenance =
    best rank per system over the unit's pages; unit ids ``d001``… per topic, kept from an earlier pool file."""
    old = old or {}
    out = {}
    for t in topics:
        d = delta[t.topic_id]
        cands = d["candidates"]
        prev = {p: u["cid"] for u in old.get(t.topic_id, {}).get("units", []) for p in u["pages"]}
        nxt = 1 + max((int(c[len(CID_PREFIX):]) for c in prev.values()), default=0)
        used: set[str] = set()
        units = []
        for g in PT.group_candidates(cands):
            cid = next((prev[p] for p in g if p in prev and prev[p] not in used), None)
            if cid is None:
                cid = f"{CID_PREFIX}{nxt:03d}"
                nxt += 1
            used.add(cid)
            systems: dict[str, int] = {}
            for p in g:
                for s, r in cands[p]["systems"].items():
                    systems[s] = min(r, systems.get(s, 999))
            units.append({"cid": cid, "pages": g, "systems": systems,
                          "variants": sorted(set().union(*(cands[p]["variants"] for p in g))),
                          "per_page": {p: cands[p]["systems"] for p in g},
                          "aliases": sorted(set().union(*(cands[p]["aliases"] for p in g)) - set(g))})
        units.sort(key=lambda u: u["pages"][0])
        out[t.topic_id] = {"track": t.track, "units": units,
                           "excluded_catalogue_matches": d["excluded_catalogue_matches"],
                           "excluded_t1_judged": d["excluded_t1_judged"], "t1_alias_links": d["t1_alias_links"]}
    return out


def pool_summary(topics_out: dict[str, dict], delta: dict[str, dict]) -> dict:
    units = [u for v in topics_out.values() for u in v["units"]]
    by_system: Counter = Counter()
    by_system_new: Counter = Counter()
    only: Counter = Counter()
    for u in units:
        for s in u["systems"]:
            by_system[s] += 1
            by_system_new[s] += is_new_source(u["pages"][0])
        if len(u["systems"]) == 1:
            only[next(iter(u["systems"]))] += 1
    per_topic = sorted(len(v["units"]) for v in topics_out.values())
    pages = {p for u in units for p in u["pages"]}
    return {"label_units": len(units), "pooled_pairs": sum(len(u["pages"]) for u in units),
            "pooled_pages": len(pages), "multi_page_units": sum(1 for u in units if len(u["pages"]) > 1),
            "units_with_aliases": sum(1 for u in units if u["aliases"]),
            "units_new_sources": sum(1 for u in units if is_new_source(u["pages"][0])),
            "units_old_sources": sum(1 for u in units if not is_new_source(u["pages"][0])),
            "pages_new_sources": sum(1 for p in pages if is_new_source(p)),
            "topics_with_units": sum(1 for v in topics_out.values() if v["units"]),
            "units_per_topic": {"min": per_topic[0], "median": per_topic[len(per_topic) // 2], "max": per_topic[-1]},
            "units_by_track": dict(sorted(Counter(v["track"] for v in topics_out.values() for _u in v["units"]).items())),
            "units_by_system": dict(sorted(by_system.items())),
            "units_by_system_new_sources": dict(sorted(by_system_new.items())),
            "units_only_one_system": dict(sorted(only.items())),
            "pairs_after_catalogue": sum(d["pairs_after_catalogue"] for d in delta.values()),
            "excluded_catalogue_matches": sum(len(d["excluded_catalogue_matches"]) for d in delta.values()),
            "excluded_t1_judged": sum(len(d["excluded_t1_judged"]) for d in delta.values()),
            "t1_alias_links": sum(len(d["t1_alias_links"]) for d in delta.values())}


def pool(systems: tuple[str, ...] = SYSTEMS) -> None:
    import duckdb

    work = PT.env_path("LB_WORK")
    work.mkdir(parents=True, exist_ok=True)
    topics = PT.load_topics()
    runs_path, exp_path = run_paths()
    rankings, run_info = load_rankings(runs_path, exp_path, systems)
    qids = {q.query_id for t in topics for q in t.queries}
    gaps = {s: len(qids - set(rankings.get(s, {}))) for s in systems if qids - set(rankings.get(s, {}))}
    if gaps:
        raise SystemExit(f"queries without a ranking: {gaps}")
    ctl = control(topics, rankings, runs_path, exp_path)
    print(json.dumps({"control": ctl}, ensure_ascii=False))
    if ctl["mismatches"]:
        raise SystemExit("the rankings do not reproduce the recorded V means")
    canon_db = PT.env_path("CANON_DB")
    con = duckdb.connect(str(canon_db), read_only=True)
    snapshot = con.execute("SELECT snapshot_id FROM meta.snapshot").fetchone()[0]
    if snapshot != SNAPSHOT:
        raise SystemExit(f"CANON_DB holds {snapshot}, expected {SNAPSHOT}")
    dup = PT.canon_duplicate_groups(con)
    rows, groups = load_t1()
    problems = PT.validate_rows(rows, topics)
    if problems:
        raise SystemExit("T1 labels fail their own checks:\n" + "\n".join(problems[:20]))
    delta = delta_candidates(topics, as_lists(rankings), {s: DEPTH for s in systems}, dup,
                             t1_judged(topics, rows, groups))
    old = load_pool(work)["topics"] if (work / POOL_FILE).is_file() else {}
    topics_out = make_units(topics, delta, old)
    summary = {"canonical_snapshot_id": snapshot, "systems": list(systems), "depth": DEPTH,
               "label_source": LABEL_SOURCE, "new_sources": f"{min(NEW_SOURCES)}…{max(NEW_SOURCES)}",
               "inputs": {"runs": run_info, "topic_set_sha256": PT.SET_SHA256,
                          "t1_qrels_sha256": PT.sha256_file(PT.OUT_TSV),
                          "t1_groups_sha256": PT.sha256_file(PT.OUT_GROUPS),
                          "canon_db_sha256": sha256_big(canon_db), "canon_duplicate_pages": len(dup)},
               "control": ctl, **pool_summary(topics_out, delta)}
    PT.write_lf(work / POOL_FILE, json.dumps({"summary": summary, "topics": topics_out}, ensure_ascii=False,
                                                  indent=0) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


# ------------------------------------------------------------------------------------------------ packets
def balanced_blocks(seq: list[tuple[str, int]], target: int = UNITS_TARGET) -> list[list[str]]:
    """Consecutive blocks of pages (``seq`` = (page, units) in page-id order) with about the same number of units:
    ``n = ceil(total / target)`` blocks; block k closes on the first page where the running count reaches
    ``round(k · total / n)``. A page (with all its units) is never split."""
    total = sum(n for _p, n in seq)
    if not total:
        return []
    n = max(1, math.ceil(total / target))
    blocks: list[list[str]] = []
    cur: list[str] = []
    run, k = 0, 1
    for p, units in seq:
        cur.append(p)
        run += units
        if k < n and run >= round(k * total / n):
            blocks.append(cur)
            cur, k = [], k + 1
    if cur:
        blocks.append(cur)
    return blocks


def render_packet(name: str, blk: list[str], by_page: dict[str, list[tuple[str, str, list[str]]]],
                  pages: dict[str, dict], defs: dict[str, dict], stems: dict[str, frozenset[str]],
                  idf: dict[str, float], secs: dict[str, str], meta: dict[str, dict], order: dict[str, int]) -> str:
    """One blind packet in the T1 format (the page loop of ``pool_t1.packets``): legend of the packet's topics, then per
    page source (title, year), length, NAV section, formulas, copies, ≤ 2 captions, formula previews, the text windows
    of its candidate topics and its units ``→ topic unit · …``. Only unit ids reach this function: no systems, no
    ranks."""
    tids = sorted({t for p in blk for t, _c, _ps in by_page[p]}, key=lambda x: order[x])
    lines = [f"# {name} — страниц {len(blk)}, единиц {sum(len(by_page[p]) for p in blk)}", "",
             "Строка ответа: `тема|единица|оценка|КОД` (КОД: KEY_QUANT KEY_MODEL KEY_OBS KEY_MECH → 3; "
             "SUP_DISCUSS SUP_FIGTAB SUP_ASPECT SUP_MODEL → 2; MENTION LIST NEIGHBOUR GENERAL → 1; "
             "OFF_TOPIC NO_TEXT FRONT_MATTER → 0)", "", "## Темы пакета"]
    for tid in tids:
        lines += PT.legend_entry(defs[tid])
    lines += ["", "## Страницы"]
    for p in blk:
        d = pages.get(p)
        cands = sorted(by_page[p], key=lambda x: order[x[0]])
        if d is None:
            lines += ["", f"### {p} | нет в каноне снимка", "→ " + " · ".join(f"{t} {c}" for t, c, _ in cands)]
            continue
        sm = meta.get(d["source_id"], {})
        head = (f"### {p} | {PT.short(sm.get('title'), 70)} ({sm.get('year') or '?'}) | "
                f"{len(d['text'])} зн.")
        if secs.get(p):
            head += f" | разд.: {PT.short(secs[p], 70)}"
        if d["formulas"]:
            head += f" | формул: {d['formulas']}"
        copies = sorted({x for _t, _c, ps in cands for x in ps if x != p})
        if copies:
            head += " | копии: " + ", ".join(copies)
        lines += ["", head]
        caps = d["captions"][:PT.MAX_CAPTIONS]
        if caps:
            lines.append(" ".join(f"[{k} {lab or ''}] {PT.short(c, PT.CAPTION_CHARS)}".replace("  ", " ")
                                  for k, lab, c in caps))
        if d.get("formula_previews"):
            lines.append("[формулы] " + " ¦ ".join(d["formula_previews"]))
        text = d["text"]
        if len(re.sub(r"\s+", "", text)) < 100:
            lines.append("[почти нет текста] " + PT.short(text, 200))
        else:
            lines.append(PT.page_windows(text, [stems[t] for t, _c, _ps in cands], idf))
        lines.append("→ " + " · ".join(f"{t} {c}" for t, c, _ in cands))
    return "\n".join(lines) + "\n"


def pages_of_units(pool_topics: dict[str, dict]) -> dict[str, list[tuple[str, str, list[str]]]]:
    """first page of a unit → [(topic, unit id, unit pages)] — a unit is shown at its first page."""
    by_page: dict[str, list[tuple[str, str, list[str]]]] = defaultdict(list)
    for tid, v in pool_topics.items():
        for u in v["units"]:
            by_page[u["pages"][0]].append((tid, u["cid"], u["pages"]))
    return dict(by_page)


def build_packets(topics: list[TB.Topic], defs: dict, stems: dict, pool_topics: dict[str, dict], pages: dict,
                  idf: dict, secs: dict, meta: dict, target: int = UNITS_TARGET) -> tuple[dict[str, str], list[dict]]:
    order = {t.topic_id: i for i, t in enumerate(topics)}
    by_page = pages_of_units(pool_topics)
    blocks = balanced_blocks([(p, len(by_page[p])) for p in sorted(by_page)], target)
    texts, index = {}, []
    for i, blk in enumerate(blocks, 1):
        name = f"pk-{i:03d}"
        texts[name] = render_packet(name, blk, by_page, pages, defs, stems, idf, secs, meta, order)
        units = [(t, c) for p in blk for t, c, _ps in sorted(by_page[p], key=lambda x: order[x[0]])]
        index.append({"packet": name, "pages": blk, "units": [list(x) for x in units], "n_pages": len(blk),
                      "n_units": len(units), "units_new_sources": sum(len(by_page[p]) for p in blk if is_new_source(p)),
                      "topics": len({t for t, _c in units}),
                      "sha256": hashlib.sha256(texts[name].encode("utf-8")).hexdigest()})
    return texts, index


# ------------------------------------------------------------------------------------------------ cross-check sample
def h_sample_pairs(path: Path = H_SAMPLE) -> frozenset[tuple[str, str]]:
    """(topic, page) of the 60 labels of the H review sample (parsed as ``pool_t1.hscore`` does)."""
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 6 and cells[0].isdigit():
            out.add((cells[1], cells[3]))
    return frozenset(out)


def draw_t1(rows: list[dict[str, str]], topics: list[TB.Topic], groups: list[dict], per_grade: dict[int, int],
            seed: int, exclude: frozenset[tuple[str, str]] = frozenset()) -> list[dict[str, str]]:
    """POOL_LABELING_V1.md §8 (``pool_t1.draw_sample`` without its final shuffle) with other counts and seed: per grade,
    round robin over the strata track × origin (only ``nav`` / one other system / several); inside a stratum the order
    of sha256(seed|topic|page); at most one label per topic within a grade while other strata have candidates. A unit
    is represented by its first page; ``exclude`` — pairs not to draw (the H review sample)."""
    track = {t.topic_id: t.track for t in topics}
    later = {(g["topic_id"], p) for g in groups for p in g["pages"][1:]}
    strata: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r["query_id"], r["doc_id"])
        if key in later or key in exclude:
            continue
        systems = {x.split("@")[0]: int(x.split("@")[1]) for x in r["pooled_from"].split(",")}
        strata[(int(r["grade"]), track[r["query_id"]], PT.origin(systems))].append(r)
    picked: list[dict] = []
    for g in sorted(per_grade, reverse=True):
        keys = sorted(k for k in strata if k[0] == g)
        pools = {k: sorted(strata[k], key=lambda r: hashlib.sha256(
            f"{seed}|{r['query_id']}|{r['doc_id']}".encode()).hexdigest()) for k in keys}
        chosen: list[dict] = []
        seen_t: set[str] = set()
        while len(chosen) < per_grade[g] and any(pools.values()):
            for k in keys:
                if len(chosen) >= per_grade[g]:
                    break
                while pools[k]:
                    r = pools[k].pop(0)
                    if r["query_id"] in seen_t and any(pools[x] for x in keys if x != k):
                        continue
                    chosen.append(r)
                    seen_t.add(r["query_id"])
                    break
        picked += chosen
    return picked


def draw_t2(pool_topics: dict[str, dict], n: int, seed: int) -> list[tuple[str, dict]]:
    """``n`` T2 units at random: the order of sha256(seed|T2|topic|first page) (a seeded draw without replacement that
    does not depend on the file order)."""
    units = [(tid, u) for tid, v in pool_topics.items() for u in v["units"]]
    units.sort(key=lambda x: hashlib.sha256(f"{seed}|T2|{x[0]}|{x[1]['pages'][0]}".encode()).hexdigest())
    return units[:n]


def draw_xcheck(topics: list[TB.Topic], pool_topics: dict[str, dict], rows: list[dict[str, str]],
                groups: list[dict], exclude: frozenset[tuple[str, str]], seed: int = XCHECK_SEED,
                n_t2: int = XCHECK_T2, per_grade: dict[int, int] | None = None) -> list[dict]:
    """The cross-check units: T2 units at random and T1 units stratified by grade (H sample excluded). A T1 unit keeps
    the pages of its duplicate group."""
    unit_pages = {(g["topic_id"], g["pages"][0]): g["pages"] for g in groups}
    out = [{"set": "T2", "topic_id": tid, "pages": u["pages"], "cid": u["cid"], "grade": None, "code": None}
           for tid, u in draw_t2(pool_topics, n_t2, seed)]
    for r in draw_t1(rows, topics, groups, per_grade or XCHECK_T1_PER_GRADE, seed, exclude):
        out.append({"set": "T1", "topic_id": r["query_id"],
                    "pages": unit_pages.get((r["query_id"], r["doc_id"]), [r["doc_id"]]), "cid": None,
                    "grade": int(r["grade"]), "code": r["rationale"]})
    return out


def build_xcheck(topics: list[TB.Topic], defs: dict, stems: dict, xunits: list[dict], pages: dict, idf: dict,
                 secs: dict, meta: dict) -> tuple[str, list[dict]]:
    """The cross-check packet (T1 format, pages in page-id order) with neutral unit ids x001… given in packet order —
    neither the id nor the position tells a T1 unit from a T2 unit — and its key (ids → set, unit, reference grade)."""
    order = {t.topic_id: i for i, t in enumerate(topics)}
    by_first: dict[str, list[dict]] = defaultdict(list)
    for x in xunits:
        by_first[x["pages"][0]].append(x)
    by_page: dict[str, list[tuple[str, str, list[str]]]] = {}
    key = []
    n = 0
    for p in sorted(by_first):
        by_page[p] = []
        for x in sorted(by_first[p], key=lambda x: order[x["topic_id"]]):
            n += 1
            xid = f"x{n:03d}"
            by_page[p].append((x["topic_id"], xid, x["pages"]))
            key.append({"xid": xid, **x})
    text = render_packet(XCHECK_NAME, sorted(by_page), by_page, pages, defs, stems, idf, secs, meta, order)
    return text, key


def source_meta(con) -> dict[str, dict]:
    """Title and year of every source of the canon (the work metadata rule of retrieval_v1 pages.json)."""
    from vkm_corpus.retrieval_lab.canon import CanonReader

    return {sid: {"title": m.title, "year": m.year} for sid, m in CanonReader(con, "duckdb").source_meta().items()}


def packets(force: bool = False) -> None:
    import duckdb

    work = PT.env_path("LB_WORK")
    jd = PT.judgments_dir()
    if not force and jd.is_dir() and any(f.read_text(encoding="utf-8").strip() for f in jd.glob("*.txt")):
        raise SystemExit("judgments exist: the packets are fixed (--force rebuilds them)")
    topics = PT.load_topics()
    defs = PT.topic_definitions(topics)
    trans = PT.translation_texts()
    stems = {tid: PT.topic_stems(d, trans.get(tid, [])) for tid, d in defs.items()}
    data = load_pool(work)
    rows, groups = load_t1()
    h_pairs = h_sample_pairs()
    xunits = draw_xcheck(topics, data["topics"], rows, groups, h_pairs)
    canon_db = PT.env_path("CANON_DB")
    con = duckdb.connect(str(canon_db), read_only=True)
    snapshot = con.execute("SELECT snapshot_id FROM meta.snapshot").fetchone()[0]
    if snapshot != SNAPSHOT:
        raise SystemExit(f"CANON_DB holds {snapshot}, expected {SNAPSHOT}")
    wanted = sorted({p for v in data["topics"].values() for u in v["units"] for p in u["pages"]}
                    | {p for x in xunits for p in x["pages"]})
    pages = PT.page_data(con, wanted)
    idf = PT.stem_idf([d["text"] for d in pages.values()])     # one idf table: all pages of this run's packets
    secs = PT.section_titles(pages)
    meta = source_meta(con)
    texts, index = build_packets(topics, defs, stems, data["topics"], pages, idf, secs, meta)
    xtext, key = build_xcheck(topics, defs, stems, xunits, pages, idf, secs, meta)
    leaks = [name for name, t in [*texts.items(), (XCHECK_NAME, xtext)] if _PROVENANCE.search(t)]
    if leaks:
        raise SystemExit(f"provenance strings in packets {leaks}")
    n_units = sum(len(v["units"]) for v in data["topics"].values())
    if sum(x["n_units"] for x in index) != n_units:
        raise SystemExit("packets do not hold every unit exactly once")
    PT.write_lf(work / "topics_handbook.md", PT.handbook(defs))
    pk = work / "packets"
    pk.mkdir(parents=True, exist_ok=True)
    for f in pk.glob("pk-*.md"):
        f.unlink()
    for name, text in texts.items():
        PT.write_lf(pk / f"{name}.md", text)
    PT.write_lf(work / "packets_index.json", json.dumps(index, ensure_ascii=False) + "\n")
    xdir = work / "xcheck"
    xdir.mkdir(parents=True, exist_ok=True)
    PT.write_lf(xdir / f"{XCHECK_NAME}.md", xtext)
    nav = os.environ.get("NAV_SECTIONS")
    td = PT.env_path("TD_WORK") / "stage_a.json"
    inputs = {f"{Path(POOL_FILE).stem}_sha256": PT.sha256_file(work / POOL_FILE), "canonical_snapshot_id": snapshot,
              "canon_db_sha256": sha256_big(canon_db), "td_stage_a_sha256": PT.sha256_file(td),
              "nav_sections_sha256": sha256_big(Path(nav).expanduser()) if nav and Path(nav).expanduser().is_file()
              else None, "t1_qrels_sha256": PT.sha256_file(PT.OUT_TSV),
              "t1_groups_sha256": PT.sha256_file(PT.OUT_GROUPS), "h_sample_sha256": PT.sha256_file(H_SAMPLE, lf=True)}
    xsha = hashlib.sha256(xtext.encode("utf-8")).hexdigest()
    PT.write_lf(work / "xcheck_key.json", json.dumps({
        "packet": XCHECK_NAME, "packet_sha256": xsha, "seed": XCHECK_SEED,
        "rule": f"{XCHECK_T2} T2 units in the order of sha256(seed|T2|topic|first page); T1 units by "
                f"POOL_LABELING_V1.md §8 strata with {XCHECK_T1_PER_GRADE} per grade, the H review sample excluded",
        "inputs": inputs, "units": key}, ensure_ascii=False, indent=1) + "\n")
    missing = sorted(p for p in wanted if p not in pages)
    sizes = [x["n_units"] for x in index]
    summary = {"inputs": inputs, "packets": len(index), "units": n_units, "pages": sum(x["n_pages"] for x in index),
               "units_per_packet": {"min": min(sizes), "max": max(sizes)}, "over_max": [x["packet"] for x in index
                                                                                      if x["n_units"] > UNITS_MAX],
               "missing_in_canon": missing, "section_titles": len(secs),
               "per_packet": [{k: x[k] for k in ("packet", "n_pages", "n_units", "units_new_sources", "topics",
                                                  "sha256")} for x in index],
               "xcheck": {"packet": XCHECK_NAME, "sha256": xsha, "units": len(key),
                          "pages": len({x["pages"][0] for x in key}),
                          "by_set": dict(Counter(x["set"] for x in key)),
                          "t1_by_grade": dict(sorted(Counter(x["grade"] for x in key if x["set"] == "T1").items())),
                          "h_sample_pairs_excluded": len(h_pairs)},
               "packets_index_sha256": PT.sha256_file(work / "packets_index.json"),
               "topics_handbook_sha256": PT.sha256_file(work / "topics_handbook.md")}
    PT.write_lf(work / "packets_summary.json", json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


# ------------------------------------------------------------------------------------------------ ingest
def build_rows(pool_topics: dict[str, dict], judg: dict[tuple[str, str], tuple[int, str]]
               ) -> tuple[list[dict[str, str]], list, list, list, list[dict]]:
    """Rows of the pooled-labels file (one per page of every judged unit) as ``pool_t1.ingest`` builds them, with
    ``LLM_AGENT_T2``; plus missing / bad / extra keys and the duplicate groups."""
    rows, missing, bad, groups, known = [], [], [], [], set()
    for tid, v in pool_topics.items():
        for u in v["units"]:
            key = (tid, u["cid"])
            known.add(key)
            if key not in judg:
                missing.append(key)
                continue
            g, code = judg[key]
            if g not in PT.GRADES or PT.CODES.get(code) != g:
                bad.append(key)
                continue
            for p in u["pages"]:
                rows.append({"query_id": tid, "level": "PAGE", "doc_id": p, "grade": str(g), "status": "CANDIDATE",
                             "basis": "POOL_JUDGMENT", "label_source": LABEL_SOURCE,
                             "pooled_from": PT.pooled_from(u["per_page"][p]), "rationale": code})
            if len(u["pages"]) > 1 or u["aliases"]:
                groups.append({"topic_id": tid, "pages": u["pages"], "aliases": u["aliases"]})
    extra = sorted(k for k in judg if k not in known)
    return rows, missing, bad, extra, groups


def validate_rows(rows: list[dict[str, str]], topics: list[TB.Topic], judged_t1: dict[str, frozenset[str]]
                  ) -> list[str]:
    """``pool_t1.validate_rows`` (known topic, PAGE id grammar, grade 0..3, CANDIDATE / POOL_JUDGMENT, ``system@rank``
    provenance, a reason code of the same grade, no duplicate pair, no catalogue target) with the label source
    ``LLM_AGENT_T2``, plus: no pair that carries a T1 judgment."""
    problems = [f"{r['query_id']} {r['doc_id']}: label_source {r['label_source']!r}" for r in rows
                if r["label_source"] != LABEL_SOURCE]
    problems += [m.replace(PT.LABEL_SOURCE, LABEL_SOURCE)
                 for m in PT.validate_rows([{**r, "label_source": PT.LABEL_SOURCE} for r in rows], topics)]
    problems += [f"{r['query_id']} {r['doc_id']}: pair judged in T1" for r in rows
                 if r["doc_id"] in judged_t1.get(r["query_id"], frozenset())]
    return problems


def ingest(partial: bool = False, out_tsv: Path = OUT_TSV, out_groups: Path = OUT_GROUPS) -> None:
    t1_files = {PT.OUT_TSV.resolve(), PT.OUT_GROUPS.resolve()}
    if out_tsv.resolve() in t1_files or out_groups.resolve() in t1_files:
        raise SystemExit("the T1 files are never written")
    topics = PT.load_topics()
    work = PT.env_path("LB_WORK")
    data = load_pool(work)
    judg = PT.read_judgments()
    rows, missing, bad, extra, groups = build_rows(data["topics"], judg)
    n_units = sum(len(v["units"]) for v in data["topics"].values())
    print(f"judged {n_units - len(missing) - len(bad)}; missing {len(missing)}; bad {len(bad)}; extra {len(extra)}")
    for name, lst in (("missing", missing), ("bad", bad), ("extra", extra)):
        if lst:
            print(f"{name}:", lst[:20])
    if not partial and (missing or bad or extra):
        raise SystemExit("not all pooled units are judged correctly (use --partial for an interim file)")
    t1_rows, t1_groups = load_t1()
    problems = validate_rows(rows, topics, t1_judged(topics, t1_rows, t1_groups))
    if problems:
        raise SystemExit("\n".join(problems[:30]))
    order = {t.topic_id: i for i, t in enumerate(topics)}
    rows.sort(key=lambda r: (order[r["query_id"]], -int(r["grade"]), r["doc_id"]))
    with open(out_tsv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=B.POOLED_COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    groups.sort(key=lambda g: (order[g["topic_id"]], g["pages"][0]))
    links = [{"topic_id": tid, **x} for tid, v in sorted(data["topics"].items(), key=lambda kv: order[kv[0]])
             for x in v["t1_alias_links"]]
    doc = {"benchmark": "TOPIC_BENCHMARK_V1", "label_source": LABEL_SOURCE, "qrels": out_tsv.name,
           "qrels_sha256": PT.sha256_file(out_tsv), "canonical_snapshot_id": data["summary"]["canonical_snapshot_id"],
           f"{Path(POOL_FILE).stem}_sha256": PT.sha256_file(work / POOL_FILE),
           "rule": "label units of the T2 pool with more than one page (duplicates among a topic's candidates share one "
                   "grade) or with duplicate aliases (API duplicates of the page, canonical duplicate_page_candidates "
                   "of the snapshot); truth P makes one target per unit: its first page, the other pages and the "
                   "aliases as alternates. t1_alias_links: candidates not labelled in T2 because a duplicate alias "
                   "carries the T1 judgment of the topic (alternates of that T1 unit)",
           "groups": groups, "t1_alias_links": links}
    PT.write_lf(out_groups, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    print("grades", dict(sorted(Counter(r["grade"] for r in rows).items())), "rows", len(rows), "groups", len(groups),
          "->", out_tsv.name)


# ------------------------------------------------------------------------------------------------ cross-check score
def parse_judgments(text: str, name: str = "file") -> tuple[dict[tuple[str, str], tuple[int, str]], list[str]]:
    """Lines ``<topic_id>|<unit>|<grade>|<CODE>`` (``#`` comments, blank lines skipped) as ``pool_t1.read_judgments``
    reads them; conflicting repeats and malformed lines are problems."""
    out: dict[tuple[str, str], tuple[int, str]] = {}
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")]
        if len(parts) != 4 or not parts[2].isdigit():
            problems.append(f"{name}:{n}: bad line {line[:80]!r}")
            continue
        key, val = (parts[0], parts[1]), (int(parts[2]), parts[3])
        if key in out and out[key] != val:
            problems.append(f"{name}:{n}: {key} judged twice with different labels")
        out[key] = val
    return out, problems


def agreement_block(pairs: list[tuple[int, int]]) -> dict:
    """``pool_t1.agreement`` (exact, ±1, Cohen's kappa unweighted / linear / quadratic, Krippendorff's alpha ordinal)
    of reference vs second grades + direction, the evidence threshold (≥ 2) and the confusion matrix."""
    a, b = [x for x, _y in pairs], [y for _x, y in pairs]
    res = PT.agreement(a, b)
    res["second_higher"] = sum(1 for x, y in pairs if y > x)
    res["second_lower"] = sum(1 for x, y in pairs if y < x)
    res["evidence_threshold_agreement"] = round(sum(1 for x, y in pairs if (x >= 2) == (y >= 2)) / len(pairs), 4)
    res["confusion_reference_rows"] = [[sum(1 for x, y in pairs if (x, y) == (i, j)) for j in PT.GRADES]
                                       for i in PT.GRADES]
    return res


def xscore_result(key: dict, second: dict[tuple[str, str], tuple[int, str]],
                  t2_grades: dict[tuple[str, str], int] | None) -> dict:
    """Second labeller vs the key: T1 units against the T1 grade in the key, T2 units against the ingested T2 grade
    of the unit's first page (pending while T2 is not ingested)."""
    by_xid = {u["xid"]: u for u in key["units"]}
    problems, answers = [], {}
    for (tid, xid), (g, code) in sorted(second.items()):
        u = by_xid.get(xid)
        if u is None:
            problems.append(f"{tid}|{xid}: unknown unit")
        elif u["topic_id"] != tid:
            problems.append(f"{tid}|{xid}: the unit belongs to {u['topic_id']}")
        elif g not in PT.GRADES or PT.CODES.get(code) != g:
            problems.append(f"{tid}|{xid}: code {code!r} does not fit grade {g}")
        else:
            answers[xid] = g
    pairs: dict[str, list[tuple[int, int]]] = {"T1": [], "T2": []}
    pending, diffs = [], []
    for xid, u in sorted(by_xid.items()):
        if xid not in answers:
            continue
        ref = u["grade"] if u["set"] == "T1" else (t2_grades or {}).get((u["topic_id"], u["pages"][0]))
        if ref is None:
            pending.append(xid)
            continue
        pairs[u["set"]].append((ref, answers[xid]))
        if ref != answers[xid]:
            diffs.append({"xid": xid, "set": u["set"], "topic_id": u["topic_id"], "page_id": u["pages"][0],
                          "grade_reference": ref, "grade_second": answers[xid]})
    out = {"units": len(by_xid), "answered": len(answers), "missing": sorted(set(by_xid) - set(answers)),
           "problems": problems, "pending_t2": pending, "t2_ingested": t2_grades is not None}
    for name, ps in (("T1", pairs["T1"]), ("T2", pairs["T2"]), ("all", pairs["T1"] + pairs["T2"])):
        out[name] = agreement_block(ps) if ps else None
    out["disagreements"] = diffs
    return out


def xscore(path: Path) -> None:
    work = PT.env_path("LB_WORK")
    key = json.load(open(work / "xcheck_key.json", encoding="utf-8"))
    second, problems = parse_judgments(path.read_text(encoding="utf-8"), path.name)
    t2 = None
    if OUT_TSV.is_file():
        t2 = {(r["query_id"], r["doc_id"]): int(r["grade"]) for r in B.load_pooled_qrels(OUT_TSV)}
    res = xscore_result(key, second, t2)
    res["problems"] = problems + res["problems"]
    res["file"] = path.name
    res["file_sha256"] = PT.sha256_file(path)
    PT.write_lf(work / "xcheck_score.json", json.dumps(res, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(res, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pool")
    p.add_argument("--systems", default=",".join(SYSTEMS),
                   help="pool systems (default: the T2 rule; a subset only for diagnostics)")
    sub.add_parser("packets").add_argument("--force", action="store_true", help="rebuild although judgments exist")
    sub.add_parser("status")
    sub.add_parser("ingest").add_argument("--partial", action="store_true", help="write an interim file")
    sub.add_parser("xscore").add_argument("file", type=Path, help="the second labeller's judgment file")
    args = ap.parse_args()
    if args.cmd == "pool":
        systems = tuple(s for s in SYSTEMS if s in args.systems.split(","))
        unknown = set(args.systems.split(",")) - set(SYSTEMS)
        if unknown or not systems:
            raise SystemExit(f"unknown systems {sorted(unknown)}; known {SYSTEMS}")
        pool(systems)
    elif args.cmd == "packets":
        packets(args.force)
    elif args.cmd == "status":
        PT.status()
    elif args.cmd == "ingest":
        ingest(args.partial)
    else:
        xscore(args.file)


if __name__ == "__main__":
    main()
