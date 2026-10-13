"""QUERY_TYPES_V1 — labels of another origin for the borehole (C) and author (D) queries (protocol:
``benchmarks/query_types_v1/POOL_LABELING_QT1.md``, labeller memo ``LABELLER_INSTRUCTIONS_QT1_RU.md``).

The automatic truth of QT-C-B1…B8 (borehole catalogue locators) and QT-D-01…12 (registry works) shares its source with
the entity and author routes of ``search_hybrid``. This script pools the first 10 pages of four systems (``bm25``,
``hybrid``, ``hybrid_g0``, ``hybrid_noroutes``) for the 20 queries, builds blind packets for LLM labellers who read the
pages themselves (``get_page`` / ``get_page_image`` only), ingests their judgments and scores nDCG@10 / P@10 with the
frozen ``vkm_corpus.retrieval_lab.metrics`` code, tests and the pre-registered decision rule.

Everything outside git lives in ``QT1_WORK`` (default ``<repo>/work/qt1_pool``): plan, raw runs (IDs only), pool,
packets, keys, judgments. Tracked outputs hold IDs and numbers only (no page text, no rationales).

usage: PYTHONPATH=src python benchmarks/query_types_v1/scripts/pool_qt1.py
           {selftest, plan, pool, packets, status, ingest, score} [--runs FILE] [--partial]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "benchmarks" / "topic_v1" / "scripts"))

from vkm_corpus.retrieval_lab import metrics as M  # noqa: E402  (frozen metric and test code)
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402  (holm)

BENCH = REPO / "benchmarks" / "query_types_v1"
SET_FILE = BENCH / "query_types_v1.jsonl"
SET_SHA256 = "3d503784ac99675d14c090511ec7de2c7cb24338a52a806a5f7a0a1ca2ef44df"     # LF-normalised
INSTRUCTIONS_SRC = BENCH / "LABELLER_INSTRUCTIONS_QT1_RU.md"
QRELS_OUT = BENCH / "qrels_qt1_pooled.tsv"
QRELS_XC_OUT = BENCH / "qrels_qt1_xc.tsv"
RESULTS_OUT = BENCH / "results_qt1.json"
RUN_NAME = "qt1_e15f9b3.jsonl"

SNAPSHOT = "snap-20261007T103222Z-2d71e9e8"
SYSTEMS = ("bm25", "hybrid", "hybrid_g0", "hybrid_noroutes")
REQUESTS = {   # the e10_after plan bodies (query added per line)
    "bm25": ("/v1/search", {"kinds": ["PAGE"], "limit": 50}),
    "hybrid": ("/v1/search/hybrid", {"candidates": 100, "kinds": ["PAGE"], "limit": 50}),
    "hybrid_g0": ("/v1/search/hybrid", {"candidates": 100, "graph": [], "kinds": ["PAGE"], "limit": 50}),
    "hybrid_noroutes": ("/v1/search/hybrid", {"author_route": False, "candidates": 100, "entity_route": False,
                                              "kinds": ["PAGE"], "limit": 50}),
}
DEPTH = 10
N_PACKETS = 4
SEED = 20261013
XC_SHARE = 0.10
GRADES = (0, 1, 2, 3)
UNKNOWN = "UNKNOWN"
RATIONALE_MAX = 200
LABEL_SOURCE, XC_SOURCE = "LLM_AGENT_QT1", "LLM_AGENT_QT1_XC"
TYPE_NAMES = {"C": "C_OBJECT/BOREHOLE", "D": "D_AUTHOR"}
ROUTE_OF = {"C": "entity_route", "D": "author_route"}
EXPECTED_ROUTE = {"hybrid": "APPLIED", "hybrid_g0": "APPLIED", "hybrid_noroutes": "OFF"}
PRIMARY_PAIRS = (("hybrid", "hybrid_noroutes"), ("hybrid", "bm25"))
DESCRIPTIVE_PAIRS = (("hybrid_g0", "hybrid"),)
UNKNOWN_MAX_SHARE = 0.20
HARM_DELTA, NONINF_MARGIN, ALPHA = -0.05, -0.05, 0.05
C_NEIGHBOURS, D_EXTRA_PAGES = 1, 5
KAPPA_LINEAR_MIN = 0.4
PAGE_RE = re.compile(r"^(VKM-SRC-\d{3}):([a-z])(\d{4})$")
VERDICT_RU = {"NO_VERDICT_UNKNOWN": "ВЕРДИКТ НЕ ВЫНОСИТСЯ", "HARM": "ВРЕД", "GAIN": "ВЫИГРЫШ",
              "NOT_WORSE": "НЕ ХУЖЕ", "UNDETERMINED": "НЕ ОПРЕДЕЛЕНО"}


# ---------------------------------------------------------------------------------------------------------- helpers
def work_dir() -> Path:
    return Path(os.environ.get("QT1_WORK") or (REPO / "work" / "qt1_pool")).expanduser()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def write_lf(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\r\n", "\n").encode("utf-8")
    path.write_bytes(data)
    return sha256_bytes(data)


def write_json(path: Path, obj) -> str:
    return write_lf(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def write_jsonl(path: Path, rows: list[dict]) -> str:
    return write_lf(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def page_parts(page_id: str) -> tuple[str, str, int]:
    m = PAGE_RE.match(page_id)
    if not m:
        raise ValueError(f"bad page id {page_id!r}")
    return m.group(1), m.group(2), int(m.group(3))


def qtype(q: dict) -> str | None:
    if q.get("type") == "C_OBJECT" and q.get("subtype") == "BOREHOLE":
        return "C"
    if q.get("type") == "D_AUTHOR":
        return "D"
    return None


def load_queries(set_file: Path = SET_FILE, check_sha: bool = True) -> list[dict]:
    raw = set_file.read_bytes().replace(b"\r\n", b"\n")
    if check_sha and sha256_bytes(raw) != SET_SHA256:
        raise SystemExit(f"set sha256 {sha256_bytes(raw)} != {SET_SHA256}")
    out = []
    for line in raw.decode("utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            t = qtype(q)
            if t:
                out.append({**q, "qt": t})
    return out


# ------------------------------------------------------------------------------------------------------------- plan
def make_plan(queries: list[dict]) -> list[dict]:
    rows = []
    for q in queries:
        for s in SYSTEMS:
            route, body = REQUESTS[s]
            rows.append({"query_id": q["query_id"], "request": {**body, "query": q["text"]}, "route": route,
                         "system": s, "topic_id": None})
    return rows


def write_plan_jsonl(path: Path, rows: list[dict]) -> str:
    return write_lf(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


# ------------------------------------------------------------------------------------------------------------- runs
def load_runs(paths: list[Path]) -> tuple[dict[tuple[str, str], dict], list[dict], list[dict]]:
    runs: dict[tuple[str, str], dict] = {}
    meta: list[dict] = []
    files = []
    for p in paths:
        n = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            n += 1
            if d.get("kind") == "run":
                runs[(d["query_id"], d["system"])] = d
            else:
                meta.append(d)
        files.append({"name": p.name, "sha256": sha256_file(p), "lines": n})
    return runs, meta, files


def top_pages(line: dict, k: int = DEPTH) -> list[str]:
    out: list[str] = []
    for h in line.get("hits") or []:
        p = h.get("page_id")
        if p and p not in out:
            out.append(p)
        if len(out) >= k:
            break
    return out


class Groups:
    """Union-find over (page, duplicate) and (page, G1 copy) pairs reported by the server."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def add_lines(self, lines) -> None:
        for line in lines:
            for h in line.get("hits") or []:
                for other in list(h.get("duplicates") or []) + list(h.get("copies") or []):
                    self.union(h["page_id"], other)

    def multi(self) -> list[list[str]]:
        acc: dict[str, set[str]] = defaultdict(set)
        for p in list(self.parent):
            acc[self.find(p)].add(p)
        return sorted(sorted(v) for v in acc.values() if len(v) > 1)


