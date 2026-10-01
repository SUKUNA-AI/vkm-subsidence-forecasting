#!/usr/bin/env python3
"""Store the nightly topic dossiers (agent OPS, 29.09.2026; user decision B1.5): a cache for instant answers and a
regression signal. Standard library only; runs with the host ``python3`` of CORE.

Reads the JSON lines of ``dossiers.py`` (run inside the API container) and writes
``$VKM_DATA_ROOT_HOST/derived/dossiers/<snapshot>/``:

* ``<topic_id>.json`` — the full API answer of ``reconstruct_topic`` (ApiResponse: envelope, record with sections,
  formulas, catalogue processes, gaps, markdown) — navigation, AUTO_EXTRACTED_UNREVIEWED, never evidence;
* ``index.json`` — per topic: counts and ids of sections (core tier / rest), catalogue processes and models, formulas,
  sources, pages, gaps, whether the topic's own process PC-xx (or model family MM-*) is among them, warnings, time.

The directory is swapped in atomically, ``derived/dossiers/CURRENT`` names it, CURRENT plus the newest ``--keep - 1``
canonical snapshot identities stay (not ordered by mtime). Unknown directory names are preserved. If no dossier
succeeded, the previous cache is left untouched. The index is compared with the
previous nightly run (``--prev-index``): topics whose sections or processes changed, big changes, self-hit drop.

    python3 dossier_store.py --in raw/dossiers.jsonl --dossiers-dir <data root>/derived/dossiers \\
        --snapshot <CURRENT> --out-index <run>/dossiers_index.json [--prev-index <prev run>/dossiers_index.json]
        [--keep 5] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "vkm.dossier_index/1"
SNAPSHOT_RE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
SNAPSHOT_ID = re.compile(r"snap-(\d{8}T\d{6}Z)-([0-9a-f]+)\Z")


def retention_plan(names: Iterable[str], current: str, keep: int) -> dict[str, Any]:
    """Keep CURRENT plus the newest canonical snapshot identities, independently of filesystem timestamps.

    Unknown names cannot be ordered safely and are preserved for inspection. The current snapshot counts toward
    the limit even when it is older than another snapshot (for example after rollback).
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be a positive integer")
    known, unknown = [], []
    for name in set(names) | {current}:
        match = SNAPSHOT_ID.fullmatch(name)
        try:
            if match is None:
                raise ValueError("not a canonical snapshot identity")
            stamp = dt.datetime.strptime(match[1], "%Y%m%dT%H%M%SZ")
        except ValueError:
            unknown.append(name)
        else:
            known.append((stamp, name))
    ordered = [name for _, name in sorted(known, reverse=True) if name != current]
    selected = [current, *ordered[:keep - 1]]
    return {"basis": "canonical_snapshot_identity", "keep": selected, "delete": sorted(ordered[keep - 1:]),
            "unclassified_preserved": sorted(n for n in unknown if n != current)}


def _safe_component(value: str, label: str) -> None:
    if (not isinstance(value, str) or not value or value in (".", "..")
            or value.startswith(".") or set(value) - SNAPSHOT_RE_CHARS):
        raise ValueError(f"bad {label}")


