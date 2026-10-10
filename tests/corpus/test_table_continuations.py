"""Synthetic original/NAV continuation fixtures through the actual NavStore."""
import copy
import json
from datetime import datetime, timezone

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nav_manifest_fixture import write_nav_manifest
from test_nav_tables import SRC, P1, P2, P3, _html, _table
from vkm_corpus.navigation import tables as T, tables_query as Q, store
from vkm_corpus.navigation.table_continuations import ContinuationLimits, get_table_continuation
from vkm_evidence.contracts import record_hash

SNAP = "snap-synthetic-continued-table"
IDS = [p + ":t0001" for p in (P1, P2, P3)]


def fragment(index, rows, *, next_id=None, header=True, width=None, source=SRC, flags=()):
    value = _table(IDS[index], (_html(rows, header),), label="Table 7", flags=list(flags))
    if width is not None:
        value["n_cols"] = width
    value.update(source_id=source, source_sha256="1" * 64, content_sha256=record_hash(value),
        extraction_signature=record_hash({"synthetic": index}), extraction_generation=1,
        continues_object_id=next_id, raw_locator=f"synthetic-page:{index + 1}/table:1", raw_artifact_id=None)
    return value


def normal_fragments(*, header=True):
    a = fragment(0, [["Зона", "Высота, м"], ["1", "1,25"], ["2", "0"], ["Примечание: synthetic|1|2"]], next_id=IDS[1])
    b_rows = [["Зона", "Высота, м"], ["3", "2,50"], ["4", ""]] if header else [["3", "2,50"], ["4", ""]]
    return [a, fragment(1, b_rows, header=header)]


def connection(fragments, page_rows=None):
    con = duckdb.connect()
    con.execute("CREATE SCHEMA canonical")
    canonical = pa.Table.from_pylist(fragments)
    canonical = canonical.set_column(canonical.schema.get_field_index("continues_object_id"),
        "continues_object_id", canonical["continues_object_id"].cast(pa.string()))
    con.register("fixture", canonical)
    con.execute("CREATE TABLE canonical.tables AS SELECT * FROM fixture")
    con.unregister("fixture")
    pages = page_rows or [{"page_id": p, "page_index": i + 1, "source_id": SRC} for i, p in enumerate((P1, P2, P3))]
    con.register("fixture_pages", pa.Table.from_pylist(pages))
    con.execute("CREATE TABLE canonical.pages AS SELECT * FROM fixture_pages")
    con.unregister("fixture_pages")
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.snapshot AS SELECT ? AS snapshot_id,? AS manifest_sha256,'synthetic' AS pipeline_version",
        [SNAP, "ab" * 32])
    built = T.build(con, respace="off")
    Q.register_tables(con, built)
    con.execute("CREATE TABLE nav_meta AS SELECT ? AS meta_json", [json.dumps({"snapshot_id": SNAP,
        "manifest_sha256": "ab" * 32, "identity_status": "SYNTHETIC"})])
    return con, built


def served(tmp_path, fragments, page_rows=None):
    con, built = connection(fragments, page_rows)
    root = tmp_path / "synthetic-data"
    (root / "duckdb").mkdir(parents=True)
    target = root / "duckdb/vkm_corpus.duckdb"
    canonical = duckdb.connect(str(target))
    canonical.execute("CREATE SCHEMA canonical")
    for name in ("tables", "pages"):
        arrow = con.execute(f"SELECT * FROM canonical.{name}").to_arrow_table()
        canonical.register("fixture", arrow)
        canonical.execute(f"CREATE TABLE canonical.{name} AS SELECT * FROM fixture")
        canonical.unregister("fixture")
    canonical.execute("CREATE SCHEMA meta")
    canonical.execute("CREATE TABLE meta.snapshot AS SELECT ? AS snapshot_id,? AS manifest_sha256,'synthetic' AS pipeline_version",
        [SNAP, "ab" * 32])
    canonical.close()
    con.close()
    directory = root / "derived/navigation" / SNAP
    directory.mkdir(parents=True)
    for name, table in built.items():
        pq.write_table(table, directory / (name + ".parquet"))
    write_nav_manifest(directory, SNAP)
    store.pack(directory)
    store.publish(root, SNAP)
    return store.NavStore(root)


