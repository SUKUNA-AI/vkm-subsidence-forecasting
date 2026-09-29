"""NAV datasets (``<dataset>.parquet`` + ``manifest.json`` of ``vkm-corpus nav build``) → rows of the NAV graph.

:class:`NavInput` opens the files listed in the manifest (sha256 and row counts verified) as DuckDB views and yields
node rows (ordered by id) and relationship rows (ordered by ``from_id, to_id, key``) for every registry entry of
:mod:`vkm_corpus.graph.nav_schema`. Only rows go to the graph that the projection rules keep; every dataset row is
accounted for as loaded, folded into another row or skipped with a reason (``accounting``), so «loaded counts equal
parquet rows» is checked per rule, never assumed. Projection rules:

* ``COVERS_PAGE`` (``covers_page_v1``) — a section covers every page of its source whose physical index lies in its
  ``[page_start_index, page_end_index]``; the page IDs come from ``section_pages`` (pages of the snapshot); the edge
  says ``deepest`` when ``section_pages`` names the section as the deepest one of the page;
* ``MENTIONED_IN`` (``mentions_top_v1``) — ``term_mentions`` of SECTION units summed per (term, section) (long
  sections are cut into windows by the builder); a pair is kept when it is among the top ``k_term`` sections of the
  term or the top ``k_section`` terms of the section by tf-idf (ties: tf, id); heading-group and page units have no
  section and stay in the NAV DuckDB (``explore_concept``);
* ``SYMBOL_OF`` (``symbol_of_v1``) — a term is the concept of a defined formula symbol when its lemma key (the builder's
  morphology, ``terms.morphology``) is the whole definition, starts it, or (≥ 2 words) starts within its first three
  words; the longest non-nested matches are kept (≤ 2 per definition); single generic words only as the whole
  definition or a project seed. Without that morphology the same rule runs on normalised surface forms
  (``symbol_of_surface_v1``). Links between books go through terms, never through bare symbols;
* formula symbols — only rows with a definition («где σ — …») become ``DEFINED_FOR`` edges and ``FormulaSymbol`` nodes
  (a symbol lives in the space of its source); bare occurrences stay in the NAV DuckDB (``find_formulas``);
* ``SAME_AS`` term edges to a term that was not kept are folded into ``Term.same_as_refs``;
* structured tables (``table_structure``) — one ``NavTable`` per table: ``GRID_OF`` its canonical ``Table``,
  ``TABLE_IN_SECTION`` its section, ``TABULATES`` the term of every property its columns, rows or caption name (one
  edge per term: ``property_keys``, value columns, value rows, caption); cells and columns stay in the NAV DuckDB
  (``get_table_structured``); the caption is cut to ``CAPTION_CHARS``;
* parameter candidates (``parameter_candidates``, ``parameters_v2``) — one ``ParameterValue`` per candidate with its
  locator: ``IN_TABLE`` (a value read from a structured grid: row, column), ``IN_BLOCK`` (the text block, character
  span), ``NEAR_FORMULA`` (a value next to a formula), ``VALUE_IN_SECTION``, and ``VALUE_OF`` the term of its property;
* property terms (``property_term_v1``) — a property of the parameters vocabulary is linked to the term whose lemma key
  (the builder's morphology) is the whole key of its label (label_ru, label_en, synonyms, parentheses dropped first;
  then the dataset's label): a clean key or nothing; without that morphology normalised surface forms are compared
  (``property_term_surface_v1``);
* term dictionary (``term_translations``) — one edge per pair, keyed by ``pair_id``: ``TRANSLATES_TO`` (a → b in the
  language order ru, en, de), ``SYNONYM_OF`` (smaller → larger id), ``ABBREVIATION_OF`` (abbreviation → full form),
  with methods, score, status and up to three example pages; a pair whose two lemma keys give one term id is skipped;
  a term id that ``terms`` does not hold becomes a ``Term`` node with ``dictionary_only = true``;
* object duplicates (``object_dup_clusters``, ``object_dup_members``) — one ``ObjectDupGroup`` per group and one
  ``DUP_MEMBER_OF`` edge per member (Figure, Table or Formula → group) with the match evidence and ``is_primary``;
* every other dataset of the build is accounted for as not projected, with the reason (``NOT_PROJECTED``).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from vkm_corpus.graph import nav_schema as N
from vkm_corpus.graph.common import ProjectionError, normalize_value, sha256_file

MANIFEST_FORMAT = "vkm-nav-manifest-v1"
TOPIC_DATASETS = ("topics", "topic_members", "topic_edges", "section_aggregates")
# datasets the graph does not project: every row is accounted for as skipped with this reason (P5)
NOT_PROJECTED: dict[str, str] = {
    "table_cells": "cells stay in the NAV DuckDB (get_table_structured)",
    "parameter_summary": "per-property counts stay in the NAV DuckDB (parameter_summary)",
    "formula_keys": "canonical formula keys stay in the NAV DuckDB (shared_formulas)",
    "figure_hashes": "image hashes: a cache of the object_duplicates build",
    "dup_clusters": "text duplicates stay in the NAV DuckDB (copies_of)",
    "dup_members": "text duplicates stay in the NAV DuckDB (copies_of)",
    "source_overlap": "source overlaps stay in the NAV DuckDB (source_overlap)",
    "section_vectors": "section vectors stay in the NAV DuckDB (similar_sections)",
}
# row roles of a structured table that carry values (vkm_corpus.navigation.tables.VALUE_ROWS; a test keeps them equal)
TABLE_VALUE_ROWS: tuple[str, ...] = ("DATA", "STAT_MAX", "STAT_MEAN", "STAT_MEDIAN", "STAT_MIN")
_LANG_RANK = "CASE {c} WHEN 'ru' THEN 0 WHEN 'en' THEN 1 WHEN 'de' THEN 2 ELSE 9 END"

# column roles of agent T's datasets (vkm_corpus.navigation.topics: topics, topic_members, topic_edges,
# section_aggregates); alternative names are tolerated, unknown layouts are skipped with the reason
TOPIC_COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "topics": {"id": ("topic_id", "id"), "parent": ("parent_topic_id", "parent_id"),
               "level": ("level", "depth"), "label": ("label", "name", "title"),
               "label_terms": ("label_terms",)},
    "topic_members": {"topic": ("topic_id",), "section": ("section_id",),
                      "similarity": ("similarity", "score", "cosine", "weight"), "rank": ("rank",),
                      "level": ("level",)},
    "topic_edges": {"src": ("topic_id_a", "src_topic_id", "topic_a", "from_topic_id", "source_topic_id"),
                    "dst": ("topic_id_b", "dst_topic_id", "topic_b", "to_topic_id", "target_topic_id"),
                    "cosine": ("cosine", "similarity", "weight", "score"), "kind": ("kind",),
                    "n_links": ("n_links",), "level": ("level",)},
    "section_aggregates": {"section": ("section_id",)},
}
TOPIC_NODE_EXTRA = ("label_terms", "label_term_ids", "n_children", "n_sections", "n_sources", "central_section_ids",
                    "coherence")
SECTION_AGGREGATE_COLUMNS = ("key_terms", "key_term_ids", "central_page_ids", "n_pages", "n_units", "n_formulas",
                             "n_figures", "n_tables", "n_bib_entries")
TOPIC_EDGE_KINDS = ("RELATED", "RELATED_TOPIC", "SIMILAR", "SIMILAR_TOPIC", "COSINE")


@dataclass(frozen=True)
class NodeRow:
    id: str
    props: dict[str, Any]


@dataclass(frozen=True)
class RelRow:
    from_id: str
    to_id: str
    key: str | None
    props: dict[str, Any]


@dataclass
class ProjectionOptions:
    mentions_top_per_term: int = N.MENTIONS_TOP_PER_TERM
    mentions_top_per_section: int = N.MENTIONS_TOP_PER_SECTION
    symbol_morphology: str = "auto"          # "auto" (the builder's morphology if importable) | "surface"
    verify_files: bool = True

    def rules(self) -> dict[str, Any]:
        return {"covers_page": N.RULE_COVERS_PAGE,
                "mentions": {"rule": N.RULE_MENTIONS, "top_per_term": self.mentions_top_per_term,
                             "top_per_section": self.mentions_top_per_section},
                "symbol_of": {"rule": N.RULE_SYMBOL_OF, "fallback": N.RULE_SYMBOL_OF_SURFACE,
                              "morphology": self.symbol_morphology},
                "formula_symbols": "rows with a definition only",
                "same_as_unkept": "folded into Term.same_as_refs",
                "property_term": {"rule": N.RULE_PROPERTY_TERM, "fallback": N.RULE_PROPERTY_TERM_SURFACE,
                                  "morphology": self.symbol_morphology,
                                  "labels": "label_ru, label_en, synonyms of the parameters vocabulary (parentheses "
                                            "dropped first), then the dataset's property_label: the first whole "
                                            "lemma key that is a term"},
                "tables": {"caption_chars": N.CAPTION_CHARS, "cells_and_columns": "not projected (NAV DuckDB)",
                           "tabulates": "one edge per (table, term): property keys of value columns, value rows "
                                        "and the caption"},
                "dictionary": {"TRANSLATION": "TRANSLATES_TO a → b (language order ru, en, de)",
                               "SYNONYM": "SYNONYM_OF smaller → larger term id",
                               "ABBREVIATION": "ABBREVIATION_OF abbreviation → full form",
                               "key": "pair_id", "self_pair": "skipped (one term id on both sides)",
                               "missing_terms": "Term nodes with dictionary_only = true"},
                "object_duplicates": "one DUP_MEMBER_OF edge per member row (Figure, Table, Formula → group)"}


def _clean(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, list):
        return [v for v in (_clean(x) for x in value) if v is not None]
    return value


def _props(allowed: tuple[str, ...], row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k in allowed:
        v = _clean(row.get(k))
        if v is not None:
            out[k] = normalize_value(v)
    return out


def _quote(path: Path) -> str:
    s = path.as_posix()
    if "'" in s:
        raise ProjectionError("E_PREFLIGHT", "NAV dataset path with a quote", stage="preflight")
    return s


def _pick(cols: set[str], candidates: tuple[str, ...]) -> str | None:
    return next((c for c in candidates if c in cols), None)


class NavInput:
    """The NAV datasets of one snapshot, opened read-only as DuckDB views (``nav_dir`` holds ``manifest.json``)."""

    def __init__(self, nav_dir: Path, manifest: dict[str, Any], manifest_sha256: str, datasets: dict[str, Path],
                 options: ProjectionOptions) -> None:
        import duckdb

        self.nav_dir = nav_dir
        self.manifest = manifest
        self.manifest_sha256 = manifest_sha256
        self.datasets = datasets
        self.options = options
        self.snapshot_id: str = str((manifest.get("snapshot") or {}).get("snapshot_id") or "")
        self.con = duckdb.connect()
        self.columns: dict[str, set[str]] = {}
        for name, path in sorted(datasets.items()):
            self.con.execute(f'CREATE VIEW "{name}" AS SELECT * FROM read_parquet(\'{_quote(path)}\')')
            self.columns[name] = {r[0] for r in self.con.execute(f'DESCRIBE "{name}"').fetchall()}
        self.topic_map, self.topic_problems = self._topic_mapping()
        self.symbol_of_info: dict[str, Any] = {}
        self.property_term_info: dict[str, Any] = {}
        self._derived: set[str] = set()
        self._term_index: dict[str, Any] | None = None

    # ------------------------------------------------------------------ opening
    @classmethod
    def open(cls, nav_dir: str | Path, options: ProjectionOptions | None = None) -> "NavInput":
        options = options or ProjectionOptions()
        nav_dir = Path(nav_dir)
        mpath = nav_dir / "manifest.json"
        if not mpath.is_file():
            raise ProjectionError("E_NO_SNAPSHOT", "manifest.json of the NAV build is missing", stage="snapshot")
        raw = mpath.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
        if manifest.get("format") != MANIFEST_FORMAT:
            raise ProjectionError("E_CANON_MANIFEST_MISMATCH", f"not a {MANIFEST_FORMAT} manifest", stage="snapshot")
        if not (manifest.get("snapshot") or {}).get("snapshot_id"):
            raise ProjectionError("E_NO_SNAPSHOT", "the NAV manifest names no canonical snapshot", stage="snapshot")
        problems: list[dict[str, Any]] = []
        datasets: dict[str, Path] = {}
        for name, entry in sorted((manifest.get("datasets") or {}).items()):
            rel = str(entry.get("path") or f"{name}.parquet")
            if "/" in rel or "\\" in rel or rel.startswith(".") or not name.replace("_", "").isalnum():
                problems.append({"dataset": name, "problem": "bad path"})
                continue
            path = nav_dir / rel
            if not path.is_file():
                problems.append({"dataset": name, "problem": "missing"})
                continue
            if options.verify_files and entry.get("sha256") and sha256_file(path) != entry["sha256"]:
                problems.append({"dataset": name, "problem": "sha256"})
                continue
            datasets[name] = path
        if problems:
            raise ProjectionError("E_CANON_MANIFEST_MISMATCH", f"{len(problems)} NAV dataset(s) do not match the "
                                  "manifest", stage="snapshot", details={"problems": problems})
        inp = cls(nav_dir, manifest, hashlib.sha256(raw).hexdigest(), datasets, options)
        wrong = []
        for name in datasets:
            want = (manifest["datasets"][name] or {}).get("rows")
            got = inp.scalar(f'SELECT count(*) FROM "{name}"')
            if want is not None and int(want) != int(got):
                wrong.append({"dataset": name, "manifest_rows": want, "parquet_rows": got})
        if wrong:
            inp.close()
            raise ProjectionError("E_CANON_MANIFEST_MISMATCH", "row counts differ from the manifest", stage="snapshot",
                                  details={"problems": wrong})
        return inp

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "NavInput":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ queries
    def has(self, *names: str) -> bool:
        return all(n in self.datasets or n in self._derived for n in names)

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        row = self.con.execute(sql, params or []).fetchone()
        return row[0] if row else None

    def fetch(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cur = self.con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def iter_dicts(self, sql: str, batch: int = 20_000) -> Iterator[dict[str, Any]]:
        cur = self.con.cursor()
        try:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            while True:
                chunk = cur.fetchmany(batch)
                if not chunk:
                    break
                for r in chunk:
                    yield dict(zip(cols, r))
        finally:
            cur.close()

    def rule_version(self, dataset: str, default: str) -> str:
        if dataset in self.datasets and "rule_version" in self.columns.get(dataset, set()):
            value = self.scalar(f'SELECT min(rule_version) FROM "{dataset}"')
            if value:
                return str(value)
        return default

    # ------------------------------------------------------------------ availability
    def skipped_types(self) -> dict[str, str]:
        """Registry entries that cannot be built from the present datasets → reason."""
        out: dict[str, str] = {}
        for node in N.NODE_TYPES:
            missing = [d for d in node.datasets if not self.has(d)]
            if missing:
                out[node.label] = f"dataset(s) absent: {', '.join(missing)}"
            elif node.part == "topics" and self.topic_problems.get("topics"):
                out[node.label] = self.topic_problems["topics"]
        for rel in N.REL_TYPES:
            missing = [d for d in rel.datasets if not self.has(d)]
            if missing:
                out[rel.name] = f"dataset(s) absent: {', '.join(missing)}"
                continue
            if rel.part == "topics":
                need = {"NAV_CHILD_OF:NavTopic": ("topics",), "IN_TOPIC": ("topics", "topic_members"),
                        "RELATED_TOPIC": ("topics", "topic_edges")}[rel.name]
                bad = [self.topic_problems[d] for d in need if self.topic_problems.get(d)]
                if bad:
                    out[rel.name] = "; ".join(bad)
                elif rel.name == "NAV_CHILD_OF:NavTopic" and "parent" not in self.topic_map.get("topics", {}):
                    out[rel.name] = "topics has no parent column"
        return out

    def _topic_mapping(self) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
        mapping: dict[str, dict[str, str]] = {}
        problems: dict[str, str] = {}
        required = {"topics": ("id",), "topic_members": ("topic", "section"), "topic_edges": ("src", "dst"),
                    "section_aggregates": ("section",)}
        for ds, spec in TOPIC_COLUMNS.items():
            if ds not in self.datasets:
                continue
            cols = self.columns[ds]
            found = {role: col for role, cands in spec.items() if (col := _pick(cols, cands))}
            missing = [role for role in required[ds] if role not in found]
            if missing:
                problems[ds] = f"{ds}: columns not recognised for {missing} (have {sorted(cols)})"
            mapping[ds] = found
        return mapping, problems

    def column_list(self, dataset: str, columns: Iterable[str], alias: str = "") -> str:
        """SELECT list of ``columns`` of a dataset; a column the dataset lacks is NULL (older or partial builds)."""
        have = self.columns.get(dataset, set())
        prefix = f"{alias}." if alias else ""
        return ", ".join(f'{prefix}"{c}" AS "{c}"' if c in have else f'NULL AS "{c}"' for c in columns)

    # ------------------------------------------------------------------ derived tables
    def _register(self, name: str, table: Any) -> None:
        self.con.register(f"{name}_in", table)
        self.con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM {name}_in")
        self.con.unregister(f"{name}_in")
        self._derived.add(name)

    def ensure_derived(self) -> None:
        if "x_symbol_of" not in self._derived and self.has("formula_symbols", "terms"):
            import pyarrow as pa

            rows, info = derive_symbol_of(self)
            self._register("x_symbol_of", pa.table({
                "term_id": pa.array([r["term_id"] for r in rows], pa.string()),
                "symbol_id": pa.array([r["symbol_id"] for r in rows], pa.string()),
                "n_formulas": pa.array([r["n_formulas"] for r in rows], pa.int32()),
                "match": pa.array([r["match"] for r in rows], pa.string()),
                "morphology": pa.array([r["morphology"] for r in rows], pa.string()),
                "rule_version": pa.array([r["rule_version"] for r in rows], pa.string())}))
            self.symbol_of_info = info
        if "x_property_terms" not in self._derived and self.has("terms") and (
                self.has("parameter_candidates") or self.has("table_structure")):
            import pyarrow as pa

            rows, info = derive_property_terms(self)
            self._register("x_property_terms", pa.table({
                "property_key": pa.array([r["property_key"] for r in rows], pa.string()),
                "term_id": pa.array([r["term_id"] for r in rows], pa.string()),
                "label": pa.array([r["label"] for r in rows], pa.string()),
                "basis": pa.array([r["basis"] for r in rows], pa.string()),
                "match": pa.array([r["match"] for r in rows], pa.string()),
                "morphology": pa.array([r["morphology"] for r in rows], pa.string()),
                "rule_version": pa.array([r["rule_version"] for r in rows], pa.string())}))
            self.property_term_info = info


# ---------------------------------------------------------------- node SQL
def _topic_sql(inp: NavInput) -> str:
    m = inp.topic_map["topics"]
    cols = inp.columns["topics"]
    sel = [f"\"{m['id']}\" AS id", f"\"{m['id']}\" AS topic_id"]
    for role, prop in (("parent", "parent_topic_id"), ("level", "level")):
        if role in m:
            sel.append(f"\"{m[role]}\" AS {prop}")
    if "label" in m:
        sel.append(f"\"{m['label']}\" AS label")
    elif "label_terms" in m:                     # agent T: the label is the list of label terms
        sel.append(f"array_to_string(\"{m['label_terms']}\"[1:4], '; ') AS label")
    sel += [f'"{c}"' for c in TOPIC_NODE_EXTRA if c in cols]
    rv = '"rule_version"' if "rule_version" in cols else "'topics'"
    return f"SELECT {', '.join(sel)}, {rv} AS rule_version FROM topics WHERE \"{m['id']}\" IS NOT NULL ORDER BY id"


def _section_sql(inp: NavInput) -> str:
    agg_join, agg_cols = "", ""
    if "section_aggregates" in inp.datasets and not inp.topic_problems.get("section_aggregates"):
        cols = inp.columns["section_aggregates"]
        key = inp.topic_map["section_aggregates"]["section"]
        present = [c for c in SECTION_AGGREGATE_COLUMNS if c in cols]
        if present:
            agg_cols = ", " + ", ".join(f'a."{c}"' for c in present)
            agg_join = f'LEFT JOIN section_aggregates a ON a."{key}" = s.section_id'
    return (f"SELECT s.section_id AS id, s.section_id, s.source_id, s.work_id, s.parent_section_id, s.level, s.ordinal, "
            f"s.numbering, s.title, s.title_path, s.method, s.confidence, s.page_start_id, s.page_end_id, "
            f"s.page_start_index, s.page_end_index, s.heading_block_id, s.rule_version{agg_cols} "
            f"FROM sections s {agg_join} ORDER BY id")


_SYMBOL_SQL = """
    WITH d AS (SELECT * FROM formula_symbols WHERE definition IS NOT NULL AND symbol_id IS NOT NULL),
    g AS (SELECT symbol_id, coalesce(definition_key, lower(definition)) AS dk, count(*) AS c,
                 arg_min(definition, formula_id) AS definition, arg_min(unit, formula_id) AS unit,
                 arg_min(definition_key, formula_id) AS definition_key
          FROM d GROUP BY 1, 2),
    p AS (SELECT * FROM g QUALIFY row_number() OVER (PARTITION BY symbol_id ORDER BY c DESC, dk) = 1),
    s AS (SELECT symbol_id, min(source_id) AS source_id, arg_min(symbol, formula_id) AS symbol,
                 arg_min(symbol_key, formula_id) AS symbol_key, count(DISTINCT formula_id) AS n_formulas,
                 count(DISTINCT coalesce(definition_key, lower(definition))) AS n_definitions,
                 min(rule_version) AS rule_version
          FROM d GROUP BY symbol_id)
    SELECT s.symbol_id AS id, s.symbol_id, s.source_id, s.symbol, s.symbol_key, p.definition, p.unit, p.definition_key,
           s.n_formulas, s.n_definitions, s.rule_version
    FROM s JOIN p USING (symbol_id) ORDER BY id"""

_PARAMETER_SQL = """
    SELECT parameter_id AS id, parameter_id, formula_id, source_id, symbol, symbol_raw, value_text, value, value_min,
           value_max, unit, block_id, in_formula, context_kind, rule_version
    FROM formula_parameters WHERE parameter_id IS NOT NULL ORDER BY id"""

_TERM_COLS = """t.term_id AS id, t.term_id, t.lemma, t.lemma_key, t.surface_forms, t.language, t.kind, t.n_words,
           t.df_units, t.df_sources, t.tf, t.cvalue, t.idf, t.seed, t.community, t.morphology, t.rule_version"""
_TERM_SQL = f"""
    SELECT {_TERM_COLS}, sa.same_as_refs
    FROM terms t
    LEFT JOIN (SELECT src_term_id AS term_id, list(DISTINCT dst_ref ORDER BY dst_ref) AS same_as_refs FROM term_edges
               WHERE kind = 'SAME_AS' AND dst_term_id IS NULL AND dst_ref IS NOT NULL GROUP BY 1) sa USING (term_id)
    ORDER BY id"""
_TERM_SQL_NO_EDGES = f"SELECT {_TERM_COLS} FROM terms t ORDER BY id"


def _dictionary_pairs_sql() -> str:
    """Pairs of the term dictionary that become edges (a known relation, two term ids, not one id on both sides)."""
    relations = ", ".join(f"'{r}'" for r in N.DICTIONARY_RELATIONS)
    return (f"SELECT * FROM term_translations WHERE pair_id IS NOT NULL AND term_id_a IS NOT NULL "
            f"AND term_id_b IS NOT NULL AND term_id_a <> term_id_b AND relation IN ({relations})")


def dictionary_terms_sql() -> str:
    """Term nodes of the term dictionary that ``terms`` does not hold (``dictionary_only``): lemma, key and language
    of the side seen first in the language order ru, en, de; all languages it appears in; its number of pairs."""
    rank = _LANG_RANK.format(c="language")
    return f"""
        WITH p AS ({_dictionary_pairs_sql()}),
             s AS (SELECT term_id_a AS term_id, lemma_a AS lemma, key_a AS lemma_key, lang_a AS language, pair_id,
                          rule_version FROM p
                   UNION ALL
                   SELECT term_id_b, lemma_b, key_b, lang_b, pair_id, rule_version FROM p),
             r AS (SELECT *, {rank} AS lang_rank FROM s WHERE term_id NOT IN (SELECT term_id FROM terms))
        SELECT term_id AS id, term_id, first(lemma ORDER BY lang_rank, lemma_key, lemma) AS lemma,
               first(lemma_key ORDER BY lang_rank, lemma_key, lemma) AS lemma_key,
               first(language ORDER BY lang_rank, lemma_key, lemma) AS language,
               list(DISTINCT language ORDER BY language) AS languages,
               count(DISTINCT pair_id)::INTEGER AS n_dictionary_pairs, true AS dictionary_only,
               min(rule_version) AS rule_version
        FROM r GROUP BY term_id"""


def _term_sql(inp: NavInput) -> str:
    base = _TERM_SQL if "term_edges" in inp.datasets else _TERM_SQL_NO_EDGES
    if "term_translations" not in inp.datasets:
        return base
    return (f"SELECT * FROM (SELECT * FROM ({base}) UNION ALL BY NAME SELECT * FROM ({dictionary_terms_sql()})) "
            f"ORDER BY id")


_TABLE_NODE_COLS = ("table_id", "source_id", "page_id", "page_index", "section_id", "table_label", "table_number",
                    "n_rows", "n_cols", "n_cells", "n_filled_cells", "n_numeric_cells", "n_header_rows",
                    "header_method", "n_bands", "n_blocks", "orientation", "recognition_method", "parse_method",
                    "confidence", "structure_ok", "covers_region", "quality_flags", "property_keys", "materials",
                    "caption_property_key", "rule_version")
_VALUE_NODE_COLS = tuple(p for p in N.NODE_BY_LABEL["ParameterValue"].properties if p != "candidate_id") + (
    "rule_version",)
_GROUP_NODE_COLS = tuple(p for p in N.NODE_BY_LABEL["ObjectDupGroup"].properties if p != "cluster_id") + (
    "rule_version",)


def _table_sql(inp: NavInput) -> str:
    cap = int(N.CAPTION_CHARS)
    if "caption" in inp.columns.get("table_structure", set()):
        caption = (f"CASE WHEN length(caption) > {cap} THEN left(caption, {cap - 1}) || '…' ELSE caption END "
                   f"AS caption, CASE WHEN length(caption) > {cap} THEN true END AS caption_truncated")
    else:
        caption = "NULL AS caption, NULL AS caption_truncated"
    cols = inp.column_list("table_structure", _TABLE_NODE_COLS)
    return (f"SELECT nav_table_id AS id, nav_table_id, {caption}, {cols} "
            f"FROM table_structure WHERE nav_table_id IS NOT NULL ORDER BY id")


def _value_sql(inp: NavInput) -> str:
    return (f"SELECT candidate_id AS id, candidate_id, {inp.column_list('parameter_candidates', _VALUE_NODE_COLS)} "
            f"FROM parameter_candidates WHERE candidate_id IS NOT NULL ORDER BY id")


def _group_sql(inp: NavInput) -> str:
    return (f"SELECT cluster_id AS id, cluster_id, {inp.column_list('object_dup_clusters', _GROUP_NODE_COLS)} "
            f"FROM object_dup_clusters WHERE cluster_id IS NOT NULL ORDER BY id")


def node_sql(inp: NavInput, node: N.NavNodeType) -> str:
    if node.label == "NavSection":
        return _section_sql(inp)
    if node.label == "FormulaSymbol":
        return _SYMBOL_SQL
    if node.label == "ParameterCandidate":
        return _PARAMETER_SQL
    if node.label == "Term":
        return _term_sql(inp)
    if node.label == "NavTopic":
        return _topic_sql(inp)
    if node.label == "NavTable":
        return _table_sql(inp)
    if node.label == "ParameterValue":
        return _value_sql(inp)
    if node.label == "ObjectDupGroup":
        return _group_sql(inp)
    raise KeyError(node.label)


def _term_post(props: dict[str, Any], row: dict[str, Any]) -> None:
    from vkm_corpus.navigation.ids import norm_text

    names = [row.get("lemma")] + list(row.get("surface_forms") or [])
    keys = sorted({norm_text(x) for x in names if x} - {""})
    if keys:
        props["name_keys"] = keys


_NODE_POST: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {"Term": _term_post}


# ---------------------------------------------------------------- relationship SQL (from_id, to_id, key, props...)
def _mentions_sql(inp: NavInput) -> str:
    kt, ks = int(inp.options.mentions_top_per_term), int(inp.options.mentions_top_per_section)
    return f"""
        WITH p AS (SELECT m.term_id, m.section_id, sum(m.tf)::BIGINT AS tf, round(sum(m.tfidf), 6) AS tfidf,
                          count(*)::INTEGER AS n_units, list_sort(list_distinct(flatten(list(m.page_ids))))[1:3] AS page_ids
                   FROM term_mentions m
                   WHERE m.section_id IS NOT NULL
                     AND m.section_id IN (SELECT section_id FROM sections)
                     AND m.term_id IN (SELECT term_id FROM terms)
                   GROUP BY 1, 2),
             r AS (SELECT *, row_number() OVER (PARTITION BY term_id ORDER BY tfidf DESC, tf DESC, section_id)
                                 AS rank_in_term,
                             row_number() OVER (PARTITION BY section_id ORDER BY tfidf DESC, tf DESC, term_id)
                                 AS rank_in_section FROM p)
        SELECT term_id AS from_id, section_id AS to_id, NULL AS key, tf, tfidf, n_units, page_ids,
               rank_in_term::INTEGER AS rank_in_term, rank_in_section::INTEGER AS rank_in_section,
               '{N.RULE_MENTIONS}' AS rule_version
        FROM r WHERE rank_in_term <= {kt} OR rank_in_section <= {ks} ORDER BY from_id, to_id"""


def _term_edge_sql(kind: str, props: str, extra_where: str = "") -> str:
    return f"""
        SELECT e.src_term_id AS from_id, e.dst_term_id AS to_id, e.edge_id AS key, {props}, e.rule_version
        FROM term_edges e
        WHERE e.kind = '{kind}' AND e.dst_term_id IS NOT NULL {extra_where}
          AND e.src_term_id IN (SELECT term_id FROM terms) AND e.dst_term_id IN (SELECT term_id FROM terms)
        ORDER BY from_id, to_id, key"""


def _topic_rel_sql(inp: NavInput, name: str) -> str:
    m = inp.topic_map
    tid = m["topics"]["id"]
    if name == "NAV_CHILD_OF:NavTopic":
        par = m["topics"]["parent"]
        return (f'SELECT "{tid}" AS from_id, "{par}" AS to_id, NULL AS key, {_rv(inp, "topics")} AS rule_version '
                f'FROM topics WHERE "{par}" IS NOT NULL AND "{par}" IN (SELECT "{tid}" FROM topics) '
                f'ORDER BY from_id, to_id')
    if name == "IN_TOPIC":
        mm = m["topic_members"]
        sim = f'"{mm["similarity"]}"' if "similarity" in mm else "NULL"
        rank = f'"{mm["rank"]}"' if "rank" in mm else "NULL"
        level = f'"{mm["level"]}"' if "level" in mm else "NULL"
        return (f'SELECT "{mm["section"]}" AS from_id, "{mm["topic"]}" AS to_id, NULL AS key, '
                f'max({sim})::DOUBLE AS similarity, min({rank})::INTEGER AS rank, min({level})::INTEGER AS level, '
                f'{_rv(inp, "topic_members", agg=True)} AS rule_version FROM topic_members '
                f'WHERE "{mm["section"]}" IN (SELECT section_id FROM sections) '
                f'AND "{mm["topic"]}" IN (SELECT "{tid}" FROM topics) GROUP BY 1, 2 ORDER BY from_id, to_id')
    if name == "RELATED_TOPIC":
        me = m["topic_edges"]
        cos = f'"{me["cosine"]}"' if "cosine" in me else "NULL"
        links = f'"{me["n_links"]}"' if "n_links" in me else "NULL"
        level = f'"{me["level"]}"' if "level" in me else "NULL"
        kinds = ", ".join(f"'{k}'" for k in TOPIC_EDGE_KINDS)
        where = f'AND upper("{me["kind"]}") IN ({kinds})' if "kind" in me else ""
        return (f'SELECT least("{me["src"]}", "{me["dst"]}") AS from_id, greatest("{me["src"]}", "{me["dst"]}") AS to_id, '
                f'NULL AS key, max({cos})::DOUBLE AS cosine, max({links})::INTEGER AS n_links, '
                f'min({level})::INTEGER AS level, {_rv(inp, "topic_edges", agg=True)} AS rule_version '
                f'FROM topic_edges WHERE "{me["src"]}" <> "{me["dst"]}" {where} '
                f'AND "{me["src"]}" IN (SELECT "{tid}" FROM topics) AND "{me["dst"]}" IN (SELECT "{tid}" FROM topics) '
                f'GROUP BY 1, 2 ORDER BY from_id, to_id')
    raise KeyError(name)


def _rv(inp: NavInput, dataset: str, agg: bool = False) -> str:
    if "rule_version" in inp.columns.get(dataset, set()):
        return "min(rule_version)" if agg else "rule_version"
    return "'topics'"


def _tabulates_sql(inp: NavInput) -> str:
    """One edge per (structured table, term of a property it names): the property keys behind it, the value columns
    and value rows naming them, whether the caption names one."""
    col = ("SELECT table_id, property_key, count(*)::INTEGER AS n FROM table_columns "
           "WHERE property_key IS NOT NULL AND role = 'VALUE' GROUP BY 1, 2") if inp.has("table_columns") else \
        "SELECT NULL::VARCHAR AS table_id, NULL::VARCHAR AS property_key, 0 AS n WHERE false"
    roles = ", ".join(f"'{r}'" for r in TABLE_VALUE_ROWS)
    rows = (f'SELECT table_id, row_property_key AS property_key, count(DISTINCT "row")::INTEGER AS n FROM table_cells '
            f"WHERE row_property_key IS NOT NULL AND row_role IN ({roles}) GROUP BY 1, 2") if inp.has("table_cells") \
        else "SELECT NULL::VARCHAR AS table_id, NULL::VARCHAR AS property_key, 0 AS n WHERE false"
    return f"""
        WITH tp AS (SELECT DISTINCT * FROM (
                        SELECT nav_table_id, table_id, unnest(property_keys) AS property_key, caption_property_key
                        FROM table_structure WHERE nav_table_id IS NOT NULL) WHERE property_key IS NOT NULL),
             col AS ({col}),
             rw AS ({rows})
        SELECT tp.nav_table_id AS from_id, pt.term_id AS to_id, NULL AS key,
               list(DISTINCT tp.property_key ORDER BY tp.property_key) AS property_keys,
               sum(coalesce(col.n, 0))::INTEGER AS n_columns, sum(coalesce(rw.n, 0))::INTEGER AS n_rows,
               coalesce(bool_or(tp.caption_property_key = tp.property_key), false) AS in_caption,
               min(pt.match) AS match, min(pt.rule_version) AS rule_version
        FROM tp JOIN x_property_terms pt ON pt.property_key = tp.property_key
        LEFT JOIN col ON col.table_id = tp.table_id AND col.property_key = tp.property_key
        LEFT JOIN rw ON rw.table_id = tp.table_id AND rw.property_key = tp.property_key
        GROUP BY 1, 2 ORDER BY from_id, to_id"""


def _dictionary_sql(inp: NavInput, relation: str) -> str:
    """Edges of one relation of the term dictionary (see ``ProjectionOptions.rules()['dictionary']``)."""
    if relation == "ABBREVIATION":                      # the builder puts the full form first (a)
        f, t, fl, tl = "term_id_b", "term_id_a", "lang_b", "lang_a"
    elif relation == "SYNONYM":                         # symmetric: stored once, from the smaller id
        f, t = "least(term_id_a, term_id_b)", "greatest(term_id_a, term_id_b)"
        fl, tl = ("CASE WHEN term_id_a <= term_id_b THEN lang_a ELSE lang_b END",
                  "CASE WHEN term_id_a <= term_id_b THEN lang_b ELSE lang_a END")
    else:                                               # TRANSLATION: a → b in the language order ru, en, de
        f, t, fl, tl = "term_id_a", "term_id_b", "lang_a", "lang_b"
    have = inp.columns.get("term_translations", set())
    pages = ("list_sort(list_distinct([e.page_id FOR e IN evidence IF e.page_id IS NOT NULL]))[1:3]"
             if "evidence" in have else "NULL")
    extra = inp.column_list("term_translations", ("methods", "n_sources", "n_occurrences", "cosine", "score",
                                                  "status", "rule_version"))
    return f"""
        SELECT {f} AS from_id, {t} AS to_id, pair_id AS key, pair_id, relation, {fl} AS from_language,
               {tl} AS to_language, {pages} AS example_page_ids, {extra}
        FROM ({_dictionary_pairs_sql()}) WHERE relation = '{relation}'
        ORDER BY from_id, to_id, key"""


def _dup_member_sql(inp: NavInput, object_type: str) -> str:
    cols = inp.column_list("object_dup_members", N.REL_BY_NAME["DUP_MEMBER_OF:Figure"].properties + ("rule_version",),
                           alias="m")
    return f"""
        SELECT m.object_id AS from_id, m.cluster_id AS to_id, NULL AS key, {cols}
        FROM object_dup_members m
        WHERE m.object_type = '{object_type}' AND m.object_id IS NOT NULL
          AND m.cluster_id IN (SELECT cluster_id FROM object_dup_clusters)
        ORDER BY from_id, to_id"""


_DUP_OBJECT_TYPE = {f"DUP_MEMBER_OF:{label}": object_type for object_type, label in N.DUP_MEMBER_LABELS.items()}


def rel_sql(inp: NavInput, rel: N.NavRelType) -> str:
    name = rel.name
    if name == "NAV_CHILD_OF:NavSection":
        return ("SELECT section_id AS from_id, parent_section_id AS to_id, NULL AS key, rule_version FROM sections "
                "WHERE parent_section_id IS NOT NULL AND parent_section_id IN (SELECT section_id FROM sections) "
                "ORDER BY from_id, to_id")
    if name == "HAS_NAV_SECTION":
        return ("SELECT source_id AS from_id, section_id AS to_id, NULL AS key, ordinal, rule_version FROM sections "
                "WHERE parent_section_id IS NULL ORDER BY from_id, to_id")
    if name == "COVERS_PAGE":
        return f"""
            WITH pg AS (SELECT source_id, page_id, min(page_index) AS page_index FROM section_pages
                        WHERE page_id IS NOT NULL GROUP BY 1, 2),
                 deep AS (SELECT DISTINCT section_id, page_id FROM section_pages)
            SELECT s.section_id AS from_id, pg.page_id AS to_id, NULL AS key, pg.page_index,
                   (d.section_id IS NOT NULL) AS deepest, '{N.RULE_COVERS_PAGE}' AS rule_version
            FROM sections s
            JOIN pg ON pg.source_id = s.source_id AND pg.page_index BETWEEN s.page_start_index AND s.page_end_index
            LEFT JOIN deep d ON d.section_id = s.section_id AND d.page_id = pg.page_id
            ORDER BY from_id, to_id"""
    if name == "IN_SECTION":
        return """
            SELECT formula_id AS from_id, section_id AS to_id, NULL AS key, equation_number, kind, number_method,
                   n_symbols, n_defined_symbols, n_refs_in, n_parameters, intro_block_id, where_block_ids, rule_version
            FROM formula_context
            WHERE section_id IS NOT NULL AND section_id IN (SELECT section_id FROM sections)
            ORDER BY from_id, to_id"""
    if name == "DEFINED_FOR":
        return """
            SELECT symbol_id AS from_id, formula_id AS to_id, NULL AS key, role, in_formula, n_occurrences, definition,
                   unit, definition_block_id, match_method, rule_version
            FROM formula_symbols WHERE definition IS NOT NULL AND symbol_id IS NOT NULL
            ORDER BY from_id, to_id"""
    if name in ("NAV_REFERS_TO", "NAV_BLOCK_REFERS_TO"):
        src, cond = (("citing_formula_id", "citing_formula_id IS NOT NULL") if name == "NAV_REFERS_TO"
                     else ("block_id", "citing_formula_id IS NULL AND block_id IS NOT NULL"))
        return f"""
            SELECT {src} AS from_id, formula_id AS to_id, ref_id AS key, ref_id, ref_type AS kind, resolution,
                   number_text, block_id, n_mentions, rule_version
            FROM formula_refs WHERE formula_id IS NOT NULL AND ref_id IS NOT NULL AND {cond}
            ORDER BY from_id, to_id, key"""
    if name == "NEAR_FORMULA":
        return """
            SELECT parameter_id AS from_id, formula_id AS to_id, NULL AS key, context_kind, in_formula, rule_version
            FROM formula_parameters WHERE parameter_id IS NOT NULL AND formula_id IS NOT NULL
            ORDER BY from_id, to_id"""
    if name == "CO_OCCURS":
        return _term_edge_sql("CO_OCCURS", "e.edge_id, e.weight AS npmi, e.n_units, e.n_sources, e.examples, e.rule")
    if name == "CONTAINS_TERM":
        return _term_edge_sql("CONTAINS", "e.edge_id, e.weight, e.n_units, e.n_sources, e.rule")
    if name == "SAME_TERM_AS":
        return _term_edge_sql("SAME_AS", "e.edge_id, e.weight, e.n_units, e.n_sources, e.examples, e.rule")
    if name == "DEFINED_AS":
        return """
            SELECT e.src_term_id AS from_id, e.dst_ref AS to_id, e.edge_id AS key, e.edge_id, e.examples[1] AS page_id,
                   e.rule, e.rule_version
            FROM term_edges e
            WHERE e.kind = 'DEFINED_AS' AND e.dst_ref IS NOT NULL AND e.src_term_id IN (SELECT term_id FROM terms)
            ORDER BY from_id, to_id, key"""
    if name == "MENTIONED_IN":
        return _mentions_sql(inp)
    if name == "SYMBOL_OF":
        return ("SELECT term_id AS from_id, symbol_id AS to_id, NULL AS key, n_formulas, match, morphology, "
                "rule_version FROM x_symbol_of ORDER BY from_id, to_id")
    if name in ("NAV_CHILD_OF:NavTopic", "IN_TOPIC", "RELATED_TOPIC"):
        return _topic_rel_sql(inp, name)
    # structured tables
    if name == "GRID_OF":
        cols = inp.column_list("table_structure", ("parse_method", "structure_ok", "covers_region", "rule_version"))
        return (f"SELECT nav_table_id AS from_id, table_id AS to_id, NULL AS key, {cols} "
                f"FROM table_structure WHERE nav_table_id IS NOT NULL AND table_id IS NOT NULL ORDER BY from_id, to_id")
    if name == "TABLE_IN_SECTION":
        return (f"SELECT nav_table_id AS from_id, section_id AS to_id, NULL AS key, "
                f"{inp.column_list('table_structure', ('table_number', 'rule_version'))} FROM table_structure "
                f"WHERE nav_table_id IS NOT NULL AND section_id IN (SELECT section_id FROM sections) "
                f"ORDER BY from_id, to_id")
    if name == "TABULATES":
        return _tabulates_sql(inp)
    # parameter candidates
    if name == "VALUE_IN_SECTION":
        return ("SELECT candidate_id AS from_id, section_id AS to_id, NULL AS key, method, rule_version "
                "FROM parameter_candidates WHERE candidate_id IS NOT NULL "
                "AND section_id IN (SELECT section_id FROM sections) ORDER BY from_id, to_id")
    if name == "IN_TABLE":
        return ("SELECT p.candidate_id AS from_id, s.nav_table_id AS to_id, NULL AS key, p.table_row, p.table_col, "
                "p.rule_version FROM parameter_candidates p JOIN table_structure s ON s.table_id = p.table_id "
                "WHERE p.candidate_id IS NOT NULL AND s.nav_table_id IS NOT NULL ORDER BY from_id, to_id")
    if name == "IN_BLOCK":
        return ("SELECT candidate_id AS from_id, block_id AS to_id, NULL AS key, method, char_start, char_end, "
                "rule_version FROM parameter_candidates WHERE candidate_id IS NOT NULL AND block_id IS NOT NULL "
                "ORDER BY from_id, to_id")
    if name == "NEAR_FORMULA:ParameterValue":
        return ("SELECT candidate_id AS from_id, formula_id AS to_id, NULL AS key, method, rule_version "
                "FROM parameter_candidates WHERE candidate_id IS NOT NULL AND formula_id IS NOT NULL "
                "ORDER BY from_id, to_id")
    if name == "VALUE_OF":
        return ("SELECT p.candidate_id AS from_id, pt.term_id AS to_id, NULL AS key, p.property_key, pt.match, "
                "pt.rule_version FROM parameter_candidates p JOIN x_property_terms pt ON pt.property_key = "
                "p.property_key WHERE p.candidate_id IS NOT NULL ORDER BY from_id, to_id")
    # term dictionary
    for relation, entry in N.DICTIONARY_RELATIONS.items():
        if name == entry:
            return _dictionary_sql(inp, relation)
    # object duplicates
    if name in _DUP_OBJECT_TYPE:
        return _dup_member_sql(inp, _DUP_OBJECT_TYPE[name])
    raise KeyError(name)


# ---------------------------------------------------------------- rows
def _common(snapshot_id: str, rule_version: Any, run_id: str | None) -> dict[str, Any]:
    out = {"layer": N.LAYER_VALUE, "snapshot_id": snapshot_id, "rule_version": str(rule_version or "")}
    if run_id:
        out["projection_run_id"] = run_id          # the load that wrote it (the sweep removes every other one)
    return out


def iter_nodes(inp: NavInput, node: N.NavNodeType, run_id: str | None = None) -> Iterator[NodeRow]:
    post = _NODE_POST.get(node.label)
    for row in inp.iter_dicts(node_sql(inp, node)):
        props = _props(node.properties, row)
        props.update(_common(inp.snapshot_id, row.get("rule_version"), run_id))
        props["id"] = row["id"]
        props["review_status"] = N.REVIEW_STATUS
        if post:
            post(props, row)
        yield NodeRow(str(row["id"]), props)


def iter_rels(inp: NavInput, rel: N.NavRelType, run_id: str | None = None) -> Iterator[RelRow]:
    inp.ensure_derived()
    for row in inp.iter_dicts(rel_sql(inp, rel)):
        props = _props(rel.properties, row)
        props.update(_common(inp.snapshot_id, row.get("rule_version"), run_id))
        yield RelRow(str(row["from_id"]), str(row["to_id"]), row.get("key"), props)


def count_node_rows(inp: NavInput, node: N.NavNodeType) -> int:
    return int(inp.scalar(f"SELECT count(*) FROM ({node_sql(inp, node)})"))


def count_rel_rows(inp: NavInput, rel: N.NavRelType) -> int:
    inp.ensure_derived()
    return int(inp.scalar(f"SELECT count(*) FROM ({rel_sql(inp, rel)})"))


def expected_counts(inp: NavInput) -> dict[str, Any]:
    """Rows the projection writes per label and registry relationship (skipped entries: 0 + reason)."""
    skipped = inp.skipped_types()
    nodes = {n.label: (0 if n.label in skipped else count_node_rows(inp, n)) for n in N.NODE_TYPES}
    rels = {r.name: (0 if r.name in skipped else count_rel_rows(inp, r)) for r in N.REL_TYPES}
    out: dict[str, Any] = {"nodes": nodes, "rels": rels, "skipped": skipped}
    if "Term" not in skipped and inp.has("term_translations"):        # included in nodes["Term"] (check N8)
        out["subsets"] = {"Term.dictionary_only": int(inp.scalar(f"SELECT count(*) FROM ({dictionary_terms_sql()})"))}
    return out


# ---------------------------------------------------------------- accounting: parquet rows → loaded / folded / skipped
def accounting(inp: NavInput, expected: dict[str, Any]) -> dict[str, Any]:
    """Every dataset row is loaded, folded or skipped by a named rule; ``closed`` says the sums add up."""
    out: dict[str, Any] = {}
    n, r = expected["nodes"], expected["rels"]

    def put(ds: str, loaded: dict[str, int], skipped: dict[str, int], rows: int | None = None,
            folded: dict[str, int] | None = None, identity: bool = True) -> None:
        total = int(rows if rows is not None else inp.scalar(f'SELECT count(*) FROM "{ds}"'))
        s = sum(loaded.values()) + sum(skipped.values()) + sum((folded or {}).values())
        out[ds] = {"rows": total, "loaded": loaded, "folded": folded or {}, "skipped": skipped,
                   "closed": (s == total) if identity else None}

    has = inp.has
    if has("sections"):
        put("sections", {"NavSection": n["NavSection"]}, {"duplicate or invalid id": inp.scalar(
            "SELECT count(*) - count(DISTINCT section_id) FROM sections")})
        tree = r["NAV_CHILD_OF:NavSection"] + r["HAS_NAV_SECTION"]
        out["sections"]["edges"] = {"NAV_CHILD_OF:NavSection": r["NAV_CHILD_OF:NavSection"],
                                    "HAS_NAV_SECTION": r["HAS_NAV_SECTION"],
                                    "one_tree_edge_per_section": tree == n["NavSection"]}
    if has("section_pages", "sections"):
        deep = int(inp.scalar("SELECT count(*) FROM (SELECT DISTINCT section_id, page_id FROM section_pages)"))
        dup = int(inp.scalar("SELECT count(*) FROM section_pages")) - deep
        in_edges = r["COVERS_PAGE"] and int(inp.scalar(
            f"SELECT count(*) FROM ({rel_sql(inp, N.REL_BY_NAME['COVERS_PAGE'])}) WHERE deepest"))
        put("section_pages", {}, {"duplicate (section, page)": dup,
                                  "section or page not covered": deep - int(in_edges or 0)},
            folded={"COVERS_PAGE.deepest": int(in_edges or 0)})
    if has("formula_context"):
        put("formula_context", {"IN_SECTION": r["IN_SECTION"]}, {"no section": inp.scalar(
            "SELECT count(*) FROM formula_context WHERE section_id IS NULL OR section_id NOT IN "
            "(SELECT section_id FROM sections)") if has("sections") else inp.scalar(
            "SELECT count(*) FROM formula_context")})
    if has("formula_symbols"):
        put("formula_symbols", {"DEFINED_FOR": r["DEFINED_FOR"]}, {"no definition (bare occurrence)": inp.scalar(
            "SELECT count(*) FROM formula_symbols WHERE definition IS NULL OR symbol_id IS NULL")})
        out["formula_symbols"]["nodes"] = {"FormulaSymbol": n["FormulaSymbol"]}
    if has("formula_refs"):
        put("formula_refs", {"NAV_REFERS_TO": r["NAV_REFERS_TO"], "NAV_BLOCK_REFERS_TO": r["NAV_BLOCK_REFERS_TO"]},
            {"unresolved (no formula)": inp.scalar(
                "SELECT count(*) FROM formula_refs WHERE formula_id IS NULL OR ref_id IS NULL OR "
                "(citing_formula_id IS NULL AND block_id IS NULL)")})
    if has("formula_parameters"):
        put("formula_parameters", {"ParameterCandidate": n["ParameterCandidate"]},
            {"no id": inp.scalar("SELECT count(*) FROM formula_parameters WHERE parameter_id IS NULL")})
        out["formula_parameters"]["edges"] = {"NEAR_FORMULA": r["NEAR_FORMULA"]}
    if has("terms"):
        # every row of `terms` is a Term node; dictionary-only Term nodes are counted under term_translations
        put("terms", {"Term": int(inp.scalar("SELECT count(*) FROM terms"))}, {})
    if has("term_edges", "terms"):
        folded = int(inp.scalar("SELECT count(*) FROM term_edges WHERE kind = 'SAME_AS' AND dst_term_id IS NULL "
                                "AND dst_ref IS NOT NULL"))
        loaded = {k: r[k] for k in ("CO_OCCURS", "CONTAINS_TERM", "SAME_TERM_AS", "DEFINED_AS")}
        total = int(inp.scalar("SELECT count(*) FROM term_edges"))
        other = total - sum(loaded.values()) - folded
        put("term_edges", loaded, {"unknown kind or term": other}, rows=total,
            folded={"Term.same_as_refs": folded})
    if has("term_mentions", "terms", "sections"):
        stats = inp.fetch("""
            SELECT count(*) FILTER (WHERE section_id IS NULL) AS no_section,
                   count(*) FILTER (WHERE section_id IS NOT NULL) AS section_rows,
                   count(DISTINCT (term_id, section_id)) FILTER (WHERE section_id IS NOT NULL) AS pairs
            FROM term_mentions""")[0]
        kept = r["MENTIONED_IN"]
        pairs = int(stats["pairs"])
        out["term_mentions"] = {
            "rows": int(stats["no_section"]) + int(stats["section_rows"]),
            "loaded": {"MENTIONED_IN": kept},
            "folded": {"windows of one (term, section) pair": int(stats["section_rows"]) - pairs},
            "skipped": {"unit without a section (heading group, page)": int(stats["no_section"]),
                        f"below top-{inp.options.mentions_top_per_term}/term and "
                        f"top-{inp.options.mentions_top_per_section}/section": pairs - kept},
            "closed": True}
    for ds in ("topics", "topic_members", "topic_edges", "section_aggregates"):
        if has(ds):
            loaded: dict[str, int] = {}
            if ds == "topics":
                loaded = {"NavTopic": n["NavTopic"]}
            elif ds == "topic_members":
                loaded = {"IN_TOPIC": r["IN_TOPIC"]}
            elif ds == "topic_edges":
                loaded = {"RELATED_TOPIC": r["RELATED_TOPIC"]}
            total = int(inp.scalar(f'SELECT count(*) FROM "{ds}"'))
            out[ds] = {"rows": total, "loaded": loaded, "folded": {} if ds != "section_aggregates" else
                       {"NavSection properties": total}, "skipped": {
                           "not projected (see problems)": total - sum(loaded.values())} if ds != "section_aggregates"
                       else {}, "closed": None, "problem": inp.topic_problems.get(ds)}
    skipped = expected["skipped"]

    def ids(ds: str, col: str) -> dict[str, int]:
        row = inp.fetch(f'SELECT count(*) FILTER (WHERE "{col}" IS NULL) AS no_id, '
                        f'count("{col}") - count(DISTINCT "{col}") AS dup FROM "{ds}"')[0]
        return {"no id": int(row["no_id"]), "duplicate id": int(row["dup"])}

    def edge_rows(names: Iterable[str]) -> dict[str, int]:
        return {k: r[k] for k in names}

    def why(name: str) -> str:
        return f"{name} skipped: {skipped[name]}"

    if has("table_structure"):
        put("table_structure", {"NavTable": n["NavTable"]}, ids("table_structure", "nav_table_id"))
        out["table_structure"]["edges"] = edge_rows(("GRID_OF", "TABLE_IN_SECTION", "TABULATES"))
        notes: dict[str, int] = {}
        if "TABLE_IN_SECTION" not in skipped:
            notes["TABLE_IN_SECTION: no section or an unknown one"] = n["NavTable"] - r["TABLE_IN_SECTION"]
        if "TABULATES" not in skipped:
            pairs = inp.fetch("""
                WITH tp AS (SELECT DISTINCT * FROM (SELECT nav_table_id, unnest(property_keys) AS property_key
                            FROM table_structure WHERE nav_table_id IS NOT NULL) WHERE property_key IS NOT NULL)
                SELECT count(*) AS pairs, count(*) FILTER (WHERE property_key IN
                       (SELECT property_key FROM x_property_terms)) AS mapped FROM tp""")[0]
            notes["TABULATES: (table, property) pairs"] = int(pairs["pairs"])
            notes["TABULATES: pairs whose property has no term (not linked)"] = int(pairs["pairs"]) - int(
                pairs["mapped"])
        out["table_structure"]["edge_notes"] = notes
    if has("table_columns"):
        total = int(inp.scalar("SELECT count(*) FROM table_columns"))
        named = int(inp.scalar("SELECT count(*) FROM table_columns WHERE property_key IS NOT NULL AND role = 'VALUE'"))
        rest = {"other columns: the grid stays in the NAV DuckDB (get_table_structured)": total - named}
        if "TABULATES" in skipped:
            put("table_columns", {}, {why("TABULATES"): named, **rest}, rows=total)
        else:
            linked = int(inp.scalar("SELECT count(*) FROM table_columns WHERE property_key IS NOT NULL AND role = "
                                    "'VALUE' AND property_key IN (SELECT property_key FROM x_property_terms)"))
            put("table_columns", {}, {"value columns naming a property without a term": named - linked, **rest},
                rows=total, folded={"TABULATES.n_columns": linked})
    if has("parameter_candidates"):
        put("parameter_candidates", {"ParameterValue": n["ParameterValue"]}, ids("parameter_candidates",
                                                                                "candidate_id"))
        out["parameter_candidates"]["edges"] = edge_rows(("VALUE_IN_SECTION", "IN_TABLE", "IN_BLOCK",
                                                          "NEAR_FORMULA:ParameterValue", "VALUE_OF"))
        notes = {}
        if "VALUE_IN_SECTION" not in skipped:
            notes["VALUE_IN_SECTION: no section or an unknown one"] = n["ParameterValue"] - r["VALUE_IN_SECTION"]
        tables = int(inp.scalar("SELECT count(*) FROM parameter_candidates WHERE candidate_id IS NOT NULL "
                                "AND table_id IS NOT NULL"))
        notes["IN_TABLE: values of a table without a structured grid" if "IN_TABLE" not in skipped
              else why("IN_TABLE")] = tables - r["IN_TABLE"]
        if "VALUE_OF" not in skipped:
            notes["VALUE_OF: values whose property has no term"] = n["ParameterValue"] - r["VALUE_OF"]
        out["parameter_candidates"]["edge_notes"] = notes
    if has("term_translations"):
        total = int(inp.scalar("SELECT count(*) FROM term_translations"))
        names = tuple(N.DICTIONARY_RELATIONS.values())
        if any(x in skipped for x in names):
            put("term_translations", {}, {why(names[0]): total}, rows=total)
        else:
            same = int(inp.scalar("SELECT count(*) FROM term_translations WHERE term_id_a = term_id_b"))
            loaded = edge_rows(names)
            put("term_translations", loaded, {
                "one term id on both sides (one lemma key in two languages)": same,
                "unknown relation or no term id": total - sum(loaded.values()) - same}, rows=total)
            out["term_translations"]["nodes"] = {"Term (dictionary_only)": int(inp.scalar(
                f"SELECT count(*) FROM ({dictionary_terms_sql()})"))}
    if has("object_dup_clusters"):
        put("object_dup_clusters", {"ObjectDupGroup": n["ObjectDupGroup"]}, ids("object_dup_clusters", "cluster_id"))
    if has("object_dup_members"):
        total = int(inp.scalar("SELECT count(*) FROM object_dup_members"))
        names = tuple(_DUP_OBJECT_TYPE)
        if any(x in skipped for x in names):
            put("object_dup_members", {}, {why(names[0]): total}, rows=total)
        else:
            loaded = edge_rows(names)
            put("object_dup_members", loaded, {"unknown object type or group, or no object id": total - sum(
                loaded.values())}, rows=total)
    for ds in sorted(inp.datasets):                  # every other dataset of the build: not projected, with the reason
        if ds not in out:
            total = int(inp.scalar(f'SELECT count(*) FROM "{ds}"'))
            reason = NOT_PROJECTED.get(ds, "the dataset stays in the NAV DuckDB")
            put(ds, {}, {f"not projected: {reason}": total}, rows=total)
    if inp.symbol_of_info:
        out["links"] = {"SYMBOL_OF": r["SYMBOL_OF"], **inp.symbol_of_info}
    if inp.property_term_info:
        out["property_terms"] = {"TABULATES": r["TABULATES"], "VALUE_OF": r["VALUE_OF"], **inp.property_term_info}
    return out


# ---------------------------------------------------------------- SYMBOL_OF (formula symbol ↔ term)
_MATCH_RANK = {"FULL": 0, "PREFIX": 1, "NEAR_START": 2}
MAX_TERMS_PER_DEFINITION = 2
NEAR_START_MAX_POS = 2


def _find(words: list[str], sub: list[str]) -> int:
    n = len(sub)
    for i in range(len(words) - n + 1):
        if words[i:i + n] == sub:
            return i
    return -1


def match_definition(full_key: str, candidate_keys: list[str], key_to_term: dict[str, str],
                     seeds: set[str]) -> list[tuple[str, str, str]]:
    """(term_id, key, match) of a definition whose phrase keys (the whole-span key first) are given."""
    words = full_key.split()
    hits = []
    for k in dict.fromkeys(candidate_keys):
        tid = key_to_term.get(k)
        if not tid:
            continue
        sub = k.split()
        pos = _find(words, sub)
        if k == full_key:
            match = "FULL"
        elif pos == 0:
            match = "PREFIX"
        elif 0 < pos <= NEAR_START_MAX_POS and len(sub) >= 2:
            match = "NEAR_START"
        else:
            continue
        if len(sub) == 1 and match != "FULL" and tid not in seeds:
            continue                                          # a single generic word («коэффициент», «function»)
        hits.append((pos if pos >= 0 else 0, -len(sub), k, tid, match))
    hits.sort()
    kept: list[tuple[int, int, str, str, str]] = []
    for h in hits:
        if any(_find(o[2].split(), h[2].split()) >= 0 for o in kept):
            continue                                          # nested in a longer kept match
        kept.append(h)
        if len(kept) >= MAX_TERMS_PER_DEFINITION:
            break
    return [(h[3], h[2], h[4]) for h in kept]


def _surface_keys(definition: str, max_words: int = 6) -> tuple[str, list[str]]:
    """Normalised word n-grams starting within the first three words (surface fallback); the whole text first."""
    from vkm_corpus.navigation.ids import norm_text

    words = norm_text(definition).split()
    full = " ".join(words)
    keys = [full] if full else []
    for pos in range(min(len(words), NEAR_START_MAX_POS + 1)):
        for n in range(min(max_words, len(words) - pos), 0, -1):
            keys.append(" ".join(words[pos:pos + n]))
    return full, keys


def term_index(inp: NavInput) -> dict[str, Any]:
    """Phrase → term matching shared by ``SYMBOL_OF`` and the property terms: the lemma keys of the builder's morphology
    (``terms.morphology``) when it can be loaded here, else normalised surface forms (``morphology = 'surface'``).
    ``keys_of(text)`` → (the key of the whole phrase, candidate keys); ``key_to_term`` → term id."""
    if inp._term_index is not None:
        return inp._term_index
    terms = inp.fetch("SELECT term_id, lemma, lemma_key, surface_forms, seed, morphology FROM terms")
    morph_name = Counter(t["morphology"] for t in terms if t.get("morphology")).most_common(1)
    morph_name = morph_name[0][0] if morph_name else None
    seeds = {t["term_id"] for t in terms if t.get("seed")}
    morph = None
    if inp.options.symbol_morphology != "surface" and morph_name in ("pymorphy3", "snowball", "crude"):
        try:
            from vkm_corpus.navigation.concepts import Morphology

            candidate = Morphology(morph_name)
            morph = candidate if candidate.name == morph_name else None
        except Exception:  # noqa: BLE001 — optional dependency (extra `navigation`) or numpy/pyarrow missing
            morph = None
    if morph is not None:
        from vkm_corpus.navigation.concepts import phrase_keys

        used = morph_name
        key_to_term = {t["lemma_key"]: t["term_id"] for t in sorted(terms, key=lambda t: t["term_id"])
                       if t.get("lemma_key")}

        def keys_of(text: str) -> tuple[str, list[str]]:
            ks = phrase_keys(text, morph)
            return (ks[0], ks) if ks else ("", [])
    else:
        from vkm_corpus.navigation.ids import norm_text

        used = "surface"
        key_to_term = {}
        for t in sorted(terms, key=lambda t: (-(1 if t.get("seed") else 0), t["term_id"])):
            for s in [t.get("lemma")] + list(t.get("surface_forms") or []):
                k = norm_text(s)
                if k:
                    key_to_term.setdefault(k, t["term_id"])
        keys_of = _surface_keys
    inp._term_index = {"morphology": used, "terms_morphology": morph_name, "seeds": seeds,
                       "key_to_term": key_to_term, "keys_of": keys_of}
    return inp._term_index


def derive_symbol_of(inp: NavInput) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``SYMBOL_OF`` rows (term_id, symbol_id, n_formulas, match, morphology, rule_version) and build info."""
    index = term_index(inp)
    used, morph_name, seeds = index["morphology"], index["terms_morphology"], index["seeds"]
    key_to_term, keys_of = index["key_to_term"], index["keys_of"]
    rule = N.RULE_SYMBOL_OF_SURFACE if used == "surface" else N.RULE_SYMBOL_OF
    defs = inp.fetch("SELECT symbol_id, formula_id, definition FROM formula_symbols "
                     "WHERE definition IS NOT NULL AND symbol_id IS NOT NULL ORDER BY symbol_id, formula_id")
    cache: dict[str, list[tuple[str, str, str]]] = {}
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    matched_rows = 0
    for d in defs:
        text = d["definition"]
        if text not in cache:
            full, keys = keys_of(text)
            cache[text] = match_definition(full, keys, key_to_term, seeds) if full else []
        hits = cache[text]
        if hits:
            matched_rows += 1
        for tid, _key, match in hits:
            a = agg.setdefault((tid, d["symbol_id"]), {"formulas": set(), "match": match})
            a["formulas"].add(d["formula_id"])
            if _MATCH_RANK[match] < _MATCH_RANK[a["match"]]:
                a["match"] = match
    rows = [{"term_id": tid, "symbol_id": sid, "n_formulas": len(a["formulas"]), "match": a["match"],
             "morphology": used, "rule_version": rule} for (tid, sid), a in sorted(agg.items())]
    by_match = Counter(r["match"] for r in rows)
    info = {"rule_version": rule, "morphology": used, "terms_morphology": morph_name,
            "definitions": len(defs), "distinct_definitions": len(cache), "definitions_matched": matched_rows,
            "symbols_linked": len({r["symbol_id"] for r in rows}), "edges_by_match": dict(sorted(by_match.items()))}
    return rows, info


