"""Pack the PUBLIC catalogues into one DuckDB file and publish it under the data root (no models, no network).

Input: every ``*.csv`` under ``catalogues/`` and ``evidence/`` of a PUBLIC checkout (sorted by path). Output::

    <out>/catalogues.duckdb   a table per CSV (all columns VARCHAR: cells verbatim, an empty cell is NULL, no type
                              inference — «0,45» stays «0,45») + ``_csv_row`` (data row number, from 1),
                              ``catalogue_files`` (path, table, rows, sha256) and ``catalogue_meta`` (manifest JSON)
    <out>/manifest.json       format, pack id, git commit, sha256/rows/columns per file, content hash, DB sha256

    <data_root>/derived/catalogues/<pack_id>/{catalogues.duckdb, manifest.json}     (``publish``)
    <data_root>/derived/catalogues/CURRENT                                            the pack the API serves

``pack_id`` is the first 12 hex digits of the PUBLIC commit whose files were packed; a working tree whose catalogue
files differ from that commit is refused unless ``allow_dirty`` (then ``<commit12>-dirty-<content8>``). The table name
is the file stem in lower case (``MATHEMATICAL_MODEL_REGISTRY.csv`` → ``mathematical_model_registry``); stems that
occur twice get their directory path as the name. Files and CURRENT are replaced atomically (tmp + ``os.replace``),
like ``navigation/store.py``. The catalogue records keep their own status (FACT … UNKNOWN), scope and scale; the pack is
a derived serving copy, the repository files stay the source of truth.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

FORMAT = "vkm-catalogues-pack-v1"
DEFAULT_ROOTS: tuple[str, ...] = ("catalogues", "evidence")
DB_FILE = "catalogues.duckdb"
MANIFEST_FILE = "manifest.json"
SUBDIR = Path("derived") / "catalogues"
CURRENT_FILE = "CURRENT"
ROW_COLUMN = "_csv_row"
META_TABLES = frozenset({"catalogue_files", "catalogue_meta"})
PACK_ID = re.compile(r"^[0-9a-f]{12}(?:-dirty-[0-9a-f]{8})?$")
NOTE = ("PUBLIC-safe evidence catalogues (no quotes), packed verbatim for serving: every record keeps its own status "
        "(FACT, DERIVATION, INTERPOLATION, MODEL_CHOICE, ENGINEERING_ASSUMPTION, ANALOGUE, UNKNOWN), scope and scale")


class PackError(RuntimeError):
    """The catalogues cannot be packed or published as asked."""


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_files(repo: str | Path, roots: Iterable[str] = DEFAULT_ROOTS) -> list[str]:
    """Repository-relative POSIX paths of the CSV files under ``roots`` (sorted)."""
    repo = Path(repo)
    out: list[str] = []
    for root in roots:
        base = repo / root
        if base.is_dir():
            out += [p.relative_to(repo).as_posix() for p in base.rglob("*.csv") if p.is_file()]
    return sorted(out)


def _ident(text: str) -> str:
    s = re.sub(r"[^0-9a-z]+", "_", text.lower()).strip("_") or "table"
    return f"t_{s}" if s[0].isdigit() else s


def table_names(rels: Iterable[str]) -> dict[str, str]:
    """Table name of every file: the lower-case stem, or the directory path when a stem occurs twice."""
    rels = sorted(rels)
    by_stem: dict[str, list[str]] = {}
    for rel in rels:
        by_stem.setdefault(_ident(Path(rel).stem), []).append(rel)
    out: dict[str, str] = {}
    for stem, group in by_stem.items():
        for rel in group:
            unique = len(group) == 1 and stem not in META_TABLES
            out[rel] = stem if unique else _ident(Path(rel).with_suffix("").as_posix())
    seen: dict[str, int] = {}
    for rel in rels:                                   # paths that normalise alike (``a-b`` / ``a_b``): numbered
        name = out[rel]
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            out[rel] = f"{name}_{seen[name]}"
    return out


def read_csv(path: str | Path) -> tuple[list[str], list[list[str | None]]]:
    """Header (made unique, case-insensitively) and rows of a UTF-8 CSV; empty cells → None, short rows padded."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        raw = next(reader, None)
        if raw is None:
            return [], []
        header: list[str] = []
        used: set[str] = set()
        for i, name in enumerate(raw, 1):
            name = name.strip() or f"column_{i}"
            base, k = name, 1
            while name.lower() in used or name.lower() == ROW_COLUMN:
                k += 1
                name = f"{base}_{k}"
            used.add(name.lower())
            header.append(name)
        rows: list[list[str | None]] = []
        for row in reader:
            if not row:                                  # a blank line, as csv.DictReader skips it
                continue
            if len(row) > len(header):
                if any(cell.strip() for cell in row[len(header):]):
                    raise PackError(f"{Path(path).name}: data row {len(rows) + 1} has {len(row)} fields, the header "
                                    f"{len(header)}")
                row = row[:len(header)]
            cells: list[str | None] = [cell if cell != "" else None for cell in row]
            rows.append(cells + [None] * (len(header) - len(cells)))
    return header, rows


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _load_table(con: Any, name: str, header: list[str], rows: list[list[str | None]]) -> None:
    columns = [*header, ROW_COLUMN]
    try:
        import pyarrow as pa
    except ImportError:                                  # the executemany path: slower, same table
        pa = None
    if pa is not None:
        arrays = [pa.array([r[i] for r in rows], type=pa.string()) for i in range(len(header))]
        arrays.append(pa.array(list(range(1, len(rows) + 1)), type=pa.int32()))
        con.register("_vkm_catalogue_rows", pa.Table.from_arrays(arrays, names=columns))
        try:
            con.execute(f"CREATE TABLE {_quote(name)} AS SELECT * FROM _vkm_catalogue_rows")
        finally:
            con.unregister("_vkm_catalogue_rows")
        return
    ddl = ", ".join(f"{_quote(c)} VARCHAR" for c in header) + f", {_quote(ROW_COLUMN)} INTEGER"
    con.execute(f"CREATE TABLE {_quote(name)} ({ddl})")
    if rows:
        marks = ", ".join("?" for _ in columns)
        con.executemany(f"INSERT INTO {_quote(name)} VALUES ({marks})", [[*r, i] for i, r in enumerate(rows, 1)])


