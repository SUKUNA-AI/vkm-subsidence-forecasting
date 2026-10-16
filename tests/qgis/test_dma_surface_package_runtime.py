"""DMA project portability on tiny synthetic files; requires an existing QGIS Python."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

if __name__ != "__main__":
    import pytest
    pytestmark = pytest.mark.qgis_runtime


def portability_case(tmp_path):
    import numpy as np
    from osgeo import gdal, ogr, osr
    from qgis.core import QgsApplication

    source = Path(__file__).resolve().parents[2] / "scripts/build_dma_surface_package.py"
    spec = importlib.util.spec_from_file_location("dma_surface_builder", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    os.chdir(tmp_path)
    package = Path("relative/package")
    data = package / "data"
    data.mkdir(parents=True)
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(32640)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(data / "dma_linked.gpkg"))
    for name in ("overlaps", "coord_groups", "qa_issues", "links", "value_records", "csv_rows",
                 "source_features", "controls_osm_shafts", "contour_variants", "contours", "aoi"):
        layer = ds.CreateLayer(name, sr, ogr.wkbPolygon)
        for field in (*module.THEMES, "variant_no", "n_value_records", "has_attributes"):
            layer.CreateField(ogr.FieldDefn(field, ogr.OFTReal))
    ds = None
    for name in ("dem_glo30_aoi_utm40n_30m.tif", "hillshade_utm40n_30m.tif",
                 "quality_hem_utm40n_30m.tif", "quality_flm_utm40n_30m.tif"):
        ds = gdal.GetDriverByName("GTiff").Create(str(data / name), 2, 2, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((500000, 30, 0, 6600000, 0, -30))
        ds.SetProjection(sr.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(np.array([[100, 101], [102, 103]], dtype="float32"))
        ds = None
    module.qgis_project(package, {"geoms": [{"values": ["V0001"]}]})
    assert module._QGIS_APP is QgsApplication.instance()
    assert module.ensure_qgis_application() is module._QGIS_APP
    relocated = tmp_path / "different_parent" / "moved_package"
    relocated.parent.mkdir()
    assert package.resolve().is_relative_to(tmp_path.resolve())
    assert relocated.resolve().is_relative_to(tmp_path.resolve())
    shutil.move(str(package), str(relocated))
    assert not package.exists()
    result = module.verify_project_fresh(relocated)
    assert result["status"] == "PASS", result
    assert not result["missing_datasources"] and not result["escaping_datasources"]
    assert all(not p or p.startswith("./data/") for p in result["relative_examples"])

    # A relative source that only exists under the original cwd must fail, even if QGIS can read the project XML.
    qgz = relocated / "dma_skru1_surface.qgz"
    with zipfile.ZipFile(qgz) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    name = next(name for name in entries if name.endswith(".qgs"))
    xml=ET.fromstring(entries[name])
    gpkg_sources=[e.text for e in xml.iter("datasource") if e.text and ".gpkg" in e.text]
    assert gpkg_sources and all("option:IMMUTABLE=YES" in source for source in gpkg_sources)
    for layer in xml.findall("./projectlayers/maplayer"):
        if (layer.findtext("datasource") or "").split("|")[1:2]==["layername=coord_groups"]:
            colours=[e.get("value") for e in layer.find("renderer-v2").iter("Option") if e.get("name")=="color"]
            assert colours and all(value.startswith("119,119,119,255") for value in colours)
    assert b"./data/" in entries[name]
    entries[name] = entries[name].replace(b"./data/", b"hidden_cwd/data/")
    with zipfile.ZipFile(qgz, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    result = module.verify_project_fresh(relocated)
    assert result["status"] == "FAIL", result
    assert result["missing_datasources"]
    # A readable TIFF header is insufficient: force actual block decoding and reject corruption.
    verifier_source = source.with_name("verify_spatial_model.py")
    verifier_spec = importlib.util.spec_from_file_location("spatial_verifier",verifier_source)
    verifier = importlib.util.module_from_spec(verifier_spec)
    verifier_spec.loader.exec_module(verifier)
    from qgis.core import QgsProject
    from vkm_world.geometry_build.scene import _add_layers
    import hashlib
    gpkg=relocated/"data/dma_linked.gpkg"
    before=hashlib.sha256(gpkg.read_bytes()).hexdigest()
    project=QgsProject()
    layers=_add_layers(project,gpkg,project.layerTreeRoot(),lambda name:True)
    assert layers and all(layer.readOnly() for layer in layers)
    assert all("option:IMMUTABLE=YES" in layer.source() for layer in layers)
    project.clear(); layers.clear()
    assert hashlib.sha256(gpkg.read_bytes()).hexdigest()==before
    corrupt = tmp_path/"truncated.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(corrupt),256,256,1,gdal.GDT_Float32,options=["TILED=YES"])
    ds.GetRasterBand(1).WriteArray(np.ones((256,256),dtype="float32"))
    ds = None
    with corrupt.open("r+b") as stream:
        stream.truncate(2048)
    try:
        verifier.read_raster(corrupt)
    except (RuntimeError,ValueError):
        pass
    else:
        raise AssertionError("truncated TIFF pixel payload was accepted")
    return {"relocated_project": "PASS", "broken_relative_source_rejected": "PASS"}


def test_relative_output_survives_relocation_and_unrelated_cwd(tmp_path):
    from vkm_qgis.service import runtime_path
    runtime = runtime_path()
    if runtime is None:
        pytest.skip("Configure an existing VKM_QGIS_ROOT or VKM_QGIS_PYTHON")
    script = Path(__file__).resolve()
    args = [str(runtime), str(script), str(tmp_path)]
    command = args
    if os.name == "nt" and runtime.suffix.lower() in {".bat", ".cmd"}:
        command = f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /s /c ""{runtime}" "{script}" "{tmp_path}""'
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=90, env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "relocated_project": "PASS", "broken_relative_source_rejected": "PASS"}


if __name__ == "__main__":
    # PyQGIS has no pytest dependency; only this small worker executes inside its installed Python.
    result = portability_case(Path(sys.argv[1]).resolve())
    print(json.dumps(result), flush=True)
    os._exit(0)
