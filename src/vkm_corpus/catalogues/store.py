"""Read-only serving of the published catalogue pack (``derived/catalogues/CURRENT``) for the API.

:class:`CatalogueStore` opens ``<data_root>/derived/catalogues/<pack_id>/catalogues.duckdb`` read only and reopens it
when CURRENT or the file changes (mtime, size, inode), like ``NavStore``. Queries are parametrised; table names are
checked against the pack's ``catalogue_files`` list, never taken from a caller. Small tables are cached as lists of
dicts per opened pack (the topic dossier matches them in Python). Without a published pack every call raises
:class:`CataloguesUnavailable` — the API then builds the dossier without the catalogue part and says so.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

from vkm_corpus.catalogues.pack import DB_FILE, current_pack_id, catalogues_root


class CataloguesUnavailable(RuntimeError):
    """No catalogue pack is published for this data root (or it cannot be opened)."""


class CatalogueStore:
    def __init__(self, data_root: str | Path | None = None, *, db_path: str | Path | None = None) -> None:
        if data_root is None and db_path is None:
            raise ValueError("a data root or a catalogues.duckdb file is required")
        self.data_root = Path(data_root) if data_root is not None else None
        self.db_path = Path(db_path) if db_path is not None else None
        self._lock = threading.Lock()
        self._con: Any = None
        self._stamp: tuple | None = None
        self._meta: dict[str, Any] = {}
        self._pack_id: str | None = None
        self._tables: dict[str, list[str]] = {}
        self._cache: dict[tuple, list[dict[str, Any]]] = {}

    # -------------------------------------------------------------- instance
    def _paths(self) -> tuple[Path, str | None]:
        if self.db_path is not None:
            return self.db_path, None
        pack_id = current_pack_id(self.data_root)           # type: ignore[arg-type]
        if not pack_id:
            raise CataloguesUnavailable("the catalogues are not published (derived/catalogues/CURRENT is missing)")
        db = catalogues_root(self.data_root) / pack_id / DB_FILE  # type: ignore[arg-type]
        if not db.is_file():
            raise CataloguesUnavailable(f"catalogue pack {pack_id} has no {DB_FILE}")
        return db, pack_id

    def _instance(self) -> Any:
        db, pack_id = self._paths()
        try:
            st = db.stat()
        except OSError as exc:
            raise CataloguesUnavailable("the catalogue pack file is missing") from exc
        stamp = (pack_id, st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))
        if self._con is not None and stamp == self._stamp:
            return self._con
        import duckdb

        old, self._con = self._con, None
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            con = duckdb.connect(str(db), read_only=True)
            meta = con.execute("SELECT meta_json FROM catalogue_meta").fetchone()
            files = con.execute("SELECT table_name FROM catalogue_files ORDER BY path").fetchall()
            cols = con.execute("SELECT table_name, column_name FROM duckdb_columns() WHERE schema_name = 'main' "
                               "ORDER BY table_name, column_index").fetchall()
        except Exception as exc:  # noqa: BLE001 - any open failure means no usable pack
            raise CataloguesUnavailable(f"cannot open the catalogue pack ({type(exc).__name__})") from exc
        by_table: dict[str, list[str]] = {}
        for table, column in cols:
            by_table.setdefault(table, []).append(column)
        self._meta = json.loads(meta[0]) if meta else {}
        self._tables = {t: by_table.get(t, []) for (t,) in files}
        self._con, self._stamp, self._cache = con, stamp, {}
        self._pack_id = pack_id or self._meta.get("pack_id")
        return con

    # -------------------------------------------------------------- API
    def pack_id(self) -> str | None:
        with self._lock:
            self._instance()
            return self._pack_id

    def meta(self) -> dict[str, Any]:
        """The pack manifest without the per-file list (format, pack id, git commit, content hash, counts)."""
        with self._lock:
            self._instance()
            return {k: v for k, v in self._meta.items() if k != "files"}

    def tables(self) -> dict[str, list[str]]:
        with self._lock:
            self._instance()
            return {t: list(c) for t, c in self._tables.items()}

    def query(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._instance().cursor()
        try:
            cur.execute(sql, list(params))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def rows(self, table: str, columns: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """All rows of a catalogue table (only the known columns asked for), cached per opened pack; [] when the
        pack has no such table."""
        with self._lock:
            self._instance()
            known = self._tables.get(table)
            if known is None:
                return []
            wanted = [c for c in (columns or known) if c in known]
            key = (table, tuple(wanted))
            hit = self._cache.get(key)
            if hit is not None:
                return hit
            cur = self._con.cursor()
        try:
            select = ", ".join('"' + c.replace('"', '""') + '"' for c in wanted) or "*"
            cur.execute(f'SELECT {select} FROM "{table}" ORDER BY "_csv_row"' if "_csv_row" in known else
                        f'SELECT {select} FROM "{table}"')
            names = [d[0] for d in cur.description]
            out = [dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            cur.close()
        with self._lock:
            self._cache[key] = out
        return out
