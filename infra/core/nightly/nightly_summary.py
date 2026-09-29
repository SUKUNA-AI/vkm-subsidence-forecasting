#!/usr/bin/env python3
"""Morning summary of the VKM nightly checks (agent OPS, 29.09.2026; user decision A3: checks at 04:00 MSK, a short
summary for 07:00–08:00 MSK). Standard library only; runs with the host ``python3`` of CORE.

Input — one run directory ``$VKM_DATA_ROOT_HOST/receipts/nightly/<YYYY-MM-DD>/`` written by ``nightly_checks.sh``:
``context.json`` (date, snapshot ids, busy state), ``steps.jsonl`` (one line per step: exit code, seconds, skipped)
and ``raw/<step>.out`` (the JSON the step printed); ``dossiers_index.json`` from ``dossier_store.py``.

Output — ``summary.json`` (schema ``vkm.nightly_summary/1``) and ``summary.md`` (Russian, at most 25 lines: one line
per check with [ОК] / [ВНИМ] / [СБОЙ] / [ПРОП], changes since the previous run, what to do). The previous run is the
newest ``<date>/summary.json`` of an earlier date (else an earlier run of the same date).

    python3 nightly_summary.py build --run-dir <run dir> [--base <receipts/nightly>]
    python3 nightly_summary.py prev --base <receipts/nightly> --date <YYYY-MM-DD>     # prints the previous run dir
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

SCHEMA = "vkm.nightly_summary/1"
MAX_LINES = 25
MSK = dt.timezone(dt.timedelta(hours=3), "MSK")          # Moscow keeps UTC+3 all year (no DST since 2014)
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
RANK = {PASS: 0, SKIP: 1, WARN: 2, FAIL: 3}
MARK = {PASS: "[ОК]", WARN: "[ВНИМ]", FAIL: "[СБОЙ]", SKIP: "[ПРОП]"}
COLOR = {PASS: "green", WARN: "yellow", FAIL: "red", SKIP: "grey"}
OVERALL_RU = {"GREEN": "ЗЕЛЁНЫЙ", "YELLOW": "ЖЁЛТЫЙ", "RED": "КРАСНЫЙ"}
REQUIRED_SERVICES = ("neo4j", "opensearch", "api", "mcp", "rx580-retrieval")
EXPECTED_MCP_TOOLS = 38
DISK_FAIL_PCT, DISK_FAIL_BYTES = 10.0, 30 * 10**9
DISK_WARN_PCT, DISK_WARN_BYTES = 20.0, 60 * 10**9
BACKUP_WARN_H, BACKUP_FAIL_H = 26.0, 36.0
EDGE_FREE_WARN_BYTES = 60 * 10**9
TOPIC_DROP_WARN = 0.02                 # absolute drop of R@50 or MRR@50 against the previous run
TOPIC_METRICS = ("page_recall@10", "page_recall@20", "page_recall@50", "mrr@50", "source_recall@10", "success@10")
ACTIONS = {
    "containers": "поднять сервис: docker compose up -d <сервис> в каталоге compose (OPERATIONS §3)",
    "disk": "освободить место: canon gc, старые сборки поиска и пакеты late (OPERATIONS §5–6)",
    "canon": "причина в raw/canon.out; CURRENT при FAIL не сдвигается (OPERATIONS §6)",
    "duckdb": "пересобрать: vkm-job duckdb build (OPERATIONS §4)",
    "graph": "причина в raw/graph.out; core reconcile или graph rebuild --cascade (OPERATIONS §4)",
    "nav": "nav graph-load --nav-dir /data/derived/navigation/<NAV CURRENT> (OPERATIONS §4)",
    "search": "причина в raw/search.out; search build --smoke --prune или search rollback (OPERATIONS §4)",
    "hybrid": "проверить rx580-retrieval и векторный индекс; lab_refresh.sh --snapshot <CURRENT> (§4a)",
    "vectors": "lab_refresh.sh --snapshot <CURRENT>: счётчики должны сойтись (OPERATIONS §4a)",
    "mcp": "причина в raw/mcp.out; docker compose up -d mcp; токен и VKM_MCP_ALLOWED_HOSTS",
    "dossiers": "причина в raw/dossiers.err и dossiers_index.json; API /v1/topic",
    "topic_v1": "сравнить raw/topic_v1.out с прошлым прогоном: что меняли в поиске или NAV",
    "backup": "status/STATUS и журнал vkm-backup на EDGE; vkm-backup-prepare на CORE (OPERATIONS §8)",
}


# ================================================================================================================ util
def load_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_json_text(text: str) -> Any:
    """The first JSON value of a command's stdout (docker may print lines before it)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener in ("{", "["):
        i = text.find(opener)
        if i >= 0:
            try:
                return json.JSONDecoder().raw_decode(text[i:])[0]
            except ValueError:
                continue
    return None


