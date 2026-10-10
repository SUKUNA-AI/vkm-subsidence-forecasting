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
import math
import os
import re
import threading
from datetime import datetime, timezone
from functools import lru_cache
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
    "topic": "vkm_corpus.navigation.topics_query:get_topic",
    "find_topics": "vkm_corpus.navigation.topics_query:find_topics",
    "similar_sections": "vkm_corpus.navigation.topics_query:similar_sections",
    "section_topics": "vkm_corpus.navigation.topics_query:section_topics",
    "copies_of": "vkm_corpus.navigation.duplicates_query:copies_of",
    "source_overlap": "vkm_corpus.navigation.duplicates_query:source_overlap",
    "copies_of_object": "vkm_corpus.navigation.object_duplicates_query:copies_of_object",
    "shared_formulas": "vkm_corpus.navigation.object_duplicates_query:shared_formulas",
    "find_parameters": "vkm_corpus.navigation.parameters_query:find_parameters",
    "parameter_summary": "vkm_corpus.navigation.parameters_query:parameter_summary",
    # term dictionary (agent TR): RU ↔ EN (DE) equivalents, synonyms, the other-language wording of a query
    "translate_term": "vkm_corpus.navigation.term_dictionary_query:translate_term",
    "synonyms": "vkm_corpus.navigation.term_dictionary_query:synonyms",
    "translate_query": "vkm_corpus.navigation.term_dictionary_query:translate_query",
    "table_structured": "vkm_corpus.navigation.tables_query:get_table_structured",
    "find_tables": "vkm_corpus.navigation.tables_query:find_tables",
    # graph stages of the hybrid search (agent GS): G3 — synonyms, abbreviations and a narrower term of the query
    "expand_query": "vkm_corpus.navigation.expansion_query:expand_query",
    # digitized chart series (agent FD2): DERIVATION values with errors, navigation — not evidence
    "find_figure_series": "vkm_corpus.navigation.figure_series_query:find_figure_series",
    "figure_series": "vkm_corpus.navigation.figure_series_query:get_figure_series",
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
    """Checked NAV datasets → nav.duckdb; exploratory origin stays unverified."""
    import duckdb
    from vkm_corpus.navigation.manifest import load_datasets

    nav_dir = Path(nav_dir)
    tables, identity, manifest = load_datasets(nav_dir)
    if not tables:
        raise NavUnavailable(f"no NAV datasets in {nav_dir.name}")
    tmp = nav_dir / (NAV_DB + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    counts: dict[str, int] = {}
    try:
        for name, table in tables.items():
            con.register("nav_input", table)
            con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM nav_input')
            con.unregister("nav_input")
            counts[name] = int(con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
        meta = {**identity, "rule_versions": manifest.get("rule_versions"), "parts": manifest.get("parts", {}),
                "built_at": manifest.get("built_at"), "packed_at": datetime.now(timezone.utc).isoformat(),
                "counts": counts}
        con.execute("CREATE TABLE nav_meta AS SELECT ?::VARCHAR AS meta_json", [json.dumps(meta, ensure_ascii=False)])
        con.execute("CHECKPOINT")
    finally:
        con.close()
    identity.verify_unchanged()
    os.replace(tmp, nav_dir / NAV_DB)
    return {"nav_db": NAV_DB, "tables": counts, "snapshot_id": identity["snapshot_id"],
            "identity_status": identity["identity_status"], "navigation_only": True,
            "scientific_decision": "NOT_CHECKED"}


def publish(data_root: str | Path, snapshot_id: str, *, require_verified: bool = True) -> Path:
    """Publish a verified origin; explicit exploratory publication stays unverified."""
    import duckdb
    from vkm_corpus.navigation.manifest import UNVERIFIED, VERIFIED, load_datasets

    if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id or snapshot_id.startswith("."):
        raise ValueError("bad NAV publication id")
    root = nav_root(data_root)
    nav_dir = root / snapshot_id
    if not (nav_dir / NAV_DB).exists():
        raise NavUnavailable(f"{snapshot_id}/{NAV_DB} is not packed")
    packed_stamp = NavStore._file_stamp(nav_dir / NAV_DB)
    con = duckdb.connect(str(nav_dir / NAV_DB), read_only=require_verified)
    try:
        rows = con.execute("SELECT meta_json FROM nav_meta").fetchall()
        if len(rows) != 1:
            raise NavUnavailable("NAV packed identity is missing or ambiguous")
        meta = json.loads(rows[0][0])
        if not isinstance(meta, dict):
            raise NavUnavailable("NAV packed identity must be an object")
        origin = meta.get("snapshot_id")
        if origin and origin != snapshot_id:
            raise NavUnavailable("NAV publication name differs from its packed origin")
        if require_verified:
            tables, checked, _manifest = load_datasets(nav_dir, snapshot_id, require_verified=True)
            if (meta.get("identity_status") != VERIFIED or checked["snapshot_id"] != origin
                    or checked["snapshot"] != meta.get("snapshot")
                    or checked["manifest_sha256"] != meta.get("manifest_sha256")
                    or checked["datasets"] != meta.get("datasets")):
                raise NavUnavailable("NAV packed identity is not verified against the current manifest")
            actual = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables "
                                                "WHERE table_schema = 'main'").fetchall()}
            if actual != set(tables) | {"nav_meta"}:
                raise NavUnavailable("NAV packed tables differ from the checked datasets")
            for name, table in tables.items():
                con.register("_publish_checked", table)
                stored = [r[:2] for r in con.execute(f'DESCRIBE "{name}"').fetchall()]
                expected = [r[:2] for r in con.execute('DESCRIBE _publish_checked').fetchall()]
                if stored != expected:
                    raise NavUnavailable(f"NAV packed dataset {name}: schema differs from checked input")
                # EXCEPT ALL preserves duplicates and handles NULL/NaN and nested Arrow values.
                changed = con.execute(f'SELECT EXISTS(SELECT * FROM ((SELECT * FROM "{name}" '
                                      'EXCEPT ALL SELECT * FROM _publish_checked) UNION ALL '
                                      f'(SELECT * FROM _publish_checked EXCEPT ALL SELECT * FROM "{name}")))').fetchone()[0]
                con.unregister("_publish_checked")
                if changed:
                    raise NavUnavailable(f"NAV packed dataset {name}: content differs from checked input")
            checked.verify_unchanged()
        else:
            # Permissive publication makes no checked-content claim, even if the
            # database originally came from a verified pack.
            meta.update({"identity_status": UNVERIFIED, "navigation_only": True,
                         "scientific_decision": "NOT_CHECKED"})
            con.execute("UPDATE nav_meta SET meta_json = ?", [json.dumps(meta, ensure_ascii=False)])
            con.execute("CHECKPOINT")
    finally:
        con.close()
    if require_verified and NavStore._file_stamp(nav_dir / NAV_DB) != packed_stamp:
        raise NavUnavailable("NAV packed database changed during publication")
    if require_verified:
        checked.verify_unchanged()
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
        self._tables: frozenset[str] = frozenset()   # datasets packed in the served nav.duckdb
        self._titles: tuple | None = None          # (stamp, lemma index of the section titles) for search_sections

    # -------------------------------------------------------------- instance
    def _paths(self) -> tuple[Path, Path, str]:
        snap = current_snapshot(self.data_root)
        if not snap:
            raise NavUnavailable("the navigation layer is not published (derived/navigation/CURRENT is missing)")
        if "/" in snap or "\\" in snap or snap.startswith("."):
            raise NavUnavailable("bad NAV CURRENT publication id")
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
        tables = set()
        for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'nav' "
                                "AND schema_name = 'main'").fetchall():
            con.execute(f'CREATE VIEW "{t}" AS SELECT * FROM nav.main."{t}"')
            if not t.startswith("nav_"):     # the part modules query nav_<dataset> (as `vkm-corpus nav build` names them)
                con.execute(f'CREATE VIEW "nav_{t}" AS SELECT * FROM nav.main."{t}"')
            tables.add(t)
        try:
            rows = con.execute("SELECT meta_json FROM nav_meta").fetchall()
            if len(rows) != 1:
                raise NavUnavailable("NAV packed identity is missing or ambiguous")
            meta = json.loads(rows[0][0])
            if not isinstance(meta, dict):
                raise NavUnavailable("NAV packed identity must be an object")
            origin = meta.get("snapshot_id")
            if origin and origin != snap:
                raise NavUnavailable("NAV CURRENT name differs from its packed origin")
            from vkm_corpus.navigation.dependencies import validate
            validate(meta)
            if "dependency_contract" in meta and tables != set(meta.get("datasets", {})) | {"nav_meta"}:
                raise NavUnavailable("NAV packed tables differ from its dependency whitelist")
        except Exception as exc:
            con.close()
            if isinstance(exc, NavUnavailable):
                raise
            raise NavUnavailable("cannot read NAV packed identity") from exc
        # CURRENT is a publication selector, never a replacement for origin metadata.
        self._con, self._stamp, self._snapshot, self._tables = con, stamp, origin, frozenset(tables - {"nav_meta"})
        return con

    # -------------------------------------------------------------- API
    def snapshot_id(self) -> str | None:
        with self._lock:
            self._instance()
            return self._snapshot

    def meta(self) -> dict[str, Any]:
        from vkm_corpus.navigation.manifest import UNVERIFIED, VERIFIED, snapshot_identified

        rows = self.query("SELECT meta_json FROM nav_meta")
        meta = json.loads(rows[0]["meta_json"]) if rows else {}
        snapshot = meta.get("snapshot") or {}
        manifest_hash = meta.get("manifest_sha256")
        verified = (meta.get("identity_status") == VERIFIED and isinstance(snapshot, dict)
                    and snapshot_identified(snapshot) and meta.get("snapshot_id") == snapshot.get("snapshot_id")
                    and isinstance(manifest_hash, str) and re.fullmatch(r"[0-9a-f]{64}", manifest_hash))
        return {**meta, "identity_status": VERIFIED if verified else UNVERIFIED,
                "navigation_only": True, "scientific_decision": "NOT_CHECKED"}

    def datasets(self) -> frozenset[str]:
        """The NAV datasets packed in the served ``nav.duckdb``."""
        with self._lock:
            self._instance()
            return self._tables

    def require(self, *names: str) -> None:
        """``NavUnavailable`` unless the served build holds every dataset in ``names`` (a part not built yet)."""
        have = self.datasets()
        missing = [n for n in names if n not in have]
        if missing:
            raise NavUnavailable(f"the navigation layer {self._snapshot} has no {', '.join(missing)}: its part is not "
                                 f"built into this nav.duckdb")

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
        """Sections whose titles (and key terms, when the build has them) carry the lemmas of the query — the
        morphology of the concept graph (pymorphy3; stems without it), not raw substrings.

        ``score`` = idf-weighted share of the query lemmas found in the title (key terms count 0.6, the ancestors'
        titles 0.3), + 0.15 when the lemmas stand together as in the query; at an equal score deeper sections come
        before whole chapters (TOPIC_BENCHMARK_V1: 61 % of the old top 5 were level 1). ``matched`` names the
        lemmas found. No hit → an empty list."""
        query = _lemmas(text)
        if not query:
            return []
        index = self._title_index()
        idf = {q: math.log((index["n"] + 1) / (index["df"].get(q, 0) + 1)) + 1.0 for q in set(query)}
        total = sum(idf.values())
        wanted = list(dict.fromkeys(query))
        out = []
        for row, title, keys, path in index["rows"]:
            if source_id and row["source_id"] != source_id:
                continue
            in_title = {q for q in wanted if q in title}
            in_keys = {q for q in wanted if q in keys} - in_title
            if not (in_title or in_keys):
                continue
            in_path = {q for q in wanted if q in path} - in_title - in_keys
            score = (sum(idf[q] for q in in_title) + 0.6 * sum(idf[q] for q in in_keys)
                     + 0.3 * sum(idf[q] for q in in_path)) / total
            if len(wanted) > 1 and _together(wanted, index["seq"][row["section_id"]]):
                score += 0.15
            out.append({**row, "score": round(score, 4), "matched": sorted(in_title | in_keys)})
        out.sort(key=lambda r: (-r["score"], -int(r.get("level") or 0), r.get("_span", 0), r["section_id"]))
        return [{k: v for k, v in r.items() if k != "_span"} for r in out[: max(1, int(limit))]]

    def _title_index(self) -> dict[str, Any]:
        """Lemmas of every section title, key terms and ancestor path — built once per served NAV build."""
        with self._lock:
            self._instance()
            stamp = self._stamp
        cached = self._titles
        if cached and cached[0] == stamp:
            return cached[1]
        cols = {r["column_name"] for r in self.query("SELECT column_name FROM duckdb_columns() "
                                                     "WHERE table_name = 'sections'")}
        shown = [c for c in ("section_id", "source_id", "level", "title", "title_path", "page_start_id", "page_end_id",
                             "method") if c in cols]
        extra = [c for c in ("key_terms", "page_start_index", "page_end_index") if c in cols]
        rows, df, seq = [], {}, {}
        for r in self.query(f"SELECT {', '.join(shown + extra)} FROM sections ORDER BY section_id"):
            title_seq = _lemmas(r.get("title") or "")
            title = set(title_seq)
            keys = set(_lemmas(" ; ".join(r.get("key_terms") or []))) if "key_terms" in r else set()
            path = set(_lemmas(r.get("title_path") or ""))
            for q in title | keys:
                df[q] = df.get(q, 0) + 1
            span = (int(r["page_end_index"]) - int(r["page_start_index"])) \
                if r.get("page_start_index") is not None and r.get("page_end_index") is not None else 0
            seq[r["section_id"]] = title_seq
            rows.append(({**{k: r.get(k) for k in shown}, "_span": span}, title, keys, path))
        index = {"rows": rows, "df": df, "seq": seq, "n": len(rows)}
        self._titles = (stamp, index)
        return index