def test_actual_served_consumer_keeps_physical_contract_and_pages_the_full_chain(tmp_path):
    original = normal_fragments()
    before = copy.deepcopy(original)
    nav = served(tmp_path, original)
    result = nav.run("table_structured", IDS[0], max_rows=2)
    assert [r["row"] for r in result["rows"]] == [0, 1]
    projection = result["continuation"]
    assert projection["status"] == "STRUCTURALLY_LINKED_UNREVIEWED", projection["reasons"]
    assert projection["scientific_admission"] == "NOT_ESTABLISHED"
    assert projection["original_read_verification"] == "NOT_RUN"
    assert projection["physical_row_count"] == 7 and projection["data_row_count"] == 3
    assert len(projection["fragment_occurrences"]) == 2
    rows, versions = [], set()
    while True:
        rows += projection["rows"]
        versions.add(projection["chain_sha256"])
        if not projection["pagination"]["has_more"]:
            break
        # Existing cursor parameter, distinct typed logical-chain token.
        result = nav.run("table_structured", IDS[0], max_rows=2, cursor=projection["pagination"]["next_cursor"])
        assert [r["row"] for r in result["rows"]] == [0, 1]
        projection = result["continuation"]
    assert len(versions) == 1 and [r["logical_row"] for r in rows] == list(range(7))
    assert [r["role"] for r in rows] == ["HEADER", "DATA", "DATA", "NOTE", "REPEATED_HEADER", "DATA", "TEXT"]
    assert rows[3]["original_nav_role"] == "GROUP" and rows[3]["role_basis"] == "PRINTED_NOTE_MARKER"
    assert [r["data_ordinal"] for r in rows if r["data_ordinal"] is not None] == list(range(3))
    zero = rows[2]["cells"][1]
    blank = rows[-1]["cells"][1]
    assert zero["text_printed"] == "0" and blank["text_printed"] == ""
    assert zero["occurrence"]["canonical_pointer"] == "/cells/5"
    assert zero["occurrence"]["table"]["locator"] == "synthetic-page:1/table:1"
    assert zero["unit_binding"] == "SOURCE_CONTEXT_UNREVIEWED"
    assert zero["unit_occurrences"][0]["occurrence"]["canonical_pointer"] == "/cells/1"
    assert rows[5]["cells"][1]["header_occurrences"][0]["table"]["object_id"] == IDS[1]
    assert rows[3]["cells"][0]["occurrence"]["table"]["object_id"] == IDS[0]
    assert original == before  # no source/NAV overwrite
    nav._con.close()


def test_declared_headerless_continuation_keeps_unknown_units_and_explicit_header_provenance():
    con, _ = connection(normal_fragments(header=False))
    result = Q.get_table_structured(con, IDS[1])["continuation"]
    assert result["status"] == "STRUCTURALLY_LINKED_UNREVIEWED"
    cells = result["rows"][-2]["cells"]
    assert cells[1]["header_binding"] == "EXPLICIT_CONTINUATION_UNREVIEWED"
    assert cells[1]["header_occurrences"][0]["table"]["object_id"] == IDS[0]
    assert cells[1]["value_interpretation"]["unit_canonical"] is None
    con.close()


@pytest.mark.parametrize("failure", ["width", "header", "units", "repeated-double-header", "truncated", "missing",
    "cross-source", "source-version", "generation", "cycle", "nonadjacent", "multiple-incoming", "duplicate-cell"])
