"""vkm-drawio file model: deterministic writer (golden bytes), reader of plain/compressed/embedded diagrams,
canonicalisation, the committed example diagrams (DRW-01…DRW-04)."""
from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest

from vkm_drawio.build import build_diagram, grid_positions
from vkm_drawio.model import DiagramSpec, PageSpec
from vkm_drawio.styles import canonical_style, merge_style, set_style_keys, text_to_value, value_to_text
from vkm_drawio.xmlio import (DiagramParseError, canonicalize_text, compress_model, content_bbox, decompress_model,
                              fmt_num, read_diagram_bytes, write_diagram)

ROOT = Path(__file__).resolve().parents[2]
DIAGRAMS = ROOT / "docs" / "diagrams"

SPEC = {
    "pages": [
        {"id": "p1", "name": "Поток", "layout": "none", "nodes": [
            {"id": "src", "label": "Источник\nPDF & DjVu", "preset": "cylinder", "x": 40, "y": 40},
            {"id": "grp", "label": "Группа", "preset": "container", "x": 300, "y": 40, "w": 220, "h": 140},
            {"id": "page", "label": "Страница", "parent": "grp", "x": 20, "y": 50},
            {"id": "fig", "label": "Рисунок", "x": 600, "y": 40.123, "tooltip": "подсказка", "link": "../README.md",
             "props": {"kind": "FIGURE", "b": "2"}},
        ], "edges": [
            {"id": "e1", "source": "src", "target": "page", "label": "HAS_PAGE"},
            {"id": "e2", "source": "page", "target": "fig", "preset": "dashed", "waypoints": [[560.5, 100.25]]},
        ]},
        {"id": "p2", "name": "Grid", "layout": "grid", "page_width": 800, "page_height": 600, "grid": False,
         "background": "#FFFFFF", "nodes": [{"id": f"n{i}", "label": f"N{i}"} for i in range(5)]},
    ]
}
# DRW-01: changing the writer's output is a deliberate act — update this hash and say why in the commit
GOLDEN_SHA256 = "35bb75cdaa035abebcaaa130daf74542a4e6ab0676d007c74049973b1c89bdba"


def _write(spec: dict) -> str:
    return write_diagram(build_diagram(DiagramSpec.model_validate(spec)))


def test_writer_golden_bytes():
    text = _write(SPEC)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == GOLDEN_SHA256, text


def test_writer_is_deterministic_and_key_order_free():
    shuffled = json.loads(json.dumps(SPEC))
    for page in shuffled["pages"]:
        page["nodes"] = [dict(reversed(list(n.items()))) for n in page["nodes"]]
        page["nodes"][-1]["props"] = dict(reversed(list(page["nodes"][-1].get("props", {}).items()))) or {}
    assert _write(SPEC) == _write(SPEC) == _write(shuffled)


def test_writer_format_rules():
    text = _write(SPEC)
    assert text.endswith("</mxfile>\n") and "\r" not in text
    assert text.startswith('<mxfile host="vkm-drawio" agent="vkm-drawio writer/1" compressed="false" pages="2">')
    for volatile in ("modified=", "etag=", "version=", " dx=", " dy="):
        assert volatile not in text
    assert 'y="40.12"' in text and 'x="560.5" y="100.25"' in text          # at most two decimals, no trailing zeros
    assert "&lt;br&gt;" in text and "&amp;amp;" in text                   # html label: escaped, newline → <br>
    assert '<UserObject id="fig" label="Рисунок" b="2" kind="FIGURE" link="../README.md" tooltip="подсказка">' in text
    assert 'background="#ffffff"' in text and 'grid="0"' in text


def test_numbers_and_styles():
    assert [fmt_num(v) for v in (0, -0.0, 12.0, 12.5, 12.345, 1e-9, -3.999)] == ["0", "0", "12", "12.5", "12.35",
                                                                                "0", "-4"]
    assert canonical_style("html=1;ellipse;rounded=0;;fillColor=#fff;ellipse;") == \
        "ellipse;fillColor=#fff;html=1;rounded=0;"
    assert merge_style("rounded=1;html=1;", "rounded=0;shape=note;") == "html=1;rounded=0;shape=note;"
    assert set_style_keys("a=1;b=2;", {"a": None, "c": "3"}) == "b=2;c=3;"
    with pytest.raises(ValueError):
        set_style_keys("", {"a": "x;y"})
    assert value_to_text(text_to_value("a < b\nc & d", True), True) == "a < b\nc & d"
    assert text_to_value("x\ny", False) == "x\ny"


def test_grid_layout_places_only_unplaced_nodes():
    page = PageSpec.model_validate({"id": "p", "name": "p", "layout": "grid", "nodes": [
        {"id": "a", "x": 10, "y": 10}, {"id": "b"}, {"id": "c"}, {"id": "d"}, {"id": "e"}]})
    pos = grid_positions(page)
    assert set(pos) == {"b", "c", "d", "e"}
    assert pos["b"] == (40.0, 120.0) and pos["c"] == (220.0, 120.0) and pos["d"] == (40.0, 230.0)
    assert len(set(pos.values())) == 4


