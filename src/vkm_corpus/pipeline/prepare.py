"""Phase 1 — prepare one source (CPU; runs in its own subprocess): inspect, paginate by two methods, native extraction
of every page (NATIVE_RAW artifacts), classification and route.

The result is a *prep summary* (JSON, one per source and prepare signature) kept in the staging cache: pagination,
per-page geometry, class, flags, route and the NATIVE_RAW artifact ids. Later phases read only this summary and the
artifacts. Nothing here calls a model.
"""
from __future__ import annotations

import json
import os
import re
import time
import traceback
import zlib
from pathlib import Path, PureWindowsPath
from typing import Any

from vkm_corpus.artifacts.store import ArtifactStore, canonical_json_bytes
from vkm_corpus.extract import classify as cls
from vkm_corpus.extract.detect import inspect_file
from vkm_corpus.extract.model import SourceInput
from vkm_corpus.pipeline.cache import StageCache
from vkm_corpus.pipeline.config import EXTRACTOR_VERSIONS, GENERATIONS, PipelineConfig
from vkm_corpus.versions import PIPELINE_VERSION

PREP_SCHEMA = "vkm.prep/1"
NATIVE_FIDELITY_RULE = "native_fidelity_v2"


def _sig(**kw: Any) -> str:
    from vkm_corpus.contracts.signatures import stage_signature

    return stage_signature(**kw)


def _cfg_hash(d: dict[str, Any]) -> str:
    from vkm_corpus.contracts.signatures import config_hash

    return config_hash(d)


def docx_grid_identity(source: SourceInput) -> dict[str, Any]:
    """Exact child-stage identity of the native Word grid adapter, independent of filename.

    This is cache freshness/provenance for grid parsing, not full object extraction
    identity or scientific admission. No source path is read to compute it.
    """
    from lxml import etree
    from vkm_corpus.extract.docx import DOCX_GRID_RULE, MAX_DOCX_GRID_COLUMNS, MAX_DOCX_GRID_POSITIONS

    libraries = {"lxml": ".".join(map(str, etree.LXML_VERSION)),
                 "libxml2": ".".join(map(str, etree.LIBXML_VERSION))}
    limits = {"max_columns": MAX_DOCX_GRID_COLUMNS, "max_grid_positions": MAX_DOCX_GRID_POSITIONS}
    signature = _sig(source_sha256=source.sha256, stage="NATIVE_TEXT", pipeline_version=PIPELINE_VERSION,
        extractor_id="docx-xml", extractor_version=EXTRACTOR_VERSIONS["docx-xml"],
        extraction_generation=GENERATIONS["docx-xml"],
        stage_config_hash=_cfg_hash({"scope": "DOCX_NATIVE_GRID", "rule": DOCX_GRID_RULE, "libraries": libraries,
                                     "limits": limits}))
    return {"docx_grid_rule": DOCX_GRID_RULE, "docx_grid_signature": signature,
            "docx_grid_libraries": libraries, "docx_grid_limits": limits}


def docx_grid_current(value: dict[str, Any], source: SourceInput) -> bool:
    """Fail closed on old/missing/forged DOCX child-stage metadata, including renamed packages."""
    if not isinstance(value, dict) or value.get("source_id") != source.source_id or value.get("source_sha256") != source.sha256:
        return False
    return all(value.get(key) == expected for key, expected in docx_grid_identity(source).items())


class _ReadOnlyNativeStore:
    """Artifact reads without ArtifactStore's directory-creating constructor."""
    find = ArtifactStore.find
    read_bytes = ArtifactStore.read_bytes
    read_json = ArtifactStore.read_json

    def __init__(self, root: Path):
        self.root = Path(root)


