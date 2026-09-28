"""Stable IDs (task §12, CP-16 p. 3, H-14, H-15, H-34, H-50): grammar, determinism, sensitivity, parsing."""
from __future__ import annotations

import random
import re
from datetime import datetime, timezone

import pytest

from vkm_corpus import ids
from vkm_corpus.ids import grammar

H = "a" * 64
PK = ids.producer_key("pdf-native", 1, H)


def test_page_ids_for_three_units():
    assert ids.page_id("VKM-SRC-243", "p", 1) == "VKM-SRC-243:p0001"
    assert ids.page_id("VKM-SRC-023", "r", 42) == "VKM-SRC-023:r0042"
    assert ids.page_id("VKM-SRC-249", "s", 17) == "VKM-SRC-249:s0017"
    ref = ids.parse_page_id("VKM-SRC-037:p0126")
    assert (ref.source_id, ref.unit, ref.index) == ("VKM-SRC-037", "p", 126) and ref.page_id == "VKM-SRC-037:p0126"
    for bad in [("VKM-SRC-1", "p", 1), ("VKM-SRC-001", "x", 1), ("VKM-SRC-001", "p", 0), ("VKM-SRC-001", "p", 10000)]:
        with pytest.raises((ids.IdError, ValueError)):
            ids.page_id(*bad)


def test_object_id_is_deterministic_and_region_anchored():
    pid = "VKM-SRC-243:p0012"
    a = ids.bbox_anchor(56.7, 60.0, 538.6, 100.0)
    oid = ids.object_id(pid, "FIGURE", "NATIVE", "PDF_XOBJECT", a, PK)
    assert re.fullmatch(grammar.OBJECT_ID, oid) and oid.startswith(pid + ":f")
    assert ids.object_id(pid, "FIGURE", "NATIVE", "PDF_XOBJECT", a, PK) == oid
    noisy = ids.bbox_anchor(56.7 + 1e-8, 60.0 - 1e-9, 538.6000001, 100.0)
    assert ids.object_id(pid, "FIGURE", "NATIVE", "PDF_XOBJECT", noisy, PK) == oid     # float noise
    changed = {
        ids.object_id(pid, "TABLE", "NATIVE", "PDF_XOBJECT", a, PK),
        ids.object_id(pid, "FIGURE", "EMBEDDED_OCR", "PDF_XOBJECT", a, PK),
        ids.object_id(pid, "FIGURE", "NATIVE", "LAYOUT_MODEL", a, PK),
        ids.object_id(pid, "FIGURE", "NATIVE", "PDF_XOBJECT", ids.bbox_anchor(56.7, 60.0, 538.6, 101.0), PK),
        ids.object_id(pid, "FIGURE", "NATIVE", "PDF_XOBJECT", a, ids.producer_key("pdf-native", 2, H)),
        ids.object_id("VKM-SRC-243:p0013", "FIGURE", "NATIVE", "PDF_XOBJECT", a, PK),
    }
    assert oid not in changed and len(changed) == 6


def test_producer_key_ignores_software_versions_but_not_models():
    base = ids.producer_key("layout", 1, H, [{"role": "LAYOUT", "model_id": "m", "model_revision": "r1"}])
    assert base == ids.producer_key("layout", 1, H, [{"role": "LAYOUT", "model_id": "m", "model_revision": "r1"}])
    assert base != ids.producer_key("layout", 1, H, [{"role": "LAYOUT", "model_id": "m", "model_revision": "r2"}])
    assert base != ids.producer_key("layout", 1, "b" * 64, [{"role": "LAYOUT", "model_id": "m", "model_revision": "r1"}])
    two = [{"role": "RECOGNITION", "model_id": "g", "model_revision": "x"},
           {"role": "LAYOUT", "model_id": "m", "model_revision": "r1"}]
    assert ids.producer_key("ocr", 1, H, two) == ids.producer_key("ocr", 1, H, list(reversed(two)))