def group_map(groups: list[list[str]]) -> dict[str, str]:
    """page → group id (the smallest member); pages outside every listed group are their own group."""
    return {p: g[0] for g in groups for p in g}


def route_status(line: dict, t: str) -> str | None:
    if t == "C":
        return (line.get("object_ref") or {}).get("entity_route")
    return (line.get("author_route") or {}).get("status")


# ------------------------------------------------------------------------------------------------------------- pool
def build_pool(queries: list[dict], runs: dict, meta: list[dict], files: list[dict], set_sha: str) -> dict:
    problems = []
    snaps = sorted({m.get("canonical_snapshot_id") for m in meta if m.get("kind") == "meta"} - {None})
    if snaps != [SNAPSHOT]:
        problems.append(f"snapshots {snaps}, expected {SNAPSHOT}")
    routes: dict[str, dict[str, str | None]] = {}
    pairs: dict[tuple[str, str], dict[str, int]] = {}
    for q in queries:
        for s in SYSTEMS:
            line = runs.get((q["query_id"], s))
            if line is None:
                problems.append(f"missing run line {q['query_id']} × {s}")
                continue
            if line.get("error"):
                problems.append(f"error {line['error']} in {q['query_id']} × {s}")
            if s in EXPECTED_ROUTE:
                st = route_status(line, q["qt"])
                routes.setdefault(q["query_id"], {})[s] = st
                if st != EXPECTED_ROUTE[s]:
                    problems.append(f"route {ROUTE_OF[q['qt']]} of {q['query_id']} × {s}: {st}, "
                                    f"expected {EXPECTED_ROUTE[s]}")
            for rank, p in enumerate(top_pages(line), 1):
                page_parts(p)
                pairs.setdefault((q["query_id"], p), {})[s] = rank
    g = Groups()
    g.add_lines(line for (qid, _s), line in runs.items() if any(q["query_id"] == qid for q in queries))
    qt = {q["query_id"]: q["qt"] for q in queries}
    plist = [{"query_id": qid, "type": qt[qid], "page_id": p, "systems": dict(sorted(sy.items()))}
             for (qid, p), sy in sorted(pairs.items())]
    n_by_q = Counter(x["query_id"] for x in plist)
    return {"benchmark": "QUERY_TYPES_V1 / QT1", "protocol": "benchmarks/query_types_v1/POOL_LABELING_QT1.md",
            "set_sha256": set_sha, "snapshot_id": SNAPSHOT, "depth": DEPTH, "systems": list(SYSTEMS),
            "runs": {"files": files,
                     "meta": [{k: m.get(k) for k in ("kind", "harness_version", "label", "n_lines",
                                                     "canonical_snapshot_id", "started_at", "finished_at", "calls",
                                                     "errors", "retries") if m.get(k) is not None} for m in meta]},
            "controls": {"ok": not problems, "problems": problems, "route_status": routes},
            "queries": [{"query_id": q["query_id"], "type": q["qt"], "text": q["text"],
                         "n_pairs": n_by_q[q["query_id"]]} for q in queries],
            "n_pairs": {"C": sum(1 for x in plist if x["type"] == "C"),
                        "D": sum(1 for x in plist if x["type"] == "D"), "all": len(plist)},
            "brought_by": {s: sum(1 for x in plist if s in x["systems"]) for s in SYSTEMS},
            "only_by": {s: sum(1 for x in plist if list(x["systems"]) == [s]) for s in SYSTEMS},
            "alias_groups": g.multi(), "pairs": plist}


