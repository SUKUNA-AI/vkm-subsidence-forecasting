"""Synthetic projection input for tests and live smoke runs (no text from any source; general domain words only).

Cases covered (agent E design §8.2, H-16, H-18, H-19, H-25, H-30, H-49):

* one work with two ACTIVE copies (``VKM-SRC-101`` full, ``VKM-SRC-102`` partial) and two inactive sources linked to it
  (absent ``106``, retired ``107``) → four ``INSTANCE_OF`` (CP-25: a register fact, the node keeps its lifecycle) and
  ``work_copy_count`` 2 (ACTIVE copies only);
* foreign pages in ``103``: page 2 carries identified content of ``VKM-WRK-101``, page 3 unidentified content;
* identical page text in ``101`` p1 and ``102`` p1 → a duplicate group; curated ``SHARES_PAGES_WITH`` with ranges;
* ``105`` p1 with two text layers (embedded OCR primary, GLM-OCR secondary);
* container citing work (journal issue ``104``), a self-citation, a candidate match, an entry with an unknown citing
  work on a foreign page;
* curated ``ABSTRACT_OF`` and ``NOT_SAME``, auto-proposed ``EDITION_OF`` and ``SHARES_PAGES_WITH`` (not projected);
* availability bases CURATED, ASSUMED_FROM_PUBLICATION and UNKNOWN; source scopes EXACT, SYNONYM and AMBIGUOUS.
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

W1, W3, W4, W5 = "VKM-WRK-101", "VKM-WRK-103", "VKM-WRK-104", "VKM-WRK-105"
A1, A2, A3 = "AUT-00000000a001", "AUT-00000000a002", "AUT-00000000a003"
V1 = "VEN-00000000b001"
RUN_REG = "RUN-20260928T000000Z-0000aaaa"
RUN_DOC = "RUN-20260928T000100Z-0000bbbb"


def _sid(n: int) -> str:
    return f"VKM-SRC-{n:03d}"


def _pid(n: int, page: int) -> str:
    return f"{_sid(n)}:p{page:04d}"


def _oid(page_id: str, kind: str, seq: int) -> str:
    return f"{page_id}:{kind}{seq:012x}"


def _h(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _art(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


def _link(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


_ENV = {"schema_version": "0.1.0", "processing_run_id": RUN_DOC, "extractor_id": "synthetic-extractor",
        "review_status": "AUTO_EXTRACTED_UNREVIEWED", "quality_flags": []}

PAGE_TEXT: dict[str, str | None] = {
    _pid(101, 1): "Оседание земной поверхности наблюдается над выработанным пространством. "
                  "Маркшейдерские наблюдения за сдвижением ведутся по профильным линиям.",
    _pid(101, 2): "Пласт сильвинита и карналлита. В сильвините встречается галит; сильвиниты залегают ниже.",
    _pid(101, 3): "Закладка выработанного пространства уменьшает оседания. Список литературы.",
    _pid(102, 1): "Оседание земной поверхности наблюдается над выработанным пространством. "
                  "Маркшейдерские наблюдения за сдвижением ведутся по профильным линиям.",
    _pid(102, 2): None,
    _pid(103, 1): "Ползучесть каменной соли при длительном нагружении описывается степенным законом.",
    _pid(103, 2): "Расчётная схема оседания земной поверхности для камерной системы разработки.",
    _pid(103, 3): "Литература к предыдущей статье выпуска.",
    _pid(104, 1): "Горное эхо: ползучести каменной соли посвящена статья о закладке.",
    _pid(104, 2): "Список литературы статьи выпуска.",
    _pid(105, 1): "Сильвин и сильвинит — разные термины. Salt creep and subsidence were creeping under backfilling.",
}


def synthetic_projection_rows() -> dict[str, list[dict[str, Any]]]:
    """Rows for ``ProjectionInput.from_rows`` (every ``e_*`` relation)."""
    sources = [
        {"source_id": _sid(101), "lifecycle_status": "ACTIVE", "file_status": "PRESENT_VERIFIED",
         "format_detected": "PDF", "site_scope_raw": "SKRU1", "site_scope": ["SKRU1"], "site_scope_mapping": "EXACT",
         "review_status": "QUICK_LOOK_ONLY", "review_status_basis": "INTAKE_QUICK_LOOK_MARKER",
         "processing_rollup": "COMPLETE", "page_count": 3},
        {"source_id": _sid(102), "lifecycle_status": "ACTIVE", "file_status": "PRESENT_VERIFIED",
         "format_detected": "PDF", "site_scope_raw": "VKM_REGIONAL", "site_scope": ["VKM_REGIONAL"],
         "site_scope_mapping": "EXACT", "review_status": "UNSEEN", "processing_rollup": "PARTIAL", "page_count": 2},
        {"source_id": _sid(103), "lifecycle_status": "ACTIVE", "file_status": "PRESENT_VERIFIED",
         "format_detected": "DJVU", "site_scope_raw": "SKRU1_SKRU2_SKRU3", "site_scope": [],
         "site_scope_mapping": "AMBIGUOUS", "review_status": "UNSEEN", "processing_rollup": "COMPLETE", "page_count": 3},
        {"source_id": _sid(104), "lifecycle_status": "ACTIVE", "file_status": "PRESENT_VERIFIED",
         "format_detected": "PDF", "site_scope_raw": "NON_VKM_ANALOG", "site_scope": ["NON_VKM"],
         "site_scope_mapping": "SYNONYM", "review_status": "UNSEEN", "processing_rollup": "COMPLETE", "page_count": 2},
        {"source_id": _sid(105), "lifecycle_status": "ACTIVE", "file_status": "PRESENT_VERIFIED",
         "format_detected": "DJVU", "site_scope_raw": "GENERAL_METHOD", "site_scope": ["GENERAL_METHOD"],
         "site_scope_mapping": "EXACT", "review_status": "UNSEEN", "processing_rollup": "NEEDS_REVIEW",
         "page_count": 1},
        {"source_id": _sid(106), "lifecycle_status": "ABSENT_BY_REGISTER",
         "lifecycle_reason_code": "ARCHIVE_DELETED_AFTER_ASSEMBLY", "file_status": "MISSING",
         "format_detected": "UNKNOWN", "site_scope_raw": "SKRU1", "site_scope": ["SKRU1"],
         "site_scope_mapping": "EXACT", "review_status": "NOT_APPLICABLE", "review_status_basis": "LIFECYCLE",
         "processing_rollup": "SKIPPED_BY_REGISTER"},
        {"source_id": _sid(107), "lifecycle_status": "RETIRED", "lifecycle_reason_code": "RETIRED_NOT_EVIDENCE",
         "file_status": "MISSING", "format_detected": "UNKNOWN", "site_scope_raw": "LEGACY_RETIRED", "site_scope": [],
         "site_scope_mapping": "NOT_A_SCOPE", "review_status": "NOT_APPLICABLE", "review_status_basis": "LIFECYCLE",
         "processing_rollup": "SKIPPED_BY_REGISTER"},
    ]
    for s in sources:
        s.update({"source_sha256": hashlib.sha256(s["source_id"].encode()).hexdigest(), "schema_version": "0.1.0",
                  "origin": "REGISTRY", "processing_run_id": RUN_REG, "quality_flags": [], "priority": "B",
                  "source_class_raw": "journal_article"})

    reg = {"schema_version": "0.1.0", "origin": "REGISTRY", "processing_run_id": RUN_REG,
           "review_status": "NOT_APPLICABLE", "quality_flags": []}
    works = [
        {"work_id": W1, "work_type": "JOURNAL_ARTICLE", "title": "Оседание земной поверхности над выработками",
         "publication_year": 2008, "doi": "10.9999/vkm.syn.1", "languages": ["ru"], "venue_id": V1, "volume": "7",
         "issue": "2", "pages_range": "10-19", "identity_status": "CATALOGUE_UNVERIFIED", "curation_status": "CURATED",
         "is_container": False, "available_latest_day": date(2008, 12, 31),
         "available_basis": "ASSUMED_FROM_PUBLICATION", "external_ids": ["PWL:PWL-9001:SAME_WORK"]},
        {"work_id": W3, "work_type": "MONOGRAPH", "title": "Ползучесть каменной соли", "publication_year": 1995,
         "languages": ["ru", "en"], "identity_status": "VERIFIED_IN_FILE", "curation_status": "AUTO_PROPOSED",
         "is_container": False, "available_latest_day": date(1996, 6, 30), "available_basis": "CURATED"},
        {"work_id": W4, "work_type": "JOURNAL_ISSUE", "title": "Горное эхо, выпуск", "publication_year": 2012,
         "languages": ["ru"], "identity_status": "CATALOGUE_UNVERIFIED", "curation_status": "AUTO_PROPOSED",
         "is_container": True, "available_basis": "UNKNOWN"},
        {"work_id": W5, "work_type": "DISSERTATION_ABSTRACT", "title": "Сдвижение горных пород", "publication_year": 2010,
         "languages": ["ru"], "identity_status": "CATALOGUE_UNVERIFIED", "curation_status": "AUTO_PROPOSED",
         "is_container": False, "available_latest_day": date(2010, 12, 31),
         "available_basis": "ASSUMED_FROM_PUBLICATION"},
    ]
    for w in works:
        w.update(reg)

    def swl(source: int, work: str | None, link_type: str, primary: bool, start: int | None = None,
            end: int | None = None, curation: str = "CURATED") -> dict[str, Any]:
        return {"link_id": "SWL-" + _link(f"{source}|{work}|{link_type}|{start}|{end}"), "source_id": _sid(source),
                "work_id": work, "link_type": link_type, "is_primary": primary, "page_start": start,
                "page_end": end, "basis": "CURATED_MANUAL", "curation_status": curation}

    links = [
        swl(101, W1, "FULL_COPY", True), swl(102, W1, "PARTIAL_COPY", True), swl(103, W3, "FULL_COPY", True),
        swl(103, W1, "FOREIGN_CONTENT", False, 2, 2), swl(103, None, "FOREIGN_CONTENT", False, 3, 3),
        swl(104, W4, "FULL_COPY", True, curation="AUTO_PROPOSED"), swl(105, W5, "FULL_COPY", True,
                                                                        curation="AUTO_PROPOSED"),
        swl(106, W1, "FULL_COPY", True), swl(107, W1, "FULL_COPY", True),
    ]

    def wrl(a: str, rel: str, b: str, sym: bool, curation: str = "CURATED") -> dict[str, Any]:
        if sym and a > b:
            a, b = b, a
        return {"relation_id": "WRL-" + _link(f"{a}|{rel}|{b}"), "from_work_id": a, "relation": rel, "to_work_id": b,
                "is_symmetric": sym, "basis": "CURATED_MANUAL", "curation_status": curation}

    work_relations = [wrl(W5, "ABSTRACT_OF", W3, False), wrl(W4, "NOT_SAME", W1, True),
                      wrl(W3, "EDITION_OF", W1, False, "AUTO_PROPOSED")]

    def srl(a: int, rel: str, b: int, ranges: tuple[int | None, ...] = (None, None, None, None),
            curation: str = "CURATED") -> dict[str, Any]:
        return {"relation_id": "SRL-" + _link(f"{a}|{rel}|{b}|{ranges[0]}|{ranges[2]}"), "from_source_id": _sid(a),
                "relation": rel, "to_source_id": _sid(b), "from_page_start": ranges[0], "from_page_end": ranges[1],
                "to_page_start": ranges[2], "to_page_end": ranges[3], "basis": "REPOSITORY_AUDIT",
                "curation_status": curation}

    source_relations = [srl(102, "DERIVED_FROM", 101), srl(107, "CONTAINS_COPY_OF", 101),
                        srl(103, "SHARES_PAGES_WITH", 101, (2, 2, 1, 1)),
                        srl(104, "SHARES_PAGES_WITH", 103, (2, 2, 3, 3), "AUTO_PROPOSED")]

    authors = [{"author_id": a, "identity_status": "NAME_KEY_ONLY", "script": "CYRL", **reg} for a in (A1, A2, A3)]
    authors[2]["script"] = "LATN"
    work_authors = [
        {"row_id": "WAU-" + _link(f"{W1}|1"), "work_id": W1, "author_id": A1, "ordinal": 1, "role": "AUTHOR",
         "name_as_listed": "Иванов И. И."},
        {"row_id": "WAU-" + _link(f"{W1}|2"), "work_id": W1, "author_id": A2, "ordinal": 2, "role": "AUTHOR",
         "name_as_listed": "Петров П. П."},
        {"row_id": "WAU-" + _link(f"{W1}|3"), "work_id": W1, "author_id": A2, "ordinal": 3, "role": "EDITOR",
         "name_as_listed": "Петров П. П."},
        {"row_id": "WAU-" + _link(f"{W3}|1"), "work_id": W3, "author_id": A3, "ordinal": 1, "role": "AUTHOR",
         "name_as_listed": "Sidorov S."},
    ]
    venues = [{"venue_id": V1, "venue_type": "JOURNAL", "identity_status": "NAME_KEY_ONLY", "issn": ["0000-0000"],
               **reg}]

    page_meta = {  # page_id -> (status, origin, primary_text_origin)
        _pid(101, 1): ("NATIVE_OK", "NATIVE", "NATIVE"), _pid(101, 2): ("NATIVE_OK", "NATIVE", "NATIVE"),
        _pid(101, 3): ("NATIVE_OK", "NATIVE", "NATIVE"), _pid(102, 1): ("NATIVE_OK", "NATIVE", "NATIVE"),
        _pid(102, 2): ("FAILED", "NATIVE", "NONE"), _pid(103, 1): ("OCR_OK", "OCR", "OCR"),
        _pid(103, 2): ("OCR_OK", "OCR", "OCR"), _pid(103, 3): ("OCR_OK", "OCR", "OCR"),
        _pid(104, 1): ("NATIVE_OK", "NATIVE", "NATIVE"), _pid(104, 2): ("NATIVE_OK", "NATIVE", "NATIVE"),
        _pid(105, 1): ("EMBEDDED_TEXT_OK", "EMBEDDED_OCR", "EMBEDDED_OCR"),
    }
    pages = []
    for page_id, (status, origin, primary) in page_meta.items():
        text = PAGE_TEXT[page_id]
        source_id, unit = page_id.split(":")
        pages.append({
            "page_id": page_id, "source_id": source_id, "page_index": int(unit[1:]),
            "page_kind": "DJVU_PAGE" if source_id in (_sid(103), _sid(105)) else "PDF_PAGE",
            "printed_page_raw": str(int(unit[1:]) + 10), "printed_page_labels": [str(int(unit[1:]) + 10)],
            "page_class": "NATIVE_TEXT" if origin == "NATIVE" else "RASTER_SCAN",
            "page_status": status, "file_text_status": "PRESENT_OK" if origin != "OCR" else "ABSENT",
            "primary_text_layer": {"NATIVE": "PDF_TEXT_LAYER", "OCR": "GLM_OCR",
                                   "EMBEDDED_OCR": "DJVU_EMBEDDED_OCR_LAYER", "NONE": "NONE"}[primary],
            "ocr_status": "DONE" if origin == "OCR" else "NOT_REQUIRED", "primary_text_origin": primary,
            "text": text, "text_rule": "page_text_v1" if text is not None else None, "text_sha256": _h(text),
            "render_artifact_id": _art("render:" + page_id), "preview_artifact_id": _art("preview:" + page_id),
            "origin": origin, **_ENV,
            "model_id": "glm-ocr" if origin == "OCR" else None,
            "model_revision": "2e85a62840cc" if origin == "OCR" else None,
            "content_sha256": _h("page:" + page_id),
        })
        if status == "FAILED":
            pages[-1]["quality_flags"] = ["EMPTY_PAGE"]

    blocks = []
    seq = 0
    for page in pages:
        text = page["text"]
        if not text:
            continue
        parts = ([t.strip() if t.strip().endswith(".") else t.strip() + "." for t in text.split(". ") if t.strip()]
                 if page["page_id"] == _pid(101, 1) else [text])
        for order, part in enumerate(parts, 1):
            seq += 1
            layer = {"NATIVE": "PDF_TEXT_LAYER", "OCR": "GLM_OCR", "EMBEDDED_OCR": "DJVU_EMBEDDED_OCR_LAYER"}[
                page["origin"]]
            blocks.append({
                "block_id": _oid(page["page_id"], "b", seq), "page_id": page["page_id"],
                "source_id": page["source_id"], "text_layer": layer, "is_primary_layer": True, "block_type": "TEXT",
                "reading_order": order, "bbox_x0": 50.0, "bbox_y0": 60.0 + 40 * order, "bbox_x1": 540.0,
                "bbox_y1": 90.0 + 40 * order, "bbox_space": "PAGE_PT_TL", "text": part, "text_sha256": _h(part),
                "language": "ru", "region_origin": "PDF_TEXT_BLOCK" if page["origin"] == "NATIVE" else "OCR_MODEL",
                "origin": page["origin"], **_ENV, "model_id": page["model_id"], "model_revision": page["model_revision"],
                "content_sha256": _h("block:" + part)})
    # the second (secondary) layer of 105 p1: GLM-OCR over the embedded layer (H-30)
    seq += 1
    blocks.append({**blocks[-1], "block_id": _oid(_pid(105, 1), "b", seq), "text_layer": "GLM_OCR",
                   "is_primary_layer": False, "origin": "OCR", "model_id": "glm-ocr", "model_revision": "2e85a62840cc",
                   "region_origin": "OCR_MODEL", "text": PAGE_TEXT[_pid(105, 1)].replace("—", "-"),
                   "text_sha256": _h(PAGE_TEXT[_pid(105, 1)].replace("—", "-"))})

    figures = [
        {"figure_id": _oid(_pid(101, 2), "f", 1), "page_id": _pid(101, 2), "source_id": _sid(101), "bbox_x0": 60.0,
         "bbox_y0": 300.0, "bbox_x1": 520.0, "bbox_y1": 600.0, "bbox_space": "PAGE_PT_TL", "figure_label": "Рис. 3.1",
         "caption": "Рис. 3.1. Мульда сдвижения над отработанным пластом", "layout_class": "VECTOR_GRAPHICS",
         "figure_type": "UNKNOWN_FIGURE_TYPE", "figure_type_method": "NONE", "region_origin": "VECTOR_CLUSTER",
         "image_artifact_id": _art("fig1"), "vector_artifact_ids": [_art("fig1-paths"), _art("fig1-svg")],
         "is_primary_layer": True, "origin": "NATIVE", **_ENV, "content_sha256": _h("fig1")},
        {"figure_id": _oid(_pid(103, 1), "f", 2), "page_id": _pid(103, 1), "source_id": _sid(103), "bbox_x0": 70.0,
         "bbox_y0": 320.0, "bbox_x1": 500.0, "bbox_y1": 560.0, "bbox_space": "PAGE_PT_TL", "figure_label": "Рисунок 2",
         "caption": "Рисунок 2. Кривая ползучести каменной соли", "layout_class": "RASTER_IMAGE",
         "figure_type": "CHART", "figure_type_method": "MODEL_CLASSIFIER", "figure_type_confidence": 0.93,
         "region_origin": "LAYOUT_MODEL", "image_artifact_id": None, "vector_artifact_ids": [],
         "is_primary_layer": True, "origin": "OCR", **_ENV, "model_id": "pp-doclayout-v3",
         "model_revision": "97d101e6", "content_sha256": _h("fig2")},
    ]
    tables = [
        {"table_id": _oid(_pid(104, 1), "t", 1), "page_id": _pid(104, 1), "source_id": _sid(104), "bbox_x0": 60.0,
         "bbox_y0": 200.0, "bbox_x1": 520.0, "bbox_y1": 320.0, "bbox_space": "PAGE_PT_TL", "table_label": "Таблица 1",
         "caption": "Таблица 1. Параметры ползучести", "n_rows": 2, "n_cols": 2,
         "text": "Параметр | Значение\nA | 1.5", "raw_format": "HTML", "recognition_method": "NATIVE_FIND_TABLES",
         "region_origin": "NATIVE_TABLE_FINDER", "image_artifact_id": _art("tab1"), "is_primary_layer": True,
         "origin": "NATIVE", **_ENV, "content_sha256": _h("tab1")},
    ]
    formulas = [
        {"formula_id": _oid(_pid(103, 1), "m", 1), "page_id": _pid(103, 1), "source_id": _sid(103), "bbox_x0": 80.0,
         "bbox_y0": 600.0, "bbox_x1": 400.0, "bbox_y1": 640.0, "bbox_space": "PAGE_PT_TL", "formula_kind": "DISPLAY",
         "equation_label": "(3.2)", "latex": r"\dot{\varepsilon} = A \sigma^{n} \exp(-Q/RT)", "raw_format": "LATEX",
         "recognition_method": "OCR_GLM", "region_origin": "LAYOUT_MODEL", "image_artifact_id": _art("m1"),
         "is_primary_layer": True, "origin": "OCR", **_ENV, "model_id": "glm-ocr", "model_revision": "2e85a62840cc",
         "content_sha256": _h("m1")},
        {"formula_id": _oid(_pid(101, 3), "m", 2), "page_id": _pid(101, 3), "source_id": _sid(101), "bbox_x0": 80.0,
         "bbox_y0": 500.0, "bbox_x1": 300.0, "bbox_y1": 530.0, "bbox_space": "PAGE_PT_TL", "formula_kind": "INLINE",
         "equation_label": None, "latex": None, "raw_format": "IMAGE_ONLY", "recognition_method": "NONE",
         "region_origin": "PDF_XOBJECT", "image_artifact_id": _art("m2"), "is_primary_layer": True,
         "origin": "NATIVE", **_ENV, "content_sha256": _h("m2")},
    ]

    def entry(page: str, seq_no: int, citing: str | None, resolution: str) -> dict[str, Any]:
        return {"entry_id": _oid(page, "c", seq_no), "page_id": page, "source_id": page.split(":")[0],
                "citing_work_id": citing, "citing_work_resolution": resolution,
                "citing_work_is_container": citing == W4, "rule_version": "citing_work_v1", "entry_label": str(seq_no),
                "ordinal_in_list": seq_no, "origin": "NATIVE", "review_status": "AUTO_EXTRACTED_UNREVIEWED",
                "quality_flags": [], "schema_version": "0.1.0", "processing_run_id": RUN_DOC,
                "content_sha256": _h(f"entry:{page}:{seq_no}")}

    e1 = entry(_pid(104, 2), 1, W4, "UNIQUE_LINK")
    e2 = entry(_pid(101, 3), 1, W1, "UNIQUE_LINK")
    e3 = entry(_pid(102, 1), 1, W1, "UNIQUE_LINK")
    e4 = entry(_pid(103, 3), 1, None, "FOREIGN_CONTENT")
    e5 = entry(_pid(101, 3), 2, W1, "UNIQUE_LINK")
    e6 = entry(_pid(103, 1), 1, W3, "UNIQUE_LINK")
    bibliography = [e1, e2, e3, e4, e5, e6]

    def bml(e: dict[str, Any], work: str, status: str, method: str = "DOI_EXACT") -> dict[str, Any]:
        return {"link_id": "BML-" + _link(f"{e['entry_id']}|{work}|{method}"), "entry_id": e["entry_id"],
                "cited_work_id": work, "match_method": method, "match_score": 1.0 if status != "CANDIDATE" else 0.61,
                "match_status": status, "matched_fields": ["doi"] if method == "DOI_EXACT" else ["title", "year"],
                "curation_status": "AUTO_PROPOSED", "accepted": status == "AUTO_EXACT_ID_MATCH",
                "rule_version": "bibliography_match_v1"}

    blinks = [bml(e1, W3, "AUTO_EXACT_ID_MATCH"), bml(e2, W3, "AUTO_EXACT_ID_MATCH"),
              bml(e3, W3, "AUTO_EXACT_ID_MATCH"), bml(e4, W1, "AUTO_EXACT_ID_MATCH"),
              bml(e5, W4, "CANDIDATE", "TITLE_YEAR"), bml(e6, W3, "AUTO_EXACT_ID_MATCH")]
    # D's `cites` view (cites_v1): accepted links with a known citing work other than the cited one, per pair
    per_entry = [(e["citing_work_id"], link["cited_work_id"], e["entry_id"], link["match_method"], e["source_id"],
                  e["citing_work_is_container"])
                 for e in bibliography for link in blinks
                 if link["entry_id"] == e["entry_id"] and link["accepted"] and e["citing_work_id"]
                 and e["citing_work_id"] != link["cited_work_id"]]
    cites = []
    for pair in sorted({(a, b) for a, b, *_ in per_entry}):
        rows = [r for r in per_entry if (r[0], r[1]) == pair]
        cites.append({"citing_work_id": pair[0], "cited_work_id": pair[1], "n_citing_entries": len(rows),
                      "n_citing_sources": len({r[4] for r in rows}), "citing_work_is_container": any(r[5] for r in rows),
                      "match_methods": sorted({r[3] for r in rows}), "entry_ids": sorted(r[2] for r in rows),
                      "rule_version": "cites_v1"})

    page_sequence = []
    for sid in sorted({p["source_id"] for p in pages}):
        ids = sorted(p["page_id"] for p in pages if p["source_id"] == sid)
        page_sequence += [{"source_id": sid, "from_page_id": a, "to_page_id": b, "rule_version": "page_sequence_v1"}
                          for a, b in zip(ids, ids[1:])]
    dup_group = "DUP-" + (_h(PAGE_TEXT[_pid(101, 1)]) or "")[:16]
    duplicates = [{"dup_group_id": dup_group, "page_id": pid, "source_id": pid.split(":")[0],
                   "rule_version": "duplicate_pages_v1"} for pid in (_pid(101, 1), _pid(102, 1))]
    # D's `work_sources` (instance link types, not rejected, with a work) and `foreign_content_pages`
    instance_links = [dict(link) for link in links
                      if link["link_type"] in ("FULL_COPY", "PARTIAL_COPY", "FRONT_MATTER_ONLY", "PART")
                      and link["curation_status"] != "REJECTED" and link["work_id"] is not None]
    foreign_pages = [{"page_id": p["page_id"], "source_id": p["source_id"], "work_id": link["work_id"],
                      "link_id": link["link_id"], "rule_version": "citing_work_v1"}
                     for link in links if link["link_type"] == "FOREIGN_CONTENT"
                     for p in pages if p["source_id"] == link["source_id"]
                     and link["page_start"] <= p["page_index"] <= link["page_end"]]

    return {"e_sources": sources, "e_works": works, "e_source_work_links": links, "e_instance_links": instance_links,
            "e_foreign_pages": foreign_pages, "e_work_relations": work_relations,
            "e_source_relations": source_relations, "e_authors": authors, "e_work_authors": work_authors,
            "e_venues": venues, "e_pages": pages, "e_blocks": blocks, "e_figures": figures, "e_tables": tables,
            "e_formulas": formulas, "e_bibliography": bibliography, "e_bibliography_links": blinks,
            "e_cites": cites, "e_page_sequence": page_sequence, "e_page_duplicates": duplicates}


def synthetic_canonical_rows() -> dict[str, list[Any]]:
    """Canonical rows (agent D's contract models, built with D's factories) for a small CANONICAL snapshot:

    * ``VKM-SRC-001`` (2 pages) and ``VKM-SRC-002`` (1 page, identical text on p1) — full/partial copies of
      ``VKM-WRK-001``; ``VKM-SRC-013`` absent by the register and ``VKM-SRC-025`` (1 page) — copies of ``VKM-WRK-013``
      (CP-25: ``INSTANCE_OF`` from both, ``work_copy_count`` 1);
    * a bibliography entry on 001 p2 whose DOI equals the DOI of ``VKM-WRK-013`` (→ RESOLVES_TO and CITES);
    * one figure, table and formula on 001 p1; one author with a work link; one venue.
    """
    from vkm_corpus import ids
    from vkm_corpus.testing import rows as f

    s1, s2, s13, s25 = "VKM-SRC-001", "VKM-SRC-002", "VKM-SRC-013", "VKM-SRC-025"
    w1, w13 = ids.work_id(s1), ids.work_id(s13)
    text_a = "Оседание земной поверхности над выработанным пространством; маркшейдерские наблюдения. " * 3
    text_b = "Ползучесть каменной соли и сильвинита; закладка выработанного пространства."
    text_c = "Расчётная схема оседания земной поверхности."
    sources = [f.make_source(sid=s1), f.make_source(sid=s2), f.make_source(sid=s25),
               f.make_source(sid=s13, lifecycle_status="ABSENT_BY_REGISTER", file_status="MISSING",
                             register_skip_status="SKIPPED_BY_REGISTER",
                             register_skip_reason="ARCHIVE_DELETED_AFTER_ASSEMBLY", review_status="NOT_APPLICABLE",
                             review_status_basis="LIFECYCLE", observed_sha256=None, observed_size_bytes=None)]
    works = [f.make_work(anchor=s1, doi="10.9999/synthetic.100", publication_year=2008),
             f.make_work(anchor=s13, doi="10.9999/synthetic.001", title="Ползучесть каменной соли",
                         publication_year=1995)]
    documents = [f.make_document(sid=s1, page_count=2), f.make_document(sid=s2, page_count=1),
                 f.make_document(sid=s25, page_count=1)]
    pages = [f.make_page(index=1, sid=s1, text=text_a), f.make_page(index=2, sid=s1, text=text_b),
             f.make_page(index=1, sid=s2, text=text_a), f.make_page(index=1, sid=s25, text=text_c)]
    blocks = [f.make_block(page_index=1, sid=s1, text=text_a), f.make_block(page_index=2, sid=s1, text=text_b),
              f.make_block(page_index=1, sid=s2, text=text_a), f.make_block(page_index=1, sid=s25, text=text_c)]
    figures = [f.make_figure(page_index=1, sid=s1)]
    tables = [f.make_table(page_index=1, sid=s1)]
    formulas = [f.make_formula(page_index=1, sid=s1)]
    bibliography = [f.make_bibliography_entry(page_index=2, sid=s1, ordinal=1)]

    def link(sid: str, wid: str | None, link_type: str, primary: bool) -> Any:
        from vkm_corpus.contracts.builders import build_row

        env = f.reg_env("source_work_links", ids.swl_id(sid, wid, link_type, None, None),
                        input_ref="PRIVATE:00_registry/work_registry/WORK_LINKS.csv")
        return build_row("source_work_links", {**env, "source_id": sid, "work_id": wid, "link_type": link_type,
                                               "is_primary": primary, "basis": "CURATED_MANUAL",
                                               "curation_status": "CURATED"})

    links = [link(s1, w1, "FULL_COPY", True), link(s2, w1, "PARTIAL_COPY", True),
             link(s13, w13, "FULL_COPY", True), link(s25, w13, "FULL_COPY", True)]
    from vkm_corpus.contracts.builders import build_row

    author = ids.author_id("Альфаев А.А.")
    authors = [build_row("authors", {**f.reg_env("authors", author, origin="DERIVED"), "author_id": author,
                                     "name_display": "Альфаев А.А.", "name_key": str(ids.name_key("Альфаев А.А.")),
                                     "script": "CYRL", "identity_status": "NAME_KEY_ONLY",
                                     "derived_from_work_ids": [w1]})]
    work_authors = [build_row("work_authors", {**f.reg_env("work_authors", ids.wau_id(w1, 1)), "work_id": w1,
                                               "author_id": author, "ordinal": 1, "role": "AUTHOR",
                                               "name_as_listed": "Альфаев А.А.", "basis": "CURATED_MANUAL",
                                               "curation_status": "CURATED"})]
    return {"sources": sources, "works": works, "documents": documents, "pages": pages, "blocks": blocks,
            "figures": figures, "tables": tables, "formulas": formulas, "bibliography_entries": bibliography,
            "source_work_links": links, "authors": authors, "work_authors": work_authors}


def write_synthetic_canonical_root(root: Any, rows: dict[str, list[Any]] | None = None,
                                   snapshot_id: str = "snap-20260928T120000Z-5e5e5e5e") -> Any:
    """Write a CANONICAL data root (marker, ``CURRENT``, manifest, one Parquet file per dataset) from canonical rows
    — the layout of agent D, with D's Arrow schemas. Returns the root path."""
    import json
    from pathlib import Path

    import pyarrow.parquet as pq

    from vkm_corpus.contracts import arrow as ca
    from vkm_corpus.contracts.datasets import STORED_DATASETS, dataset

    root = Path(root)
    rows = synthetic_canonical_rows() if rows is None else rows
    canonical = root / "canonical"
    (canonical / "_snapshots").mkdir(parents=True, exist_ok=True)
    (root / ".vkm_root.json").write_text(json.dumps({"root_kind": "CANONICAL", "layout_version": "1"}) + "\n",
                                          encoding="utf-8")
    datasets: dict[str, Any] = {}
    for name in STORED_DATASETS:
        data = rows.get(name) or []
        table = ca.rows_to_table(name, data) if data else ca.empty_table(name)
        rel = f"{name}/run=RUN-20260928T100000Z-0a0b0c0d/part-00000.parquet"
        path = canonical / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path)
        spec = dataset(name)
        datasets[name] = {"kind": str(spec.cls) if hasattr(spec, "cls") else None, "schema_version": spec.version,
                          "rows": table.num_rows,
                          "files": [{"path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                     "bytes": path.stat().st_size, "rows": table.num_rows}]}
    manifest = {"manifest_version": "1.0.0", "snapshot_id": snapshot_id, "datasets": datasets,
                "counts": {"sources_total": len(rows.get("sources") or [])}}
    (canonical / "_snapshots" / f"{snapshot_id}.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    (canonical / "CURRENT").write_text(snapshot_id + "\n", encoding="utf-8")
    return root


def synthetic_input(**kwargs: Any) -> Any:
    """``ProjectionInput`` over the synthetic rows (imports DuckDB lazily)."""
    from vkm_corpus.graph.canon import ProjectionInput
    from vkm_corpus.graph.common import SnapshotInfo

    info = SnapshotInfo(snapshot_id="snap-20260928T000000Z-5e5e5e5e", manifest_sha256=_h("synthetic-manifest") or "",
                        schema_versions=("synthetic=0.1.0",), manifest_counts={"sources_total": 7})
    return ProjectionInput.from_rows(synthetic_projection_rows(), info=info)
