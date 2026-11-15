"""Collation-independent persisted-row integrity tests.

Rows, references and batch metadata are synthetic strings built in this file. No
database, DSN, server locale, private fixture or scientific identifier is used.
The fake store only proves comparator behaviour: order must be irrelevant while
identity cardinality and all twelve persisted fields stay exact.
"""
from __future__ import annotations

import copy
import importlib.util
import random
import struct
from itertools import permutations
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "db_adapter_model_under_test", ROOT / "src/vkm_geometry" / "db_adapter.py")
adapter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(adapter)

BATCH = "SYNTHETIC-MODEL-ORDER-01"
WKB = (b"\x01" + struct.pack("<I2d", 1, 12.5, -3.25)).hex()
# Python code-point order differs from locale/case-insensitive order here.
IDS = ("S:B:02", "S:a:01", "S:\u0410:01", "S:\u0430:01")


def make_row(fid, *, source="SRC:A", work="WORK:01"):
    row = {
        "batch_id": BATCH,
        "source_feature_id": fid,
        "source_id": source,
        "work_id": work,
        "frame_id": "FRAME:base",
        "coordinate_unit": "pixel",
        "geometry_type": "Point",
        "wkb_hex": WKB,
        "feature_json": adapter.canonical({"source_feature_id": fid}),
        "matches_json": adapter.canonical([]),
        "batch_provenance_json": adapter.canonical({"fixture": "SYNTHETIC_MODEL_ONLY"}),
    }
    row["row_sha256"] = adapter.digest(row)
    assert set(row) == set(adapter.COLUMNS)
    return row


def make_rows(fids):
    return [make_row(fid, source="SRC:" + fid) for fid in fids]


def make_plan(rows, *, refs=None, metadata_json=None):
    rows = copy.deepcopy(rows)
    if refs is None:
        pairs = {}
        for row in rows:
            pair = (row["source_id"], row["work_id"])
            pairs[pair] = adapter.canonical({"source": pair[0], "work": pair[1]})
        refs = [{"source_id": pair[0], "work_id": pair[1], "reference_json": pairs[pair]}
                for pair in sorted(pairs)]
    return {
        "batch_id": BATCH,
        "content_digest": "a" * 64,
        "extraction_version": "MODEL-ORDER-01",
        "registry_sha256": "b" * 64,
        "batch_json": adapter.canonical({"fixture": "SYNTHETIC_MODEL_ONLY"})
        if metadata_json is None else metadata_json,
        "rows": sorted(rows, key=lambda row: row["source_feature_id"]),
        "source_refs": copy.deepcopy(refs),
    }


def as_tuples(rows):
    return [tuple(row[column] for column in adapter.COLUMNS) for row in rows]


class Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return copy.deepcopy(self._rows)

    def fetchone(self):
        return copy.deepcopy(self._rows[0])


class FakeStore:
    """Stand-in for one engine's three persisted query groups."""

    def __init__(self, plan, *, row_order=None, ref_order=None, rows=None, refs=None,
                 metadata=None, native_ok=True):
        if rows is None:
            rows = as_tuples(plan["rows"])
        if refs is None:
            refs = [(ref["source_id"], ref["work_id"], ref["reference_json"])
                    for ref in plan["source_refs"]]
        if metadata is None:
            metadata = [(plan["content_digest"], plan["extraction_version"],
                         plan["registry_sha256"], plan["batch_json"])]
        rows, refs = list(rows), list(refs)
        if row_order is not None:
            rows = [rows[index] for index in row_order]
        if ref_order is not None:
            refs = [refs[index] for index in ref_order]
        self.rows, self.refs, self.metadata = rows, refs, list(metadata)
        self.native_ok = native_ok
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append(sql)
        if sql.startswith("SELECT content_digest,"):
            return Cursor(self.metadata)
        if sql.startswith("SELECT " + ",".join(adapter.COLUMNS) + " FROM "):
            return Cursor(self.rows)
        if sql.startswith("SELECT source_id,work_id,reference_json "):
            return Cursor(self.refs)
        if sql.startswith("SELECT bool_and("):
            return Cursor([(self.native_ok,)])
        if sql == "LOAD spatial":
            return Cursor([])
        raise AssertionError("unexpected synthetic SQL: " + sql)


