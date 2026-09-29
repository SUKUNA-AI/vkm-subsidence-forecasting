"""Morning summary of the nightly checks (agent OPS, ``infra/core/nightly/nightly_summary.py``): every step's raw
output → a green / yellow / red check, changes since the previous run, what to do; ``summary.md`` in Russian and at
most 25 lines, ``summary.json`` for the 07:30 reader. Synthetic run directories only."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "infra" / "core" / "nightly" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = _load("nightly_summary")
CUR = "snap-20260929T193550Z-738eebee"
NOW = dt.datetime(2026, 9, 30, 1, 25, tzinfo=dt.timezone.utc)          # 04:25 MSK


def green_outputs() -> dict[str, object]:
    checks = lambda prefix, n: [{"check_id": f"{prefix}{i}", "title": "t", "status": "PASS", "count": 0}  # noqa: E731
                                for i in range(1, n + 1)]
    return {
        "containers": "\n".join(json.dumps({"Service": s, "Name": f"vkm-core-{s}-1", "State": "running",
                                            "Health": "healthy" if s != "mcp-admin" else "", "Status": "Up"})
                                for s in ("neo4j", "opensearch", "api", "mcp", "mcp-admin", "rx580-retrieval")),
        "disk": "  1B-blocks        Used       Avail Mounted on\n476432752640 167587889152 284568231936 /\n",
        "canon": {"status": "PASS", "blocking_failures": 0, "warnings": 2, "counts": {}, "not_pass": []},
        "duckdb": {"current": CUR, "snapshot_id": CUR, "duckdb_file": True, "up_to_date": True},
        "graph": checks("C", 16),
        "nav": {"status": "PASS", "snapshot_id": CUR, "checks": checks("N", 7)},
        "search_status": {"aliases": {t: {"indices": [f"vkm-{t}-x"], "built_from_snapshot_id": CUR}
                                      for t in ("pages", "blocks", "figures", "tables", "formulas")},
                          "vectors": {"count": 205784, "built_from_snapshot_id": CUR, "build_id": "b1"}},
        "search": checks("Q", 8),
        "rx580": {"status": "ok", "late_store": {"status": "READY", "pack_id": f"{CUR}-2b0a341cb07a",
                                                 "snapshot_id": CUR, "count": 205784}},
        "hybrid": {"status": "PASS", "late": True, "latency": {"p50_ms": 148.2},
                   "queries": [{"query": "q", "pass": True}] * 3},
        "vectors": {"units": {"count": 205784, "snapshot_id": CUR},
                    "packs": [{"dir": "derived/embeddings/multivector/m/r/s/packs", "pack_id": f"{CUR}-2b0a341cb07a",
                               "count": 205784, "snapshot_id": CUR}]},
        "mcp": {"verdict": "PASS", "expected": 45, "tools_listed": 45, "missing_tools": [],
                "summary": {"PASS": 45, "WARN": 0, "FAIL": 0, "SKIP": 0}, "calls": []},
        "dossiers": {"stored": True},
        "topic_v1": {"n_queries": 351, "errors": 0, "canonical_snapshot_id": CUR,
                     "summary": {"page_recall@10": 0.156, "page_recall@20": 0.2166, "page_recall@50": 0.3031,
                                 "mrr@50": 0.2554, "source_recall@10": 0.4279, "success@10": 0.5185},
                     "registered": {"page_recall@50": 0.3031}},
        "backup": {"edge": {"status": "DONE", "verdict": "PASS", "finished_at": "2026-09-29T23:41:00+00:00",
                            "snapshot": "2026-09-30", "source": {"bytes": 42_100_000_000},
                            "transfer": {"bytes_transferred": 310_000_000},
                            "store": {"count": 7, "fs_free_bytes": 300_000_000_000, "over_budget": False}},
                   "source": {"name": "2026-09-30T0200"}},
    }


def dossier_index(ok=117, total=117, changed=0, self_hit=0.97):
    return {"schema": "vkm.dossier_index/1", "snapshot": CUR, "topics": [],
            "summary": {"topics": total, "ok": ok, "sections": 900, "processes": 400, "process_self_hit": self_hit,
                        "model_self_hit": 0.8},
            "diff": {"changed": changed, "big_changes": 0, "self_hit_drop": 0.0}}


def make_run(base: Path, date: str = "2026-09-30", outputs: dict | None = None, rc: dict | None = None,
             omit: tuple = (), context: dict | None = None, dossiers: dict | None = None) -> Path:
    outputs = green_outputs() if outputs is None else outputs
    rc = rc or {}
    run = base / date
    (run / "raw").mkdir(parents=True)
    ctx = {"date": date, "run_id": "nightly-x", "started_at": "2026-09-30T01:00:02+00:00",
           "canonical_current": CUR, "nav_current": CUR, "catalogues_current": "99f0ee6c6e05", "deep": False,
           "busy": None, **(context or {})}
    (run / "context.json").write_text(json.dumps(ctx), encoding="utf-8")
    steps = []
    for name in ("containers", "disk", "canon", "duckdb", "graph", "nav", "search_status", "search", "rx580",
                 "hybrid", "vectors", "mcp", "dossiers", "topic_v1", "backup"):
        if name in omit:
            continue
        out = outputs.get(name)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        (run / "raw" / f"{name}.out").write_text(text or "", encoding="utf-8")
        (run / "raw" / f"{name}.err").write_text("", encoding="utf-8")
        steps.append({"step": name, "rc": rc.get(name, 0), "seconds": 5, "skipped": False, "note": None})
    (run / "steps.jsonl").write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
    (run / "dossiers_index.json").write_text(json.dumps(dossiers or dossier_index()), encoding="utf-8")
    return run


def by_id(summary):
    return {c["id"]: c for c in summary["checks"]}


# ---------------------------------------------------------------- green
def test_all_green(tmp_path):
    run = make_run(tmp_path)
    s = S.build_summary(run, now=NOW)
    assert s["overall"] == "GREEN" and s["counts"] == {"PASS": 13, "WARN": 0, "FAIL": 0, "SKIP": 0}
    c = by_id(s)
    assert c["graph"]["detail"] == "16/16 PASS" and c["nav"]["detail"] == "7/7 PASS"
    assert c["vectors"]["metrics"]["units"] == 205784 and c["vectors"]["status"] == "PASS"
    assert c["disk"]["metrics"]["free_pct"] == 59.7 and "284,6" in c["disk"]["detail"]
    assert c["backup"]["metrics"]["age_h"] == 1.7 and "30.09 02:41" in c["backup"]["detail"]
    md = s["markdown"]
    lines = md.splitlines()
    assert lines[0] == "# Ночные проверки ВКМ — 2026-09-30: ЗЕЛЁНЫЙ"
    assert "04:00–04:25 МСК" in lines[1] and "NAV = CURRENT" in lines[1]
    assert len(lines) <= 25 and sum(1 for x in lines if x.startswith("[ОК]")) == 13
    assert "Изменения со вчера: первый прогон — сравнивать не с чем" in md and "Что сделать: ничего" in md
    assert md.endswith("summary.json и raw/\n")


def test_summary_files_and_cli(tmp_path, capsys):
    run = make_run(tmp_path)
    assert S.main(["build", "--run-dir", str(run)]) == 0
    assert (run / "summary.md").read_text(encoding="utf-8") == capsys.readouterr().out
    data = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert data["schema"] == S.SCHEMA and data["date"] == "2026-09-30" and data["markdown"]
    assert {c["color"] for c in data["checks"]} == {"green"}


# ---------------------------------------------------------------- red and yellow
def test_failures_turn_red_with_actions(tmp_path):
    out = green_outputs()
    out["graph"][6] = {"check_id": "C7", "title": "t", "status": "FAIL", "count": 3}
    out["search_status"]["vectors"]["count"] = 186116
    out["backup"]["edge"]["finished_at"] = "2026-09-28T11:41:00+00:00"          # 37.7 h old
    run = make_run(tmp_path, outputs=out)
    s = S.build_summary(run, now=NOW)
    c = by_id(s)
    assert s["overall"] == "RED"
    assert c["graph"]["status"] == "FAIL" and "C7" in c["graph"]["detail"]
    assert c["vectors"]["status"] == "FAIL" and "счётчики различаются" in c["vectors"]["detail"]
    assert c["backup"]["status"] == "FAIL" and "37,7 ч" in c["backup"]["detail"]
    assert len(s["actions"]) == 3 and any("lab_refresh.sh" in a for a in s["actions"])
    assert "# Ночные проверки ВКМ — 2026-09-30: КРАСНЫЙ" in s["markdown"]
    assert s["markdown"].count("[СБОЙ]") == 3 and len(s["markdown"].splitlines()) <= 25


def test_timeouts_missing_steps_and_command_errors(tmp_path):
    out = green_outputs()
    out["duckdb"] = ""
    run = make_run(tmp_path, outputs=out, rc={"mcp": 124, "duckdb": 1}, omit=("topic_v1",))
    (run / "raw" / "duckdb.err").write_text("Traceback …\nConfigError: VKM_DATA_ROOT is not set\n", encoding="utf-8")
    s = S.build_summary(run, now=NOW)
    c = by_id(s)
    assert c["mcp"]["status"] == "FAIL" and "превышено время" in c["mcp"]["detail"]
    assert c["duckdb"]["status"] == "FAIL" and "кодом 1" in c["duckdb"]["detail"] and "ConfigError" in c["duckdb"]["detail"]
    assert c["topic_v1"]["status"] == "SKIP" and c["topic_v1"]["detail"] == "шаг не выполнялся"
    assert s["overall"] == "RED"


def test_only_a_skip_is_yellow_and_warnings_are_yellow(tmp_path):
    run = make_run(tmp_path, omit=("topic_v1",))
    assert S.build_summary(run, now=NOW)["overall"] == "YELLOW"
    out = green_outputs()
    out["backup"]["edge"]["verdict"] = "WARN"
    out["nav"]["snapshot_id"] = "snap-20260928T160616Z-5d669f09"
    run = make_run(tmp_path / "b", outputs=out)
    s = S.build_summary(run, now=NOW)
    c = by_id(s)
    assert s["overall"] == "YELLOW" and c["backup"]["status"] == "WARN"
    assert c["nav"]["status"] == "WARN" and "другому снимку" in c["nav"]["detail"]


def test_missing_edge_receipt_and_disk_thresholds(tmp_path):
    out = green_outputs()
    out["backup"] = {"edge": None, "source": {"name": "2026-09-30T0200"}}
    out["disk"] = "476432752640 440000000000 36432752640 /\n"                     # 7.6 % free
    s = S.build_summary(make_run(tmp_path, outputs=out), now=NOW)
    c = by_id(s)
    assert c["backup"]["status"] == "FAIL" and "нет квитанции EDGE" in c["backup"]["detail"]
    assert c["disk"]["status"] == "FAIL"
    assert S.parse_df("Файловая система 1B-блоков\n100 40 60 /\n") == {"size": 100, "used": 40, "free": 60}


def test_mcp_and_dossier_and_topic_thresholds(tmp_path):
    out = green_outputs()
    out["mcp"] = {"verdict": "FAIL", "expected": 45, "tools_listed": 44, "missing_tools": ["rerank_visual"],
                  "summary": {"PASS": 43, "FAIL": 1}, "calls": [{"tool": "rerank_visual", "status": "FAIL"}]}
    out["topic_v1"]["errors"] = 40
    s = S.build_summary(make_run(tmp_path, outputs=out, dossiers=dossier_index(ok=100)), now=NOW)
    c = by_id(s)
    assert c["mcp"]["status"] == "FAIL" and "нет: rerank_visual" in c["mcp"]["detail"]
    assert c["dossiers"]["status"] == "FAIL" and c["dossiers"]["detail"].startswith("100/117")
    assert c["topic_v1"]["status"] == "FAIL" and "ошибок 40" in c["topic_v1"]["detail"]


# ---------------------------------------------------------------- changes since the previous run
def test_changes_against_the_previous_run(tmp_path):
    prev = make_run(tmp_path, date="2026-09-29")
    S.write_summary(prev, S.build_summary(prev, now=NOW - dt.timedelta(days=1)))
    out = green_outputs()
    out["topic_v1"]["summary"]["page_recall@50"] = 0.2701                       # −0.033: a regression signal
    out["disk"] = "476432752640 177587889152 274568231936 /\n"                  # 10 GB less free
    out["graph"][0] = {"check_id": "C1", "title": "t", "status": "FAIL", "count": 1}
    run = make_run(tmp_path, outputs=out, context={"canonical_current": "snap-20260930T000000Z-00000001"},
                   dossiers=dossier_index(changed=4))
    s = S.build_summary(run, now=NOW)
    assert s["prev"]["dir"] == "2026-09-29"
    ch = "\n".join(s["changes"])
    assert f"новый снимок: {CUR} → snap-20260930T000000Z-00000001" in ch
    assert "Граф DOCUMENT C1–C16: [ОК] → [СБОЙ]" in ch
    assert "topic_v1: R@50 0,303→0,270" in ch and "досье: изменились 4 тем" in ch
    assert "диск CORE: свободно 284,6 ГБ → 274,6 ГБ" in ch
    t = by_id(s)["topic_v1"]
    assert t["status"] == "WARN" and "Δ -0,033" in t["detail"]
    assert len(s["markdown"].splitlines()) <= 25


def test_markdown_never_exceeds_25_lines(tmp_path):
    out = green_outputs()
    for c in out["graph"]:
        c["status"] = "FAIL"
    broken = ("canon", "duckdb", "hybrid", "search", "mcp", "nav", "containers")
    for name in broken:
        out[name] = ""
    run = make_run(tmp_path, outputs=out, rc={n: 1 for n in broken}, context={"busy": "vkm-job, lab_refresh"})
    s = S.build_summary(run, now=NOW)
    lines = s["markdown"].splitlines()
    assert len(lines) == 25 and lines[2].startswith("Во время прогона шли задания")
    assert len(s["actions"]) == 8 and lines[-2].endswith("(и ещё 2 — см. summary.json)")
    assert lines[-1].startswith("Подробно:") and lines.index("Что сделать:") == 17


def test_find_prev_prefers_an_earlier_date(tmp_path):
    for d in ("2026-09-27", "2026-09-29", "2026-09-30-rerun-040501", "2026-09-30"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "summary.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-09-28").mkdir()                                            # no summary: ignored
    assert S.find_prev(tmp_path, "2026-09-30", exclude=tmp_path / "2026-09-30").name == "2026-09-29"
    assert S.find_prev(tmp_path, "2026-09-27", exclude=tmp_path / "2026-09-27") is None
    only_today = tmp_path / "t"
    for d in ("2026-10-01-rerun-040501", "2026-10-01"):
        (only_today / d).mkdir(parents=True)
        (only_today / d / "summary.json").write_text("{}", encoding="utf-8")
    assert S.find_prev(only_today, "2026-10-01", exclude=only_today / "2026-10-01").name == "2026-10-01-rerun-040501"


def test_json_text_with_leading_noise():
    assert S.load_json_text('Creating vkm-nightly …\n{"status": "PASS"}\n') == {"status": "PASS"}
    assert S.load_json_text("noise\n[1, 2]") == [1, 2]
    assert S.load_json_text("") is None and S.load_json_text("not json") is None
    assert S.num_ru(205784) == "205 784" and S.num_ru(0.30314, 3) == "0,303" and S.gb_ru(None) == "—"


@pytest.mark.parametrize("status,mark", [("PASS", "[ОК]"), ("WARN", "[ВНИМ]"), ("FAIL", "[СБОЙ]"), ("SKIP", "[ПРОП]")])
def test_marks(status, mark):
    assert S.MARK[status] == mark
