"""Serving store of the navigation layer (NAV) for the API and MCP.

Layout under the data root::

    derived/navigation/<snapshot_id>/<dataset>.parquet   outputs of ``vkm-corpus nav build`` (+ manifest.json)
    derived/navigation/<snapshot_id>/nav.duckdb          one table per dataset + ``nav_meta`` (``pack``)
    derived/navigation/CURRENT                            the snapshot id whose nav.duckdb is served (``publish``)

:class:`NavStore` answers queries from an in-memory DuckDB instance that ATTACHes the canonical DuckDB file and the
current ``nav.duckdb`` read-only and exposes the canonical tables as ``canonical.<name>`` and the NAV datasets under
their own names — the query functions of the navigation modules run unchanged against it (a cursor is passed as
``con``). A change of either file (mtime, size, inode) or of CURRENT rebuilds the instance on the next query. The
layer is DERIVED navigation (``AUTO_EXTRACTED_UNREVIEWED``), never evidence.
"""
from __future__ import annotations

import importlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

NAV_SUBDIR = Path("derived") / "navigation"
CURRENT_FILE = "CURRENT"
NAV_DB = "nav.duckdb"
CANONICAL_DB = Path("duckdb") / "vkm_corpus.duckdb"

# query functions of the navigation modules (written by the part owners against the dataset names)
QUERY_FUNCTIONS: dict[str, str] = {
    "outline": "vkm_corpus.navigation.sections_query:get_outline",
    "section": "vkm_corpus.navigation.sections_query:get_section",
    "section_of_page": "vkm_corpus.navigation.sections_query:section_of_page",
    "formula_context": "vkm_corpus.navigation.formulas_query:get_formula_context",
    "find_formulas": "vkm_corpus.navigation.formulas_query:find_formulas",
    "explore_concept": "vkm_corpus.navigation.concepts_query:explore_concept",
    "copies_of": "vkm_corpus.navigation.duplicates_query:copies_of",
    "source_overlap": "vkm_corpus.navigation.duplicates_query:source_overlap",
}


class NavUnavailable(RuntimeError):
    """The navigation layer is not built/published for this data root (or a part of it is missing)."""


def nav_root(data_root: str | Path) -> Path:
    return Path(data_root) / NAV_SUBDIR


