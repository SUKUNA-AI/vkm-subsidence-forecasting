"""Parsers of MAPDL outputs: ``<jobname>.out`` (and ``.err``), ``<jobname>.mntr`` and ``*VWRITE`` tables.

Everything here is pure text processing (no Ansys needed) and is covered by unit tests with snippets in the format of
Mechanical APDL 2026 R1. Summaries never copy whole files: counts (MAPDL's own totals when present), the first few
messages (shortened), licence failures, load-step/substep/iteration history, bisections (``.mntr`` attempts) and
timings. The echo of the input deck (batch ``-b`` listing, lines ``   12  COMMAND``) is ignored when messages are
searched, so comments in a deck never count as solver messages.
"""
from __future__ import annotations

import re
from typing import Any

MAX_MESSAGES = 5
MAX_MESSAGE_CHARS = 400

_ERROR = re.compile(r"\*\*\*\s*ERROR\s*\*\*\*")
_WARNING = re.compile(r"\*\*\*\s*WARNING\s*\*\*\*")
_NOTE = re.compile(r"\*\*\*\s*NOTE\s*\*\*\*")
_FATAL = re.compile(r"\*\*\*\s*FATAL\s*\*\*\*|\bFATAL ERROR\b", re.IGNORECASE)
_ECHO = re.compile(r"^\s{0,8}\d{1,7}\s{2}\S")                      # batch listing of the input deck
LICENCE_PATTERNS = (
    re.compile(r"licen[sc]e[^\n]{0,120}(?:not available|unavailable|denied|could not|cannot|failed|failure|"
               r"no such feature|expired|exceeded)", re.IGNORECASE),
    re.compile(r"(?:FlexNet|FLEXlm)[^\n]{0,80}error", re.IGNORECASE),
    re.compile(r"ANSLIC_\w+[^\n]{0,80}(?:error|fail)", re.IGNORECASE),
    re.compile(r"\bno licen[sc]es?\b", re.IGNORECASE),
)
_LS_DONE = re.compile(r"\*\*\*\s*LOAD STEP\s+(\d+)\s+SUBSTEP\s+(\d+)\s+COMPLETED\.\s+CUM ITER\s*=\s*(\d+)")
_TIME = re.compile(r"\*\*\*\s*TIME\s*=\s*([-+0-9.EeDd]+)\s+TIME INC\s*=\s*([-+0-9.EeDd]+)")
_CONVERGED = re.compile(r">>>\s*SOLUTION CONVERGED AFTER EQUILIBRIUM ITERATION\s+(\d+)")
_NOT_CONVERGED = re.compile(r"not converged|did not converge|failed to converge|unconverged", re.IGNORECASE)
_NOT_CONVERGED_SETTING = re.compile(r"TERMINATE ANALYSIS IF NOT CONVERGED", re.IGNORECASE)
_BISECT = re.compile(r"\bbisect", re.IGNORECASE)
_RELEASE = re.compile(r"Release\s+(\d{4}\s+R\d)", re.IGNORECASE)
_ELAPSED = re.compile(r"Elapsed Time \(sec\)\s*=\s*([0-9.]+)")
_CP = re.compile(r"CP Time\s*\(sec\)\s*=\s*([0-9.]+)")
_COUNTS = re.compile(r"NUMBER OF (WARNING|ERROR)\s+MESSAGES ENCOUNTERED\s*=\s*(\d+)")
_LICENCE_TIME = re.compile(r"Elapsed time spent obtaining a license\s*:\s*([0-9.]+)")
_SOLVE_TIME = re.compile(r"Elapsed time spent computing solution\s*:\s*([0-9.]+)")
_RUN_COMPLETED = re.compile(r"RUN COMPLETED")
_BUILD_LINE = re.compile(r"RELEASE=\s*(\d{4}\s+R\d)\s+BUILD=\s*([0-9.]+)\s+(UP\d{8})")