# ---------------------------------------------------------------------------------------------------------- packets
def assign_packets(n_by_query: dict[str, int], n_packets: int = N_PACKETS) -> list[list[str]]:
    """LPT: queries by decreasing size (ties by id), each to the packet with the fewest pairs (ties: lower index)."""
    bins: list[list[str]] = [[] for _ in range(n_packets)]
    load = [0] * n_packets
    for qid in sorted(n_by_query, key=lambda q: (-n_by_query[q], q)):
        i = min(range(n_packets), key=lambda j: (load[j], j))
        bins[i].append(qid)
        load[i] += n_by_query[qid]
    return bins


def page_sort_key(p: str) -> tuple[str, str, int]:
    return page_parts(p)


def order_units(pairs: list[dict], qids: list[str], rng: random.Random) -> list[dict]:
    """Query blocks in random order; units of a block by page id (no ranks, no systems)."""
    blocks = list(qids)
    rng.shuffle(blocks)
    out = []
    for qid in blocks:
        out += sorted((x for x in pairs if x["query_id"] == qid), key=lambda x: page_sort_key(x["page_id"]))
    return out


def xc_sample(pairs: list[dict], share: float = XC_SHARE, seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for t in ("C", "D"):
        sub = sorted((x for x in pairs if x["type"] == t), key=lambda x: (x["query_id"], x["page_id"]))
        k = int(round(share * len(sub)))
        out += rng.sample(sub, k) if k else []
    return out


def build_packets(w: Path, pool: dict, instructions: Path = INSTRUCTIONS_SRC) -> dict:
    text = {q["query_id"]: q["text"] for q in pool["queries"]}
    pairs = pool["pairs"]
    bins = assign_packets({q["query_id"]: q["n_pairs"] for q in pool["queries"] if q["n_pairs"]})
    key: dict[str, dict] = {}
    index = {"protocol": pool["protocol"], "pool_sha256": sha256_file(w / "pool_qt1.json"), "seed": SEED,
             "packets": []}
    for i, qids in enumerate(bins, 1):
        name = f"packet_{i:02d}"
        units = order_units(pairs, qids, random.Random(SEED + i))
        rows = []
        for j, x in enumerate(units, 1):
            uid = f"P{i}-{j:03d}"
            rows.append({"unit_id": uid, "query": text[x["query_id"]], "page_id": x["page_id"]})
            key[uid] = {"packet": name, "query_id": x["query_id"], "type": x["type"], "page_id": x["page_id"]}
        sha = write_jsonl(w / "packets" / f"{name}.jsonl", rows)
        index["packets"].append({"name": name, "file": f"packets/{name}.jsonl", "n_units": len(rows),
                                 "n_queries": len(qids), "n_C": sum(1 for x in units if x["type"] == "C"),
                                 "n_D": sum(1 for x in units if x["type"] == "D"),
                                 "n_pages": len({x["page_id"] for x in units}), "sha256": sha})
    xs = xc_sample(pairs)
    xq = sorted({x["query_id"] for x in xs})
    xunits = order_units(xs, xq, random.Random(SEED))
    xrows, xkey = [], {}
    for j, x in enumerate(xunits, 1):
        uid = f"X-{j:03d}"
        xrows.append({"unit_id": uid, "query": text[x["query_id"]], "page_id": x["page_id"]})
        xkey[uid] = {"packet": "xc_01", "query_id": x["query_id"], "type": x["type"], "page_id": x["page_id"]}
    xsha = write_jsonl(w / "xc" / "xc_01.jsonl", xrows)
    index["xc"] = {"name": "xc_01", "file": "xc/xc_01.jsonl", "n_units": len(xrows),
                   "n_C": sum(1 for x in xunits if x["type"] == "C"),
                   "n_D": sum(1 for x in xunits if x["type"] == "D"), "sha256": xsha}
    dst = w / "packets" / "INSTRUCTIONS_RU.md"
    shutil.copyfile(instructions, dst)
    index["instructions"] = {"file": "packets/INSTRUCTIONS_RU.md", "sha256": sha256_file(dst),
                             "source": "benchmarks/query_types_v1/LABELLER_INSTRUCTIONS_QT1_RU.md"}
    index["keys"] = {"packets_key": write_json(w / "keys" / "packets_key.json", key),
                     "xc_key": write_json(w / "keys" / "xc_key.json", xkey)}
    (w / "judgments").mkdir(parents=True, exist_ok=True)
    write_json(w / "packets_index.json", index)
    return index


# ----------------------------------------------------------------------------------------------------------- ingest
def check_judgments(path: Path, units: dict[str, dict]) -> tuple[dict[str, dict], list[str], list[dict]]:
    """Format errors (packet refused) and reading-budget deviations (label kept) of one judgments file."""
    errors: list[str] = []
    devs: list[dict] = []
    got: dict[str, dict] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"{path.name}:{n}: not JSON ({e.msg})")
            continue
        uid = d.get("unit_id")
        if uid not in units:
            errors.append(f"{path.name}:{n}: unknown unit_id {uid!r}")
            continue
        if uid in got:
            errors.append(f"{path.name}:{n}: duplicate unit_id {uid}")
            continue
        g = d.get("grade")
        if not (g == UNKNOWN or (isinstance(g, int) and not isinstance(g, bool) and g in GRADES)):
            errors.append(f"{path.name}:{n}: {uid} bad grade {g!r}")
            continue
        r = d.get("rationale")
        if not isinstance(r, str) or len(r) > RATIONALE_MAX:
            errors.append(f"{path.name}:{n}: {uid} rationale missing or longer than {RATIONALE_MAX}")
            continue
        pr = d.get("pages_read")
        if not isinstance(pr, list) or not all(isinstance(p, str) and PAGE_RE.match(p) for p in pr):
            errors.append(f"{path.name}:{n}: {uid} pages_read is not a list of page ids")
            continue
        u = units[uid]
        if u["page_id"] not in pr:
            errors.append(f"{path.name}:{n}: {uid} pages_read lacks the unit page")
            continue
        src, letter, idx = page_parts(u["page_id"])
        others = sorted(set(pr) - {u["page_id"]})
        codes = []
        if any(page_parts(p)[0] != src for p in others):
            codes.append("OTHER_SOURCE")
        if u["type"] == "C" and any(page_parts(p)[0] == src and (page_parts(p)[1] != letter
                                                                  or abs(page_parts(p)[2] - idx) > C_NEIGHBOURS)
                                    for p in others):
            codes.append("C_BEYOND_NEIGHBOURS")
        if u["type"] == "D" and len([p for p in others if page_parts(p)[0] == src]) > D_EXTRA_PAGES:
            codes.append("D_OVER_BUDGET")
        if codes:
            devs.append({"unit_id": uid, "codes": codes, "n_pages_read": len(set(pr))})
        got[uid] = {"grade": g, "n_pages_read": len(set(pr))}
    missing = sorted(set(units) - set(got))
    if missing:
        errors.append(f"{path.name}: {len(missing)} units without a judgment (first: {missing[:5]})")
    return got, errors, devs