@pytest.mark.parametrize("pg", [True, False])
def test_every_storage_permutation_returns_one_canonical_order(pg):
    plan = make_plan(make_rows(IDS))
    baseline = adapter._assert_rows(FakeStore(plan), pg, plan)
    assert baseline == plan["rows"]
    for order in permutations(range(len(IDS))):
        store = FakeStore(plan, row_order=order, ref_order=order)
        assert adapter._assert_rows(store, pg, plan) == baseline


def test_canonical_order_is_code_point_not_case_or_locale():
    plan = make_plan(make_rows(reversed(IDS)))
    ids = [row["source_feature_id"]
           for row in adapter._assert_rows(FakeStore(plan, row_order=(3, 2, 1, 0)), True, plan)]
    assert ids == sorted(IDS)


@pytest.mark.parametrize("seed", range(4))
def test_shuffled_plan_and_store_order_is_irrelevant(seed):
    plan = make_plan(make_rows(IDS))
    rng = random.Random(seed)
    rows = copy.deepcopy(plan["rows"])
    refs = copy.deepcopy(plan["source_refs"])
    rng.shuffle(rows)
    rng.shuffle(refs)
    shuffled = {**plan, "rows": rows, "source_refs": refs}
    assert adapter._assert_rows(FakeStore(shuffled), False, shuffled) == plan["rows"]


def test_comparator_does_not_mutate_supplied_plan():
    plan = make_plan(make_rows(IDS))
    snapshot = copy.deepcopy(plan)
    adapter._assert_rows(FakeStore(plan, row_order=(3, 2, 1, 0)), True, plan)
    assert plan == snapshot


@pytest.mark.parametrize("pg", [True, False])
def test_duplicated_identity_is_rejected_not_merged(pg):
    plan = make_plan([make_row("S:dup:01", source="SRC:ONE")])
    stored = as_tuples(plan["rows"]) + as_tuples([make_row("S:dup:01", source="SRC:TWO")])
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, rows=stored), pg, plan)


def test_exact_duplicate_row_is_rejected_on_both_sides():
    plan = make_plan([make_row("S:dup:02", source="SRC:ONE")])
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, rows=as_tuples(plan["rows"]) * 2), True, plan)
    duplicated = copy.deepcopy(plan)
    duplicated["rows"] = duplicated["rows"] * 2
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(duplicated), True, duplicated)


@pytest.mark.parametrize("column", adapter.COLUMNS)
@pytest.mark.parametrize("side", ["stored", "plan"])
def test_every_one_of_twelve_columns_must_match_exactly(column, side):
    plan = make_plan(make_rows(("S:field:01", "S:field:02")))
    rows = copy.deepcopy(plan["rows"])
    rows[0][column] = rows[0][column] + "SYNTHETIC_MUTATION"
    if side == "plan":
        mutated = {**plan, "rows": rows}
        with pytest.raises(adapter.Conflict):
            adapter._assert_rows(FakeStore(plan), True, mutated)
    else:
        with pytest.raises(adapter.Conflict):
            adapter._assert_rows(FakeStore(plan, rows=as_tuples(rows)), True, plan)


def test_field_values_swapped_between_identities_are_rejected():
    plan = make_plan([make_row("S:swap:01", source="SRC:ONE"),
                      make_row("S:swap:02", source="SRC:TWO")])
    rows = copy.deepcopy(plan["rows"])
    rows[0]["source_id"], rows[1]["source_id"] = rows[1]["source_id"], rows[0]["source_id"]
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, rows=as_tuples(rows)), True, plan)


def test_distinct_unicode_spellings_are_never_normalized():
    composed, decomposed = "S:\u00e9:01", "S:e\u0301:01"
    plan = make_plan([make_row(composed, source="SRC:ONE")])
    stored = as_tuples([make_row(decomposed, source="SRC:ONE")])
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, rows=stored), True, plan)


