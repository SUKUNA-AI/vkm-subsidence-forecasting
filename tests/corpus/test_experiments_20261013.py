"""Search experiments of 13.10.2026 (benchmarks/topic_v1/PREREGISTRATION_EXPERIMENTS_2026-10-13.md): the plan harness
against a local stand-in API (no network, no container), the plans, the three ranking forms with copy groups and the
decision rules of E6 / E7."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from vkm_corpus.retrieval_lab import topic_bench as TB

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "benchmarks" / "topic_v1" / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def harness(monkeypatch):
    """Stand-in API: /v1/search/hybrid refuses the field ``late_fusion``; hits carry duplicates, copies and texts."""
    state = {"requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/v1/status":
                return self._send(200, {"ok": True, "status": {"canonical": {"snapshot_id": "SNAP-T"}}})
            return self._send(404, {"ok": False})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, req))
            if "late_fusion" in req:
                return self._send(400, {"ok": False, "error": {"code": "INVALID_ARGUMENT"}})
            copies = [{"page_id": "VKM-SRC-202:p0154", "coverage": 0.9, "kinds": ["SAME_WORK_COPY"]}] \
                if req.get("graph") != [] and self.path.endswith("hybrid") else []
            items = [{"envelope": {"object_id": "VKM-SRC-025:p0154", "object_kind": "PAGE",
                                   "page_id": "VKM-SRC-025:p0154", "source_id": "VKM-SRC-025"},
                      "record": {"duplicates": [{"page_id": "VKM-SRC-025:p0155"}], "copies": copies,
                                 "highlights": ["секретный текст страницы"]}},
                     {"envelope": {"object_id": "VKM-SRC-001:p0001", "object_kind": "PAGE",
                                   "page_id": "VKM-SRC-001:p0001", "source_id": "VKM-SRC-001"},
                      "record": {"duplicates": []}}]
            rec = {"late": True, "late_candidates": 100, "candidates": 100, "fused_total": 2,
                   "translation": {"status": "APPLIED", "text": "секретный перевод"},
                   "stages": {"graph": {"requested": req.get("graph"), "status": {"collapse": "APPLIED"}},
                              "formulations": {"status": "APPLIED", "answered": 3,
                                               "runs": [{"n": 1, "origin": "caller", "status": "OK",
                                                         "text": "секретная формулировка"}]}
                              if req.get("formulations") else None},
                   "timings_ms": {"total": 5.0}}
            return self._send(200, {"ok": True, "item": {"record": rec}, "items": items, "meta": {"warnings": []}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("VKM_BENCH_API", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.delenv("VKM_API_TOKEN_FILE", raising=False)
    mod = _load("plan_harness_20261013", SCRIPTS / "harness_plan.py")
    yield mod, state
    server.shutdown()


def _plan(lines: list[dict]) -> str:
    return "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines)


def test_harness_runs_ids_only_and_reads_copies(harness, capsys):
    mod, state = harness
    plan = [{"query_id": "Q1", "topic_id": "T1", "system": "g0", "route": "/v1/search/hybrid",
             "request": {"query": "оседание", "kinds": ["PAGE"], "limit": 50, "graph": []}},
            {"query_id": "Q1", "topic_id": "T1", "system": "g1", "route": "/v1/search/hybrid",
             "request": {"query": "оседание", "kinds": ["PAGE"], "limit": 50, "graph": ["collapse"],
                         "formulations": ["вторая", "третья"]}},
            {"query_id": "Q1", "topic_id": "T1", "system": "bm25", "route": "/v1/search",
             "request": {"query": "оседание", "kinds": ["PAGE"], "limit": 50}}]
    mod.PLAN_JSONL = _plan(plan)
    mod.main([])
    out = capsys.readouterr().out
    assert "секрет" not in out
    lines = [json.loads(x) for x in out.splitlines()]
    assert lines[0]["kind"] == "meta" and lines[-1]["kind"] == "end"
    runs = {x["system"]: x for x in lines if x["kind"] == "run"}
    assert runs["g0"]["hits"][0] == {"page_id": "VKM-SRC-025:p0154", "source_id": "VKM-SRC-025",
                                     "duplicates": ["VKM-SRC-025:p0155"], "copies": []}
    assert runs["g1"]["hits"][0]["copies"] == ["VKM-SRC-202:p0154"]
    assert runs["g1"]["formulations"]["runs"] == [{"n": 1, "origin": "caller", "status": "OK"}]
    assert runs["g1"]["translation_status"] == "APPLIED"
    assert "graph" not in runs["bm25"]
    # preflight: one request per distinct field set, limit 1, before the measured requests
    assert [r[1]["limit"] for r in state["requests"][:3]] == [1, 1, 1]


def test_harness_preflight_stops_on_a_refused_field(harness, capsys):
    mod, state = harness
    mod.PLAN_JSONL = _plan([{"query_id": "Q1", "system": "late_rrf", "route": "/v1/search/hybrid",
                             "request": {"query": "x", "late_fusion": "rrf"}}])
    with pytest.raises(SystemExit, match="preflight"):
        mod.main([])
    assert all(r[1]["limit"] == 1 for r in state["requests"])
    assert '"kind": "run"' not in capsys.readouterr().out


def test_harness_refuses_duplicate_plan_lines(harness):
    mod, _state = harness
    line = {"query_id": "Q1", "system": "g0", "route": "/v1/search/hybrid", "request": {"query": "x"}}
    with pytest.raises(SystemExit, match="duplicate"):
        mod.load_plan(_plan([line, line]))
    with pytest.raises(SystemExit, match="route"):
        mod.load_plan(_plan([{**line, "route": "/v1/page"}]))


def test_plans_e6e7_requests():
    P = _load("plans_20261013", SCRIPTS / "plans_20261013.py")
    topics = P.load_topics()
    llm = {v: P.load_llm(v) for v in P.E6_VARIANTS}
    plan = P.plan_e6e7(topics, llm)
    by = {}
    for x in plan:
        by.setdefault(x["system"], []).append(x)
    assert {k: len(v) for k, v in by.items()} == {"g0": 351, "g1": 351, "g15": 351, "human3": 234, "llm3": 234}
    assert all(x["request"]["graph"] == [] for s in ("g0", "human3", "llm3") for x in by[s])
    assert all(x["request"]["graph"] == ["collapse", "topics"] for x in by["g15"])
    assert all(x["request"]["kinds"] == ["PAGE"] and x["request"]["limit"] == 50 and x["request"]["candidates"] == 100
               and "late" not in x["request"] and "translate" not in x["request"] for x in plan)
    t0 = topics[0]
    q0 = t0["queries"][0]
    h = next(x for x in by["human3"] if x["query_id"] == q0["query_id"])
    assert h["request"]["formulations"] == [q["text"] for q in t0["queries"][1:]]
    m = next(x for x in by["llm3"] if x["query_id"] == q0["query_id"])
    assert m["request"]["formulations"] == llm["NAME"][q0["query_id"]]
    assert not any(x["query_id"].endswith("-2") for x in by["llm3"] + by["human3"])


def test_plans_refuse_a_changed_model_file(tmp_path):
    P = _load("plans_20261013", SCRIPTS / "plans_20261013.py")
    src = (P.LLM_DIR / "llm_name.jsonl").read_bytes()
    (tmp_path / "llm_name.jsonl").write_bytes(src.replace(b"f1", b"f1 ", 1))
    with pytest.raises(SystemExit, match="sha256"):
        P.load_llm("NAME", tmp_path)
    (tmp_path / "llm_name.jsonl").write_bytes(src.replace(b"\n", b"\r\n"))
    assert len(P.load_llm("NAME", tmp_path)) == 117                   # a CRLF checkout keeps the hash (LF-normalised)


def _scorer():
    return _load("score_experiments_20261013", SCRIPTS / "score_experiments_20261013.py")


def _line(hits):
    return {"hits": [{"page_id": p, "duplicates": d, "copies": c} for p, d, c in hits]}


def test_ranking_forms_and_copy_groups():
    S = _scorer()
    g0 = _line([("VKM-SRC-001:p0001", [], []), ("VKM-SRC-202:p0154", [], [])])
    g1 = _line([("VKM-SRC-025:p0154", ["VKM-SRC-025:p0155"], ["VKM-SRC-202:p0154"]), ("VKM-SRC-001:p0001", [], [])])
    groups = S.Groups()
    groups.add_runs({("Q", "g0"): g0, ("Q", "g1"): g1})
    members = groups.members()
    assert groups.find("VKM-SRC-202:p0154") == groups.find("VKM-SRC-025:p0155")
    plain = S.ranking(g1, "plain")
    assert plain.aliases[0] == frozenset({"VKM-SRC-025:p0154", "VKM-SRC-025:p0155"})
    assert "VKM-SRC-202:p0154" in S.ranking(g1, "ordinary").aliases[0]
    grouped0 = S.ranking(g0, "groups", groups, members)                  # the graph [] answer also gets the group
    assert "VKM-SRC-025:p0154" in grouped0.aliases[1]
    t = TB.Topic("T", "PROCESS", "G", "", (), (TB.Target("T/1", "VKM-SRC-025:p0154", "VKM-SRC-025"),
                                                   TB.Target("T/2", "VKM-SRC-202:p0154", "VKM-SRC-202"),
                                                   TB.Target("T/3", "VKM-SRC-009:p0009", "VKM-SRC-009")))
    gt = S.group_topics([t], groups, members)[0]
    assert len(gt.targets) == 2                                            # two copies of one page are one target
    assert TB.query_metrics(gt, grouped0)["page_recall@10"] == 0.5
    assert TB.query_metrics(t, S.ranking(g0, "plain"))["page_recall@10"] == pytest.approx(1 / 3)


def _cmp(delta, p_holm, ci_lo=None, p=None):
    return {"delta": delta, "p_holm": p_holm, "p_value": p if p is not None else p_holm,
            "ci_lo": ci_lo if ci_lo is not None else delta - 0.02, "ci_hi": delta + 0.02, "n_topics": 117}


def test_decide_e6():
    S = _scorer()
    comps = {"V": {}, "P": {}}
    for s in ("NAME", "PARA1"):
        for a, b in S.E6_PAIRS:
            for m in S.TOPIC_METRICS:
                comps["V"][f"{s}:{a} - {s}:{b} | {m}"] = _cmp(0.0, 1.0, ci_lo=-0.01)
                comps["P"][f"{s}:{a} - {s}:{b} | {m}"] = _cmp(0.0, 1.0, ci_lo=-0.01)
    comps["V"]["NAME:llm3 - NAME:base | page_recall@50"] = _cmp(0.08, 0.001)
    judged = {f"{s}:{x}": {"judged@10": 0.9} for s in S.E6_SETS for x in S.E6_SYSTEMS}
    res = {"comparisons": comps, "judged": judged}
    d = S.decide_e6(res)
    assert d["per_set"]["NAME"]["gain_rule"] and not d["per_set"]["PARA1"]["gain_rule"]
    assert d["verdict"].startswith("ЧАСТИЧНО")
    comps["V"]["PARA1:llm3 - PARA1:base | page_recall@50"] = _cmp(0.06, 0.01)
    assert S.decide_e6(res)["verdict"].startswith("НОРМА")
    comps["P"]["PARA1:llm3 - PARA1:base | mrr@50"] = _cmp(-0.05, 0.01)
    assert S.decide_e6(res)["verdict"].startswith("ЧАСТИЧНО")          # harm on P (judged ≥ 0.85)
    judged["PARA1:llm3"]["judged@10"] = 0.8
    d = S.decide_e6(res)
    assert d["verdict"].startswith("НОРМА") and not d["per_set"]["PARA1"]["P_reported"]


def test_decide_e11():
    S = _scorer()
    variants = S.E11_SYSTEMS[1:]
    res = {"judged": {s: {"judged@10": 0.95} for s in S.E11_SYSTEMS},
           "comparisons": {t: {f"{v} - late_default | {m}": _cmp(0.0, 1.0, p=0.9) for v in variants
                               for m in S.TOPIC_METRICS} for t in ("V", "P")}}
    retr = {"summary": {"ordinary": {"P": {"text": {s: {"judged@10": 0.9} for s in S.E11_SYSTEMS},
                                           "visual": {s: {"judged@10": 0.9} for s in S.E11_SYSTEMS}}}},
            "comparisons": {f"ordinary|{t}|{tr}|{v} - late_default|{m}": {"delta": 0.0, "p_value": 1.0}
                            for t in ("V", "P") for tr in ("text", "visual") for v in variants
                            for m in ("ndcg@10", "recall@50")}}
    fam = {}
    for v in variants:
        fam[f"topic V | {v} - late_default | page_recall@50"] = _cmp(0.0, 1.0, ci_lo=-0.01)
        fam[f"retrieval P text | {v} - late_default | ndcg@10"] = _cmp(0.0, 1.0, ci_lo=-0.01)
    assert S.decide_e11(res, retr, fam)["default"] is None
    fam["retrieval P text | late_rrf - late_default | ndcg@10"] = _cmp(0.03, 0.01, ci_lo=0.01)
    d = S.decide_e11(res, retr, fam)
    assert d["late_rrf"]["passes"] and d["default"] == "late_rrf"
    retr["comparisons"]["ordinary|V|visual|late_rrf - late_default|ndcg@10"] = {"delta": -0.03, "p_value": 0.02}
    d = S.decide_e11(res, retr, fam)
    assert not d["late_rrf"]["passes"] and d["late_rrf"]["harm"] and d["default"] is None
    retr["comparisons"]["ordinary|V|visual|late_rrf - late_default|ndcg@10"] = {"delta": 0.0, "p_value": 1.0}
    retr["summary"]["ordinary"]["P"]["text"]["late_bt"]["judged@10"] = 0.8       # another pair: no effect
    assert S.decide_e11(res, retr, fam)["late_rrf"]["passes"]
    retr["summary"]["ordinary"]["P"]["text"]["late_rrf"]["judged@10"] = 0.8      # the P gain is stopped
    d = S.decide_e11(res, retr, fam)
    assert not d["late_rrf"]["passes"] and "retrieval P text ndcg@10" in d["late_rrf"]["stopped_judged"]


def test_decide_e7():
    S = _scorer()
    fam = {f"{a} - {b} | {m}": _cmp(0.0, 1.0, ci_lo=-0.01, p=0.9) for a, b in S.E7_FAMILY for m in S.TOPIC_METRICS}
    res = {"comparisons": {"groups": {"V": dict(fam), "P": dict(fam)}}}
    res["comparisons"]["groups"]["V"]["g15 - g1 | page_recall@50"] = _cmp(0.025, 0.01, ci_lo=0.005)
    retr = {"summary": {"ordinary": {"P": {"text": {"g0": {"judged@10": 0.95}}, "visual": {"g0": {"judged@10": 0.9}}}}},
            "comparisons": {"groups|P|text|g15 - g1|ndcg@10": {"delta": 0.0, "p_value": 1.0},
                            "groups|P|visual|g15 - g1|recall@50": {"delta": -0.05, "p_value": 0.01}}}
    d = S.decide_e7(res, retr, True)
    assert d["G5"]["default"] and d["G1"]["no_harm"]                       # visual R@50 is not a harm criterion
    retr["comparisons"]["groups|P|text|g1 - g0|recall@50"] = {"delta": -0.03, "p_value": 0.02}
    assert not S.decide_e7(res, retr, True)["G1"]["no_harm"]
    retr["summary"]["ordinary"]["P"]["text"]["g0"]["judged@10"] = 0.80
    d = S.decide_e7(res, retr, True)
    assert d["G1"]["no_harm"] and d["G5"]["retrieval_tracks_stopped_judged"] == ["text"] and not d["G5"]["default"]
    assert not S.decide_e7(res, None, False)["G5"]["default"]              # P not reported → no decision
