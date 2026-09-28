"""vkm-cad v1 building blocks without AutoCAD: script frame (LISP), jobs and receipts, the engine lock, tables and
explicit transforms, drawings from a spec, the pure-Python fallback, the .NET toolchain helpers and the audit diff."""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest

from cad_toy import grid
from vkm_cad import dotnet, lisp, tables
from vkm_cad.audit import Audit, files_diff, files_snapshot, registry_diff
from vkm_cad.errors import ToolFailure
from vkm_cad.jobs import JOB_ID, EngineLock, JobStore, logical_source, render_receipt, safe_name

ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------------------------------------ script frame
def test_script_frame_encoding_markers_and_no_empty_line_before_epilogue(tmp_path):
    run_dir = tmp_path / "Диплом run" / "R001"
    text = lisp.script("R001", run_dir, "_.LINE 0,0 1,1\n", run_dir / "out.dwg", [tmp_path / "host.dll"])
    data = lisp.encode(text)
    assert data.startswith(lisp.BOM)
    decoded = data[3:].decode("utf-8")
    assert "\r\n" in decoded and "\n" not in decoded.replace("\r\n", "")          # CRLF only
    # an empty line at the command prompt repeats the last command: never between body and epilogue
    assert "\r\n\r\n" not in decoded
    assert '(setvar "SECURELOAD" 0)' in text and '(setvar "FILEDIA" 0)' in text and '(setvar "CMDDIA" 0)' in text
    assert f'(setq vkm:run-dir "{run_dir.as_posix()}/")' in text                  # forward slashes, trailing /
    assert '(command "_.NETLOAD"' in text and "(vkm:finish" in text and text.rstrip().endswith("_.QUIT _Y")
    assert text.index("_.LINE") < text.index("(vkm:finish")
    read_only = lisp.script("R002", run_dir, "(princ)", None)
    assert "(vkm:finish nil)" in read_only


def test_lisp_string_and_paths():
    assert lisp.lisp_str('a "b" \\ c\nd') == '"a \\"b\\" \\\\ c\\nd"'
    assert lisp.lisp_path(Path("C:/x y/Мульда.dwg")) == '"C:/x y/Мульда.dwg"'
    body = lisp.load_user_lisp(Path("C:/r/user.lsp"))
    assert "vl-catch-all-apply 'load" in body and "(vkm:result vkm:r)" in body
    assert "ISO_full_bleed_A3" in lisp.plot_model(Path("C:/r/p.pdf"), "ISO_full_bleed_A3_(420.00_x_297.00_MM)")
    assert '"_.-PDFIMPORT" "_F"' in lisp.pdf_import(Path("C:/r/in.pdf"), 2)
    assert '"_.-PLOT" "_N" "VKM_A3"' in lisp.plot_layout("VKM_A3", Path("C:/r/s.pdf"))


