"""CADFIX live (markers ``services`` and ``desktop``; ``VKM_TEST_CAD_LIVE=1`` on the workstation): ``cad_layout_sheet`` +
``cad_plot_pdf`` in plain-AutoCAD jobs — the shape that crashed the Core Console on 29.09.2026 (root cause in
``test_cad_layout_sheet.py``).

1. A ``cad_draw`` DXF made the job drawing (ACAD job): Cyrillic TrueType text, negative coordinates, layouts without a
   page setup — sheet with a long Cyrillic title block, AutoCAD's default viewport reused, PDF A3 landscape.
2. The host-created viewport path on the same drawing (``reuse_default_viewport = false``): AutoCAD's default viewport
   erased in its own transaction, a new viewport turned on in a separate one.
3. A fresh ``acadiso`` drawing (no DXF involved), which crashed the same way before the fix.

The Civil 3D path (template drawing, ``/product C3D``) is covered by ``test_cad_live.py``. Synthetic data only.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.services, pytest.mark.desktop,
              pytest.mark.skipif(os.environ.get("VKM_TEST_CAD_LIVE") != "1" or sys.platform != "win32",
                                 reason="NOT_RUN: live AutoCAD test, set VKM_TEST_CAD_LIVE=1 on the workstation")]

TITLE = {"title": "Профиль оседаний по профильной линии (синтетика)", "subtitle": "проверка листа ACAD, эпохи 1–3",
         "designation": "VKM-CADFIX-LIVE", "organization": "toy data", "developer": "vkm-cad (проверка)",
         "date": "29.09.2026", "sheet": "1", "sheets": "1", "scale": "усл.", "material": "синтетические данные"}


def _spec() -> dict:
    epochs = [("EPOCH_1", 5, 0.0), ("EPOCH_2", 3, 60.0), ("EPOCH_3", 1, 120.0)]
    entities: list[dict] = [{"type": "polyline", "points": [[0, 0], [300, 0], [300, -230], [0, -230]], "closed": True,
                             "layer": "AXES"},
                            {"type": "text", "at": [150, 12], "text": "Профиль оседаний (синтетика)", "height": 6,
                             "align": "BOTTOM_CENTER", "layer": "TITLE"}]
    for k in range(0, 301, 30):
        entities.append({"type": "line", "start": [k, 0], "end": [k, -230], "layer": "GRID"})
        entities.append({"type": "text", "at": [k, -236], "text": str(46 + k // 30), "height": 3,
                         "align": "TOP_CENTER", "layer": "LABELS"})
    for name, _color, depth in epochs:
        pts = [[x, -20 - depth * (1 - ((x - 150) / 150) ** 2)] for x in range(0, 301, 15)]
        entities.append({"type": "polyline", "points": pts, "layer": name})
        entities += [{"type": "circle", "center": p, "radius": 1.2, "layer": name} for p in pts]
        entities.append({"type": "text", "at": [310, -20 - depth], "text": f"эпоха {name[-1]}", "height": 3.5,
                         "layer": name})
    return {"units": "unitless",
            "layers": [{"name": "AXES", "color": 7}, {"name": "GRID", "color": 8}, {"name": "LABELS", "color": 7},
                       {"name": "TITLE", "color": 7}] + [{"name": n, "color": c} for n, c, _d in epochs],
            "entities": entities}


def _page_mm(pdf: Path) -> tuple[float, float]:
    m = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf.read_bytes())
    assert m, "no MediaBox in the PDF"
    return float(m.group(1)) * 25.4 / 72, float(m.group(2)) * 25.4 / 72


def _pdf_text(pdf: Path) -> str | None:
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    out = subprocess.run([exe, "-enc", "UTF-8", str(pdf), "-"], capture_output=True, timeout=60)
    return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else None


LAYOUT_STATE = """var lay = (Layout)ctx.Tr.GetObject(LayoutManager.Current.GetLayoutId("{name}"), OpenMode.ForRead);
var ps = (BlockTableRecord)ctx.Tr.GetObject(lay.BlockTableRecordId, OpenMode.ForRead);
var vps = new List<object>();
foreach (ObjectId id in ps)
{{
    var v = ctx.Tr.GetObject(id, OpenMode.ForRead) as Viewport;
    if (v != null) vps.Add(new Dictionary<string, object> {{ ["on"] = v.On, ["locked"] = v.Locked, ["scale"] = v.CustomScale,
                                                           ["width"] = v.Width, ["height"] = v.Height }});
}}
return new Dictionary<string, object> {{ ["device"] = lay.PlotConfigurationName, ["media"] = lay.CanonicalMediaName,
                                        ["style"] = lay.CurrentStyleSheet, ["viewports"] = vps }};"""


def _check_sheet(jobs, jid: str, sheet: dict, name: str, pdf_name: str, style: str) -> Path:
    assert sheet["device"] == "DWG To PDF.pc3" and sheet["media"] == "ISO_full_bleed_A3_(420.00_x_297.00_MM)"
    assert sheet["paper_mm"] == [420, 297] and sheet["title_block"].startswith("GOST 2.104")
    state = jobs.exec(jid, "csharp", LAYOUT_STATE.format(name=name), save=False)["value"]
    assert state["device"] == "DWG To PDF.pc3" and state["style"] == style
    content = [v for v in state["viewports"] if v["locked"]]
    assert len(state["viewports"]) == 2 and len(content) == 1             # the overall viewport + the sheet viewport
    assert all(v["on"] for v in state["viewports"])                       # on in the saved drawing
    assert abs(content[0]["scale"] - sheet["paper_mm_per_model_unit"]) < 1e-6 * max(1.0, sheet["paper_mm_per_model_unit"])
    plotted = jobs.plot_pdf(job_id=jid, layouts=[name], out_name=pdf_name)["pdfs"][0]
    pdf = jobs.store.get(jid).dir(plotted["path"])
    assert plotted["bytes"] > 20_000
    width, height = _page_mm(pdf)
    assert abs(width - 420) < 1.0 and abs(height - 297) < 1.0             # A3 landscape
    return pdf


def test_live_sheet_in_plain_autocad_jobs(tmp_path_factory):
    from vkm_cad.cadjobs import MODEL_UNITS_MM, CadJobs, _media
    from vkm_cad.jobs import JobStore

    root = Path(os.environ.get("VKM_CAD_LIVE_ROOT") or tmp_path_factory.mktemp("cadfix_live"))
    env = {**os.environ, "VKM_CAD_JOBS": str(root / "cad_jobs")}
    jobs = CadJobs(JobStore.from_env(env), env=env)

    # 1. cad_draw DXF → DWG → job drawing (ACAD) → sheet with the default viewport of the layout → PDF
    drawn = jobs.draw(_spec(), name="cadfix_profile", as_job_drawing=True)
    jid = drawn["job_id"]
    assert jobs.store.get(jid).state()["product"] == "ACAD"
    sheet = jobs.layout_sheet(job_id=jid, name="SHEET_A", model_units="unitless", title_block=TITLE,
                              style_table="acad.ctb")
    assert sheet["viewport"].startswith("default viewport") and sheet["viewports_erased"] == 0
    assert "title" in sheet["title_block_fitted"]                         # the long title is fitted into its cell
    pdf = _check_sheet(jobs, jid, sheet, "SHEET_A", "cadfix_sheet_a", "acad.ctb")
    text = _pdf_text(pdf)
    if text is not None:
        assert TITLE["title"] in text and "Разраб." in text and TITLE["developer"] in text

    # 2. the host-created viewport: AutoCAD's default viewport erased, a new one turned on in its own transaction
    media, rotation = _media("A3", "landscape")
    args = {"name": "SHEET_B", "media": media, "rotation": rotation, "style_table": "monochrome.ctb",
            "paper_mm_per_model_unit": MODEL_UNITS_MM["unitless"], "scale_denominator": None, "model_window": [],
            "title_block": TITLE, "overwrite": False, "landscape": True, "reuse_default_viewport": False}
    _run_id, _rd, ops = jobs._host(jobs.store.get(jid), [{"op": "acad.layout_sheet", "args": args}])
    created = ops[0]["result"]
    assert created["viewport"] == "created by the host" and created["viewports_erased"] >= 1
    _check_sheet(jobs, jid, created, "SHEET_B", "cadfix_sheet_b", "monochrome.ctb")

    # 3. a fresh acadiso drawing (no DXF): the same order crashed it before the fix
    fresh = jobs.job_create("cadfix fresh acadiso", "ACAD")["job_id"]
    jobs.exec(fresh, "scr", '(command "_.LINE" "0,0" "200,100" "")\n(command "_.CIRCLE" "100,50" "30")\n')
    sheet_c = jobs.layout_sheet(job_id=fresh, name="SHEET_C", scale_denominator=1, model_units="mm",
                                title_block={"title": "Лист на шаблоне acadiso", "sheet": "1", "sheets": "1"})
    assert abs(sheet_c["scale_denominator"] - 1) < 1e-9
    _check_sheet(jobs, fresh, sheet_c, "SHEET_C", "cadfix_sheet_c", "monochrome.ctb")

    for job_id in (jid, fresh):
        runs = jobs.store.get(job_id).state()["runs"]
        assert all(r["status"] == "OK" for r in runs), [(r["run_id"], r["kind"], r["status"]) for r in runs]