def _message_after(lines: list[str], i: int) -> str:
    """The text of the message whose banner is on line ``i`` (banner tail + following non-blank lines)."""
    parts = [re.sub(r"^.*?\*\*\*\s*(ERROR|WARNING|NOTE)\s*\*\*\*\s*", "", lines[i]).strip()]
    for line in lines[i + 1:i + 12]:
        if not line.strip() or _ERROR.search(line) or _WARNING.search(line) or _NOTE.search(line):
            break
        parts.append(line.strip())
    text = " ".join(p for p in parts if p)
    text = re.sub(r"(?:CP|ELAPSED TIME)\s*=\s*[0-9.]+\s*TIME\s*=\s*[0-9:]+", "", text).strip()
    return text[:MAX_MESSAGE_CHARS]


def solver_lines(text: str) -> list[str]:
    """Lines of the output without the echo of the input deck."""
    return [ln for ln in text.splitlines() if not _ECHO.match(ln)]


def licence_failure(text: str) -> str | None:
    body = "\n".join(solver_lines(text))
    for pattern in LICENCE_PATTERNS:
        m = pattern.search(body)
        if m:
            return m.group(0)[:MAX_MESSAGE_CHARS]
    return None


def parse_out(text: str) -> dict[str, Any]:
    """Summary of a MAPDL output (``.out`` or ``.err``)."""
    lines = solver_lines(text)
    body = "\n".join(lines)
    errors, warnings = [], []
    n_err = n_warn = n_note = 0
    for i, line in enumerate(lines):
        if _ERROR.search(line):
            n_err += 1
            if len(errors) < MAX_MESSAGES:
                errors.append(_message_after(lines, i))
        elif _WARNING.search(line):
            n_warn += 1
            if len(warnings) < MAX_MESSAGES:
                warnings.append(_message_after(lines, i))
        elif _NOTE.search(line):
            n_note += 1
    substeps = substep_table(body)
    not_conv = sum(1 for ln in lines if _NOT_CONVERGED.search(ln) and not _NOT_CONVERGED_SETTING.search(ln))
    summary: dict[str, Any] = {
        "errors": n_err, "warnings": n_warn, "notes": n_note,
        "first_errors": errors, "first_warnings": warnings,
        "fatal": bool(_FATAL.search(body)),
        "licence_failure": licence_failure(text),
        "substeps_completed": len(substeps),
        "load_steps_completed": len({r["load_step"] for r in substeps}),
        "equilibrium_iterations": [int(n) for n in _CONVERGED.findall(body)][:1000],
        "not_converged_messages": not_conv,
        "bisection_messages": len(_BISECT.findall(body)),
        "run_completed": bool(_RUN_COMPLETED.search(body)),
    }
    counts = {kind.lower(): int(n) for kind, n in _COUNTS.findall(body)}
    if counts:                                    # MAPDL's own totals at the end of the run are authoritative
        summary["errors_reported"] = counts.get("error")
        summary["warnings_reported"] = counts.get("warning")
        summary["errors"] = max(n_err, counts.get("error", 0))
        summary["warnings"] = max(n_warn, counts.get("warning", 0))
    if m := _BUILD_LINE.search(body):
        summary["release"], summary["build"], summary["update"] = m.group(1), m.group(2), m.group(3)
    elif m := _RELEASE.search(body):
        summary["release"] = m.group(1)
    if substeps:
        summary["last_substep"] = substeps[-1]
    for key, pattern in (("elapsed_s", _ELAPSED), ("cp_s", _CP), ("licence_wait_s", _LICENCE_TIME),
                         ("solve_s", _SOLVE_TIME)):
        found = pattern.findall(body)
        if found:
            summary[key] = float(found[-1])
    return summary


