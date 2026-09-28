"""The v1 operations (cadjobs) on a fake Core Console: the Civil 3D chain with receipts and derivations, drawing
promotion only on success, host failures, engine choice and fallback, the exec/query channels, drawings, PDF
import, the python_com gate and cad_capabilities."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from cad_toy import grid
from vkm_cad import dotnet
from vkm_cad.cadjobs import CIVIL_ENGINE, CadJobs
from vkm_cad.detect import Installation
from vkm_cad.engine import ConsoleResult
from vkm_cad.errors import ToolFailure
from vkm_cad.jobs import JobStore


def _fake_result(op: dict[str, Any]) -> dict[str, Any]:
    a = op["args"]
    name = op["op"]
    if name == "c3d.points":
        return {"added": sum(1 for r in a["rows"] if r["z"] is not None), "point_group": a["point_group"],
                "name_policy": a["name_policy"]}
    if name == "c3d.tin":
        return {"name": a["name"], "points": 3, "triangles": 1, "z_min": 1.0, "z_max": 2.0, "x_min": 0.0,
                "y_min": 0.0, "x_max": 10.0, "y_max": 10.0,
                "triangles_export": [[[0, 0, 1], [10, 0, 1], [0, 10, 2]]], "triangles_export_truncated": False}
    if name == "c3d.contours":
        return {"count": 1, "polylines": [{"elevation": 1.5, "closed": False, "major": False,
                                           "points": [[0, 5], [5, 0]]}]}
    if name == "c3d.volume":
        return {"cut_volume": 12.5, "fill_volume": 0.0, "net_volume": -12.5,
                "dz_surface": {"z_min": -1.0, "z_max": 0.0, "x_min": 0.0, "y_min": 0.0, "x_max": 10.0,
                               "y_max": 10.0}, "dz_triangles_export": [[[0, 0, 0], [10, 0, -1], [0, 10, 0]]],
                "isolines": [{"elevation": -0.5, "closed": False, "major": False, "points": [[1, 1], [2, 2]]}]}
    if name == "c3d.alignment_profile":
        return {"alignment": a["name"], "length": 10.0, "profiles": [f"{a['name']} - {s}" for s in a["surfaces"]],
                "samples": [{"station": 0.0, "x": 0.0, "y": 5.0, **{s: 1.0 for s in a["surfaces"]}},
                            {"station": 10.0, "x": 10.0, "y": 5.0, **{s: None for s in a["surfaces"]}}]}
    if name == "acad.layout_sheet":
        return {"layout": a["name"], "media": a["media"], "scale_denominator": a["scale_denominator"] or 750.0,
                "model_window": a["model_window"]}
    raise AssertionError(name)


class FakeConsole:
    def __init__(self, fail_op: str | None = None):
        self.fail_op = fail_op
        self.specs: list[Any] = []

    def run(self, spec):
        self.specs.append(spec)
        rd = spec.run_dir
        ok = True
        failures: list[str] = []
        request = spec.env.get("VKM_CAD_REQUEST")
        if request:
            ops = json.loads(Path(request).read_text(encoding="utf-8"))["ops"]
            out = []
            for op in ops:
                if op["op"] == self.fail_op:
                    out.append({"op": op["op"], "ok": False, "error": "ArgumentException: boom"})
                    ok = False
                    break
                out.append({"op": op["op"], "ok": True, "result": _fake_result(op)})
            (rd / "result.json").write_text(json.dumps({"ok": ok, "ops": out}), encoding="utf-8")
            if not ok:
                failures.append("HOST operation failed")
        if "user.lsp" in spec.body or "query.lsp" in spec.body:
            (rd / "result.txt").write_text("42\n", encoding="utf-8")
        if "VKMUSER" in spec.body:
            (rd / "result.json").write_text(json.dumps({"ok": True, "result": 7, "log": ["x"]}), encoding="utf-8")
        for target in re.findall(r'"([^"]+\.(?:pdf|dwg|dxf))"', spec.body):
            path = Path(target)
            if path.parent == rd and not path.exists():
                if path.suffix == ".dxf":
                    from vkm_cad import draw, dxf

                    doc = draw.new_world_document("mm")
                    doc.modelspace().add_line((0, 0), (1, 1))
                    path.write_bytes(dxf.to_bytes(doc))
                else:
                    path.write_bytes(b"%PDF-fake" if path.suffix == ".pdf" else b"AC1032 fake")
        if ok and spec.save_to is not None:
            spec.save_to.write_bytes(b"AC1032 saved " + spec.run_id.encode())
        markers = [f"BEGIN {spec.run_id}", "EXTENTS (0.0 0.0 0.0) (1.0 1.0 0.0)",
                   f"END {spec.run_id} {'OK' if ok else 'FAILED'}"]
        return ConsoleResult(exit_code=0, timed_out=False, crashed=False, dialog=False, duration_s=0.1,
                             script_read=True, completed=True, ok=ok, markers=markers, failures=failures,
                             console_tail=[], killed=[], windows=[], foreign_cad_processes={},
                             command=["accoreconsole.exe"])


def _jobs(tmp_path, console=None, civil=True, env=None):
    inst = Installation(tmp_path / "acad", "R25.1", 2026, "ru-RU", "rus",
                        {"ACAD": "ACAD-9101:419", **({"C3D": "ACAD-9100:419"} if civil else {})},
                        {"ACAD": "25.1.60.0", "C3D": "13.8.280.0"})
    chain = dotnet.Toolchain(["csc.exe"], "VS_ROSLYN", tmp_path, "8.0.22", tmp_path, civil, [])
    console = console or FakeConsole()
    jobs = CadJobs(JobStore(tmp_path / "jobs"), env=env or {}, installation=lambda: inst,
                   console_factory=lambda _j: console, toolchain=lambda _j: (chain, []),
                   host_builder=lambda _j, name, _src: tmp_path / f"{name}.dll", audit=False)
    return jobs, console


def test_civil_chain_receipts_and_derivations(tmp_path):
    pytest.importorskip("ezdxf")
    jobs, console = _jobs(tmp_path)
    jid = jobs.job_create("chain", "C3D")["job_id"]
    table = tmp_path / "e2.csv"
    table.write_text("name;x;y;z\n" + "".join(f"{r['name']};{r['x']};{r['y']};{str(r['z']).replace('.', ',')}\n"
                                               for r in grid(1.5)), encoding="utf-8")
    p1 = jobs.points_from_table(job_id=jid, rows=grid(0.0), point_group="E1", name_policy="group_prefix")
    p2 = jobs.points_from_table(job_id=jid, table=str(table), point_group="E2", decimal=",")
    assert p1["engine"] == CIVIL_ENGINE and p1["civil3d"]["added"] == 100 and p2["input"]["source"].startswith("<")
    assert any("group>_<name>" in c for c in p1["outputs"][0]["derivation"]["model_choices"])
    s1 = jobs.tin_surface(job_id=jid, name="S1", point_group="E1")
    jobs.tin_surface(job_id=jid, name="S2", point_group="E2")
    host_args = json.loads((console.specs[-1].run_dir / "request.json").read_text(encoding="utf-8"))["ops"][0]["args"]
    assert host_args["point_group"] == "E2"                                   # Civil point group reused
    assert s1["outputs"][0]["derivation"]["kind"] == "INTERPOLATION" and s1["crs_status"] == "UNKNOWN_CRS"
    c = jobs.contours(job_id=jid, surface="S1", interval=0.5, major_interval=1.0)
    assert c["count"] == 1 and c["outputs"][0]["derivation"]["kind"] == "DERIVATION"
    d = jobs.difference_surface(job_id=jid, base="S1", compare="S2", name="TROUGH", contour_interval=0.5)
    assert d["stats"]["cut_volume"] == 12.5 and d["isolines"] == 1 and d["dz_surface"] == "TROUGH_DZ"
    assert "not an observation" in d["outputs"][0]["derivation"]["assumptions"][0]
    pr = jobs.alignment_profile(job_id=jid, name="L1", polyline=[[0, 5], [10, 5]], surfaces=["S1", "S2"])
    assert pr["samples"] == 2 and pr["preview"][1]["S1"] is None
    csv_text = jobs.store.get(jid).dir(pr["outputs"][0]["path"]).read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == "station,x,y,S1,S2" and csv_text.splitlines()[2].endswith(",,")
    sheet = jobs.layout_sheet(job_id=jid, name="SHEET", paper="A1", orientation="portrait", scale_denominator=500,
                              title_block={"title": "Мульда"})
    assert sheet["media"] == "ISO_full_bleed_A1_(594.00_x_841.00_MM)"
    assert sheet["model_window_source"].startswith("union") and sheet["model_window"][0][0] < 0
    pdf = jobs.plot_pdf(job_id=jid, layouts=["SHEET"], out_name="мульда")
    assert pdf["pdfs"][0]["path"] == "out/мульда.pdf" and pdf["pdfs"][0]["derivation"]["method"].startswith("plot")
    state = jobs.store.get(jid).state()
    assert all(r["status"] == "OK" for r in state["runs"]) and len(state["runs"]) == 9
    assert state["drawing"]["from_run"] == "R008"                             # the sheet run saved last
    receipt = json.loads(jobs.store.get(jid).dir("receipt.json").read_text(encoding="utf-8"))
    assert str(tmp_path) not in json.dumps(receipt)
    for out in receipt["outputs"]:
        assert out["derivation"]["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
        assert out["derivation"]["never_input_of_extraction"] is True and out["derivation"]["epsg"] is None
    assert console.specs[-1].save_to is None                                  # plotting saves nothing


def test_failed_host_op_keeps_the_drawing_and_reports(tmp_path):
    pytest.importorskip("ezdxf")
    jobs, _console = _jobs(tmp_path, FakeConsole(fail_op="c3d.tin"))
    jid = jobs.job_create("fail", "C3D")["job_id"]
    jobs.points_from_table(job_id=jid, rows=grid(0.0), point_group="E1")
    before = jobs.store.get(jid).state()["drawing"]
    with pytest.raises(ToolFailure) as exc:
        jobs.tin_surface(job_id=jid, name="S1", point_group="E1")
    assert exc.value.code == "HOST_OP_FAILED" and "boom" in exc.value.message and exc.value.details["run_id"] == "R002"
    state = jobs.store.get(jid).state()
    assert state["drawing"] == before and state["runs"][-1]["status"] == "FAILED"
    assert "S1" not in state["surfaces"]


def test_engine_choice_and_fallback(tmp_path):
    pytest.importorskip("scipy")
    pytest.importorskip("ezdxf")
    jobs, _console = _jobs(tmp_path, civil=False)
    acad = jobs.job_create("acad", "ACAD")["job_id"]
    with pytest.raises(ToolFailure) as exc:
        jobs.points_from_table(job_id=acad, rows=grid(0.0), point_group="E1", engine="civil3d")
    assert exc.value.code == "CIVIL3D_UNAVAILABLE"
    pts = jobs.points_from_table(job_id=acad, rows=grid(0.0), point_group="E1")          # auto → fallback
    assert pts["engine"] == "PURE_PYTHON_FALLBACK" and pts["warnings"]
    jobs.points_from_table(job_id=acad, rows=grid(1.5), point_group="E2")
    s = jobs.tin_surface(job_id=acad, name="S1", point_group="E1")
    jobs.tin_surface(job_id=acad, name="S2", point_group="E2")
    assert s["stats"]["triangles"] == 162 and any("NOT" not in c for c in s["model_choices"])
    d = jobs.difference_surface(job_id=acad, base="S1", compare="S2", name="T", contour_interval=0.25)
    assert d["engine"] == "PURE_PYTHON_FALLBACK" and d["stats"]["cut_volume"] > 3000 and d["isolines"] > 0
    c = jobs.contours(job_id=acad, surface="T_DZ", interval=0.5)
    assert c["levels"] and max(c["levels"]) <= 0
    with pytest.raises(ToolFailure) as exc:
        jobs.tin_surface(job_id=acad, name="S1", point_group="E1")
    assert exc.value.code == "WOULD_OVERWRITE"
    with pytest.raises(ToolFailure) as exc:
        jobs.contours(job_id=acad, surface="NOPE", interval=1.0)
    assert exc.value.code == "SURFACE_NOT_FOUND"
    with pytest.raises(ToolFailure) as exc:
        jobs.layout_sheet(job_id=acad)
    assert exc.value.code == "INPUT_NOT_FOUND"                                 # no drawing in a fallback job


def test_exec_and_query_channels(tmp_path):
    jobs, console = _jobs(tmp_path)
    jid = jobs.job_create("exec", "C3D")["job_id"]
    scr = jobs.exec(jid, "scr", '(command "_.CIRCLE" "0,0" "5")\n')
    assert scr["ok"] and scr["drawing"]["from_run"] == "R001" and console.specs[-1].input_drawing is None
    lsp = jobs.exec(jid, "lisp", "(+ 40 2)", save=False)
    assert lsp["value"] == "42" and console.specs[-1].input_drawing.name == "in.dwg"
    assert (console.specs[-1].run_dir / "user.lsp").read_bytes().startswith(b"\xef\xbb\xbf")
    cs = jobs.exec(jid, "csharp", "return 7;")
    assert cs["value"] == 7 and cs["log"] == ["x"] and console.specs[-1].netload[0].name == "VkmCadUser.dll"
    q = jobs.query(jid, '(getvar "INSUNITS")')
    assert q["value"] == "42" and console.specs[-1].save_to is None
    q2 = jobs.query(jid, "db.Insunits", kind="csharp")
    assert q2["value"] == 7
    with pytest.raises(ToolFailure) as exc:
        jobs.exec(jid, "python_com", "x = 1")
    assert exc.value.code == "HIDDEN_INSTANCE_NOT_ALLOWED"
    with pytest.raises(ToolFailure) as exc:
        jobs.exec(jid, "vba", "x")
    assert exc.value.code == "INVALID_ARGUMENT"
    state = jobs.store.get(jid).state()
    assert [r["kind"] for r in state["runs"]] == ["exec:scr", "exec:lisp", "exec:csharp", "query:lisp",
                                                  "query:csharp"]
    assert all(r["code_sha256"] for r in state["runs"])


def test_draw_convert_and_pdf_import(tmp_path):
    pytest.importorskip("ezdxf")
    jobs, console = _jobs(tmp_path)
    spec = {"units": "m", "entities": [{"type": "line", "start": [0, 0], "end": [10, 5]}]}
    drawn = jobs.draw(spec, name="схема", to_dwg=True, as_job_drawing=True)
    assert drawn["dxf"]["path"] == "out/схема.dxf" and drawn["dwg"]["path"] == "out/схема.dwg"
    assert drawn["dwg"]["job_drawing"]["path"] == "work/drawing.dwg"
    assert console.specs[-1].input_drawing.suffix == ".dxf"
    back = jobs.convert(drawn["job_id"], "drawing", "dxf", name="back")
    assert back["path"] == "out/back.dxf" and back["derivation"]["method"].startswith("DWG")
    with pytest.raises(ToolFailure):
        jobs.convert(drawn["job_id"], "../secret.dwg", "dxf")
    with pytest.raises(ToolFailure):
        jobs.convert(drawn["job_id"], "drawing", "dwg", as_job_drawing=True)
    # a DXF of any origin (here a fallback-style output) → the job drawing → a sheet
    plain = jobs.draw(spec, name="fallback_like")
    conv = jobs.convert(plain["job_id"], "out/fallback_like.dxf", "dwg", as_job_drawing=True)
    assert conv["job_drawing"]["path"] == "work/drawing.dwg"
    assert jobs.layout_sheet(job_id=plain["job_id"], scale_denominator=100)["model_window_source"] == \
        "drawing extents"
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4 toy")
    imp = jobs.pdf_import(pdf=str(pdf), pages=[1, 2])
    assert [p["page"] for p in imp["pages"]] == [1, 2] and imp["pages"][0]["summary"]["entities"] == 1
    assert imp["pages"][0]["dwg"]["path"] == "out/in_p001.dwg" and console.specs[-1].input_drawing is None
    with pytest.raises(ToolFailure) as exc:
        jobs.pdf_import(pdf=str(tmp_path / "in.txt"))
    assert exc.value.code == "FORMAT_NOT_SUPPORTED"


def test_capabilities_and_missing_engine(tmp_path):
    jobs, _console = _jobs(tmp_path)
    caps = jobs.capabilities()
    assert caps["channels"]["dotnet"]["compiler"] == "VS_ROSLYN"
    assert caps["exec_kinds"]["python_com"]["available"] is False
    assert {"cad_draw", "c3d_tin_surface", "cad_pdf_import"} <= {o["tool"] for o in caps["operations"]}
    assert any(row["area"].startswith("TIN") for row in caps["civil3d_api_reach"])
    bare = CadJobs(JobStore(tmp_path / "j2"), env={}, installation=lambda: None,
                   toolchain=lambda _j: (None, ["no compiler"]), audit=False)
    job = bare.job_create("x", "C3D")["job_id"]
    with pytest.raises(ToolFailure) as exc:
        bare.exec(job, "scr", "_.LINE")
    assert exc.value.code == "CAD_ENGINE_UNAVAILABLE"
    assert bare.capabilities()["channels"]["dotnet"]["reasons"] == ["no compiler"]