_PARENS = re.compile(r"\s*\([^)]*\)")


def property_labels(key: str, dataset_label: str | None = None) -> list[tuple[str, str]]:
    """(basis, label) candidates of a property, in the order they are tried: the vocabulary's label_ru, label_en and
    synonyms (each first without its parenthetical remark), then the label printed in the dataset."""
    from vkm_corpus.navigation import parameters_vocab as V

    out: list[tuple[str, str]] = []

    def add(basis: str, text: str | None) -> None:
        text = " ".join((text or "").split())
        if text and text not in {t for _b, t in out}:
            out.append((basis, text))

    p = V.PROPERTY_BY_KEY.get(key)
    if p is not None:
        for basis, text in (("label_ru", p.label_ru), ("label_en", p.label_en),
                            *(("synonym", s) for s in p.synonyms)):
            add(basis, _PARENS.sub("", text))
            add(basis, text)
    add("dataset_label", _PARENS.sub("", dataset_label or ""))
    add("dataset_label", dataset_label)
    return out


def derive_property_terms(inp: NavInput) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Property key → term (``property_term_v1``): the first label (``property_labels``) whose whole lemma key is a
    term of the concept graph. A property without such a label gets no term (no partial or nested match)."""
    index = term_index(inp)
    used, key_to_term, keys_of = index["morphology"], index["key_to_term"], index["keys_of"]
    rule = N.RULE_PROPERTY_TERM_SURFACE if used == "surface" else N.RULE_PROPERTY_TERM
    labels: dict[str, str | None] = {}
    if inp.has("parameter_candidates"):
        for r in inp.fetch("SELECT property_key, min(property_label) AS label FROM parameter_candidates "
                           "WHERE property_key IS NOT NULL GROUP BY 1"):
            labels[r["property_key"]] = r["label"]
    if inp.has("table_structure"):
        for r in inp.fetch("SELECT DISTINCT unnest(property_keys) AS property_key FROM table_structure"):
            if r["property_key"]:
                labels.setdefault(r["property_key"], None)
    if inp.has("table_columns") and "property_label" in inp.columns.get("table_columns", set()):
        for r in inp.fetch("SELECT property_key, min(property_label) AS label FROM table_columns "
                           "WHERE property_key IS NOT NULL GROUP BY 1"):
            if not labels.get(r["property_key"]):
                labels[r["property_key"]] = r["label"]
    rows, unmapped, by_basis = [], [], Counter()
    for key in sorted(labels):
        hit = None
        for basis, text in property_labels(key, labels[key]):
            full, _keys = keys_of(text)
            tid = key_to_term.get(full) if full else None
            if tid:
                hit = (tid, basis, text)
                break
        if hit is None:
            unmapped.append(key)
            continue
        by_basis[hit[1]] += 1
        rows.append({"property_key": key, "term_id": hit[0], "label": hit[2], "basis": hit[1], "match": "FULL",
                     "morphology": used, "rule_version": rule})
    info = {"rule_version": rule, "morphology": used, "properties": len(labels), "properties_with_term": len(rows),
            "terms": len({r["term_id"] for r in rows}), "by_basis": dict(sorted(by_basis.items())),
            "properties_without_term": unmapped}
    return rows, info


# ---------------------------------------------------------------- preflight (static checks of the datasets)
def _cycles(pairs: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """(nodes on a cycle, nodes with several parents) of child → parent pairs."""
    parents: dict[str, list[str]] = defaultdict(list)
    for child, parent in pairs:
        parents[child].append(parent)
    multi = sorted(c for c, ps in parents.items() if len(set(ps)) > 1)
    on_cycle: set[str] = set()
    state: dict[str, int] = {}
    for start in sorted(parents):
        path: list[str] = []
        node = start
        while node is not None and state.get(node, 0) == 0:
            state[node] = 1
            path.append(node)
            ps = parents.get(node)
            node = ps[0] if ps else None
        if node is not None and state.get(node) == 1:
            on_cycle.update(path[path.index(node):])
        for p in path:
            state[p] = 2
    return sorted(on_cycle), multi


def tree_problems(pairs: list[tuple[str, str]]) -> list[str]:
    cyc, multi = _cycles(pairs)
    return [f"on a cycle: {x}" for x in cyc[:20]] + [f"several parents: {x}" for x in multi[:20]]


def preflight(inp: NavInput, expected: dict[str, Any], acct: dict[str, Any]) -> list[Any]:
    from vkm_corpus.graph.common import check

    res = []
    dups = []
    for node in N.NODE_TYPES:
        if node.label in expected["skipped"]:
            continue
        sql = node_sql(inp, node)
        d = int(inp.scalar(f"SELECT count(*) - count(DISTINCT id) FROM ({sql})"))
        nulls = int(inp.scalar(f"SELECT count(*) FROM ({sql}) WHERE id IS NULL"))
        if d or nulls:
            dups.append(f"{node.label}: {d} duplicate id(s), {nulls} null id(s)")
    res.append(check("P1", "NAV node ids are unique and not null", dups, code="E_DUPLICATE_ID"))
    problems = []
    if inp.has("sections"):
        n = int(inp.scalar("SELECT count(*) FROM sections WHERE parent_section_id IS NOT NULL AND parent_section_id "
                           "NOT IN (SELECT section_id FROM sections)"))
        if n:
            problems.append(f"sections with an unknown parent: {n}")
        n = int(inp.scalar("SELECT count(*) FROM sections WHERE page_start_index IS NULL OR page_end_index IS NULL "
                           "OR page_end_index < page_start_index OR page_start_id IS NULL OR page_end_id IS NULL"))
        if n:
            problems.append(f"sections with an invalid page range: {n}")
    if "NavTopic" not in expected["skipped"] and "parent" in inp.topic_map.get("topics", {}):
        m = inp.topic_map["topics"]
        n = int(inp.scalar(f'SELECT count(*) FROM topics WHERE "{m["parent"]}" IS NOT NULL AND "{m["parent"]}" NOT IN '
                           f'(SELECT "{m["id"]}" FROM topics)'))
        if n:
            problems.append(f"topics with an unknown parent: {n}")
    res.append(check("P2", "tree parents exist; section page ranges are valid", problems, code="E_PREFLIGHT"))
    cyc = []
    if inp.has("sections"):
        pairs = [(r["c"], r["p"]) for r in inp.fetch("SELECT section_id AS c, parent_section_id AS p FROM sections "
                                                      "WHERE parent_section_id IS NOT NULL")]
        cyc += [f"NavSection {x}" for x in tree_problems(pairs)]
    if "NAV_CHILD_OF:NavTopic" not in expected["skipped"] and inp.has("topics"):
        pairs = [(r["from_id"], r["to_id"]) for r in inp.fetch(_topic_rel_sql(inp, "NAV_CHILD_OF:NavTopic"))]
        cyc += [f"NavTopic {x}" for x in tree_problems(pairs)]
    res.append(check("P3", "NAV trees (sections, topics) are acyclic with one parent per node", cyc,
                     code="E_INVARIANT_VIOLATION"))
    no_pages = []
    if "COVERS_PAGE" not in expected["skipped"]:
        rows = inp.fetch(f"""SELECT s.section_id FROM sections s WHERE NOT EXISTS (
                               SELECT 1 FROM ({rel_sql(inp, N.REL_BY_NAME['COVERS_PAGE'])}) c
                               WHERE c.from_id = s.section_id) ORDER BY 1 LIMIT 20""")
        no_pages = [r["section_id"] for r in rows]
    res.append(check("P4", "every section covers at least one page of section_pages", no_pages, code="E_PREFLIGHT"))
    open_ = [f"{ds}: {a['rows']} rows, loaded+folded+skipped differ" for ds, a in acct.items()
             if isinstance(a, dict) and a.get("closed") is False]
    res.append(check("P5", "every dataset row is loaded, folded or skipped by a named rule", open_,
                     code="E_COUNT_MISMATCH"))
    ref_skips = []
    if inp.has("term_edges", "terms"):
        n = int(inp.scalar("SELECT count(*) FROM term_edges WHERE src_term_id NOT IN (SELECT term_id FROM terms) OR "
                           "(dst_term_id IS NOT NULL AND dst_term_id NOT IN (SELECT term_id FROM terms))"))
        if n:
            ref_skips.append(f"term_edges with an unknown term: {n}")
    if inp.has("formula_context", "sections"):
        n = int(inp.scalar("SELECT count(*) FROM formula_context WHERE section_id IS NOT NULL AND section_id NOT IN "
                           "(SELECT section_id FROM sections)"))
        if n:
            ref_skips.append(f"formula_context rows with an unknown section: {n}")
    if inp.has("term_mentions", "sections"):
        n = int(inp.scalar("SELECT count(*) FROM term_mentions WHERE section_id IS NOT NULL AND section_id NOT IN "
                           "(SELECT section_id FROM sections)"))
        if n:
            ref_skips.append(f"term_mentions rows with an unknown section: {n}")
    for ds in ("table_structure", "parameter_candidates"):
        if inp.has(ds, "sections"):
            n = int(inp.scalar(f"SELECT count(*) FROM {ds} WHERE section_id IS NOT NULL AND section_id NOT IN "
                               "(SELECT section_id FROM sections)"))
            if n:
                ref_skips.append(f"{ds} rows with an unknown section: {n}")
    if inp.has("parameter_candidates", "table_structure"):
        n = int(inp.scalar("SELECT count(*) FROM parameter_candidates WHERE table_id IS NOT NULL AND table_id NOT IN "
                           "(SELECT table_id FROM table_structure)"))
        if n:
            ref_skips.append(f"parameter_candidates rows of a table without a structured grid: {n}")
    if inp.has("object_dup_members", "object_dup_clusters"):
        n = int(inp.scalar("SELECT count(*) FROM object_dup_members WHERE cluster_id IS NULL OR cluster_id NOT IN "
                           "(SELECT cluster_id FROM object_dup_clusters)"))
        if n:
            ref_skips.append(f"object_dup_members rows of an unknown group: {n}")
    res.append(check("P6", "references between NAV datasets resolve (rows that do not are skipped)", ref_skips,
                     code="E_DANGLING_REFERENCE", warn_only=True))
    for ds, problem in sorted(inp.topic_problems.items()):
        res.append(check(f"P7:{ds}", "topic dataset columns recognised (the part is skipped otherwise)", [problem],
                         code="E_CANON_MAPPING", warn_only=True))
    return res


# ---------------------------------------------------------------- offline resolution against the canonical DuckDB
DOC_TABLES: dict[str, tuple[str, str]] = {"Page": ("pages", "page_id"), "Block": ("blocks", "object_id"),
                                          "Formula": ("formulas", "object_id"), "Source": ("sources", "source_id"),
                                          "Table": ("tables", "object_id"), "Figure": ("figures", "object_id")}


def document_references(inp: NavInput, canon_duckdb: str | Path) -> dict[str, Any]:
    """Dry-run form of check N1: DOCUMENT ids used by cross-layer edges that the canonical DuckDB does not hold."""
    path = Path(canon_duckdb)
    inp.con.execute(f"ATTACH '{_quote(path)}' AS canon_ref (READ_ONLY)")
    try:
        snap = None
        try:
            snap = inp.scalar("SELECT snapshot_id FROM canon_ref.meta.snapshot")
        except Exception:  # noqa: BLE001
            snap = None
        out: dict[str, Any] = {"canonical_snapshot_id": snap, "matches_nav_snapshot": snap == inp.snapshot_id,
                               "missing": {}}
        skipped = inp.skipped_types()
        for rel in N.REL_TYPES:
            if not rel.cross_layer or rel.name in skipped:
                continue
            for side, label in (("from_id", rel.start), ("to_id", rel.end)):
                if label not in DOC_TABLES:
                    continue
                table, col = DOC_TABLES[label]
                sql = (f"SELECT count(*) AS n, list({side} ORDER BY {side})[1:5] AS examples FROM "
                       f"(SELECT DISTINCT {side} FROM ({rel_sql(inp, rel)})) x WHERE {side} NOT IN "
                       f"(SELECT {col} FROM canon_ref.canonical.{table})")
                row = inp.fetch(sql)[0]
                out["missing"][f"{rel.name}.{label}"] = {"n": int(row["n"]), "examples": row["examples"] or []}
        out["all_resolved"] = all(v["n"] == 0 for v in out["missing"].values())
        return out
    finally:
        inp.con.execute("DETACH canon_ref")