@pytest.mark.parametrize("kind", ["missing", "extra", "short_tuple", "long_tuple", "non_text_id"])
def test_row_cardinality_and_shape_defects_are_rejected(kind):
    plan = make_plan(make_rows(("S:card:01", "S:card:02")))
    rows = as_tuples(plan["rows"])
    if kind == "missing":
        rows = rows[:-1]
    elif kind == "extra":
        rows = rows + as_tuples([make_row("S:card:03", source="SRC:S:card:03")])
    elif kind == "short_tuple":
        rows[0] = rows[0][:-1]
    elif kind == "long_tuple":
        rows[0] = rows[0] + ("EXTRA",)
    else:
        rows[0] = (rows[0][0], None) + rows[0][2:]
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, rows=rows), True, plan)


def test_reference_order_is_irrelevant():
    plan = make_plan(make_rows(("S:ref:01", "S:ref:02")))
    assert adapter._assert_rows(FakeStore(plan, ref_order=(1, 0)), True, plan) == plan["rows"]


@pytest.mark.parametrize("kind", ["duplicate", "missing", "extra", "mutated_json", "swapped_pair"])
def test_reference_defects_are_rejected(kind):
    plan = make_plan(make_rows(("S:ref:01", "S:ref:02")))
    refs = [(ref["source_id"], ref["work_id"], ref["reference_json"])
            for ref in plan["source_refs"]]
    if kind == "duplicate":
        refs = refs + [refs[0]]
    elif kind == "missing":
        refs = refs[:-1]
    elif kind == "extra":
        refs = refs + [("SRC:EXTRA", "WORK:01", "{}")]
    elif kind == "mutated_json":
        refs[0] = (refs[0][0], refs[0][1], '{"changed":true}')
    else:
        refs[0] = ("SRC:S:ref:02", refs[0][1], refs[0][2])
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, refs=refs), True, plan)


def test_duplicate_plan_reference_pairs_are_rejected():
    plan = make_plan([make_row("S:ref:03", source="SRC:ONE")])
    plan["source_refs"] = plan["source_refs"] + copy.deepcopy(plan["source_refs"])
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan), True, plan)


def test_both_engines_answer_with_identical_canonical_rows():
    plan = make_plan(make_rows(IDS))
    pg_rows = adapter._assert_rows(FakeStore(plan, row_order=(2, 0, 3, 1)), True, plan)
    dd_rows = adapter._assert_rows(FakeStore(plan, row_order=(3, 1, 0, 2)), False, plan)
    assert pg_rows == dd_rows
    assert adapter.digest(pg_rows) == adapter.digest(dd_rows)
    again = adapter._assert_rows(FakeStore(plan, row_order=(1, 3, 2, 0)), True, plan)
    assert adapter.digest(again) == adapter.digest(pg_rows)


class ReadbackConnection(FakeStore):
    """Fresh-connection stand-in that records the read-only/close lifecycle."""

    def __init__(self, plan, **kwargs):
        super().__init__(plan, **kwargs)
        self.closed = False
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)
        if sql == "BEGIN READ ONLY":
            return Cursor([])
        return super().execute(sql, params)

    def close(self):
        self.closed = True


def test_readback_returns_same_ordered_rows_for_different_engine_orders(monkeypatch):
    plan = make_plan(make_rows(IDS))
    # readback() validates the real plan schema before touching a store; the
    # synthetic harness plan is not an import plan, so only that gate is stubbed.
    monkeypatch.setattr(adapter, "verify_plan", lambda supplied: None)
    pg = ReadbackConnection(plan, row_order=(2, 3, 0, 1))
    dd = ReadbackConnection(plan, row_order=(3, 0, 2, 1))
    receipt = adapter.readback(plan, lambda: pg, lambda: dd)
    assert receipt["status"] == "PASS"
    assert receipt["rows"] == len(plan["rows"])
    assert receipt["rows_digest"] == adapter.digest(plan["rows"])
    assert receipt["scientific_admission"] == "NOT_GRANTED_BY_DB_LOAD"
    assert "BEGIN READ ONLY" in pg.sql
    assert pg.closed and dd.closed


