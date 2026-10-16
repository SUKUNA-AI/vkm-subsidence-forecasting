"""Portable GIS scenes and regional DSM products using the installed GIS runtime."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np

from .pipeline import write_json

_QGIS_APPLICATION = None  # Keep the native QApplication alive across all scene builds.


def ensure_qgis_application():
    global _QGIS_APPLICATION
    from qgis.core import QgsApplication
    _QGIS_APPLICATION = QgsApplication.instance()
    if _QGIS_APPLICATION is None:
        _QGIS_APPLICATION = QgsApplication([], False)
        _QGIS_APPLICATION.initQgis()
    return _QGIS_APPLICATION


def build_regional_surface(resources_root: Path, mining: Path, terrain: Path, out: Path,
                           *, pixel_m: float = 30., buffer_m: float = 2000.) -> dict:
    """Continuous raster over supported plan coverage, independent of DMA polygon edges.

    The extent is a MODEL_CHOICE. No surface values are inferred from contours,
    well depths or mining indicators; masks retain their categorical semantics.
    """
    from osgeo import gdal, osr, ogr
    from scripts.build_dma_surface_package import DEMD, TILE, AUX

    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    bounds = []
    ds = gdal.OpenEx(str(mining / "mining.gpkg"), gdal.OF_VECTOR)
    for layer in ds:
        if layer.GetName().endswith("_utm_scenario") and layer.GetFeatureCount():
            a, b, c, d = layer.GetExtent()
            bounds.append((a, c, b, d))
    ds = None
    original = gdal.Open(str(terrain / "data/dem_glo30_aoi_utm40n_30m.tif"))
    gt = original.GetGeoTransform()
    bounds.append((gt[0], gt[3] + gt[5]*original.RasterYSize,
                   gt[0] + gt[1]*original.RasterXSize, gt[3]))
    original = None
    pixel, buffer = float(pixel_m), float(buffer_m)
    if not (math.isfinite(pixel) and pixel >= 30 and math.isfinite(buffer) and 0 <= buffer <= 10000):
        raise ValueError("regional pixel must be >=30 m and buffer 0..10000 m")
    extent = [math.floor((min(b[0] for b in bounds)-buffer)/pixel)*pixel,
              math.floor((min(b[1] for b in bounds)-buffer)/pixel)*pixel,
              math.ceil((max(b[2] for b in bounds)+buffer)/pixel)*pixel,
              math.ceil((max(b[3] for b in bounds)+buffer)/pixel)*pixel]
    co = ["COMPRESS=DEFLATE", "TILED=YES"]
    horizontal = osr.SpatialReference()
    horizontal.ImportFromEPSG(32640)
    compound = osr.SpatialReference()
    compound.SetFromUserInput("EPSG:32640+3855")
    source = resources_root / DEMD
    products = [("dsm_utm30m.tif", source / TILE, "bilinear", -32767, "m")]
    products += [(f"quality_{code.lower()}.tif", source/rel,
                  "bilinear" if code == "HEM" else "near",
                  -32767 if code == "HEM" else 255, "m" if code == "HEM" else "1")
                 for code, rel in AUX.items()]
    diagnostics = {}
    surface = None
    for name, path, method, nodata, unit in products:
        dest = gdal.Warp(str(out/name), str(path), srcSRS="EPSG:4326", dstSRS="EPSG:32640",
                         outputBounds=extent, xRes=pixel, yRes=pixel, resampleAlg=method,
                         dstNodata=nodata, creationOptions=co,
                         **({"srcNodata": -32767} if name == "quality_hem.tif" else {}))
        dest.SetSpatialRef(compound if name.startswith("dsm") else horizontal)
        band = dest.GetRasterBand(1)
        band.SetUnitType(unit)
        arr = band.ReadAsArray()
        valid = np.isfinite(arr) & (arr != nodata)
        diagnostics[name] = {"shape": list(arr.shape), "resampling": method, "nodata": nodata,
                             "valid_pixels": int(valid.sum()), "missing_pixels": int((~valid).sum()),
                             "minimum": float(arr[valid].min()) if valid.any() else None,
                             "maximum": float(arr[valid].max()) if valid.any() else None}
        if name.startswith("dsm"):
            surface = arr
            surface_valid = valid
            grid = dest.GetGeoTransform()
        band = None
        dest = None
    for name, mask in [("surface_support.tif", surface_valid.astype("uint8"))]:
        dest = gdal.GetDriverByName("GTiff").Create(str(out/name), mask.shape[1], mask.shape[0], 1,
                                                   gdal.GDT_Byte, options=co)
        dest.SetGeoTransform(grid)
        dest.SetSpatialRef(horizontal)
        dest.GetRasterBand(1).WriteArray(mask)
        dest.GetRasterBand(1).SetUnitType("1")
        dest = None
    coverage = {}
    vectors = gdal.OpenEx(str(mining/"mining.gpkg"),gdal.OF_VECTOR)
    for site in ("skru1","skru2","skru3"):
        layer = vectors.GetLayerByName(site+"_workings_utm_scenario")
        memory = gdal.GetDriverByName("MEM").Create("",surface.shape[1],surface.shape[0],1,gdal.GDT_Byte)
        memory.SetGeoTransform(grid)
        memory.SetSpatialRef(horizontal)
        # This is a coarse raster coverage diagnostic, not a repair or volume calculation.
        gdal.RasterizeLayer(memory,[1],layer,burn_values=[1])
        mask = memory.ReadAsArray().astype(bool)
        hem = gdal.Open(str(out/"quality_hem.tif"))
        hem_values = hem.ReadAsArray()
        hem_valid = mask & (hem_values != -32767) & np.isfinite(hem_values)
        coverage[site] = {"rasterised_plan_cells":int(mask.sum()),
                          "surface_supported_cells":int((mask & surface_valid).sum()),
                          "surface_missing_cells":int((mask & ~surface_valid).sum()),
                          "hem_supported_cells":int(hem_valid.sum()),
                          "hem_missing_cells":int((mask & ~hem_valid).sum()),
                          "hem_random_error_median_m":float(np.median(hem_values[hem_valid])) if hem_valid.any() else None,
                          "interpretation":"approximate rasterised source plan area, 30m support; no geometry repair"}
        hem = None
        layer = None
        memory = None
    vectors = None
    # Diagnostic profile routes are explicit choices, not geological section traces.
    with (out/"dsm_profiles.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["profile_id", "sample_id", "east_m", "north_m", "distance_m", "height_EGM2008_m", "support"])
        for j, b in enumerate(bounds):
            x, y = np.linspace(b[0], b[2], 201), np.full(201, (b[1]+b[3])/2)
            for i, (e, n) in enumerate(zip(x,y)):
                col, row = int((e-grid[0])/grid[1]), int((n-grid[3])/grid[5])
                supported = (0 <= row < surface.shape[0] and 0 <= col < surface.shape[1]
                             and surface_valid[row,col])
                writer.writerow([f"DSM_DIAGNOSTIC_{j+1:02}", i, e, n, e-x[0],
                                 float(surface[row,col]) if supported else "", int(supported)])
    metadata = {"schema": "vkm.regional_surface/1", "epistemic_status": "DERIVATION",
                "input_product": "Copernicus GLO-30 N59E056 DSM, not bare-earth DTM",
                "epoch": {"acquisition_interval": [2011,2015], "historical_premining_surface": "UNKNOWN"},
                "horizontal": "EPSG:32640, east/north, m", "vertical": "EGM2008 EPSG:3855, m",
                "vertical_tie_to_mine_or_Baltic": None, "vertical_exaggeration": 1.,
                "extent_utm_m": extent, "pixel_m": pixel,
                "model_choices": [f"union of approximate plan extents and DMA area plus {buffer:g} m buffer",
                                  f"{pixel:g} m grid; raster interpolation does not improve measurement accuracy",
                                  "preserve DSM vegetation, structures, dumps and flattened water surface",
                                  "profiles east-west through extent centres are diagnostic routes"],
                "upper_rock_boundary": "UNKNOWN; DSM is an explicit available surface representation",
                "hem_scope": "source random height error component, not full terrain uncertainty",
                "diagnostics": diagnostics, "coverage_by_mine":coverage, "scientific_admission": "NOT_CHECKED"}
    write_json(out/"surface_metadata.json", metadata)
    return metadata


def _add_layers(project, path: Path, group, predicate, *, visible=False):
    from osgeo import gdal
    from qgis.core import (QgsVectorLayer, QgsSingleSymbolRenderer, QgsFillSymbol,
                           QgsLineSymbol, QgsMarkerSymbol, Qgis)
    source = gdal.OpenEx(str(path), gdal.OF_VECTOR)
    names = [source.GetLayer(i).GetName() for i in range(source.GetLayerCount())]
    source = None
    added = []
    for name in names:
        if not predicate(name):
            continue
        options=QgsVectorLayer.LayerOptions(); options.forceReadOnly=True
        layer = QgsVectorLayer(f"{path.resolve()}|layername={name}|option:IMMUTABLE=YES", name, "ogr",options)
        layer.setReadOnly(True)
        if not layer.isValid():
            raise ValueError(f"unreadable exported layer: {name}")
        kind = layer.geometryType()
        if kind == Qgis.GeometryType.Polygon:
            symbol = QgsFillSymbol.createSimple({"color":"0,0,0,0", "outline_color":"#7f3b08",
                                                 "outline_width":"0.3"})
        elif kind == Qgis.GeometryType.Line:
            symbol = QgsLineSymbol.createSimple({"line_color":"#1f78b4", "line_width":"0.3"})
        elif kind == Qgis.GeometryType.Point:
            symbol = QgsMarkerSymbol.createSimple({"name":"circle", "color":"#e31a1c", "size":"1.6"})
        else:
            symbol = None
        if symbol:
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        project.addMapLayer(layer, False)
        group.addLayer(layer).setItemVisibilityChecked(visible)
        layer.setCustomProperty("vkm/scientific_admission", "NOT_CHECKED")
        added.append(layer)
    return added


def build_qgis_scenes(package: Path) -> dict:
    from qgis.core import (QgsApplication, QgsProject, QgsRasterLayer, QgsCoordinateReferenceSystem,
                           QgsRasterDemTerrainProvider, Qgis, QgsReferencedRectangle, QgsRectangle)
    ensure_qgis_application()
    package = package.resolve()
    base = package/"terrain/dma_skru1_surface.qgz"
    projects = []
    p = QgsProject()
    if not p.read(str(base)):
        raise ValueError("surface project unreadable")
    p.setFilePathStorage(Qgis.FilePathType.Relative)
    root = p.layerTreeRoot()
    group = root.insertGroup(0, "Планы СКРУ1/2/3: приблизительное совмещение; Z неизвестна")
    approximate = _add_layers(p, package/"mining/mining.gpkg", group,
                              lambda n: n.endswith("_utm_scenario"), visible=False)
    # The regional DSM is not clipped by DMA and covers all approximate plan extents.
    raster = QgsRasterLayer(str(package/"regional/dsm_utm30m.tif"), "Региональный DSM, EGM2008, м; 2011–2015; ×1", "gdal")
    if not raster.isValid():
        raise ValueError("regional surface unreadable")
    p.addMapLayer(raster, False)
    root.addGroup("Непрерывная поверхность всех доступных планов").addLayer(raster)
    p.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(raster.extent(), p.crs()))
    provider = QgsRasterDemTerrainProvider()
    provider.setLayer(raster)
    provider.setScale(1.)
    provider.setOffset(0.)
    p.elevationProperties().setTerrainProvider(provider)
    raster.elevationProperties().setEnabled(True)
    group = root.addGroup("Граф связей, события и источники; неизвестные связи сохранены")
    _add_layers(p, package/"mining/mining.gpkg", group,
                lambda n: not n.endswith(("_local_source", "_utm_scenario", "_source_dated"))
                          and "checked_local" not in n, visible=False)
    p.setTitle("ВКМ: DSM и планы — горизонтальное совмещение приблизительное; подземная Z неизвестна")
    p.setFileName(str(package/"01_surface_and_planimetry.qgz"))
    p.setPresetHomePath("")
    if not p.write():
        raise ValueError("failed writing main project")
    projects.append({"path": p.fileName(), "layers":len(p.mapLayers()), "frame":"EPSG:32640"})
    p.clear()
    for i, site in enumerate(("skru1","skru2","skru3"), 2):
        p = QgsProject()
        p.setFilePathStorage(Qgis.FilePathType.Relative)
        group = p.layerTreeRoot().addGroup(f"{site.upper()}: локальные исходные планы и состояния")
        added = _add_layers(p, package/"mining/mining.gpkg", group,
                             lambda n: n.startswith(site+"_") and n.endswith(("_local_source", "_source_dated", "checked_local")),
                             visible=True)
        # Raw automatic point candidates are inspectable, never the default well layer.
        for layer in added:
            if layer.name().endswith("points_local_source"):
                p.layerTreeRoot().findLayer(layer.id()).setItemVisibilityChecked(False)
        spatial = [l for l in added if l.isSpatial()]
        if spatial:
            p.setCrs(spatial[0].crs())
            extent = QgsRectangle(spatial[0].extent())
            for layer in spatial[1:]:
                extent.combineExtentWith(layer.extent())
            p.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(extent, p.crs()))
        p.setTitle(f"{site.upper()}: исходный локальный frame, м; абсолютная Z UNKNOWN")
        p.setFileName(str(package/f"{i:02}_{site}_local.qgz"))
        p.setPresetHomePath("")
        if not p.write():
            raise ValueError("failed writing local project")
        projects.append({"path":p.fileName(), "layers":len(added), "frame":site.upper()+"_LOCAL"})
        p.clear()
    # Geology products retain source-section coordinates; never overlay on map XY.
    geology = package/"geology"
    gpkg = geology/"geology.gpkg"
    if gpkg.exists():
        p = QgsProject()
        p.setFilePathStorage(Qgis.FilePathType.Relative)
        group = p.layerTreeRoot().addGroup("Скважины и интервалы: local XY; значения не являются абсолютной Z")
        added = _add_layers(p, gpkg, group, lambda n: n != "interval_grid_local", visible=True)
        for layer in added:
            if not layer.isSpatial():
                p.layerTreeRoot().findLayer(layer.id()).setItemVisibilityChecked(False)
        # Keep different intervals/methods separately switchable; no hidden method winner.
        from qgis.core import QgsVectorLayer, QgsMarkerSymbol, QgsSingleSymbolRenderer
        method_colors = {"nearest":"#666666", "idw1":"#2166ac", "idw2":"#762a83", "linear":"#d95f02"}
        for interval in ("TKT_to_SMT","SMT_to_PKS","SKZ_to_SZ","SZ_to_PDKS"):
            subgroup = group.addGroup(interval+": интервальная величина, м; сценарий")
            for method in ("nearest","idw1","idw2","linear"):
                options=QgsVectorLayer.LayerOptions(); options.forceReadOnly=True
                layer = QgsVectorLayer(f"{gpkg.resolve()}|layername=interval_grid_local|option:IMMUTABLE=YES",method,"ogr",options)
                layer.setReadOnly(True)
                if not layer.setSubsetString(f'"interval_id" = \'{interval}\' AND "method" = \'{method}\''):
                    raise ValueError("interval subset failed")
                # QGIS otherwise assigns a random default colour on each build.
                layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple({
                    "name":"circle", "color":method_colors[method], "size":"1.8",
                    "outline_color":"#333333", "outline_width":"0.15"})))
                p.addMapLayer(layer,False); subgroup.addLayer(layer).setItemVisibilityChecked(False)
        spatial = [l for l in added if l.isSpatial()]
        if spatial:
            p.setCrs(spatial[0].crs())
            p.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(spatial[0].extent(),p.crs()))
        p.setTitle("Геология: локальные опоры и интервальные сценарии; абсолютные поверхности BLOCKED")
        p.setFileName(str(package/"05_geology_local_constraints.qgz"))
        p.setPresetHomePath("")
        if not p.write():
            raise ValueError("failed writing geological project")
        projects.append({"path":p.fileName(), "layers":len(p.mapLayers()), "frame":"SKRU1_LOCAL_SH1"})
        p.clear()
    section = geology/"section/source_section.gpkg"
    if section.exists():
        from qgis.core import QgsCategorizedSymbolRenderer,QgsRendererCategory,QgsFillSymbol
        p=QgsProject(); p.setFilePathStorage(Qgis.FilePathType.Relative)
        group=p.layerTreeRoot().addGroup("Исходный метрический разрез; трасса на карте и datum UNKNOWN")
        added=_add_layers(p,section,group,lambda n:True,visible=True)
        for layer in added:
            if layer.name()=="section_units":
                palette={f["unit_id"]:f["color_rgb"] for f in layer.getFeatures()}
                categories=[]
                for unit,colour in sorted(palette.items()):
                    rgb=json.loads(colour)
                    symbol=QgsFillSymbol.createSimple({"color":",".join(map(str,rgb)),"outline_color":"#555555","outline_width":"0.15"})
                    categories.append(QgsRendererCategory(unit,symbol,unit))
                layer.setRenderer(QgsCategorizedSymbolRenderer("unit_id",categories))
        p.setCrs(added[0].crs())
        extent=QgsRectangle(added[0].extent())
        for layer in added[1:]:
            extent.combineExtentWith(layer.extent())
        extent.scale(1.1)
        p.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(extent,p.crs()))
        p.setTitle("SRC011 fig4.7a: source section, m; graphical derivation; map trace UNKNOWN")
        p.setFileName(str(package/"06_source_geological_section.qgz")); p.setPresetHomePath("")
        if not p.write():
            raise ValueError("failed writing section scene")
        projects.append({"path":p.fileName(),"layers":len(added),"frame":"SECTION_S011_F4_7A"})
        p.clear()
    for item in projects:
        item["path"] = Path(item["path"]).relative_to(package).as_posix()
    return {"projects":projects, "vertical_exaggeration":1.,
            "geology_absolute_alignment":"UNKNOWN",
            "mining_rendering":"plan footprints; no subsurface elevation assigned"}
