"""Native workbook inspection. No Excel process, macro execution, formula evaluation or OCR.

OOXML is read directly so formulas AND cached values survive together. BIFF uses xlrd for
cells/styles and retains original formula token records; RPN tokens are never labelled parsed formulas.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import posixpath
import struct
import xml.etree.ElementTree as ET
import zipfile

from .manifest import DatasetVersion, confined, verify_members

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKGREL = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 128 * 1024 * 1024
    max_uncompressed_bytes: int = 256 * 1024 * 1024
    max_part_bytes: int = 32 * 1024 * 1024
    max_parts: int = 10000
    max_cells: int = 250_000
    max_compression_ratio: int = 1000


def _xml(raw: bytes):
    # ASCII declarations in UTF-16/32 also have to be rejected before ElementTree sees them.
    declarations = raw.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise ValueError("DTD/entity declarations are not allowed in workbook XML")
    return ET.fromstring(raw)


def _text(element):
    return "".join(e.text or "" for e in element.iter(f"{{{MAIN}}}t"))


def _part(base: str, target: str) -> str:
    if "\\" in target or ":" in target or target.startswith("//"):
        raise ValueError("unsafe workbook relationship")
    result = posixpath.normpath(target.lstrip("/") if target.startswith("/") else
                                 posixpath.join(posixpath.dirname(base), target))
    if result.startswith("../") or result == "..":
        raise ValueError("workbook relationship escapes package")
    return result


def inspect_workbook(root: Path, version: DatasetVersion, entrypoint: str, *,
                     include_values: bool = False, destination: str = "local", limits: Limits = Limits()) -> dict:
    version.policy.require(destination)
    if entrypoint not in version.entrypoints:
        raise ValueError("not a registered entrypoint")
    verify_members(root, version.files)
    path = confined(root, entrypoint)
    if path.stat().st_size > limits.max_file_bytes:
        raise ValueError("workbook exceeds file memory guard")
    if path.suffix.lower() == ".xlsx":
        report = _xlsx(path, include_values, limits)
    elif path.suffix.lower() == ".xls":
        report = _xls(path, include_values, limits)
    else:
        raise ValueError("workbook inspection accepts XLS/XLSX only")
    verify_members(root, version.files)
    return {"schema": "vkm-workbook-inspection-v1", "dataset_id": version.dataset_id,
            "dataset_version": version.digest, "entrypoint": entrypoint, "policy": version.policy.as_dict(),
            "values_included": include_values, "formula_evaluation": "NOT_RUN",
            "scientific_admission": "NOT_CHECKED", **report}


def _xlsx(path: Path, values: bool, limits: Limits) -> dict:
    ns = {"s": MAIN}
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        if len(infos) > limits.max_parts or len(set(names)) != len(names) or \
                len({n.casefold() for n in names}) != len(names):
            raise ValueError("duplicate package parts or too many parts")
        if sum(i.file_size for i in infos) > limits.max_uncompressed_bytes:
            raise ValueError("workbook exceeds uncompressed memory guard")
        for i in infos:
            if PurePosixPath(i.filename).is_absolute() or ".." in PurePosixPath(i.filename).parts or \
                    "\\" in i.filename or i.flag_bits & 1:
                raise ValueError("unsafe/encrypted package part")
            if i.file_size > limits.max_part_bytes or i.file_size > max(1, i.compress_size) * limits.max_compression_ratio:
                raise ValueError("workbook part exceeds decompression guard")
        def xml(name):
            return _xml(z.read(name))
        workbook = xml("xl/workbook.xml")
        if workbook.tag != f"{{{MAIN}}}workbook":
            raise ValueError("unsupported OOXML namespace (never silently inspect an empty workbook)")
        rels = xml("xl/_rels/workbook.xml.rels")
        relationships = {e.attrib["Id"]: dict(e.attrib) for e in rels}
        if len(relationships) != len(rels):
            raise ValueError("duplicate workbook relationship ID")
        shared = [_text(e) for e in xml("xl/sharedStrings.xml")] if "xl/sharedStrings.xml" in names else []
        styles = xml("xl/styles.xml") if "xl/styles.xml" in names else None
        style_formats = [dict(e.attrib) for e in styles.findall("s:cellXfs/s:xf", ns)] if styles is not None else []
        custom_formats = [dict(e.attrib) for e in styles.findall("s:numFmts/s:numFmt", ns)] if styles is not None else []
        sheets, n_cells = [], 0
        for sheet in workbook.findall("s:sheets/s:sheet", ns):
            rel = relationships[sheet.attrib[f"{{{REL}}}id"]]
            if rel.get("TargetMode") == "External":
                raise ValueError("external worksheet cannot be inspected as native data")
            part = _part("xl/workbook.xml", rel["Target"])
            tree = xml(part)
            if tree.tag != f"{{{MAIN}}}worksheet":
                raise ValueError("unsupported worksheet namespace/type; refusing an empty inspection")
            cells, rows, seen = [], [], set()
            for row in tree.findall("s:sheetData/s:row", ns):
                rows.append(dict(row.attrib))
                for cell in row.findall("s:c", ns):
                    n_cells += 1
                    if n_cells > limits.max_cells:
                        raise ValueError("workbook exceeds cell memory guard")
                    address = cell.attrib.get("r")
                    if not address or address in seen:
                        raise ValueError("missing/duplicate native cell coordinate")
                    seen.add(address)
                    f, v, inline = cell.find("s:f", ns), cell.find("s:v", ns), cell.find("s:is", ns)
                    native_type = cell.attrib.get("t", "n")
                    item = {"address": address, "locator": {"part": part, "cell": address},
                            "native_type": native_type, "style": cell.attrib.get("s"),
                            "formula_present": f is not None, "cached_value_present": v is not None,
                            "inline_string_present": inline is not None,
                            "cell_attributes": dict(cell.attrib)}
                    if values:
                        raw = v.text if v is not None else None
                        resolved = raw
                        if native_type == "s" and raw is not None:
                            index = int(raw)
                            if not 0 <= index < len(shared):
                                raise ValueError("shared-string reference outside dictionary")
                            resolved = shared[index]
                        elif inline is not None:
                            resolved = _text(inline)
                        item.update(raw_value=raw, value=resolved, formula=f.text if f is not None else None,
                                    formula_attributes=dict(f.attrib) if f is not None else None)
                    cells.append(item)
            sheets.append({"name": sheet.attrib["name"], "state": sheet.attrib.get("state", "visible"),
                           "part": part, "sheet_id": sheet.attrib["sheetId"], "cells": cells,
                           "rows": rows, "columns": [dict(e.attrib) for e in tree.findall("s:cols/s:col", ns)],
                           "merged_cells": [e.attrib["ref"] for e in tree.findall("s:mergeCells/s:mergeCell", ns)],
                           "dimension": (tree.find("s:dimension", ns).attrib.get("ref")
                                         if tree.find("s:dimension", ns) is not None else None)})
        # Inventory every package part, including unhandled metadata, drawings, comments, links and embedded files.
        parts = [{"part": i.filename, "bytes": i.file_size, "sha256": hashlib.sha256(z.read(i)).hexdigest()}
                 for i in infos if not i.is_dir()]
        defined = [{"attributes": dict(e.attrib), **({"expression": e.text} if values else {})}
                   for e in workbook.findall("s:definedNames/s:definedName", ns)]
        props = workbook.find("s:workbookPr", ns)
        return {"format": "XLSX", "engine": "native-ooxml-v1", "sheets": sheets, "cell_count": n_cells,
                "parts": parts, "date_system": "1904" if props is not None and props.get("date1904") in {"1", "true"}
                else "1900", "style_formats": style_formats, "custom_number_formats": custom_formats,
                "defined_names": defined, "relationships": list(relationships.values()),
                "macros_present": any("vbaproject" in n.lower() for n in names),
                "interpretation": "NATIVE_LEXICAL_VALUES; dates/units/decimal separators are not inferred"}


def _xls(path: Path, values: bool, limits: Limits) -> dict:
    try:
        import xlrd
        from xlrd.compdoc import CompDoc
    except ImportError as e:
        raise RuntimeError("XLS inspection requires the existing xlrd runtime; no installation is performed") from e
    raw = path.read_bytes()
    comp = CompDoc(raw)
    stream = comp.get_named_stream("Workbook") or comp.get_named_stream("Book")
    if stream is None:
        raise ValueError("XLS has no Workbook/Book stream")
    if len(stream) > limits.max_uncompressed_bytes:
        raise ValueError("BIFF stream exceeds memory guard")
    # Native formula RPN and shared/array formula definitions are preserved even though xlrd returns cached values.
    formulas, dependencies, boundaries, at, continued_formula = [], [], [], 0, False
    while at < len(stream):
        if at + 4 > len(stream):
            if not any(stream[at:]):
                break  # OLE stream padding is not a truncated BIFF record.
            raise ValueError("truncated BIFF record")
        opcode, size = struct.unpack_from("<HH", stream, at)
        if opcode == 0 and size == 0 and not any(stream[at:]):
            break
        end = at + 4 + size
        if end > len(stream):
            raise ValueError("truncated BIFF payload")
        payload = stream[at + 4:end]
        if opcode == 0x85 and size >= 4:
            boundaries.append(struct.unpack_from("<I", payload)[0])
        if opcode in {0x0017, 0x0018, 0x0023, 0x01AE}:
            dependency = {"stream_offset": at, "opcode": opcode, "payload_size": size,
                          "payload_sha256": hashlib.sha256(payload).hexdigest()}
            if values:
                dependency["native_biff_payload_hex"] = payload.hex()
            dependencies.append(dependency)
        capture = opcode in {0x06, 0x0206, 0x0406, 0x04BC, 0x0221, 0x0236} or \
            (continued_formula and opcode in {0x003C, 0x0207})
        if capture:
            item = {"stream_offset": at, "opcode": opcode, "payload_size": size,
                    "payload_sha256": hashlib.sha256(payload).hexdigest()}
            if opcode in {0x06, 0x0206, 0x0406} and size >= 6:
                row, col, xf = struct.unpack_from("<HHH", payload)
                item.update(row=row, col=col, style=xf)
            if values:
                item["native_biff_payload_hex"] = payload.hex()
            formulas.append(item)
        continued_formula = capture
        at = end
    book = xlrd.open_workbook(file_contents=raw, on_demand=True, formatting_info=True, ragged_rows=True)
    try:
        if book.biff_version != 80:
            raise ValueError("only BIFF8 XLS is qualified; earlier BIFF needs a separate native-record adapter")
        sheets, total = [], 0
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            cells = []
            for row in range(sheet.nrows):
                for col in range(sheet.row_len(row)):
                    cell = sheet.cell(row, col)
                    if cell.ctype == xlrd.XL_CELL_EMPTY:
                        continue
                    total += 1
                    if total > limits.max_cells:
                        raise ValueError("workbook exceeds cell memory guard")
                    entry = {"row": row, "col": col, "native_type": cell.ctype, "style": cell.xf_index,
                             "locator": {"sheet_index": index, "row": row, "col": col}}
                    if values:
                        entry["cached_value"] = cell.value
                    cells.append(entry)
            sheets.append({"name": sheet.name, "state": {0: "visible", 1: "hidden", 2: "veryHidden"}[sheet.visibility],
                           "nrows": sheet.nrows, "ncols": sheet.ncols, "cells": cells,
                           "merged_cells": sheet.merged_cells,
                           "hidden_rows": [k for k, v in sheet.rowinfo_map.items() if v.hidden],
                           "hidden_columns": [k for k, v in sheet.colinfo_map.items() if v.hidden]})
        return {"format": "XLS", "engine": "xlrd-" + xlrd.__version__, "date_system": str(book.datemode),
                "biff_version": book.biff_version, "codepage": book.codepage, "sheets": sheets, "cell_count": total,
                "formula_representation": "BIFF_RPN_UNINTERPRETED", "native_formula_records": formulas,
                "native_dependency_records": dependencies,
                "defined_names": [{"name": n.name, "scope": n.scope, "hidden": bool(n.hidden),
                                   "macro": bool(n.macro), **({"formula_rpn_hex": n.raw_formula.hex()}
                                                             if values else {})} for n in book.name_obj_list],
                "sheet_bof_offsets": boundaries, "workbook_stream_sha256": hashlib.sha256(stream).hexdigest(),
                "number_formats": {str(k): v.format_str for k, v in book.format_map.items()},
                "interpretation": "CACHED_VALUES; native formula bytes retained, never evaluated"}
    finally:
        book.release_resources()