def test_ids_do_not_depend_on_row_order():
    pid = "VKM-SRC-001:p0001"
    boxes = [(10.0 * i, 10.0, 10.0 * i + 5, 20.0) for i in range(1, 30)]
    first = [ids.object_id(pid, "BLOCK", "NATIVE", "PDF_TEXT_BLOCK", ids.bbox_anchor(*b), PK) for b in boxes]
    shuffled = boxes[:]
    random.Random(7).shuffle(shuffled)
    second = {b: ids.object_id(pid, "BLOCK", "NATIVE", "PDF_TEXT_BLOCK", ids.bbox_anchor(*b), PK) for b in shuffled}
    assert first == [second[b] for b in boxes]


def test_anchor_without_geometry_and_duplicates():
    pid = "VKM-SRC-249:s0003"
    a1 = ids.ordinal_anchor(4, "Первый  абзац")
    assert a1 == ids.ordinal_anchor(4, "первый абзац")                  # NFKC + casefold + spaces
    assert a1 != ids.ordinal_anchor(5, "первый абзац")
    alloc = ids.ObjectIdAllocator()
    x1, d1 = alloc.allocate(pid, "BLOCK", "NATIVE", "EPUB_ELEMENT", a1, PK)
    x2, d2 = alloc.allocate(pid, "BLOCK", "NATIVE", "EPUB_ELEMENT", a1, PK)
    assert (d1, d2) == (False, True) and x1 != x2
    assert x2 == ids.object_id(pid, "BLOCK", "NATIVE", "EPUB_ELEMENT", a1, PK, dup=1)


def test_docx_objects_are_document_scoped():
    doc = ids.document_id("VKM-SRC-023")
    oid = ids.object_id(doc, "BLOCK", "NATIVE", "DOCX_ELEMENT", ids.xml_anchor("/w:body/w:p[17]"), PK)
    assert oid.startswith("VKM-SRC-023:doc:b")
    ref = ids.parse_object_id(oid)
    assert ref.page_id is None and ref.scope_id == doc and ref.object_kind == "BLOCK"
    with pytest.raises(ids.IdError):
        ids.object_id("VKM-SRC-023:r0001", "BLOCK", "NATIVE", "DOCX_ELEMENT", ids.xml_anchor("/w:p[1]"), PK)
    with pytest.raises(ids.IdError):
        ids.object_id(doc, "BLOCK", "NATIVE", "PDF_TEXT_BLOCK", ids.xml_anchor("/w:p[1]"), PK)


def test_parse_object_id_roundtrip():
    oid = ids.object_id("VKM-SRC-005:p0002", "BIBLIOGRAPHY_ENTRY", "NATIVE", "PDF_TEXT_BLOCK",
                        ids.bbox_anchor(1, 2, 3, 4), PK)
    ref = ids.parse_object_id(oid)
    assert ref.source_id == "VKM-SRC-005" and ref.page_id == "VKM-SRC-005:p0002"
    assert ref.object_kind == "BIBLIOGRAPHY_ENTRY" and len(ref.hash12) == 12


@pytest.mark.parametrize("kind,good,bad", [
    ("source", "VKM-SRC-042", "VKM-SRC-42"),
    ("document", "VKM-SRC-042:doc", "VKM-SRC-042:document"),
    ("page", "VKM-SRC-042:r0001", "VKM-SRC-042:x0001"),
    ("object", "VKM-SRC-042:p0001:m0123456789ab", "VKM-SRC-042:p0001:e0123456789ab"),
    ("work", "VKM-WRK-013", "VKM-WRK-000013"),
    ("author", "AUT-0123456789ab", "AUT-0123456789AB"),
    ("artifact", "sha256:" + "0" * 64, "sha256:" + "0" * 63),
    ("run", "RUN-20260928T142233Z-3f9a2c1b", "run-20260928T142233Z-3f9a2c1b"),
    ("commit", "CMT-0123456789abcdef", "CMT-0123"),
    ("snapshot", "snap-20260928T150000Z-1a2b3c4d", "snap-2026-1a2b3c4d"),
    ("link", "SWL-0123456789abcdef", "XYZ-0123456789abcdef"),
])
def test_grammar_accepts_and_rejects(kind, good, bad):
    assert grammar.matches(kind, good) and not grammar.matches(kind, bad)