# ------------------------------------------------------------------------------------------------ jobs
def test_jobs_layout_inputs_outputs_and_receipt(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create(label="t", product="C3D")
    assert JOB_ID.match(job.job_id) and all((job.path / d).is_dir() for d in ("in", "runs", "out", "work"))
    source = tmp_path / "ext" / "таблица.csv"
    source.parent.mkdir()
    source.write_text("x,y\n1,2\n", encoding="utf-8")
    entry = job.copy_input(source, env={"VKM_WORK": str(tmp_path / "nowhere")})
    assert entry["source"] == "<external>/таблица.csv" and entry["original_unchanged"]
    assert job.dir(entry["path"]).read_bytes() == source.read_bytes()
    assert logical_source(source, {"VKM_WORK": str(tmp_path)}) == "$VKM_WORK/ext/таблица.csv"
    again = job.copy_input(source)                                     # never overwrites an input
    assert again["path"] != entry["path"]
    run_id, run_dir = job.new_run("exec:scr")
    assert run_id == "R001" and run_dir.is_dir()
    produced = run_dir / "out.dwg"
    produced.write_bytes(b"AC1032 toy")
    promoted = job.promote_drawing(produced, run_id)
    assert job.has_drawing() and promoted["sha256"] == job.state()["drawing"]["sha256"]
    (job.dir("out") / "a.json").write_text("{}", encoding="utf-8")
    out = job.register_output("out/a.json", "X", {"kind": "DERIVATION"}, run_id)
    assert out["logical"] == f"job:{job.job_id}/out/a.json"
    job.finish_run(run_id, {"status": "OK"})
    receipt = json.loads(job.dir("receipt.json").read_text(encoding="utf-8"))
    assert receipt["schema"] == "vkm-cad.job_receipt/1" and receipt["runs"][0]["status"] == "OK"
    assert str(tmp_path) not in json.dumps(receipt) and receipt["rules"]["crs_default"] == "UNKNOWN_CRS"
    with pytest.raises(ToolFailure) as exc:
        job.out_path("a.json")
    assert exc.value.code == "WOULD_OVERWRITE"
    assert store.list()[0]["job_id"] == job.job_id
    with pytest.raises(ToolFailure) as exc:
        store.get("CADJ-20260101T000000Z-00000000")
    assert exc.value.code == "CAD_JOB_NOT_FOUND"
    with pytest.raises(ToolFailure):
        store.get("../etc")
    for bad in ("../x", "a/b", "c:d", ""):
        with pytest.raises(ToolFailure):
            safe_name(bad)
    assert render_receipt(job.state())["product"] == "C3D"


def test_jobs_root_policy(tmp_path):
    assert JobStore.from_env({}).root is None
    assert JobStore.from_env({"VKM_WORK": str(tmp_path)}).root == (tmp_path / "cad_jobs").resolve()
    assert JobStore.from_env({"VKM_CAD_JOBS": str(tmp_path / "r" / "j"), "VKM_RESOURCES_ROOT": str(tmp_path / "r")}
                             ).root is None
    assert JobStore.from_env({"VKM_CAD_JOBS": str(ROOT / "docs" / "jobs")}).root is None
    assert JobStore.from_env({"VKM_CAD_JOBS": str(ROOT / "work" / "cad_jobs")}).root is not None
    with pytest.raises(ToolFailure) as exc:
        JobStore(None, "x").require()
    assert exc.value.code == "JOBS_UNAVAILABLE"


def test_engine_lock_busy_and_stale(tmp_path):
    lock = EngineLock(tmp_path, {"job_id": "J", "run_id": "R001"}, pid_alive=lambda pid: True)
    with lock:
        other = EngineLock(tmp_path, {"job_id": "K", "run_id": "R001"}, pid_alive=lambda pid: True)
        with pytest.raises(ToolFailure) as exc:
            other.acquire()
        assert exc.value.code == "CAD_ENGINE_BUSY" and exc.value.retryable and exc.value.details["job_id"] == "J"
        stale = EngineLock(tmp_path, {"job_id": "K", "run_id": "R002"}, pid_alive=lambda pid: False)
        stale.acquire()                                        # the holder's process is gone: taken over
        stale.release()
    assert not (tmp_path / "_engine.lock").exists()


# ------------------------------------------------------------------------------------------------ tables
def test_tables_csv_decimal_comma_json_and_unknown_z(tmp_path):
    csv_path = tmp_path / "t.csv"
    csv_path.write_text("id;X;Y;H\nRp1;1,5;2,25;100,125\nRp2;3;4;\n", encoding="utf-8")
    rows = tables.point_rows(tables.read_records(csv_path), {"name": "id", "x": "X", "y": "Y", "z": "H"}, decimal=",")
    assert rows[0] == {"name": "Rp1", "x": 1.5, "y": 2.25, "z": 100.125, "desc": None}
    assert rows[1]["z"] is None                                       # unknown stays unknown
    js = tmp_path / "t.json"
    js.write_text(json.dumps({"rows": [{"name": "a", "x": 1, "y": 2, "z": 3}]}), encoding="utf-8")
    assert tables.point_rows(tables.read_records(js), None)[0]["z"] == 3.0
    with pytest.raises(ToolFailure) as exc:
        tables.point_rows([{"a": 1}], None)
    assert exc.value.code == "TABLE_FORMAT_ERROR"
    with pytest.raises(ToolFailure):
        tables.point_rows([{"name": "a", "x": 1, "y": 2}, {"name": "a", "x": 2, "y": 3}], None)   # duplicates
    with pytest.raises(ToolFailure) as exc:
        tables.point_rows([{"x": "1,5", "y": 2}], None)                                         # decimal "."
    assert exc.value.code == "TABLE_FORMAT_ERROR"
    with pytest.raises(ToolFailure) as exc:
        tables.read_records(tmp_path / "t.xlsx")
    assert exc.value.code == "FORMAT_NOT_SUPPORTED"


def test_explicit_transforms_and_crs_block():
    assert tables.check_transform(None) is None and tables.crs_block(None)["crs_status"] == "UNKNOWN_CRS"
    for bad in ({"type": "epsg", "basis": "x" * 20}, {"type": "offset", "params": {"dx": 1, "dy": 2}},
                {"type": "offset", "params": {"dx": 1, "dy": 2}, "basis": "short"}):
        with pytest.raises(ToolFailure) as exc:
            tables.check_transform(bad)
        assert exc.value.code == "CRS_STATUS_NOT_ALLOWED"
    t = tables.check_transform({"type": "helmert2d", "params": {"tx": 10, "ty": 20, "scale": 2, "rotation_deg": 90},
                                "basis": "toy transform for the test", "target_crs_label": "local toy"})
    moved = tables.apply_transform([{"name": "a", "x": 1.0, "y": 0.0, "z": 5.0, "desc": None}], t)[0]
    assert math.isclose(moved["x"], 10.0, abs_tol=1e-9) and math.isclose(moved["y"], 22.0) and moved["z"] == 5.0
    affine = tables.check_transform({"type": "affine2d", "params": dict(a=1, b=0, c=5, d=0, e=1, f=-5, dz=1),
                                     "basis": "toy affine for the test"})
    assert tables.transform_points([[1, 1, 1]], affine) == [[6.0, -4.0, 2.0]]
    crs = tables.crs_block(t)
    assert crs["crs_status"] == "EXPLICIT_TRANSFORM" and crs["epsg"] is None
    assert crs["crs_status_basis"]["kind"] == "MODEL_CHOICE" and crs["crs_status_basis"]["target_crs_label"] == "local toy"


# ------------------------------------------------------------------------------------------------ drawings
def test_draw_spec_all_entity_types_roundtrip():
    pytest.importorskip("ezdxf")
    from vkm_cad import draw, dxf

    spec = {"units": "m", "layers": [{"name": "Скважины", "color": 1, "linetype": "DASHED"}],
            "blocks": [{"name": "BH", "entities": [{"type": "circle", "center": [0, 0], "radius": 1}],
                        "attdefs": [{"tag": "ID", "at": [1.5, 0], "height": 1.5}]}],
            "entities": [{"type": "point", "at": [1, 2, 3]}, {"type": "line", "start": [0, 0], "end": [1, 1]},
                         {"type": "polyline", "points": [[0, 0], [5, 0], [5, 5]], "closed": True, "elevation": 7},
                         {"type": "polyline3d", "points": [[0, 0, 1], [1, 1, 2]]},
                         {"type": "circle", "center": [3, 3], "radius": 2, "layer": "Скважины"},
                         {"type": "arc", "center": [3, 3], "radius": 3, "start_angle": 0, "end_angle": 90},
                         {"type": "text", "at": [0, -2], "text": "Мульда", "height": 1.5, "align": "MIDDLE_CENTER"},
                         {"type": "mtext", "at": [0, -5], "text": "две\\Pстроки", "height": 1, "width": 20},
                         {"type": "hatch", "boundary": [[0, 0], [2, 0], [2, 2]], "pattern": "ANSI31"},
                         {"type": "hatch", "boundary": [[5, 5], [6, 5], [6, 6]]},
                         {"type": "insert", "block": "BH", "at": [10, 10], "attributes": {"ID": "скв. 1"}},
                         {"type": "dimension", "kind": "linear", "p1": [0, 0], "p2": [5, 0], "base": [0, 3]},
                         {"type": "dimension", "kind": "aligned", "p1": [0, 0], "p2": [5, 5], "distance": 2}]}
    doc, summary = draw.draw_spec(spec)
    assert summary["insunits"] == 6 and summary["counts"]["dimension"] == 2 and summary["counts"]["block"] == 1
    data = dxf.to_bytes(doc)
    assert data == dxf.to_bytes(draw.draw_spec(spec)[0])                       # deterministic bytes
    back = dxf.read_bytes(data)
    types = [e.dxftype() for e in back.modelspace()]
    for kind in ("POINT", "LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "TEXT", "MTEXT", "HATCH", "INSERT",
                 "DIMENSION"):
        assert kind in types, kind
    insert = next(e for e in back.modelspace() if e.dxftype() == "INSERT")
    assert insert.get_attrib_text("ID") == "скв. 1"
    assert "Скважины" in back.layers and draw.TEXT_STYLE in back.styles
    for bad in ({"entities": [{"type": "spline"}]}, {"entities": [{"type": "insert", "block": "NOPE", "at": [0, 0]}]},
                {"entities": [{"type": "line", "start": [0, "x"], "end": [1, 1]}]}, {"units": "furlong"},
                {"entities": [{"type": "circle", "center": [0, 0], "radius": 1, "color": 999}]}):
        with pytest.raises(ToolFailure):
            draw.draw_spec(bad)


# ------------------------------------------------------------------------------------------------ fallback
def _tin(depth: float, name: str):
    from vkm_cad import fallback

    return fallback.build_tin([[r["x"], r["y"], r["z"]] for r in grid(depth)], name=name)


def test_fallback_tin_contours_difference_profile():
    pytest.importorskip("scipy")
    from vkm_cad import fallback

    s1, s2 = _tin(0.0, "S1"), _tin(1.5, "S2")
    st = s2.stats()
    assert st["triangles"] == 162 and st["points"] == 100 and st["area_2d"] == pytest.approx(8100.0)
    assert s2.elevation_at(45.0, 45.0) is not None and s2.elevation_at(200.0, 0.0) is None
    assert s1.elevation_at(20.0, 30.0) == pytest.approx(100.2)                   # plane: exact
    lines = fallback.contours(s2, 0.25, 1.0)
    assert lines and all(abs(p["elevation"] / 0.25 - round(p["elevation"] / 0.25)) < 1e-9 for p in lines)
    assert any(p["closed"] for p in lines) and any(p["major"] for p in lines)
    assert all(len(p["points"]) >= 2 for p in lines)
    dz, stats = fallback.difference(s1, s2, name="D")
    assert stats["dz_surface"]["z_min"] == pytest.approx(-1.5 * math.exp(-50 / 800), abs=1e-4)
    assert stats["fill_volume"] == pytest.approx(0.0, abs=1e-6) and stats["cut_volume"] > 3000
    # analytic volume of the sampled Gaussian over the grid area ≈ 2πσ²·depth·coverage; the TIN is within 5 %
    assert 0.9 < stats["cut_volume"] / (2 * math.pi * 400 * 1.5 * 0.94) < 1.1
    rows = fallback.profile([s1, s2], [[0, 45], [90, 45]], 5.0)
    assert rows[0]["station"] == 0 and rows[-1]["station"] == 90 and len(rows) == 19
    deepest = min(rows, key=lambda r: r["S2"] - r["S1"])
    assert 40 <= deepest["station"] <= 50
    off = fallback.profile([s1], [[80, 45], [120, 45]], 10.0)
    assert off[-1]["S1"] is None                                                   # outside the TIN: unknown
    back = fallback.Tin.from_json(json.loads(fallback.tin_json_bytes(s2)))
    assert back.stats() == s2.stats()


def test_fallback_boundary_max_edge_duplicates_and_breaklines():
    pytest.importorskip("scipy")
    from vkm_cad import fallback

    pts = [[r["x"], r["y"], r["z"]] for r in grid(0.0)] + [[0.0, 0.0, 55.0]]      # duplicate XY, other Z
    tin = fallback.build_tin(pts, name="B", boundary=[[0, 0], [45, 0], [45, 90], [0, 90]], max_edge=11.0,
                             breaklines=[[[5, 5, 100], [15, 5, 100]]])
    assert tin.warnings and "duplicate" in tin.warnings[0]
    assert any("NOT enforced" in c for c in tin.model_choices) and any("boundary" in c for c in tin.model_choices)
    st = tin.stats()
    assert st["x_max"] <= 45.0 + 1e-9 and st["max_triangle_edge"] <= 11.0
    with pytest.raises(ToolFailure):
        fallback.build_tin([[0, 0, 1], [1, 1, 1]], name="X")
    with pytest.raises(ToolFailure):
        fallback.stations([[0, 0], [0, 0]], 1.0)
    with pytest.raises(ToolFailure):
        fallback.levels(0, 1, 0)


# ------------------------------------------------------------------------------------------------ .NET helpers
def _pe(clr: bool) -> bytes:
    data = bytearray(4096)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    opt = 0x80 + 24
    struct.pack_into("<H", data, opt, 0x20B)                                         # PE32+
    struct.pack_into("<II", data, opt + 112 + 14 * 8, 0x2000 if clr else 0, 0x48 if clr else 0)
    return bytes(data)


def test_dotnet_discovery_and_sources(tmp_path):
    managed, native, text = tmp_path / "m.dll", tmp_path / "n.dll", tmp_path / "t.dll"
    managed.write_bytes(_pe(True))
    native.write_bytes(_pe(False))
    text.write_bytes(b"not a PE")
    assert dotnet.is_managed(managed) and not dotnet.is_managed(native) and not dotnet.is_managed(text)
    runtimes = ("Microsoft.AspNetCore.App 8.0.22 [Z:\\dotnet\\shared\\Microsoft.AspNetCore.App]\n"
                "Microsoft.NETCore.App 6.0.3 [Z:\\dotnet\\shared\\Microsoft.NETCore.App]\n"
                "Microsoft.NETCore.App 8.0.9 [Z:\\dotnet\\shared\\Microsoft.NETCore.App]\n"
                "Microsoft.NETCore.App 8.0.22 [Z:\\dotnet\\shared\\Microsoft.NETCore.App]\n")
    found = dotnet.find_runtime(8, run=lambda cmd: runtimes)
    assert found is not None and found[1] == "8.0.22" and found[0].name == "8.0.22"
    assert dotnet.find_runtime(9, run=lambda cmd: runtimes) is None
    outputs = {"--list-sdks": "6.0.201 [Z:\\dotnet\\sdk]\n", "-all": "Z:\\VS\n"}
    compilers = dotnet.find_compilers({"programfiles(x86)": "Z:\\PF86"},
                                      run=lambda cmd: next((v for k, v in outputs.items() if k in cmd), None),
                                      exists=lambda p: True)
    assert [k for k, _ in compilers] == ["VS_ROSLYN", "SDK_ROSLYN"]              # case-insensitive env lookup
    assert compilers[1][1][0] == "dotnet" and compilers[1][1][1].endswith("csc.dll")
    user = dotnet.user_source('return "x";', civil=True).decode("utf-8")
    assert "CommandMethod(\"VKMUSER\"" in user and "CivilDocument.GetCivilDocument(db)" in user
    expr = dotnet.user_source("db.Insunits", civil=False, expression=True).decode("utf-8")
    assert "return (object)(db.Insunits);" in expr and "Autodesk.Civil" not in expr
    assert dotnet._strip_paths("Z:\\a\\b\\UserJob.cs(3,5): error CS1002: ; expected") == \
        "UserJob.cs(3,5): error CS1002: ; expected"
    host = dotnet.host_source().decode("utf-8")
    assert 'CommandMethod("VKMHOST"' in host and "VKM_CAD_REQUEST" in host
    for token in ("SendCommand", "Process.Start", "WebClient", "HttpClient"):
        assert token not in host                                    # the host runs no commands and no network


def test_dotnet_compile_error_is_reported(tmp_path):
    chain = dotnet.Toolchain(["csc.exe"], "VS_ROSLYN", tmp_path, "8.0.22", tmp_path, True, [])

    class Proc:
        returncode = 1
        stdout = "Z:\\x\\UserJob.cs(4,1): error CS0103: The name 'y' does not exist\n".encode("utf-8")
        stderr = b""

    result = dotnet.compile_library(chain, [tmp_path / "a.cs"], tmp_path / "out" / "a.dll",
                                    runner=lambda *a, **k: Proc())
    assert not result.ok and result.errors == ["UserJob.cs(4,1): error CS0103: The name 'y' does not exist"]
    assert (tmp_path / "out" / "a.rsp").read_text(encoding="utf-8").count("/reference:") == 0


# ------------------------------------------------------------------------------------------------ audit
def test_audit_diffs_and_file_snapshot(tmp_path):
    before = {("AutoCAD\\R25.1", None): "", ("AutoCAD\\R25.1", "LastLaunchedProduct"): "a"}
    after = {("AutoCAD\\R25.1", None): "", ("AutoCAD\\R25.1", "LastLaunchedProduct"): "b",
             ("AutoCAD\\R25.1\\AEC", None): ""}
    assert registry_diff(before, after) == [
        {"key": "AutoCAD\\R25.1", "value": "LastLaunchedProduct", "change": "CHANGED"},
        {"key": "AutoCAD\\R25.1\\AEC", "value": None, "change": "ADDED"}]
    root = tmp_path / "prof"
    (root / "Support").mkdir(parents=True)
    (root / "Support" / "a.aws").write_text("1", encoding="utf-8")
    snap1 = files_snapshot([("<APPDATA>/x", root)])
    (root / "Support" / "b.csv").write_text("2", encoding="utf-8")
    changes = files_diff(snap1, files_snapshot([("<APPDATA>/x", root)]))
    assert changes == [{"file": "<APPDATA>/x/Support/b.csv", "change": "ADDED"}]
    with Audit(2026, enabled=False) as audit:
        pass
    assert audit.result() == {"status": "DISABLED"}