def load_lines(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def num_ru(value: Any, digits: int = 0) -> str:
    """205784 → «205 784», 0.3031 (digits 3) → «0,303»."""
    if value is None:
        return "—"
    if digits:
        return f"{float(value):.{digits}f}".replace(".", ",")
    return f"{int(round(float(value))):,}".replace(",", " ")


def gb_ru(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) / 1e9:.1f}".replace(".", ",") + " ГБ"


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def msk(t: dt.datetime | None, fmt: str = "%H:%M") -> str:
    return t.astimezone(MSK).strftime(fmt) if t else "—"


def check(cid: str, title: str, status: str, detail: str, **metrics: Any) -> dict[str, Any]:
    return {"id": cid, "title": title, "status": status, "color": COLOR[status], "detail": detail,
            "metrics": {k: v for k, v in metrics.items() if v is not None}}


class Run:
    """A run directory: steps, raw outputs, context."""

    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.context: dict[str, Any] = load_json(run_dir / "context.json") or {}
        self.steps: dict[str, dict[str, Any]] = {s.get("step"): s for s in load_lines(run_dir / "steps.jsonl")}

    def step(self, name: str) -> dict[str, Any] | None:
        return self.steps.get(name)

    def raw(self, name: str) -> Any:
        try:
            return load_json_text((self.dir / "raw" / f"{name}.out").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return None

    def raw_lines(self, name: str) -> list[dict[str, Any]]:
        return load_lines(self.dir / "raw" / f"{name}.out")

    def err_tail(self, name: str, limit: int = 160) -> str:
        try:
            text = (self.dir / "raw" / f"{name}.err").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        return (lines[-1] if lines else "")[:limit]


def not_run(run: Run, name: str) -> str | None:
    """Why a step has no result: skipped, timed out, failed without output — None when it ran."""
    s = run.step(name)
    if s is None:
        return "шаг не выполнялся"
    if s.get("skipped"):
        return "пропущен" + (f": {s['note']}" if s.get("note") else "")
    if s.get("rc") in (124, 137):
        return f"превышено время ({s.get('seconds')} с)"
    return None


def failed_cmd(run: Run, name: str) -> dict[str, Any]:
    """A step that ran but gave no usable output."""
    rc = (run.step(name) or {}).get("rc")
    tail = run.err_tail(name)
    what = "нет результата (пустой или не JSON вывод)" if rc == 0 else f"команда завершилась с кодом {rc}"
    return check(name, TITLES[name], FAIL, what + (f": {tail}" if tail else ""))


def skipped(run: Run, name: str, why: str) -> dict[str, Any]:
    s = run.step(name) or {}
    status = FAIL if s.get("rc") in (124, 137) else SKIP
    return check(name, TITLES[name], status, why)


TITLES = {
    "containers": "Контейнеры vkm-core", "disk": "Диск CORE", "canon": "Снимок: валидатор",
    "duckdb": "DuckDB", "graph": "Граф DOCUMENT C1–C16", "nav": "Граф NAV N1–N7", "search": "Поиск BM25 (smoke)",
    "hybrid": "Гибрид + late (smoke)", "vectors": "Векторы: единицы = dense = late", "mcp": "MCP: инструменты чтения",
    "dossiers": "Досье 117 тем", "topic_v1": "topic_v1 (hybrid_late)", "backup": "Копия на EDGE",
}


# ============================================================================================================= checks
def check_containers(run: Run) -> dict[str, Any]:
    why = not_run(run, "containers")
    if why:
        return skipped(run, "containers", why)
    rows = run.raw_lines("containers")
    if not rows:
        return failed_cmd(run, "containers")
    by = {r.get("Service"): r for r in rows if r.get("Service")}
    bad = []
    for svc in REQUIRED_SERVICES:
        r = by.get(svc)
        if r is None or r.get("State") != "running":
            bad.append(f"{svc}: {'нет' if r is None else r.get('State')}")
        elif (r.get("Health") or "") not in ("", "healthy"):
            bad.append(f"{svc}: {r.get('Health')}")
    running = sum(1 for r in rows if r.get("State") == "running")
    healthy = sum(1 for r in rows if r.get("Health") == "healthy")
    if bad:
        return check("containers", TITLES["containers"], FAIL, "; ".join(bad), running=running, total=len(rows))
    return check("containers", TITLES["containers"], PASS, f"{running} работают, {healthy} healthy",
                 running=running, healthy=healthy, total=len(rows))


def parse_df(text: str) -> dict[str, int] | None:
    for line in reversed((text or "").strip().splitlines()):
        parts = line.split()
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            return {"size": int(parts[0]), "used": int(parts[1]), "free": int(parts[2])}
    return None


def check_disk(run: Run) -> dict[str, Any]:
    why = not_run(run, "disk")
    if why:
        return skipped(run, "disk", why)
    try:
        df = parse_df((run.dir / "raw" / "disk.out").read_text(encoding="utf-8"))
    except OSError:
        df = None
    if not df or not df["size"]:
        return failed_cmd(run, "disk")
    pct = round(100.0 * df["free"] / df["size"], 1)
    status = FAIL if (pct < DISK_FAIL_PCT or df["free"] < DISK_FAIL_BYTES) else \
        WARN if (pct < DISK_WARN_PCT or df["free"] < DISK_WARN_BYTES) else PASS
    return check("disk", TITLES["disk"], status, f"свободно {gb_ru(df['free'])} ({num_ru(pct)} %)",
                 free_bytes=df["free"], size_bytes=df["size"], free_pct=pct)


def check_canon(run: Run) -> dict[str, Any]:
    why = not_run(run, "canon")
    if why:
        return skipped(run, "canon", why)
    r = run.raw("canon")
    if not isinstance(r, dict) or "status" not in r:
        return failed_cmd(run, "canon")
    blocking = r.get("blocking_failures") or 0
    warnings = r.get("warnings") or 0
    blocking_n = blocking if isinstance(blocking, int) else len(blocking)
    warnings_n = warnings if isinstance(warnings, int) else len(warnings)
    deep = " (--deep)" if run.context.get("deep") else ""
    detail = f"{r['status']}{deep}: блокирующих {blocking_n}, предупреждений {warnings_n}"
    if r["status"] != "PASS":
        ids = [c.get("check_id") for c in (r.get("not_pass") or []) if c.get("status") == "FAIL"][:4]
        detail += (f"; {', '.join(map(str, ids))}" if ids else "")
    return check("canon", TITLES["canon"], PASS if r["status"] == "PASS" else FAIL, detail,
                 blocking=blocking_n, warnings=warnings_n, deep=bool(run.context.get("deep")))


def check_duckdb(run: Run) -> dict[str, Any]:
    why = not_run(run, "duckdb")
    if why:
        return skipped(run, "duckdb", why)
    r = run.raw("duckdb")
    if not isinstance(r, dict) or "current" not in r:
        return failed_cmd(run, "duckdb")
    if r.get("up_to_date"):
        return check("duckdb", TITLES["duckdb"], PASS, "построена из CURRENT", snapshot=r.get("snapshot_id"))
    return check("duckdb", TITLES["duckdb"], FAIL,
                 f"снимок DuckDB {r.get('snapshot_id') or 'нет файла'}, CURRENT {r.get('current')}",
                 snapshot=r.get("snapshot_id"))


def _check_list(items: list[dict[str, Any]]) -> tuple[int, int, list[str], list[str]]:
    n_pass = sum(1 for c in items if c.get("status") == PASS)
    fails = [str(c.get("check_id")) for c in items if c.get("status") == FAIL]
    warns = [str(c.get("check_id")) for c in items if c.get("status") == WARN]
    return n_pass, len(items), fails, warns


def check_graph(run: Run) -> dict[str, Any]:
    why = not_run(run, "graph")
    if why:
        return skipped(run, "graph", why)
    r = run.raw("graph")
    if not isinstance(r, list) or not r:
        return failed_cmd(run, "graph")
    n_pass, n, fails, warns = _check_list(r)
    status = FAIL if fails else WARN if warns else PASS
    detail = f"{n_pass}/{n} PASS" + (f"; FAIL: {', '.join(fails[:6])}" if fails else "") + \
        (f"; WARN: {', '.join(warns[:6])}" if warns else "")
    return check("graph", TITLES["graph"], status, detail, passed=n_pass, total=n, failed=fails or None)


def check_nav(run: Run) -> dict[str, Any]:
    why = not_run(run, "nav")
    if why:
        return skipped(run, "nav", why)
    r = run.raw("nav")
    if not isinstance(r, dict):
        return failed_cmd(run, "nav")
    if r.get("status") == SKIP:
        return check("nav", TITLES["nav"], WARN, f"не проверен: {r.get('reason')}")
    checks = r.get("checks") or []
    n_pass, n, fails, warns = _check_list(checks)
    status = FAIL if (r.get("status") != PASS or fails) else WARN if warns else PASS
    detail = f"{n_pass}/{n} PASS" + (f"; FAIL: {', '.join(fails[:6])}" if fails else "")
    current = run.context.get("canonical_current")
    if r.get("snapshot_id") and current and r["snapshot_id"] != current:
        status = max(status, WARN, key=RANK.get)
        detail += "; NAV построен по другому снимку"
    return check("nav", TITLES["nav"], status, detail, passed=n_pass, total=n, nav_snapshot=r.get("snapshot_id"))


def check_search(run: Run) -> dict[str, Any]:
    why = not_run(run, "search")
    if why:
        return skipped(run, "search", why)
    r = run.raw("search")
    if not isinstance(r, list) or not r:
        return failed_cmd(run, "search")
    n_pass, n, fails, warns = _check_list(r)
    status = FAIL if fails else WARN if warns else PASS
    detail = f"{n_pass}/{n} PASS" + (f"; FAIL: {', '.join(fails[:6])}" if fails else "")
    st = run.raw("search_status")
    current = run.context.get("canonical_current")
    if isinstance(st, dict):
        built = {k: (v or {}).get("built_from_snapshot_id") for k, v in (st.get("aliases") or {}).items()
                 if (v or {}).get("indices")}
        behind = sorted(k for k, v in built.items() if current and v != current)
        if behind:
            status = max(status, WARN, key=RANK.get)
            detail += f"; индексы не на CURRENT: {', '.join(behind)}"
    return check("search", TITLES["search"], status, detail, passed=n_pass, total=n)


def check_hybrid(run: Run) -> dict[str, Any]:
    why = not_run(run, "hybrid")
    if why:
        return skipped(run, "hybrid", why)
    r = run.raw("hybrid")
    if not isinstance(r, dict) or "status" not in r:
        return failed_cmd(run, "hybrid")
    qs = r.get("queries") or []
    n_ok = sum(1 for q in qs if q.get("pass"))
    p50 = (r.get("latency") or {}).get("p50_ms")
    detail = f"{n_ok}/{len(qs)} PASS, p50 {num_ru(p50)} мс"
    bad = [q.get("error") for q in qs if not q.get("pass") and q.get("error")]
    if bad:
        detail += f"; {', '.join(sorted(set(map(str, bad)))[:3])}"
    return check("hybrid", TITLES["hybrid"], PASS if r["status"] == PASS else FAIL, detail, passed=n_ok,
                 total=len(qs), p50_ms=p50, late=r.get("late"))


def check_vectors(run: Run) -> dict[str, Any]:
    why = not_run(run, "vectors")
    if why:
        return skipped(run, "vectors", why)
    v = run.raw("vectors") or {}
    st = run.raw("search_status") or {}
    rx = run.raw("rx580") or {}
    current = run.context.get("canonical_current")
    units = v.get("units") or {}
    dense = (st.get("vectors") or {}) if isinstance(st, dict) else {}
    served = (rx.get("late_store") or {}) if isinstance(rx, dict) else {}
    packs = [p for p in (v.get("packs") or []) if isinstance(p, dict)]
    pointer = next((p for p in packs if p.get("pack_id") == served.get("pack_id")), packs[0] if packs else {})
    counts = {"units": units.get("count"), "dense": dense.get("count"), "late": served.get("count")}
    snaps = {"units": units.get("snapshot_id"), "dense": dense.get("built_from_snapshot_id"),
             "late": served.get("snapshot_id")}
    problems = []
    if None in counts.values():
        missing = [k for k, x in counts.items() if x is None]
        problems.append("нет данных: " + ", ".join(missing))
    elif len(set(counts.values())) != 1:
        problems.append("счётчики различаются: " + ", ".join(f"{k} {num_ru(x)}" for k, x in counts.items()))
    off = [k for k, s in snaps.items() if s and current and s != current]
    if off:
        problems.append("не на CURRENT: " + ", ".join(off))
    status = FAIL if problems else PASS
    detail = "; ".join(problems) if problems else f"{num_ru(counts['units'])} = {num_ru(counts['dense'])} = " \
                                                  f"{num_ru(counts['late'])}"
    if not problems and pointer and served.get("pack_id") and pointer.get("pack_id") != served.get("pack_id"):
        status, detail = WARN, detail + "; обслуживается не пакет из packs/CURRENT"
    return check("vectors", TITLES["vectors"], status, detail, units=counts["units"], dense=counts["dense"],
                 late=counts["late"], late_pack=served.get("pack_id"), dense_build=dense.get("build_id"))


def check_mcp(run: Run) -> dict[str, Any]:
    why = not_run(run, "mcp")
    if why:
        return skipped(run, "mcp", why)
    r = run.raw("mcp")
    if not isinstance(r, dict) or "verdict" not in r:
        return failed_cmd(run, "mcp")
    summ = r.get("summary") or {}
    n_ok = summ.get(PASS, 0)
    listed = r.get("tools_listed")
    detail = f"{n_ok}/{r.get('expected', EXPECTED_MCP_TOOLS)} отвечают" + \
        (f", в списке {listed}" if listed is not None and listed != r.get("expected") else "")
    if r.get("missing_tools"):
        detail += f"; нет: {', '.join(r['missing_tools'][:5])}"
    bad = [c.get("tool") for c in r.get("calls") or [] if c.get("status") == FAIL]
    if bad:
        detail += f"; сбой: {', '.join(bad[:5])}"
    warn = [c.get("tool") for c in r.get("calls") or [] if c.get("status") in (WARN, SKIP)]
    if warn and not bad:
        detail += f"; без проверки/ошибка данных: {', '.join(warn[:5])}"
    status = r["verdict"] if r["verdict"] in RANK else FAIL
    return check("mcp", TITLES["mcp"], status, detail, passed=n_ok, failed=len(bad), listed=listed)


def check_dossiers(run: Run, prev: dict[str, Any] | None) -> dict[str, Any]:
    why = not_run(run, "dossiers")
    if why:
        return skipped(run, "dossiers", why)
    idx = load_json(run.dir / "dossiers_index.json")
    if not isinstance(idx, dict) or "topics" not in idx:
        return failed_cmd(run, "dossiers")
    s = idx.get("summary") or {}
    total, ok = s.get("topics", 0), s.get("ok", 0)
    diff = idx.get("diff") or {}
    changed = diff.get("changed")
    detail = f"{ok}/{total}" + (f"; изменились {changed}" if changed is not None else "; сравнивать не с чем")
    self_hit = s.get("process_self_hit"), s.get("model_self_hit")
    if self_hit[0] is not None:
        share = lambda x: "—" if x is None else f"{num_ru(100 * x)} %"  # noqa: E731
        detail += f"; свой PC-xx у {share(self_hit[0])} тем, своё семейство MM у {share(self_hit[1])}"
    status = PASS
    if total and ok < 0.9 * total:
        status = FAIL
    elif ok < total or (diff.get("big_changes") or 0) > max(3, total // 10) or (diff.get("self_hit_drop") or 0) > 0.05:
        status = WARN
    return check("dossiers", TITLES["dossiers"], status, detail, ok=ok, topics=total, changed=changed,
                 big_changes=diff.get("big_changes"), process_self_hit=self_hit[0], model_self_hit=self_hit[1],
                 sections=s.get("sections"), processes=s.get("processes"), snapshot=idx.get("snapshot"))


def check_topic(run: Run, prev: dict[str, Any] | None) -> dict[str, Any]:
    why = not_run(run, "topic_v1")
    if why:
        return skipped(run, "topic_v1", why)
    r = run.raw("topic_v1")
    if not isinstance(r, dict) or not r.get("summary"):
        return failed_cmd(run, "topic_v1")
    m = r["summary"]
    prev_m = ((prev or {}).get("metrics") or {}).get("topic_v1") or {}
    delta = {k: round(m[k] - prev_m[k], 4) for k in TOPIC_METRICS
             if isinstance(m.get(k), (int, float)) and isinstance(prev_m.get(k), (int, float))}
    reg = r.get("registered") or {}
    detail = f"R@50 {num_ru(m.get('page_recall@50'), 3)}"
    if "page_recall@50" in delta:
        detail += f" (Δ {delta['page_recall@50']:+.3f})".replace(".", ",")
    elif reg.get("page_recall@50") is not None:
        detail += f" (регистрация {num_ru(reg['page_recall@50'], 3)})"
    detail += f", MRR {num_ru(m.get('mrr@50'), 3)}, S@10 {num_ru(m.get('success@10'), 3)}"
    errors = r.get("errors") or 0
    n = r.get("n_queries") or 0
    status = PASS
    if not n or errors > 0.1 * n:
        status = FAIL
    elif errors or any(delta.get(k, 0) < -TOPIC_DROP_WARN for k in ("page_recall@50", "mrr@50")):
        status = WARN
    if errors:
        detail += f"; ошибок {errors}"
    out = check("topic_v1", TITLES["topic_v1"], status, detail, n_queries=n, errors=errors, delta=delta or None,
                canonical_snapshot=r.get("canonical_snapshot_id"))
    out["metrics"].update({k: m.get(k) for k in TOPIC_METRICS if m.get(k) is not None})
    return out


def check_backup(run: Run, now: dt.datetime) -> dict[str, Any]:
    why = not_run(run, "backup")
    if why:
        return skipped(run, "backup", why)
    r = run.raw("backup") or {}
    edge, source = r.get("edge"), r.get("source")
    if not isinstance(edge, dict):
        detail = "нет квитанции EDGE (receipts/backup/edge/latest.json)"
        if isinstance(source, dict):
            detail += f"; манифест CORE {source.get('name')}"
        return check("backup", TITLES["backup"], FAIL, detail)
    finished = parse_time(edge.get("finished_at"))
    age_h = round((now - finished).total_seconds() / 3600, 1) if finished else None
    verdict = edge.get("verdict")
    store = edge.get("store") or {}
    transfer = edge.get("transfer") or {}
    src = edge.get("source") or {}
    detail = (f"{msk(finished, '%d.%m %H:%M')}, {verdict or edge.get('status')}, "
              f"{gb_ru(src.get('bytes'))} (новых {gb_ru(transfer.get('bytes_transferred'))}), "
              f"снимков {store.get('count', '—')}, EDGE свободно {gb_ru(store.get('fs_free_bytes'))}")
    status = PASS
    if edge.get("status") == "FAILED" or verdict == FAIL or age_h is None or age_h > BACKUP_FAIL_H:
        status = FAIL
    elif verdict == WARN or age_h > BACKUP_WARN_H or store.get("over_budget") or \
            (store.get("fs_free_bytes") is not None and store["fs_free_bytes"] < EDGE_FREE_WARN_BYTES):
        status = WARN
    if age_h is not None and age_h > BACKUP_WARN_H:
        detail += f"; квитанции {num_ru(age_h, 1)} ч"
    if edge.get("note"):
        detail += f"; {str(edge['note'])[:120]}"
    return check("backup", TITLES["backup"], status, detail, verdict=verdict, age_h=age_h,
                 snapshot=edge.get("snapshot"), logical_bytes=src.get("bytes"),
                 new_bytes=transfer.get("bytes_transferred"), store_bytes=store.get("store_bytes"),
                 snapshots=store.get("count"), edge_free_bytes=store.get("fs_free_bytes"))


# ============================================================================================================ summary
def find_prev(base: Path, date: str, exclude: Path | None = None) -> Path | None:
    """The newest run with a summary.json of a date before ``date``; else an earlier run of the same date."""
    if not base.is_dir():
        return None
    dated, same = [], []
    for p in base.iterdir():
        if not p.is_dir() or (exclude is not None and p.resolve() == exclude.resolve()):
            continue
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(-rerun-\d{6})?", p.name)
        if not m or not (p / "summary.json").is_file():
            continue
        if m.group(1) < date:
            dated.append(p)
        elif m.group(1) == date:
            same.append(p)
    pick = sorted(dated, key=lambda p: p.name)[-1:] or sorted(same, key=lambda p: p.name)[-1:]
    return pick[0] if pick else None


def changes(summary: dict[str, Any], prev: dict[str, Any] | None) -> list[str]:
    if not prev:
        return ["первый прогон — сравнивать не с чем"]
    out = []
    snap, psnap = summary["snapshot"], prev.get("snapshot") or {}
    if snap.get("canonical") != psnap.get("canonical"):
        out.append(f"новый снимок: {psnap.get('canonical') or '—'} → {snap.get('canonical') or '—'}")
    if snap.get("nav") != psnap.get("nav") and snap.get("nav"):
        out.append(f"NAV: {psnap.get('nav') or '—'} → {snap.get('nav')}")
    pstat = {c["id"]: c["status"] for c in prev.get("checks") or []}
    for c in summary["checks"]:
        before = pstat.get(c["id"])
        if before and before != c["status"]:
            out.append(f"{c['title']}: {MARK[before]} → {MARK[c['status']]}")
    m, pm = summary["metrics"], prev.get("metrics") or {}
    t, pt = m.get("topic_v1") or {}, pm.get("topic_v1") or {}
    moved = [f"{k.split('@')[0].replace('page_recall', 'R').replace('mrr', 'MRR')}@{k.split('@')[1]} "
             f"{pt[k]:.3f}→{t[k]:.3f}".replace(".", ",")
             for k in ("page_recall@50", "mrr@50") if isinstance(t.get(k), (int, float)) and
             isinstance(pt.get(k), (int, float)) and abs(t[k] - pt[k]) >= 0.001]
    if moved:
        out.append("topic_v1: " + ", ".join(moved))
    d = m.get("dossiers") or {}
    if d.get("changed"):
        out.append(f"досье: изменились {d['changed']} тем (разделы/процессы)")
    u, pu = (m.get("vectors") or {}).get("units"), (pm.get("vectors") or {}).get("units")
    if u is not None and pu is not None and u != pu:
        out.append(f"единиц поиска: {num_ru(pu)} → {num_ru(u)}")
    b = m.get("backup") or {}
    if b.get("new_bytes"):
        out.append(f"копия: новых {gb_ru(b['new_bytes'])}, снимков {b.get('snapshots', '—')}")
    f, pf = (m.get("disk") or {}).get("free_bytes"), (pm.get("disk") or {}).get("free_bytes")
    if f is not None and pf is not None and abs(f - pf) >= 2 * 10**9:
        out.append(f"диск CORE: свободно {gb_ru(pf)} → {gb_ru(f)}")
    return out or ["нет"]


def build_summary(run_dir: Path, *, base: Path | None = None, now: dt.datetime | None = None,
                  prev_dir: Path | None = None) -> dict[str, Any]:
    run = Run(run_dir)
    now = now or dt.datetime.now(dt.timezone.utc)
    base = base or run_dir.parent
    date = run.context.get("date") or run_dir.name[:10]
    if prev_dir is None:
        prev_dir = find_prev(base, date, exclude=run_dir)
    prev = load_json(prev_dir / "summary.json") if prev_dir else None
    checks = [check_containers(run), check_disk(run), check_canon(run), check_duckdb(run), check_graph(run),
              check_nav(run), check_search(run), check_hybrid(run), check_vectors(run), check_mcp(run),
              check_dossiers(run, prev), check_topic(run, prev), check_backup(run, now)]
    worst = max((c["status"] for c in checks), key=RANK.get)
    overall = "RED" if worst == FAIL else "YELLOW" if worst in (WARN,) else "GREEN"
    if overall == "GREEN" and any(c["status"] == SKIP for c in checks):
        overall = "YELLOW"
    metrics = {c["id"]: c["metrics"] for c in checks}
    summary: dict[str, Any] = {
        "schema": SCHEMA, "date": date, "run_id": run.context.get("run_id"), "host": "CORE",
        "started_at": run.context.get("started_at"), "finished_at": now.isoformat(timespec="seconds"),
        "overall": overall, "overall_ru": OVERALL_RU[overall],
        "counts": {s: sum(1 for c in checks if c["status"] == s) for s in (PASS, WARN, FAIL, SKIP)},
        "snapshot": {"canonical": run.context.get("canonical_current"), "nav": run.context.get("nav_current"),
                     "catalogues": run.context.get("catalogues_current")},
        "busy": run.context.get("busy") or None,
        "checks": checks, "metrics": metrics,
        "prev": {"dir": prev_dir.name if prev_dir else None, "date": (prev or {}).get("date"),
                 "overall": (prev or {}).get("overall")},
    }
    summary["changes"] = changes(summary, prev)
    summary["actions"] = [f"{c['title']}: {ACTIONS[c['id']]}" for c in checks if c["status"] in (FAIL, WARN)] or \
        ["ничего"]
    summary["markdown"] = render(summary)
    return summary


def render(s: dict[str, Any]) -> str:
    started = parse_time(s.get("started_at"))
    finished = parse_time(s.get("finished_at"))
    snap = s["snapshot"]
    head = [f"# Ночные проверки ВКМ — {s['date']}: {s['overall_ru']}",
            f"Прогон {msk(started)}–{msk(finished)} МСК · снимок {snap.get('canonical') or '—'}"
            + (" · NAV = CURRENT" if snap.get("nav") and snap.get("nav") == snap.get("canonical")
               else f" · NAV {snap.get('nav') or '—'}")]
    if s.get("busy"):
        head.append(f"Во время прогона шли задания ({s['busy']}) — возможны ложные сбои")
    body = [f"{MARK[c['status']]} {c['title']}: {c['detail']}" for c in s["checks"]]
    footer = [f"Подробно: receipts/nightly/{s['date']}/summary.json и raw/"]
    ch, act = list(s["changes"]) or ["нет"], list(s["actions"]) or ["ничего"]
    room = MAX_LINES - len(head) - len(body) - len(footer)       # lines for the two blocks, headers included

    def need(items: list[str]) -> int:
        return 1 if len(items) == 1 else 1 + len(items)

    act_lines = max(1, min(need(act), room - 1))                 # what to do first; changes keep ≥ 1 line
    ch_lines = max(1, min(need(ch), room - act_lines))
    lines = head + body + block("Изменения со вчера", ch, ch_lines) + block("Что сделать", act, act_lines) + footer
    return "\n".join(lines[:MAX_LINES]) + "\n"


def block(title: str, items: list[str], max_lines: int) -> list[str]:
    """A titled list in at most ``max_lines`` lines: one item inline, else a header and «- item» lines."""
    if len(items) == 1:
        return [f"{title}: {items[0]}"]
    if max_lines <= 1:
        return [f"{title}: {len(items)} пунктов — см. summary.json"]
    shown = items[:max_lines - 1]
    out = [f"{title}:"] + [f"- {x}" for x in shown]
    if len(items) > len(shown):
        out[-1] += f" (и ещё {len(items) - len(shown)} — см. summary.json)"
    return out


def write_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    for name, text in (("summary.json", json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True) + "\n"),
                       ("summary.md", summary["markdown"])):
        tmp = run_dir / (name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, run_dir / name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nightly_summary.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="summary.json + summary.md of a run directory")
    b.add_argument("--run-dir", required=True)
    b.add_argument("--base", help="receipts/nightly (default: the parent of --run-dir)")
    b.add_argument("--prev-dir", help="compare with this run instead of the automatic previous one")
    b.add_argument("--dry-run", action="store_true", help="print the markdown only; write nothing")
    p = sub.add_parser("prev", help="print the previous run directory (empty if none)")
    p.add_argument("--base", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--exclude")
    args = ap.parse_args(argv)
    if args.cmd == "prev":
        found = find_prev(Path(args.base), args.date, Path(args.exclude) if args.exclude else None)
        print(found or "")
        return 0
    run_dir = Path(args.run_dir)
    summary = build_summary(run_dir, base=Path(args.base) if args.base else None,
                            prev_dir=Path(args.prev_dir) if args.prev_dir else None)
    if not args.dry_run:
        write_summary(run_dir, summary)
    sys.stdout.write(summary["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
