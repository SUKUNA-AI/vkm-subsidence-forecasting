"""Bounded, canonical-backed table continuation projection; never evidence.

Only explicit ``continues_object_id`` edges are followed. Canonical physical
objects and NAV IDs are retained; no value, unit or source record is rewritten.
The existing structured-table consumer exposes this projection separately.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Literal, Mapping

from pydantic import Field, ValidationError

from vkm_evidence.contracts import ObjectRef, Sha256, StrictModel, record_hash
from vkm_evidence.objects import canonical_locator
from vkm_corpus.navigation import ids as nav_ids
from vkm_corpus.contracts.models import TableContinuationProvenance

RULE = "table_continuations_v1"
CURSOR_PREFIX = "lt1:"
REVIEW = "AUTO_EXTRACTED_UNREVIEWED"
HEAD_ROLES = frozenset({"HEADER", "TITLE", "UNITS", "NUMBERING_HEAD", "AXIS_TITLE", "AXIS"})
DATA_ROLES = frozenset({"DATA", "STAT_MEAN", "STAT_MIN", "STAT_MAX", "STAT_MEDIAN", "STAT_DISPERSION", "STAT_COUNT"})
UNSAFE_FLAGS = frozenset({"TRUNCATED", "REPETITION", "EMPTY_ON_INK", "TABLE_STRUCTURE_UNCERTAIN",
    "OVERLAPPING_CELLS", "BAND_COLUMNS_MISMATCH", "BANDS_UNRESOLVED", "TOO_LARGE", "UNIT_CONFLICT"})
META_FIELDS = ("object_id", "source_id", "source_sha256", "page_id", "content_sha256", "extraction_signature",
    "extraction_generation", "continues_object_id", "raw_locator", "raw_artifact_id", "quality_flags", "n_rows", "n_cols", "caption")


class ContinuationLimits(StrictModel):
    max_fragments: int = Field(default=64, ge=1, le=512)
    max_cells: int = Field(default=100_000, ge=1, le=1_000_000)
    max_canonical_bytes: int = Field(default=16 * 1024**2, ge=1, le=128 * 1024**2)
    max_navigation_bytes: int = Field(default=16 * 1024**2, ge=1, le=128 * 1024**2)
    max_grid_positions: int = Field(default=100_000, ge=1, le=1_000_000)
    max_output_cells: int = Field(default=4096, ge=1, le=100_000)
    max_output_bytes: int = Field(default=2 * 1024**2, ge=1024, le=16 * 1024**2)


class CellOccurrence(StrictModel):
    """Canonical cell position; native bbox/character offsets are not invented."""
    table: ObjectRef
    cell_id: str
    canonical_pointer: str
    cell_sha256: Sha256
    physical_row: int = Field(ge=0)
    col: int = Field(ge=0)
    row_span: int = Field(ge=1)
    col_span: int = Field(ge=1)


class LogicalCursor(StrictModel):
    version: Literal[1] = 1
    anchor_table_id: str
    chain_sha256: Sha256
    after: int = Field(ge=0)


class ContinuationBlocked(ValueError):
    pass


def _rows(con, sql, params=()):
    cur = con.execute(sql, list(params))
    fields = [d[0] for d in cur.description]
    return [dict(zip(fields, r)) for r in cur.fetchall()]


def _name(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("unsafe continuation dataset name")
    return '"' + value + '"'


def _unavailable(reason):
    return {"rule_version": RULE, "status": "NOT_AVAILABLE", "reasons": [reason],
        "review_status": REVIEW, "scientific_admission": "NOT_ESTABLISHED", "rows": [],
        "pagination": {"has_more": False, "next_cursor": None, "complete": False}}


def _snapshot(con):
    try:
        nav = _rows(con, "SELECT meta_json FROM nav_meta")
    except Exception:
        return None
    if len(nav) != 1:
        raise ContinuationBlocked("AMBIGUOUS_NAV_IDENTITY")
    try:
        meta = _unique_json(nav[0]["meta_json"])
        if not isinstance(meta, dict):
            raise ValueError("invalid NAV identity")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ContinuationBlocked("INVALID_NAV_IDENTITY") from exc
    canonical = None
    for view in ("canon.meta.snapshot", "meta.snapshot"):
        try:
            canonical = _rows(con, f"SELECT snapshot_id, manifest_sha256 FROM {view}")
        except Exception:  # absent attachment is explicit NOT_AVAILABLE below
            continue
        break
    if canonical is None:
        return None
    origin = meta.get("snapshot") if isinstance(meta.get("snapshot"), dict) else meta
    if (len(canonical) != 1 or not isinstance(meta.get("snapshot_id"), str)
            or meta.get("snapshot_id") != canonical[0]["snapshot_id"]
            or origin.get("snapshot_id") != canonical[0]["snapshot_id"]
            or origin.get("manifest_sha256") != canonical[0]["manifest_sha256"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(canonical[0]["manifest_sha256"]))):
        raise ContinuationBlocked("CANONICAL_NAV_GENERATION_MISMATCH")
    return {**canonical[0], "nav_identity_status": meta.get("identity_status", "UNVERIFIED"),
        "nav_manifest_sha256": meta.get("manifest_sha256"), "nav_rule_versions": meta.get("rule_versions")}


def _unique_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate logical cursor field")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def _cursor(raw, anchor, digest):
    try:
        if len(raw) > 2048 or not raw.startswith(CURSOR_PREFIX):
            raise ValueError("invalid logical cursor")
        value = raw[len(CURSOR_PREFIX):]
        token = LogicalCursor.model_validate(_unique_json(base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)))
        if token.anchor_table_id != anchor or token.chain_sha256 != digest:
            raise ValueError("logical cursor is stale or names another table chain")
        return token.after
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("invalid or stale logical table cursor") from exc


def _encode_cursor(anchor, digest, after):
    raw = json.dumps(LogicalCursor(anchor_table_id=anchor, chain_sha256=digest, after=after).model_dump(),
        sort_keys=True, separators=(",", ":")).encode()
    return CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _meta(con, table_id, source_id, source_sha=None):
    # Reject cross-source ID before reading even that other table's metadata.
    if not isinstance(table_id, str) or table_id.split(":", 1)[0] != source_id:
        raise ContinuationBlocked("CROSS_SOURCE_CONTINUATION")
    identities = _rows(con, "SELECT object_id,source_id,source_sha256 FROM canonical.tables WHERE object_id=?", [table_id])
    if len(identities) != 1:
        raise ContinuationBlocked("MISSING_OR_DUPLICATE_FRAGMENT")
    identity = identities[0]
    if identity["source_id"] != source_id or source_sha is not None and identity["source_sha256"] != source_sha:
        raise ContinuationBlocked("SOURCE_VERSION_MISMATCH")
    # Only identity fields are fetched before authorization/version checks.
    fields = {r[0] for r in con.execute("DESCRIBE canonical.tables").fetchall()}
    optional = [f"t.{c}" if c in fields else f"NULL AS {c}"
        for c in ("continuation_provenance", "raw_artifacts", "origin")]
    values = _rows(con, "SELECT " + ",".join("t." + c for c in META_FIELDS)
        + "," + ",".join(optional)
        + ",p.page_index FROM canonical.tables t LEFT JOIN canonical.pages p ON p.page_id=t.page_id WHERE t.object_id=?",
        [table_id])
    if len(values) != 1:
        raise ContinuationBlocked("MISSING_OR_DUPLICATE_FRAGMENT")
    value = values[0]
    if (value["source_id"] != source_id or source_sha is not None and value["source_sha256"] != source_sha):
        raise ContinuationBlocked("SOURCE_VERSION_MISMATCH")
    if not isinstance(value["page_index"], int) or value["page_index"] < 1:
        raise ContinuationBlocked("UNKNOWN_PHYSICAL_PAGE")
    return value


def _chain(con, anchor, source_id, limits):
    first = _meta(con, anchor, source_id)
    seen = {anchor}
    while True:
        previous = _rows(con, "SELECT object_id,source_id,source_sha256 FROM canonical.tables WHERE continues_object_id=? LIMIT 2",
            [first["object_id"]])
        if not previous:
            break
        if len(previous) != 1:
            raise ContinuationBlocked("AMBIGUOUS_PREDECESSOR")
        identifier = previous[0]["object_id"]
        if identifier in seen or len(seen) >= limits.max_fragments:
            raise ContinuationBlocked("CYCLIC_OR_OVERSIZED_CHAIN")
        first = _meta(con, identifier, source_id, first["source_sha256"])
        seen.add(identifier)
    chain, seen, current = [], set(), first
    while True:
        if current["object_id"] in seen or len(chain) >= limits.max_fragments:
            raise ContinuationBlocked("CYCLIC_OR_OVERSIZED_CHAIN")
        if current["extraction_generation"] != first["extraction_generation"]:
            raise ContinuationBlocked("EXTRACTION_GENERATION_MISMATCH")
        chain.append(current)
        seen.add(current["object_id"])
        next_id = current["continues_object_id"]
        if next_id is None:
            break
        next_meta = _meta(con, next_id, source_id, first["source_sha256"])
        if next_meta["page_index"] != current["page_index"] + 1:
            raise ContinuationBlocked("NONADJACENT_OR_REVERSED_CONTINUATION")
        incoming = _rows(con, "SELECT object_id FROM canonical.tables WHERE continues_object_id=? LIMIT 2", [next_id])
        if len(incoming) != 1 or incoming[0]["object_id"] != current["object_id"]:
            raise ContinuationBlocked("AMBIGUOUS_PREDECESSOR")
        current = next_meta
    return chain


def _require_link_provenance(con, chain, limits):
    for index, meta in enumerate(chain):
        proof = meta.get("continuation_provenance")
        if proof is None:
            continue  # exact historical edge, explicitly LEGACY_UNREVIEWED
        proof = TableContinuationProvenance.model_validate(proof)
        if index + 1 == len(chain):
            raise ContinuationBlocked("ORPHANED_CONTINUATION_DECLARATION")
        target, candidate = chain[index + 1], proof.candidate
        registered = {meta.get("raw_artifact_id")} | {r["artifact_id"] for r in meta.get("raw_artifacts") or []}
        raw_hash = con.execute("SELECT CASE WHEN octet_length(encode(raw_output))<=? THEN sha256(raw_output) ELSE NULL END "
            "FROM canonical.tables WHERE object_id=?", [limits.max_canonical_bytes, target["object_id"]]).fetchone()[0]
        if (candidate.source_sha256 != meta["source_sha256"] or proof.target_object_id != target["object_id"]
                or meta["continues_object_id"] != proof.target_object_id
                or candidate.target_page_index != target["page_index"]
                or candidate.target_raw_locator != target["raw_locator"]
                or candidate.target_raw_content_sha256 != raw_hash
                or proof.target_extraction_generation != target["extraction_generation"]
                or candidate.declaration_artifact_id not in registered
                or candidate.basis == "NATIVE_SOURCE_RELATION" and meta.get("origin") != "NATIVE"):
            raise ContinuationBlocked("CONTINUATION_DECLARATION_IDENTITY_MISMATCH")


def _fragment(con, meta, snapshot, tables, limits, remaining_cells, remaining_bytes, remaining_navigation, remaining_grid):
    tid = meta["object_id"]
    if (not isinstance(meta["n_rows"], int) or not isinstance(meta["n_cols"], int)
            or meta["n_rows"] < 0 or meta["n_cols"] < 1
            or meta["n_rows"] * meta["n_cols"] > remaining_grid):
        raise ContinuationBlocked("CANONICAL_GRID_BUDGET_EXCEEDED")
    # Enforce limits in SQL before fetching source cell texts into Python.
    size = con.execute("SELECT array_length(cells),octet_length(encode(to_json(cells))) FROM canonical.tables WHERE object_id=?",
        [tid]).fetchone()
    if size is None or size[0] is None or size[0] > remaining_cells or size[1] > remaining_bytes:
        raise ContinuationBlocked("CANONICAL_CELL_BUDGET_EXCEEDED")
    raw = con.execute("SELECT cells FROM canonical.tables WHERE object_id=?", [tid]).fetchone()[0]
    navigation_bytes = 0
    for name, max_count in (("table_structure", 1), ("table_cells", len(raw)), ("table_columns", meta["n_cols"])):
        nav_size = con.execute(f"SELECT count(*),coalesce(sum(octet_length(encode(to_json(n)))),0) "
            f"FROM {_name(tables[name])} n WHERE table_id=?", [tid]).fetchone()
        navigation_bytes += nav_size[1]
        if nav_size[0] > max_count or navigation_bytes > remaining_navigation:
            raise ContinuationBlocked("NAVIGATION_FRAGMENT_BUDGET_EXCEEDED")
    structures = _rows(con, f"SELECT * FROM {_name(tables['table_structure'])} WHERE table_id=?", [tid])
    if len(structures) != 1:
        raise ContinuationBlocked("MISSING_OR_DUPLICATE_NAV_FRAGMENT")
    structure = structures[0]
    cells = _rows(con, f"SELECT * FROM {_name(tables['table_cells'])} WHERE table_id=? ORDER BY \"row\",col,cell_id", [tid])
    columns = _rows(con, f"SELECT * FROM {_name(tables['table_columns'])} WHERE table_id=? ORDER BY block,col", [tid])
    if (structure["source_id"] != meta["source_id"] or structure["page_id"] != meta["page_id"]
            or structure.get("nav_table_id") != nav_ids.table_id(tid)
            or structure["n_rows"] != meta["n_rows"] or structure["n_cols"] != meta["n_cols"]
            or len(cells) != len(raw) or structure.get("n_blocks") != 1
            or not structure.get("structure_ok")
            or UNSAFE_FLAGS & set((meta.get("quality_flags") or []) + (structure.get("quality_flags") or []))):
        raise ContinuationBlocked("UNSAFE_OR_STALE_FRAGMENT_STRUCTURE")
    occurrence = ObjectRef(source_id=meta["source_id"], source_sha256=meta["source_sha256"],
        snapshot_id=snapshot["snapshot_id"], object_id=tid, object_version=meta["extraction_signature"],
        content_sha256=meta["content_sha256"], locator=canonical_locator(meta),
        extraction_generation=str(meta["extraction_generation"]))
    by_position, refs, occupied = {}, {}, set()
    for index, cell in enumerate(raw):
        key = (cell["row"], cell["col"])
        if key in by_position or cell["row_span"] < 1 or cell["col_span"] < 1:
            raise ContinuationBlocked("DUPLICATE_OR_INVALID_CELL")
        by_position[key] = cell
        for r in range(cell["row"], cell["row"] + cell["row_span"]):
            for c in range(cell["col"], cell["col"] + cell["col_span"]):
                if r < 0 or c < 0 or r >= meta["n_rows"] or c >= meta["n_cols"] or (r, c) in occupied:
                    raise ContinuationBlocked("OVERLAPPING_OR_OUT_OF_BOUNDS_CELL")
                occupied.add((r, c))
        refs[key] = CellOccurrence(table=occurrence, cell_id="", canonical_pointer=f"/cells/{index}",
            cell_sha256=record_hash(cell), physical_row=cell["row"], col=cell["col"],
            row_span=cell["row_span"], col_span=cell["col_span"])
    nav_positions = set()
    for cell in cells:
        key = (cell["row"], cell["col"])
        original = by_position.get(key)
        if (key in nav_positions or original is None
                or any(cell.get(k) != original.get(k) for k in ("text", "row_span", "col_span"))
                or cell.get("cell_id") != nav_ids.table_cell_id(nav_ids.table_id(tid), cell["row"], cell["col"])
                or cell.get("source_id") != meta["source_id"] or cell.get("page_id") != meta["page_id"]
                or UNSAFE_FLAGS & set(cell.get("flags") or [])):
            raise ContinuationBlocked("STALE_OR_AMBIGUOUS_NAV_CELL")
        nav_positions.add(key)
        refs[key] = refs[key].model_copy(update={"cell_id": cell["cell_id"]})
    return {"meta": meta, "occurrence": occurrence.model_dump(), "structure": structure,
        "cells": cells, "canonical_cells_sha256": record_hash(raw), "original": by_position, "refs": refs,
        "columns": columns}, size[0], size[1], navigation_bytes


def _headers(fragment):
    cells = [c for c in fragment["cells"] if c["row_role"] in HEAD_ROLES]
    # Exact printed header (including raw units and spans); no fuzzy equality.
    signature = tuple((c["row"], c["col"], c["row_span"], c["col_span"], c["text"]) for c in cells)
    return signature, cells


def _require_declared_hints(fragments):
    """A printed hint is only a warning, never an inferred continuation edge."""
    backward = re.compile(r"(?:продолжени[ея]\s+(?:табл\.?|таблиц[ыа])|continu(?:ation\s+of\s+table|ed\s+table)|table\s+\S+\s*\(continued\))", re.I)
    forward = re.compile(r"(?:продолжение\s+(?:на\s+)?следующ(?:ей|ую)\s+страниц[еу]|continued\s+(?:on\s+)?(?:the\s+)?next\s+page)", re.I)
    def printed(fragment):
        return [fragment["meta"].get("caption") or ""] + [c.get("text") or "" for c in fragment["cells"]]
    if any(backward.search(text) for text in printed(fragments[0])):
        raise ContinuationBlocked("UNDECLARED_PREDECESSOR_HINT")
    if any(forward.search(text) for text in printed(fragments[-1])):
        raise ContinuationBlocked("UNDECLARED_SUCCESSOR_HINT")


def _logical_rows(fragments):
    first = fragments[0]
    header_signature, headers = _headers(first)
    if len(fragments) > 1 and not headers:
        raise ContinuationBlocked("FIRST_FRAGMENT_HEADER_UNKNOWN")
    header_refs = defaultdict(list)
    for cell in headers:
        ref = first["refs"][(cell["row"], cell["col"])]
        for col in range(cell["col"], cell["col"] + cell["col_span"]):
            header_refs[col].append(ref)
    column_units = {c["col"]: c.get("unit_canonical") for c in first["columns"]}
    rows, note_count, physical_count, data_count = [], 0, 0, 0
    for index, fragment in enumerate(fragments):
        if fragment["meta"]["n_cols"] != first["meta"]["n_cols"]:
            raise ContinuationBlocked("CONTINUATION_WIDTH_MISMATCH")
        signature, local_headers = _headers(fragment)
        if index and signature and signature != header_signature:
            raise ContinuationBlocked("CONTINUATION_HEADER_OR_UNIT_MISMATCH")
        local_refs = defaultdict(list)
        for cell in local_headers:
            ref = fragment["refs"][(cell["row"], cell["col"])]
            for col in range(cell["col"], cell["col"] + cell["col_span"]):
                local_refs[col].append(ref)
        by_row = defaultdict(list)
        for cell in fragment["cells"]:
            if index and not local_headers and cell.get("unit_canonical") is not None:
                inherited = column_units.get(cell["col"])
                if inherited is not None and inherited != cell["unit_canonical"]:
                    raise ContinuationBlocked("CONTINUATION_LOCAL_UNIT_CONFLICT")
            by_row[cell["row"]].append(cell)
        for physical in range(fragment["meta"]["n_rows"]):
            cells = by_row[physical]
            roles = {c["row_role"] for c in cells}
            if len(roles) > 1:
                raise ContinuationBlocked("AMBIGUOUS_ROW_ROLE")
            role = next(iter(roles), "EMPTY")
            logical_role = "REPEATED_HEADER" if index and role in HEAD_ROLES else role
            note_marker = (role in {"GROUP", "TEXT", "NOTE"} and cells and re.match(
                r"^\s*(?:Примечани[ея]|Notes?)(?:\s*\d+)?\s*[:.]", cells[0].get("text") or "", re.I))
            if note_marker:
                logical_role = "NOTE"
            row = {"logical_row": physical_count, "table_id": fragment["meta"]["object_id"],
                "page_id": fragment["meta"]["page_id"], "physical_row": physical,
                "original_nav_role": role, "role": logical_role,
                "role_basis": "PRINTED_NOTE_MARKER" if note_marker else "EXISTING_NAV_UNREVIEWED",
                "data_ordinal": None, "_cells": cells, "_fragment": fragment,
                "_headers": local_refs if local_headers else header_refs,
                "_inherited": bool(index and not local_headers)}
            if role in DATA_ROLES:
                row["data_ordinal"] = data_count
                data_count += 1
            if logical_role == "NOTE":
                note_count += len(cells)
            rows.append(row)
            physical_count += 1
    return rows, note_count, data_count


def _materialize(row):
    """Expand version-pinned cell/header contexts only for the bounded page."""
    fragment = row["_fragment"]
    out = {k: v for k, v in row.items() if not k.startswith("_")}
    out["cells"] = []
    for cell in row["_cells"]:
        ref = fragment["refs"][(cell["row"], cell["col"])].model_dump()
        head = row["_headers"].get(cell["col"], []) if row["original_nav_role"] not in HEAD_ROLES else []
        units = []
        if cell.get("unit_source") == "CELL":
            units = [{"kind": "CELL", "occurrence": ref}]
        elif cell.get("unit_source") in {"HEADER", "UNITS_ROW", "COLUMN_HEADER"}:
            units = [{"kind": "CELL", "occurrence": h.model_dump()} for h in head]
        elif cell.get("unit_source") == "CAPTION" and fragment["meta"].get("caption"):
            units = [{"kind": "TABLE_FIELD", "table": fragment["occurrence"], "canonical_pointer": "/caption",
                "field_sha256": record_hash(fragment["meta"]["caption"])}]
        out["cells"].append({"occurrence": ref, "text_printed": cell["text"],
            "header_occurrences": [h.model_dump() for h in head],
            "header_binding": ("EXPLICIT_CONTINUATION_UNREVIEWED" if row["_inherited"] and head
                else "LOCAL_HEADER_UNREVIEWED" if head else "UNKNOWN"),
            "unit_occurrences": units, "unit_binding": "SOURCE_CONTEXT_UNREVIEWED" if units else "UNKNOWN",
            "value_interpretation": {k: cell.get(k) for k in ("value_type", "value_text", "value_min", "value_max",
                "unit_raw", "unit_canonical", "unit_source", "flags")}})
    return out


def get_table_continuation(con: Any, anchor_table_id: str, source_id: str, *, max_rows=200,
        cursor: str | None = None, tables: Mapping[str, str] | None = None,
        limits: ContinuationLimits | None = None) -> dict[str, Any]:
    """Read a complete explicit chain, return one bounded page of row references.

    The caller has already authorized the anchor source. A different source is
    rejected before reading its cell texts; no additional source is authorized.
    NOT_AVAILABLE describes older schemas; corrupt/ambiguous chains are BLOCKED.
    """
    limits = limits or ContinuationLimits()
    tables = {name: name for name in ("table_structure", "table_cells", "table_columns")} | dict(tables or {})
    try:
        fields = {r[0] for r in con.execute("DESCRIBE canonical.tables").fetchall()}
    except Exception:
        if cursor:
            raise ValueError("logical cursor canonical source is unavailable")
        return _unavailable("CANONICAL_TABLES_NOT_ATTACHED")
    if not (set(META_FIELDS) | {"cells"}) <= fields:
        if cursor:
            raise ValueError("logical cursor source schema changed")
        return _unavailable("CANONICAL_OCCURRENCE_METADATA_MISSING")
    try:
        snapshot = _snapshot(con)
        if snapshot is None:
            if cursor:
                raise ValueError("logical cursor snapshot is unavailable")
            return _unavailable("EXACT_CANONICAL_SNAPSHOT_NOT_AVAILABLE")
        chain = _chain(con, anchor_table_id, source_id, limits)
        _require_link_provenance(con, chain, limits)
        fragments, remaining_cells, remaining_bytes = [], limits.max_cells, limits.max_canonical_bytes
        remaining_navigation, remaining_grid = limits.max_navigation_bytes, limits.max_grid_positions
        for meta in chain:
            fragment, cells, byte_count, nav_bytes = _fragment(con, meta, snapshot, tables, limits,
                remaining_cells, remaining_bytes, remaining_navigation, remaining_grid)
            fragments.append(fragment)
            remaining_cells -= cells
            remaining_bytes -= byte_count
            remaining_navigation -= nav_bytes
            remaining_grid -= meta["n_rows"] * meta["n_cols"]
        _require_declared_hints(fragments)
        logical, note_count, data_count = _logical_rows(fragments)
        fingerprint = record_hash({"rule": RULE, "snapshot": snapshot,
            "fragments": [{k: f[k] for k in ("meta", "occurrence", "structure", "cells", "columns", "canonical_cells_sha256")}
                for f in fragments]})
        after = _cursor(cursor, anchor_table_id, fingerprint) if cursor else -1
        if after >= len(logical):
            raise ValueError("logical table cursor is outside the chain")
        page, cell_count, byte_count = [], 0, 0
        for descriptor in logical[after + 1:]:
            row = _materialize(descriptor)
            size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
            if (len(page) >= max(1, min(500, int(max_rows))) or cell_count + len(row["cells"]) > limits.max_output_cells
                    or byte_count + size > limits.max_output_bytes):
                if not page:
                    raise ContinuationBlocked("SINGLE_ROW_OUTPUT_BUDGET_EXCEEDED")
                break
            page.append(row)
            cell_count += len(row["cells"])
            byte_count += size
        last = page[-1]["logical_row"] if page else after
        more = last < len(logical) - 1
        return {"rule_version": RULE, "status": "STRUCTURALLY_LINKED_UNREVIEWED" if len(fragments) > 1 else "SINGLE_FRAGMENT_UNREVIEWED",
            "review_status": REVIEW, "scientific_admission": "NOT_ESTABLISHED", "original_read_verification": "NOT_RUN",
            "chain_sha256": fingerprint, "snapshot": snapshot, "anchor_table_id": anchor_table_id,
            "fragment_occurrences": [f["occurrence"] for f in fragments], "physical_row_count": len(logical),
            "continuation_declarations": [{"table": f["occurrence"], "canonical_pointer": "/continuation_provenance",
                "status": "SOURCE_DECLARED_UNREVIEWED" if f["meta"].get("continuation_provenance") else "LEGACY_UNREVIEWED",
                "provenance": f["meta"].get("continuation_provenance")}
                for f in fragments if f["meta"].get("continues_object_id")],
            "data_row_count": data_count, "data_count_basis": "EXISTING_NAV_UNREVIEWED_ROW_ROLE",
            "physical_cell_count": sum(len(f["cells"]) for f in fragments),
            "note_occurrence_count": note_count,
            "note_occurrences": [c["occurrence"] for row in page if row["role"] == "NOTE" for c in row["cells"]],
            "rows": page, "reasons": [],
            "pagination": {"has_more": more, "next_cursor": _encode_cursor(anchor_table_id, fingerprint, last) if more else None,
                "complete": not more and after == -1, "cursor_scope": "LOGICAL_ROW_CHAIN", "after": after}}
    except (ContinuationBlocked, TypeError, KeyError, ValidationError) as exc:
        if cursor:
            raise ValueError("logical cursor chain is unavailable or changed") from exc
        result = _unavailable(str(exc) if isinstance(exc, ContinuationBlocked) else "INVALID_CANONICAL_OCCURRENCE")
        result["status"] = "BLOCKED_UNDECLARED" if str(exc).startswith("UNDECLARED_") else "BLOCKED"
        return result
