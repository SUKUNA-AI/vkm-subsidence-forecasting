"""V1 pooled judging (design §12 p. 4): top-10 pages of the main systems for every covered query; candidates without a
VERIFIED label are graded by the LLM agent (label source ``LLM_AGENT_V1``, status CANDIDATE, basis POOL_JUDGMENT) from
the text of the page's best-matching units, with a one-line rationale in the agent's own words.

    build    — pool → ``$J_POOL/pool.json`` + judging packets ``$J_POOL/packets/batch-NN.md`` (snippets: git-ignored)
    topup    — after the main pool: unjudged top-10 pages of the ablation systems (``TOPUP_SYSTEMS``: late depth, BIB
               policy, unit set, fusion) → ``$J_POOL/pool_topup.json`` + ``packets/topup-NN.md`` (cids ``tNN``);
               ``--dry`` only counts
    ingest   — ``$J_POOL/judgments/*.txt`` (lines ``<query_id>|c<NN>|<grade>|<rationale>``) → checks (every candidate
               judged exactly once, grades 0–3) → ``benchmarks/retrieval_v1/qrels_v1_pooled.tsv`` (IDs, grades,
               rationale; no corpus text) + ``$J_POOL/judgments_full.jsonl``
    hsample  — stratified sample of ~60 pooled labels for the user's review (ревью H): ``H_REVIEW_SAMPLE.md`` (IDs,
               query, grade, rationale) + snippets in ``$J_HREVIEW`` (a git-ignored place under ``work/``)

Environment: ``J_V1`` (work dir), ``J_POOL`` (pool dir, default ``$J_V1/pool``), ``J_HREVIEW`` (snippet file path).
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from vkm_corpus.retrieval_lab import bench as B
from vkm_corpus.retrieval_lab.textproc import analyze, split_sentences

V1 = Path(os.environ["J_V1"])
POOL = Path(os.environ.get("J_POOL", str(V1 / "pool")))
BENCH_DIR = Path("benchmarks/retrieval_v0")
V1_DIR = Path("benchmarks/retrieval_v1")
# system → pool depth; E is pooled to 12 so that the reranked top-10 (rerank of E's top-8 / top-12) is covered
POOL_SYSTEMS = {"A|bm25-os": 10, "B|dense": 10, "C|rrf-prod": 10, "E|rrf-prod>late@100": 12, "L|late-full@final": 10}
SHORT = {"A|bm25-os": "bm25", "B|dense": "dense", "C|rrf-prod": "rrf", "E|rrf-prod>late@100": "E",
         "L|late-full@final": "late", "R|E|rrf-prod>late@100>rerank@8": "rr8",
         "R|E|rrf-prod>late@100>rerank@12": "rr12"}
SNIPPET_MAIN = 330
SNIPPET_SECOND = 170
QUERIES_PER_BATCH = 6
POOLED_COLUMNS = B.POOLED_COLUMNS
LABEL_SOURCE = B.POOLED_LABEL_SOURCES[0]


def load_units_text() -> tuple[dict[str, dict], dict[str, list[str]]]:
    rows, by_page = {}, defaultdict(list)
    for name in ("units_dense.jsonl", "units_final.jsonl"):
        with open(V1 / name, encoding="utf-8") as f:
            for line in f:
                u = json.loads(line)
                rows.setdefault(u["unit_id"], u)
                if name == "units_final.jsonl" and u["kind"] != "BIB_ENTRY":
                    by_page[u["page_id"]].append(u["unit_id"])
    return rows, by_page


def window(text: str, query: str, limit: int) -> str:
    """The run of sentences with the most query stems, ≤ ``limit`` characters (else the head of the text)."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    q = set(analyze(query))
    sents = split_sentences(text) or [text]
    scores = [len(q & set(analyze(s))) for s in sents]
    best, best_score = 0, -1
    for i in range(len(sents)):
        length, sc, j = 0, 0, i
        while j < len(sents) and length + len(sents[j]) <= limit:
            length += len(sents[j]) + 1
            sc += scores[j]
            j += 1
        if sc > best_score:
            best, best_score = i, sc
    out, length = [], 0
    for s in sents[best:]:
        if length + len(s) > limit:
            if not out:
                out.append(s[:limit] + "…")
            break
        out.append(s)
        length += len(s) + 1
    return (("… " if best > 0 else "") + " ".join(out)).strip()


