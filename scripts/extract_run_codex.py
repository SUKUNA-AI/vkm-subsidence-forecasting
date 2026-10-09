#!/usr/bin/env python3
"""World passport extraction, step 2b: run the labelled producer (GPT-6.1 Sol through ``codex exec`` on the owner's
ChatGPT subscription) on packets, one independent ``codex exec`` per packet, several in parallel.

For every packet ``<work>/sol/<run>/<packet_id>/`` receives: ``prompt.txt`` (task + packet, sent on stdin),
``events.jsonl`` (the ``--json`` stream), ``stderr.log``, ``answer.json`` (last message, ``-o``), ``meta.json``
(sha256 of packet, task, schema and prompt; codex version, model, effort, flags; times; exit code; token usage;
attempts) and the marker ``DONE``. A rerun skips packets with ``DONE`` (resume after a stop). A usage-limit error
pauses all workers until the reset the message names (or a fixed wait) and retries the packet.

Codex gets an empty working directory, a read-only sandbox, no user config (no MCP servers), no tools (shell, web,
browser, memories, agents disabled) and must not run or write anything. Model and effort are passed on every call;
``~/.codex/config.toml`` is neither read nor changed. Answers are AUTO_EXTRACTED_UNREVIEWED.

Usage:  python scripts/extract_run_codex.py --work <dir> --run <label> --effort high [--packets id,id | --tier A]
        [--limit N] [--parallel 4] [--status]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MODEL = "gpt-6.1-sol"
DISABLED = ("shell_tool", "unified_exec", "multi_agent", "apps", "browser_use", "computer_use", "image_generation",
            "memories", "plugins", "view_image", "goals", "skill_search", "tool_suggest", "in_app_browser")
LIMIT_RX = re.compile(r"usage limit|rate.?limit|too many requests|\b429\b|quota|limit reached|hit your", re.I)
TRANSIENT_RX = re.compile(r"stream disconnected|timed out|timeout|connection|503|502|500|overloaded|unavailable", re.I)
TIMEOUT_S = 100 * 60
DEFAULT_LIMIT_WAIT_S = 20 * 60
MAX_ATTEMPTS = 4

_pause_until = 0.0
_lock = threading.Lock()
_log_lock = threading.Lock()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def find_codex() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    found = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not found:
        exe = shutil.which("codex")
        if exe:
            return Path(exe)
        raise SystemExit("codex.exe not found under %LOCALAPPDATA%\\OpenAI\\Codex\\bin")
    return found[0]


def wait_seconds(text: str) -> int:
    """Seconds to wait from a usage-limit message («try again in 2 hours 13 minutes», «in 45m», «at 14:05»)."""
    t = text.lower()
    total = 0
    for n, u in re.findall(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", t):
        n = int(n)
        total += n * 3600 if u.startswith("h") else n * 60 if u.startswith("m") else n
    if total:
        return min(total + 60, 6 * 3600)
    m = re.search(r"(?:at|после)\s+(\d{1,2}):(\d{2})", t)
    if m:
        nowl = dt.datetime.now()
        target = nowl.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if target <= nowl:
            target += dt.timedelta(days=1)
        return int((target - nowl).total_seconds()) + 60
    return DEFAULT_LIMIT_WAIT_S


def log(work: Path, run: str, msg: str) -> None:
    line = f"{now()} {msg}"
    with _log_lock:
        with open(work / "sol" / run / "run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)


def command(codex: Path, effort: str, schema: Path, answer: Path, cwd: Path) -> list[str]:
    cmd = [str(codex), "exec", "-m", MODEL, "-c", f'model_reasoning_effort="{effort}"', "-c", 'web_search="disabled"',
           "--ignore-user-config"]
    for f in DISABLED:
        cmd += ["--disable", f]
    cmd += ["--output-schema", str(schema), "-o", str(answer), "--json", "--ephemeral", "-s", "read-only",
            "--skip-git-repo-check", "-C", str(cwd), "--color", "never", "-"]
    return cmd


def usage_of(events_text: str) -> dict:
    u = {}
    for line in events_text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            for k, v in ev["usage"].items():
                if isinstance(v, int):
                    u[k] = u.get(k, 0) + v
    return u


def errors_of(events_text: str) -> list[str]:
    out = []
    for line in events_text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") in ("error", "turn.failed"):
            out.append(json.dumps(ev, ensure_ascii=False)[:1000])
    return out


def run_packet(pid: str, a, codex: Path, version: str, task: bytes, schema_path: Path) -> dict:
    global _pause_until
    pk = json.loads((a.work / "packets" / f"{pid}.json").read_text(encoding="utf-8"))
    body = (a.work / "packets" / f"{pid}.txt").read_bytes()
    if sha(body) != pk["manifest"]["packet_sha256"]:
        raise SystemExit(f"{pid}: packet text sha256 differs from its manifest")
    d = a.work / "sol" / a.run / pid
    if (d / "DONE").is_file():
        return {"packet_id": pid, "status": "SKIPPED_DONE"}
    d.mkdir(parents=True, exist_ok=True)
    prompt = task + b"\n\n" + body
    (d / "prompt.txt").write_bytes(prompt)
    cwd = a.work / "sol_cwd" / f"{a.run}__{pid}"
    cwd.mkdir(parents=True, exist_ok=True)
    meta = {"packet_id": pid, "run": a.run, "producer": {"codex_cli": version, "model": MODEL, "effort": a.effort,
            "auth": "ChatGPT subscription (codex login)", "disabled_features": list(DISABLED),
            "flags": ["--ignore-user-config", "--ephemeral", "-s read-only", "--skip-git-repo-check",
                      "--output-schema", "--json", 'web_search="disabled"']},
            "sha256": {"packet": pk["manifest"]["packet_sha256"], "task": sha(task),
                       "schema": sha(schema_path.read_bytes()), "prompt": sha(prompt)},
            "page_ids": pk["manifest"]["page_ids"], "tier": pk["manifest"]["tier"],
            "review_status": "AUTO_EXTRACTED_UNREVIEWED", "attempts": []}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        while True:
            with _lock:
                wait = _pause_until - time.time()
            if wait <= 0:
                break
            time.sleep(min(wait, 60))
        ans = d / "answer.json"
        if ans.exists():
            ans.unlink()
        t0 = time.time()
        rec = {"attempt": attempt, "started_at": now()}
        ev_path, err_path = d / f"events.attempt{attempt}.jsonl", d / f"stderr.attempt{attempt}.log"
        with open(ev_path, "wb") as fo, open(err_path, "wb") as fe:       # streamed: progress is visible on disk
            proc = subprocess.Popen(command(codex, a.effort, schema_path, ans, cwd), stdin=subprocess.PIPE,
                                    stdout=fo, stderr=fe, cwd=str(cwd))
            try:
                proc.communicate(input=prompt, timeout=TIMEOUT_S)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                code = "TIMEOUT"
        rec.update(finished_at=now(), seconds=round(time.time() - t0, 1), exit_code=code)
        ev_text = ev_path.read_text(encoding="utf-8", errors="replace")
        err_text = err_path.read_text(encoding="utf-8", errors="replace")
        rec["usage"] = usage_of(ev_text)
        rec["errors"] = errors_of(ev_text)
        ok = code == 0 and ans.is_file()
        if ok:
            try:
                parsed = json.loads(ans.read_text(encoding="utf-8"))
                rec["n_records"] = len(parsed.get("records", []))
            except ValueError as e:
                ok = False
                rec["errors"].append(f"ANSWER_NOT_JSON: {e}")
        meta["attempts"].append(rec)
        if ok:
            shutil.copyfile(d / f"events.attempt{attempt}.jsonl", d / "events.jsonl")
            shutil.copyfile(d / f"stderr.attempt{attempt}.log", d / "stderr.log")
            meta.update(status="OK", exit_code=code, usage=rec["usage"], finished_at=rec["finished_at"])
            (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            (d / "DONE").write_text(now() + "\n", encoding="utf-8")
            return {"packet_id": pid, "status": "OK", "n_records": rec["n_records"], "usage": rec["usage"],
                    "seconds": rec["seconds"]}
        blob = " ".join(rec["errors"]) + " " + err_text[-4000:]
        if LIMIT_RX.search(blob):
            w = wait_seconds(blob)
            with _lock:
                _pause_until = max(_pause_until, time.time() + w)
            log(a.work, a.run, f"{pid}: usage limit — all workers pause {w // 60} min")
            rec["limit_wait_s"] = w
            continue                                   # a limit wait does not consume the retry budget below
        if attempt < MAX_ATTEMPTS:
            back = 60 * attempt * (2 if TRANSIENT_RX.search(blob) else 1)
            log(a.work, a.run, f"{pid}: attempt {attempt} failed (exit {code}); retry in {back}s")
            time.sleep(back)
    meta.update(status="FAILED")
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"packet_id": pid, "status": "FAILED"}


def status(a) -> int:
    mans = [json.loads(x) for x in open(a.work / "packets.jsonl", encoding="utf-8")]
    root = a.work / "sol" / a.run
    done, pages, recs, tok_in, tok_out, secs = 0, 0, 0, 0, 0, []
    for m in mans:
        d = root / m["packet_id"]
        if (d / "DONE").is_file():
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            done += 1
            pages += m["n_pages"]
            recs += meta["attempts"][-1].get("n_records", 0)
            tok_in += meta.get("usage", {}).get("input_tokens", 0)
            tok_out += meta.get("usage", {}).get("output_tokens", 0)
    print(json.dumps({"run": a.run, "packets_done": done, "packets_total": len(mans), "pages_done": pages,
                      "pages_total": sum(m["n_pages"] for m in mans), "records": recs, "input_tokens": tok_in,
                      "output_tokens": tok_out}, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--run", required=True)
    ap.add_argument("--effort", default="high", choices=("low", "medium", "high", "xhigh"))
    ap.add_argument("--packets", default="")
    ap.add_argument("--tier", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    a.work = a.work.resolve()
    if a.status:
        return status(a)
    codex = find_codex()
    version = subprocess.run([str(codex), "--version"], capture_output=True, text=True).stdout.strip()
    task = (a.work / "task.txt").read_bytes()
    schema_path = a.work / "schema.json"
    mans = [json.loads(x) for x in open(a.work / "packets.jsonl", encoding="utf-8")]
    ids = [m["packet_id"] for m in mans]
    if a.packets:
        want = [x.strip() for x in a.packets.split(",") if x.strip()]
        ids = [x for x in want if x in set(ids)]
    if a.tier:
        tiers = {m["packet_id"]: m["tier"] for m in mans}
        ids = [x for x in ids if tiers[x] in a.tier]
    if a.limit:
        ids = ids[:a.limit]
    (a.work / "sol" / a.run).mkdir(parents=True, exist_ok=True)
    log(a.work, a.run, f"start: {len(ids)} packets, effort {a.effort}, parallel {a.parallel}, {version}")
    res = []
    with ThreadPoolExecutor(max_workers=a.parallel) as ex:
        futs = {ex.submit(run_packet, pid, a, codex, version, task, schema_path): pid for pid in ids}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            res.append(r)
            log(a.work, a.run, f"[{i}/{len(ids)}] {r['packet_id']}: {r['status']} records={r.get('n_records', '-')} "
                               f"in={r.get('usage', {}).get('input_tokens', '-')} "
                               f"out={r.get('usage', {}).get('output_tokens', '-')} s={r.get('seconds', '-')}")
    bad = [r for r in res if r["status"] == "FAILED"]
    log(a.work, a.run, f"end: ok={sum(r['status'] == 'OK' for r in res)} skipped="
                       f"{sum(r['status'] == 'SKIPPED_DONE' for r in res)} failed={len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
