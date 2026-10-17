#!/usr/bin/env python3
"""Read an exported package with GIS providers from any working directory.

Use the installed GIS Python. This checks exported data and relative paths; it
does not approve scientific validity or resolve unknown frames.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"),str(ROOT)]

from vkm_world.geometry_build.pipeline import inventory, write_json


def read_raster(path: Path) -> dict:
    from osgeo import gdal
    gdal.UseExceptions()
    source = gdal.Open(str(path))
    if source is None:
        raise ValueError(f"cannot read {path.name}")
    shape = (source.RasterYSize,source.RasterXSize)
    for i in range(1,source.RasterCount+1):
        values = source.GetRasterBand(i).ReadAsArray()
        if values is None or values.shape != shape:
            raise ValueError(f"incomplete raster data: {path.name}, band {i}")
    result = {"shape":list(shape),"bands":source.RasterCount}
    source = None
    return result


def verify(package: Path) -> dict:
    from osgeo import gdal, ogr
    from qgis.core import QgsProject
    from vkm_world.geometry_build.scene import ensure_qgis_application

    gdal.UseExceptions()
    ogr.UseExceptions()
    ensure_qgis_application()
    package = package.resolve()
    saved = json.loads((package/"output_inventory.json").read_text(encoding="utf-8"))
    actual = inventory(package)
    actual.pop("output_inventory.json",None)
    if actual != saved:
        raise ValueError("package output inventory differs from saved hashes")
    manifest = json.loads((package/"manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "vkm.spatial_package/1" or set(manifest.get("step_keys",{})) != {
            "terrain","mining","geology","regional","paraview"}:
        raise ValueError("incomplete or unsupported spatial package")
    expected_projects = [package/p["path"] for p in manifest["scenes"]["projects"]]
    if not expected_projects or any(not p.is_file() for p in expected_projects):
        raise ValueError("missing declared QGIS scenes")
    projects = []
    for path in sorted(package.rglob("*.qgz")):
        with zipfile.ZipFile(path) as archive:
            xml = ET.fromstring(archive.read(next(n for n in archive.namelist() if n.endswith(".qgs"))))
        sources = [e.text for e in xml.iter("datasource") if e.text]
        for source in sources:
            if re.match(r"^([A-Za-z]:|/|\\\\)",source):
                raise ValueError(f"absolute datasource in {path.name}")
            target = (path.parent/source.split("|")[0]).resolve()
            if not target.is_relative_to(package) or not target.is_file():
                raise ValueError(f"missing/escaping datasource in {path.name}: {source}")
        project = QgsProject()
        if not project.read(str(path)):
            raise ValueError(f"cannot open {path.name}")
        invalid = [l.name() for l in project.mapLayers().values() if not l.isValid()]
        if invalid:
            raise ValueError(f"invalid project layers: {invalid}")
        projects.append({"path":path.relative_to(package).as_posix(),
                         "layers":len(project.mapLayers()),"sources":len(sources)})
        project.clear()
    vectors = []
    for path in sorted(package.rglob("*.gpkg")):
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro",uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise ValueError(f"SQLite integrity: {path.name}: {integrity}")
        connection.close()
        source = ogr.Open(str(path),False)
        if source is None:
            raise ValueError(f"cannot open {path.name}")
        counts = {}
        for layer in source:
            count = 0
            for feature in layer:
                # Force attribute and geometry decoding, beyond cached feature counts.
                feature.items()
                geom = feature.GetGeometryRef()
                if geom is not None:
                    geom.ExportToIsoWkb()
                count += 1
            if count != layer.GetFeatureCount():
                raise ValueError(f"feature balance: {path.name}:{layer.GetName()}")
            counts[layer.GetName()] = count
        vectors.append({"path":path.relative_to(package).as_posix(),"integrity":"ok","layers":counts})
        source = None
    rasters = []
    for path in sorted(package.rglob("*.tif")):
        rasters.append({"path":path.relative_to(package).as_posix(),**read_raster(path)})
    after=inventory(package); after.pop("output_inventory.json",None)
    if after!=saved:
        raise ValueError("independent reading mutated package bytes")
    return {"status":"PASS", "inventory_files":len(saved),"projects":projects,
            "vectors":vectors,"rasters":rasters,"scientific_admission":"NOT_CHECKED"}


def qgz_scene_digest(path: Path) -> str:
    """Compare rendered layer settings, retaining colours and subset expressions.

    QGIS generates symbol-layer, category and range UUIDs on each write. Those
    UUIDs do not affect rendering; their values, bounds and styles are retained.
    """
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read(next(n for n in archive.namelist() if n.endswith(".qgs"))))

    def tree(element):
        if element is None:
            return None
        attrs = {k:v for k,v in element.attrib.items()
                 if not (k in {"id","uuid"} and re.fullmatch(r"\{[0-9a-fA-F-]{36}\}",v))}
        return [element.tag, sorted(attrs.items()), (element.text or "").strip(),
                [tree(child) for child in element]]

    layers = []
    for layer in root.findall("./projectlayers/maplayer"):
        layers.append([layer.findtext("layername"), layer.findtext("datasource"),
                       layer.findtext("provider"), layer.findtext("subsetstring"),
                       tree(layer.find("renderer-v2")), tree(layer.find("pipe"))])
    value = [root.findtext("title"), sorted(layers,key=lambda row:json.dumps(row,sort_keys=True))]
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def compare(left: Path,right: Path) -> dict:
    """Compare all bytes; separately compare primary GIS data and scoped scene settings."""
    from scripts.build_dma_surface_package import content_digest
    a,b = inventory(left),inventory(right)
    differences = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    data_paths = sorted(k for k in a if Path(k).suffix.lower() in
                        {".gpkg",".tif",".png",".qgz",".csv",".jsonl",".vtp",".vtm"})
    def digest(path):
        return qgz_scene_digest(path) if path.suffix.lower()==".qgz" else content_digest(path)
    semantic_differences = [k for k in data_paths if k not in b or
                            digest(left/k) != digest(right/k)]
    extra_data = [k for k in b.keys()-a.keys() if Path(k).suffix.lower() in
                 {".gpkg",".tif",".png",".qgz",".csv",".jsonl",".vtp",".vtm"}]
    return {"bytes_equal":not differences,"byte_differences":differences,
            "primary_data_semantics_equal":not semantic_differences and not extra_data,
            "primary_data_semantic_differences":semantic_differences+extra_data,
            "primary_data_files":len(data_paths),
            "comparison_scope":"GPKG attributes/geometries; raster pixels/grid/CRS/NoData; PNG pixels; "
                "QGZ title/layer names/datasources/provider/subsets/vector renderer and raster pipe settings "
                "excluding generated symbol-layer/category/range UUIDs; CSV/JSONL/VTK exact bytes. "
                "QGZ camera and layer-tree UI state are excluded; portability is checked separately. "
                "Receipt hashes, state containers and operational logs remain in the complete byte difference list."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package",required=True,type=Path)
    parser.add_argument("--against",type=Path)
    parser.add_argument("--report",required=True,type=Path)
    args = parser.parse_args()
    package,report = args.package.resolve(),args.report.resolve()
    if report.is_relative_to(package):
        raise ValueError("verification report must be outside the immutable package")
    result = verify(package)
    if args.against:
        result["comparison"] = compare(package,args.against.resolve())
        if not result["comparison"]["primary_data_semantics_equal"]:
            result["status"] = "FAIL"
    write_json(report,result)
    print(json.dumps({"status":result["status"],"projects":len(result["projects"]),
                      "gpkg":len(result["vectors"]),"rasters":len(result["rasters"])}),flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    status = 1
    try:
        status = main()
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(status)
