"""TOPIC_BENCHMARK_V1 experiment harness (post hoc): the variants' requests, the preflight against an API image
without the opt-in fields, and the run lines read by the frozen ``score.load_runs`` / ``score.ranking_of`` and the
adapter ``score_experiments.py``. A local stand-in API (http.server) — no network, no container."""
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
BENCH = ROOT / "benchmarks" / "topic_v1"
SCRIPTS = BENCH / "scripts"
OPT_IN = {"formulations", "expand", "max_per_source"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def api(monkeypatch):
    """Stand-in /v1/search/hybrid: two pages of different sources; an ``old`` image refuses the opt-in fields and a
    pool above 200 (as the deployed API before the flags)."""
    state = {"old": False, "requests": []}

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
            return self._send(404, {"ok": False, "error": {"code": "NOT_FOUND"}})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(req)
            if state["old"] and (OPT_IN & set(req) or req.get("late_candidates", 0) > 200):
                return self._send(400, {"ok": False, "error": {"code": "INVALID_ARGUMENT"}})
            stages = {"late": {"candidates": req.get("late_candidates", 100), "scored": 2}}
            if req.get("formulations") or req.get("expand") == "terms":
                stages["formulations"] = {"status": "APPLIED", "answered": 2, "duplicates_merged": 0,
                                          "runs": [{"n": 0, "origin": "query", "status": "OK", "text": "secret"}]}
            if req.get("max_per_source"):
                stages["source_cap"] = {"max_per_source": req["max_per_source"], "over_cap": 1, "moved": 1,
                                        "sources_over_cap": 1}
            pages = ["VKM-SRC-001:p0001", "VKM-SRC-002:p0003"]
            items = [{"envelope": {"object_id": p, "object_kind": "PAGE", "page_id": p, "source_id": p[:11]},
                      "record": {"duplicates": [], "highlights": ["текст страницы"]}} for p in pages]
            rec = {"late": True, "late_candidates": req.get("late_candidates", 100),
                   "candidates": req.get("candidates"), "fused_total": 2, "timings_ms": {"total": 3.0},
                   "stages": stages, "formulations": {"sent": [{"origin": "translation", "text": "secret"}],
                                                      "skipped": [], "terms": {"status": "APPLIED", "parts": {}}}
                   if req.get("expand") else None}
            return self._send(200, {"ok": True, "item": {"record": rec}, "items": items})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("VKM_BENCH_API", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.delenv("VKM_API_TOKEN_FILE", raising=False)
    harness = _load("topic_harness_experiments", SCRIPTS / "harness_experiments.py")
    harness.SET_JSONL = (BENCH / "topic_set_v1.jsonl").read_text(encoding="utf-8")
    yield harness, state
    server.shutdown()


def test_variant_requests():
    h = _load("topic_harness_experiments_req", SCRIPTS / "harness_experiments.py")
    core = {"query": "q", "kinds": ["PAGE"], "limit": 50, "candidates": 100}          # harness_core hybrid_late
    assert h.request_of("baseline", "q", ["a", "b"]) == core
    assert h.request_of("human3", "q", ["a", "b"]) == {**core, "formulations": ["a", "b"]}
    assert h.request_of("human3", "q", []) == core
    assert h.request_of("terms+cap5+pool200", "q", []) == {**core, "expand": "terms", "max_per_source": 5,
                                                          "candidates": 200, "late_candidates": 200}
    assert h.request_of("pool300", "q", [])["late_candidates"] == 300
    assert set(h.VARIANTS) == {"baseline", "terms", "human3", "cap5", "pool200", "pool300", "terms+cap5",
                               "terms+cap5+pool200"}
    core_h = _load("topic_harness_core_cmp", SCRIPTS / "harness_core.py")
    payload = {"items": [{"envelope": {"page_id": "VKM-SRC-001:p0002", "source_id": "VKM-SRC-001"},
                          "record": {"duplicates": ["VKM-SRC-006:p0002"]}}]}
    assert h.page_hits(payload) == core_h.page_hits(payload)                         # the same hit format


def test_run_lines_are_scored_by_the_frozen_reader(api, tmp_path, capsys):
    harness, state = api
    harness.main(["--variants", "baseline,human3,terms+cap5,pool300", "--limit-queries", "3", "--outlines", "none"])
    out = capsys.readouterr().out
    lines = [json.loads(x) for x in out.splitlines()]
    assert lines[0]["kind"] == "meta" and lines[0]["preflight"] == {"expand": 200, "formulations": 200,
                                                                  "late_candidates": 200, "max_per_source": 200}
    runs = [x for x in lines if x["kind"] == "run"]
    assert len(runs) == 12 and lines[-1]["kind"] == "end"
    human = [r for r in state["requests"] if r.get("formulations") and r["query"] != "оседание"]
    assert len(human) == 3 and all(len(r["formulations"]) == 2 and r["query"] not in r["formulations"]
                                   for r in human)
    assert "secret" not in out and "текст" not in out                              # ids and counters only
    cap = next(r for r in runs if r["system"] == "terms+cap5")
    assert cap["source_cap"]["max_per_source"] == 5 and cap["formulation_plan"]["sent"] == ["translation"]
    path = tmp_path / "exp.jsonl"
    path.write_text(out, encoding="utf-8")
    score = _load("topic_score_frozen", SCRIPTS / "score.py")
    loaded, outlines, meta, _files = score.load_runs([str(path)])
    idx = TB.SectionIndex.from_outlines(outlines)
    for (_qid, system), line in loaded.items():
        assert score.ranking_of(line, idx).pages == ["VKM-SRC-001:p0001", "VKM-SRC-002:p0003"], system
    adapter = _load("topic_score_experiments", SCRIPTS / "score_experiments.py")
    topics = TB.load_set(BENCH / "topic_set_v1.jsonl")
    rows = adapter.score_rows(topics, loaded, idx, adapter.systems_of(loaded))
    assert {r["system"] for r in rows} == {"baseline", "human3", "terms+cap5", "pool300"} and len(rows) == 12
    assert adapter.dilution(loaded, ["baseline"]) == {"baseline": 1.0}


def test_preflight_stops_on_an_api_without_the_flags(api, capsys):
    harness, state = api
    state["old"] = True
    with pytest.raises(SystemExit) as exc:
        harness.main(["--variants", "baseline,cap5", "--limit-queries", "1", "--outlines", "none"])
    assert "max_per_source" in str(exc.value)
    harness.main(["--variants", "baseline,pool200", "--limit-queries", "1", "--outlines", "none"])
    runs = [json.loads(x) for x in capsys.readouterr().out.splitlines() if '"run"' in x]
    assert [r["error"] for r in runs] == [None, None]                               # ≤ 200: every image