def test_name_keys():
    assert ids.author_id("Иванов И. И.") == ids.author_id("Иванов И.И.") == ids.author_id("И.И. Иванов")
    assert ids.author_id("Королёв А.А.") == ids.author_id("Королев А.А.")
    assert ids.author_id("Ivanov I.I.") != ids.author_id("Иванов И.И.")          # scripts never merge
    assert ids.author_id("Иванов Иван Иванович") != ids.author_id("Иванов И.И.")  # initials ≠ full names
    assert ids.author_id("Мусихин В.В. (докладчик)") == ids.author_id("Мусихин В.В.")
    key = ids.name_key("Барях А.А.")
    assert (key.surname, key.initials, key.full_names, key.script) == ("барях", "аа", "", "CYRL")
    assert re.fullmatch(grammar.AUTHOR_ID, ids.author_id("Baryakh A."))
    assert ids.venue_id("Горный журнал") == ids.venue_id("горный  журнал")
    assert ids.venue_id("Горный журнал") != ids.venue_id("Gornyi Zhurnal")
    assert ids.venue_id(issn_l="0017-2278") == ids.venue_id("anything", issn_l="00172278")


def test_doi_isbn_normalisation():
    assert ids.normalize_doi("https://doi.org/10.15372/FTPRPI20260301") == "10.15372/ftprpi20260301"
    assert ids.normalize_doi("doi: 10.1000/ABC.") == "10.1000/abc"
    assert ids.normalize_doi("not a doi") is None
    assert ids.normalize_isbn("978-5-02-038183-4") == "9785020381834"
    assert ids.normalize_isbn("5-02-038183-7") == "9785020381834"          # ISBN-10 converted
    assert ids.normalize_isbn("978-5-02-038183-2") is None                 # bad check digit


def test_work_ids_from_anchor_source():
    assert ids.work_id("VKM-SRC-013") == "VKM-WRK-013" and ids.work_number("VKM-WRK-013") == 13


def test_artifact_run_commit_snapshot_ids():
    assert ids.artifact_id(b"abc") == "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    now = datetime(2026, 9, 28, 14, 22, 33, tzinfo=timezone.utc)
    rid = ids.new_run_id(now, token="3f9a2c1b")
    assert rid == "RUN-20260928T142233Z-3f9a2c1b" and ids.run_started_at(rid) == now
    assert ids.new_run_id(now) != ids.new_run_id(now)                               # events are unique
    body = {"source_id": "VKM-SRC-001", "processing_run_id": rid, "parent_commit_id": None}
    c1 = ids.commit_id({**body, "committed_at": "t1"})
    assert c1 == ids.commit_id({**body, "committed_at": "t2", "commit_id": "CMT-x"})  # time is not identity
    assert c1 != ids.commit_id({**body, "parent_commit_id": c1})
    sid = ids.snapshot_id(now, {"datasets": {}})
    assert grammar.matches("snapshot", sid)
    s1 = ids.step_id(rid, "VKM-SRC-001", "VKM-SRC-001:p0001", "OCR", 1)
    assert s1 != ids.step_id(rid, "VKM-SRC-001", "VKM-SRC-001:p0001", "OCR", 2)
    assert grammar.matches("step", s1) and grammar.matches("error", ids.error_id(rid, s1, "OCR_FAILED", 1))


def test_link_ids_are_derived_from_composite_keys():
    a = ids.swl_id("VKM-SRC-209", "VKM-WRK-208", "PART", None, None)
    assert a == ids.swl_id("VKM-SRC-209", "VKM-WRK-208", "PART", None, None)
    assert a != ids.swl_id("VKM-SRC-209", "VKM-WRK-208", "PART", 1, 99)
    assert ids.wrl_id("VKM-WRK-001", "ABSTRACT_OF", "VKM-WRK-196").startswith("WRL-")
    assert ids.wau_id("VKM-WRK-001", 1) != ids.wau_id("VKM-WRK-001", 2)
    with pytest.raises(ids.IdError):
        ids.link_id("XYZ", "a")


def test_relative_and_logical_paths():
    assert grammar.check_relative_path("04_articles/x.pdf") == "04_articles/x.pdf"
    for bad in ["/abs/x", "C:/x", "a\\b", "../x", "a/../../b"]:
        with pytest.raises(ValueError):
            grammar.check_relative_path(bad)
    assert grammar.check_logical_ref("PRIVATE:00_registry/SOURCE_REGISTER.csv")
    with pytest.raises(ValueError):
        grammar.check_logical_ref("00_registry/SOURCE_REGISTER.csv")