def test_invalid_or_ambiguous_chain_is_not_merged(failure):
    fragments = normal_fragments()
    if failure == "width":
        fragments[1] = fragment(1, [["Зона", "Высота, м", "extra"], ["3", "2,50", "4"]])
    elif failure in {"header", "units", "repeated-double-header"}:
        head = ["Different", "Высота, м"] if failure == "header" else ["Зона", "Высота, мм"]
        if failure == "repeated-double-header":
            fragments[1] = fragment(1, [["Зона", "Высота, м"], ["Зона", "Высота, м"], ["3", "2,50"]])
        else:
            fragments[1] = fragment(1, [head, ["3", "2,50"]])
    elif failure == "truncated":
        fragments[1]["quality_flags"] = ["TRUNCATED"]
    elif failure == "missing":
        fragments.pop()
    elif failure == "cross-source":
        fragments[0]["continues_object_id"] = "VKM-SRC-OTHER:p0002:t0001"
    elif failure == "source-version":
        fragments[1]["source_sha256"] = "2" * 64
    elif failure == "generation":
        fragments[1]["extraction_generation"] = 2
    elif failure == "cycle":
        fragments[1]["continues_object_id"] = IDS[0]
    elif failure == "nonadjacent":
        fragments[0]["continues_object_id"] = IDS[2]
        fragments[1] = fragment(2, [["Зона", "Высота, м"], ["3", "2,50"]])
    elif failure == "multiple-incoming":
        fragments.append(fragment(2, [["Зона", "Высота, м"], ["5", "3,50"]], next_id=IDS[1]))
    elif failure == "duplicate-cell":
        fragments[1]["cells"].append(copy.deepcopy(fragments[1]["cells"][-1]))
    con, _ = connection(fragments)
    physical = Q.get_table_structured(con, IDS[0])
    assert physical["found"] and physical["continuation"]["status"] == "BLOCKED"
    assert physical["continuation"]["rows"] == [] and not physical["continuation"]["pagination"]["complete"]
    con.close()


def test_limits_are_explicit_and_do_not_make_partial_chain_complete():
    con, _ = connection(normal_fragments())
    result = get_table_continuation(con, IDS[0], SRC, limits=ContinuationLimits(max_cells=1))
    assert result["status"] == "BLOCKED" and result["reasons"] == ["CANONICAL_CELL_BUDGET_EXCEEDED"]
    result = get_table_continuation(con, IDS[0], SRC, max_rows=1)
    assert result["pagination"]["has_more"] and not result["pagination"]["complete"]
    con.close()


def test_cursor_binds_generation_chain_all_cells_roles_and_order():
    con, built = connection(normal_fragments())
    cursor = Q.get_table_structured(con, IDS[0], max_rows=1)["continuation"]["pagination"]["next_cursor"]
    with pytest.raises(ValueError, match="cursor"):
        Q.get_table_structured(con, IDS[1], max_rows=1, cursor=cursor)
    cells = built["table_cells"].to_pylist()
    cells[-2]["value_text"] = "changed interpretation"
    con.unregister("table_cells")
    con.register("table_cells", pa.Table.from_pylist(cells, schema=T.TABLE_CELLS_SCHEMA()))
    with pytest.raises(ValueError, match="stale"):
        Q.get_table_structured(con, IDS[0], max_rows=1, cursor=cursor)
    con.close()


def test_unknown_snapshot_and_old_schema_are_not_ready():
    con, _ = connection(normal_fragments())
    con.execute("UPDATE meta.snapshot SET manifest_sha256=?", ["cd" * 32])
    assert Q.get_table_structured(con, IDS[0])["continuation"]["reasons"] == ["CANONICAL_NAV_GENERATION_MISMATCH"]
    con.execute("ALTER TABLE canonical.tables DROP COLUMN extraction_signature")
    assert Q.get_table_structured(con, IDS[0])["continuation"]["status"] == "NOT_AVAILABLE"
    con.close()


def test_wide_table_output_is_bounded_without_truncating_columns_or_logical_rows():
    heads = ["Зона"] + [f"synthetic column {i}, м" for i in range(256)]
    values = ["synthetic-zone"] + [str(i) for i in range(256)]
    fragments = [fragment(0, [heads, values], next_id=IDS[1]), fragment(1, [heads, values])]
    con, _ = connection(fragments)
    limits = ContinuationLimits(max_output_cells=300)
    result = get_table_continuation(con, IDS[0], SRC, limits=limits)
    rows = []
    while True:
        assert len(result["rows"]) == 1 and len(result["rows"][0]["cells"]) == 257
        rows += result["rows"]
        if not result["pagination"]["has_more"]:
            break
        assert not result["pagination"]["complete"]
        result = get_table_continuation(con, IDS[0], SRC, limits=limits,
            cursor=result["pagination"]["next_cursor"])
    assert len(rows) == 4 and [r["logical_row"] for r in rows] == list(range(4))
    assert len({c["occurrence"]["cell_id"] for r in rows for c in r["cells"]}) == 1028
    single = get_table_continuation(con, IDS[0], SRC, limits=ContinuationLimits(max_output_cells=100))
    assert single["status"] == "BLOCKED" and single["reasons"] == ["SINGLE_ROW_OUTPUT_BUDGET_EXCEEDED"]
    con.close()


