"""Scratch DXF operations with ezdxf (CAD-07…CAD-12): vector import (Y flip, units, XDATA), H-20 header rules and
determinism, CRS policy, artifact lookup and integrity, geometry extraction, export, overwrite protection, scratch-root
policy, and ``cad_save_copy`` on a fake AutoCAD with a real file (the original is only read)."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("ezdxf")

from vkm_cad import dxf  # noqa: E402
from vkm_cad.com_read import ComSession  # noqa: E402
from vkm_cad.errors import ToolFailure  # noqa: E402
from vkm_cad.scratch import ScratchStore  # noqa: E402
from vkm_cad.service import CadService  # noqa: E402

VECTORS = {"schema": "vkm.vector_paths/1", "coordinate_space": "PAGE_SPACE", "bbox_space": "PAGE_PT_TL", "paths": [
    {"seqno": 3, "type": "s", "color": [1, 0, 0], "width": 0.75, "layer": "Контуры",
     "items": [["l", [10, 20], [110, 20]], ["c", [0, 0], [10, 10], [20, 10], [30, 0]]]},
    {"seqno": 4, "type": "f", "fill": [0.2, 0.4, 0.6], "items": [["re", [50, 50, 150, 100], 1],
                                                                  ["qu", [[0, 0], [10, 0], [0, 10], [10, 10]]],
                                                                  ["unknown-op", 1]]},
]}


def _blob(obj=VECTORS) -> bytes:
    return gzip.compress(json.dumps(obj, sort_keys=True).encode("utf-8"), mtime=0)


def _store_vector(root: Path, blob: bytes) -> str:
    digest = hashlib.sha256(blob).hexdigest()
    target = root / "vector" / digest[:2] / digest[2:4]
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{digest}.json.gz").write_bytes(blob)
    return "sha256:" + digest


@pytest.fixture()
def svc(tmp_path) -> CadService:
    return CadService(ScratchStore(tmp_path / "scratch"), artifact_roots=[tmp_path / "artifacts"],
                      detector=lambda: {"capabilities": {"read_open_documents": "UNAVAILABLE:TEST"}, "products": []})


def _header(text: str, var: str) -> str | None:
    m = re.search(re.escape(var) + r"\n\s*\d+\n(.*)", text)
    return m.group(1).strip() if m else None


def test_vector_import_units_flip_xdata_and_manifest(svc, tmp_path):
    aid = _store_vector(tmp_path / "artifacts", _blob())
    out = svc.import_pdf_vector(aid, page_height_pt=842, source_id="VKM-SRC-014", page_id="VKM-SRC-014:p0003")
    assert out["counts"] == {"LINE": 1, "SPLINE": 1, "LWPOLYLINE": 2, "skipped_items": 1, "paths": 2}
    assert (out["coordinate_space"], out["crs_status"], out["epsg"], out["insunits"]) == ("DRAWING_UNITS",
                                                                                         "UNKNOWN_CRS", None, 0)
    doc_dir = tmp_path / "scratch" / out["scratch_doc_id"]
    text = (doc_dir / "doc.dxf").read_text(encoding="utf-8")
    assert _header(text, "$INSUNITS") == "0"
    assert _header(text, "$TDCREATE") == _header(text, "$TDUPDATE") == "2451545.0"          # fixed dates (H-20)
    assert _header(text, "$VERSIONGUID") == "{00000000-0000-0000-0000-000000000000}"
    assert "VKM_UNITS=PAGE_PT" in text and "ocg_layer=Контуры" in text
    geometry = svc.extract_geometry(out["scratch_doc_id"])
    rows = [json.loads(line) for line in (doc_dir / "out" / "geometry.jsonl").read_text("utf-8").splitlines()]
    line = next(r for r in rows if r["type"] == "LINE")
    assert line["points"] == [[10.0, 822.0], [110.0, 822.0]] and line["layer"] == "VKM_STROKE"   # y = 842 − y
    assert line["vkm"]["VKM_UNITS"] == "PAGE_PT" and line["vkm"]["seqno"] == "3"
    rect = next(r for r in rows if r["type"] == "LWPOLYLINE")
    assert rect["closed"] and rect["layer"] == "VKM_FILL" and rect["vkm"]["fill_rgb"] == "#336699"
    assert geometry["summary"]["by_layer"] == {"VKM_FILL": 2, "VKM_STROKE": 2}
    manifest = json.loads((doc_dir / "manifest.json").read_text("utf-8"))
    assert manifest["review_status"] == "AUTO_EXTRACTED_UNREVIEWED" and manifest["never_input_of_extraction"]
    assert manifest["derived_from"][0] == {"artifact_id": aid, "kind": "VECTOR_PATHS_JSON", "location": "DATA_ROOT",
                                           "copy": "in/vector_paths.json.gz"}
    assert {f["kind"] for f in manifest["files"]} == {"CAD_DXF", "CAD_GEOMETRY_JSONL"}
    assert (doc_dir / "in" / "vector_paths.json.gz").read_bytes() == _blob()


def test_same_input_same_bytes_and_unknown_height(svc, tmp_path):
    aid = _store_vector(tmp_path / "artifacts", _blob())
    a = svc.import_pdf_vector(aid, page_height_pt=842)
    b = svc.import_pdf_vector(aid, page_height_pt=842)
    assert a["sha256"] == b["sha256"] and a["scratch_doc_id"] != b["scratch_doc_id"]
    c = svc.import_pdf_vector(aid)
    assert c["y_transform"].startswith("y_cad = -y_page") and c["sha256"] != a["sha256"]
    embedded = _store_vector(tmp_path / "artifacts", _blob({**VECTORS, "page_height_pt": 842}))
    assert svc.import_pdf_vector(embedded)["y_transform"].startswith("y_cad = 842 - y_page")


def test_crs_policy(svc, tmp_path):
    aid = _store_vector(tmp_path / "artifacts", _blob())
    for status, rationale in (("EXACT_COORDINATED", None), ("MAP_DIGITIZED", "x" * 20), ("SCHEMATIC", None),
                              ("SCHEMATIC", "short")):
        with pytest.raises(ToolFailure) as exc:
            svc.import_pdf_vector(aid, crs_status=status, crs_rationale=rationale)
        assert exc.value.code == "CRS_STATUS_NOT_ALLOWED"
    out = svc.import_pdf_vector(aid, crs_status="SCHEMATIC", crs_rationale="схема без масштаба и привязки")
    manifest = svc.scratch.manifest(out["scratch_doc_id"])
    assert manifest["crs_status"] == "SCHEMATIC" and manifest["crs_status_basis"]["kind"] == "MODEL_CHOICE"
    assert manifest["epsg"] is None


def test_artifact_lookup_integrity_and_sources(svc, tmp_path):
    with pytest.raises(ToolFailure) as exc:
        svc.import_pdf_vector("sha256:" + "0" * 64)
    assert exc.value.code == "NO_VECTOR_ARTIFACT"
    with pytest.raises(ToolFailure) as exc:
        svc.import_pdf_vector("not-an-id")
    assert exc.value.code == "INVALID_ARGUMENT"
    blob = _blob()
    digest = hashlib.sha256(blob).hexdigest()
    corrupt = tmp_path / "artifacts" / "vector" / digest[:2] / digest[2:4]
    corrupt.mkdir(parents=True)
    (corrupt / f"{digest}.json.gz").write_bytes(blob + b"tampered")
    with pytest.raises(ToolFailure) as exc:
        svc.import_pdf_vector("sha256:" + digest)
    assert exc.value.code == "ARTIFACT_HASH_MISMATCH"
    inbox_blob = _blob({**VECTORS, "clip": [0, 0, 1, 1]})
    inbox = tmp_path / "scratch" / "inbox"
    inbox.mkdir(parents=True)
    inbox_id = hashlib.sha256(inbox_blob).hexdigest()
    (inbox / f"{inbox_id}.json.gz").write_bytes(inbox_blob)
    assert svc.import_pdf_vector("sha256:" + inbox_id)["derived_from"][0]["location"] == "SCRATCH_INBOX"

    class Fetcher:
        def fetch(self, artifact_id):
            return api_blob if artifact_id == "sha256:" + api_id else None

    api_blob = _blob({**VECTORS, "n_paths": 2})
    api_id = hashlib.sha256(api_blob).hexdigest()
    svc.fetcher = Fetcher()
    assert svc.import_pdf_vector("sha256:" + api_id)["derived_from"][0]["location"] == "VKM_API"
    wrong = _store_vector(tmp_path / "artifacts", gzip.compress(b'{"schema": "other"}', mtime=0))
    with pytest.raises(ToolFailure) as exc:
        svc.import_pdf_vector(wrong)
    assert exc.value.code == "VECTOR_FORMAT_UNSUPPORTED"
    for kwargs in ({"source_id": "SRC-1"}, {"page_id": "VKM-SRC-001:x1"},
                   {"source_id": "VKM-SRC-001", "page_id": "VKM-SRC-002:p0001"}, {"page_height_pt": 0}):
        with pytest.raises(ToolFailure) as exc:
            svc.import_pdf_vector("sha256:" + api_id, **kwargs)
        assert exc.value.code == "INVALID_ARGUMENT"


def test_export_and_overwrite(svc, tmp_path):
    empty = svc.create_scratch_document(label="пустой", dxf_version="R2010")
    doc_id = empty["scratch_doc_id"]
    out = svc.export_dxf(doc_id, "R2018")
    assert out["dxf_version"] == "R2018" and out["insunits"] == 0
    exported = (tmp_path / "scratch" / doc_id / "out" / f"{doc_id}.R2018.dxf").read_text(encoding="utf-8")
    assert "AC1032" in exported
    with pytest.raises(ToolFailure) as exc:
        svc.export_dxf(doc_id, "R2018")
    assert exc.value.code == "WOULD_OVERWRITE"
    assert svc.export_dxf(doc_id, "R2018", overwrite=True)["sha256"] == out["sha256"]     # deterministic
    with pytest.raises(ToolFailure) as exc:
        svc.export_dxf(doc_id, "R2000")
    assert exc.value.code == "INVALID_ARGUMENT"                                          # no down-conversion
    with pytest.raises(ToolFailure) as exc:
        svc.export_dxf(doc_id, "R2013", out_name="../escape.dxf")
    assert exc.value.code == "INVALID_ARGUMENT"
    with pytest.raises(ToolFailure) as exc:
        svc.extract_geometry("CADS-20260101T000000Z-00000000")
    assert exc.value.code == "SCRATCH_DOC_NOT_FOUND"
    with pytest.raises(ToolFailure) as exc:
        svc.extract_geometry("../../etc")
    assert exc.value.code == "INVALID_ARGUMENT"


def test_scratch_root_policy(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    assert ScratchStore.from_env({}).root is None
    assert ScratchStore.from_env({"VKM_CAD_SCRATCH": "${VKM_CAD_SCRATCH}"}).root is None
    ok = ScratchStore.from_env({"VKM_WORK": str(tmp_path / "w")})
    assert ok.root == (tmp_path / "w").resolve() / "cad_scratch"
    assert ScratchStore.from_env({"VKM_CAD_SCRATCH": str(tmp_path / "p" / "s"),
                                  "VKM_RESOURCES_ROOT": str(tmp_path / "p")}).root is None
    assert ScratchStore.from_env({"VKM_CAD_SCRATCH": str(tmp_path / "d" / "canonical" / "x"),
                                  "VKM_DATA_ROOT": str(tmp_path / "d")}).root is None
    assert ScratchStore.from_env({"VKM_CAD_SCRATCH": str(repo / "docs" / "cad")}).root is None
    assert ScratchStore.from_env({"VKM_CAD_SCRATCH": str(repo / "work" / "cad")}).root is not None
    with pytest.raises(ToolFailure) as exc:
        ScratchStore(None, "not configured").require()
    assert exc.value.code == "SCRATCH_UNAVAILABLE"


def test_save_copy_reads_the_saved_file_only(tmp_path):
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    original = user_dir / "План рудника.dxf"
    original.write_bytes(dxf.to_bytes(dxf.new_document()))
    before = original.read_bytes()

    class Doc:
        _oleobj_ = object()
        Name, FullName, Saved, ReadOnly = original.name, str(original), False, False

    class Docs:
        _oleobj_ = object()
        Count = 1

        def Item(self, i):  # noqa: N802
            return Doc()

    class App:
        _oleobj_ = object()
        Documents = Docs()

    session = ComSession(attach=lambda p: App(), com_error=RuntimeError, variant=object)
    svc = CadService(ScratchStore(tmp_path / "scratch"), com=session,
                     detector=lambda: {"capabilities": {"read_open_documents": "AVAILABLE"}, "products": []})
    out = svc.save_copy("#1")
    assert out["captured_state"] == "LAST_SAVED_ON_DISK" and out["unsaved_changes"] is True
    assert out["original_unchanged"] and out["format"] == "dxf" and str(user_dir) not in json.dumps(out)
    assert original.read_bytes() == before
    copy_dir = tmp_path / "scratch" / out["scratch_doc_id"]
    assert (copy_dir / "in" / "source.dxf").read_bytes() == before == (copy_dir / "doc.dxf").read_bytes()
    assert svc.extract_geometry(out["scratch_doc_id"])["summary"]["entities"] == 0
    unavailable = CadService(ScratchStore(tmp_path / "s2"),
                             detector=lambda: {"capabilities": {"read_open_documents": "UNAVAILABLE:NOT_WINDOWS"},
                                               "products": []})
    with pytest.raises(ToolFailure) as exc:
        unavailable.list_open_documents()
    assert exc.value.code == "CAD_UNAVAILABLE"
