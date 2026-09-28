"""Synthetic canonical root for tests of every agent (never source text; everything is generated).

    canon = synthetic_canon(tmp_path / "data", with_duckdb=True)
    canon.snapshot_id, canon.manifest, canon.layout, canon.duckdb_path, canon.ids["foreign_entry"]

What it contains (the real flow is exercised end to end):

* a synthetic PRIVATE tree (register CSV, small files with real signatures, curated work registry) under
  ``<root>_private/``;
* a STAGING root ``<root>_staging/`` where the registry import and one extraction run write commits, run markers
  and blobs, plus a crashed run (START only) — then everything is copied to the CANONICAL root (markers last),
  admitted and snapshotted; ``CURRENT`` points to a validated snapshot;
* sources: 001 native PDF (3 pages, a running head, bibliography with an exact DOI match and a title/year
  candidate); 002 raster-scan PDF with an embedded foreign OCR layer (page 1 EMBEDDED_TEXT_OK) and GLM-OCR on page 2
  (two layers, OCR primary); 005 journal article whose page 2 carries foreign content (its bibliography entry never
  cites for the host) and whose page 1 duplicates page 3 of 001 (SHARES_PAGES_WITH + duplicate candidate);
  013 absent by register and 022 retired (SKIPPED_BY_REGISTER); 023 DOCX with two pinned render pages,
  document-scoped XML blocks and an OMML formula; 025 PDF derived from 013 with a layout figure (vector artifacts),
  a native table with caption and a GLM formula, a NOT_STORED_REPRODUCIBLE render; 042 DjVu (embedded OCR page +
  FAILED page with an error); 050 present but not processed; 202 another copy of the 013/025 work; 249 EPUB with two
  spine items. Work group 013/025/202 → VKM-WRK-013; NOT_SAME between works 001 and 002.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vkm_corpus import ids
from vkm_corpus.contracts.builders import ProducerContext, SourceContext, build_row, doc_envelope
from vkm_corpus.contracts.signatures import config_hash, stage_signature
from vkm_corpus.contracts.text_rules import normalize_text_v1, page_text_v1
from vkm_corpus.contracts.vocab import ArtifactKind, RootKind
from vkm_corpus.parquet.admit import admit
from vkm_corpus.parquet.blobs import put_blob
from vkm_corpus.parquet.commits import acquire_lease, commit_source
from vkm_corpus.parquet.layout import CanonLayout, init_root
from vkm_corpus.parquet.runs import RunRecorder
from vkm_corpus.parquet.snapshot import build_snapshot
from vkm_corpus.parquet.validator import ValidationOptions
from vkm_corpus.registry.importer import import_registry
from vkm_corpus.registry.rules import REGISTER_COLUMNS, QUICK_LOOK_MARKER
from vkm_corpus.registry.works import WORK_LINKS_COLUMNS, WORK_REGISTER_COLUMNS, write_csv
from vkm_corpus.versions import PIPELINE_VERSION

T0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
GLM = {"role": "RECOGNITION", "model_id": "zai-org/GLM-OCR", "model_revision": "2e85a62840ccac27daa451df36c736c4636b8628"}
LAYOUT = {"role": "LAYOUT", "model_id": "PaddlePaddle/PP-DocLayoutV3", "model_revision": "97d101e6" + "0" * 32}
SHARED_TEXT = ("Общий синтетический абзац, напечатанный на двух страницах разных выпусков. Его повтор не является "
               "независимым свидетельством и должен попасть в кандидаты дублей страниц. " * 2).strip()
SYN_DOI = "10.9999/synthetic.{}"


# ================================================================= synthetic PRIVATE tree
def _pdf_bytes(tag: str, bom: bool = False) -> bytes:
    body = f"%PDF-1.7\n% synthetic {tag}\n1 0 obj << /Type /Catalog >> endobj\ntrailer << >>\n%%EOF\n".encode()
    return (b"\xef\xbb\xbf" + body) if bom else body


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 28, 0, 0, 0))
            z.writestr(info, data)
    return buf.getvalue()


SOURCES: list[dict[str, Any]] = [
    # id, path, class, scope, migration status, bytes
    {"n": 1, "path": "04_articles/synthetic_001.pdf", "cls": "journal_article", "scope": "VKM_regional",
     "data": _pdf_bytes("001")},
    {"n": 2, "path": "03_books/synthetic_002_scan.pdf", "cls": "monograph", "scope": "VKM_Solikamsk",
     "data": _pdf_bytes("002", bom=True)},
    {"n": 5, "path": "04_articles/synthetic_005.pdf", "cls": "journal_article", "scope": "other_potash_deposit",
     "data": _pdf_bytes("005")},
    {"n": 13, "path": "03_books/synthetic_013.zip", "cls": "book_archive", "scope": "VKM_REGIONAL",
     "status": "ORIGINAL_ARCHIVE_DELETED_AFTER_PDF_ASSEMBLY", "data": None},
    {"n": 22, "path": "08_data_archives/synthetic_022.zip", "cls": "retired_legacy_archive",
     "scope": "LEGACY_RETIRED", "status": "RETIRED_FROM_CURRENT_RESEARCH", "data": None},
    {"n": 23, "path": "01_primary_sources/synthetic_023.docx", "cls": "thesis_secondary", "scope": "SKRU1",
     "data": _zip_bytes({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})},
    {"n": 25, "path": "03_books/synthetic_025.pdf", "cls": "derived_convenience_pdf", "scope": "VKM_REGIONAL",
     "data": _pdf_bytes("025")},
    {"n": 42, "path": "03_books/synthetic_042.djvu", "cls": "monograph", "scope": "GENERAL_METHOD",
     "data": b"AT&TFORM\x00\x00\x00\x10DJVMsynthetic042"},
    {"n": 50, "path": "04_articles/synthetic_050.pdf", "cls": "journal_article", "scope": "NON_VKM_ANALOG",
     "data": _pdf_bytes("050")},
    {"n": 202, "path": "03_books/synthetic_202.pdf", "cls": "textbook", "scope": "VKM_REGIONAL",
     "data": _pdf_bytes("202")},
    {"n": 249, "path": "03_books/synthetic_249.epub", "cls": "textbook", "scope": "GENERAL_METHOD",
     "data": _zip_bytes({"mimetype": b"application/epub+zip", "OEBPS/content.opf": b"<package/>"})},
]
PHASE1 = {1: "FULLY_REVIEWED", 2: "FULLY_REVIEWED", 5: "RELEVANT_SECTIONS_REVIEWED", 13: "SUPERSEDED_BY_COPY",
          22: "RETIRED_NOT_EVIDENCE", 23: "FULLY_REVIEWED", 25: "FULLY_REVIEWED"}


def sid(n: int) -> str:
    return ids.source_id(n)


def write_private_tree(private: Path) -> dict[str, Any]:
    """Synthetic register, source files and curated work registry; returns paths and hashes."""
    reg_rows = []
    for s in SOURCES:
        data = s["data"]
        sha = hashlib.sha256(data or f"absent-{s['n']}".encode()).hexdigest()
        size = len(data) if data else 1000 + s["n"]
        if data is not None:
            p = private / s["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        notes = f"Синтетическая заметка {s['n']:03d}."
        if s["n"] >= 196:
            notes += f" {QUICK_LOOK_MARKER}"
        reg_rows.append({"resource_id": sid(s["n"]), "canonical_path": s["path"],
                         "original_filename": Path(s["path"]).name, "sha256": sha, "size_bytes": str(size),
                         "source_class": s["cls"], "evidence_scope": s["scope"], "priority": "A",
                         "scientific_role": "synthetic fixture", "migration_source": "synthetic_intake_2026-09-27",
                         "migration_status": s.get("status", "ADDED_BY_USER_EXACT"), "notes": notes})
    write_csv(private / "00_registry" / "SOURCE_REGISTER.csv", REGISTER_COLUMNS, reg_rows)
    cov = private / "coverage" / "SOURCE_COVERAGE_MASTER.csv"
    write_csv(cov, ("source_id", "coverage_level"), [{"source_id": sid(n), "coverage_level": c}
                                                     for n, c in sorted(PHASE1.items())])
    wr = private / "00_registry" / "work_registry"
    write_csv(wr / "WORK_REGISTER.csv", WORK_REGISTER_COLUMNS, _work_rows())
    write_csv(wr / "WORK_LINKS.csv", WORK_LINKS_COLUMNS, _link_rows())
    return {"private": private, "coverage": cov, "work_registry": wr}


def _work(n: int, wtype: str, title: str, authors: str, year: int | None, **kw: Any) -> dict[str, Any]:
    row = {"work_id": ids.work_id(sid(n)), "status": "ACTIVE", "anchor_source_id": sid(n), "work_type": wtype,
           "title": title, "authors": authors, "year": str(year) if year else "", "year_raw": str(year or ""),
           "language": "ru", "basis_title": "HUNT_TABLE", "basis_authors": "HUNT_TABLE", "basis_year": "HUNT_TABLE",
           "basis_venue": "UNKNOWN", "basis_identifiers": "UNKNOWN", "identity_status": "CATALOGUE_UNVERIFIED",
           "available_from_basis": "UNKNOWN", "curation_status": "CURATED", "curated_by": "synthetic-fixture",
           "curated_at": "2026-09-28"}
    row.update(kw)
    return row


def _work_rows() -> list[dict[str, Any]]:
    return [
        _work(1, "JOURNAL_ARTICLE", "Синтетическая статья о мульде", "Альфаев А.А.; Бетин Б.Б.", 2020,
              venue="Вестник синтетики", doi=SYN_DOI.format("001"), basis_identifiers="HUNT_TABLE"),
        _work(2, "MONOGRAPH", "Синтетическая монография со сканом", "Гаммов Г.Г.", 1976),
        _work(5, "JOURNAL_ARTICLE", "Синтетическая статья выпуска", "Дельтин Д.Д.; Альфаев А.А.", 2022,
              venue="Вестник синтетики", doi=SYN_DOI.format("005"), basis_identifiers="HUNT_TABLE"),
        _work(13, "TEACHING_MANUAL", "Синтетическое учебное пособие", "Эпсилонов Э.Э.", 2008,
              isbn="978-5-02-038183-4", basis_identifiers="REGISTER_NOTES"),
        _work(22, "PROJECT_DATA_PACKAGE", "Синтетический пакет данных (retired)", "", None),
        _work(23, "THESIS", "Синтетическая выпускная работа", "Зетова З.З.|AUTHOR; Этов Э.Э.|SUPERVISOR", 2026),
        _work(42, "MONOGRAPH", "Синтетическая книга в DjVu", "Тетов Т.Т.", 1973,
              identity_status="FILENAME_PAGECOUNT_UNVERIFIED", basis_title="FILENAME_PAGECOUNT_UNVERIFIED"),
        _work(50, "JOURNAL_ARTICLE", "Synthetic analogue article", "Iotov I.", 2019, venue="Synthetic Journal",
              language="en"),
        _work(249, "TEXTBOOK", "Synthetic geophysics textbook", "Kappov K.; Lambdin L.", 2020, language="en",
              external_ids="ASIN:B000SYNTH0:SAME_WORK"),
    ]


def _link(kind: str, a: str, rel: str, b: str = "", **kw: Any) -> dict[str, Any]:
    row = {"link_kind": kind, "from_id": a, "relation": rel, "to_id": b, "is_primary": "", "basis":
           "REPOSITORY_AUDIT", "curation_status": "CURATED"}
    row.update(kw)
    return row


def _link_rows() -> list[dict[str, Any]]:
    w13 = ids.work_id(sid(13))
    rows = [_link("SOURCE_WORK", sid(n), "FULL_COPY", ids.work_id(sid(n)), is_primary="true",
                  basis="BOOTSTRAP_SINGLETON") for n in (1, 2, 5, 22, 23, 42, 50, 249)]
    rows += [_link("SOURCE_WORK", sid(13), "FULL_COPY", w13, is_primary="true"),
             _link("SOURCE_WORK", sid(25), "FULL_COPY", w13, is_primary="true"),
             _link("SOURCE_WORK", sid(202), "FULL_COPY", w13, is_primary="true"),
             _link("SOURCE_WORK", sid(5), "FOREIGN_CONTENT", "", from_page_start="2", from_page_end="2",
                   is_primary="false", notes="page 2 carries the end of another article"),
             _link("WORK_WORK", ids.work_id(sid(1)), "NOT_SAME", ids.work_id(sid(2))),
             _link("SOURCE_SOURCE", sid(25), "DERIVED_FROM", sid(13)),
             _link("SOURCE_SOURCE", sid(22), "CONTAINS_COPY_OF", sid(23), basis="SHA256_IDENTITY"),
             _link("SOURCE_SOURCE", sid(5), "SHARES_PAGES_WITH", sid(1), from_page_start="1", from_page_end="1",
                   to_page_start="3", to_page_end="3")]
    return rows


# ================================================================= document objects
@dataclass
class _SourceBuild:
    src: SourceContext
    t: datetime
    run_id: str
    layout: CanonLayout
    rows: dict[str, list[Any]] = field(default_factory=lambda: {n: [] for n in (
        "documents", "pages", "blocks", "figures", "tables", "formulas", "bibliography_entries")})
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def blob(self, kind: str, tag: str, media: str = "application/json", page_id: str | None = None,
             **extra: Any) -> str:
        data = json.dumps({"synthetic": tag, "source": self.src.source_id}, sort_keys=True).encode("utf-8")
        row = put_blob(self.layout, kind, data, media, run_id=self.run_id, created_at=self.t,
                       source_id=self.src.source_id, page_id=page_id, **extra)
        self.artifacts.setdefault(row["artifact_id"], row)
        return row["artifact_id"]

    def recipe_render(self, page_id: str, dpi: int = 200) -> str:
        """A PAGE_RENDER registered NOT_STORED_REPRODUCIBLE (H-23): only the recipe and the hash are kept."""
        data = f"render-{page_id}-{dpi}".encode()
        aid = ids.artifact_id(data)
        self.artifacts.setdefault(aid, {
            "artifact_id": aid, "artifact_kind": ArtifactKind.PAGE_RENDER, "media_type": "image/png",
            "size_bytes": None, "storage_relpath": None, "retention_class": "KEEP_REFERENCED",
            "materialization": "NOT_STORED_REPRODUCIBLE", "created_by_run_id": self.run_id,
            "registered_source_id": self.src.source_id, "registered_page_id": page_id, "created_at": self.t,
            "pixel_sha256": hashlib.sha256(data).hexdigest(), "image_dpi": float(dpi),
            "recipe": {"tool": "pymupdf", "tool_version": "1.28.2", "profile": f"r{dpi}g",
                       "source_sha256": self.src.source_sha256, "page_id": page_id, "params_json": "{}"}})
        return aid


def _prod(run_id: str, extractor: str, models: tuple = (), gen: int = 1) -> ProducerContext:
    return ProducerContext(PIPELINE_VERSION, run_id, extractor, "0.1.0", config_hash({"x": extractor}),
                           config_hash({"raw": extractor}), gen).with_models(*models)


def _page(b: _SourceBuild, index: int, *, unit: str = "p", kind: str = "PDF_PAGE", page_class: str = "NATIVE_TEXT",
          status: str | None = None, blocks: list[dict] | None = None, file_layer: str = "PDF_TEXT_LAYER",
          raw: str | None = None, origin_no_text: str = "NATIVE", bbox_space: str = "PAGE_PT_TL",
          **extra: Any) -> str:
    pid = ids.page_id(b.src.source_id, unit, index)
    primary = [x for x in (blocks or []) if x["is_primary_layer"]]
    pt = page_text_v1(primary)
    layer = primary[0]["text_layer"] if pt.normalized_text else "NONE"
    text_origin = primary[0]["origin"] if pt.normalized_text else None
    if status is None:
        status = {"NATIVE": "NATIVE_OK", "EMBEDDED_OCR": "EMBEDDED_TEXT_OK", "OCR": "OCR_OK"}.get(text_origin or "",
                                                                                                    "NEEDS_REVIEW")
    origin = "DERIVED" if kind == "DOCX_RENDERED_PAGE" else (text_origin or origin_no_text)
    # a page whose text was produced by a model carries that model (§50: revision of the model)
    models = tuple(primary[0]["models"]) if text_origin == "OCR" else ()
    env = doc_envelope(b.src, _prod(b.run_id, "paginate", models), object_kind="PAGE", object_id=pid, page_id=pid,
                       origin=origin, created_at=b.t, raw_artifact_id=raw if pt.normalized_text or raw else None)
    fields = dict(page_index=index, page_kind=kind, page_class=page_class, bbox_space=bbox_space,
                  file_text_layer=file_layer, file_text_status="PRESENT_OK" if file_layer != "NONE" else "ABSENT",
                  page_status=status, primary_text_layer=layer, primary_text_origin=text_origin,
                  normalized_text=pt.normalized_text, text_sha256=pt.text_sha256, char_count=pt.char_count,
                  text_rule="page_text_v1")
    if bbox_space == "PAGE_PT_TL":
        fields.update(width_pt=595.3, height_pt=841.9, page_box="CROPBOX")
    fields.update(extra)
    b.rows["pages"].append(build_row("pages", env, **fields))
    return pid


def _block(b: _SourceBuild, pid: str | None, order: int, text: str, *, prod: ProducerContext, origin: str = "NATIVE",
           layer: str = "PDF_TEXT_LAYER", region: str = "PDF_TEXT_BLOCK", primary: bool = True,
           btype: str = "TEXT", raw: str | None = None, bbox: tuple | None = None, docx_path: str | None = None,
           **extra: Any) -> dict[str, Any]:
    if docx_path is not None:
        scope, anchor, space, box = ids.document_id(b.src.source_id), ids.xml_anchor(docx_path), "NONE", None
    elif bbox is None and region == "EPUB_ELEMENT":
        scope, anchor, space, box = pid, ids.ordinal_anchor(order, text), "NONE", None
    else:
        box = bbox or (56.0, 60.0 + 40 * order, 540.0, 90.0 + 40 * order)
        scope, anchor, space = pid, ids.bbox_anchor(*box), "PAGE_PT_TL"
    oid = ids.object_id(scope, "BLOCK", origin, region, anchor, prod.producer_key())
    env = doc_envelope(b.src, prod, object_kind="BLOCK", object_id=oid, page_id=pid, origin=origin, created_at=b.t,
                       raw_artifact_id=raw, raw_content_sha256=hashlib.sha256(text.encode()).hexdigest())
    fields = dict(region_origin=region, text_layer=layer, bbox_space=space, is_primary_layer=primary,
                  block_type=btype, reading_order=order, text=text, normalized_text=normalize_text_v1(text),
                  char_count=len(text), language="ru", docx_paragraph_path=docx_path)
    if box:
        fields.update(bbox_x0=box[0], bbox_y0=box[1], bbox_x1=box[2], bbox_y1=box[3])
    fields.update(extra)
    row = build_row("blocks", env, **fields)
    b.rows["blocks"].append(row)
    return row.model_dump()


def _bib(b: _SourceBuild, pid: str, ordinal: int, text: str, *, prod: ProducerContext, raw: str,
         doi: str | None = None, title: str | None = None, year: int | None = None) -> str:
    box = (56.0, 500.0 + 30 * ordinal, 540.0, 525.0 + 30 * ordinal)
    oid = ids.object_id(pid, "BIBLIOGRAPHY_ENTRY", "NATIVE", "PDF_TEXT_BLOCK", ids.bbox_anchor(*box),
                        prod.producer_key())
    env = doc_envelope(b.src, prod, object_kind="BIBLIOGRAPHY_ENTRY", object_id=oid, page_id=pid, origin="NATIVE",
                       created_at=b.t, raw_artifact_id=raw,
                       raw_content_sha256=hashlib.sha256(text.encode()).hexdigest())
    b.rows["bibliography_entries"].append(build_row(
        "bibliography_entries", env, region_origin="PDF_TEXT_BLOCK", text_layer="PDF_TEXT_LAYER", bbox_x0=box[0],
        bbox_y0=box[1], bbox_x1=box[2], bbox_y1=box[3], bbox_space="PAGE_PT_TL", entry_label=str(ordinal),
        ordinal_in_list=ordinal, text=text, normalized_text=normalize_text_v1(text), parsed_title=title,
        parsed_year=year, parsed_year_raw=str(year) if year else None, parsed_doi=doi, parse_method="regex-v1",
        parse_confidence=0.9, language="ru"))
    return oid


def _document(b: _SourceBuild, page_count: int, *, fmt: str = "PDF", basis: str = "PDF_PAGE_TREE", unit: str = "p",
              cls: str = "NATIVE", status: str = "COMPLETE", **extra: Any) -> None:
    env = doc_envelope(b.src, _prod(b.run_id, "inspect"), object_kind="DOCUMENT",
                       object_id=ids.document_id(b.src.source_id), origin="NATIVE", created_at=b.t)
    b.rows["documents"].append(build_row(
        "documents", env, format_detected=fmt, pagination_basis=basis, page_unit=unit, page_count=page_count,
        page_count_check=page_count, page_count_check_method="second-counter", document_class=cls,
        classifier_version="synthetic-1", processing_status=status, **extra))


def _sentences(tag: str, k: int) -> str:
    return f"Синтетический абзац {k} ({tag}): альфа бета гамма дельта эпсилон."


def build_source_objects(b: _SourceBuild, n: int, out_ids: dict[str, str]) -> tuple[str, int]:
    """Rows of one synthetic source; returns (document processing status, page count)."""
    rid = b.run_id
    native = _prod(rid, "pdf-native")
    bibp = _prod(rid, "bib-segmenter")
    if n == 1:
        for p in (1, 2, 3):
            pid = ids.page_id(b.src.source_id, "p", p)
            raw = b.blob(ArtifactKind.NATIVE_RAW, f"native-{p}", page_id=pid)
            blocks = []
            if p == 1:
                blocks.append(_block(b, pid, 1, "Синтетический колонтитул", prod=native, btype="PAGE_HEADER", raw=raw))
            if p == 3:
                blocks.append(_block(b, pid, 1, SHARED_TEXT, prod=native, raw=raw))
            else:
                blocks += [_block(b, pid, 2, _sentences(f"001 p{p}", 1), prod=native, raw=raw),
                           _block(b, pid, 3, _sentences(f"001 p{p}", 2), prod=native, raw=raw)]
            _page(b, p, blocks=blocks, raw=raw, render_artifact_id=b.blob(ArtifactKind.PAGE_RENDER, f"r-{p}",
                  "image/png", page_id=pid), render_dpi=200)
            if p == 2:
                out_ids["exact_doi_entry"] = _bib(b, pid, 1, "1. Дельтин Д.Д. Синтетическая статья выпуска // "
                                                  "Вестник синтетики. 2022. doi:" + SYN_DOI.format("005"),
                                                  prod=bibp, raw=raw, doi=SYN_DOI.format("005"), year=2022)
                out_ids["candidate_entry"] = _bib(b, pid, 2, "2. Kappov K. Synthetic geophysics textbook. 2020.",
                                                  prod=bibp, raw=raw, title="Synthetic geophysics textbook", year=2020)
        out_ids["page_001_3"] = ids.page_id(b.src.source_id, "p", 3)
        _document(b, 3, file_meta_title="synthetic 001")
        return "COMPLETE", 3
    if n == 2:
        emb = _prod(rid, "pdf-embedded-layer")
        ocr = _prod(rid, "glm-ocr", (LAYOUT, GLM))
        for p in (1, 2):
            pid = ids.page_id(b.src.source_id, "p", p)
            raw_emb = b.blob(ArtifactKind.NATIVE_RAW, f"embedded-{p}", page_id=pid)
            blocks = [_block(b, pid, 1, f"Встроенный слой скана {p}: альфа бета", prod=emb, origin="EMBEDDED_OCR",
                             layer="PDF_EMBEDDED_OCR_LAYER", primary=(p == 1), raw=raw_emb,
                             embedded_layer_evidence="HIDDEN_TEXT_LAYER", text_layer_producer="SynthScan 1.0",
                             quality_flags=["EMBEDDED_TEXT_LAYER", "HIDDEN_TEXT_LAYER"])]
            extra: dict[str, Any] = {"embedded_layer_evidence": "HIDDEN_TEXT_LAYER",
                                     "text_layer_producer": "SynthScan 1.0", "file_text_char_count": 40}
            raw = raw_emb
            if p == 2:
                raw_lay = b.blob(ArtifactKind.LAYOUT_RAW, f"layout-{p}", page_id=pid, producer_signature=None)
                raw_ocr = b.blob(ArtifactKind.OCR_RAW, f"ocr-{p}", page_id=pid,
                                 producer_signature=hashlib.sha256(b"call-002-2").hexdigest(), attempt=1)
                blocks.append(_block(b, pid, 1, "Распознанный текст скана 2: альфа бета гамма", prod=ocr,
                                     origin="OCR", layer="GLM_OCR", region="LAYOUT_MODEL", raw=raw_ocr,
                                     bbox=(60.0, 80.0, 530.0, 120.0), recognition_confidence=0.93,
                                     raw_artifacts=[{"role": "LAYOUT_DETECTIONS", "artifact_id": raw_lay},
                                                    {"role": "OCR_RESPONSE", "artifact_id": raw_ocr}]))
                extra.update(ocr_status="DONE", ocr_char_count=44, native_ocr_cer=0.12,
                             layout_raw_artifact_id=raw_lay, ocr_raw_artifact_id=raw_ocr)
                raw = raw_ocr
                out_ids["ocr_block"] = blocks[-1]["object_id"]
            else:
                out_ids["embedded_block"] = blocks[0]["object_id"]
            _page(b, p, page_class="RASTER_SCAN", file_layer="PDF_EMBEDDED_OCR_LAYER", blocks=blocks, raw=raw,
                  page_route="OCR_OPTIONAL", **extra)
        _document(b, 2, cls="SCANNED_WITH_TEXT_LAYER", text_layer_producer="SynthScan 1.0",
                  quality_flags=["LEADING_BYTES_BEFORE_HEADER"])
        return "COMPLETE", 2
    if n == 5:
        p1 = ids.page_id(b.src.source_id, "p", 1)
        p2 = ids.page_id(b.src.source_id, "p", 2)
        raw1 = b.blob(ArtifactKind.NATIVE_RAW, "native-1", page_id=p1)
        raw2 = b.blob(ArtifactKind.NATIVE_RAW, "native-2", page_id=p2)
        _page(b, 1, blocks=[_block(b, p1, 1, SHARED_TEXT, prod=native, raw=raw1)], raw=raw1)
        out_ids["host_entry"] = _bib(b, p1, 1, "1. Альфаев А.А. Синтетическая статья о мульде. 2020. doi:"
                                     + SYN_DOI.format("001"), prod=bibp, raw=raw1, doi=SYN_DOI.format("001"),
                                     year=2020)
        _page(b, 2, blocks=[_block(b, p2, 1, "Окончание чужой синтетической статьи.", prod=native, raw=raw2)],
              raw=raw2, quality_flags=["FOREIGN_WORK_CONTENT"])
        out_ids["foreign_entry"] = _bib(b, p2, 1, "1. Альфаев А.А. Синтетическая статья о мульде. 2020. doi:"
                                        + SYN_DOI.format("001"), prod=bibp, raw=raw2, doi=SYN_DOI.format("001"),
                                        year=2020)
        out_ids["foreign_page"] = p2
        _document(b, 2)
        return "COMPLETE", 2
    if n == 23:
        docx = _prod(rid, "docx-xml")
        render = b.blob(ArtifactKind.DOCX_RENDERED_PDF, "render-pdf", "application/pdf")
        xml_raw = b.blob(ArtifactKind.NATIVE_RAW, "document-xml")
        for p in (1, 2):
            pid = ids.page_id(b.src.source_id, "r", p)
            blocks = [_block(b, pid, k, _sentences(f"023 r{p}", k), prod=docx, layer="DOCX_XML",
                             region="DOCX_ELEMENT", raw=xml_raw, docx_path=f"/w:body/w:p[{10 * p + k}]")
                      for k in (1, 2)]
            if p == 1:
                out_ids["docx_block"] = blocks[0]["object_id"]
            _page(b, p, unit="r", kind="DOCX_RENDERED_PAGE", page_class="RENDERED_FROM_SOURCE", file_layer="DOCX_XML",
                  blocks=blocks, raw=render, page_box="RENDER_PDF")
        doc_scope = ids.document_id(b.src.source_id)
        oid = ids.object_id(doc_scope, "FORMULA", "NATIVE", "DOCX_ELEMENT", ids.xml_anchor("/w:body/w:p[13]/m:oMath[1]"),
                            docx.producer_key())
        env = doc_envelope(b.src, docx, object_kind="FORMULA", object_id=oid, page_id=ids.page_id(b.src.source_id,
                           "r", 1), origin="NATIVE", created_at=b.t, raw_artifact_id=xml_raw)
        b.rows["formulas"].append(build_row(
            "formulas", env, region_origin="DOCX_ELEMENT", text_layer="DOCX_XML", bbox_space="NONE",
            docx_paragraph_path="/w:body/w:p[13]/m:oMath[1]", formula_kind="DISPLAY", equation_label="(2.1)",
            recognition_method="NATIVE_OMML", raw_format="OMML", raw_output="<m:oMath><m:r>x</m:r></m:oMath>",
            normalized_latex="x"))
        out_ids["docx_formula"] = oid
        _document(b, 2, fmt="DOCX", basis="DOCX_PINNED_RENDER", unit="r", cls="WORD_DOCX",
                  pagination_render_profile="libreoffice-24.2.7.2;fonts=synthetic", pagination_artifact_id=render)
        return "COMPLETE", 2
    if n == 25:
        p1 = ids.page_id(b.src.source_id, "p", 1)
        p2 = ids.page_id(b.src.source_id, "p", 2)
        raw1 = b.blob(ArtifactKind.NATIVE_RAW, "native-1", page_id=p1)
        raw2 = b.blob(ArtifactKind.NATIVE_RAW, "native-2", page_id=p2)
        lay = b.blob(ArtifactKind.LAYOUT_RAW, "layout-1", page_id=p1)
        ocr_raw = b.blob(ArtifactKind.OCR_RAW, "ocr-formula-1", page_id=p1,
                         producer_signature=hashlib.sha256(b"call-025-1").hexdigest(), attempt=1)
        cap = _block(b, p1, 1, "Рис. 1. Синтетическая схема мульды", prod=native, btype="CAPTION", raw=raw1)
        tcap = _block(b, p1, 2, "Таблица 1. Синтетические параметры", prod=native, btype="CAPTION", raw=raw1)
        body = _block(b, p1, 3, _sentences("025 p1", 1), prod=native, raw=raw1)
        _page(b, 1, page_class="VECTOR", blocks=[cap, tcap, body], raw=raw1, layout_raw_artifact_id=lay,
              render_artifact_id=b.recipe_render(p1), render_dpi=200)
        _page(b, 2, blocks=[_block(b, p2, 1, _sentences("025 p2", 1), prod=native, raw=raw2)], raw=raw2)
        layp = _prod(rid, "layout-figures", (LAYOUT,))
        fbox = (100.0, 300.0, 400.0, 500.0)
        fid = ids.object_id(p1, "FIGURE", "NATIVE", "LAYOUT_MODEL", ids.bbox_anchor(*fbox), layp.producer_key())
        env = doc_envelope(b.src, layp, object_kind="FIGURE", object_id=fid, page_id=p1, origin="NATIVE",
                           created_at=b.t, raw_artifact_id=lay,
                           raw_artifacts=[{"role": "LAYOUT_DETECTIONS", "artifact_id": lay}])
        b.rows["figures"].append(build_row(
            "figures", env, region_origin="LAYOUT_MODEL", text_layer="PDF_TEXT_LAYER", bbox_x0=fbox[0],
            bbox_y0=fbox[1], bbox_x1=fbox[2], bbox_y1=fbox[3], bbox_space="PAGE_PT_TL", figure_label="Рис. 1",
            caption=cap["text"], caption_normalized=cap["normalized_text"], caption_block_id=cap["object_id"],
            layout_class="VECTOR_GRAPHICS", layout_score=0.91,
            image_artifact_id=b.blob(ArtifactKind.FIGURE_CROP, "fig-1", "image/png", page_id=p1), image_dpi=200,
            vector_artifacts=[{"format": "PATHS_JSON", "artifact_id": b.blob(ArtifactKind.VECTOR_PATHS_JSON,
                                                                             "paths-1", page_id=p1)},
                              {"format": "SVG", "artifact_id": b.blob(ArtifactKind.VECTOR_SVG, "svg-1",
                                                                      "image/svg+xml", page_id=p1)}]))
        out_ids["figure"] = fid
        tabp = _prod(rid, "table-finder")
        tbox = (60.0, 520.0, 540.0, 640.0)
        raw_tab = b.blob(ArtifactKind.NATIVE_RAW, "find-tables-1", page_id=p1)
        tid = ids.object_id(p1, "TABLE", "NATIVE", "NATIVE_TABLE_FINDER", ids.bbox_anchor(*tbox), tabp.producer_key())
        html = "<table><tr><th>параметр</th><th>значение</th></tr><tr><td>альфа</td><td>1.5</td></tr></table>"
        env = doc_envelope(b.src, tabp, object_kind="TABLE", object_id=tid, page_id=p1, origin="NATIVE",
                           created_at=b.t, raw_artifact_id=raw_tab,
                           raw_content_sha256=hashlib.sha256(html.encode()).hexdigest(),
                           raw_artifacts=[{"role": "NATIVE_TABLE_FINDER", "artifact_id": raw_tab}])
        b.rows["tables"].append(build_row(
            "tables", env, region_origin="NATIVE_TABLE_FINDER", text_layer="PDF_TEXT_LAYER", bbox_x0=tbox[0],
            bbox_y0=tbox[1], bbox_x1=tbox[2], bbox_y1=tbox[3], bbox_space="PAGE_PT_TL", table_label="Таблица 1",
            caption=tcap["text"], caption_normalized=tcap["normalized_text"], caption_block_id=tcap["object_id"],
            recognition_method="NATIVE_FIND_TABLES", raw_format="HTML", raw_output=html, n_rows=2, n_cols=2,
            header_rows=1, cells=[{"row": 0, "col": 0, "is_header": True, "text": "параметр"},
                                  {"row": 0, "col": 1, "is_header": True, "text": "значение"},
                                  {"row": 1, "col": 0, "text": "альфа"}, {"row": 1, "col": 1, "text": "1.5"}],
            normalized_text="параметр | значение\nальфа | 1.5",
            image_artifact_id=b.blob(ArtifactKind.TABLE_CROP, "tab-1", "image/png", page_id=p1), image_dpi=300))
        out_ids["table"] = tid
        ocr = _prod(rid, "glm-ocr", (LAYOUT, GLM))
        mbox = (200.0, 650.0, 400.0, 680.0)
        mid = ids.object_id(p1, "FORMULA", "OCR", "LAYOUT_MODEL", ids.bbox_anchor(*mbox), ocr.producer_key())
        env = doc_envelope(b.src, ocr, object_kind="FORMULA", object_id=mid, page_id=p1, origin="OCR",
                           created_at=b.t, raw_artifact_id=ocr_raw,
                           raw_content_sha256=hashlib.sha256(b"eta = eta_m (1 - e^{-t/T})").hexdigest(),
                           raw_artifacts=[{"role": "LAYOUT_DETECTIONS", "artifact_id": lay},
                                          {"role": "OCR_RESPONSE", "artifact_id": ocr_raw}])
        b.rows["formulas"].append(build_row(
            "formulas", env, region_origin="LAYOUT_MODEL", text_layer="GLM_OCR", bbox_x0=mbox[0], bbox_y0=mbox[1],
            bbox_x1=mbox[2], bbox_y1=mbox[3], bbox_space="PAGE_PT_TL", formula_kind="DISPLAY", equation_label="(1)",
            recognition_method="OCR_GLM", raw_format="LATEX", raw_output="$$\\eta = \\eta_m (1 - e^{-t/T})$$",
            normalized_latex="\\eta = \\eta_m (1 - e^{-t/T})", latex_parse_ok=True, native_glyph_text="η=ηm(1-e-t/T)",
            recognition_confidence=0.88,
            image_artifact_id=b.blob(ArtifactKind.FORMULA_CROP, "formula-1", "image/png", page_id=p1), image_dpi=300))
        out_ids["formula"] = mid
        _document(b, 2, cls="MIXED")
        return "COMPLETE", 2
    if n == 42:
        djvu = _prod(rid, "djvu-text")
        p1 = ids.page_id(b.src.source_id, "p", 1)
        raw1 = b.blob(ArtifactKind.NATIVE_RAW, "djvu-txt-1", page_id=p1)
        blk = _block(b, p1, 1, "Текстовый слой DjVu: альфа бета гамма", prod=djvu, origin="EMBEDDED_OCR",
                     layer="DJVU_EMBEDDED_OCR_LAYER", region="DJVU_TEXT_ZONE", raw=raw1,
                     embedded_layer_evidence="DJVU_TXT")
        _page(b, 1, kind="DJVU_PAGE", page_class="RASTER_SCAN", file_layer="DJVU_EMBEDDED_OCR_LAYER", blocks=[blk],
              raw=raw1, page_box="DJVU_IMAGE", native_dpi=300.0, embedded_layer_evidence="DJVU_TXT")
        _page(b, 2, kind="DJVU_PAGE", page_class="RASTER_SCAN", file_layer="NONE", status="FAILED",
              page_box="DJVU_IMAGE", native_dpi=300.0, ocr_status="FAILED")
        out_ids["failed_page"] = ids.page_id(b.src.source_id, "p", 2)
        _document(b, 2, fmt="DJVU", basis="DJVU_PAGE_ORDER", cls="SCANNED_PARTIAL_TEXT_LAYER", status="PARTIAL",
                  shared_component_count=1)
        return "PARTIAL", 2
    if n == 202:
        p1 = ids.page_id(b.src.source_id, "p", 1)
        raw1 = b.blob(ArtifactKind.NATIVE_RAW, "native-1", page_id=p1)
        _page(b, 1, blocks=[_block(b, p1, 1, _sentences("202 p1", 1), prod=native, raw=raw1)], raw=raw1)
        _document(b, 1)
        return "COMPLETE", 1
    if n == 249:
        epub = _prod(rid, "epub-xhtml")
        for s in (1, 2):
            pid = ids.page_id(b.src.source_id, "s", s)
            raw = b.blob(ArtifactKind.NATIVE_RAW, f"xhtml-{s}", page_id=pid)
            blocks = [_block(b, pid, k, f"Synthetic spine item {s}, paragraph {k}: alpha beta gamma.", prod=epub,
                             layer="EPUB_XHTML", region="EPUB_ELEMENT", raw=raw) for k in (1, 2)]
            if s == 1:
                out_ids["epub_block"] = blocks[0]["object_id"]
            _page(b, s, unit="s", kind="EPUB_SPINE_ITEM", page_class="REFLOWABLE", file_layer="EPUB_XHTML",
                  blocks=blocks, raw=raw, bbox_space="NONE", spine_href=f"OEBPS/ch{s:02d}.xhtml")
        _document(b, 2, fmt="EPUB", basis="EPUB_SPINE", unit="s", cls="REFLOWABLE_EPUB")
        return "COMPLETE", 2
    raise ValueError(f"no synthetic objects for source {n}")


# ================================================================= the canon
@dataclass
class SyntheticCanon:
    root: Path
    layout: CanonLayout
    staging: CanonLayout
    private_root: Path
    snapshot_id: str
    manifest: dict[str, Any]
    report: dict[str, Any]
    ids: dict[str, str]
    duckdb_path: Path | None = None

    def connect(self):
        """In-memory DuckDB over CURRENT with all views and macros."""
        from vkm_corpus.duckdb.build import open_snapshot

        return open_snapshot(self.layout, self.manifest)


PROCESSED = (1, 2, 5, 23, 25, 42, 202, 249)


def synthetic_canon(root: str | Path, *, with_duckdb: bool = False, now: datetime | None = None) -> SyntheticCanon:
    """Build the synthetic CANONICAL root at ``root`` (fresh directory) and return handles and notable IDs."""
    root = Path(root)
    t0 = (now or T0).astimezone(timezone.utc)
    private = root.with_name(root.name + "_private")
    staging = init_root(root.with_name(root.name + "_staging"), RootKind.STAGING)
    paths = write_private_tree(private)
    import_registry(staging, private, work_registry=paths["work_registry"], coverage_path=paths["coverage"],
                    now=t0, code_revision="synthetic")
    notable: dict[str, str] = {}
    t1 = t0 + timedelta(hours=1)
    run = RunRecorder(staging, run_kind="EXTRACTION", cli_command="vkm-corpus run extract --all",
                      host_role="WORKSTATION", config={"synthetic": True}, code_revision="synthetic",
                      models=[{**LAYOUT, "backend": "transformers"}, {**GLM, "backend": "vllm"}])
    run.start(t1)
    t_rows = t1 + timedelta(minutes=5)
    reg = {r["resource_id"]: r for r in _register_rows(private)}
    steps, errors = [], []
    for n in PROCESSED:
        s = sid(n)
        src = SourceContext(s, reg[s]["sha256"], tuple(reg[s]["_scope"].scopes), reg[s]["evidence_scope"],
                            reg[s]["_scope"].mapping.value)
        b = _SourceBuild(src, t_rows, run.run_id, staging)
        status, pages = build_source_objects(b, n, notable)
        with acquire_lease(staging, s, run.run_id):
            res = commit_source(staging, source_id=s, source_sha256=src.source_sha256, run_id=run.run_id,
                                tables=b.rows, artifact_rows=list(b.artifacts.values()),
                                document_processing_status=status, page_count=pages, committed_at=t_rows,
                                code_revision="synthetic")
        run.note_commit(res.commit_id)
        step = ids.step_id(run.run_id, s, None, "COMMIT", 1)
        steps.append(_step(run.run_id, step, s, None, "COMMIT", "EXECUTED",
                           "PARTIAL" if status == "PARTIAL" else "NATIVE_OK", src.source_sha256, t_rows,
                           commit_id=res.commit_id))
        if n == 42:
            fstep = ids.step_id(run.run_id, s, notable["failed_page"], "RENDER", 1)
            steps.append(_step(run.run_id, fstep, s, notable["failed_page"], "RENDER", "EXECUTED", "FAILED",
                               src.source_sha256, t_rows, page_index=2, reason_code="RENDER_FAILED"))
            errors.append({"schema_version": "0.1.0", "error_id": ids.error_id(run.run_id, fstep, "RENDER_FAILED", 1),
                           "processing_run_id": run.run_id, "step_id": fstep, "source_id": s,
                           "page_id": notable["failed_page"], "page_index": 2, "stage": "RENDER",
                           "code": "RENDER_FAILED", "tool": "ddjvu", "tool_version": "3.5.30",
                           "message": "synthetic render failure", "retryable": True, "created_at": t_rows})
    for n in (13, 22):
        s = sid(n)
        steps.append(_step(run.run_id, ids.step_id(run.run_id, s, None, "INSPECT", 1), s, None, "INSPECT",
                           "SKIPPED_BY_POLICY", "SKIPPED_BY_REGISTER", reg[s]["sha256"], t_rows,
                           reason_code="ARCHIVE_DELETED_AFTER_ASSEMBLY" if n == 13 else "RETIRED_NOT_EVIDENCE"))
    run.add_steps(steps)
    run.add_errors(errors)
    run.end("PARTIAL", now=t1 + timedelta(minutes=30), log_bytes=b'{"msg": "synthetic run log"}\n',
            counters={"n_sources_planned": len(PROCESSED) + 2, "n_steps_executed": len(steps),
                      "n_steps_failed": len(errors), "n_model_calls": 2})
    # a crashed run: START and one journal part, no END (visible in the snapshot, H-11)
    crash = RunRecorder(staging, run_kind="EXTRACTION", cli_command="vkm-corpus run extract --source VKM-SRC-050",
                        host_role="WORKSTATION", config={"synthetic": "crash"}, code_revision="synthetic")
    crash.start(t1 + timedelta(minutes=40))
    crash.add_steps([_step(crash.run_id, ids.step_id(crash.run_id, sid(50), None, "INSPECT", 1), sid(50), None,
                           "INSPECT", "EXECUTED", "NOT_PROCESSED", reg[sid(50)]["sha256"],
                           t1 + timedelta(minutes=41))])
    notable["crashed_run"] = crash.run_id
    notable["extraction_run"] = run.run_id
    canonical = publish_to_canonical(staging, root)
    adm = admit(canonical, now=t1 + timedelta(hours=1))
    if adm["rejected"] or adm["pending"]:
        raise RuntimeError(f"synthetic admission failed: {adm}")
    res = build_snapshot(canonical, now=t1 + timedelta(hours=1), options=ValidationOptions(deep=True))
    if not res["current_moved"]:
        fails = [c for c in res["report"]["checks"] if c["status"] == "FAIL"]
        raise RuntimeError(f"synthetic snapshot did not validate: {json.dumps(fails, ensure_ascii=False)[:4000]}")
    from vkm_corpus.parquet.reader import load_manifest

    manifest = load_manifest(canonical, res["snapshot_id"])
    duck = None
    if with_duckdb:
        from vkm_corpus.duckdb.build import build_duckdb

        build_duckdb(canonical)
        duck = canonical.duckdb_file
    return SyntheticCanon(root, canonical, staging, private, res["snapshot_id"], manifest, res["report"], notable,
                          duck)


def _step(run_id: str, step_id: str, source_id: str, page_id: str | None, stage: str, outcome: str, status: str,
          source_sha256: str, t: datetime, **extra: Any) -> dict[str, Any]:
    return {"schema_version": "0.1.0", "step_id": step_id, "processing_run_id": run_id, "source_id": source_id,
            "page_id": page_id, "stage": stage, "attempt": 1, "outcome": outcome, "status": status,
            "stage_signature": stage_signature(source_sha256=source_sha256, stage=stage,
                                               pipeline_version=PIPELINE_VERSION, extractor_id="synthetic",
                                               extractor_version="0.1.0", stage_config_hash=config_hash({})),
            "source_sha256": source_sha256, "pipeline_version": PIPELINE_VERSION, "extractor_id": "synthetic",
            "extractor_version": "0.1.0", "config_hash": config_hash({}), "started_at": t, "finished_at": t,
            "duration_ms": 0, "host_role": "WORKSTATION", **extra}


def _register_rows(private: Path) -> list[dict[str, Any]]:
    from vkm_corpus.contracts.site_scope import map_site_scope
    from vkm_corpus.registry.sources import load_register

    rows, _ = load_register(private / "00_registry" / "SOURCE_REGISTER.csv")
    for r in rows:
        r["_scope"] = map_site_scope(r["evidence_scope"])
    return rows


def publish_to_canonical(staging: CanonLayout, root: Path) -> CanonLayout:
    """Copy immutable files of a STAGING root into a CANONICAL root (blobs and partitions first, markers last) —
    what ``rsync`` does between WORKSTATION and CORE."""
    canonical = init_root(root, RootKind.CANONICAL)
    shutil.copytree(staging.artifacts, canonical.artifacts, dirs_exist_ok=True)
    for child in sorted(staging.canonical.iterdir()):
        if child.name in ("_commits", "_runs", "_leases") or not child.is_dir():
            continue
        shutil.copytree(child, canonical.canonical / child.name, dirs_exist_ok=True)
    for marker_dir in ("_runs", "_commits"):
        src = staging.canonical / marker_dir
        if src.is_dir():
            shutil.copytree(src, canonical.canonical / marker_dir, dirs_exist_ok=True)
    return canonical