def test_cross_source_edge_is_blocked_before_other_original_cell_content_is_requested():
    fragments = normal_fragments()
    other = "VKM-SRC-OTHER:p0002:t0001"
    fragments[0]["continues_object_id"] = other
    con, _ = connection(fragments)
    class Tripwire:
        def execute(self, sql, params=()):
            assert not (other in params and ("SELECT cells" in sql or "to_json(cells)" in sql))
            return con.execute(sql, params)
    result = get_table_continuation(Tripwire(), IDS[0], SRC)
    assert result["status"] == "BLOCKED" and result["reasons"] == ["CROSS_SOURCE_CONTINUATION"]
    con.close()


def test_logical_cursor_never_ignores_disappeared_schema_or_generation():
    con, _ = connection(normal_fragments())
    cursor = Q.get_table_structured(con, IDS[0], max_rows=1)["continuation"]["pagination"]["next_cursor"]
    con.execute("UPDATE meta.snapshot SET snapshot_id='different-synthetic-generation'")
    with pytest.raises(ValueError, match="cursor"):
        Q.get_table_structured(con, IDS[0], cursor=cursor)
    con.execute("ALTER TABLE canonical.tables DROP COLUMN extraction_signature")
    with pytest.raises(ValueError, match="cursor"):
        Q.get_table_structured(con, IDS[0], cursor=cursor)
    con.close()


def test_unlinked_same_number_and_identical_headers_never_infer_a_join():
    fragments = normal_fragments()
    fragments[0]["continues_object_id"] = None
    con, _ = connection(fragments)
    result = Q.get_table_structured(con, IDS[0])["continuation"]
    assert result["status"] == "SINGLE_FRAGMENT_UNREVIEWED"
    assert [ref["object_id"] for ref in result["fragment_occurrences"]] == [IDS[0]]
    assert result["physical_row_count"] == 4
    con.close()


@pytest.mark.parametrize("caption,reason", [("Продолжение таблицы 7", "UNDECLARED_PREDECESSOR_HINT"),
    ("Table 7 (continued)", "UNDECLARED_PREDECESSOR_HINT"),
    ("Продолжение на следующей странице", "UNDECLARED_SUCCESSOR_HINT")])
def test_printed_hint_is_visible_blocked_debt_without_guessing_a_target(caption, reason):
    fragments = normal_fragments()[:1]
    fragments[0]["continues_object_id"] = None
    fragments[0]["caption"] = caption
    con, _ = connection(fragments)
    result = Q.get_table_structured(con, IDS[0])
    assert result["found"]
    assert result["continuation"]["status"] == "BLOCKED_UNDECLARED"
    assert result["continuation"]["reasons"] == [reason]
    con.close()


def test_canonical_cell_pointer_order_is_part_of_cursor_identity():
    con, _ = connection(normal_fragments())
    cursor = get_table_continuation(con, IDS[0], SRC, max_rows=1)["pagination"]["next_cursor"]
    con.execute("UPDATE canonical.tables SET cells=list_reverse(cells) WHERE object_id=?", [IDS[0]])
    with pytest.raises(ValueError, match="stale"):
        get_table_continuation(con, IDS[0], SRC, cursor=cursor)
    con.close()


def test_grid_and_navigation_memory_guards_run_before_unbounded_materialization():
    con, _ = connection(normal_fragments())
    con.execute("UPDATE canonical.tables SET n_rows=2000000000 WHERE object_id=?", [IDS[0]])
    result = get_table_continuation(con, IDS[0], SRC)
    assert result["reasons"] == ["CANONICAL_GRID_BUDGET_EXCEEDED"]
    con.execute("UPDATE canonical.tables SET n_rows=4 WHERE object_id=?", [IDS[0]])
    result = get_table_continuation(con, IDS[0], SRC, limits=ContinuationLimits(max_navigation_bytes=1))
    assert result["reasons"] == ["NAVIGATION_FRAGMENT_BUDGET_EXCEEDED"]
    con.close()