def git_state(repo: str | Path, roots: Iterable[str] = DEFAULT_ROOTS) -> dict[str, Any]:
    """``{"commit": HEAD or None, "dirty_paths": [...] or None}`` (None when git cannot read the checkout)."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                              encoding="utf-8").stdout

    try:
        commit = git("rev-parse", "HEAD").strip().lower()
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty_paths": None}
    try:
        status = git("status", "--porcelain", "--untracked-files=all", "--", *roots)
        dirty: list[str] | None = sorted({line[3:].strip().strip('"') for line in status.splitlines() if line.strip()})
    except (OSError, subprocess.CalledProcessError):
        dirty = None
    return {"commit": commit or None, "dirty_paths": dirty}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def pack(repo: str | Path, out: str | Path, *, commit: str | None = None, allow_dirty: bool = False,
         roots: Iterable[str] = DEFAULT_ROOTS) -> dict[str, Any]:
    """CSV catalogues of ``repo`` → ``<out>/catalogues.duckdb`` + ``<out>/manifest.json``; returns the manifest."""
    import duckdb

    repo, out, roots = Path(repo), Path(out), tuple(roots)
    if _inside(out, repo) and not _inside(out, repo / "work"):
        raise PackError("the pack is runtime data: write it outside the repository (or under its git-ignored work/)")
    rels = find_files(repo, roots)
    if not rels:
        raise PackError(f"no CSV files under {', '.join(roots)} of the given checkout")
    state = git_state(repo, roots)
    given = (commit or "").strip().lower() or None
    if given is not None and not re.fullmatch(r"[0-9a-f]{12,40}", given):
        raise PackError("--commit must be 12…40 hex digits")
    if given and state["commit"] and not state["commit"].startswith(given) and not given.startswith(state["commit"]):
        raise PackError(f"the files are those of HEAD {state['commit'][:12]}, not of {given[:12]}")
    head = given or state["commit"]
    if not head:
        raise PackError("the PUBLIC commit is unknown: run inside the git checkout or pass --commit")
    dirty_paths = state["dirty_paths"] if state["commit"] else None
    names = table_names(rels)
    files: list[dict[str, Any]] = []
    loaded: list[tuple[str, list[str], list[list[str | None]]]] = []
    for rel in rels:
        header, rows = read_csv(repo / rel)
        files.append({"path": rel, "table": names[rel], "rows": len(rows), "columns": header,
                      "sha256": sha256_file(repo / rel)})
        loaded.append((names[rel], header, rows))
    content = hashlib.sha256("".join(f"{f['path']}\t{f['sha256']}\n" for f in files).encode("utf-8")).hexdigest()
    dirty = bool(dirty_paths) if dirty_paths is not None else None
    if dirty and not allow_dirty:
        raise PackError(f"{len(dirty_paths or [])} catalogue file(s) differ from commit {head[:12]}: commit them or "
                        "pass --allow-dirty")
    pack_id = head[:12] if not dirty else f"{head[:12]}-dirty-{content[:8]}"
    meta: dict[str, Any] = {
        "format": FORMAT, "pack_id": pack_id, "git_commit": head, "commit12": head[:12],
        "commit_source": "argument" if given else "git",
        "working_tree": ("clean" if dirty is False else "dirty" if dirty else "not verified (git unavailable)"),
        "dirty_paths": dirty_paths if dirty else [], "content_sha256": content, "roots": list(roots),
        "packed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "n_files": len(files),
        "n_rows": sum(f["rows"] for f in files), "files": files, "note": NOTE}
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / (DB_FILE + ".tmp")
    for stale in (tmp, out / (DB_FILE + ".tmp.wal")):
        if stale.exists():
            stale.unlink()
    con = duckdb.connect(str(tmp))
    try:
        for name, header, rows in loaded:
            _load_table(con, name, header, rows)
        con.execute("CREATE TABLE catalogue_files (path VARCHAR, table_name VARCHAR, n_rows INTEGER, "
                    "n_columns INTEGER, sha256 VARCHAR)")
        con.executemany("INSERT INTO catalogue_files VALUES (?, ?, ?, ?, ?)",
                        [[f["path"], f["table"], f["rows"], len(f["columns"]), f["sha256"]] for f in files])
        con.execute("CREATE TABLE catalogue_meta AS SELECT ?::VARCHAR AS meta_json",
                    [json.dumps(meta, ensure_ascii=False, sort_keys=True)])
        con.execute("CHECKPOINT")
    finally:
        con.close()
    os.replace(tmp, out / DB_FILE)
    manifest = {**meta, "db": {"file": DB_FILE, "sha256": sha256_file(out / DB_FILE),
                               "bytes": (out / DB_FILE).stat().st_size}}
    _write_json(out / MANIFEST_FILE, manifest)
    return manifest


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes((json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    os.replace(tmp, path)


def catalogues_root(data_root: str | Path) -> Path:
    return Path(data_root) / SUBDIR


def current_pack_id(data_root: str | Path) -> str | None:
    try:
        value = (catalogues_root(data_root) / CURRENT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if PACK_ID.fullmatch(value) else None


def publish(data_root: str | Path, pack_dir: str | Path) -> dict[str, Any]:
    """Copy a pack to ``derived/catalogues/<pack_id>/`` (verified against its manifest) and point CURRENT at it."""
    data_root, pack_dir = Path(data_root), Path(pack_dir)
    try:
        manifest = json.loads((pack_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackError(f"no readable {MANIFEST_FILE} in the pack directory") from exc
    pack_id = str(manifest.get("pack_id") or "")
    if manifest.get("format") != FORMAT or not PACK_ID.fullmatch(pack_id):
        raise PackError("not a catalogues pack (format / pack_id)")
    want = (manifest.get("db") or {}).get("sha256")
    if not (pack_dir / DB_FILE).is_file() or sha256_file(pack_dir / DB_FILE) != want:
        raise PackError(f"{DB_FILE} does not match its manifest")
    root = catalogues_root(data_root)
    target = root / pack_id
    target.mkdir(parents=True, exist_ok=True)
    if target.resolve() != pack_dir.resolve():
        for name in (DB_FILE, MANIFEST_FILE):
            dst = target / name
            if dst.is_file() and sha256_file(dst) == sha256_file(pack_dir / name):
                continue
            tmp = target / (name + ".tmp")
            shutil.copyfile(pack_dir / name, tmp)
            os.replace(tmp, dst)
    if sha256_file(target / DB_FILE) != want:
        raise PackError("the published copy does not match the manifest")
    current_tmp = root / (CURRENT_FILE + ".tmp")
    current_tmp.write_bytes((pack_id + "\n").encode("utf-8"))
    os.replace(current_tmp, root / CURRENT_FILE)
    return {"pack_id": pack_id, "git_commit": manifest.get("git_commit"), "n_files": manifest.get("n_files"),
            "n_rows": manifest.get("n_rows"), "db_sha256": want}
