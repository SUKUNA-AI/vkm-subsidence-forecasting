"""V2 pooled judging (PREREGISTRATION §4): unjudged top-k pages of the V2 systems → blind judging packets → labels
``LLM_AGENT_V2`` (status CANDIDATE, basis POOL_JUDGMENT) in ``benchmarks/retrieval_v2/qrels_v2_pooled.tsv``.

    build [--only-p1]  — pool P1 (E:m, E+VIS:v both tracks; VIS:v visual track) and P2 (B:m, VIS:v text track, E3:v;
                         depth 10, or 5 if P1 ∪ P2@10 has more than 5 000 new pages); pages with any label (VERIFIED,
                         LLM_AGENT_V1, earlier LLM_AGENT_V2) are skipped; packets ``$V2_WORK/pool/packets/pk-NNN.md``
                         are blind: no system names, no ranks, candidates of a query ordered by page id (cids ``vNN``,
                         stable across rebuilds: an already assigned cid keeps its page)
    ingest [--partial] — ``$V2_WORK/pool/judgments/*.txt`` (lines ``<query_id>|<cid>|<grade>|<rationale>``) → checks →
                         the public TSV (IDs, grades, provenance ``system@rank``, one-line rationale; no corpus text)
    status             — pool size, judged / missing per packet

Snippets (git-ignored work dir only): the page's best unit by late MaxSim (final units without BIB), the unit that
brought the page in a B:m system (else the served dense unit), and for visual queries or pages from VIS systems the
first figure captions of the page. Environment: ``J_V1`` (read only), ``V2_WORK``.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from vkm_corpus.retrieval_lab import bench as B  # noqa: E402

V1 = Path(os.environ["J_V1"])
WORK = Path(os.environ["V2_WORK"])
POOL = WORK / "pool"
V1_DIR = REPO / "benchmarks/retrieval_v1"
V2_DIR = REPO / "benchmarks/retrieval_v2"
OUT_TSV = V2_DIR / "qrels_v2_pooled.tsv"
LABEL_SOURCE = "LLM_AGENT_V2"
MAX_NEW_FULL = 5000
CANDS_PER_PACKET = 110
SNIP_MAIN, SNIP_SECOND, SNIP_CAPTION = 330, 170, 140


def _window():
    spec = importlib.util.spec_from_file_location("pool_v1", REPO / "benchmarks/retrieval_v1/scripts/pool_v1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.window


def short(name: str) -> str:
    """System name → provenance token ``[A-Za-z0-9_]+`` (``E:qwen3-0.6b`` → ``E_qwen3_0_6b``)."""
    kind, _, key = name.partition(":")
    kind = {"E+VIS": "EVIS", "E3": "E3", "VIS": "VIS", "E": "E", "B": "B"}[kind]
    return kind + "_" + re.sub(r"[^A-Za-z0-9]", "_", key)


def systems_of(rankings: dict) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """P1 and P2 systems with the tracks they are pooled on (the served baselines are already pooled by V1)."""
    p1, p2 = {}, {}
    for name in rankings:
        kind, _, key = name.partition(":")
        if key == "nano-rx580" or not key or "~" in key:     # served baselines (pooled by V1); parity variants
            continue
        if kind in ("E", "E+VIS"):
            p1[name] = ("text", "visual")
        elif kind == "VIS":
            p1[name] = ("visual",)
            p2[name] = ("text",)
        elif kind in ("B", "E3"):
            p2[name] = ("text", "visual")
    return p1, p2


def labelled() -> dict[str, set[str]]:
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    out: dict[str, set[str]] = defaultdict(set)
    for r in bench.qrels:
        if r.level == "PAGE" and r.status == "VERIFIED":
            out[r.query_id].add(r.doc_id)
    for path in (V1_DIR / "qrels_v1_pooled.tsv",):
        for r in B.load_pooled_qrels(path):
            out[r["query_id"]].add(r["doc_id"])
    return out


def build() -> None:
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    covered = set(json.load(open(V1 / "prepare.json", encoding="utf-8"))["covered_queries"])
    data = json.load(open(WORK / "out" / "rankings_v2.json", encoding="utf-8"))
    rankings, best = data["rankings"], data["best"]
    p1, p2 = systems_of(rankings)
    have = labelled()
    track = {q.query_id: q.track for q in bench.queries}

    def collect(systems: dict[str, tuple[str, ...]], depth: int) -> dict[str, dict[str, dict[str, int]]]:
        pool: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
        for name, tracks in systems.items():
            for qid, lst in rankings[name].items():
                if qid not in covered or track[qid] not in tracks:
                    continue
                for rank, page in enumerate(lst[:depth], 1):
                    if page in have.get(qid, set()):
                        continue
                    pool[qid].setdefault(page, {})[short(name)] = rank
        return pool

    pool1 = collect(p1, 10)
    new1 = {(q, p) for q, d in pool1.items() for p in d}
    pool2_10 = collect(p2, 10)
    new12 = new1 | {(q, p) for q, d in pool2_10.items() for p in d}
    depth2 = 10 if len(new12) <= MAX_NEW_FULL else 5
    only_p1 = "--only-p1" in sys.argv
    pool2 = {} if only_p1 else (pool2_10 if depth2 == 10 else collect(p2, 5))
    merged: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    for pl in (pool1, pool2):
        for q, d in pl.items():
            for p, s in d.items():
                merged[q].setdefault(p, {}).update(s)
    # stable cids: reuse the assignment of an earlier build
    old = json.load(open(POOL / "pool.json", encoding="utf-8"))["pool"] if (POOL / "pool.json").is_file() else {}
    pool_out: dict[str, list[dict]] = {}
    for q in sorted(merged, key=lambda x: [qq.query_id for qq in bench.queries].index(x)):
        prev = {c["page_id"]: c["cid"] for c in old.get(q, [])}
        nxt = 1 + max((int(c[1:]) for c in prev.values()), default=0)
        cands = []
        for p in sorted(merged[q]):
            cid = prev.get(p)
            if cid is None:
                cid = f"v{nxt:02d}"
                nxt += 1
            cands.append({"cid": cid, "page_id": p, "systems": merged[q][p]})
        pool_out[q] = cands
    stats = {"p1_systems": sorted(p1), "p2_systems": sorted(p2), "p1_new_pages": len(new1),
             "p1_p2_new_pages_at_10": len(new12), "p2_depth": depth2, "only_p1": only_p1,
             "pool_pages": sum(len(v) for v in pool_out.values()), "queries": len(pool_out)}
    POOL.mkdir(parents=True, exist_ok=True)
    (POOL / "pool.json").write_text(json.dumps({"stats": stats, "pool": pool_out}, ensure_ascii=False, indent=0),
                                    encoding="utf-8")
    write_packets(bench, pool_out, best)
    print(json.dumps(stats, ensure_ascii=False))


def write_packets(bench, pool: dict[str, list[dict]], best: dict) -> None:
    window = _window()
    rows, by_page, captions = {}, defaultdict(list), defaultdict(list)
    for name in ("units_dense.jsonl", "units_final.jsonl"):
        with open(V1 / name, encoding="utf-8") as f:
            for line in f:
                u = json.loads(line)
                rows.setdefault(u["unit_id"], u)
                if name == "units_final.jsonl" and u["kind"] != "BIB_ENTRY":
                    by_page[u["page_id"]].append(u["unit_id"])
                    if u["kind"] == "FIGURE":
                        captions[u["page_id"]].append(u["unit_id"])
    upos = {u: i for i, u in enumerate(json.load(open(V1 / "cache" / "units_ids.json", encoding="utf-8")))}
    S_late = np.load(V1 / "cache" / "S_late.npy", mmap_mode="r")
    qcol = {q.query_id: i for i, q in enumerate(bench.queries)}
    pages_meta = json.load(open(V1 / "pages.json", encoding="utf-8"))
    judged = read_judgments(strict=False)
    pk = POOL / "packets"
    pk.mkdir(parents=True, exist_ok=True)
    for f in pk.glob("pk-*.md"):
        f.unlink()
    qmap = {q.query_id: q for q in bench.queries}
    blocks, cur, n_cur = [], [], 0
    for qid, cands in pool.items():
        todo = [c for c in cands if (qid, c["cid"]) not in judged]
        if not todo:
            continue
        q = qmap[qid]
        j = qcol[qid]
        lines = [f"## {qid} [{q.track} · {q.category}]", f"Запрос: {q.text}", f"Смысл: {q.intent}"]
        if q.expected_kinds:
            lines.append(f"Ожидается: {', '.join(q.expected_kinds)}")
        for c in todo:
            p = c["page_id"]
            pm = pages_meta["pages"].get(p, {})
            sm = pages_meta["sources"].get(pm.get("source_id"), {})
            lines.append(f"- {c['cid']} {p} | {(sm.get('title') or '')[:70]} ({sm.get('year') or '?'}) | "
                         f"с. {pm.get('label') or '?'}")
            units = [x for x in by_page.get(p, []) if x in upos]
            u1 = max(units, key=lambda x: S_late[upos[x], j]) if units else None
            u2 = None
            for sysname, _rank in sorted(c["systems"].items(), key=lambda x: x[1]):
                if sysname.startswith("B_"):
                    key = "B:" + next((k.split(":", 1)[1] for k in best if short(k) == sysname), "")
                    u2 = best.get(key, {}).get(qid, {}).get(p)
                    if u2:
                        break
            if u2 is None:
                u2 = best.get("B:nano-rx580", {}).get(qid, {}).get(p)
            shown = []
            for k, uid in enumerate(x for x in (u1, u2) if x):
                if uid in shown or uid not in rows:
                    continue
                shown.append(uid)
                u = rows[uid]
                lines.append(f"  [{u['kind']}] {window(u['text'], q.text, SNIP_MAIN if k == 0 else SNIP_SECOND)}")
            if q.track == "visual" or any(s.startswith(("VIS_", "EVIS_")) for s in c["systems"]):
                for uid in [x for x in captions.get(p, []) if x not in shown][:2]:
                    lines.append(f"  [подпись] {window(rows[uid]['text'], q.text, SNIP_CAPTION)}")
            if not shown and not captions.get(p):
                lines.append("  [нет текстовых единиц на странице]")
        cur.append("\n".join(lines))
        n_cur += len(todo)
        if n_cur >= CANDS_PER_PACKET:
            blocks.append(cur)
            cur, n_cur = [], 0
    if cur:
        blocks.append(cur)
    for i, blk in enumerate(blocks, 1):
        (pk / f"pk-{i:03d}.md").write_text("\n\n".join(blk) + "\n", encoding="utf-8")
    print("packets", len(blocks))


def read_judgments(strict: bool = True) -> dict[tuple[str, str], tuple[int, str]]:
    out: dict[tuple[str, str], tuple[int, str]] = {}
    d = POOL / "judgments"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.txt")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 3)
            if len(parts) != 4 or not parts[2].strip().isdigit():
                if strict:
                    raise SystemExit(f"{f.name}:{n}: bad line {line[:80]!r}")
                continue
            qid, cid, grade, why = (x.strip() for x in parts)
            out[(qid, cid)] = (int(grade), why)
    return out


def ingest() -> None:
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    pool = json.load(open(POOL / "pool.json", encoding="utf-8"))["pool"]
    judg = read_judgments()
    rows, missing, bad = [], [], []
    for qid, cands in pool.items():
        for c in cands:
            key = (qid, c["cid"])
            if key not in judg:
                missing.append(key)
                continue
            g, why = judg[key]
            if g not in B.GRADES or not why or len(why) > B.MAX_RATIONALE_CHARS or "\t" in why:
                bad.append(key)
                continue
            src = ",".join(f"{k}@{v}" for k, v in sorted(c["systems"].items(), key=lambda x: (x[1], x[0])))
            rows.append({"query_id": qid, "level": "PAGE", "doc_id": c["page_id"], "grade": str(g),
                         "status": "CANDIDATE", "basis": "POOL_JUDGMENT", "label_source": LABEL_SOURCE,
                         "pooled_from": src, "rationale": why})
    known = {(q, c["cid"]) for q, cs in pool.items() for c in cs}
    extra = [k for k in judg if k not in known]
    print(f"judged {len(rows)}; missing {len(missing)}; bad {len(bad)}; extra {len(extra)}")
    if bad:
        print("bad:", bad[:20])
    if extra:
        print("extra:", extra[:20])
    if "--partial" not in sys.argv and (missing or bad or extra):
        raise SystemExit("not all pooled candidates are judged (use --partial for an interim file)")
    problems = B.validate_pooled_qrels(rows, bench)
    problems += B.pooled_round_overlaps(B.load_pooled_qrels(V1_DIR / "qrels_v1_pooled.tsv"), rows)
    if problems:
        raise SystemExit("\n".join(problems[:30]))
    rows.sort(key=lambda r: (r["query_id"], -int(r["grade"]), r["doc_id"]))
    with open(OUT_TSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=B.POOLED_COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print("grades", dict(sorted(Counter(r["grade"] for r in rows).items())), "->", OUT_TSV.name)


def status() -> None:
    pool = json.load(open(POOL / "pool.json", encoding="utf-8"))
    judg = read_judgments(strict=False)
    total = sum(len(v) for v in pool["pool"].values())
    done = sum(1 for q, cs in pool["pool"].items() for c in cs if (q, c["cid"]) in judg)
    print(json.dumps(pool["stats"], ensure_ascii=False))
    print(f"judged {done} / {total}")


if __name__ == "__main__":
    {"build": build, "ingest": ingest, "status": status}[sys.argv[1]]()
