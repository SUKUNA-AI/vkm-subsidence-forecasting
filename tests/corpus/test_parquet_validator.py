"""Canon validator: the synthetic canon passes; each injected defect fails its own check (design D §11 + review H)."""
from __future__ import annotations

import copy
import hashlib

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("duckdb")

from vkm_corpus.contracts import arrow as ca  # noqa: E402
from vkm_corpus.parquet.runs import describe_file  # noqa: E402
from vkm_corpus.parquet.validator import ValidationOptions, validate  # noqa: E402

TEST_RUN = "RUN-20260928T235959Z-7e577e57"


@pytest.fixture(scope="module")
def canon(tmp_path_factory):
    from vkm_corpus.testing.synthetic import synthetic_canon

    return synthetic_canon(tmp_path_factory.mktemp("validator") / "data")


def inject(canon, name, mutate, source_id=None, n=[0]):
    """Manifest copy in which one file of ``name`` is replaced by a mutated copy (originals are untouched)."""
    n[0] += 1
    m = copy.deepcopy(canon.manifest)
    files = m["datasets"][name]["files"]
    idx = next(i for i, f in enumerate(files) if source_id is None or f"source_id={source_id}/" in f["path"])
    original = files[idx]
    table = pq.read_table(canon.layout.path(original["path"]))
    new = mutate(table)
    if isinstance(new, list):
        new = pa.Table.from_pylist(new, schema=table.schema)
    parts = original["path"].split("/")
    run_part = next(i for i, p in enumerate(parts) if p.startswith("run="))
    parts[run_part] = f"run={TEST_RUN[:-2]}{n[0]:02x}"
    rel = "/".join(parts)
    path = canon.layout.path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(new, path)
    entry = describe_file(canon.layout, name, rel)
    files[idx] = entry.to_json()
    acc = ca.EMPTY_DIGEST
    for f in files:
        acc = acc + ca.RowDigest.parse(f["rows"], f["digest"])
    m["datasets"][name]["table_fingerprint"] = ca.fingerprint_of(name, acc)
    return m


def failed(canon, manifest, **opts) -> set[str]:
    report = validate(canon.layout, manifest, ValidationOptions(**opts))
    return {c["check_id"] for c in report["checks"] if c["status"] == "FAIL"}


def rows_with(table, pred, **changes):
    out = []
    for r in table.to_pylist():
        if pred(r):
            r = {**r, **changes}
        out.append(r)
    return out


def test_synthetic_canon_passes_with_trace(canon):
    report = validate(canon.layout, canon.manifest, ValidationOptions(deep=True, expected_sources=11))
    status = {c["check_id"]: c["status"] for c in report["checks"]}
    assert report["status"] == "PASS" and status["T01"] == "PASS" and status["B04"] == "PASS"
    assert status["E12"] == "PASS" and status["F10"] == "PASS" and status["F02"] == "WARN"   # 050 not processed
    assert report["counts"]["sources_total"] == 11 and report["counts"]["rollup_closed"] is True


def test_expected_source_count_and_acceptance_mode(canon):
    assert "F01" in failed(canon, canon.manifest, expected_sources=251)
    assert "F02" in failed(canon, canon.manifest, acceptance=True)


FIRST = lambda r: True  # noqa: E731


