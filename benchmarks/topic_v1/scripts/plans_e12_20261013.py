"""Plans of experiment E12 (PREREGISTRATION_E12_2026-10-13.md): the opt-in ``long_query`` mode of the hybrid search
(agent M, ``vkm_corpus.search.long_query``) on four sets. Four systems everywhere — ``strict`` (no ``long_query``
field: today's default request, byte for byte), ``relax``, ``strip``, ``relax_strip`` (``long_query`` = the name);
every other field is the same within a set:

    nl      NL117 — the 117 natural-language questions ``nl_questions_para2.jsonl`` (one per topic, written from the
            PARA2 queries ``TQ-…-2``), systems ``nl_<mode>``; fields of E6c: ``graph: []``, ``author_route: false``,
            ``entity_route: false``
    topic   topic_v1, all 351 queries (NAME / PARA1 / PARA2), systems ``<mode>``; fields of E6c (as ``nl``)
    qt      query_types_v1, 70 queries, systems ``<mode>``; fields of its base run (E10 ``hybrid``): server defaults
    rv      retrieval_v1, the 189 queries of benchmarks/retrieval_v0, systems ``<mode>``; fields of its base run
            (``rv_default`` of the 13.10 route checks): server defaults

Every request: ``POST /v1/search/hybrid``, kinds PAGE, limit 50, candidates 100 (``plans_20261013.BASE_REQUEST``).
Lines are interleaved by query (the four modes of a query, then the next query). Plans hold query texts and stay in
git-ignored ``work/``; run with ``run_plan.sh`` (``harness_plan.py`` in the API container, read-only, concurrency 1).

``offline`` prints (IDs and numbers only) how many queries of each set are *long* by ``long_query.content_terms``
(≥ ``LONG_QUERY_MIN_TERMS``) and what ``strip`` would do — computed without the API.

``repeat`` builds the repeat plan of the code check (§6 of the pre-registration) from the results JSON of
``score_e12_20261013.py``: every required line that differs from ``strict``, asked again twice (``~r1``, ``~r2``).

usage: python benchmarks/topic_v1/scripts/plans_e12_20261013.py {nl,topic,qt,rv} --out <plan.jsonl>
       PYTHONPATH=src python benchmarks/topic_v1/scripts/plans_e12_20261013.py offline [--out <json>]
       python benchmarks/topic_v1/scripts/plans_e12_20261013.py repeat --results <results.json> --out <plan.jsonl>
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


P = _load("plans_20261013", HERE / "plans_20261013.py")

NL_FILE = ("nl_questions_para2.jsonl", "9434fe2cbfe38b6582975011ef1f02bec7e2f3b3a71e6bdd8206d6e091ae1b21")   # LF
# the generator's task text: sha256 of the bytes as handed over (CRLF) and of the LF file kept in the repository
TASK_FILE = ("nl_questions_para2_task.txt", "6f4cca51e29064c68469159edde158566ac7a969273ab6f6da059bc209bbea7b",
             "416b9fcc9713a6f2b5131d818231fe525442f2057c127a656c55b10e01770fe1")
NL_VARIANT = "PARA2"
MODES = ("strict", "relax", "strip", "relax_strip")
TOPIC_COMMON = {"graph": [], **P.NO_ROUTES}          # E6c fields of the topic sets
SETS = ("nl", "topic", "qt", "rv")


def nl_system(mode: str) -> str:
    return f"nl_{mode}"


def mode_fields(mode: str) -> dict:
    """``strict`` sends no ``long_query`` field (the API default: the request of the base runs, unchanged)."""
    return {} if mode == "strict" else {"long_query": mode}


def check_task_file(directory: Path = P.LLM_DIR) -> None:
    name, sha_crlf, sha_lf = TASK_FILE
    data = (directory / name).read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(data).hexdigest() != sha_lf or \
            hashlib.sha256(data.replace(b"\n", b"\r\n")).hexdigest() != sha_crlf:
        raise SystemExit(f"{name}: sha256 differs from the pre-registered {sha_lf} (LF) / {sha_crlf} (CRLF)")


def load_nl(directory: Path = P.LLM_DIR) -> list[dict]:
    """The 117 questions in file order; sha256 checked (the file is LF); query ids = the PARA2 queries, in set order."""
    name, sha = NL_FILE
    path = directory / name
    if P.sha256_lf(path) != sha:
        raise SystemExit(f"{name}: sha256 differs from the pre-registered {sha}")
    check_task_file(directory)
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    para2 = [q["query_id"] for t in P.load_topics() for q in t["queries"] if q["variant"] == NL_VARIANT]
    if [r["query_id"] for r in rows] != para2:
        raise SystemExit(f"{name}: query ids are not the {NL_VARIANT} queries of topic_v1 in set order")
    return rows


def topic_of() -> dict[str, str]:
    return {q["query_id"]: t["topic_id"] for t in P.load_topics() for q in t["queries"]}


def qt_queries() -> list[dict]:
    return [json.loads(x) for x in P.QT_SET.read_text(encoding="utf-8").splitlines() if x.strip()]


def plan_nl() -> list[dict]:
    tmap = topic_of()
    return [P.line(r["query_id"], tmap[r["query_id"]], nl_system(m),
                   P.hybrid(r["question"], **TOPIC_COMMON, **mode_fields(m))) for r in load_nl() for m in MODES]


def plan_topic() -> list[dict]:
    return [P.line(q["query_id"], t["topic_id"], m, P.hybrid(q["text"], **TOPIC_COMMON, **mode_fields(m)))
            for t in P.load_topics() for q in t["queries"] for m in MODES]


def plan_qt() -> list[dict]:
    return [P.line(q["query_id"], q.get("topic_id"), m, P.hybrid(q["text"], **mode_fields(m)))
            for q in qt_queries() for m in MODES]


def plan_rv() -> list[dict]:
    return [P.line(q["query_id"], None, m, P.hybrid(q["text"], **mode_fields(m)))
            for q in P.retrieval_queries() for m in MODES]


# ------------------------------------------------------------------------------------------------ offline counts
def set_texts() -> dict[str, list[tuple[str, str, str]]]:
    """set → [(query_id, group, text)]: group = topic variant (topic), query type (qt), track (rv), "NL" (nl)."""
    return {"nl": [(r["query_id"], "NL", r["question"]) for r in load_nl()],
            "topic": [(q["query_id"], q["variant"], q["text"]) for t in P.load_topics() for q in t["queries"]],
            "qt": [(q["query_id"], q["type"], q["text"]) for q in qt_queries()],
            "rv": [(q["query_id"], q["track"], q["text"]) for q in P.retrieval_queries()]}


def offline() -> dict:
    """Long / short per set by ``long_query.content_terms`` and what ``strip`` would do (no API; IDs and numbers)."""
    from vkm_corpus.search import long_query as LQ

    out: dict = {"min_terms": LQ.LONG_QUERY_MIN_TERMS, "relaxed_msm": LQ.RELAXED_MSM, "sets": {}}
    for name, rows in set_texts().items():
        per = {}
        for qid, group, text in rows:
            pl = LQ.plan(text, "strip")
            per[qid] = {"group": group, "terms": pl.terms, "long": pl.long, "strip": pl.strip,
                        "terms_after_strip": pl.terms_after_strip}
        groups: dict[str, dict] = {}
        for g in sorted({x["group"] for x in per.values()}):
            xs = [x for x in per.values() if x["group"] == g]
            groups[g] = {"n": len(xs), "long": sum(x["long"] for x in xs),
                         "strip": dict(Counter(str(x["strip"]) for x in xs if x["long"]))}
        out["sets"][name] = {"n": len(per), "long": sum(x["long"] for x in per.values()),
                             "short": sum(not x["long"] for x in per.values()),
                             "terms": dict(sorted(Counter(x["terms"] for x in per.values()).items())),
                             "strip": dict(Counter(str(x["strip"]) for x in per.values() if x["long"])),
                             "groups": groups,
                             "long_ids": sorted(q for q, x in per.items() if x["long"]),
                             "per_query": per}
    return out


def plan_repeat(results: dict) -> list[dict]:
    """Repeat protocol (§6 of the pre-registration): for every required code-check line that differs from ``strict``,
    the same two requests again, twice — systems ``<strict>~r1``, ``<mode>~r1``, then ``~r2`` (strict once per query).
    """
    builders = {"nl": plan_nl, "topic": plan_topic, "qt": plan_qt, "rv": plan_rv}
    want: dict[str, dict[str, list[str]]] = {}
    for mode, v in results["code_check"]["verdict"].items():
        for key in v["classes"]:
            set_, qid = key.split("|", 1)
            want.setdefault(set_, {}).setdefault(qid, []).append(mode)
    out = []
    for r in (1, 2):
        for set_, qs in want.items():
            lines = {(x["query_id"], x["system"]): x for x in builders[set_]()}
            name = nl_system if set_ == "nl" else (lambda m: m)
            for qid, modes in sorted(qs.items()):
                for m in ("strict", *sorted(set(modes), key=MODES.index)):
                    x = lines[(qid, name(m))]
                    out.append({**x, "system": f"{name(m)}~r{r}"})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=[*SETS, "offline", "repeat"])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--results", type=Path, help="repeat: the results JSON of score_e12_20261013.py (code_check)")
    args = ap.parse_args()
    if args.which == "repeat":
        if not args.out or not args.results:
            raise SystemExit("repeat needs --results and --out")
        plan = plan_repeat(json.loads(args.results.read_text(encoding="utf-8")))
        if not plan:
            raise SystemExit("repeat: no differing lines in the code check — nothing to repeat")
        P.write(plan, args.out)
        return
    if args.which == "offline":
        res = offline()
        text = json.dumps(res, ensure_ascii=False, indent=1) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(text.encode("utf-8"))
        for name, s in res["sets"].items():
            print(name, {k: s[k] for k in ("n", "long", "short", "strip")},
                  {g: (x["n"], x["long"]) for g, x in s["groups"].items()})
        return
    if not args.out:
        raise SystemExit("--out is required")
    plan = {"nl": plan_nl, "topic": plan_topic, "qt": plan_qt, "rv": plan_rv}[args.which]()
    P.write(plan, args.out)


if __name__ == "__main__":
    main()