def test_readback_rejects_duplicate_identity_on_one_engine(monkeypatch):
    plan = make_plan(make_rows(("S:rb:01", "S:rb:02")))
    monkeypatch.setattr(adapter, "verify_plan", lambda supplied: None)
    pg = ReadbackConnection(plan)
    dd = ReadbackConnection(plan, rows=as_tuples(plan["rows"]) + as_tuples(plan["rows"])[:1])
    with pytest.raises(adapter.Conflict):
        adapter.readback(plan, lambda: pg, lambda: dd)
    assert pg.closed and dd.closed


@pytest.mark.parametrize("kind", ["string_row", "two_element_ref", "string_ref", "ref_none_id"])
def test_malformed_persisted_shapes_raise_conflict_not_exception_type_leak(kind):
    """Malformed query results must surface as Conflict, never as raw TypeError."""
    plan = make_plan(make_rows(("S:shape:01", "S:shape:02")))
    if kind == "string_row":
        with pytest.raises(adapter.Conflict):
            adapter._assert_rows(FakeStore(plan, rows=["S:shape:01"]), True, plan)
        return
    refs = [(ref["source_id"], ref["work_id"], ref["reference_json"])
            for ref in plan["source_refs"]]
    if kind == "two_element_ref":
        refs[0] = refs[0][:2]
    elif kind == "string_ref":
        refs[0] = "SRC:S:shape:01"
    else:
        refs[0] = (None, refs[0][1], refs[0][2])
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, refs=refs), True, plan)


@pytest.mark.parametrize("kind", ["missing_key", "non_mapping"])
def test_malformed_plan_shapes_raise_conflict(kind):
    plan = make_plan(make_rows(("S:shape:03", "S:shape:04")))
    if kind == "missing_key":
        broken = copy.deepcopy(plan)
        del broken["rows"][0]["source_feature_id"]
        with pytest.raises(adapter.Conflict):
            adapter._assert_rows(FakeStore(plan), True, broken)
    else:
        broken = {**plan, "source_refs": ["SRC:S:shape:03"]}
        with pytest.raises(adapter.Conflict):
            adapter._assert_rows(FakeStore(plan), True, broken)


def test_identity_is_compared_exactly_without_case_or_unicode_folding():
    plan = make_plan([make_row("S:Case:01", source="SRC:ONE")])
    for other in ("S:case:01", "S:CASE:01"):
        with pytest.raises(adapter.Conflict):
            adapter._assert_rows(
                FakeStore(plan, rows=as_tuples([make_row(other, source="SRC:ONE")])), True, plan)


def test_extra_plan_rows_without_matching_store_rows_are_rejected():
    plan = make_plan(make_rows(("S:extra:01", "S:extra:02")))
    shortened = as_tuples(plan["rows"])[:-1]
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, rows=shortened), False, plan)


def test_native_geometry_check_still_blocks_reordered_rows():
    plan = make_plan(make_rows(IDS))
    store = FakeStore(plan, row_order=(3, 2, 1, 0), native_ok=False)
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(store, False, plan)
    assert any(sql.startswith("SELECT bool_and(") for sql in store.executed)
    assert "LOAD spatial" in store.executed


def test_singleton_metadata_guards_are_preserved():
    plan = make_plan([make_row("S:meta:01", source="SRC:ONE")])
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, metadata=[]), True, plan)
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, metadata=[
            ("c" * 64, plan["extraction_version"], plan["registry_sha256"], plan["batch_json"])]),
            True, plan)
    with pytest.raises(adapter.Conflict):
        adapter._assert_rows(FakeStore(plan, metadata=[
            (plan["content_digest"], plan["extraction_version"], plan["registry_sha256"],
             '"not-an-object"')]), True, plan)


def test_metadata_json_key_order_tolerance_is_preserved():
    plan = make_plan([make_row("S:meta:02", source="SRC:ONE")],
                     metadata_json=adapter.canonical({"alpha": 1, "beta": 2}))
    metadata = [(plan["content_digest"], plan["extraction_version"],
                 plan["registry_sha256"], '{"beta":2,"alpha":1}')]
    assert adapter._assert_rows(FakeStore(plan, metadata=metadata), True, plan) == plan["rows"]
