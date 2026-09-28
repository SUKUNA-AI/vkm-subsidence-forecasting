"""CAD-L (``desktop``, ``VKM_TEST_CAD_LIVE=1``): the v1 chain on the real AutoCAD 2026 / Civil 3D 2026, headless.

Toy data only: a 10 × 10 grid (10 m) in two epochs, the second with a synthetic Gaussian trough (1.5 m). Chain:
COGO points → TIN → contours → difference surface (trough, volumes, dz isolines) → profile along a line through the
trough → A3 sheet with a title block → PDF; plus cad_draw DXF → DWG, DWG → DXF, a hand-written PDF → -PDFIMPORT, the
four exec channels available headless (scr, lisp, csharp, query) and the pure-Python fallback for comparison.
``VKM_CAD_LIVE_RECEIPT=<file>`` writes a receipt (timings, sizes, hashes; logical paths only).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cad_toy import grid, synthetic_pdf

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(os.environ.get("VKM_TEST_CAD_LIVE") != "1" or sys.platform != "win32",
                                 reason="live AutoCAD test: set VKM_TEST_CAD_LIVE=1 on the workstation")]


def _out(result: dict, suffix: str) -> dict:
    return next(o for o in result["outputs"] if o["path"].endswith(suffix))


def test_live_chain(tmp_path_factory):
    from vkm_cad import __version__
    from vkm_cad.cadjobs import CadJobs
    from vkm_cad.jobs import JobStore

    root = Path(os.environ.get("VKM_CAD_LIVE_ROOT") or tmp_path_factory.mktemp("cadlive"))
    env = {**os.environ, "VKM_CAD_JOBS": str(root / "cad_jobs")}
    jobs = CadJobs(JobStore.from_env(env), env=env)
    steps: list[dict] = []

    def step(name: str, fn, keep=lambda r: {}):
        t0 = time.perf_counter()
        result = fn()
        steps.append({"step": name, "seconds": round(time.perf_counter() - t0, 2), **keep(result)})
        return result

    caps = step("cad_capabilities", jobs.capabilities, lambda r: {
        "core_console": r["channels"]["core_console"]["available"], "dotnet": r["channels"]["dotnet"]["compiler"],
        "civil_references": r["channels"]["dotnet"]["civil_references"]})
    assert caps["channels"]["core_console"]["available"] and caps["channels"]["dotnet"]["available"]

    # ---------------------------------------------------------------- Civil 3D chain (headless .NET)
    job = step("cad_job_create C3D", lambda: jobs.job_create("smoke trough", "C3D"))
    jid = job["job_id"]
    tables = root / "tables"
    tables.mkdir(exist_ok=True)
    epoch2 = tables / "epoch2.csv"               # Russian number format: ';' and decimal comma
    epoch2.write_text("name;x;y;z;desc\n" + "".join(
        f"{r['name']};{str(r['x']).replace('.', ',')};{str(r['y']).replace('.', ',')};"
        f"{str(r['z']).replace('.', ',')};{r['desc']}\n" for r in grid(1.5)), encoding="utf-8")
    p1 = step("c3d_points_from_table E1 (inline rows)", lambda: jobs.points_from_table(
        job_id=jid, rows=grid(0.0), point_group="E1", units="m", name_policy="group_prefix"),
        lambda r: {"added": r["civil3d"]["added"], "engine": r["engine"]})
    p2 = step("c3d_points_from_table E2 (CSV ; decimal comma)", lambda: jobs.points_from_table(
        job_id=jid, table=str(epoch2), point_group="E2", decimal=",", units="m", name_policy="group_prefix"),
        lambda r: {"added": r["civil3d"]["added"], "input_sha256": r["input"]["sha256"]})
    assert p1["civil3d"]["added"] == 100 and p2["civil3d"]["added"] == 100 and p2["crs_status"] == "UNKNOWN_CRS"
    s1 = step("c3d_tin_surface S1", lambda: jobs.tin_surface(job_id=jid, name="S1", point_group="E1", units="m"),
              lambda r: {k: r["stats"][k] for k in ("points", "triangles", "z_min", "z_max")})
    s2 = step("c3d_tin_surface S2", lambda: jobs.tin_surface(job_id=jid, name="S2", point_group="E2", units="m"),
              lambda r: {k: r["stats"][k] for k in ("points", "triangles", "z_min", "z_max")})
    assert s1["stats"]["triangles"] == s2["stats"]["triangles"] == 162
    assert s1["engine"] == "CIVIL3D_HEADLESS_DOTNET"
    c2 = step("c3d_contours S2 (0.25 / 1.0)", lambda: jobs.contours(job_id=jid, surface="S2", interval=0.25,
                                                                    major_interval=1.0, units="m"),
              lambda r: {"count": r["count"], "major": r["major_count"], "levels": len(r["levels"])})
    assert c2["count"] > 0 and all(abs(lv / 0.25 - round(lv / 0.25)) < 1e-6 for lv in c2["levels"])
    d = step("c3d_difference_surface TROUGH", lambda: jobs.difference_surface(
        job_id=jid, base="S1", compare="S2", name="TROUGH", contour_interval=0.25, contour_major=1.0, units="m"),
        lambda r: {"cut_volume": r["stats"]["cut_volume"], "fill_volume": r["stats"]["fill_volume"],
                   "dz_min": r["stats"]["dz_surface"]["z_min"], "isolines": r["isolines"]})
    assert d["stats"]["cut_volume"] > 0 and d["stats"]["fill_volume"] == 0
    assert -1.5 < d["stats"]["dz_surface"]["z_min"] < -1.3
    prof = step("c3d_alignment_profile LINE1", lambda: jobs.alignment_profile(
        job_id=jid, name="LINE1", polyline=[[0, 45], [90, 45]], surfaces=["S1", "S2"], station_interval=5.0),
        lambda r: {"samples": r["samples"], "length": r.get("length")})
    deepest = min(prof["preview"], key=lambda row: row["S2"] - row["S1"])
    assert 35 <= deepest["station"] <= 55
    sheet = step("cad_layout_sheet A3 1:500", lambda: jobs.layout_sheet(
        job_id=jid, name="VKM_A3", paper="A3", scale_denominator=500, model_units="m",
        title_block={"title": "Мульда оседания (синтетика)", "designation": "VKM-CAD-SMOKE",
                     "organization": "toy data", "developer": "vkm-cad", "sheet": "1", "sheets": "1"}),
        lambda r: {"media": r["media"], "scale_denominator": r["scale_denominator"],
                   "model_window_source": r["model_window_source"]})
    assert sheet["scale_denominator"] == 500 and sheet["media"].startswith("ISO_full_bleed_A3")
    pdf = step("cad_plot_pdf VKM_A3", lambda: jobs.plot_pdf(job_id=jid, layouts=["VKM_A3"], out_name="trough_sheet"),
               lambda r: {"bytes": r["pdfs"][0]["bytes"], "sha256": r["pdfs"][0]["sha256"]})
    assert pdf["pdfs"][0]["bytes"] > 10_000

    # ---------------------------------------------------------------- exec channels on the same job
    lisp = step("cad_query lisp", lambda: jobs.query(jid, '(getvar "CTAB")'), lambda r: {"value": r["value"]})
    assert lisp["value"] == '"Model"'
    csq = step("cad_query csharp", lambda: jobs.query(jid, "civil.GetSurfaceIds().Count", kind="csharp"),
               lambda r: {"value": r["value"]})
    assert csq["value"] == 4                                            # S1, S2, TROUGH, TROUGH_DZ
    scr = step("cad_exec scr", lambda: jobs.exec(jid, "scr", '(command "_.CIRCLE" "45,45" "30")\n'),
               lambda r: {"ok": r["ok"]})
    assert scr["ok"]
    lval = step("cad_exec lisp", lambda: jobs.exec(jid, "lisp", '(sslength (ssget "_X" (list (cons 0 "CIRCLE"))))',
                                                   save=False), lambda r: {"value": r["value"]})
    assert lval["value"] == "1"
    csx = step("cad_exec csharp", lambda: jobs.exec(jid, "csharp", "var n = 0;\nforeach (ObjectId id in civil."
                                                    "GetAlignmentIds()) n++;\nreturn n;", save=False),
               lambda r: {"value": r["value"]})
    assert csx["value"] == 1

    # ---------------------------------------------------------------- drawing, conversions, PDF import (ACAD)
    spec = {"units": "m", "layers": [{"name": "Скважины", "color": 1}],
            "blocks": [{"name": "BH", "entities": [{"type": "circle", "center": [0, 0], "radius": 1}],
                        "attdefs": [{"tag": "ID", "at": [1.5, 0], "height": 1.5}]}],
            "entities": [{"type": "insert", "block": "BH", "at": [10, 10], "layer": "Скважины",
                          "attributes": {"ID": "скв. 1"}},
                         {"type": "polyline", "points": [[0, 0], [50, 0], [50, 30]], "layer": "Линия"},
                         {"type": "text", "at": [0, -5], "text": "Профильная линия", "height": 2},
                         {"type": "hatch", "boundary": [[20, 20], [30, 20], [30, 25]], "pattern": "ANSI31"},
                         {"type": "dimension", "kind": "aligned", "p1": [0, 0], "p2": [50, 0], "distance": 5}]}
    drawn = step("cad_draw → DWG", lambda: jobs.draw(spec, name="scheme", to_dwg=True),
                 lambda r: {"dxf_bytes": r["dxf"]["bytes"], "dwg_bytes": r["dwg"]["bytes"],
                            "dwg_sha256": r["dwg"]["sha256"]})
    back = step("cad_convert DWG → DXF", lambda: jobs.convert(drawn["job_id"], "out/scheme.dwg", "dxf",
                                                              name="scheme_roundtrip"),
                lambda r: {"bytes": r["bytes"]})
    assert back["bytes"] > 1000
    pdf_in = root / "synthetic.pdf"
    pdf_in.write_bytes(synthetic_pdf())
    imp = step("cad_pdf_import synthetic", lambda: jobs.pdf_import(pdf=str(pdf_in), pages=[1]),
               lambda r: {"entities": r["pages"][0]["summary"]["entities"],
                          "by_type": r["pages"][0]["summary"]["by_type"], "extents": r["pages"][0]["extents"]})
    assert imp["pages"][0]["summary"]["entities"] >= 3

    # ---------------------------------------------------------------- the pure-Python fallback on the same data
    fb = jobs.job_create("smoke fallback", "ACAD")["job_id"]
    step("fallback points ×2", lambda: [jobs.points_from_table(job_id=fb, rows=grid(depth), point_group=g,
                                                               engine="fallback") for depth, g in ((0.0, "E1"),
                                                                                                   (1.5, "E2"))])
    f1 = jobs.tin_surface(job_id=fb, name="S1", point_group="E1")
    f2 = jobs.tin_surface(job_id=fb, name="S2", point_group="E2")
    fd = step("fallback difference", lambda: jobs.difference_surface(job_id=fb, base="S1", compare="S2", name="TROUGH",
                                                                      contour_interval=0.25),
              lambda r: {"cut_volume": r["stats"]["cut_volume"], "dz_min": r["stats"]["dz_surface"]["z_min"]})
    assert f1["stats"]["triangles"] == f2["stats"]["triangles"] == 162
    assert abs(fd["stats"]["cut_volume"] - d["stats"]["cut_volume"]) / d["stats"]["cut_volume"] < 0.02
    assert abs(fd["stats"]["dz_surface"]["z_min"] - d["stats"]["dz_surface"]["z_min"]) < 1e-6

    receipt_path = os.environ.get("VKM_CAD_LIVE_RECEIPT")
    if receipt_path:
        state = jobs.store.get(jid).state()
        receipt = {
            "receipt": "vkm-cad.v1_smoke/1", "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bridge_version": __version__, "host_role": "WORKSTATION", "data": "synthetic toy data only",
            "products": caps["products"], "channels": {k: {kk: vv for kk, vv in v.items() if kk in (
                "available", "file_version", "compiler", "runtime", "civil_references", "allowed", "scipy")}
                for k, v in caps["channels"].items()},
            "steps": steps,
            "civil_job_runs": [{k: r.get(k) for k in ("run_id", "kind", "status", "duration_s", "exit_code")}
                               for r in state["runs"]],
            "civil_job_outputs": [{k: o[k] for k in ("path", "kind", "bytes", "sha256")} for o in state["outputs"]],
            "side_effects_audit_last_run": state["runs"][-1].get("side_effects_audit"),
        }
        Path(receipt_path).write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
