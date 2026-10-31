"""Executed only by the configured pvpython, with a generated JSON payload.

Readers and filters are fixed. No user Python, state loading, plugins, external
references, coordinate transformations, or arbitrary expressions are supported.
"""
from pathlib import Path
import json
import math
import sys

from paraview import simple as pv


def information(proxy):
    proxy.UpdatePipeline()
    info = proxy.GetDataInformation()
    bounds = list(info.GetBounds())
    if not all(math.isfinite(v) for v in bounds):
        raise ValueError("nonfinite bounds")
    return {"points": info.GetNumberOfPoints(), "cells": info.GetNumberOfCells(), "bounds": bounds,
            "point_arrays": list(proxy.PointData.keys()), "cell_arrays": list(proxy.CellData.keys())}


def main():
    path = Path(sys.argv[1]).resolve()
    args = json.loads(path.read_text(encoding="utf-8"))
    out = path.parent.parent / "out"
    report = {"native_program": "ParaView", "version": str(pv.GetParaViewVersion()),
              "operation": args["operation"], "coordinate_transform": "NONE", "display_scale": [1, 1, 1],
              "frame": args["frame"], "unit": args["unit"], "scientific_admission": "NOT_GRANTED"}
    if args["operation"] != "probe":
        source = path.parent / args["input"]
        readers = {".vtu": pv.XMLUnstructuredGridReader, ".vtp": pv.XMLPolyDataReader,
                   ".vtk": pv.LegacyVTKReader, ".stl": pv.STLReader, ".ply": pv.PLYReader}
        data = readers[source.suffix]()
        if "FileNames" in data.ListProperties():
            data.FileNames = [str(source)]
        else:
            data.FileName = [str(source)]
        report["input"] = information(data)
        if not report["input"]["points"]:
            raise ValueError("empty native dataset")
        sizes = pv.CellSize(Input=data)
        sizes.ComputeVertexCount = 0
        sizes.ComputeLength = 0
        sizes.ComputeArea = 0
        sizes.ComputeVolume = 1
        sizes.UpdatePipeline()
        vol = sizes.GetDataInformation().GetCellDataInformation().GetArrayInformation("Volume")
        if vol:
            report["native_cell_signed_volume_range"] = list(vol.GetComponentRange(0))
            report["volume_note"] = "Signed VTK representation diagnostic; not geological/FEM validity."
        shown = data
        if not report["input"]["cells"] and args["operation"] == "render":
            # Native vertex cells retain point positions; no glyph radius or
            # geometric scaling is introduced for point-only datasets.
            shown = pv.ConvertToPointCloud(Input=data)
            if not information(shown)["cells"]:
                raise ValueError("native point-cloud conversion produced no display vertex cells")
            report["display_point_vertex_cells"] = True
        if args["operation"] == "slice":
            shown = pv.Slice(Input=data)
            shown.SliceType = "Plane"
            shown.SliceType.Origin = args["origin"]
            shown.SliceType.Normal = args["normal"]
            report["slice"] = information(shown)
            report["slice_plane"] = {"origin": args["origin"], "normal": args["normal"]}
            if not report["slice"]["cells"]:
                raise ValueError("slice does not intersect any cells")
            pv.SaveData(str(out / "slice.vtp"), proxy=shown)
        if args["operation"] in {"render", "slice"}:
            view = pv.CreateView("RenderView")
            view.ViewSize = [args["width"], args["height"]]
            view.UseColorPaletteForBackground = 0
            view.Background = [0.12, 0.15, 0.18]
            view.CameraParallelProjection = 1
            display = pv.Show(shown, view)
            display.Representation = "Surface With Edges"
            display.Scale = [1, 1, 1]
            display.DiffuseColor = [0.75, 0.82, 0.87]
            # Explicit association also works on datasets with no scalar arrays
            # (6.2 RC2 ColorBy(None) can otherwise resolve association 'NONE').
            display.ColorArrayName = ["POINTS", ""]
            note = pv.Text()
            note.Text = "DERIVED / NOT FIELD ADMITTED\n" + args["frame"] + " | unit=" + args["unit"]
            pv.Show(note, view)
            pv.ResetCamera(view)
            pv.Render(view)
            pv.SaveScreenshot(str(out / "view.png"), view, ImageResolution=[args["width"], args["height"]])
            pv.SaveState(str(out / "view.pvsm"))
    (out / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