# top-up pool (after the main pool is judged): systems whose conclusions matter (late depth, BIB policy CP-42, unit
# set, fusion variants) and whose top-10 still had unjudged pages; judged in the same way, cids ``tNN``
TOPUP_SYSTEMS = {"E|rrf-prod>late@100@final_bib": ("Eb", 10), "E|rrf-prod>late@100@dense": ("Ed", 10),
                 "L|late-full@final_bib": ("Lb", 10), "L|late-full@dense": ("Ld", 10),
                 "E|rrf-prod>late@30": ("E30", 10), "E|rrf-prod>late@50": ("E50", 10),
                 "E|rrf-prod>late@200": ("E200", 10), "F3|rrf(bm25-os,dense,late-full)": ("F3", 10),
                 "E|bm25-os>late@100": ("Ebm", 10)}


def build(topup: bool = False) -> None:
    data = json.load(open(V1 / "out" / "rankings.json", encoding="utf-8"))
    rankings, bestd = data["rankings"], data["best"]
    if (V1 / "out" / "rerank.json").is_file():
        rr = json.load(open(V1 / "out" / "rerank.json", encoding="utf-8"))
        rankings.update(rr["rankings"])
    bench = B.load_benchmark(BENCH_DIR)
    prep = json.load(open(V1 / "prepare.json", encoding="utf-8"))
    covered = set(prep["covered_queries"])
    pages_meta = json.load(open(V1 / "pages.json", encoding="utf-8"))
    judged_any = bench.judgments(level="PAGE", statuses=("VERIFIED",))
    systems = {n: (SHORT[n], d) for n, d in POOL_SYSTEMS.items()}
    prefix, out_name, batch_name, per_batch = "c", "pool.json", "batch", QUERIES_PER_BATCH
    if topup:
        main = json.load(open(POOL / "pool.json", encoding="utf-8"))["pool"]
        for qid, cands in main.items():
            for c in cands:
                judged_any.setdefault(qid, {})[c["page_id"]] = -1
        systems, prefix, out_name, batch_name, per_batch = TOPUP_SYSTEMS, "t", "pool_topup.json", "topup", 24
    units, by_page = load_units_text()
    import numpy as np

    S_late = np.load(V1 / "cache" / "S_late.npy", mmap_mode="r")          # (units of the run, queries)
    upos = {u: i for i, u in enumerate(json.load(open(V1 / "cache" / "units_ids.json", encoding="utf-8")))}
    qcol = {q.query_id: i for i, q in enumerate(bench.queries)}
    pool: dict[str, list[dict]] = {}
    stats = Counter()
    for q in bench.queries:
        if q.query_id not in covered:
            continue
        cand: dict[str, dict] = {}
        for sysname, (short, depth) in systems.items():
            lst = rankings.get(sysname, {}).get(q.query_id)
            if lst is None:
                stats[f"missing:{sysname}"] += 1
                continue
            for rank, (page, _s) in enumerate(lst[:depth], 1):
                c = cand.setdefault(page, {"page_id": page, "systems": {}})
                c["systems"][short] = rank
        stats["pool_pages"] += len(cand)
        unjudged = [c for p, c in cand.items() if p not in judged_any.get(q.query_id, {})]
        stats["unjudged"] += len(unjudged)
        if not unjudged:
            continue
        # snippets: the page's best unit for late interaction (final collection; else the dense unit that retrieved
        # it, else the page's first unit) and a second unit from the lexical side (lab BM25 over units incl.
        # bibliography entries — shows why a reference-list page matched) or the dense side, if different
        dense_best = bestd.get("B|dense", {}).get(q.query_id, {})
        bm_best = bestd.get("A|bm25-lab@final_bib", {}).get(q.query_id, {})
        j = qcol[q.query_id]
        for c in unjudged:
            p = c["page_id"]
            cand_units = [x for x in by_page.get(p, []) if x in upos]
            late_u = max(cand_units, key=lambda x: S_late[upos[x], j]) if cand_units else None
            u1 = late_u or dense_best.get(p) or (by_page.get(p) or [None])[0]
            u2 = next((u for u in (bm_best.get(p), dense_best.get(p)) if u and u != u1), None)
            c["units"] = [u for u in (u1, u2) if u]
        unjudged.sort(key=lambda c: (min(c["systems"].values()), c["page_id"]))
        for i, c in enumerate(unjudged, 1):
            c["cid"] = f"{prefix}{i:02d}"
        pool[q.query_id] = unjudged
    if topup and "--dry" in sys.argv:
        print(json.dumps(dict(stats), ensure_ascii=False), "queries with top-up", len(pool))
        return
    POOL.mkdir(parents=True, exist_ok=True)
    (POOL / out_name).write_text(json.dumps({"systems": {n: d for n, (_s, d) in systems.items()}, "pool": pool,
                                             "stats": stats}, ensure_ascii=False, indent=0), encoding="utf-8")
    # packets
    pk = POOL / "packets"
    pk.mkdir(exist_ok=True)
    qs = [q for q in bench.queries if q.query_id in pool]
    sources = pages_meta["sources"]
    for b in range(0, len(qs), per_batch):
        lines = []
        for q in qs[b:b + per_batch]:
            lines.append(f"## {q.query_id} [{q.track}/{q.category}] {q.text}")
            lines.append(f"INTENT: {q.intent}")
            if q.expected_kinds:
                lines.append(f"EXPECTED: {', '.join(q.expected_kinds)}")
            for c in pool[q.query_id]:
                p = c["page_id"]
                pm = pages_meta["pages"].get(p, {})
                sm = sources.get(pm.get("source_id"), {})
                title = (sm.get("title") or "")[:70]
                sysline = ",".join(f"{k}{v}" for k, v in sorted(c["systems"].items(), key=lambda x: x[1]))
                lines.append(f"- {c['cid']} {p} | {title} ({sm.get('year') or '?'}) | p.{pm.get('label') or '?'} | {sysline}")
                for k, uid in enumerate(c.get("units", [])):
                    u = units[uid]
                    snip = window(u["text"], q.text, SNIPPET_MAIN if k == 0 else SNIPPET_SECOND)
                    lines.append(f"  [{u['kind']}] {snip}")
            lines.append("")
        (pk / f"{batch_name}-{b // per_batch + 1:02d}.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(dict(stats), ensure_ascii=False), "batches", (len(qs) + per_batch - 1) // per_batch)


def load_pools() -> dict[str, list[dict]]:
    """The main pool plus the top-up pool (if built), per query."""
    pool = json.load(open(POOL / "pool.json", encoding="utf-8"))["pool"]
    if (POOL / "pool_topup.json").is_file():
        for qid, cands in json.load(open(POOL / "pool_topup.json", encoding="utf-8"))["pool"].items():
            pool.setdefault(qid, []).extend(cands)
    return pool


def read_judgments() -> dict[tuple[str, str], tuple[int, str]]:
    out: dict[tuple[str, str], tuple[int, str]] = {}
    dups = []
    for f in sorted((POOL / "judgments").glob("*.txt")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 3)
            if len(parts) != 4 or not parts[2].strip().isdigit():
                raise SystemExit(f"{f.name}:{n}: bad line {line[:80]!r}")
            qid, cid, grade, why = (x.strip() for x in parts)
            if (qid, cid) in out:
                dups.append((qid, cid))
            out[(qid, cid)] = (int(grade), why)
    if dups:
        print("duplicates (last wins):", dups[:20])
    return out


def ingest() -> None:
    pool = load_pools()
    judg = read_judgments()
    rows, missing, bad = [], [], []
    full = []
    for qid, cands in pool.items():
        for c in cands:
            key = (qid, c["cid"])
            if key not in judg:
                missing.append(key)
                continue
            g, why = judg[key]
            if g not in B.GRADES or len(why) > B.MAX_RATIONALE_CHARS or "\t" in why:
                bad.append(key)
                continue
            src = ",".join(f"{k}@{v}" for k, v in sorted(c["systems"].items(), key=lambda x: x[1]))
            rows.append({"query_id": qid, "level": "PAGE", "doc_id": c["page_id"], "grade": str(g),
                         "status": "CANDIDATE", "basis": "POOL_JUDGMENT", "label_source": LABEL_SOURCE,
                         "pooled_from": src, "rationale": why})
            full.append({"query_id": qid, "cid": c["cid"], "page_id": c["page_id"], "grade": g, "rationale": why,
                         "systems": c["systems"], "units": c.get("units", [])})
    extra = [k for k in judg if k[0] not in pool or k[1] not in {c["cid"] for c in pool[k[0]]}]
    print(f"judged {len(rows)}; missing {len(missing)}; bad {len(bad)}; extra {len(extra)}")
    if missing:
        print("missing:", missing[:30])
    if bad:
        print("bad:", bad[:30])
    if "--partial" not in sys.argv and (missing or bad):
        raise SystemExit("not all pooled candidates are judged (use --partial for an interim file)")
    rows.sort(key=lambda r: (r["query_id"], -int(r["grade"]), r["doc_id"]))
    with open(V1_DIR / "qrels_v1_pooled.tsv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=POOLED_COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    with open(POOL / "judgments_full.jsonl", "w", encoding="utf-8") as f:
        for r in full:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("grades", Counter(r["grade"] for r in rows))


def hsample(n_total: int = 60, seed: int = 20260928) -> None:
    """Stratified by grade (≈ equal), then by track and by how the page entered the pool (only the reranker / only
    late / only BM25 / several systems); deterministic."""
    bench = B.load_benchmark(BENCH_DIR)
    qmap = {q.query_id: q for q in bench.queries}
    full = [json.loads(l) for l in open(POOL / "judgments_full.jsonl", encoding="utf-8")]
    units, _ = load_units_text()

    def origin(r):
        s = set(r["systems"])
        if s <= {"rr8", "rr12"}:
            return "rerank-only"
        if s <= {"late", "E"}:
            return "late-only"
        if s <= {"bm25"}:
            return "bm25-only"
        if s <= {"dense"}:
            return "dense-only"
        if s <= {"rrf"}:
            return "rrf-only"
        if s <= {short for short, _d in TOPUP_SYSTEMS.values()}:
            return "topup-only"
        return "several"

    rng = random.Random(seed)
    strata = defaultdict(list)
    for r in full:
        strata[(r["grade"], qmap[r["query_id"]].track, origin(r))].append(r)
    per_grade = n_total // 4
    picked = []
    for g in (3, 2, 1, 0):
        keys = sorted(k for k in strata if k[0] == g)
        pools = {k: sorted(strata[k], key=lambda r: hashlib.sha256(f"{seed}|{r['query_id']}|{r['page_id']}".encode()).hexdigest())
                 for k in keys}
        chosen, seen_q = [], set()
        while len(chosen) < per_grade and any(pools.values()):
            for k in keys:
                if len(chosen) >= per_grade:
                    break
                while pools[k]:
                    r = pools[k].pop(0)
                    if r["query_id"] in seen_q and any(pools[x] for x in keys if x != k):
                        continue
                    chosen.append(r)
                    seen_q.add(r["query_id"])
                    break
        picked += chosen
    rng.shuffle(picked)
    lines = ["# H_REVIEW_SAMPLE — выборка пул-меток V1 для ревью H", "",
             "Метки источника `LLM_AGENT_V1` (статус CANDIDATE, основание POOL_JUDGMENT) — оценки агента J-V1 по тексту",
             "лучших единиц страницы. Выборка стратифицирована: по ≈15 меток каждой оценки (3/2/1/0), внутри — по треку и",
             "по тому, какая система привела страницу в пул. Текст фрагментов — только в git-игнорируемом файле",
             "(`work/corpus_platform/impl/retrieval_lab/v1/H_REVIEW_SNIPPETS.md`); здесь — только ID, запрос, оценка и",
             "обоснование. Шкала: 3 — прямо отвечает; 2 — существенно релевантно; 1 — упоминание/контекст; 0 — нерелевантно.",
             "", "Как отвечать: в колонке «H» поставить ✓ (согласен) или свою оценку 0–3.", "",
             "| # | query_id | запрос | page_id | оценка J-V1 | обоснование | привела в пул | H |",
             "|---|---|---|---|---|---|---|---|"]
    snip = ["# H_REVIEW_SNIPPETS — фрагменты страниц к выборке H (git-ignored, содержит текст корпуса)", ""]
    for i, r in enumerate(picked, 1):
        q = qmap[r["query_id"]]
        src = ",".join(f"{k}@{v}" for k, v in sorted(r["systems"].items(), key=lambda x: x[1]))
        qtext = q.text if len(q.text) <= 120 else q.text[:117] + "…"
        lines.append(f"| {i} | {r['query_id']} | {qtext} | {r['page_id']} | {r['grade']} | {r['rationale']} | {src} | |")
        snip.append(f"## {i}. {r['query_id']} → {r['page_id']} (оценка {r['grade']}: {r['rationale']})")
        snip.append(f"Запрос: {q.text}")
        for uid in r.get("units", []):
            u = units.get(uid)
            if u:
                snip.append(f"[{u['kind']} {uid}] {window(u['text'], q.text, 900)}")
        snip.append("")
    stats = Counter((r["grade"], qmap[r["query_id"]].track) for r in picked)
    lines += ["", f"Состав: {len(picked)} меток; по (оценка, трек): " +
              ", ".join(f"{g}/{t}: {n}" for (g, t), n in sorted(stats.items(), reverse=True)) + "."]
    (V1_DIR / "H_REVIEW_SAMPLE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = Path(os.environ["J_HREVIEW"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(snip) + "\n", encoding="utf-8")
    print("sample", len(picked), dict(stats))


if __name__ == "__main__":
    {"build": build, "topup": lambda: build(topup=True), "ingest": ingest, "hsample": hsample}[sys.argv[1]]()