def test_missing_or_invalid_nav_identity_is_explicit_not_an_unhandled_error():
    con, _ = connection(normal_fragments())
    con.execute("UPDATE nav_meta SET meta_json='[]'")
    assert get_table_continuation(con, IDS[0], SRC)["reasons"] == ["INVALID_NAV_IDENTITY"]
    con.execute("DROP TABLE nav_meta")
    assert get_table_continuation(con, IDS[0], SRC)["status"] == "NOT_AVAILABLE"
    con.close()


def test_forged_same_prefix_source_identity_is_checked_before_caption_or_cells():
    con, _ = connection(normal_fragments())
    con.execute("UPDATE canonical.tables SET source_id='VKM-SRC-OTHER' WHERE object_id=?", [IDS[1]])
    class Tripwire:
        def execute(self, sql, params=()):
            if IDS[1] in params:
                assert "t.caption" not in sql and "SELECT cells" not in sql and "to_json(cells)" not in sql
            return con.execute(sql, params)
    result = get_table_continuation(Tripwire(), IDS[0], SRC)
    assert result["reasons"] == ["SOURCE_VERSION_MISMATCH"]
    con.close()


def producer_result(*, declared=True):
    """An actual TableX/CanonMapper input; no artificial canonical IDs or envelope filling."""
    from vkm_corpus.extract.model import SourceInput, SourceResult, DocumentX, Pagination, PageX, TableX
    from vkm_corpus.contracts.models import TableContinuationCandidate
    from vkm_corpus.contracts.signatures import stage_signature
    from vkm_corpus.versions import PIPELINE_VERSION
    source = SourceInput("VKM-SRC-901", "synthetic/table.pdf", "1" * 64, 1234, evidence_scope="GENERAL_METHOD")
    document = DocumentX("PDF", "1.7", None, Pagination("p", "PDF_PAGE", "PDF_PAGE_TREE", 2, "synthetic"))
    tables = []
    for page in (1, 2):
        fixture = fragment(page - 1, [["Зона", "Высота, м"], [str(page), "1,25"]])
        artifact = "sha256:" + record_hash({"synthetic native": page})
        signature = stage_signature(source_sha256=source.sha256, stage="NORMALIZE", pipeline_version=PIPELINE_VERSION,
            extractor_id="synthetic-native-tables", extractor_version="1.0", stage_config_hash="a" * 64,
            page_unit="p", page_index=page, input_artifact_ids=[artifact])
        tables.append(TableX(page_index=page, bbox=(50., 100., 500., 400.), origin="NATIVE",
            region_origin="NATIVE_TABLE_FINDER", extractor_id="synthetic-native-tables", extractor_version="1.0",
            generation="1", raw_config_hash="a" * 64, raw_artifact_id=artifact,
            raw_locator=f"synthetic:page[{page}]/table[1]", recognition_method="NATIVE_FIND_TABLES",
            raw_output=fixture["raw_output"], cells=fixture["cells"], n_rows=2, n_cols=2,
            extraction_signature=signature))
    if declared:
        import hashlib
        tables[0].continuation_candidate = TableContinuationCandidate(source_sha256=source.sha256,
            target_page_index=2, target_raw_locator=tables[1].raw_locator,
            target_raw_content_sha256=hashlib.sha256(tables[1].raw_output.encode()).hexdigest(),
            declaration_locator="synthetic:page[1]/table[1]/explicit-next", declaration_artifact_id=tables[0].raw_artifact_id,
            basis="NATIVE_SOURCE_RELATION")
    return SourceResult(source=source, document=document, tables=tables,
        pages=[PageX(page_index=p, page_kind="PDF_PAGE", width_pt=595., height_pt=842.) for p in (1, 2)])


def mapped(result):
    from vkm_corpus.extract.to_canon import CanonMapper
    return CanonMapper(result, run_id="RUN-20261004T100000Z-01020304", config_hashes={"objects": "b" * 64},
        created_at=datetime(2026, 10, 4, 10, tzinfo=timezone.utc)).build().tables


