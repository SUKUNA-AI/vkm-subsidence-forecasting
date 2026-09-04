#!/usr/bin/env python
"""Run structural, provenance, accessibility, and optional render QA for the thesis DOCX."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4
import zipfile
from xml.etree import ElementTree as ET

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DOCX_RELATIVE = Path("docs/thesis/SPECIAL_SECTION_SKRU1_RU.docx")
SOURCE_MAP_RELATIVE = Path("docs/thesis/SPECIAL_SECTION_SKRU1_RU_SOURCE_MAP.json")
QA_RELATIVE = Path("docs/thesis/SPECIAL_SECTION_SKRU1_RU_QA.json")
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "r": "http://schemas.openxmlformats.org/package/2006/relationships",
    "v": "urn:schemas-microsoft-com:vml",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--manual-reviewed", action="store_true")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def check_page_geometry(document: ET.Element) -> tuple[bool, list[dict[str, int]]]:
    records = []
    for section in document.findall(".//w:sectPr", NS):
        size = section.find("w:pgSz", NS)
        margins = section.find("w:pgMar", NS)
        if size is None or margins is None:
            return False, records
        values = {
            "width": int(size.attrib[f"{{{NS['w']}}}w"]),
            "height": int(size.attrib[f"{{{NS['w']}}}h"]),
            "left": int(margins.attrib[f"{{{NS['w']}}}left"]),
            "right": int(margins.attrib[f"{{{NS['w']}}}right"]),
            "top": int(margins.attrib[f"{{{NS['w']}}}top"]),
            "bottom": int(margins.attrib[f"{{{NS['w']}}}bottom"]),
        }
        records.append(values)
    expected = {
        "width": 11906,
        "height": 16838,
        "left": 1701,
        "right": 850,
        "top": 1134,
        "bottom": 1134,
    }
    return bool(records) and all(
        all(abs(record[key] - value) <= 3 for key, value in expected.items())
        for record in records
    ), records


def check_normal_style(styles: ET.Element) -> tuple[bool, dict[str, Any]]:
    style = next(
        (
            item
            for item in styles.findall("w:style", NS)
            if item.attrib.get(f"{{{NS['w']}}}styleId") == "Normal"
        ),
        None,
    )
    if style is None:
        return False, {}
    fonts = style.find("w:rPr/w:rFonts", NS)
    size = style.find("w:rPr/w:sz", NS)
    spacing = style.find("w:pPr/w:spacing", NS)
    indent = style.find("w:pPr/w:ind", NS)
    detail = {
        "ascii_font": fonts.attrib.get(f"{{{NS['w']}}}ascii") if fonts is not None else None,
        "hansi_font": fonts.attrib.get(f"{{{NS['w']}}}hAnsi") if fonts is not None else None,
        "east_asia_font": fonts.attrib.get(f"{{{NS['w']}}}eastAsia") if fonts is not None else None,
        "font_half_points": int(size.attrib[f"{{{NS['w']}}}val"]) if size is not None else None,
        "line_twips": int(spacing.attrib.get(f"{{{NS['w']}}}line", 0)) if spacing is not None else None,
        "line_rule": spacing.attrib.get(f"{{{NS['w']}}}lineRule") if spacing is not None else None,
        "first_line_twips": int(indent.attrib.get(f"{{{NS['w']}}}firstLine", 0)) if indent is not None else None,
    }
    passed = (
        detail["ascii_font"] == "Times New Roman"
        and detail["hansi_font"] == "Times New Roman"
        and detail["east_asia_font"] == "Times New Roman"
        and detail["font_half_points"] == 28
        and detail["line_twips"] == 360
        and detail["first_line_twips"] in range(705, 713)
    )
    return passed, detail


def render_checks(render_dir: Path | None, *, manual_reviewed: bool) -> dict[str, Any]:
    if render_dir is None:
        return {
            "status": "NOT_RUN",
            "page_count": 0,
            "all_pages_nonblank": False,
            "manual_visual_review_completed": False,
        }
    pages = sorted(render_dir.glob("page-*.png"))
    records = []
    for path in pages:
        with Image.open(path) as image:
            grayscale = image.convert("L")
            extrema = grayscale.getextrema()
            width, height = image.size
        records.append(
            {
                "path": path.name,
                "width_px": width,
                "height_px": height,
                "grayscale_extrema": list(extrema),
                "nonblank": extrema[0] < 245,
            }
        )
    passed = bool(pages) and all(item["nonblank"] for item in records) and manual_reviewed
    return {
        "status": "PASS" if passed else "FAIL",
        "page_count": len(pages),
        "all_pages_nonblank": bool(pages) and all(item["nonblank"] for item in records),
        "manual_visual_review_completed": manual_reviewed,
        "pages": records,
    }


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    docx = root / DOCX_RELATIVE
    source_map_path = root / SOURCE_MAP_RELATIVE
    if not docx.is_file() or not source_map_path.is_file():
        raise FileNotFoundError("Special-section DOCX and source map must exist")
    source_map = json.loads(source_map_path.read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    with zipfile.ZipFile(docx) as archive:
        members = archive.namelist()
        add("zip_integrity", archive.testzip() is None, f"members={len(members)}")
        required = {"[Content_Types].xml", "word/document.xml", "word/styles.xml"}
        add("required_ooxml_parts", required.issubset(members), sorted(required))
        parsed: dict[str, ET.Element] = {}
        xml_errors = []
        for name in members:
            if name.endswith((".xml", ".rels")):
                try:
                    parsed[name] = ET.fromstring(archive.read(name))
                except ET.ParseError as exc:
                    xml_errors.append({"part": name, "error": str(exc)})
        add("xml_integrity", not xml_errors, xml_errors or f"parsed={len(parsed)}")
        document = parsed["word/document.xml"]
        styles = parsed["word/styles.xml"]
        tracked_names = {"ins", "del", "moveFrom", "moveTo", "moveFromRangeStart", "moveToRangeStart"}
        tracked = [local_name(node.tag) for node in document.iter() if local_name(node.tag) in tracked_names]
        comments = [name for name in members if "comments" in name.lower()]
        comment_markers = [
            local_name(node.tag)
            for node in document.iter()
            if local_name(node.tag) in {"commentRangeStart", "commentRangeEnd", "commentReference"}
        ]
        add("tracked_changes_absent", not tracked, tracked)
        add("comments_absent", not comments and not comment_markers, {"parts": comments, "markers": comment_markers})
        external = []
        for name, tree in parsed.items():
            if not name.endswith(".rels"):
                continue
            for rel in tree:
                if rel.attrib.get("TargetMode") == "External":
                    external.append({"part": name, "target": rel.attrib.get("Target")})
        add("external_relationships_absent", not external, external)
        drawings = document.findall(".//wp:docPr", NS)
        missing_alt = [
            node.attrib.get("id")
            for node in drawings
            if not node.attrib.get("descr", "").strip() or not node.attrib.get("title", "").strip()
        ]
        add(
            "drawing_accessibility",
            len(drawings) == int(source_map["figure_count"]) and not missing_alt,
            {"drawings": len(drawings), "missing_alt_ids": missing_alt},
        )
        watermarks = [name for name in members if "watermark" in name.lower()]
        header_shapes = []
        for name, tree in parsed.items():
            if name.startswith("word/header"):
                header_shapes.extend(local_name(node.tag) for node in tree.iter() if local_name(node.tag) == "shape")
        add("watermark_absent", not watermarks and not header_shapes, {"parts": watermarks, "header_shapes": len(header_shapes)})
        all_xml_text = "\n".join(
            archive.read(name).decode("utf-8", errors="ignore")
            for name in members
            if name.endswith((".xml", ".rels"))
        )
        absolute_hits = sorted(
            set(
                re.findall(
                    r"(?:(?<![A-Za-z])[A-Za-z]:[\\/][^<\"\s]+|file://[^<\"\s]+)",
                    all_xml_text,
                )
            )
        )
        add("absolute_paths_absent", not absolute_hits, absolute_hits)
        token_hits = [token for token in (":codex-file-citation", "turn0search", "turn1search") if token in all_xml_text]
        add("internal_tokens_absent", not token_hits, token_hits)
        visible_text = "".join((node.text or "") for node in document.findall(".//w:t", NS))
        checkpoint_tokens = (
            "6.6.4 Checkpoint/recovery и CUDA-оптимизация",
            "3 860 manifests",
            "19 300 retained states",
            "Outer labels не участвуют",
        )
        checkpoint_missing = [token for token in checkpoint_tokens if token not in visible_text]
        add("gate_c1_checkpoint_evidence", not checkpoint_missing, checkpoint_missing)
        geometry_pass, geometry = check_page_geometry(document)
        add("a4_margins", geometry_pass, geometry)
        normal_pass, normal_detail = check_normal_style(styles)
        add("normal_style", normal_pass, normal_detail)
        tables = len(document.findall(".//w:tbl", NS))
        expected_tables_raw = source_map.get("physical_table_count")
        if expected_tables_raw is None:
            expected_tables_raw = source_map["table_count"]
        expected_tables = int(expected_tables_raw)
        add("table_count", tables == expected_tables, {"document": tables, "source_map": expected_tables})

    document_hash = sha256_file(docx)
    add("source_map_document_hash", source_map["document_sha256"] == document_hash, document_hash)
    source_failures = []
    for item in source_map["sources"]:
        path = root / item["path"]
        actual = sha256_file(path) if path.is_file() else "MISSING"
        if actual != item["sha256"]:
            source_failures.append({"path": item["path"], "expected": item["sha256"], "actual": actual})
    add("source_map_sources", not source_failures, source_failures or f"sources={len(source_map['sources'])}")
    render_dir = args.render_dir.resolve() if args.render_dir else None
    render = render_checks(render_dir, manual_reviewed=args.manual_reviewed)
    add("render_and_manual_visual_review", render["status"] == "PASS", render)
    failed = [item for item in checks if item["status"] != "PASS"]
    report = {
        "schema_version": 1,
        "document": DOCX_RELATIVE.as_posix(),
        "document_sha256": document_hash,
        "status": "PASS" if not failed else "FAIL",
        "checks": checks,
        "check_count": len(checks),
        "failed_checks": len(failed),
        "render": render,
    }
    output = root / QA_RELATIVE
    output.parent.mkdir(parents=True, exist_ok=True)
    work = root / "work" / "gate_c1_reporting"
    work.mkdir(parents=True, exist_ok=True)
    temporary = work / f"{output.name}.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(output)
    print(json.dumps({"status": report["status"], "checks": len(checks), "failed": len(failed)}, ensure_ascii=False, indent=2))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