def read_lines(path: Path) -> list[dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _ids(items: Iterable[dict[str, Any]], key: str) -> list[str]:
    seen: list[str] = []
    for it in items or []:
        v = it.get(key) if isinstance(it, dict) else None
        if isinstance(v, str) and v not in seen:
            seen.append(v)
    return seen


def self_hit(topic_id: str, track: str | None, process_ids: list[str], model_ids: list[str]) -> bool | None:
    """Does the dossier name the topic's own catalogue entry: PC-xx among its processes, a model of the family MM-X
    (MM-X-001 …) among its models."""
    if track == "PROCESS":
        return topic_id in process_ids
    if track == "MODEL_FAMILY":
        return any(m == topic_id or m.startswith(topic_id + "-") for m in model_ids)
    return None


def index_entry(line: dict[str, Any], file_sha: str | None = None) -> dict[str, Any]:
    base = {k: line.get(k) for k in ("topic_id", "track", "group", "title", "ok", "error_code", "http", "ms")}
    body = line.get("response") or {}
    rec = ((body.get("item") or {}).get("record") or {}) if line.get("ok") else {}
    if not rec:
        return {**base, "ok": False}
    sections = rec.get("sections") or []
    cat = rec.get("catalogue") or {}
    process_ids = _ids(cat.get("processes"), "process_id")
    model_ids = _ids(cat.get("models"), "model_id")
    pages = rec.get("pages") or {}
    warnings = sorted({w.get("code") for w in (body.get("meta") or {}).get("warnings") or [] if w.get("code")})
    return {**base, "mode": rec.get("mode"), "rule_version": rec.get("rule_version"),
            "sections": len(sections), "sections_core": sum(1 for s in sections if s.get("tier") == "CORE"),
            "section_ids": _ids(sections, "section_id"), "processes": len(process_ids), "process_ids": process_ids,
            "models": len(model_ids), "model_ids": model_ids, "formulas": len(rec.get("formulas") or []),
            "visual": len(rec.get("visual") or []), "sources": len(rec.get("sources") or []),
            "pages_core": len(pages.get("core") or []), "pages_rest": len(pages.get("rest") or []),
            "gaps": len(rec.get("gaps") or []), "self_hit": self_hit(line["topic_id"], line.get("track"),
                                                                     process_ids, model_ids),
            "warnings": warnings, "markdown_chars": len(rec.get("markdown") or ""),
            "inputs": {k: (rec.get("inputs") or {}).get(k) for k in ("canonical_snapshot_id", "nav_snapshot_id")},
            "file": f"{line['topic_id']}.json", "sha256": file_sha}


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [e for e in entries if e.get("ok")]

    def share(track: str) -> float | None:
        xs = [e["self_hit"] for e in ok if e.get("track") == track and e.get("self_hit") is not None]
        return round(sum(1 for x in xs if x) / len(xs), 4) if xs else None

    ms = sorted(e["ms"] for e in ok if isinstance(e.get("ms"), (int, float)))
    return {"topics": len(entries), "ok": len(ok), "errors": len(entries) - len(ok),
            "sections": sum(e.get("sections", 0) for e in ok), "processes": sum(e.get("processes", 0) for e in ok),
            "formulas": sum(e.get("formulas", 0) for e in ok), "gaps": sum(e.get("gaps", 0) for e in ok),
            "empty_sections": sorted(e["topic_id"] for e in ok if not e.get("sections")),
            "process_self_hit": share("PROCESS"), "model_self_hit": share("MODEL_FAMILY"),
            "p50_ms": ms[(len(ms) - 1) // 2] if ms else None, "max_ms": ms[-1] if ms else None,
            "error_codes": sorted({e.get("error_code") for e in entries if not e.get("ok") and e.get("error_code")})}


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    a, b = set(a), set(b)
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def diff(now: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, Any] | None:
    """Per topic against the previous nightly index: counts and ids of sections and processes."""
    if not prev or not prev.get("topics"):
        return None
    before = {e["topic_id"]: e for e in prev["topics"] if e.get("ok")}
    changed, big, lost = [], [], []
    for e in now["topics"]:
        p = before.get(e["topic_id"])
        if not e.get("ok"):
            if p:
                lost.append(e["topic_id"])
            continue
        if not p:
            continue
        same = (e.get("sections") == p.get("sections") and e.get("processes") == p.get("processes")
                and set(e.get("section_ids") or []) == set(p.get("section_ids") or [])
                and set(e.get("process_ids") or []) == set(p.get("process_ids") or []))
        if same:
            continue
        row = {"topic_id": e["topic_id"], "sections": [p.get("sections"), e.get("sections")],
               "processes": [p.get("processes"), e.get("processes")],
               "section_jaccard": round(jaccard(e.get("section_ids") or [], p.get("section_ids") or []), 3),
               "process_jaccard": round(jaccard(e.get("process_ids") or [], p.get("process_ids") or []), 3)}
        changed.append(row)
        ds = abs((e.get("sections") or 0) - (p.get("sections") or 0))
        if ds >= max(3, 0.5 * (p.get("sections") or 0)) or row["process_jaccard"] < 0.5:
            big.append(e["topic_id"])
    s_now, s_prev = now.get("summary") or {}, prev.get("summary") or {}
    drop = None
    if s_now.get("process_self_hit") is not None and s_prev.get("process_self_hit") is not None:
        drop = round(s_prev["process_self_hit"] - s_now["process_self_hit"], 4)
    return {"prev_snapshot": prev.get("snapshot"), "prev_created_at": prev.get("created_at"),
            "changed": len(changed), "big_changes": len(big), "lost": lost, "self_hit_drop": drop,
            "examples": changed[:10], "big": big[:20]}


def _write_json(path: Path, obj: Any) -> str:
    data = (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return hashlib.sha256(data).hexdigest()


def store(lines: list[dict[str, Any]], dossiers_dir: Path, snapshot: str, *, run_tag: str, keep: int = 5,
          dry_run: bool = False) -> tuple[dict[str, Any], bool]:
    """Write the snapshot directory (unless dry_run or nothing succeeded); returns (index, stored)."""
    _safe_component(snapshot, "snapshot id")
    _safe_component(run_tag, "run tag")
    if dossiers_dir.is_symlink():
        raise ValueError("dossier root must not be a symlink")
    meta = next((x for x in lines if x.get("kind") == "meta"), {})
    end = next((x for x in lines if x.get("kind") == "end"), {})
    dossiers = [x for x in lines if x.get("kind") == "dossier"]
    seen_topics: set[str] = set()
    for line in dossiers:
        _safe_component(line.get("topic_id"), "topic id")
        if line["topic_id"].casefold() in seen_topics or line["topic_id"].lower() == "index":
            raise ValueError("duplicate or reserved topic id")
        seen_topics.add(line["topic_id"].casefold())
    dirs = list(dossiers_dir.iterdir()) if dossiers_dir.is_dir() else []
    plan = retention_plan((p.name for p in dirs if p.is_dir() and not p.is_symlink()
                           and not p.name.startswith(".")), snapshot, keep)
    entries: list[dict[str, Any]] = []
    stored = False
    tmp = dossiers_dir / f".tmp-{run_tag}"
    any_ok = any(x.get("ok") for x in dossiers)
    if any_ok and not dry_run:
        tmp.mkdir(parents=True, exist_ok=False)
    for x in dossiers:
        sha = None
        if x.get("ok") and not dry_run:
            sha = _write_json(tmp / f"{x['topic_id']}.json", x.get("response"))
        entries.append(index_entry(x, sha))
    index = {"schema": SCHEMA, "snapshot": snapshot, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "run": run_tag, "generator": {"version": meta.get("version"), "budget": meta.get("budget")},
             "truncated": bool(end.get("truncated")), "summary": summarize(entries), "topics": entries,
             "retention": plan}
    if any_ok and not dry_run:
        _write_json(tmp / "index.json", index)
        final = dossiers_dir / snapshot
        old = dossiers_dir / f".old-{run_tag}"
        if final.is_symlink() or old.exists() or old.is_symlink():
            raise ValueError("dossier publication target is unsafe or staging is occupied")
        if final.exists():
            os.replace(final, old)
        os.replace(tmp, final)
        shutil.rmtree(old, ignore_errors=True)
        (dossiers_dir / "CURRENT.tmp").write_text(snapshot + "\n", encoding="utf-8")
        os.replace(dossiers_dir / "CURRENT.tmp", dossiers_dir / "CURRENT")
        stored = True
        for name in plan["delete"]:
            p = dossiers_dir / name
            # Recheck containment/symlink after publication; a substituted path must never be traversed by cleanup.
            if p.is_dir() and not p.is_symlink() and p.resolve().parent == dossiers_dir.resolve():
                shutil.rmtree(p, ignore_errors=True)
        # Other .tmp/.old directories may belong to interrupted or active writers. This run never removes them.
    return index, stored


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dossier_store.py", description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="inp", required=True, help="JSON lines of dossiers.py")
    ap.add_argument("--dossiers-dir", required=True, help="<data root>/derived/dossiers")
    ap.add_argument("--snapshot", required=True, help="canonical CURRENT of the run")
    ap.add_argument("--out-index", help="copy of the index for the nightly run directory (with the diff)")
    ap.add_argument("--prev-index", help="index of the previous nightly run")
    ap.add_argument("--keep", type=int, default=5, help="snapshot directories to keep")
    ap.add_argument("--run-tag", default=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    lines = read_lines(Path(args.inp))
    index, stored = store(lines, Path(args.dossiers_dir), args.snapshot, run_tag=args.run_tag, keep=args.keep,
                          dry_run=args.dry_run)
    prev = None
    if args.prev_index and Path(args.prev_index).is_file():
        try:
            prev = json.loads(Path(args.prev_index).read_text(encoding="utf-8"))
        except ValueError:
            prev = None
    index["diff"] = diff(index, prev)
    index["stored"] = stored
    if args.out_index and not args.dry_run:
        Path(args.out_index).parent.mkdir(parents=True, exist_ok=True)
        _write_json(Path(args.out_index), index)
    out = {"stored": stored, "snapshot": args.snapshot, "truncated": index["truncated"], **index["summary"],
           "diff": index["diff"] and {k: v for k, v in index["diff"].items() if k != "examples"}}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    return 0 if index["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
