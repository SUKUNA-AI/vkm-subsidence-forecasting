"""GRAPH_SEARCH_V1 top-up labels (PREREGISTRATION §8): the unlabelled pages among the first 10 of every final system
(E, G1–G5 with the dev-chosen variants, GC) on the retrieval_v1 queries → blind judging packets → labels
``LLM_AGENT_GS`` (status CANDIDATE, basis POOL_JUDGMENT) in ``benchmarks/graph_search_v1/qrels_gs_pooled.tsv``.

    build        — pool + packets ``$GS_WORK/pool/packets/pk-NNN.md`` (blind: no system names, no ranks; candidates of
                   a query ordered by page id; cids ``gNN`` stable across rebuilds); pages with any label (VERIFIED,
                   LLM_AGENT_V1, LLM_AGENT_V2) are skipped
    ingest       — ``$GS_WORK/pool/judgments/*.txt`` (lines ``<query_id>|<cid>|<grade>|<rationale>``) → checks → TSV
    status       — pool size and judged count

Snippets (git-ignored work dir only; the V2 protocol): the page's best unit by late MaxSim of V1 (final units without
BIB_ENTRY, the production query encodings of V1), the page's best unit by the lab BM25 when it differs, and for visual
queries two figure captions of the page. Environment: ``GS_WORK``, ``J_V1``.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab.textproc import analyze  # noqa: E402

WORK = Path(os.environ["GS_WORK"])
V1 = Path(os.environ["J_V1"])
POOL = WORK / "pool"
OUT_TSV = REPO / "benchmarks/graph_search_v1/qrels_gs_pooled.tsv"
LABEL_SOURCE = "LLM_AGENT_GS"
DEPTH = 10
CANDS_PER_PACKET = 60
SNIP_MAIN, SNIP_SECOND, SNIP_CAPTION = 330, 170, 140


def _window():
    spec = importlib.util.spec_from_file_location("pool_v1", REPO / "benchmarks/retrieval_v1/scripts/pool_v1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.window


def labelled() -> dict[str, set[str]]:
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    out: dict[str, set[str]] = defaultdict(set)
    for r in bench.qrels:
        if r.level == "PAGE":
            out[r.query_id].add(r.doc_id)
    for path in (REPO / "benchmarks/retrieval_v1/qrels_v1_pooled.tsv",
                 REPO / "benchmarks/retrieval_v2/qrels_v2_pooled.tsv"):
        for r in B.load_pooled_qrels(path):
            out[r["query_id"]].add(r["doc_id"])
    return out


def build() -> None:
    import run as R

    sets = R.load_sets()
    finals = R.final_systems()
    runs = R.read_runs("runs.jsonl", "runs_gc.jsonl")
    have = labelled()
    merged: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    for name, variant in finals.items():
        sysruns = runs[variant] if variant in runs else runs[name]
        for q in sets["v1"]:
            for rank, page in enumerate(sysruns[q.query_id]["order"][:DEPTH], 1):
                if page in have.get(q.query_id, set()):
                    continue
                merged[q.query_id].setdefault(page, {})[name] = rank
    old = json.load(open(POOL / "pool.json", encoding="utf-8"))["pool"] if (POOL / "pool.json").is_file() else {}
    order = [q.query_id for q in sets["v1"]]
    pool_out: dict[str, list[dict]] = {}
    for qid in sorted(merged, key=order.index):
        prev = {c["page_id"]: c["cid"] for c in old.get(qid, [])}
        nxt = 1 + max((int(c[1:]) for c in prev.values()), default=0)
        cands = []
        for p in sorted(merged[qid]):
            cid = prev.get(p)
            if cid is None:
                cid = f"g{nxt:02d}"
                nxt += 1
            cands.append({"cid": cid, "page_id": p, "systems": merged[qid][p]})
        pool_out[qid] = cands
    by_sys = Counter(s for cs in pool_out.values() for c in cs for s in c["systems"])
    stats = {"systems": finals, "depth": DEPTH, "pool_pages": sum(len(v) for v in pool_out.values()),
             "queries": len(pool_out), "pages_per_system": dict(sorted(by_sys.items()))}
    POOL.mkdir(parents=True, exist_ok=True)
    (POOL / "pool.json").write_text(json.dumps({"stats": stats, "pool": pool_out}, ensure_ascii=False, indent=0),
                                    encoding="utf-8")
    write_packets(sets, pool_out)
    print(json.dumps(stats, ensure_ascii=False))


def write_packets(sets: dict, pool: dict[str, list[dict]]) -> None:
    window = _window()
    rows, by_page, captions = {}, defaultdict(list), defaultdict(list)
    with open(V1 / "units_final.jsonl", encoding="utf-8") as f:
        for line in f:
            u = json.loads(line)
            rows[u["unit_id"]] = u
            if u["kind"] != "BIB_ENTRY":
                by_page[u["page_id"]].append(u["unit_id"])
                if u["kind"] == "FIGURE":
                    captions[u["page_id"]].append(u["unit_id"])
    upos = {u: i for i, u in enumerate(json.load(open(V1 / "cache" / "units_ids.json", encoding="utf-8")))}
    S_late = np.load(V1 / "cache" / "S_late.npy", mmap_mode="r")
    bench = sets["bench"]
    qcol = {q.query_id: i for i, q in enumerate(bench.queries)}
    pages_meta = json.load(open(V1 / "pages.json", encoding="utf-8"))
    judged = read_judgments(strict=False)
    qmap = {q.query_id: q for q in bench.queries}
    pk = POOL / "packets"
    pk.mkdir(parents=True, exist_ok=True)
    for f in pk.glob("pk-*.md"):
        f.unlink()
    blocks, cur, n_cur = [], [], 0
    for qid, cands in pool.items():
        todo = [c for c in cands if (qid, c["cid"]) not in judged]
        if not todo:
            continue
        q = qmap[qid]
        j = qcol[qid]
        qterms = set(analyze(q.text))
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
            texts = [x for x in by_page.get(p, []) if x in rows]
            u2 = max(texts, key=lambda x: len(qterms & set(analyze(rows[x]["text"])))) if texts else None
            shown = []
            for k, uid in enumerate(x for x in (u1, u2) if x):
                if uid in shown:
                    continue
                shown.append(uid)
                u = rows[uid]
                lines.append(f"  [{u['kind']}] {window(u['text'], q.text, SNIP_MAIN if k == 0 else SNIP_SECOND)}")
            if q.track == "visual":
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
    for earlier in ("benchmarks/retrieval_v1/qrels_v1_pooled.tsv", "benchmarks/retrieval_v2/qrels_v2_pooled.tsv"):
        problems += B.pooled_round_overlaps(B.load_pooled_qrels(REPO / earlier), rows)
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
