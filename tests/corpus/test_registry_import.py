"""Registry import on a synthetic PRIVATE tree: file checks (sha256, size, LFS pointer, absent by register), format by
signature, CP-05/06/07 rules, the curated Work files and their type signatures, the sha256 cache."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vkm_corpus.contracts.site_scope import UnknownScopeError
from vkm_corpus.registry import rules
from vkm_corpus.registry.rules import REGISTER_COLUMNS, QUICK_LOOK_MARKER, ReviewRuleError
from vkm_corpus.registry.sources import build_source_rows, load_register, verify_files
from vkm_corpus.registry.works import (
    WORK_LINKS_COLUMNS,
    WORK_REGISTER_COLUMNS,
    WorkRegistryError,
    load_work_registry,
    parse_authors,
    parse_external_ids,
    write_csv,
)

T = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
RUN = "RUN-20260928T100000Z-0a0b0c0d"
CFG = "a" * 64


def _reg_row(n, path, data, **kw):
    row = {"resource_id": f"VKM-SRC-{n:03d}", "canonical_path": path, "original_filename": Path(path).name,
           "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": str(len(data)),
           "source_class": "journal_article", "evidence_scope": "GENERAL_METHOD", "priority": "B",
           "scientific_role": "synthetic", "migration_source": "synthetic_2026-09-27",
           "migration_status": "ADDED_BY_USER_EXACT", "notes": ""}
    row.update(kw)
    return row


@pytest.fixture()
def private(tmp_path):
    root = tmp_path / "private"
    files = {
        "a/ok.pdf": b"\xef\xbb\xbf%PDF-1.4 synthetic",
        "a/changed.pdf": b"%PDF-1.4 changed",
        "a/lfs.pdf": b"version https://git-lfs.github.com/spec/v1\noid sha256:00\nsize 1\n",
        "a/short.djvu": b"AT&TFORM synthetic",
    }
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    rows = [
        _reg_row(1, "a/ok.pdf", files["a/ok.pdf"], evidence_scope="VKM_regional"),
        _reg_row(2, "a/changed.pdf", b"%PDF-1.4 original"),
        _reg_row(3, "a/lfs.pdf", b"%PDF-1.4 real bytes"),
        _reg_row(13, "a/deleted.zip", b"zip", migration_status="ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY"),
        _reg_row(22, "a/retired.zip", b"zip2", evidence_scope="LEGACY_RETIRED",
                 migration_status="RETIRED_FROM_CURRENT_RESEARCH"),
        _reg_row(42, "a/missing.pdf", b"%PDF missing"),
        _reg_row(200, "a/short.djvu", files["a/short.djvu"], notes=f"quick look {QUICK_LOOK_MARKER}"),
    ]
    rows[1]["size_bytes"] = str(len(files["a/changed.pdf"]))       # same size, different bytes → sha mismatch
    write_csv(root / "00_registry" / "SOURCE_REGISTER.csv", REGISTER_COLUMNS, rows)
    return root


def test_file_checks_and_sources(private, tmp_path):
    rows, sha = load_register(private / "00_registry" / "SOURCE_REGISTER.csv")
    checks = verify_files(private, rows, cache_path=tmp_path / "cache.json", workers=4)
    status = {k: v.status.value for k, v in checks.items()}
    assert status == {"VKM-SRC-001": "PRESENT_VERIFIED", "VKM-SRC-002": "SHA256_MISMATCH",
                      "VKM-SRC-003": "LFS_POINTER_ONLY", "VKM-SRC-013": "MISSING", "VKM-SRC-022": "MISSING",
                      "VKM-SRC-042": "MISSING", "VKM-SRC-200": "PRESENT_VERIFIED"}
    assert checks["VKM-SRC-001"].format.value == "PDF" and "LEADING_BYTES_BEFORE_HEADER" in checks["VKM-SRC-001"].flags
    assert checks["VKM-SRC-200"].format.value == "DJVU"
    src = {s.source_id: s for s in build_source_rows(rows, checks, run_id=RUN, created_at=T, register_sha256=sha,
                                                        coverage={"VKM-SRC-001": "FULLY_REVIEWED",
                                                                  "VKM-SRC-002": "RELEVANT_SECTIONS_REVIEWED",
                                                                  "VKM-SRC-003": "FULLY_REVIEWED"},
                                                        config_hash=CFG)}
    assert (src["VKM-SRC-013"].lifecycle_status, src["VKM-SRC-013"].register_skip_reason) == (
        "ABSENT_BY_REGISTER", "ARCHIVE_DELETED_AFTER_ASSEMBLY")
    assert (src["VKM-SRC-022"].lifecycle_status, src["VKM-SRC-022"].register_skip_status) == (
        "RETIRED", "SKIPPED_BY_REGISTER")
    assert src["VKM-SRC-022"].site_scope == [] and src["VKM-SRC-022"].site_scope_mapping == "NOT_A_SCOPE"
    assert (src["VKM-SRC-001"].review_status, src["VKM-SRC-001"].review_status_basis) == (
        "FULLY_REVIEWED", "PHASE1_COVERAGE_MASTER")
    assert src["VKM-SRC-002"].review_status == "RELEVANT_SECTIONS_REVIEWED"
    assert src["VKM-SRC-042"].review_status == "UNSEEN" and src["VKM-SRC-042"].lifecycle_status == "ACTIVE"
    assert src["VKM-SRC-200"].review_status == "QUICK_LOOK_ONLY"
    assert src["VKM-SRC-013"].review_status == "NOT_APPLICABLE"
    assert src["VKM-SRC-001"].site_scope_raw == "VKM_regional" and src["VKM-SRC-001"].site_scope_mapping == "CASE"
    assert "REGISTER_NOTES_NOT_EVIDENCE" in src["VKM-SRC-200"].quality_flags
    assert src["VKM-SRC-001"].input_row == 1 and src["VKM-SRC-001"].input_ref.startswith("PRIVATE:")


def test_stat_cache_is_explicitly_opt_in_and_not_a_fresh_verification(private, tmp_path, monkeypatch):
    from vkm_corpus.registry import sources as mod

    rows, _ = load_register(private / "00_registry" / "SOURCE_REGISTER.csv")
    cache = tmp_path / "cache.json"
    verify_files(private, rows, cache_path=cache, fresh=False)
    calls = []
    monkeypatch.setattr(mod, "sha256_of", lambda p: calls.append(p) or "0" * 64)
    verify_files(private, rows, cache_path=cache, fresh=False)
    assert calls == []


def test_review_and_scope_rules_refuse_unknowns():
    with pytest.raises(ReviewRuleError):
        rules.review_for(5, "ACTIVE", None, "")                     # 001–041 need Phase-1 coverage
    with pytest.raises(ReviewRuleError):
        rules.review_for(230, "ACTIVE", None, "no marker")          # 196–251 need the quick-look marker
    from vkm_corpus.contracts.site_scope import map_site_scope
    with pytest.raises(UnknownScopeError):
        map_site_scope("SKRU-1")


def test_format_detection(tmp_path):
    import zipfile

    def z(name, members):
        p = tmp_path / name
        with zipfile.ZipFile(p, "w") as f:
            for k, v in members.items():
                f.writestr(k, v)
        return p

    assert rules.detect_format(z("b.epub", {"mimetype": "application/epub+zip"}))[0].value == "EPUB"
    assert rules.detect_format(z("c.docx", {"[Content_Types].xml": "x", "word/document.xml": "y"}))[0].value == "DOCX"
    assert rules.detect_format(z("d.zip", {"x.txt": "x"}))[0].value == "ZIP"
    (tmp_path / "e.bin").write_bytes(b"\x89PNG....")
    assert rules.detect_format(tmp_path / "e.bin")[0].value == "IMAGE"
    assert rules.detect_format(tmp_path / "none.pdf")[0].value == "UNKNOWN"


def _work_files(tmp_path, works, links):
    d = tmp_path / "work_registry"
    write_csv(d / "WORK_REGISTER.csv", WORK_REGISTER_COLUMNS, works)
    write_csv(d / "WORK_LINKS.csv", WORK_LINKS_COLUMNS, links)
    return d


def _work(wid, anchor, **kw):
    row = {"work_id": wid, "status": "ACTIVE", "anchor_source_id": anchor, "work_type": "JOURNAL_ARTICLE",
           "title": "Синтетика", "authors": "Альфаев А.А.; Бетин Б.Б.|EDITOR", "year": "2020",
           "identity_status": "CATALOGUE_UNVERIFIED", "curation_status": "CURATED", "available_from_basis": "UNKNOWN",
           "venue": "Вестник синтетики", "doi": "https://doi.org/10.9999/ABC", "isbn": "978-5-02-038183-4",
           "external_ids": "PWL:PWL-0001:SAME_WORK; URN_UUID:urn:uuid:1234:SAME_WORK"}
    row.update(kw)
    return row


def test_work_registry_loads_with_signatures(tmp_path):
    d = _work_files(tmp_path, [_work("VKM-WRK-001", "VKM-SRC-001"), _work("VKM-WRK-002", "VKM-SRC-002")], [
        {"link_kind": "SOURCE_WORK", "from_id": "VKM-SRC-001", "relation": "FULL_COPY", "to_id": "VKM-WRK-001",
         "is_primary": "true", "basis": "BOOTSTRAP_SINGLETON", "curation_status": "CURATED"},
        {"link_kind": "SOURCE_WORK", "from_id": "VKM-SRC-002", "relation": "FOREIGN_CONTENT", "to_id": "",
         "from_page_start": "1", "from_page_end": "1", "is_primary": "false", "basis": "PHASE1_EVIDENCE",
         "curation_status": "CURATED"},
        {"link_kind": "WORK_WORK", "from_id": "VKM-WRK-002", "relation": "NOT_SAME", "to_id": "VKM-WRK-001",
         "basis": "REPOSITORY_AUDIT", "curation_status": "CURATED"},
        {"link_kind": "SOURCE_SOURCE", "from_id": "VKM-SRC-002", "relation": "SHARES_PAGES_WITH",
         "to_id": "VKM-SRC-001", "from_page_start": "1", "from_page_end": "1", "to_page_start": "3",
         "to_page_end": "3", "basis": "REPOSITORY_AUDIT", "curation_status": "CURATED"},
    ])
    wr = load_work_registry(d, run_id=RUN, created_at=T, config_hash=CFG)
    w = wr.works[0]
    assert w.doi == "10.9999/abc" and w.isbn == ["9785020381834"] and w.review_status == "NOT_APPLICABLE"
    assert [e.scheme for e in w.external_ids] == ["PWL", "URN_UUID"] and w.external_ids[1].value == "urn:uuid:1234"
    rel = wr.work_relations[0]
    assert (rel.from_work_id, rel.to_work_id, rel.is_symmetric) == ("VKM-WRK-001", "VKM-WRK-002", True)
    assert wr.source_work_links[1].work_id is None and not wr.source_work_links[1].is_primary
    roles = {(a.name_as_listed, a.role) for a in wr.work_authors}
    assert ("Бетин Б.Б.", "EDITOR") in roles
    assert len(wr.authors) == 2 and all(a.identity_status == "NAME_KEY_ONLY" for a in wr.authors)
    assert len(wr.venues) == 1 and wr.works[0].venue_id == wr.venues[0].venue_id
    assert set(wr.inputs) == {"work_register_sha256", "work_links_sha256"}


@pytest.mark.parametrize("link", [
    {"link_kind": "SOURCE_WORK", "from_id": "VKM-SRC-001", "relation": "ABSTRACT_OF", "to_id": "VKM-WRK-001"},
    {"link_kind": "SOURCE_WORK", "from_id": "VKM-SRC-001", "relation": "FULL_COPY", "to_id": "VKM-WRK-999"},
    {"link_kind": "WORK_WORK", "from_id": "VKM-WRK-001", "relation": "FULL_COPY", "to_id": "VKM-WRK-001"},
    {"link_kind": "SOURCE_SOURCE", "from_id": "VKM-WRK-001", "relation": "DERIVED_FROM", "to_id": "VKM-SRC-001"},
    {"link_kind": "UNKNOWN", "from_id": "VKM-SRC-001", "relation": "FULL_COPY", "to_id": "VKM-WRK-001"},
])
def test_link_type_signatures_are_enforced(tmp_path, link):
    d = _work_files(tmp_path, [_work("VKM-WRK-001", "VKM-SRC-001")],
                    [{**link, "basis": "CURATED_MANUAL", "curation_status": "CURATED", "is_primary": "true"}])
    with pytest.raises((WorkRegistryError, ValueError)):
        load_work_registry(d, run_id=RUN, created_at=T, config_hash=CFG)


def test_cell_parsers():
    assert parse_authors("Иванов И.И.; Петров П.П.|SUPERVISOR") == [("Иванов И.И.", "AUTHOR"),
                                                                      ("Петров П.П.", "SUPERVISOR")]
    with pytest.raises(ValueError):
        parse_authors("Иванов И.И.|BOSS")
    assert parse_external_ids("ASIN:B00X:SAME_WORK") == [{"scheme": "ASIN", "value": "B00X", "relation": "SAME_WORK"}]
    with pytest.raises(WorkRegistryError):
        parse_external_ids("PWL-0001")


def test_import_registry_commits_and_journals(private, tmp_path):
    pytest.importorskip("pyarrow")
    from vkm_corpus.parquet.commits import list_markers
    from vkm_corpus.parquet.layout import init_root
    from vkm_corpus.registry.importer import import_registry

    works = [_work(f"VKM-WRK-{n:03d}", f"VKM-SRC-{n:03d}", doi="", isbn="", external_ids="")
             for n in (1, 2, 3, 13, 22, 42, 200)]
    links = [{"link_kind": "SOURCE_WORK", "from_id": f"VKM-SRC-{n:03d}", "relation": "FULL_COPY",
              "to_id": f"VKM-WRK-{n:03d}", "is_primary": "true", "basis": "BOOTSTRAP_SINGLETON",
              "curation_status": "AUTO_PROPOSED"} for n in (1, 2, 3, 13, 22, 42, 200)]
    d = _work_files(tmp_path, works, links)
    cov = tmp_path / "cov.csv"
    write_csv(cov, ("source_id", "coverage_level"), [{"source_id": f"VKM-SRC-{n:03d}", "coverage_level":
                                                      "FULLY_REVIEWED"} for n in (1, 2, 3, 13, 22)])
    st = init_root(tmp_path / "staging", "STAGING")
    res = import_registry(st, private, work_registry=d, coverage_path=cov, now=T)
    assert res["sources"] == 7 and res["works"] == 7 and res["errors"] == 3            # 002 sha, 003 lfs, 042 missing
    markers = list_markers(st)
    assert len(markers) == 1 and markers[0]["key"] == "REGISTRY"
    assert markers[0]["inputs"]["source_register_sha256"] == res["register_sha256"]
    again = import_registry(st, private, work_registry=d, coverage_path=cov, now=T)
    assert again["noop"] and len(list_markers(st)) == 1                                   # unchanged register