def current_snapshot(data_root: str | Path) -> str | None:
    try:
        value = (nav_root(data_root) / CURRENT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def pack(nav_dir: str | Path) -> dict[str, Any]:
    """``<nav_dir>/*.parquet`` → ``<nav_dir>/nav.duckdb`` (tables named by dataset + ``nav_meta``); atomic replace."""
    import duckdb

    nav_dir = Path(nav_dir)
    parts = sorted(p for p in nav_dir.glob("*.parquet"))
    if not parts:
        raise NavUnavailable(f"no NAV datasets in {nav_dir.name}")
    manifest: dict[str, Any] = {}
    if (nav_dir / "manifest.json").exists():
        manifest = json.loads((nav_dir / "manifest.json").read_text(encoding="utf-8"))
    tmp = nav_dir / (NAV_DB + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    counts: dict[str, int] = {}
    try:
        for p in parts:
            name = p.stem
            if not name.replace("_", "").isalnum():
                raise ValueError(f"bad dataset name {name!r}")
            con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM read_parquet(?)', [str(p)])
            counts[name] = int(con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
        meta = {"snapshot_id": manifest.get("snapshot_id"), "rule_versions": manifest.get("rule_versions"),
                "built_at": manifest.get("built_at"), "packed_at": datetime.now(timezone.utc).isoformat(),
                "counts": counts}
        con.execute("CREATE TABLE nav_meta AS SELECT ?::VARCHAR AS meta_json", [json.dumps(meta, ensure_ascii=False)])
        con.execute("CHECKPOINT")
    finally:
        con.close()
    os.replace(tmp, nav_dir / NAV_DB)
    return {"nav_db": NAV_DB, "tables": counts, "snapshot_id": manifest.get("snapshot_id")}


def publish(data_root: str | Path, snapshot_id: str) -> Path:
    """Point CURRENT at ``derived/navigation/<snapshot_id>`` (which must hold a packed nav.duckdb)."""
    root = nav_root(data_root)
    if not (root / snapshot_id / NAV_DB).exists():
        raise NavUnavailable(f"{snapshot_id}/{NAV_DB} is not packed")
    tmp = root / (CURRENT_FILE + ".tmp")
    tmp.write_text(snapshot_id + "\n", encoding="utf-8")
    os.replace(tmp, root / CURRENT_FILE)
    return root / CURRENT_FILE


def resolve(name: str) -> Callable[..., Any]:
    module, _, attr = QUERY_FUNCTIONS[name].partition(":")
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as exc:
        raise NavUnavailable(f"navigation query {name!r} is not available in this build") from exc


class NavStore:
    """Read-only serving of NAV + canonical tables for query functions (thread-safe instance swap)."""

    def __init__(self, data_root: str | Path, *, canonical_db: str | Path | None = None,
                 functions: dict[str, Callable[..., Any]] | None = None) -> None:
        self.data_root = Path(data_root)
        self.canonical_db = Path(canonical_db) if canonical_db else self.data_root / CANONICAL_DB
        self._functions = dict(functions or {})
        self._lock = threading.Lock()
        self._con: Any = None
        self._stamp: tuple | None = None
        self._snapshot: str | None = None

    # -------------------------------------------------------------- instance
    def _paths(self) -> tuple[Path, Path, str]:
        snap = current_snapshot(self.data_root)
        if not snap:
            raise NavUnavailable("the navigation layer is not published (derived/navigation/CURRENT is missing)")
        nav_db = nav_root(self.data_root) / snap / NAV_DB
        if not nav_db.exists():
            raise NavUnavailable(f"navigation layer {snap} is not packed")
        if not self.canonical_db.exists():
            raise NavUnavailable("the canonical DuckDB file is missing")
        return self.canonical_db, nav_db, snap

    @staticmethod
    def _file_stamp(p: Path) -> tuple:
        st = p.stat()
        return st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0)

    def _instance(self) -> Any:
        canon_db, nav_db, snap = self._paths()
        stamp = (snap, self._file_stamp(canon_db), self._file_stamp(nav_db))
        if self._con is not None and stamp == self._stamp:
            return self._con
        import duckdb

        old, self._con = self._con, None
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        con = duckdb.connect()
        con.execute(f"ATTACH '{_sql_path(canon_db)}' AS canon (READ_ONLY)")
        con.execute(f"ATTACH '{_sql_path(nav_db)}' AS nav (READ_ONLY)")
        con.execute("CREATE SCHEMA IF NOT EXISTS canonical")
        for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'canon' "
                                "AND schema_name = 'canonical'").fetchall():
            con.execute(f'CREATE VIEW canonical."{t}" AS SELECT * FROM canon.canonical."{t}"')
        for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'nav' "
                                "AND schema_name = 'main'").fetchall():
            con.execute(f'CREATE VIEW "{t}" AS SELECT * FROM nav.main."{t}"')
            if not t.startswith("nav_"):     # the part modules query nav_<dataset> (as `vkm-corpus nav build` names them)
                con.execute(f'CREATE VIEW "nav_{t}" AS SELECT * FROM nav.main."{t}"')
        self._con, self._stamp, self._snapshot = con, stamp, snap
        return con

    # -------------------------------------------------------------- API
    def snapshot_id(self) -> str | None:
        with self._lock:
            self._instance()
            return self._snapshot

    def meta(self) -> dict[str, Any]:
        rows = self.query("SELECT meta_json FROM nav_meta")
        return json.loads(rows[0]["meta_json"]) if rows else {}

    def query(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._instance().cursor()
        try:
            cur.execute(sql, list(params))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def run(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Run a navigation query function (``QUERY_FUNCTIONS``) with a cursor of the serving instance."""
        fn = self._functions.get(name) or resolve(name)
        with self._lock:
            cur = self._instance().cursor()
        try:
            return fn(cur, *args, **kwargs)
        finally:
            cur.close()

    def search_sections(self, text: str, *, source_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Sections whose title path (and key terms, when present) contain the query words; more words first."""
        words = [w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in text).split() if len(w) > 2][:8]
        if not words:
            return []
        cols = {r["column_name"] for r in self.query("SELECT column_name FROM duckdb_columns() "
                                                     "WHERE table_name = 'sections'")}
        parts = [f"coalesce({c}, '')" for c in ("title_path", "title") if c in cols]
        if "key_terms" in cols:
            parts.append("coalesce(array_to_string(key_terms, ' '), '')")
        hay = "lower(" + " || ' ' || ".join(parts or ["''"]) + ")"
        score = " + ".join(f"CASE WHEN strpos({hay}, ?) > 0 THEN 1 ELSE 0 END" for _ in words)
        where = "WHERE source_id = ?" if source_id else ""
        params: list[Any] = [*words, *([source_id] if source_id else [])]
        shown = ", ".join(c for c in ("section_id", "source_id", "level", "title", "title_path", "page_start_id",
                                      "page_end_id", "method") if c in cols)
        rows = self.query(f"SELECT {shown}, ({score}) AS score FROM sections {where} "
                          f"ORDER BY score DESC, level, section_id LIMIT {int(limit) * 3}", params)
        return [r for r in rows if r["score"]][:limit]


def _sql_path(p: Path) -> str:
    s = str(p)
    if "'" in s:
        raise ValueError("path with a quote")
    return s