CASES = [
    ("B01", "pages", "VKM-SRC-001", lambda t: t.to_pylist() + t.to_pylist()[:1]),
    ("C02", "blocks", "VKM-SRC-001", lambda t: rows_with(t, lambda r: r["reading_order"] == 2,
                                                         page_id="VKM-SRC-001:p0099")),
    ("C04", "pages", "VKM-SRC-001", lambda t: rows_with(t, FIRST, render_artifact_id="sha256:" + "f" * 64)),
    ("C05", "blocks", "VKM-SRC-202", lambda t: rows_with(t, FIRST, processing_run_id="RUN-20200101T000000Z-deadbeef")),
    ("E01", "sources", None, lambda t: rows_with(t, lambda r: r["source_id"] == "VKM-SRC-050", review_status="FACT")),
    ("E03", "sources", None, lambda t: rows_with(t, lambda r: r["source_id"] == "VKM-SRC-050",
                                                 review_status="FULLY_REVIEWED")),
    ("D02", "formulas", "VKM-SRC-025", lambda t: rows_with(t, lambda r: r["origin"] == "OCR", model_id=None,
                                                           model_revision=None, models=[])),
    ("D04", "blocks", "VKM-SRC-202", lambda t: rows_with(t, FIRST, source_sha256="f" * 64)),
    ("F03", "pages", "VKM-SRC-001", lambda t: [r for r in t.to_pylist() if r["page_index"] != 2]),
    ("F03", "documents", "VKM-SRC-202", lambda t: rows_with(t, FIRST, page_count=99)),
    ("F05", "pages", "VKM-SRC-202", lambda t: rows_with(t, FIRST, page_status="FAILED")),
    ("E06", "blocks", "VKM-SRC-202", lambda t: rows_with(t, FIRST, quality_flags=["NOT_A_FLAG"])),
    ("E06", "pages", "VKM-SRC-202", lambda t: rows_with(t, FIRST, quality_flags=["FIGURE_TYPE_LOW_CONFIDENCE"])),
    ("E04", "figures", "VKM-SRC-025", lambda t: rows_with(t, FIRST, detected_figure_type="MINE_PLAN",
                                                          figure_type_method="MODEL_CLASSIFIER",
                                                          figure_type_confidence=0.42, figure_type_threshold=0.8)),
    ("E05", "artifacts", "VKM-SRC-025", lambda t: rows_with(t, FIRST, coordinate_space="PAGE_PT_TL",
                                                            crs_status="UNKNOWN_CRS")),
    ("D01", "pages", "VKM-SRC-202", lambda t: t.drop_columns(["page_status"])),
    ("F07", "source_work_links", None, lambda t: rows_with(t, lambda r: r["source_id"] == "VKM-SRC-202",
                                                           is_primary=False)),
    ("E08", "source_work_links", None, lambda t: rows_with(t, lambda r: r["source_id"] == "VKM-SRC-025",
                                                           curation_status="AUTO_PROPOSED")),
    ("G01", "blocks", "VKM-SRC-202", lambda t: t.append_column("ocr_text", pa.array(["x"] * t.num_rows))),
    ("G02", "sources", None, lambda t: rows_with(t, lambda r: r["source_id"] == "VKM-SRC-050",
                                                 canonical_path="/abs/x.pdf")),
    ("B04", "figures", "VKM-SRC-025", lambda t: rows_with(t, FIRST, bbox_x0=105.0)),
    ("E12", "pages", "VKM-SRC-202", lambda t: rows_with(t, FIRST, normalized_text="подмена",
                                                        text_sha256=hashlib.sha256("подмена".encode()).hexdigest(),
                                                        char_count=7)),
    ("E11", "pages", "VKM-SRC-042", lambda t: rows_with(t, lambda r: r["page_index"] == 1, primary_text_origin="NATIVE",
                                                        primary_text_layer="PDF_TEXT_LAYER")),
    ("E10", "sources", None, lambda t: rows_with(t, lambda r: r["source_id"] == "VKM-SRC-001", site_scope=["SKRU1"])),
    ("D08", "blocks", "VKM-SRC-202", lambda t: rows_with(t, FIRST, source_site_scope=["SKRU1"])),
    ("C03", "figures", "VKM-SRC-025", lambda t: rows_with(t, FIRST,
                                                          caption_block_id="VKM-SRC-025:p0001:b000000000000")),
]


@pytest.mark.parametrize("check,name,source,mutate", CASES, ids=[f"{c[0]}-{c[1]}" for c in CASES])
def test_injected_defect_fails_its_check(canon, check, name, source, mutate):
    manifest = inject(canon, name, mutate, source)
    assert check in failed(canon, manifest)


def _stored_artifact(c):
    for f in c.manifest["datasets"]["artifacts"]["files"]:
        for r in pq.read_table(c.layout.path(f["path"])).to_pylist():
            if r["materialization"] == "STORED" and r["artifact_kind"] == "NATIVE_RAW":
                return r
    raise AssertionError("no stored artifact in the synthetic canon")


def test_missing_blob_fails_f10(tmp_path):
    from vkm_corpus.testing.synthetic import synthetic_canon

    c = synthetic_canon(tmp_path / "data")
    stored = _stored_artifact(c)
    c.layout.blob_path(stored["storage_relpath"]).unlink()
    assert "F10" in failed(c, c.manifest)


def test_tampered_blob_fails_only_deep(tmp_path):
    from vkm_corpus.testing.synthetic import synthetic_canon

    c = synthetic_canon(tmp_path / "data")
    stored = _stored_artifact(c)
    c.layout.blob_path(stored["storage_relpath"]).write_bytes(b"tampered")
    assert "F10" not in failed(c, c.manifest)
    assert "F10" in failed(c, c.manifest, deep=True)


def test_stable_raw_content_against_parent(canon):
    """B07: the same object id must keep its raw_content_sha256 across snapshots (else bump the generation)."""
    parent = canon.manifest
    child = inject(canon, "blocks", lambda t: rows_with(t, FIRST, raw_content_sha256="0" * 64), "VKM-SRC-202")
    report = validate(canon.layout, child, ValidationOptions(parent_manifest=parent))
    assert {c["check_id"] for c in report["checks"] if c["status"] == "FAIL"} >= {"B07"}