def test_real_producer_to_canonical_to_navstore_has_immutable_unreviewed_link(tmp_path):
    result = producer_result()
    original = copy.deepcopy(result)
    canon = mapped(result)
    no_links = mapped(producer_result(declared=False))
    assert [r.object_id for r in canon["tables"]] == [r.object_id for r in no_links["tables"]]
    assert [r.raw_content_sha256 for r in canon["tables"]] == [r.raw_content_sha256 for r in no_links["tables"]]
    assert [r.extraction_signature for r in canon["tables"]] == [r.extraction_signature for r in no_links["tables"]]
    assert canon["tables"][0].continues_object_id == canon["tables"][1].object_id
    nav = served(tmp_path, [r.model_dump() for r in canon["tables"]], [r.model_dump() for r in canon["pages"]])
    projection = nav.run("table_structured", canon["tables"][0].object_id)["continuation"]
    assert projection["status"] == "STRUCTURALLY_LINKED_UNREVIEWED", projection["reasons"]
    assert projection["physical_row_count"] == 4 and projection["data_row_count"] == 2
    declaration = projection["continuation_declarations"][0]
    assert declaration["status"] == "SOURCE_DECLARED_UNREVIEWED"
    assert declaration["provenance"]["candidate"]["declaration_locator"] == "synthetic:page[1]/table[1]/explicit-next"
    assert projection["scientific_admission"] == "NOT_ESTABLISHED"
    assert result == original
    nav._con.close()


@pytest.mark.parametrize("failure", ["source", "target-hash", "target-locator", "target-ambiguous", "declaration-artifact", "generation", "nonadjacent", "signature"])
def test_producer_rejects_unbound_or_ambiguous_link_before_publication(failure):
    result = producer_result()
    candidate = result.tables[0].continuation_candidate
    if failure == "source":
        result.tables[0].continuation_candidate = candidate.model_copy(update={"source_sha256": "2" * 64})
    elif failure == "target-hash":
        result.tables[0].continuation_candidate = candidate.model_copy(update={"target_raw_content_sha256": "0" * 64})
    elif failure == "target-locator":
        result.tables[0].continuation_candidate = candidate.model_copy(update={"target_raw_locator": "unavailable"})
    elif failure == "target-ambiguous":
        result.tables.append(copy.deepcopy(result.tables[1]))
    elif failure == "declaration-artifact":
        result.tables[0].continuation_candidate = candidate.model_copy(update={"declaration_artifact_id": "sha256:" + "0" * 64})
    elif failure == "generation":
        result.tables[1].generation = "2"
    elif failure == "nonadjacent":
        result.tables[1].page_index = 3
        result.tables[0].continuation_candidate = candidate.model_copy(update={"target_page_index": 3})
    elif failure == "signature":
        result.tables[1].extraction_signature = "not-a-signature"
    with pytest.raises(ValueError):
        mapped(result)


def test_missing_exact_producer_signature_does_not_make_scientific_object_version(tmp_path):
    result = producer_result()
    result.tables[1].extraction_signature = None
    canon = mapped(result)
    nav = served(tmp_path, [r.model_dump() for r in canon["tables"]], [r.model_dump() for r in canon["pages"]])
    projection = nav.run("table_structured", canon["tables"][0].object_id)["continuation"]
    assert projection["status"] == "BLOCKED"
    assert projection["rows"] == [] and projection["scientific_admission"] == "NOT_ESTABLISHED"
    nav._con.close()


@pytest.mark.parametrize("mutation", ["candidate-hash", "declaration-artifact", "target-locator", "target-generation", "target-object"])
def test_consumer_rechecks_typed_source_declaration_not_only_a_saved_digest(mutation):
    canon = mapped(producer_result())
    fragments = [r.model_dump() for r in canon["tables"]]
    proof = fragments[0]["continuation_provenance"]
    if mutation == "candidate-hash":
        proof["candidate_sha256"] = "0" * 64
    elif mutation == "declaration-artifact":
        proof["candidate"]["declaration_artifact_id"] = "sha256:" + "0" * 64
        proof["candidate_sha256"] = record_hash(proof["candidate"])
    elif mutation == "target-locator":
        proof["candidate"]["target_raw_locator"] = "forged"
        proof["candidate_sha256"] = record_hash(proof["candidate"])
    elif mutation == "target-generation":
        proof["target_extraction_generation"] = 2
    elif mutation == "target-object":
        proof["target_object_id"] = fragments[0]["object_id"]
    con, _ = connection(fragments, [r.model_dump() for r in canon["pages"]])
    projection = get_table_continuation(con, fragments[0]["object_id"], "VKM-SRC-901")
    assert projection["status"] == "BLOCKED" and projection["rows"] == []
    con.close()