def qrels_text(rows: list[dict]) -> str:
    buf = io.StringIO()
    wr = csv.writer(buf, delimiter="\t", lineterminator="\n")
    wr.writerow(["query_id", "page_id", "grade", "label_source", "packet", "unit_id", "n_pages_read"])
    for r in sorted(rows, key=lambda r: (r["query_id"], r["page_id"])):
        wr.writerow([r["query_id"], r["page_id"], r["grade"], r["label_source"], r["packet"], r["unit_id"],
                     r["n_pages_read"]])
    return buf.getvalue()


def ingest(w: Path, qrels_out: Path, xc_out: Path, partial: bool = False) -> dict:
    index = json.loads((w / "packets_index.json").read_text(encoding="utf-8"))
    key = json.loads((w / "keys" / "packets_key.json").read_text(encoding="utf-8"))
    xkey = json.loads((w / "keys" / "xc_key.json").read_text(encoding="utf-8"))
    report: dict = {"packets": {}, "deviations": [], "errors": []}
    rows: list[dict] = []
    for pk in index["packets"]:
        name = pk["name"]
        if sha256_file(w / pk["file"]) != pk["sha256"]:
            report["errors"].append(f"{name}: packet file changed after the index")
        units = {u: v for u, v in key.items() if v["packet"] == name}
        path = w / "judgments" / f"{name}.jsonl"
        if not path.exists():
            if not partial:
                report["errors"].append(f"{name}: no judgments file")
            report["packets"][name] = "MISSING"
            continue
        got, errs, devs = check_judgments(path, units)
        report["errors"] += errs
        report["deviations"] += [{**d, "packet": name} for d in devs]
        report["packets"][name] = {"n": len(got), "sha256": sha256_file(path)}
        rows += [{"query_id": units[u]["query_id"], "page_id": units[u]["page_id"], "grade": v["grade"],
                  "label_source": LABEL_SOURCE, "packet": name, "unit_id": u, "n_pages_read": v["n_pages_read"]}
                 for u, v in got.items()]
    xpath = w / "judgments" / "xc_01.jsonl"
    xrows: list[dict] = []
    if xpath.exists():
        got, errs, devs = check_judgments(xpath, xkey)
        report["errors"] += errs
        report["deviations"] += [{**d, "packet": "xc_01"} for d in devs]
        report["packets"]["xc_01"] = {"n": len(got), "sha256": sha256_file(xpath)}
        xrows = [{"query_id": xkey[u]["query_id"], "page_id": xkey[u]["page_id"], "grade": v["grade"],
                  "label_source": XC_SOURCE, "packet": "xc_01", "unit_id": u, "n_pages_read": v["n_pages_read"]}
                 for u, v in got.items()]
    else:
        report["packets"]["xc_01"] = "NOT_RUN"
    if report["errors"]:
        write_json(w / "ingest_report.json", report)
        raise SystemExit("ingest refused:\n  " + "\n  ".join(report["errors"]))
    report["qrels_sha256"] = write_lf(qrels_out, qrels_text(rows))
    report["n_labels"] = len(rows)
    if xrows:
        report["qrels_xc_sha256"] = write_lf(xc_out, qrels_text(xrows))
    write_json(w / "ingest_report.json", report)
    return report


def read_qrels(path: Path) -> dict[str, dict[str, int | str]]:
    out: dict[str, dict[str, int | str]] = defaultdict(dict)
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            g = r["grade"]
            out[r["query_id"]][r["page_id"]] = g if g == UNKNOWN else int(g)
    return dict(out)


# ------------------------------------------------------------------------------------------------------------ score
def ranked_units(line: dict, gid: dict[str, str] | None) -> list[str]:
    """First 10 distinct pages mapped to their copy groups; a repeated group keeps its slot with gain 0."""
    out, seen = [], set()
    for i, p in enumerate(top_pages(line)):
        g = gid.get(p, p) if gid is not None else p
        if g in seen:
            out.append(f"#REPEAT:{i}")
        else:
            seen.add(g)
            out.append(g)
    return out


