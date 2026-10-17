"""Portable ParaView loader and exporter; copied beside the private/local scene, no GIS dependency.

Run ``pvpython --no-mpi --force-offscreen-rendering open_scene.py --build`` to recreate VTK exports, state and CPU
previews from data/manifest.json and terrain_arrays.npz. In ParaView Python use ``exec(compile(open(path).read(),
path, 'exec'), {'__file__': path})`` to load the saved state with data-directory remapping.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np
import vtk
from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy


def strings(name, values):
    array = vtk.vtkStringArray()
    array.SetName(name)
    for value in values:
        array.InsertNextValue(str(value))
    return array


def numbers(name, values):
    array = numpy_to_vtk(np.ascontiguousarray(values), deep=True)
    array.SetName(name)
    return array


def cells(connectivity):
    connectivity = np.asarray(connectivity, dtype=np.int64)
    array = vtk.vtkCellArray()
    array.SetData(numpy_to_vtkIdTypeArray(np.arange(0, connectivity.size+1, connectivity.shape[1], dtype=np.int64), deep=True),
                  numpy_to_vtkIdTypeArray(connectivity.ravel(), deep=True))
    return array


def write_poly(poly, path):
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(path))
    writer.SetInputData(poly)
    writer.SetDataModeToAppended()
    if writer.Write() != 1:
        raise RuntimeError("VTK writer failed: " + path.name)
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    reread = reader.GetOutput()
    assert reread.GetNumberOfPoints() == poly.GetNumberOfPoints()
    assert reread.GetNumberOfCells() == poly.GetNumberOfCells()
    assert np.array_equal(vtk_to_numpy(reread.GetPoints().GetData()), vtk_to_numpy(poly.GetPoints().GetData()))
    for original, attributes, count in ((poly.GetPointData(), reread.GetPointData(), poly.GetNumberOfPoints()),
                                        (poly.GetCellData(), reread.GetCellData(), poly.GetNumberOfCells())):
        identity = attributes.GetAbstractArray("stable_id")
        if identity is not None:
            assert identity.GetNumberOfValues() == count
            source_id = original.GetAbstractArray("stable_id")
            assert all(identity.GetValue(i) == source_id.GetValue(i) for i in range(count))
            assert len({identity.GetValue(i) for i in range(count)}) == count, "Duplicate stable IDs in " + path.name
    return {"file": path.name, "points": reread.GetNumberOfPoints(), "cells": reread.GetNumberOfCells(),
            "bounds": list(reread.GetBounds()), "read_back": "PASS"}


def terrain(base, manifest):
    source = np.load(base / "data/terrain_arrays.npz")
    z, valid, gt = source["z"], source["valid"], source["gt"]
    rows, cols = np.nonzero(valid)
    xyz = np.column_stack((gt[0]+(cols+.5)*gt[1], gt[3]+(rows+.5)*gt[5], z[valid]))
    poly = vtk.vtkPolyData()
    points = vtk.vtkPoints()
    points.SetData(numpy_to_vtk(xyz, deep=True))
    poly.SetPoints(points)
    index = np.full(z.shape, -1, dtype="int64")
    index[valid] = np.arange(len(rows))
    r, c = np.nonzero(valid[:-1, :-1] & valid[1:, :-1] & valid[:-1, 1:] & valid[1:, 1:])
    tri1 = np.column_stack((index[r, c], index[r+1, c], index[r, c+1]))
    tri2 = np.column_stack((index[r, c+1], index[r+1, c], index[r+1, c+1]))
    triangles = np.concatenate((tri1, tri2))
    poly.SetPolys(cells(triangles))
    normal_z = np.cross(xyz[triangles[:, 1]]-xyz[triangles[:, 0]], xyz[triangles[:, 2]]-xyz[triangles[:, 0]])[:, 2]
    assert np.all(normal_z > 0), "Terrain triangles must have upward orientation"
    poly.GetPointData().AddArray(numbers("elevation_EGM2008_m", z[valid]))
    poly.GetPointData().AddArray(numbers("source_row", rows.astype("int32")))
    poly.GetPointData().AddArray(numbers("source_col", cols.astype("int32")))
    poly.GetPointData().AddArray(strings("stable_id", (f"{manifest['grid_id']}:r{r}:c{c}" for r, c in zip(rows, cols))))
    for quality in manifest["quality"]:
        code = quality["code"]
        values = source[code][valid].copy()
        if code == "HEM":
            values = values.astype("float64")
            if quality["nodata"] is not None:
                values[values == quality["nodata"]] = np.nan
        poly.GetPointData().AddArray(numbers(code, values))
    poly.GetCellData().AddArray(strings("stable_id", (f"{manifest['grid_id']}:r{rr}:c{cc}:t{t}"
        for t in (0, 1) for rr, cc in zip(r, c))))
    poly.GetFieldData().AddArray(strings("crs_wkt", [manifest["crs_wkt"]]))
    poly.GetFieldData().AddArray(strings("vertical_datum", [manifest["vertical_datum"]]))
    poly.GetFieldData().AddArray(strings("epistemic_status", ["DERIVATION: source raster triangulation; no gain in accuracy"]))
    cross_term = np.abs(z[r, c].astype("float64") + z[r+1, c+1] - z[r, c+1] - z[r+1, c]) / 4
    poly.GetFieldData().AddArray(numbers("mesh_bilinear_difference_bound_m_max_p95",
                                        np.array([cross_term.max(), np.percentile(cross_term, 95)])))
    return poly


def layer_poly(layer):
    xyz, line_cells, point_cells, records = [], [], [], []
    # VTK stores vertex cells before line cells; metadata is ordered identically below.
    point_records, line_records = [], []
    for feature in layer["features"]:
        start = len(xyz)
        xyz.extend(feature["xyz"])
        indices = list(range(start, len(xyz)))
        if feature["kind"] == "point":
            point_cells.append(indices)
            point_records.append(feature)
        elif len(indices) >= 2:
            line_cells.append(indices)
            line_records.append(feature)
    records = point_records + line_records
    poly = vtk.vtkPolyData()
    points = vtk.vtkPoints()
    points.SetData(numpy_to_vtk(np.asarray(xyz, dtype="float64"), deep=True))
    poly.SetPoints(points)
    for target, records_indices in ((poly.SetVerts, point_cells), (poly.SetLines, line_cells)):
        array = vtk.vtkCellArray()
        for indices in records_indices:
            array.InsertNextCell(len(indices))
            for value in indices:
                array.InsertCellPoint(value)
        target(array)
    for name, values in (("stable_id", [r["stable_id"] for r in records]),
                         ("source_object_id", [r["source_object_id"] for r in records]),
                         ("source_fid", [str(r["source_fid"]) for r in records]),
                         ("geometry_role", [layer["geometry_role"]]*len(records)),
                         ("source_layer", [layer["name"]]*len(records))):
        poly.GetCellData().AddArray(strings(name, values))
    for name in ("mine", "seam", "state_date", "time_status", "semantic_role", "interpretation_id", "source_path",
                 "source_sha256", "epistemic_status", "verification", "frame_id", "frame_kind",
                 "source_x_unit", "source_y_unit", "source_y_axis"):
        poly.GetCellData().AddArray(strings(name, (r["properties"].get(name) or "UNKNOWN" for r in records)))
    if layer["geometry_role"] == "DISPLAY_DRAPE_PHYSICAL_Z_UNKNOWN":
        poly.GetCellData().AddArray(strings("physical_z", ["UNKNOWN"]*len(records)))
    poly.GetFieldData().AddArray(strings("frame", [layer["frame"]]))
    poly.GetFieldData().AddArray(strings("axis_mapping", [layer["axis_mapping"]]))
    poly.GetFieldData().AddArray(strings("vertical_relation_to_scene", [layer["vertical_relation_to_scene"]]))
    return poly


def multiblock(base, name, blocks):
    dataset = vtk.vtkMultiBlockDataSet()
    for i, (label, poly) in enumerate(blocks):
        dataset.SetBlock(i, poly)
        dataset.GetMetaData(i).Set(vtk.vtkCompositeDataSet.NAME(), label)
    writer = vtk.vtkXMLMultiBlockDataWriter()
    writer.SetFileName(str(base / "data" / name))
    writer.SetInputData(dataset)
    assert writer.Write() == 1


def portable_state(path, base):
    tree = ET.parse(path)
    prefix = str(base.resolve()).replace("\\", "/") + "/"
    for element in tree.iter():
        for key, value in list(element.attrib.items()):
            normalized = value.replace("\\", "/")
            if normalized.startswith(prefix):
                element.set(key, "./" + normalized[len(prefix):])
    tree.write(path, encoding="utf-8", xml_declaration=True)


def identify_derived_slice(path, manifest, origin, normal):
    """A cut creates new vertices/cells; inherited source IDs are lineage, never derivative identity."""
    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    poly = vtk.vtkPolyData()
    poly.DeepCopy(reader.GetOutput())
    parent = poly.GetCellData().GetAbstractArray("stable_id")
    assert parent is not None, "Slice lost its source triangle lineage"
    poly.GetCellData().AddArray(strings("parent_surface_cell_id", (parent.GetValue(i) for i in range(poly.GetNumberOfCells()))))
    poly.GetCellData().RemoveArray("stable_id")
    poly.GetPointData().RemoveArray("stable_id")
    for name in ("source_row", "source_col", "FLM", "WBM", "EDM"):
        poly.GetPointData().RemoveArray(name)
    hem = poly.GetPointData().GetArray("HEM")
    if hem is not None:
        hem.SetName("interpolated_source_HEM_sigma_m_not_total_uncertainty")
    prefix = manifest["grid_id"] + ":vertical_slice_ew_centre"
    poly.GetPointData().AddArray(strings("stable_id", (prefix+":point:"+str(i) for i in range(poly.GetNumberOfPoints()))))
    poly.GetCellData().AddArray(strings("stable_id", (prefix+":cell:"+str(i) for i in range(poly.GetNumberOfCells()))))
    poly.GetFieldData().AddArray(strings("epistemic_status", ["DERIVATION: intersection with piecewise planar DSM mesh; no underground geology inferred"]))
    poly.GetFieldData().AddArray(strings("parent_grid_id", [manifest["grid_id"]]))
    poly.GetFieldData().AddArray(strings("quality_scope", ["Categorical masks omitted: interpolation across categories is undefined; HEM interpolated diagnostic only"]))
    poly.GetFieldData().AddArray(numbers("slice_plane_origin", np.asarray(origin, dtype="float64")))
    poly.GetFieldData().AddArray(numbers("slice_plane_normal", np.asarray(normal, dtype="float64")))
    return write_poly(poly, path)


def scenes(base, manifest, output_files):
    import paraview.simple as pvs
    view = pvs.CreateView("RenderView")
    view.ViewSize = [1440, 1000]
    view.UseColorPaletteForBackground = 0
    view.Background = [1, 1, 1]
    view.EnableRayTracing = 1
    view.BackEnd = "OSPRay raycaster"
    dsm = pvs.XMLPolyDataReader(registrationName="DSM: EGM2008 metres; true scale x1", FileName=[str(base / "data/terrain.vtp")])
    dsm.UpdatePipeline()
    display = pvs.Show(dsm, view)
    pvs.ColorBy(display, ("POINTS", "elevation_EGM2008_m"))
    display.RescaleTransferFunctionToDataRange(True, False)
    display.SetScalarBarVisibility(view, True)
    display.Scale = [1, 1, 1]
    legend = pvs.GetScalarBar(pvs.GetColorTransferFunction("elevation_EGM2008_m"), view)
    legend.Title = "DSM elevation [m, EGM2008]"
    legend.TitleColor = [0, 0, 0]
    legend.LabelColor = [0, 0, 0]
    for layer, file in output_files:
        if layer["geometry_role"] != "DISPLAY_DRAPE_PHYSICAL_Z_UNKNOWN":
            continue
        source = pvs.XMLPolyDataReader(registrationName="DISPLAY DRAPE: " + layer["name"], FileName=[str(base / "data" / file)])
        rep = pvs.Show(source, view)
        rep.ColorArrayName = ["POINTS", ""]
        rep.DiffuseColor = [.05, .05, .05]
        rep.LineWidth = 1.1
        rep.PointSize = 3
    text = pvs.Text(registrationName="Scientific scope")
    text.Text = "DSM / EGM2008 m / scale x1 | Plans are display drapes; underground Z UNKNOWN"
    rep = pvs.Show(text, view)
    rep.Color = [0, 0, 0]
    rep.FontSize = 12
    view.ResetCamera()
    bounds = dsm.GetDataInformation().GetBounds()
    cx, cy = (bounds[0]+bounds[1])/2, (bounds[2]+bounds[3])/2
    size = max(bounds[1]-bounds[0], bounds[3]-bounds[2])
    cz = (bounds[4]+bounds[5])/2
    view.CameraFocalPoint = [cx, cy, cz]
    view.CameraPosition = [cx+size*.4, cy-size*.8, cz+size*.75]
    view.CameraViewUp = [0, 0, 1]
    view.CameraParallelProjection = 1
    view.CameraParallelScale = size*.72
    pvs.SaveScreenshot(str(base / "terrain_true_scale.png"), view)
    # A physical vertical section of the DSM, with actual unscaled coordinates, remains independently readable.
    cut = pvs.Slice(registrationName="DSM vertical slice; no underground geology inferred", Input=dsm)
    cut.SliceType = "Plane"
    cut.SliceType.Origin = [cx, cy, cz]
    cut.SliceType.Normal = [0, 1, 0]
    cut.UpdatePipeline()
    pvs.SaveData(str(base / "data/terrain_vertical_slice.vtp"), proxy=cut)
    cells_count = cut.GetDataInformation().GetNumberOfCells()
    assert cells_count > 0
    slice_check = identify_derived_slice(base / "data/terrain_vertical_slice.vtp", manifest, [cx, cy, cz], [0, 1, 0])
    # The saved scene reads the derivative with its new IDs; the temporary cutter inherited source IDs.
    pvs.Delete(cut)
    derivative = pvs.XMLPolyDataReader(registrationName="DSM vertical slice: derivative IDs + parent triangle lineage",
                                     FileName=[str(base / "data/terrain_vertical_slice.vtp")])
    derivative.UpdatePipeline()
    pvs.Hide(derivative, view)
    pvs.SaveState(str(base / "scene.pvsm"))
    portable_state(base / "scene.pvsm", base)
    # Source section frames are shown in separate views, never shifted under the global terrain.
    local_count = 0
    for layer, file in output_files:
        if layer["geometry_role"] != "SOURCE_SECTION_SEPARATE_FRAME":
            continue
        local_view = pvs.CreateView("RenderView")
        local_view.ViewSize = [1200, 750]
        local_view.EnableRayTracing = 1
        local_view.BackEnd = "OSPRay raycaster"
        section = pvs.XMLPolyDataReader(registrationName="SEPARATE FRAME: " + layer["name"], FileName=[str(base / "data" / file)])
        section_rep = pvs.Show(section, local_view)
        section_rep.ColorArrayName = ["POINTS", ""]
        section_rep.DiffuseColor = [0.8, 0.15, 0.05]
        section_rep.LineWidth = 2
        local_view.ResetCamera()
        section.UpdatePipeline()
        bounds = section.GetDataInformation().GetBounds()
        centre_x = (bounds[0]+bounds[1])/2
        centre_z = (bounds[4]+bounds[5])/2
        size = max(bounds[1]-bounds[0], bounds[5]-bounds[4], 1.)
        local_view.CameraFocalPoint = [centre_x, 0, centre_z]
        local_view.CameraPosition = [centre_x, -2*size, centre_z]
        local_view.CameraViewUp = [0, 0, 1]
        local_view.CameraParallelProjection = 1
        local_view.CameraParallelScale = size*.6
        note = pvs.Text(registrationName="Source frame: " + layer["name"])
        note.Text = "SEPARATE SOURCE FRAME / map trace and vertical tie UNKNOWN / scale x1"
        pvs.Show(note, local_view)
        pvs.SaveScreenshot(str(base / ("section_frame_"+str(local_count)+".png")), local_view)
        local_count += 1
    if local_count:
        pvs.SaveState(str(base / "scene_with_separate_sections.pvsm"))
        portable_state(base / "scene_with_separate_sections.pvsm", base)
    return {"render_backend": "CPU OSPRay raycaster", "vertical_scale": 1,
            "dsm_slice_cells": cells_count, "dsm_slice_read_back": slice_check, "separate_section_views": local_count}


def main():
    base = Path(__file__).resolve().parent
    if "--build" not in sys.argv:
        from paraview.simple import LoadState, GetSources
        state = base / ("scene_with_separate_sections.pvsm" if (base / "scene_with_separate_sections.pvsm").exists() else "scene.pvsm")
        LoadState(str(state), data_directory=str(base / "data"), restrict_to_data_directory=True)
        if "--verify-state" in sys.argv:
            readers = []
            for source in GetSources().values():
                if "FileName" not in source.ListProperties():
                    continue
                for filename in source.FileName:
                    assert Path(filename).resolve().is_relative_to(base / "data"), filename
                source.UpdatePipeline()
                count = source.GetDataInformation().GetNumberOfPoints()
                assert count > 0
                readers.append({"files": [Path(p).name for p in source.FileName], "points": count})
            assert readers
            print(json.dumps({"relocated_state": "PASS", "readers": readers}), flush=True)
        return
    manifest = json.loads((base / "data/manifest.json").read_text(encoding="utf-8"))
    poly = terrain(base, manifest)
    interpolation_bound = vtk_to_numpy(poly.GetFieldData().GetArray("mesh_bilinear_difference_bound_m_max_p95")).tolist()
    checks = [write_poly(poly, base / "data/terrain.vtp")]
    global_blocks = [("DSM_EGM2008_TRUE_SCALE", poly)]
    outputs = []
    for i, layer in enumerate(manifest["layers"]):
        poly = layer_poly(layer)
        filename = f"layer_{i:03d}.vtp"
        checks.append(write_poly(poly, base / "data" / filename))
        outputs.append((layer, filename))
        if layer["geometry_role"] == "DISPLAY_DRAPE_PHYSICAL_Z_UNKNOWN":
            global_blocks.append(("DISPLAY_DRAPE_"+layer["name"], poly))
        else:
            multiblock(base, f"section_frame_{i:03d}.vtm", [(layer["name"], poly)])
    multiblock(base, "scene_global.vtm", global_blocks)
    render = scenes(base, manifest, outputs)
    checks.append(render["dsm_slice_read_back"])
    result = {"status": "PASS", "vtk_version": vtk.vtkVersion.GetVTKVersion(), "python": sys.version.split()[0],
              "independent_reader": "vtkXMLPolyDataReader", "files": checks, "render": render,
              "mesh_vs_bilinear_bound_m": {"max": interpolation_bound[0], "p95": interpolation_bound[1],
                                            "scope": "Representation difference, not terrain accuracy or uncertainty"},
              "unknowns": ["Baltic vertical relation", "mining physical Z", "source section map trace"],
              "omitted_display_objects": len(manifest["omitted_display_objects"])}
    (base / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=1)+"\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "files": len(checks), "render": render}), flush=True)


if __name__ == "__main__":
    main()
