"""``cad_capabilities``: what Claude can call, through which channel, and what needs the hidden instance.

Dynamic part: products and versions, whether ``accoreconsole``, the C# compiler with the .NET 8 reference set, the
Civil 3D managed API, scipy and the hidden-instance gate are available. Static part: the Civil 3D API reach per
channel and the headless commands verified on the workstation (28.09.2026, AutoCAD 2026 / Civil 3D 2026, ru-RU).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vkm_cad import __version__, dxf, fallback, hidden
from vkm_cad.detect import win_file_version

if TYPE_CHECKING:
    from vkm_cad.cadjobs import CadJobs

VERIFIED_HEADLESS_COMMANDS = [
    "_.LINE", "_.CIRCLE", "_.ERASE", "_.ZOOM", "_.SAVEAS (2018; DWG from DXF/new)", "_.QSAVE", "_.DXFOUT (R12…2018)",
    "_.-PLOT (model: extents/fit; layouts: page setup; DWG To PDF.pc3, canonical media names)",
    "_.-PDFIMPORT (_File, page, insert, scale, rotation → objects in mm)", "_.NETLOAD", "_.QUIT",
    "AutoLISP core (entmake, ssget, getvar/setvar, command, command-s, vl-catch-all-apply, open … \"utf8\")",
]
NOT_HEADLESS = [
    "ActiveX in LISP (vlax-get-acad-object returns nil in Core Console): vla-*/vlax-* object model → csharp or "
    "python_com",
    "Civil 3D UI commands (AECC *.arx UI modules are not loaded in Core Console) → csharp (.NET API) or python_com",
    "dialog-only commands (FILEDIA/CMDDIA are 0; anything that needs a window fails or is killed)",
]
CIVIL_API_REACH = [
    # area, csharp headless (.NET API in accoreconsole /product C3D), python_com (COM AeccXLand in a hidden
    # instance), lisp/scr headless, fallback
    ("COGO points, point groups (queries)", "YES — verified", "YES (AeccXLand Points/PointGroups)", "NO", "DXF points"),
    ("TIN surfaces: points, breaklines, boundaries, build options", "YES — verified", "YES", "NO",
     "Delaunay (no constrained breaklines)"),
    ("contours (extract polylines), surface styles", "YES — verified (ExtractContours)", "YES (styles/labels)", "NO",
     "marching triangles"),
    ("TIN volume surfaces (cut/fill), dz TIN", "YES — verified", "YES", "NO", "dz TIN + exact volumes"),
    ("alignments from polylines, surface profiles, profile views", "YES — verified", "YES", "NO",
     "profile sampling (CSV/DXF)"),
    ("grid surfaces, watersheds, slope analysis", "YES — API present, not exercised", "YES", "NO", "—"),
    ("sample lines, sections, section views", "YES — API present, not exercised", "YES", "NO", "—"),
    ("corridors, assemblies, feature lines, grading", "YES — API present, not exercised",
     "YES (AeccXRoadway)", "NO", "—"),
    ("pipe / pressure networks", "YES — API present (AeccPressurePipesMgd needs a reference), not exercised",
     "YES (AeccXPipe)", "NO", "—"),
    ("parcels, sites, survey database", "YES — API present, not exercised", "YES (AeccXSurvey)", "NO", "—"),
    ("labels, label styles, tables", "YES — API present, not exercised", "YES", "NO", "—"),
    ("LandXML import/export, data shortcuts", "PARTIAL — no public LandXML API; data shortcuts via "
     "AeccDataShortcutMgd (not referenced)", "PARTIAL (commands need the UI)", "NO", "—"),
]


def build(jobs: "CadJobs") -> dict[str, Any]:
    inst = jobs.installation(required=False)
    chain = jobs.toolchain(required=False)
    core = bool(inst and inst.accoreconsole.is_file())
    core_version = win_file_version(inst.accoreconsole) if core and inst else None
    civil_headless = bool(core and inst and "C3D" in inst.products and chain and chain.civil)
    scipy_version = fallback.scipy_version()
    gate = hidden.allowed(jobs.env)
    channels = {
        "core_console": {
            "available": core, "file_version": core_version,
            "products": sorted(inst.products) if inst else [], "locale": inst.locale if inst else None,
            "isolated_profile": True, "stdin": "DEVNULL (a missing script cannot hang the run)",
            "script": "UTF-8 with BOM, CRLF, global command names (_ / _.)",
            "crash_policy": "non-zero exit, a crash-reporter child or a visible window → the job's process tree is "
                            "killed; nothing is promoted; crash reports are never sent"},
        "dotnet": {
            "available": chain is not None, "compiler": chain.compiler_kind if chain else None,
            "runtime": f"Microsoft.NETCore.App {chain.runtime_version}" if chain else None, "target": "net8.0",
            "civil_references": bool(chain and chain.civil), "reasons": jobs.toolchain_reasons(),
            "note": "no SDK install: Roslyn csc compiles against the installed .NET 8 runtime and the AutoCAD / "
                    "Civil 3D managed assemblies"},
        "hidden_instance": {
            "allowed": gate, "gate": f"{hidden.GATE}=1",
            "why_gated": "a full acad.exe writes the user's AutoCAD profile (window placement, profile values, "
                         "CurVer/LastLaunchedProduct, *.aws workspaces) and plug-in autoloaders may change the "
                         "user's CUIX; headless runs do not",
            "refused_when": "any acad.exe is running (a user session is never touched)",
            "attach": "Running Object Table entry of the AutoCAD CLSID whose window belongs to the started PID"},
        "fallback_python": {"available": scipy_version is not None, "scipy": scipy_version,
                            "ezdxf": dxf.ezdxf_version()},
    }
    exec_kinds = {
        "scr": {"channel": "core_console", "available": core, "save": "promoted on END OK"},
        "lisp": {"channel": "core_console", "available": core, "value": "the last expression (vl-prin1-to-string)"},
        "csharp": {"channel": "core_console + .NET (VKMUSER)", "available": core and chain is not None,
                   "context": "ctx.Doc, ctx.Db, ctx.Ed, ctx.Tr (open transaction), civil (CivilDocument, C3D jobs); "
                              "return a value → JSON"},
        "python_com": {"channel": "hidden_instance", "available": gate and bool(inst),
                       "namespace": "app, doc, civil() (AeccXUiLand application), run_dir, out_dir, result, log"},
    }
    c3d = "core_console + .NET host (VkmCadHost)" if civil_headless else "fallback_python (Civil 3D unavailable)"
    operations = [
        {"tool": "cad_draw", "channel": "ezdxf (+ core_console for DWG)"},
        {"tool": "cad_convert", "channel": "core_console (SAVEAS / DXFOUT)"},
        {"tool": "c3d_points_from_table", "channel": c3d, "fallback": "DXF/JSON points"},
        {"tool": "c3d_tin_surface", "channel": c3d, "fallback": "scipy Delaunay"},
        {"tool": "c3d_contours", "channel": c3d, "fallback": "marching triangles"},
        {"tool": "c3d_difference_surface", "channel": c3d, "fallback": "dz TIN + exact volumes"},
        {"tool": "c3d_alignment_profile", "channel": c3d, "fallback": "profile sampling"},
        {"tool": "cad_layout_sheet", "channel": "core_console + .NET host", "fallback": None},
        {"tool": "cad_plot_pdf", "channel": "core_console (-PLOT)", "fallback": None},
        {"tool": "cad_pdf_import", "channel": "core_console (-PDFIMPORT)",
         "fallback": "cad_import_pdf_vector (native vectors of the corpus pipeline → DXF)"},
    ]
    return {"bridge_version": __version__, "host_role": "WORKSTATION",
            "products": {k: v for k, v in (inst.versions.items() if inst else [])},
            "year": inst.year if inst else None, "channels": channels, "exec_kinds": exec_kinds,
            "operations": operations,
            "civil3d_api_reach": [dict(zip(("area", "csharp_headless", "python_com_hidden", "lisp_scr_headless",
                                            "fallback"), row)) for row in CIVIL_API_REACH],
            "headless_commands_verified": VERIFIED_HEADLESS_COMMANDS, "not_headless": NOT_HEADLESS,
            "rules": ["new documents and job directories only; user documents are never opened",
                      "one AutoCAD process at a time (engine lock)", "local only: no network calls by the bridge",
                      "outputs are DERIVED, UNKNOWN_CRS unless an explicit transform is passed, "
                      "AUTO_EXTRACTED_UNREVIEWED"]}