def _sql_path(p: Path) -> str:
    s = str(p)
    if "'" in s:
        raise ValueError("path with a quote")
    return s


# ------------------------------------------------------------------ lemmas for search_sections
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_CRUDE_ENDINGS = tuple(sorted(set("""ями ами ого его ому ему ыми ими ых их ой ей ый ий ая яя ое ее ые ие ую юю ом ем
    ам ям ах ях ов ев ью ия ья ье ы и а я о е у ю ь""".split()), key=len, reverse=True))
_STOP_FALLBACK = frozenset("""и в во на по для о об от до из с со к ко при за под над без через между как что это или
    а также не но the of and in on for to an with by from as at is are""".split())


@lru_cache(maxsize=1)
def _analyser() -> Any:
    """The concept graph's morphology (``concepts_query._morph``: pymorphy3 → Snowball → crude) and its stop lists;
    None when the concept modules cannot be imported (then a crude suffix stripper)."""
    try:
        from vkm_corpus.navigation import concepts, concepts_query

        stop = concepts.STOP_WORDS_RU | concepts.PREPOSITIONS_RU | concepts.STOP_EN
        return concepts_query._morph(), stop, concepts._singular_en
    except Exception:  # noqa: BLE001 - numpy/pyarrow or the dictionaries missing: crude stems
        return None


@lru_cache(maxsize=200_000)
def _lemma(word: str) -> str:
    found = _analyser()
    if found is None:
        for e in _CRUDE_ENDINGS:
            if word.endswith(e) and len(word) - len(e) >= 3:
                return word[: -len(e)]
        return word
    morph, _stop, singular = found
    if not re.search("[а-я]", word):
        return singular(word)
    if morph.name == "pymorphy3":
        rw = morph.ru_word(word)
        return (rw.noun[0] if rw.noun else rw.adj[0] if rw.adj else word).replace("ё", "е")
    return morph.ru_stem(word)


def _lemmas(text: str) -> list[str]:
    """Lemmas of the content words of a text, in order (stop words and words under 3 letters dropped)."""
    found = _analyser()
    stop = found[1] if found is not None else _STOP_FALLBACK
    return [_lemma(w) for w in _WORD.findall((text or "").lower().replace("ё", "е")) if len(w) >= 3 and w not in stop]


def _together(query: list[str], title: list[str]) -> bool:
    """The query lemmas stand next to each other in the title (in the query's order or reversed)."""
    for seq in (query, query[::-1]):
        n = len(seq)
        if any(title[i:i + n] == seq for i in range(len(title) - n + 1)):
            return True
    return False