def docx_grid_cache_current(prep: dict[str, Any], source: SourceInput, *, store=None, data_root=None) -> bool:
    """Fresh native child metadata in both summary and hash-verified artifact; no filesystem writes."""
    if (not isinstance(prep, dict) or (prep.get("inspect") or {}).get("file_format") != "DOCX"
            or not docx_grid_current(prep, source)):
        return False
    aid = prep.get("document_raw_artifact_id")
    if not isinstance(aid, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", aid):
        return False
    if store is None:
        if data_root is None:
            return False
        store = _ReadOnlyNativeStore(Path(data_root) / "artifacts")
    try:
        raw = store.read_json(aid)
        return (isinstance(raw, dict) and raw.get("schema") == "vkm.native_raw.docx_document/1"
                and docx_grid_current(raw, source))
    except (KeyError, ValueError, FileNotFoundError, RuntimeError, OSError, EOFError, zlib.error):
        return False


def source_format_admitted(cfg: PipelineConfig, source: SourceInput, prep: dict[str, Any], *,
                           _fresh_source_identity: dict[str, Any] | None = None) -> bool:
    """Production cache reuse requires the actual admitted source format.

    The private keyword is only for the current source-owned planner's fresh admission result;
    never populate it from prep/cache/manifest JSON. Direct consumers independently admit bytes.
    This is a predicate, not a new PREPARE/commit/OCR signature component. Exploratory readers
    retain their historical diagnostic behavior when original files are unavailable.
    """
    if cfg.profile != "production":
        return True
    from vkm_corpus.registry.sources import RegisterError, fresh_source_identity

    actual = _fresh_source_identity
    if actual is None:
        actual = fresh_source_identity(cfg.resources_root, source.canonical_path, source.sha256, source.size_bytes)
    if (actual.get("canonical_path") != source.canonical_path or actual.get("sha256") != source.sha256
            or actual.get("size_bytes") != source.size_bytes or actual.get("verification") != "FRESH_SHA256"
            or not isinstance(actual.get("file_format"), str)):
        raise RegisterError("fresh source format identity does not match selected source")
    inspection = prep.get("inspect")
    return (actual["file_format"] in {"PDF", "DJVU", "EPUB", "DOCX"}
            and isinstance(inspection, dict) and prep.get("source_id") == source.source_id
            and prep.get("source_sha256") == source.sha256
            and inspection.get("file_format") == actual["file_format"])


def library_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        from vkm_corpus.extract.pdf_native import pdfium_version, pymupdf_version

        out["pymupdf"] = pymupdf_version()
        out["pypdfium2"] = pdfium_version()
    except Exception:  # noqa: BLE001
        pass
    try:
        from vkm_corpus.extract.djvu import djvulibre_version

        out["djvulibre"] = djvulibre_version()
    except Exception:  # noqa: BLE001
        pass
    return out


def prepare_signature(cfg: PipelineConfig, source: SourceInput, extra: dict[str, Any] | None = None) -> str:
    conf = {"native": cfg.stage_config("NATIVE_TEXT"), "classify": cfg.stage_config("CLASSIFY"),
            "libraries": library_versions(), "spread_aspect": cfg.spread_aspect,
            "native_fidelity_rule": NATIVE_FIDELITY_RULE, **(extra or {})}
    return _sig(source_sha256=source.sha256, stage="PREPARE", pipeline_version=PIPELINE_VERSION,
                extractor_id="vkm-pipeline", extractor_version=EXTRACTOR_VERSIONS["vkm-pipeline"],
                stage_config_hash=_cfg_hash(conf))


def prep_path(data_root: Path, source_id: str, signature: str) -> Path:
    return Path(data_root) / "cache" / "prep" / source_id / f"{signature}.json"


def load_prep(data_root: Path, source_id: str, signature: str) -> dict[str, Any] | None:
    p = prep_path(data_root, source_id, signature)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _page_id(source_id: str, unit: str, index: int) -> str:
    return f"{source_id}:{unit}{index:04d}"


def _err(code: str, stage: str, exc: BaseException | str, page_index: int | None = None, retryable: bool = False,
         tool: str = "vkm_corpus") -> dict[str, Any]:
    msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return {"code": code, "stage": stage, "message": str(msg)[:500], "retryable": retryable, "page_index": page_index,
            "tool": tool, "exception_type": None if isinstance(exc, str) else type(exc).__name__}


# ---------------------------------------------------------------------------------------------------- formats
def _spreads(pages: list[dict[str, Any]], aspect: float) -> bool:
    ws = [(p.get("width_pt") or 0, p.get("height_pt") or 0) for p in pages if p.get("width_pt")]
    if not ws:
        return False
    wide = sum(1 for w, h in ws if h > 0 and w / h >= aspect)
    return wide / len(ws) >= 0.8


def _prepare_pdf(cfg: PipelineConfig, store: ArtifactStore, src: SourceInput, path: Path,
                 prep: dict[str, Any]) -> None:
    from vkm_corpus.extract import pages as pg
    from vkm_corpus.extract import pdf_native

    pag = pg.paginate_pdf(path)
    prep["pagination"] = pag.__dict__
    doc = pdf_native.open_pdf(path)
    n_read = doc.page_count
    try:
        info = pdf_native.document_info(doc, path)
        rec = store.put_json(info, "NATIVE_RAW", compress=True, source_id=src.source_id)
        prep["document_raw_artifact_id"] = rec.artifact_id
        md = info.get("metadata") or {}
        prep["document"] = {"is_encrypted": info.get("is_encrypted"), "has_native_page_labels":
                            bool(info.get("page_label_rules")), "metadata": md,
                            "producer": md.get("producer"), "creator": md.get("creator")}
        font_cache: dict[int, tuple[bool, str, str]] = {}
        light = os.environ.get("VKM_PREP_LIGHT") == "1"
        for i in range(doc.page_count):
            idx = i + 1
            row: dict[str, Any] = {"page_index": idx, "page_kind": "PDF_PAGE"}
            try:
                t0 = time.perf_counter()
                npg = pdf_native.extract_page(doc, i, font_cache, with_drawings_count=not light)
                r = store.put_json(npg.raw, "NATIVE_RAW", compress=True, source_id=src.source_id,
                                   page_id=_page_id(src.source_id, "p", idx))
                c, flags = cls.classify_pdf_page(npg.features, cfg.classifier)
                route, reason = cls.route_for(c, flags)
                f = npg.features
                row.update({"width_pt": npg.raw["width_pt"], "height_pt": npg.raw["height_pt"],
                            "rotation": npg.raw["rotation"], "label": npg.raw.get("label"),
                            "native_raw_artifact_id": r.artifact_id, "plain_text_sha256": npg.raw["plain_text_sha256"],
                            "page_class": c, "class_flags": flags, "route": route, "route_reason": reason,
                            "native_chars": f.get("chars"), "invisible_chars": f.get("chars_invisible"),
                            "img_cov": f.get("img_cov"), "img_main_dpi": f.get("img_main_dpi"),
                            "img_main_bpc": f.get("img_main_bpc"), "n_paths": f.get("n_paths"),
                            "n_images": f.get("n_images"), "t_ms": int((time.perf_counter() - t0) * 1000)})
                if light:
                    row["class_flags"] = flags + ["PATHS_UNKNOWN"]
            except Exception as exc:  # noqa: BLE001 - the page row stays, with an error
                row.update({"route": "FAILED", "error": _err("NATIVE_EXTRACT_FAILED", "NATIVE_TEXT", exc, idx,
                                                              tool="pymupdf")})
                try:  # geometry from the page tree, if the page object itself is readable
                    r = doc[i].rect
                    row.update({"width_pt": round(float(r.width), 3), "height_pt": round(float(r.height), 3),
                                "rotation": int(doc[i].rotation)})
                except Exception:  # noqa: BLE001
                    pass
            prep["pages"].append(row)
    finally:
        doc.close()
    if pag.count != n_read:
        prep["errors"].append(_err("PAGECOUNT_MISMATCH", "PAGINATE", "page tree length changed while reading"))


def _prepare_djvu(cfg: PipelineConfig, store: ArtifactStore, src: SourceInput, path: Path,
                  prep: dict[str, Any]) -> None:
    from vkm_corpus.extract import djvu
    from vkm_corpus.extract import pages as pg

    pag = pg.paginate_djvu(path)
    prep["pagination"] = pag.__dict__
    struct = djvu.iff_structure(path)
    try:
        layers = djvu.text_layer_all(path)
        meta = djvu.metadata(path)
    except djvu.DjvuToolMissing as exc:
        prep["errors"].append(_err("DECODER_MISSING", "NATIVE_TEXT", exc, retryable=True, tool="djvulibre"))
        layers, meta = {}, {}
    rec = store.put_json({"schema": "vkm.native_raw.djvu_document/1", "top_form": struct.top_form,
                          "bundled": struct.bundled, "dirm_files": struct.dirm_files,
                          "shared_components": struct.shared_components, "component_kinds": struct.component_kinds,
                          "djvulibre": djvu.djvulibre_version(), **meta}, "NATIVE_RAW", compress=True,
                         source_id=src.source_id)
    prep["document_raw_artifact_id"] = rec.artifact_id
    prep["document"] = {"is_encrypted": False, "has_native_page_labels": None, "metadata": {}}
    for idx in range(1, pag.count + 1):
        row: dict[str, Any] = {"page_index": idx, "page_kind": "DJVU_PAGE"}
        info = struct.pages[idx - 1] if idx <= len(struct.pages) else None
        try:
            if info is None or not info.width_px or not info.dpi:
                raise ValueError("page INFO chunk missing")
            zone = layers.get(idx)
            lines = djvu.page_lines(zone, info.height_px, info.dpi) if zone is not None else []
            text = "\n".join(ln["text"] for ln in lines)
            c, flags = cls.classify_djvu_page(info.has_text, text if zone is not None else None, cfg.classifier)
            route, reason = cls.route_for(c, flags)
            s = 72.0 / info.dpi
            w_pt, h_pt = round(info.width_px * s, 3), round(info.height_px * s, 3)
            raw = {"schema": "vkm.native_raw.djvu_page/1", "page_index": idx, "width_px": info.width_px,
                   "height_px": info.height_px, "dpi": info.dpi, "rotation_flags": info.rotation_flags,
                   "chunks": info.chunks, "has_text_chunk": info.has_text, "lines": lines,
                   "text_zone_page_box_px": list(zone.box_px) if zone is not None else None}
            r = store.put_json(raw, "NATIVE_RAW", compress=True, source_id=src.source_id,
                               page_id=_page_id(src.source_id, "p", idx))
            row.update({"width_pt": w_pt, "height_pt": h_pt, "rotation": info.rotation_deg, "native_dpi": info.dpi,
                        "width_px": info.width_px, "height_px": info.height_px,
                        "native_raw_artifact_id": r.artifact_id, "page_class": c, "class_flags": flags,
                        "route": route, "route_reason": reason, "native_chars": len(text.replace(" ", "")),
                        "img_main_dpi": info.dpi, "img_cov": 1.0, "n_lines": len(lines)})
        except Exception as exc:  # noqa: BLE001
            row.update({"route": "FAILED", "error": _err("NATIVE_EXTRACT_FAILED", "NATIVE_TEXT", exc, idx,
                                                          tool="djvulibre")})
        prep["pages"].append(row)


def _prepare_epub(cfg: PipelineConfig, store: ArtifactStore, src: SourceInput, path: Path,
                  prep: dict[str, Any]) -> None:
    from vkm_corpus.extract import epub
    from vkm_corpus.extract import pages as pg

    pag = pg.paginate_epub(path)
    prep["pagination"] = pag.__dict__
    ed = epub.read_epub(path)
    rec = store.put_json({"schema": "vkm.native_raw.epub_document/1", "opf_path": ed.opf_path, "version": ed.version,
                          "metadata": ed.metadata, "page_list_source": ed.page_list_source,
                          "n_page_anchors": ed.n_page_anchors, "manifest_media_types": ed.manifest_media_types,
                          "package_manifest": ed.package_manifest},
                         "NATIVE_RAW", compress=True, source_id=src.source_id)
    prep["document_raw_artifact_id"] = rec.artifact_id
    prep["document"] = {"is_encrypted": False, "has_native_page_labels": ed.n_page_anchors > 0,
                        "metadata": {k: "; ".join(v) if v else None for k, v in ed.metadata.items()},
                        "languages": ed.metadata.get("language", []),
                        "identifiers": ed.metadata.get("identifier", []), "page_list_source": ed.page_list_source}
    for unit in ed.spine:
        row: dict[str, Any] = {"page_index": unit.index, "page_kind": "EPUB_SPINE_ITEM", "spine_href": unit.href,
                               "page_class": "REFLOWABLE", "class_flags": [], "route": "NATIVE",
                               "route_reason": "EPUB_XHTML"}
        raw = {"schema": "vkm.native_raw.epub_unit/1", "index": unit.index, "idref": unit.idref, "href": unit.href,
               "linear": unit.linear, "media_type": unit.media_type,
               "blocks": [b.__dict__ for b in unit.blocks], "images": [i.__dict__ for i in unit.images],
               "tables": [t.__dict__ for t in unit.tables], "page_anchors": unit.page_anchors,
               "bib_ids": unit.bib_ids, "parse_error": unit.parse_error,
               "maths": [m.__dict__ for m in unit.maths], "raw_markup": unit.raw_markup,
               "raw_markup_base64": unit.raw_markup_base64,
               "diagnostics": unit.diagnostics}
        r = store.put_json(raw, "NATIVE_RAW", compress=True, source_id=src.source_id,
                           page_id=_page_id(src.source_id, "s", unit.index))
        row["native_raw_artifact_id"] = r.artifact_id
        row["n_formula_images"] = sum(1 for i in unit.images if i.is_formula_candidate)
        row["native_chars"] = sum(len(b.text) for b in unit.blocks)
        if unit.parse_error:
            row.update({"route": "FAILED", "error": _err("NATIVE_EXTRACT_FAILED", "NATIVE_TEXT", unit.parse_error,
                                                          unit.index, tool="lxml")})
        prep["pages"].append(row)


def docx_render_signature(src: SourceInput, image: str) -> str:
    return _sig(source_sha256=src.sha256, stage="DOCX_RENDER", pipeline_version=PIPELINE_VERSION,
                extractor_id="libreoffice-docx-pdf", extractor_version=EXTRACTOR_VERSIONS["libreoffice-docx-pdf"],
                stage_config_hash=_cfg_hash({"image": image}))


def _prepare_docx(cfg: PipelineConfig, store: ArtifactStore, cache: StageCache, src: SourceInput, path: Path,
                  prep: dict[str, Any]) -> None:
    from vkm_corpus.extract import docx as dx
    from vkm_corpus.extract import pages as pg
    from vkm_corpus.extract import pdf_native

    dd = dx.read_docx(path)
    identity = docx_grid_identity(src)
    rec = store.put_json({"schema": "vkm.native_raw.docx_document/1", "source_id": src.source_id,
                          "source_sha256": src.sha256, **identity, "paragraphs": [p.__dict__ for p in dd.paragraphs],
                          "maths": [m.__dict__ for m in dd.maths], "tables": [t.__dict__ for t in dd.tables],
                          "images": [i.__dict__ for i in dd.images], "app_properties": dd.app_properties,
                          "core_properties": dd.core_properties, "counts": dd.counts,
                          "raw_parts": dd.raw_parts, "raw_parts_base64": dd.raw_parts_base64,
                          "diagnostics": dd.diagnostics, "package_manifest": dd.package_manifest},
                         "NATIVE_RAW", compress=True, source_id=src.source_id,
                         producer_signature=identity["docx_grid_signature"])
    prep["document_raw_artifact_id"] = rec.artifact_id
    prep.update(identity)
    prep["docx_counts"] = dd.counts
    prep["document"] = {"is_encrypted": False, "has_native_page_labels": None,
                        "metadata": {**{f"app_{k}": v for k, v in dd.app_properties.items()},
                                     **{f"core_{k}": v for k, v in dd.core_properties.items()}}}
    render = cache.get_stage(docx_render_signature(src, cfg.docx_render_image))
    if render is None:
        prep["pagination"] = None
        prep["errors"].append(_err("RENDER_FAILED", "PAGINATE",
                                   "pinned DOCX render missing: run `vkm-corpus run render-docx` on the Docker host",
                                   retryable=True, tool="libreoffice"))
        return
    pdf_id = render["outputs"]["pdf_artifact_id"]
    pdf_path = store.find(pdf_id)
    if pdf_path is None:
        prep["errors"].append(_err("ARTIFACT_MISSING", "PAGINATE", f"render {pdf_id} not in the store"))
        return
    profile = render["outputs"]["profile_string"]
    pag = pg.paginate_rendered_pdf(pdf_path, profile)
    prep["pagination"] = pag.__dict__
    prep["render"] = {"pdf_artifact_id": pdf_id, "profile": render["outputs"]["profile"], "profile_string": profile}
    doc = pdf_native.open_pdf(pdf_path)
    try:
        for i in range(doc.page_count):
            idx = i + 1
            row: dict[str, Any] = {"page_index": idx, "page_kind": "DOCX_RENDERED_PAGE"}
            try:
                npg = pdf_native.extract_page(doc, i, {}, with_drawings_count=False)
                r = store.put_json(npg.raw, "NATIVE_RAW", compress=True, source_id=src.source_id,
                                   page_id=_page_id(src.source_id, "r", idx))
                row.update({"width_pt": npg.raw["width_pt"], "height_pt": npg.raw["height_pt"],
                            "rotation": npg.raw["rotation"], "native_raw_artifact_id": r.artifact_id,
                            "plain_text_sha256": npg.raw["plain_text_sha256"], "page_class": "RENDERED_FROM_SOURCE",
                            "class_flags": [], "route": "NATIVE", "route_reason": "DOCX_XML",
                            "native_chars": npg.features.get("chars")})
            except Exception as exc:  # noqa: BLE001
                row.update({"route": "FAILED", "error": _err("NATIVE_EXTRACT_FAILED", "NATIVE_TEXT", exc, idx)})
            prep["pages"].append(row)
    finally:
        doc.close()


# ---------------------------------------------------------------------------------------------------- entry
def prepare_source(cfg: PipelineConfig, store: ArtifactStore, cache: StageCache, src: SourceInput,
                   *, force: bool = False) -> dict[str, Any]:
    """Prep summary of a source (cached by the prepare signature unless ``force``)."""
    t0 = time.perf_counter()
    prep: dict[str, Any] = {"schema": PREP_SCHEMA, "source_id": src.source_id, "source_sha256": src.sha256,
                            "prepare_signature": None, "run_id": cache.run_id, "pages": [], "errors": [],
                            "lifecycle": src.lifecycle, "libraries": library_versions()}
    try:
        if not re.fullmatch(r"VKM-SRC-[0-9]{3}", src.source_id):
            raise ValueError("invalid source identity")
        root = Path(cfg.resources_root).resolve(strict=True)
        relative = Path(src.canonical_path)
        if relative.is_absolute() or PureWindowsPath(src.canonical_path).drive:
            raise ValueError("source path must be relative to PRIVATE root")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("source path escapes PRIVATE root")
        if not src.sha256 or not re.fullmatch(r"[0-9a-f]{64}", src.sha256):
            raise ValueError("source requires registered lowercase SHA-256")
        sig = prepare_signature(cfg, src, {"docx_image": cfg.docx_render_image}
                                if src.canonical_path.lower().endswith(".docx") else None)
        prep["prepare_signature"] = sig
        info = inspect_file(path, src.sha256)
    except (OSError, ValueError) as exc:
        prep.update(status="FAILED", transient_errors=False)
        prep["errors"].append(_err("NATIVE_EXTRACT_FAILED", "INSPECT", str(exc)))
        return prep
    prep["inspect"] = {"path_exists": info.path_exists, "size_bytes": info.size_bytes, "sha256": info.sha256,
                       "is_lfs_pointer": info.is_lfs_pointer, "file_format": info.file_format,
                       "format_version": info.format_version, "container_detail": info.container_detail,
                       "leading_bytes": info.leading_bytes, "flags": info.flags}
    if not info.path_exists:
        prep["status"] = "FAILED"
        prep["errors"].append(_err("SOURCE_FILE_MISSING", "INSPECT", "file not found", retryable=False))
    elif info.is_lfs_pointer:
        prep["status"] = "FAILED"
        prep["errors"].append(_err("SOURCE_LFS_POINTER", "INSPECT", "LFS pointer, not content", retryable=True))
    elif "SHA256_MISMATCH" in info.flags:
        prep["status"] = "FAILED"
        prep["errors"].append(_err("SOURCE_SHA256_MISMATCH", "INSPECT", "sha256 differs from the register"))
    elif "UNREADABLE" in info.flags:
        prep["status"] = "FAILED"
        prep["errors"].append(_err("SOURCE_UNREADABLE", "INSPECT", "source format inspection failed"))
    else:
        # Admission is based on current bytes, never mtime/size or cached success.
        if not force:
            cached = load_prep(cfg.data_root, src.source_id, sig)
            docx_cache_current = True
            if (info.file_format == "DOCX" or cached is not None
                    and (cached.get("inspect") or {}).get("file_format") == "DOCX"):
                # Format was inspected from admitted bytes, never guessed from
                # the filename. The child identity closes legacy/disguised cache reuse.
                docx_cache_current = (cached is not None
                    and (cached.get("inspect") or {}).get("file_format") == info.file_format == "DOCX"
                    and docx_grid_cache_current(cached, src, store=store, data_root=cfg.data_root))
            if (cached is not None and cached.get("status") == "PREPARED" and not cached.get("transient_errors")
                    and not cached.get("errors") and cached.get("schema") == PREP_SCHEMA
                    and cached.get("source_sha256") == src.sha256 and cached.get("prepare_signature") == sig
                    and not any(row.get("route") == "FAILED" for row in cached.get("pages", []))
                    and info.file_format in {"PDF", "DJVU", "EPUB", "DOCX"}
                    and (cfg.profile != "production"
                         or (cached.get("inspect") or {}).get("file_format") == info.file_format)
                    and docx_cache_current):
                return cached
        try:
            if info.file_format == "PDF":
                _prepare_pdf(cfg, store, src, path, prep)
            elif info.file_format == "DJVU":
                _prepare_djvu(cfg, store, src, path, prep)
            elif info.file_format == "EPUB":
                _prepare_epub(cfg, store, src, path, prep)
            elif info.file_format == "DOCX":
                _prepare_docx(cfg, store, cache, src, path, prep)
            else:
                prep["status"] = "UNSUPPORTED"
                prep["errors"].append(_err("FORMAT_UNSUPPORTED", "INSPECT", f"format {info.file_format}"))
        except Exception as exc:  # noqa: BLE001 - recorded; the source commit carries the error
            prep["errors"].append({**_err("PAGINATION_FAILED" if not prep.get("pagination") else "INTERNAL_ERROR",
                                          "PAGINATE", exc), "traceback": traceback.format_exc()[-1500:]})
        pag = prep.get("pagination")
        if pag and pag.get("check_count") is not None and pag["check_count"] != pag["count"]:
            prep["errors"].append(_err("PAGECOUNT_MISMATCH", "PAGINATE",
                                       f"{pag['method']}={pag['count']} vs {pag['check_method']}={pag['check_count']}"))
        classes = [(p.get("page_class") or "UNKNOWN", p.get("class_flags") or []) for p in prep["pages"]]
        prep["document_class"] = cls.document_class(classes, info.file_format)
        prep["spreads"] = info.file_format in ("PDF", "DJVU") and _spreads(prep["pages"], cfg.spread_aspect)
        prep.setdefault("status", "PREPARED")
    prep["transient_errors"] = any(e.get("retryable") for e in prep["errors"])
    from vkm_corpus.coverage.accounting import expected_summary

    prep["accounting_expected"] = expected_summary(prep)
    prep["t_s"] = round(time.perf_counter() - t0, 2)
    if not info.path_exists or info.is_lfs_pointer or {"SHA256_MISMATCH", "UNREADABLE"}.intersection(info.flags):
        return prep  # do not poison or overwrite a cache for different/unavailable bytes
    p = prep_path(cfg.data_root, src.source_id, sig)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(canonical_json_bytes(prep))
    os.replace(tmp, p)
    cache.add_stage(stage_signature=sig, stage="PREPARE", source_id=src.source_id, page_index=None,
                    outputs={"prep_relpath": str(p.relative_to(cfg.data_root)).replace("\\", "/"),
                             "n_pages": len(prep["pages"]), "status": prep.get("status")},
                    status="OK" if prep.get("status") == "PREPARED" else prep.get("status", "FAILED"))
    return prep