def judged_units(q: dict[str, int | str], gid: dict[str, str] | None, unknown_as: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for p, g in q.items():
        v = unknown_as if g == UNKNOWN else int(g)
        k = gid.get(p, p) if gid is not None else p
        out[k] = max(out.get(k, 0), v)
    return out


def metric_rows(queries: list[dict], runs: dict, qrels: dict, gid: dict[str, str] | None, unknown_as: int
                ) -> tuple[list[dict], list[str]]:
    rows, excluded = [], []
    for q in queries:
        judged = judged_units(qrels.get(q["query_id"], {}), gid, unknown_as)
        if not any(v > 0 for v in judged.values()):
            excluded.append(q["query_id"])
            continue
        rel = {k for k, v in judged.items() if v >= 2}
        for s in SYSTEMS:
            line = runs.get((q["query_id"], s))
            if line is None:
                continue
            ranked = ranked_units(line, gid)
            pages = top_pages(line)
            qj = qrels.get(q["query_id"], {})
            rows.append({"query_id": q["query_id"], "type": q["qt"], "system": s,
                         "ndcg@10": M.ndcg_at(ranked, judged, DEPTH),
                         "p@10": M.precision_at(ranked, rel, DEPTH),
                         "judged@10": (sum(1 for p in pages if p in qj) / len(pages)) if pages else 1.0})
    return rows, excluded


def compare(rows: list[dict], t: str, a: str, b: str, metric: str) -> dict:
    ta = {r["query_id"]: r[metric] for r in rows if r["type"] == t and r["system"] == a}
    tb = {r["query_id"]: r[metric] for r in rows if r["type"] == t and r["system"] == b}
    test = M.paired_randomization(ta, tb)
    ci = M.bootstrap_ci(ta, tb)
    return {"n_queries": test["n"], "delta": test["delta"], "ci_lo": ci["lo"], "ci_hi": ci["hi"],
            "p_value": test["p_value"]}


def family(rows: list[dict], metric: str, pairs=PRIMARY_PAIRS) -> dict[str, dict]:
    out = {f"{t} | {a} - {b}": compare(rows, t, a, b, metric) for t in ("C", "D") for a, b in pairs}
    adj = TB.holm({k: v["p_value"] for k, v in out.items() if v["n_queries"]})
    return {k: {**v, "p_holm": adj.get(k)} for k, v in out.items()}


def verdict(cmp: dict, unknown_share: float) -> str:
    if unknown_share > UNKNOWN_MAX_SHARE:
        return "NO_VERDICT_UNKNOWN"
    if not cmp.get("n_queries"):
        return "UNDETERMINED"
    if cmp["delta"] < HARM_DELTA and cmp["p_value"] < ALPHA:
        return "HARM"
    if cmp["delta"] > 0 and cmp["p_holm"] is not None and cmp["p_holm"] < ALPHA:
        return "GAIN"
    if cmp["ci_lo"] >= NONINF_MARGIN:
        return "NOT_WORSE"
    return "UNDETERMINED"


def means(rows: list[dict], metric: str) -> dict:
    out: dict = {}
    for t in ("C", "D"):
        for s in SYSTEMS:
            xs = [r[metric] for r in rows if r["type"] == t and r["system"] == s]
            if xs:
                out.setdefault(t, {})[s] = round(sum(xs) / len(xs), 4)
    return out


def rnd(obj):
    if isinstance(obj, float):
        return round(obj, 5)
    if isinstance(obj, dict):
        return {k: rnd(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rnd(v) for v in obj]
    return obj


def auto_truth_agreement(queries: list[dict], qrels: dict) -> dict:
    out: dict = {"C": {}, "D": {}}
    for q in queries:
        qj = qrels.get(q["query_id"], {})
        if q["qt"] == "C":
            truth = {t["page_id"] for t in q["truth"]["targets"]} | {a for t in q["truth"]["targets"]
                                                                    for a in t.get("alt_page_ids") or ()}
            inp = [p for p in qj if p in truth]
            out["C"][q["query_id"]] = {
                "truth_pages_in_pool": len(inp),
                "truth_pages_graded_ge2": sum(1 for p in inp if qj[p] != UNKNOWN and qj[p] >= 2),
                "truth_pages_unknown": sum(1 for p in inp if qj[p] == UNKNOWN),
                "other_pages_graded_ge2": sum(1 for p in qj if p not in truth and qj[p] != UNKNOWN and qj[p] >= 2),
                "other_pages_graded_3": sum(1 for p in qj if p not in truth and qj[p] == 3)}
        else:
            srcs = set(q["truth"]["source_ids"])
            inp = [p for p in qj if page_parts(p)[0] in srcs]
            out["D"][q["query_id"]] = {
                "truth_source_pages_in_pool": len(inp),
                "truth_source_pages_graded_3": sum(1 for p in inp if qj[p] == 3),
                "truth_source_pages_unknown": sum(1 for p in inp if qj[p] == UNKNOWN),
                "other_pages_graded_3": sum(1 for p in qj if page_parts(p)[0] not in srcs and qj[p] == 3),
                "other_sources_graded_3": sorted({page_parts(p)[0] for p in qj
                                                  if page_parts(p)[0] not in srcs and qj[p] == 3})}
    for t, keys in (("C", ("truth_pages_in_pool", "truth_pages_graded_ge2", "other_pages_graded_ge2")),
                    ("D", ("truth_source_pages_in_pool", "truth_source_pages_graded_3", "other_pages_graded_3"))):
        tot = {k: sum(v[k] for v in out[t].values()) for k in keys}
        tot["share_truth_confirmed"] = round(tot[keys[1]] / tot[keys[0]], 4) if tot[keys[0]] else None
        out[t] = {"total": tot, "per_query": out[t]}
    return out


def xc_agreement(w: Path, qrels: dict, xc_path: Path) -> dict:
    if not xc_path.exists():
        return {"status": "NOT_RUN"}
    import pool_t1 as PT  # noqa: E402  (agreement measures of the topic benchmark)
    xc = read_qrels(xc_path)
    a, b, unk = [], [], 0
    for qid, pages in xc.items():
        for p, g2 in pages.items():
            g1 = qrels.get(qid, {}).get(p)
            if g1 is None:
                continue
            if g1 == UNKNOWN or g2 == UNKNOWN:
                unk += 1
                continue
            a.append(int(g1))
            b.append(int(g2))
    if not a:
        return {"status": "NO_PAIRS", "n_unknown_excluded": unk}
    full = PT.agreement(a, b)
    binary = PT.agreement([int(x >= 2) for x in a], [int(x >= 2) for x in b], k=2)
    flag = full["kappa_linear"] < KAPPA_LINEAR_MIN
    return {"status": "DONE", "n_unknown_excluded": unk, "graded": full,
            "binary_ge2": {"n": binary["n"], "exact": binary["exact"], "kappa": binary["kappa"]},
            "labels_unreliable": flag}


def score(w: Path, queries: list[dict], runs_paths: list[Path], qrels_path: Path, xc_path: Path, out: Path,
          partial: bool = False) -> dict:
    pool = json.loads((w / "pool_qt1.json").read_text(encoding="utf-8"))
    runs, _meta, files = load_runs(runs_paths)
    if [f["sha256"] for f in files] != [f["sha256"] for f in pool["runs"]["files"]]:
        raise SystemExit("runs differ from the pooled runs")
    qrels = read_qrels(qrels_path)
    unlabelled = [(x["query_id"], x["page_id"]) for x in pool["pairs"]
                  if x["page_id"] not in qrels.get(x["query_id"], {})]
    if unlabelled and not partial:
        raise SystemExit(f"{len(unlabelled)} pooled pairs without a label (first {unlabelled[:3]})")
    gid = group_map(pool["alias_groups"])
    n_type = Counter(x["type"] for x in pool["pairs"])
    unk_type = Counter(x["type"] for x in pool["pairs"]
                       if qrels.get(x["query_id"], {}).get(x["page_id"]) == UNKNOWN)
    unknown_share = {t: (unk_type[t] / n_type[t] if n_type[t] else 0.0) for t in ("C", "D")}
    grades = {t: dict(sorted(Counter(str(qrels.get(x["query_id"], {}).get(x["page_id"], "UNLABELLED"))
                                     for x in pool["pairs"] if x["type"] == t).items())) for t in ("C", "D")}
    res: dict = {"benchmark": "QUERY_TYPES_V1 / QT1", "protocol": pool["protocol"], "snapshot_id": SNAPSHOT,
                 "set_sha256": pool["set_sha256"], "runs": pool["runs"]["files"],
                 "pool_sha256": sha256_file(w / "pool_qt1.json"),
                 "qrels_sha256": sha256_bytes(qrels_path.read_bytes()),
                 "n_pairs": pool["n_pairs"], "grades": grades, "unknown_share": unknown_share,
                 "unlabelled": len(unlabelled)}
    for variant, unknown_as in (("main", 0), ("unknown_as_2", 2)):
        rows, excluded = metric_rows(queries, runs, qrels, gid, unknown_as)
        prim = family(rows, "ndcg@10")
        block = {"unknown_as": unknown_as, "excluded_queries": excluded,
                 "mean": {"ndcg@10": means(rows, "ndcg@10"), "p@10": means(rows, "p@10"),
                          "judged@10": means(rows, "judged@10")},
                 "primary_ndcg@10": prim,
                 "verdicts": {}}
        for t in ("C", "D"):
            v = verdict(prim[f"{t} | hybrid - hybrid_noroutes"], unknown_share[t])
            block["verdicts"][t] = {"route": ROUTE_OF[t], "verdict": v, "verdict_ru": VERDICT_RU[v]}
        if variant == "main":
            block["secondary_p@10"] = family(rows, "p@10")
            block["descriptive_ndcg@10"] = {f"{t} | {a} - {b}": compare(rows, t, a, b, "ndcg@10")
                                            for t in ("C", "D") for a, b in DESCRIPTIVE_PAIRS}
            prow, pexcl = metric_rows(queries, runs, qrels, None, unknown_as)
            block["pages_without_groups"] = {"excluded_queries": pexcl, "mean": means(prow, "ndcg@10"),
                                             "ndcg@10": family(prow, "ndcg@10")}
            block["per_query"] = rows
        res[variant] = block
    multi = [g for g in pool["alias_groups"]
             if sum(1 for x in pool["pairs"] if x["page_id"] in g) > 1]
    res["alias_groups"] = {"n_groups": len(pool["alias_groups"]), "n_groups_with_2plus_pooled_pages": len(multi)}
    res["auto_truth_agreement"] = auto_truth_agreement(queries, qrels)
    rep = w / "ingest_report.json"
    if rep.exists():
        r = json.loads(rep.read_text(encoding="utf-8"))
        res["ingest_deviations"] = {"n": len(r.get("deviations", [])),
                                    "by_code": dict(Counter(c for d in r.get("deviations", []) for c in d["codes"])),
                                    "units": r.get("deviations", [])}
    res["cross_check"] = xc_agreement(w, qrels, xc_path)
    res = rnd(res)
    write_json(out, res)
    return res


# --------------------------------------------------------------------------------------------------------- selftest
def _fake_set(path: Path) -> None:
    rows = []
    for i in (1, 2, 3):
        rows.append({"query_id": f"QT-C-B{i}", "type": "C_OBJECT", "subtype": "BOREHOLE", "text": f"скважина {i}",
                     "truth": {"kind": "PAGES", "targets": [{"page_id": f"VKM-SRC-90{i}:p0001", "alt_page_ids": []}]},
                     "meta": {}})
    for i in (1, 2, 3):
        rows.append({"query_id": f"QT-D-0{i}", "type": "D_AUTHOR", "text": f"работы Автор{i} А.А.",
                     "truth": {"kind": "SOURCES", "source_ids": [f"VKM-SRC-80{i}"]}, "meta": {}})
    rows.append({"query_id": "QT-E-01", "type": "E_PARAM", "text": "x", "truth": {"kind": "PAGES", "targets": []}})
    write_jsonl(path, rows)


def _fake_runs(path: Path, queries: list[dict], rng: random.Random) -> None:
    lines = [{"kind": "meta", "canonical_snapshot_id": SNAPSHOT, "harness_version": "selftest", "n_lines": 0}]
    for q in queries:
        base = 900 if q["qt"] == "C" else 800
        own = f"VKM-SRC-{base + int(q['query_id'][-1])}"
        for s in SYSTEMS:
            pages = [f"{own}:p{n:04d}" for n in (1, 2, 3)] if s in ("hybrid", "hybrid_g0") else []
            while len(pages) < 12:
                p = f"VKM-SRC-{rng.randint(700, 705)}:p{rng.randint(1, 8):04d}"
                if p not in pages:
                    pages.append(p)
            hits = [{"page_id": p, "source_id": p.split(":")[0], "duplicates": [], "copies": []} for p in pages]
            if s == "hybrid":
                hits[3]["copies"] = [f"VKM-SRC-799:p{base // 100 + int(q['query_id'][-1]):04d}"]
            line = {"kind": "run", "query_id": q["query_id"], "system": s, "error": None, "hits": hits}
            st = EXPECTED_ROUTE.get(s)
            if q["qt"] == "C":
                line["object_ref"] = {"entity_route": st}
            else:
                line["author_route"] = {"status": st}
            lines.append(line)
    write_jsonl(path, lines)


def selftest() -> None:
    # 1. frozen metric with copy groups: [A, A' (copy of A), B], grades A 3, B 2
    gid = group_map([["X:p0001", "Y:p0001"]])
    line = {"hits": [{"page_id": "X:p0001"}, {"page_id": "Y:p0001"}, {"page_id": "Z:p0001"}]}
    r = ranked_units(line, gid)
    j = judged_units({"X:p0001": 3, "Y:p0001": 1, "Z:p0001": 2}, gid, 0)
    assert r == ["X:p0001", "#REPEAT:1", "Z:p0001"], r
    assert j == {"X:p0001": 3, "Z:p0001": 2}, j
    exp = (3 + 2 / 2) / (3 + 2 / 1.584962500721156)
    assert abs(M.ndcg_at(r, j, 10) - exp) < 1e-12
    assert judged_units({"X:p0001": UNKNOWN}, None, 2) == {"X:p0001": 2}
    # 2. decision rule
    c = {"n_queries": 8, "delta": -0.10, "p_value": 0.01, "p_holm": 0.04, "ci_lo": -0.2, "ci_hi": -0.02}
    assert verdict(c, 0.0) == "HARM"
    assert verdict(c, 0.25) == "NO_VERDICT_UNKNOWN"
    assert verdict({**c, "delta": 0.2, "p_holm": 0.03, "ci_lo": 0.1}, 0.0) == "GAIN"
    assert verdict({**c, "delta": 0.01, "p_value": 0.6, "p_holm": 1.0, "ci_lo": -0.04}, 0.0) == "NOT_WORSE"
    assert verdict({**c, "delta": -0.03, "p_value": 0.3, "p_holm": 1.0, "ci_lo": -0.12}, 0.0) == "UNDETERMINED"
    assert verdict({**c, "delta": -0.08, "p_value": 0.2, "p_holm": 0.8, "ci_lo": -0.2}, 0.0) == "UNDETERMINED"
    # 3. LPT packets
    assert assign_packets({"a": 5, "b": 4, "c": 3, "d": 3, "e": 1}, 2) == [["a", "d"], ["b", "c", "e"]]
    # 4. end to end on synthetic data in a temp dir (no network, nothing under the repository)
    rng = random.Random(1)
    with tempfile.TemporaryDirectory() as td:
        w = Path(td) / "w"
        set_file = Path(td) / "set.jsonl"
        _fake_set(set_file)
        qs = load_queries(set_file, check_sha=False)
        assert [q["qt"] for q in qs] == ["C", "C", "C", "D", "D", "D"]
        plan = make_plan(qs)
        assert len(plan) == 24 and {p["system"] for p in plan} == set(SYSTEMS)
        assert plan[3]["request"]["author_route"] is False and plan[3]["request"]["entity_route"] is False
        runs_file = w / "runs" / RUN_NAME
        _fake_runs(runs_file, qs, rng)
        runs, meta, files = load_runs([runs_file])
        pool = build_pool(qs, runs, meta, files, "selftest")
        assert pool["controls"]["ok"], pool["controls"]["problems"]
        exp_pairs = {(q["query_id"], p) for q in qs for s in SYSTEMS for p in top_pages(runs[(q["query_id"], s)])}
        assert {(x["query_id"], x["page_id"]) for x in pool["pairs"]} == exp_pairs
        assert pool["alias_groups"] and all(any(p.startswith("VKM-SRC-799") for p in g) for g in pool["alias_groups"])
        write_json(w / "pool_qt1.json", pool)
        instr = Path(td) / "instr.md"
        write_lf(instr, "# памятка\n")
        index = build_packets(w, pool, instr)
        key = json.loads((w / "keys" / "packets_key.json").read_text(encoding="utf-8"))
        seen_q: dict[str, str] = {}
        n_units = 0
        for pk in index["packets"]:
            for u in read_jsonl(w / pk["file"]):
                assert set(u) == {"unit_id", "query", "page_id"}, u
                k = key[u["unit_id"]]
                assert k["page_id"] == u["page_id"] and seen_q.setdefault(k["query_id"], pk["name"]) == pk["name"]
                n_units += 1
        assert n_units == len(pool["pairs"]) == len(key)
        assert (w / "packets" / "INSTRUCTIONS_RU.md").read_bytes() == instr.read_bytes()
        xkey = json.loads((w / "keys" / "xc_key.json").read_text(encoding="utf-8"))
        assert xkey and all((v["query_id"], v["page_id"]) in exp_pairs for v in xkey.values())
        assert not set(xkey) & set(key)
        # synthetic judgments: own-source pages 3, copies source 3, others 0, one UNKNOWN per packet
        own = {q["query_id"]: f"VKM-SRC-{(900 if q['qt'] == 'C' else 800) + int(q['query_id'][-1])}" for q in qs}

        def grade_of(qid: str, p: str) -> int:
            return 3 if p.startswith(own[qid]) or p.startswith("VKM-SRC-799") else 0

        for pk in index["packets"]:
            units = read_jsonl(w / pk["file"])
            out = [{"unit_id": u["unit_id"], "grade": grade_of(key[u["unit_id"]]["query_id"], u["page_id"]),
                    "rationale": "синтетика", "pages_read": [u["page_id"]]} for u in units]
            out[0]["grade"] = UNKNOWN
            write_jsonl(w / "judgments" / f"{pk['name']}.jsonl", out[1:])
            try:
                ingest(w, Path(td) / "q.tsv", Path(td) / "x.tsv")
                raise AssertionError("ingest accepted a packet with a missing unit")
            except SystemExit as e:
                assert "without a judgment" in str(e)
            out[1]["pages_read"] = [units[1]["page_id"], "VKM-SRC-001:p0001"]     # another source: deviation
            write_jsonl(w / "judgments" / f"{pk['name']}.jsonl", out)
        bad = read_jsonl(w / "judgments" / "packet_01.jsonl")
        write_jsonl(w / "judgments" / "packet_01.jsonl", [{**bad[0], "grade": 4}] + bad[1:])
        try:
            ingest(w, Path(td) / "q.tsv", Path(td) / "x.tsv")
            raise AssertionError("ingest accepted grade 4")
        except SystemExit as e:
            assert "bad grade" in str(e)
        write_jsonl(w / "judgments" / "packet_01.jsonl", bad)
        xunits = read_jsonl(w / "xc" / "xc_01.jsonl")
        write_jsonl(w / "judgments" / "xc_01.jsonl",
                    [{"unit_id": u["unit_id"], "grade": grade_of(xkey[u["unit_id"]]["query_id"], u["page_id"]),
                      "rationale": "x", "pages_read": [u["page_id"]]} for u in xunits])
        rep = ingest(w, Path(td) / "q.tsv", Path(td) / "x.tsv")
        assert rep["n_labels"] == len(pool["pairs"]) and rep["deviations"], rep
        qr = read_qrels(Path(td) / "q.tsv")
        assert sum(len(v) for v in qr.values()) == len(pool["pairs"])
        res = score(w, qs, [runs_file], Path(td) / "q.tsv", Path(td) / "x.tsv", Path(td) / "res.json")
        m = res["main"]
        assert set(m["verdicts"]) == {"C", "D"}
        assert m["mean"]["judged@10"]["C"]["hybrid"] == 1.0
        assert m["mean"]["ndcg@10"]["D"]["hybrid"] > m["mean"]["ndcg@10"]["D"]["hybrid_noroutes"]
        assert res["cross_check"]["status"] == "DONE"
        txt = (Path(td) / "res.json").read_text(encoding="utf-8")
        assert "rationale" not in txt and "синтетика" not in txt
    print("selftest OK")


# ------------------------------------------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("selftest", "plan", "pool", "packets", "status", "ingest", "score"))
    ap.add_argument("--runs", action="append", default=[], help=f"raw runs (default: <work>/runs/{RUN_NAME})")
    ap.add_argument("--partial", action="store_true", help="ingest / score with missing packets (descriptive)")
    args = ap.parse_args()
    if args.cmd == "selftest":
        selftest()
        return
    w = work_dir()
    runs_paths = [Path(p) for p in args.runs] or [w / "runs" / RUN_NAME]
    if args.cmd == "plan":
        qs = load_queries()
        sha = write_plan_jsonl(w / "plan_qt1.jsonl", make_plan(qs))
        print(f"plan: {len(qs)} queries × {len(SYSTEMS)} systems, sha256 {sha}")
    elif args.cmd == "pool":
        qs = load_queries()
        runs, meta, files = load_runs(runs_paths)
        pool = build_pool(qs, runs, meta, files, SET_SHA256)
        sha = write_json(w / "pool_qt1.json", pool)
        print(f"pool: {pool['n_pairs']}, controls ok={pool['controls']['ok']}, sha256 {sha}")
        for p in pool["controls"]["problems"]:
            print("  PROBLEM", p)
        if not pool["controls"]["ok"]:
            raise SystemExit(2)
    elif args.cmd == "packets":
        pool = json.loads((w / "pool_qt1.json").read_text(encoding="utf-8"))
        if not pool["controls"]["ok"]:
            raise SystemExit("pool controls failed")
        index = build_packets(w, pool)
        for pk in index["packets"] + [index["xc"]]:
            print(f"{pk['name']}: {pk['n_units']} units (C {pk['n_C']}, D {pk['n_D']}), sha256 {pk['sha256']}")
        print(f"instructions sha256 {index['instructions']['sha256']}; keys {index['keys']}")
        print(f"index sha256 {sha256_file(w / 'packets_index.json')}")
    elif args.cmd == "status":
        index = json.loads((w / "packets_index.json").read_text(encoding="utf-8"))
        for pk in index["packets"] + [index["xc"]]:
            path = w / "judgments" / f"{pk['name']}.jsonl"
            n = len(read_jsonl(path)) if path.exists() else 0
            print(f"{pk['name']}: {n}/{pk['n_units']}")
    elif args.cmd == "ingest":
        rep = ingest(w, QRELS_OUT, QRELS_XC_OUT, args.partial)
        print(f"ingested {rep['n_labels']} labels, deviations {len(rep['deviations'])}, "
              f"qrels sha256 {rep['qrels_sha256']}")
    elif args.cmd == "score":
        res = score(w, load_queries(), runs_paths, QRELS_OUT, QRELS_XC_OUT, RESULTS_OUT, args.partial)
        for t, v in res["main"]["verdicts"].items():
            print(t, v["verdict_ru"], res["main"]["primary_ndcg@10"][f"{t} | hybrid - hybrid_noroutes"])


if __name__ == "__main__":
    main()