def substep_table(text: str) -> list[dict[str, Any]]:
    """Load step / substep / cumulative iterations / time (from the ``.out`` solution log)."""
    rows = [{"load_step": int(a), "substep": int(b), "cum_iter": int(c)} for a, b, c in _LS_DONE.findall(text)]
    times = _TIME.findall(text)
    for row, (t, inc) in zip(rows, times):
        row["time"] = to_float(t)
        row["time_inc"] = to_float(inc)
    prev = 0
    for row in rows:
        row["iterations"] = row["cum_iter"] - prev
        prev = row["cum_iter"]
    return rows


MNTR_COLUMNS = ("load_step", "substep", "attempt", "iterations", "cum_iter", "increment", "time")


def parse_mntr(text: str) -> dict[str, Any]:
    """``.mntr`` (solution history): one row per attempted substep. A substep with ``attempt > 1`` was bisected
    (cut back); monitor columns (elapsed time, memory, max displacement, plastic/creep strain, residual) are named from
    the third header line."""
    lines = text.splitlines()
    monitor_names: list[str] = []
    for i, ln in enumerate(lines):
        if re.match(r"\s*LOAD\s+SUB-", ln) and i + 2 < len(lines):
            monitor_names = lines[i + 2].split()
            break
    rows = []
    for ln in lines:
        toks = ln.split()
        if len(toks) < len(MNTR_COLUMNS) or not all(re.fullmatch(r"\d+", t) for t in toks[:5]):
            continue
        vals = [to_float(t) for t in toks]
        if any(v is None for v in vals):
            continue
        row: dict[str, Any] = {}
        for name, value in zip(MNTR_COLUMNS, vals):
            row[name] = int(value) if name in ("load_step", "substep", "attempt", "iterations", "cum_iter") \
                else value
        for name, value in zip(monitor_names, vals[len(MNTR_COLUMNS):]):
            row[name] = value
        rows.append(row)
    return {"rows": rows, "monitors": monitor_names,
            "bisected_substeps": sum(1 for r in rows if r["attempt"] > 1),
            "max_iterations": max((r["iterations"] for r in rows), default=0),
            "load_steps": sorted({r["load_step"] for r in rows})}


def to_float(token: str) -> float | None:
    token = token.strip().replace("D", "E").replace("d", "e")
    m = re.fullmatch(r"([-+]?(?:\d+\.?\d*|\.\d+))([-+]\d{3})", token)
    if m:                                                    # Fortran drops the E for 3-digit exponents
        token = f"{m.group(1)}E{m.group(2)}"
    try:
        return float(token)
    except ValueError:
        return None


def parse_kv(text: str) -> dict[str, float]:
    """``name = value`` lines written by ``*VWRITE`` with a Fortran format such as ``('uz_top = ',E24.16)``."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+)\s*$", line)
        if m:
            value = to_float(m.group(2))
            if value is not None:
                out[m.group(1).lower()] = value
    return out


def parse_table(text: str, columns: list[str]) -> list[dict[str, float]]:
    """Whitespace table of numbers (one row per line) written by ``*VWRITE`` → list of dicts."""
    rows = []
    for line in text.splitlines():
        vals = [to_float(v) for v in line.split()]
        if len(vals) != len(columns) or any(v is None for v in vals):
            continue
        rows.append(dict(zip(columns, vals)))
    return rows


def parse_prnsol(text: str) -> dict[int, dict[str, float]]:
    """``PRNSOL`` listing → {node: {column: value}} (column names from the ``NODE`` header line)."""
    out: dict[int, dict[str, float]] = {}
    header: list[str] = []
    for line in text.splitlines():
        toks = line.split()
        if toks and toks[0] == "NODE":
            header = [t.lower() for t in toks[1:]]
            continue
        if header and toks and re.fullmatch(r"\d+", toks[0]) and len(toks) == len(header) + 1:
            vals = [to_float(t) for t in toks[1:]]
            if all(v is not None for v in vals):
                out[int(toks[0])] = dict(zip(header, vals))
    return out