def test_roundtrip_read_equals_written():
    text = _write(SPEC)
    diagram = read_diagram_bytes(text.encode("utf-8"))
    assert [p.id for p in diagram.pages] == ["p1", "p2"]
    fig = diagram.pages[0].cell("fig")
    assert fig.wrapper == "UserObject" and fig.wrapper_attrs["kind"] == "FIGURE"
    assert write_diagram(diagram) == text                        # canonical form is a fixed point
    assert canonicalize_text(text.encode("utf-8")) == text


def test_reader_compressed_pages_and_foreign_attribute_order():
    model = ('<mxGraphModel dx="900" dy="700" pageWidth="827" grid="1"><root><mxCell id="0"/>'
             '<mxCell id="1" parent="0"/><mxCell parent="1" vertex="1" style="html=1;rounded=1;" value="Кириллица" '
             'id="v1"><mxGeometry height="50" width="120.000" y="7.5" x="0" as="geometry"/></mxCell>'
             '<object label="Obj" id="v2" custom="1"><mxCell style="ellipse" vertex="1" parent="1">'
             '<mxGeometry x="200" y="10" width="40" height="40" as="geometry"/></mxCell></object></root>'
             '</mxGraphModel>')
    compressed = compress_model(model)
    assert decompress_model(compressed) == model
    raw = (f'<mxfile host="Electron" modified="2026-01-01T00:00:00Z" etag="x" version="31.5.3">'
           f'<diagram id="d1" name="Сжатая">{compressed}</diagram>'
           f'<diagram id="d2" name="Plain">{model}</diagram></mxfile>').encode("utf-8")
    diagram = read_diagram_bytes(raw)
    assert diagram.pages[0].compressed_in_file and not diagram.pages[1].compressed_in_file
    assert diagram.pages[0].cell("v1").value == "Кириллица" and diagram.pages[0].cell("v2").wrapper == "object"
    out = canonicalize_text(raw)
    assert "Electron" not in out and "modified" not in out and 'dx="' not in out
    assert '<mxGeometry y="7.5" width="120" height="50" as="geometry" />' in out
    assert '<object id="v2" label="Obj" custom="1">' in out
    assert canonicalize_text(out.encode("utf-8")) == out


def _png_with_model(xml: str, compressed_chunk: bool) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    from urllib.parse import quote

    payload = quote(xml, safe="").encode("ascii")
    text = chunk(b"zTXt", b"mxGraphModel\x00\x00" + zlib.compress(payload)) if compressed_chunk else \
        chunk(b"tEXt", b"mxfile\x00" + payload)
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
    return b"\x89PNG\r\n\x1a\n" + ihdr + text + idat + chunk(b"IEND", b"")


@pytest.mark.parametrize("compressed_chunk", [True, False])
def test_reader_embedded_png(compressed_chunk):
    text = _write(SPEC)
    diagram = read_diagram_bytes(_png_with_model(text, compressed_chunk))
    assert write_diagram(diagram) == text


def test_reader_embedded_svg_and_missing_model():
    from xml.sax.saxutils import quoteattr

    text = _write(SPEC)
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" content={quoteattr(text)}><g/></svg>'.encode("utf-8")
    assert write_diagram(read_diagram_bytes(svg)) == text
    with pytest.raises(DiagramParseError):
        read_diagram_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><g/></svg>')
    with pytest.raises(DiagramParseError):
        read_diagram_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)


def test_reader_rejects_entities_duplicates_and_garbage():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><mxfile><diagram>&a;</diagram></mxfile>'
    with pytest.raises(DiagramParseError):
        read_diagram_bytes(bomb)
    dup = (b'<mxfile><diagram id="d" name="d"><mxGraphModel><root><mxCell id="0"/><mxCell id="0"/></root>'
           b'</mxGraphModel></diagram></mxfile>')
    with pytest.raises(DiagramParseError, match="duplicate"):
        read_diagram_bytes(dup)
    for bad in (b"not xml", b"<html/>", b'<mxfile><diagram id="d">%%%not-base64%%%</diagram></mxfile>'):
        with pytest.raises(DiagramParseError):
            read_diagram_bytes(bad)


def test_content_bbox_accounts_for_containers():
    diagram = build_diagram(DiagramSpec.model_validate(SPEC))
    x0, y0, x1, y1 = content_bbox(diagram.pages[0])
    assert (x0, y0) == (40.0, 40.0) and x1 == 720.0 and y1 == 180.0


def test_committed_example_diagrams_rebuild_byte_identically():
    specs = sorted((DIAGRAMS / "specs").glob("*.spec.json"))
    assert specs, "docs/diagrams/specs/*.spec.json are the sources of the example diagrams"
    for spec_path in specs:
        name = spec_path.name[: -len(".spec.json")]
        spec = DiagramSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
        committed = (DIAGRAMS / f"{name}.drawio").read_bytes()
        assert write_diagram(build_diagram(spec)).encode("utf-8") == committed, name
        assert canonicalize_text(committed).encode("utf-8") == committed, name
