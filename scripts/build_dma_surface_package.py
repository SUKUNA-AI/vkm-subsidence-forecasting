#!/usr/bin/env python3
"""DMA contours of SKRU-1 on the ground surface (12.10.2026): a checked GeoPackage of contours, indicator records and
the links between them, the elevation surface from an open DEM, alignment checks, previews and a QGIS project with
relative paths. Run with the QGIS Python (GDAL, PyQGIS, numpy, shapely, matplotlib):

    python-qgis scripts/build_dma_surface_package.py --out <new package dir>       (VKM_RESOURCES_ROOT = PRIVATE)
    python-qgis scripts/build_dma_surface_package.py --out <new dir> --verify <existing package dir>

Inputs (PRIVATE ``08_data_archives/``), every one checked by hash before use:
- ``dma_skru1_losev_20261011/``: dataset version ``dma-skru1-losev`` (``dataset_manifest.json``, originals) and the two
  verified direct conversions to GPKG (``conversions/geojson``, ``conversions/csv``; ``vkm_datasets``);
- ``copernicus_dem_glo30_n59e056/``: Copernicus DEM GLO-30 tile N59E056 (DSM, EGM2008) and its quality layers;
- ``osm_controls_20261012/``: OSM rivers and mine shafts (independent control objects).

Rules: originals are never written; one geometry is stored once for display, but every source feature, every CSV row,
every indicator record and every link keeps its source identity (GeoJSON FID 0…, CSV row 1…); identical numbers are
not identical objects; all load / index variants are kept (never averaged, never the first one only); empty and
attribute-less objects stay; no MakeValid, clipping or removal of overlaps. The surface is the DEM grid itself
(reprojected by bilinear resampling to the working CRS on a 30 m grid — not denser than the source, no gain in
accuracy), never one height per polygon; NoData is never 0; the vertical datum stays EGM2008 (no shift to the
Baltic system: the transformation is UNKNOWN). Every derived object is DERIVATION; scientific admission NOT_CHECKED.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vkm_datasets import DatasetVersion, verify_members  # noqa: E402
from vkm_datasets.gis import verify_conversion_bundle  # noqa: E402

RULE = "dma_surface_package_v1"
WORK_EPSG, VERT_EPSG = 32640, 3855                      # WGS 84 / UTM 40N; EGM2008 heights
PIXEL = 30.0
AOI_BUFFER = 2000.0
FIXED_DATE = "2026-10-12T00:00:00Z"                     # GeoPackage timestamps: reproducible bytes
DSET = Path("08_data_archives/dma_skru1_losev_20261011")
DEMD = Path("08_data_archives/copernicus_dem_glo30_n59e056")
OSMD = Path("08_data_archives/osm_controls_20261012")
TILE = "Copernicus_DSM_COG_10_N59_00_E056_00_DEM.tif"
AUX = {"FLM": "AUXFILES/Copernicus_DSM_COG_10_N59_00_E056_00_FLM.tif",
       "EDM": "AUXFILES/Copernicus_DSM_COG_10_N59_00_E056_00_EDM.tif",
       "WBM": "AUXFILES/Copernicus_DSM_COG_10_N59_00_E056_00_WBM.tif",
       "HEM": "AUXFILES/Copernicus_DSM_COG_10_N59_00_E056_00_HEM.tif"}
MASK_CODES = {   # Copernicus DEM Product Handbook i5.0, tables 6–8
    "EDM": {0: "void", 1: "not edited", 2: "infill of external elevation data", 3: "interpolated", 4: "smoothed",
            5: "airport editing", 6: "raised negative elevation", 7: "flattened", 8: "ocean", 9: "lake", 10: "river",
            11: "shoreline", 12: "morphed", 13: "shifted"},
    "FLM": {0: "void", 1: "edited (not filled)", 2: "not edited / not filled", 3: "ASTER", 4: "SRTM90", 5: "SRTM30",
            6: "GMTED2010", 7: "SRTM30plus", 8: "TerraSAR-X radargrammetric", 9: "AW3D30"},
    "WBM": {0: "no water", 1: "ocean", 2: "lake", 3: "river"}}
ATTRIBUTION = ("produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH "
               "2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved")
DEM_LABEL = "Copernicus DEM GLO-30 (DSM, TanDEM-X 2011–2015), высоты EGM2008, м"
FIELDS = ("xcoord", "ycoord", "exel_Load", "TRI_mean", "TRI_range", "Canny_mean", "Canny_range", "SRTM140_mean",
          "SRTM140_range", "SRTM245_mean", "SRTM245_range", "Disp_mean_500", "Disp_min_500", "Disp_max_500",
          "Disp_mean_1000", "Disp_min_1000", "Disp_max_1000", "Rel_mera", "Disp500_mera", "Disp1000_mera",
          "Disp500_Rel_mera", "Disp1000_Rel_mera")
THEMES = ("exel_Load", "Rel_mera", "Disp500_mera", "Disp1000_mera", "Disp500_Rel_mera", "Disp1000_Rel_mera",
          "TRI_mean", "Disp_mean_1000")
CODE_FILES = ("scripts/build_dma_surface_package.py", "src/vkm_datasets/gis.py", "src/vkm_datasets/manifest.py")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ranges(nums: list[int]) -> str:
    """[2914, 2915, 2916, 3001] → «2914-2916;3001»."""
    out, nums = [], sorted(nums)
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"{nums[i]}-{nums[j]}" if j > i else f"{nums[i]}")
        i = j + 1
    return ";".join(out)


# ------------------------------------------------------------------------------------------------------- inputs
def load_inputs(res: Path) -> dict:
    base = res / DSET
    manifest = DatasetVersion.from_dict(json.loads((base / "dataset_manifest.json").read_text(encoding="utf-8")))
    verify_members(base / "original", manifest.files)
    proofs = {k: verify_conversion_bundle(base / "conversions" / k, manifest.digest) for k in ("geojson", "csv")}
    feats = []
    ds = gdal.OpenEx(str(base / "conversions/geojson/data.gpkg"), gdal.OF_VECTOR | gdal.OF_READONLY)
    lyr = ds.GetLayerByName("QGIS_DMA2")
    for f in lyr:
        g = f.GetGeometryRef()
        vals = None if not f.IsFieldSetAndNotNull("xcoord") else tuple(f.GetField(k) for k in FIELDS)
        feats.append({"fid": f.GetFID(), "wkb": bytes(g.ExportToIsoWkb()) if g is not None else b"",
                      "empty": g is None or g.IsEmpty(), "values": vals})
    ds = None
    rows = []
    ds = gdal.OpenEx(str(base / "conversions/csv/data.gpkg"), gdal.OF_VECTOR | gdal.OF_READONLY)
    lyr = ds.GetLayer(0)
    for f in lyr:
        rows.append({"row": f.GetFID(), "raw": tuple(f.GetField(k) for k in FIELDS)})
    ds = None
    feats.sort(key=lambda x: x["fid"])
    rows.sort(key=lambda x: x["row"])
    dem = res / DEMD
    intake = json.loads((dem / "INTAKE_RECEIPT.json").read_text(encoding="utf-8"))
    for f in intake["files"]:
        if sha_file(dem / f["path"]) != f["sha256"]:
            raise ValueError("DEM file hash mismatch: " + f["path"])
    osm_intake = json.loads((res / OSMD / "INTAKE_RECEIPT.json").read_text(encoding="utf-8"))
    if sha_file(res / OSMD / "overpass_controls.json") != osm_intake["file"]["sha256"]:
        raise ValueError("OSM file hash mismatch")
    return {"manifest": manifest, "proofs": proofs, "features": feats, "rows": rows, "dem_dir": dem,
            "dem_intake": intake, "osm": json.loads((res / OSMD / "overpass_controls.json").read_text(encoding="utf-8")),
            "osm_intake": osm_intake}


# -------------------------------------------------------------------------------------------------------- links
def link(feats: list[dict], rows: list[dict]) -> dict:
    """Split the joined layer into geometries, indicator records, coordinate groups and the links between them."""
    value_of, values = {}, []                 # float tuple → value id; first appearance in the CSV
    group_of, groups = {}, []
    for r in rows:
        fl = tuple(float(x) for x in r["raw"])
        if fl not in value_of:
            value_of[fl] = f"V{len(values) + 1:04d}"
            values.append({"value_id": value_of[fl], "raw": r["raw"], "values": fl, "rows": []})
        values[int(value_of[fl][1:]) - 1]["rows"].append(r["row"])
        cg = (fl[0], fl[1])
        if cg not in group_of:
            group_of[cg] = f"C{len(groups) + 1:04d}"
            groups.append({"cg_id": group_of[cg], "raw_xy": r["raw"][:2], "xy": cg, "rows": [], "fids": [],
                           "geoms": set()})
        groups[int(group_of[cg][1:]) - 1]["rows"].append(r["row"])
        r["value_id"], r["cg_id"] = value_of[fl], group_of[cg]
    geom_of, geoms = {}, []
    pairs = defaultdict(list)
    for f in feats:
        f["geom_id"] = None
        if not f["empty"]:
            h = sha(f["wkb"])
            if h not in geom_of:
                geom_of[h] = f"G{len(geoms) + 1:04d}"
                geoms.append({"geom_id": geom_of[h], "wkb_sha256": h, "wkb": f["wkb"], "fids": [], "values": set(),
                              "groups": set()})
            f["geom_id"] = geom_of[h]
            geoms[int(f["geom_id"][1:]) - 1]["fids"].append(f["fid"])
        f["value_id"] = f["cg_id"] = None
        if f["values"] is not None:
            if f["values"] not in value_of:
                raise ValueError(f"GeoJSON FID {f['fid']}: attribute record absent from the CSV")
            f["value_id"] = value_of[f["values"]]
            f["cg_id"] = group_of[(f["values"][0], f["values"][1])]
            g = groups[int(f["cg_id"][1:]) - 1]
            g["fids"].append(f["fid"])
            if f["geom_id"]:
                g["geoms"].add(f["geom_id"])
                gm = geoms[int(f["geom_id"][1:]) - 1]
                gm["values"].add(f["value_id"])
                gm["groups"].add(f["cg_id"])
                pairs[(f["geom_id"], f["value_id"])].append(f["fid"])
    links = [{"link_id": f"L{i:04d}", "geom_id": g, "value_id": v, "fids": fids}
             for i, ((g, v), fids) in enumerate(sorted(pairs.items()), 1)]
    return {"values": values, "groups": groups, "geoms": geoms, "links": links, "features": feats, "rows": rows}


# ---------------------------------------------------------------------------------------------------- geometry
def transforms():
    src, dst = osr.SpatialReference(), osr.SpatialReference()
    src.ImportFromEPSG(4326)
    dst.ImportFromEPSG(WORK_EPSG)
    for s in (src, dst):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(src, dst), osr.CoordinateTransformation(dst, src), src, dst


def metric(L: dict) -> dict:
    fwd, back, _, _ = transforms()
    from shapely import wkb as swkb
    worst = 0.0
    for g in L["geoms"]:
        geom = ogr.CreateGeometryFromWkb(g["wkb"])
        utm = geom.Clone()
        utm.Transform(fwd)
        g["utm_wkb"] = bytes(utm.ExportToIsoWkb())
        g["area_m2"] = utm.GetArea()
        rt = utm.Clone()
        rt.Transform(back)
        a, b = swkb.loads(g["wkb"]), swkb.loads(bytes(rt.ExportToIsoWkb()))
        for pa, pb in zip(_coords(a), _coords(b)):
            dx = (pa[0] - pb[0]) * 111320 * math.cos(math.radians(pa[1]))
            worst = max(worst, abs(dx), abs((pa[1] - pb[1]) * 110574))
    for c in L["groups"]:
        p = ogr.Geometry(ogr.wkbPoint)
        p.AddPoint_2D(*c["xy"])
        p.Transform(fwd)
        c["utm"] = (p.GetX(), p.GetY())
    return {"roundtrip_max_m": worst}


def _coords(geom):
    for poly in getattr(geom, "geoms", [geom]):
        yield from poly.exterior.coords
        for ring in poly.interiors:
            yield from ring.coords


def overlaps(L: dict) -> list[dict]:
    from shapely import wkb as swkb, STRtree
    from shapely.geometry import MultiPolygon
    G = [swkb.loads(g["utm_wkb"]) for g in L["geoms"]]
    tree = STRtree(G)
    out = []
    for a, b in zip(*tree.query(G, predicate="intersects")):
        if a >= b:
            continue
        inter = G[a].intersection(G[b])
        # keep the areal part only: touching edges / vertices add lines and points to the intersection
        polys = [x for x in getattr(inter, "geoms", [inter]) if x.geom_type in ("Polygon", "MultiPolygon")]
        inter = MultiPolygon([q for x in polys for q in getattr(x, "geoms", [x])])
        if inter.area <= 1.0:
            continue
        small = min(G[a].area, G[b].area)
        frac = inter.area / small
        within = G[a].within(G[b]) or G[b].within(G[a])
        kind = "CONTAINMENT" if within else "MAJOR_OVERLAP" if (frac > 0.10 or inter.area > 1000) else "SLIVER"
        out.append({"geom_a": L["geoms"][a]["geom_id"], "geom_b": L["geoms"][b]["geom_id"], "area_m2": inter.area,
                    "frac_of_smaller": frac, "kind": kind, "wkb": inter.wkb})
    out.sort(key=lambda o: -o["area_m2"])
    for i, o in enumerate(out, 1):
        o["overlap_id"] = f"O{i:03d}"
    return out


def centroid_offsets(L: dict) -> None:
    from shapely import wkb as swkb
    for g in L["geoms"]:
        c = swkb.loads(g["utm_wkb"]).centroid
        g["centroid"] = (c.x, c.y)
        g["centroid_offset_m"] = {cg: math.dist((c.x, c.y), L["groups"][int(cg[1:]) - 1]["utm"])
                                  for cg in sorted(g["groups"])}


def qa_issues(L: dict, ovl: list[dict]) -> list[dict]:
    out = []

    def add(kind, severity, refs, text):
        out.append({"issue_id": f"Q{len(out) + 1:03d}", "kind": kind, "severity": severity, "refs": refs,
                    "description": text})
    for o in ovl:
        ga, gb = (L["geoms"][int(x[1:]) - 1] for x in (o["geom_a"], o["geom_b"]))
        add("OVERLAP_" + o["kind"], "HIGH" if o["kind"] != "SLIVER" else "LOW",
            f"{o['overlap_id']}: {o['geom_a']} (FID {ranges(ga['fids'])}) × {o['geom_b']} (FID {ranges(gb['fids'])})",
            f"пересечение {o['area_m2']:.0f} м² = {100 * o['frac_of_smaller']:.1f} % меньшего контура; не исправлялось "
            "(без MakeValid, обрезки и удаления); основание для выбора — нет")
    empty = [f["fid"] for f in L["features"] if f["empty"]]
    if empty:
        add("EMPTY_GEOMETRY", "MEDIUM", "FID " + ranges(empty),
            "пустая MultiPolygon у объектов; атрибутов нет; сохранены в source_features")
    noattr = sorted({f["geom_id"] for f in L["features"] if f["value_id"] is None and f["geom_id"]})
    if noattr:
        add("CONTOUR_WITHOUT_ATTRIBUTES", "MEDIUM", ";".join(noattr),
            f"{len(noattr)} контуров ({sum(1 for f in L['features'] if f['value_id'] is None and f['geom_id'])} объектов) "
            "без показателей; контуры сохранены, связь с CSV отсутствует")
    for c in L["groups"]:
        if len(c["geoms"]) > 1:
            add("COORD_GROUP_SEVERAL_GEOMETRIES", "HIGH", f"{c['cg_id']} → {';'.join(sorted(c['geoms']))}",
                f"одна координатная группа (k = {len(c['rows'])} строк CSV) связана с {len(c['geoms'])} контурами; "
                f"объектов {len(c['fids'])} при ожидаемых k² = {len(c['rows']) ** 2}")
        elif len(c["fids"]) != len(c["rows"]) ** 2:
            add("JOIN_MULTIPLICITY", "MEDIUM", c["cg_id"], f"объектов {len(c['fids'])}, k² = {len(c['rows']) ** 2}")
    for g in L["geoms"]:
        if len(g["groups"]) > 1:
            add("GEOMETRY_SEVERAL_COORD_GROUPS", "HIGH", f"{g['geom_id']} ← {';'.join(sorted(g['groups']))}",
                "один контур несёт строки двух координатных групп; центроид совпадает только с одной из них")
        bad = {cg: d for cg, d in g["centroid_offset_m"].items() if d > 5.0}
        if bad:
            add("CENTROID_NOT_AT_XY", "MEDIUM", f"{g['geom_id']} ({'; '.join(f'{k}: {v:.0f} м' for k, v in bad.items())})",
                "xcoord/ycoord записи не совпадают с центроидом контура (у остальных — < 1 мм)")
    import re
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x
    item = [q for q in out if q["kind"] not in ("CONTOUR_WITHOUT_ATTRIBUTES",)]
    for q in item:
        ids = re.findall(r"G\d{4}", q["refs"])
        for a, b in zip(ids, ids[1:]):
            parent[find(a)] = find(b)
    roots = {}
    for q in item:
        ids = re.findall(r"G\d{4}", q["refs"])
        q["cluster"] = ""
        if ids:
            members = sorted(g for g in parent if find(g) == find(ids[0]))
            if sum(1 for o in item if set(re.findall(r"G\d{4}", o["refs"])) & set(members)) > 1:
                q["cluster"] = roots.setdefault(find(ids[0]), f"K{len(roots) + 1}") + ": " + ";".join(members)
    multi = [g for g in L["geoms"] if len({L["values"][int(v[1:]) - 1]["values"][2] for v in g["values"]}) > 1]
    add("SEVERAL_LOADS_PER_CONTOUR", "INFO", f"{len(multi)} контуров",
        "у контура несколько записей с разной нагрузкой exel_Load (природа — пласт/блок? — не установлена в данных); "
        "все варианты сохранены в contour_variants, не усреднены")
    return out


# --------------------------------------------------------------------------------------------------------- DEM
def aoi_bounds(L: dict) -> tuple[float, float, float, float]:
    from shapely import wkb as swkb
    xs, ys = [], []
    for g in L["geoms"]:
        b = swkb.loads(g["utm_wkb"]).bounds
        xs += [b[0], b[2]]
        ys += [b[1], b[3]]
    f = lambda v, up: (math.ceil if up else math.floor)(v / PIXEL) * PIXEL       # noqa: E731
    return (f(min(xs) - AOI_BUFFER, False), f(min(ys) - AOI_BUFFER, False), f(max(xs) + AOI_BUFFER, True),
            f(max(ys) + AOI_BUFFER, True))


def dem_products(inp: dict, aoi, out: Path) -> dict:
    fwd, back, src, dst = transforms()
    lon, lat = [], []
    for x, y in ((aoi[0], aoi[1]), (aoi[0], aoi[3]), (aoi[2], aoi[1]), (aoi[2], aoi[3])):
        p = ogr.Geometry(ogr.wkbPoint)
        p.AddPoint_2D(x, y)
        p.Transform(back)
        lon.append(p.GetX())
        lat.append(p.GetY())
    win = [min(lon) - 0.005, max(lat) + 0.005, max(lon) + 0.005, min(lat) - 0.005]
    co = ["COMPRESS=DEFLATE", "TILED=YES"]
    tile = str(inp["dem_dir"] / TILE)
    native = out / "data" / "dem_glo30_aoi_native_wgs84.tif"
    gdal.Translate(str(native), tile, projWin=win, outputSRS=f"EPSG:4326+{VERT_EPSG}", creationOptions=co + ["PREDICTOR=3"])
    surf = out / "data" / "dem_glo30_aoi_utm40n_30m.tif"
    tmp = out / "data" / "_warp.tif"
    gdal.Warp(str(tmp), tile, srcSRS="EPSG:4326", dstSRS=f"EPSG:{WORK_EPSG}", outputBounds=aoi, xRes=PIXEL, yRes=PIXEL,
              resampleAlg="bilinear", dstNodata=-32767, outputType=gdal.GDT_Float32,
              creationOptions=co + ["PREDICTOR=3"])
    gdal.Translate(str(surf), str(tmp), outputSRS=f"EPSG:{WORK_EPSG}+{VERT_EPSG}", noData=-32767,
                   creationOptions=co + ["PREDICTOR=3"])
    tmp.unlink()
    hill = out / "data" / "hillshade_utm40n_30m.tif"
    gdal.DEMProcessing(str(hill), str(surf), "hillshade", azimuth=315, altitude=45, zFactor=1.0, computeEdges=True,
                       creationOptions=co)
    quality = {}
    for code, rel in AUX.items():
        q = out / "data" / f"quality_{code.lower()}_utm40n_30m.tif"
        nodata = {"srcNodata": -32767, "dstNodata": -32767} if code == "HEM" else {}   # HEM: edited pixels
        gdal.Warp(str(q), str(inp["dem_dir"] / rel), srcSRS="EPSG:4326", dstSRS=f"EPSG:{WORK_EPSG}",
                  outputBounds=aoi, xRes=PIXEL, yRes=PIXEL, resampleAlg="bilinear" if code == "HEM" else "near",
                  creationOptions=co, **nodata)
        quality[code] = q
    return {"native": native, "surface": surf, "hillshade": hill, "quality": quality, "projwin_wgs84": win}


def read_band(p: Path):
    ds = gdal.Open(str(p))
    b = ds.GetRasterBand(1)
    arr = b.ReadAsArray().astype("float64")
    nd = b.GetNoDataValue()
    gt = ds.GetGeoTransform()
    ds = None
    return arr, nd, gt


def footprint_mask(L: dict, gt, shape) -> np.ndarray:
    drv = gdal.GetDriverByName("MEM")
    r = drv.Create("", shape[1], shape[0], 1, gdal.GDT_Byte)
    r.SetGeoTransform(gt)
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(WORK_EPSG)
    r.SetProjection(sr.ExportToWkt())
    v = gdal.GetDriverByName("MEM").Create("", 0, 0, 0, gdal.GDT_Unknown)       # GDAL ≥ 3.11: MEM holds vectors
    lyr = v.CreateLayer("c", sr, ogr.wkbMultiPolygon)
    for g in L["geoms"]:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkb(g["utm_wkb"]))
        lyr.CreateFeature(f)
    gdal.RasterizeLayer(r, [1], lyr, burn_values=[1])
    return r.GetRasterBand(1).ReadAsArray().astype(bool)


def contour_edges(L: dict, gt, shape) -> np.ndarray:
    """Edges of the contours burnt on the surface grid: 1 — contour, 2 — contour without indicators, 3 — overlap."""
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(WORK_EPSG)
    r = gdal.GetDriverByName("MEM").Create("", shape[1], shape[0], 1, gdal.GDT_Byte)
    r.SetGeoTransform(gt)
    r.SetProjection(sr.ExportToWkt())
    v = gdal.GetDriverByName("MEM").Create("", 0, 0, 0, gdal.GDT_Unknown)
    lyr = v.CreateLayer("e", sr, ogr.wkbMultiLineString)
    lyr.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    noattr = {f["geom_id"] for f in L["features"] if f["value_id"] is None and f["geom_id"]}
    for g in L["geoms"]:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("code", 2 if g["geom_id"] in noattr else 1)
        f.SetGeometry(ogr.CreateGeometryFromWkb(g["utm_wkb"]).GetBoundary())
        lyr.CreateFeature(f)
    for o in L.get("overlaps", []):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("code", 3)
        f.SetGeometry(ogr.CreateGeometryFromWkb(o["wkb"]).GetBoundary())
        lyr.CreateFeature(f)
    gdal.RasterizeLayer(r, [1], lyr, options=["ATTRIBUTE=code", "ALL_TOUCHED=TRUE"])
    return r.GetRasterBand(1).ReadAsArray()


def dem_checks(L: dict, prod: dict) -> dict:
    z, nd, gt = read_band(prod["surface"])
    valid = z != nd
    mask = footprint_mask(L, gt, z.shape)
    out = {"grid": {"rows": z.shape[0], "cols": z.shape[1], "pixel_m": PIXEL, "origin": [gt[0], gt[3]]},
           "nodata_value": nd, "nodata_pixels_aoi": int((~valid).sum()), "nodata_pixels_in_contours": int((~valid & mask).sum()),
           "zero_pixels_aoi": int((valid & (z == 0)).sum()),
           "elevation_aoi_m": [round(float(z[valid].min()), 2), round(float(z[valid].max()), 2)],
           "elevation_contours_m": [round(float(z[valid & mask].min()), 2), round(float(z[valid & mask].max()), 2)],
           "contour_pixels": int(mask.sum())}
    for code, q in prod["quality"].items():
        a, qnd, _ = read_band(q)
        if code == "HEM":
            ok = mask & (a != qnd)
            out["HEM_sigma_m_in_contours"] = {"pixels": int(ok.sum()), "edited_pixels_without_hem": int((mask & (a == qnd)).sum()),
                                              "mean": round(float(a[ok].mean()), 3),
                                              "p95": round(float(np.percentile(a[ok], 95)), 3), "max": round(float(a[ok].max()), 3)}
        else:
            vals = Counter(int(v) for v in a[mask].ravel())
            out[f"{code}_in_contours"] = {f"{k} {MASK_CODES[code].get(k, '?')}": v for k, v in sorted(vals.items())}
    return out


def sample(z, nd, gt, x, y):
    """Bilinear sample of the surface at (x, y) in the working CRS; None outside or near NoData."""
    c = (x - gt[0]) / gt[1] - 0.5
    r = (y - gt[3]) / gt[5] - 0.5
    i, j = int(math.floor(r)), int(math.floor(c))
    if i < 0 or j < 0 or i + 1 >= z.shape[0] or j + 1 >= z.shape[1]:
        return None
    w = z[i:i + 2, j:j + 2]
    if (w == nd).any():
        return None
    fr, fc = r - i, c - j
    return float(w[0, 0] * (1 - fr) * (1 - fc) + w[0, 1] * (1 - fr) * fc + w[1, 0] * fr * (1 - fc) + w[1, 1] * fr * fc)


def alignment(L: dict, prod: dict, osm: dict) -> dict:
    """Independent controls. DEM: OSM river lines must run along valley floors (lowest point of a ±150 m profile
    across the line); a horizontal shift of the DEM would show as a systematic offset. Contours: the OSM SKRU-1 shaft
    must not lie inside a mined contour (a shaft stands in its safety pillar) — a plausibility check only."""
    from shapely.geometry import LineString, Point
    from shapely import wkb as swkb
    from shapely.ops import unary_union
    fwd, _, _, _ = transforms()
    z, nd, gt = read_band(prod["surface"])

    def utm(lon, lat):
        p = ogr.Geometry(ogr.wkbPoint)
        p.AddPoint_2D(lon, lat)
        p.Transform(fwd)
        return p.GetX(), p.GetY()
    rivers, shafts = defaultdict(list), []
    for e in osm["elements"]:
        t = e.get("tags", {})
        if t.get("waterway") == "river" and e.get("geometry"):
            rivers[t.get("name", "?")].append(LineString([utm(p["lon"], p["lat"]) for p in e["geometry"]]))
        elif t.get("man_made") == "mineshaft" and "lat" in e:
            shafts.append({"osm_id": f"node/{e['id']}", "name": t.get("name", ""), "operator": t.get("operator", ""),
                           "xy": utm(e["lon"], e["lat"])})
    res = {"rivers": {}, "shafts": [], "osm_snapshot": osm["osm3s"]["timestamp_osm_base"]}
    all_off = []
    for name, lines in sorted(rivers.items()):
        offs, drops = [], []
        for ln in lines:
            n = int(ln.length // 200)
            for k in range(1, n):
                a, b = ln.interpolate(k * 200 - 5), ln.interpolate(k * 200 + 5)
                p = ln.interpolate(k * 200)
                dx, dy = b.x - a.x, b.y - a.y
                norm = math.hypot(dx, dy) or 1
                px, py = -dy / norm, dx / norm
                prof = [(s, sample(z, nd, gt, p.x + s * px, p.y + s * py)) for s in range(-150, 151, 10)]
                if any(v is None for _, v in prof):
                    continue
                smin = min(prof, key=lambda t: t[1])[0]
                offs.append(smin)
                drops.append(np.mean([v for s, v in prof if abs(s) >= 120]) - dict(prof)[0])
        if offs:
            a = np.abs(np.array(offs))
            bank = float(np.median(drops))
            informative = bank >= 0.5
            res["rivers"][name] = {"profiles": len(offs), "median_abs_offset_m": float(np.median(a)),
                                   "share_within_30m": round(float((a <= 30).mean()), 3),
                                   "median_signed_offset_m": float(np.median(offs)),
                                   "median_bank_minus_line_m": round(bank, 2),
                                   "use": "CONTROL" if informative else
                                   "NOT_INFORMATIVE: flat water wider than the ±150 m profile (no valley floor)"}
            if informative:
                all_off += offs
    if all_off:
        a = np.abs(np.array(all_off))
        res["rivers_informative"] = {"profiles": len(all_off), "median_abs_offset_m": float(np.median(a)),
                                     "share_within_30m": round(float((a <= 30).mean()), 3),
                                     "note": "small rivers in forest: a DSM valley floor under canopy is less sharp"}
    mined = unary_union([swkb.loads(g["utm_wkb"]) for g in L["geoms"]])
    for s in shafts:
        p = Point(s["xy"])
        res["shafts"].append({**{k: v for k, v in s.items() if k != "xy"}, "inside_contour": bool(mined.contains(p)),
                              "distance_to_nearest_contour_m": round(float(mined.distance(p)), 1)})
    return res


# ---------------------------------------------------------------------------------------------------- GeoPackage
def write_gpkg(path: Path, L: dict, ovl: list[dict], issues: list[dict], align: dict, aoi) -> None:
    gdal.SetConfigOption("OGR_CURRENT_DATE", FIXED_DATE)
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(str(path))
    utm, wgs = osr.SpatialReference(), osr.SpatialReference()
    utm.ImportFromEPSG(WORK_EPSG)
    wgs.ImportFromEPSG(4326)
    for s in (utm, wgs):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    T = {int: ogr.OFTInteger, float: ogr.OFTReal, str: ogr.OFTString}

    def layer(name, srs, gtype, cols, records, geom_key=None, desc=""):
        lyr = ds.CreateLayer(name, srs, gtype, options=["SPATIAL_INDEX=YES" if gtype != ogr.wkbNone else "",
                                                        f"DESCRIPTION={desc}"] if gtype != ogr.wkbNone
                             else [f"DESCRIPTION={desc}"])
        for c, t in cols:
            lyr.CreateField(ogr.FieldDefn(c, T[t]))
        for rec in records:
            f = ogr.Feature(lyr.GetLayerDefn())
            for c, t in cols:
                v = rec.get(c)
                if v is not None:
                    f.SetField(c, t(v))
            if geom_key and rec.get(geom_key):
                f.SetGeometry(ogr.CreateGeometryFromWkb(rec[geom_key]))
            lyr.CreateFeature(f)
        return lyr

    V = L["values"]
    variants = defaultdict(list)
    for lk in L["links"]:
        variants[lk["geom_id"]].append(lk)
    geo_rows = []
    for g in L["geoms"]:
        vals = [V[int(v[1:]) - 1] for v in sorted(g["values"])]
        loads = sorted({v["values"][2] for v in vals})
        geo_rows.append({"geom_id": g["geom_id"], "wkb_sha256": g["wkb_sha256"], "source_fids": ranges(g["fids"]),
                         "n_source_features": len(g["fids"]), "n_value_records": len(vals),
                         "n_csv_rows": sum(len(v["rows"]) for v in vals), "coord_groups": ";".join(sorted(g["groups"])),
                         "has_attributes": int(bool(vals)), "area_m2": round(g["area_m2"], 1),
                         "n_load_variants": len(loads), "load_min": loads[0] if loads else None,
                         "load_max": loads[-1] if loads else None,
                         "load_range": (loads[-1] - loads[0]) if loads else None,
                         "status": "DERIVATION: reprojection of the source polygon (EPSG:4326 → 32640)",
                         "utm_wkb": g["utm_wkb"], "wkb": g["wkb"]})
    gcols = [("geom_id", str), ("wkb_sha256", str), ("source_fids", str), ("n_source_features", int),
             ("n_value_records", int), ("n_csv_rows", int), ("coord_groups", str), ("has_attributes", int),
             ("area_m2", float), ("n_load_variants", int), ("load_min", float), ("load_max", float),
             ("load_range", float), ("status", str)]
    layer("contours", utm, ogr.wkbMultiPolygon, gcols, geo_rows, "utm_wkb",
          "Distinct non-empty source polygons (one per geometry); identity of every source feature in source_fids")
    layer("contours_source_wgs84", wgs, ogr.wkbMultiPolygon, gcols[:4], geo_rows, "wkb",
          "The same polygons, source geometry bytes unchanged (EPSG:4326, lon/lat)")
    vrows = []
    for gid, lks in variants.items():
        g = L["geoms"][int(gid[1:]) - 1]
        lks = sorted(lks, key=lambda lk: (V[int(lk["value_id"][1:]) - 1]["values"][2], lk["value_id"]))
        for n, lk in enumerate(lks, 1):
            v = V[int(lk["value_id"][1:]) - 1]
            vrows.append({"link_id": lk["link_id"], "geom_id": gid, "value_id": lk["value_id"], "variant_no": n,
                          "n_variants": len(lks), "source_fids": ranges(lk["fids"]),
                          "csv_rows": ranges(v["rows"]), **{k: v["values"][i] for i, k in enumerate(FIELDS)},
                          "utm_wkb": g["utm_wkb"]})
    vcols = [("link_id", str), ("geom_id", str), ("value_id", str), ("variant_no", int), ("n_variants", int),
             ("source_fids", str), ("csv_rows", str)] + [(k, float) for k in FIELDS]
    layer("contour_variants", utm, ogr.wkbMultiPolygon, vcols, vrows, "utm_wkb",
          "One feature per (contour, indicator record) link; variants ordered by exel_Load — the order has no physical "
          "meaning; the nature of several records per contour is not established")
    layer("value_records", None, ogr.wkbNone,
          [("value_id", str), ("n_csv_rows", int), ("csv_rows", str), ("n_source_features", int)]
          + [(k, float) for k in FIELDS] + [(k + "_raw", str) for k in FIELDS],
          [{"value_id": v["value_id"], "n_csv_rows": len(v["rows"]), "csv_rows": ranges(v["rows"]),
            "n_source_features": sum(1 for f in L["features"] if f["value_id"] == v["value_id"]),
            **{k: v["values"][i] for i, k in enumerate(FIELDS)}, **{k + "_raw": v["raw"][i] for i, k in enumerate(FIELDS)}}
           for v in V], desc="Distinct indicator records of the CSV (values as REAL and as the original text)")
    layer("csv_rows", None, ogr.wkbNone,
          [("csv_row", int), ("csv_line", int), ("value_id", str), ("cg_id", str)],
          [{"csv_row": r["row"], "csv_line": r["row"] + 1, "value_id": r["value_id"], "cg_id": r["cg_id"]}
           for r in L["rows"]], desc="Every CSV data row (row 1 = file line 2) → its record and coordinate group")
    layer("source_features", None, ogr.wkbNone,
          [("fid_source", int), ("feature_no", int), ("geom_id", str), ("value_id", str), ("cg_id", str),
           ("geometry_state", str), ("attribute_state", str)],
          [{"fid_source": f["fid"], "feature_no": f["fid"] + 1, "geom_id": f["geom_id"], "value_id": f["value_id"],
            "cg_id": f["cg_id"], "geometry_state": "EMPTY" if f["empty"] else "OK",
            "attribute_state": "NONE" if f["value_id"] is None else "OK"} for f in L["features"]],
          desc="Every GeoJSON feature (OGR/QGIS FID from 0; feature_no = FID + 1) → geometry, record, group")
    layer("links", None, ogr.wkbNone,
          [("link_id", str), ("geom_id", str), ("value_id", str), ("n_supporting_features", int),
           ("supporting_fids", str), ("basis", str)],
          [{"link_id": lk["link_id"], "geom_id": lk["geom_id"], "value_id": lk["value_id"],
            "n_supporting_features": len(lk["fids"]), "supporting_fids": ranges(lk["fids"]),
            "basis": "SOURCE_FEATURE_PAIR (GeoJSON feature carries this geometry and this record)"} for lk in L["links"]],
          desc="Contour ↔ indicator record links with the source features that state them")
    cg_rows = []
    for c in L["groups"]:
        p = ogr.Geometry(ogr.wkbPoint)
        p.AddPoint_2D(*c["utm"])
        cg_rows.append({"cg_id": c["cg_id"], "xcoord_raw": c["raw_xy"][0], "ycoord_raw": c["raw_xy"][1],
                        "k_csv_rows": len(c["rows"]), "csv_rows": ranges(c["rows"]), "n_features": len(c["fids"]),
                        "k_squared": len(c["rows"]) ** 2, "n_geoms": len(c["geoms"]),
                        "geom_ids": ";".join(sorted(c["geoms"])), "wkb": bytes(p.ExportToIsoWkb())})
    layer("coord_groups", utm, ogr.wkbPoint,
          [("cg_id", str), ("xcoord_raw", str), ("ycoord_raw", str), ("k_csv_rows", int), ("csv_rows", str),
           ("n_features", int), ("k_squared", int), ("n_geoms", int), ("geom_ids", str)], cg_rows, "wkb",
          "Points xcoord/ycoord of the CSV; k rows of one group joined to k copies of its polygon give k² features")
    layer("overlaps", utm, ogr.wkbMultiPolygon,
          [("overlap_id", str), ("geom_a", str), ("geom_b", str), ("area_m2", float), ("frac_of_smaller", float),
           ("kind", str)],
          [{**o, "wkb": bytes(ogr.ForceToMultiPolygon(ogr.CreateGeometryFromWkb(o["wkb"])).ExportToIsoWkb())} for o in ovl],
          "wkb", "Intersections of distinct contours > 1 m² (nothing repaired)")
    layer("qa_issues", None, ogr.wkbNone, [("issue_id", str), ("kind", str), ("severity", str), ("refs", str),
                                           ("cluster", str), ("description", str)], issues,
          desc="Disputed objects; issues on the same contours share a cluster")
    sh = []
    fwd, _, _, _ = transforms()
    for s in align["shafts"]:
        sh.append(dict(s))
    layer("controls_osm_shafts", None, ogr.wkbNone,
          [("osm_id", str), ("name", str), ("operator", str), ("inside_contour", int),
           ("distance_to_nearest_contour_m", float)], sh, desc="OSM mine shafts (ODbL) used as a plausibility check")
    box = ogr.CreateGeometryFromWkt(f"POLYGON(({aoi[0]} {aoi[1]},{aoi[2]} {aoi[1]},{aoi[2]} {aoi[3]},{aoi[0]} {aoi[3]},"
                                    f"{aoi[0]} {aoi[1]}))")
    layer("aoi", utm, ogr.wkbPolygon, [("name", str)], [{"name": f"contours + {AOI_BUFFER:.0f} m", "wkb":
                                                          bytes(box.ExportToIsoWkb())}], "wkb", "Area of the surface")
    ds = None
    gdal.SetConfigOption("OGR_CURRENT_DATE", None)


# ------------------------------------------------------------------------------------------------------ previews
def previews(out: Path, L: dict, ovl: list[dict], prod: dict, align: dict, osm: dict) -> list[Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from shapely import wkb as swkb
    matplotlib.rcParams.update({"font.size": 8, "svg.hashsalt": "vkm", "path.simplify": False})
    z, nd, gt = read_band(prod["surface"])
    hs, _, _ = read_band(prod["hillshade"])
    zm = np.ma.masked_equal(z, nd)
    ext = (gt[0], gt[0] + gt[1] * z.shape[1], gt[3] + gt[5] * z.shape[0], gt[3])
    polys = {g["geom_id"]: swkb.loads(g["utm_wkb"]) for g in L["geoms"]}
    noattr = {f["geom_id"] for f in L["features"] if f["value_id"] is None and f["geom_id"]}
    fwd, _, _, _ = transforms()
    files = []

    def rings(geom):
        for p in getattr(geom, "geoms", [geom]):
            yield np.asarray(p.exterior.coords)

    def base(ax, title, extent=None):
        ax.imshow(hs, extent=ext, cmap="gray", vmin=0, vmax=255)
        im = ax.imshow(zm, extent=ext, cmap="terrain", alpha=0.45)
        ax.set_aspect("equal")
        if extent:
            ax.set_xlim(extent[0], extent[2])
            ax.set_ylim(extent[1], extent[3])
        ax.set_title(title)
        ax.ticklabel_format(useOffset=False, style="plain")
        ax.set_xlabel("E, м (UTM 40N, EPSG:32640)")
        ax.set_ylabel("N, м")
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        bar = 1000 if (x1 - x0) > 4000 else 200
        ax.plot([x0 + 0.05 * (x1 - x0), x0 + 0.05 * (x1 - x0) + bar], [y0 + 0.05 * (y1 - y0)] * 2, "k-", lw=3)
        ax.text(x0 + 0.05 * (x1 - x0) + bar / 2, y0 + 0.07 * (y1 - y0), f"{bar} м", ha="center")
        ax.annotate("N", xy=(x1 - 0.05 * (x1 - x0), y1 - 0.05 * (y1 - y0)), ha="center", fontsize=11, weight="bold")
        return im

    def foot(fig, extra=""):
        fig.text(0.01, 0.005, f"Высоты: {DEM_LABEL}; {ATTRIBUTION}. Контуры: набор dma-skru1-losev (привязка по OSM, "
                 f"точность UNKNOWN). Реки/стволы: © OpenStreetMap contributors (ODbL). {extra}", fontsize=5.5,
                 wrap=True)

    def save(fig, name):
        p = out / "previews" / name
        fig.savefig(p, dpi=150, metadata={"Software": None}, bbox_inches="tight", pad_inches=0.15)
        plt.close(fig)
        files.append(p)

    rivers = []
    for e in osm["elements"]:
        if e.get("tags", {}).get("waterway") == "river" and e.get("geometry"):
            pts = []
            for p in e["geometry"]:
                g = ogr.Geometry(ogr.wkbPoint)
                g.AddPoint_2D(p["lon"], p["lat"])
                g.Transform(fwd)
                pts.append((g.GetX(), g.GetY()))
            rivers.append(np.asarray(pts))
    # 1. overview
    fig, ax = plt.subplots(figsize=(9, 8))
    im = base(ax, "Контуры ДМА (585 различных геометрий) на поверхности DSM — общий план")
    for gid, p in polys.items():
        for r in rings(p):
            ax.plot(r[:, 0], r[:, 1], color="navy" if gid in noattr else "black", lw=0.4,
                    ls="--" if gid in noattr else "-")
    for o in ovl:
        for r in rings(swkb.loads(o["wkb"])):
            ax.fill(r[:, 0], r[:, 1], color="red", alpha=0.7 if o["kind"] != "SLIVER" else 1, lw=0)
    for r in rivers:
        ax.plot(r[:, 0], r[:, 1], color="tab:blue", lw=0.8)
    for e in osm["elements"]:
        t = e.get("tags", {})
        if t.get("man_made") == "mineshaft" and "lat" in e:
            q = ogr.Geometry(ogr.wkbPoint)
            q.AddPoint_2D(e["lon"], e["lat"])
            q.Transform(fwd)
            skru1 = "СКРУ-1" in t.get("operator", "")
            ax.plot(q.GetX(), q.GetY(), marker="^", color="magenta" if skru1 else "darkorange", ms=7, mec="k")
            ax.annotate(f"{t.get('name', '')} ({t.get('operator', '').split()[0]})", (q.GetX(), q.GetY()),
                        xytext=(4, 4), textcoords="offset points", fontsize=6)
    ax.set_xlim(ext[0], ext[1])
    ax.set_ylim(ext[2], ext[3])
    fig.colorbar(im, ax=ax, shrink=0.6, label="высота DSM, м (EGM2008)")
    foot(fig, "Пунктир — контуры без показателей; красное — перекрытия контуров; синее — реки OSM. Вертикального "
         "преувеличения нет.")
    save(fig, "01_overview_plan.png")
    # 2. overlaps
    major = [o for o in ovl if o["kind"] != "SLIVER"]
    if major:
        ids = {o["geom_a"] for o in major} | {o["geom_b"] for o in major}
        bb = np.array([polys[i].bounds for i in ids])
        pad = 250
        win = (bb[:, 0].min() - pad, bb[:, 1].min() - pad, bb[:, 2].max() + pad, bb[:, 3].max() + pad)
        fig, ax = plt.subplots(figsize=(8, 7))
        base(ax, "Крупные перекрытия контуров (не исправлялись)", win)
        colors = ["tab:orange", "tab:purple", "tab:green", "tab:brown"]
        for i, gid in enumerate(sorted(ids)):
            g = L["geoms"][int(gid[1:]) - 1]
            for k, r in enumerate(rings(polys[gid])):
                ax.plot(r[:, 0], r[:, 1], color=colors[i % 4], lw=1.8 - 0.3 * i,
                        label=(f"{gid}: FID {ranges(g['fids'])} (№ {ranges([f + 1 for f in g['fids']])}); "
                               f"{g['area_m2'] / 1e4:.1f} га") if k == 0 else None)
        for o in major:
            for k, r in enumerate(rings(swkb.loads(o["wkb"]))):
                ax.fill(r[:, 0], r[:, 1], facecolor="none", edgecolor="red", hatch="////", lw=0.8,
                        label=(f"{o['overlap_id']} = {o['geom_a']} ∩ {o['geom_b']}: {o['area_m2']:.0f} м² "
                               f"({100 * o['frac_of_smaller']:.0f} % меньшего)") if k == 0 else None)
        ax.legend(loc="upper left", fontsize=6.5, framealpha=0.85)
        foot(fig, "FID — нумерация OGR/QGIS с 0; № — порядковый номер объекта в файле с 1.")
        save(fig, "02_overlaps.png")
    # 3. load variants
    fig, axs = plt.subplots(2, 2, figsize=(11, 10))
    V = L["values"]
    var = defaultdict(list)
    for lk in L["links"]:
        var[lk["geom_id"]].append(V[int(lk["value_id"][1:]) - 1]["values"][2])
    cm = plt.get_cmap("viridis")
    panels = [("Число разных записей (вариантов) на контур", lambda g: len(var[g]), plt.get_cmap("YlOrRd", 5), (0.5, 5.5)),
              ("exel_Load — минимальный вариант", lambda g: min(var[g]), cm, (0, 1)),
              ("exel_Load — максимальный вариант", lambda g: max(var[g]), cm, (0, 1)),
              ("Размах exel_Load между вариантами", lambda g: max(var[g]) - min(var[g]), plt.get_cmap("magma_r"), (0, 0.5))]
    for ax, (title, fn, cmap, (lo, hi)) in zip(axs.ravel(), panels):
        base(ax, title)
        cols, verts = [], []
        for gid, p in polys.items():
            if gid not in var:
                continue
            for r in rings(p):
                verts.append(r)
                cols.append(fn(gid))
        pc = PolyCollection(verts, array=np.array(cols, dtype=float), cmap=cmap, clim=(lo, hi), edgecolor="k",
                            linewidths=0.15)
        ax.add_collection(pc)
        fig.colorbar(pc, ax=ax, shrink=0.7)
        ax.set_xlim(ext[0], ext[1])
        ax.set_ylim(ext[2], ext[3])
    foot(fig, "Несколько записей на контур: природа (пласт, блок, редакция?) в данных не установлена; варианты не "
         "усреднены. exel_Load нормирован (0…1), исходная единица UNKNOWN.")
    save(fig, "03_load_variants.png")
    # 4. relief 3D — the 2D map (relief colours, hillshade, contour edges) is the texture of the surface
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    from matplotlib import colors as mcolors
    edges = contour_edges(L, gt, z.shape)
    norm = mcolors.Normalize(vmin=float(zm.min()), vmax=float(zm.max()))
    rgb = plt.get_cmap("terrain")(norm(zm.filled(float(zm.min()))))[..., :3]
    rgb = rgb * (0.55 + 0.45 * (hs[..., None] / 255.0))
    rgb[edges == 1] = (0.0, 0.0, 0.0)
    rgb[edges == 2] = (0.1, 0.25, 1.0)
    rgb[edges == 3] = (0.9, 0.0, 0.0)
    ve = 10
    yy, xx = np.mgrid[0:z.shape[0], 0:z.shape[1]]
    X = (ext[0] + (xx + 0.5) * gt[1]) / 1000
    Y = (ext[3] + (yy + 0.5) * gt[5]) / 1000
    Z = zm.filled(np.nan)
    fig = plt.figure(figsize=(11, 8.5))
    ax = fig.add_subplot(111, projection="3d", computed_zorder=False)
    ax.plot_surface(X, Y, Z, facecolors=rgb, rcount=z.shape[0], ccount=z.shape[1], linewidth=0, antialiased=False,
                    shade=False)
    dx, dy = X.max() - X.min(), Y.max() - Y.min()
    ax.set_box_aspect((dx, dy, ve * (np.nanmax(Z) - np.nanmin(Z)) / 1000))
    ax.set_xlabel("E, км (UTM 40N)")
    ax.set_ylabel("N, км")
    ax.set_zlabel("высота, м (EGM2008)")
    ax.set_title(f"Поверхность DSM с контурами ДМА (вертикальное преувеличение ×{ve})")
    ax.view_init(elev=38, azim=-62)
    foot(fig, f"Вертикальное преувеличение ×{ve} (ось высот — в истинных метрах). Сетка 30 м (билинейное "
         "перепроецирование исходных 1″×1,5″, без уплотнения). Чёрное — контуры, синее — контуры без показателей, "
         "красное — перекрытия; контуры нанесены текстурой на поверхность, своей высоты не имеют.")
    save(fig, "04_relief_3d.png")
    # 5. other disputed objects
    special = []
    for c in L["groups"]:
        if len(c["geoms"]) > 1:
            special.append(("Координатная группа → 2 контура: " + c["cg_id"], sorted(c["geoms"]), [c["utm"]]))
    for g in L["geoms"]:
        bad = {cg: d for cg, d in g["centroid_offset_m"].items() if d > 5}
        if bad and len(special) < 6:
            special.append((f"{g['geom_id']}: xcoord/ycoord ≠ центроид", [g["geom_id"]],
                            [L["groups"][int(cg[1:]) - 1]["utm"] for cg in bad]))
    if special:
        n = len(special)
        fig, axs = plt.subplots(1, n, figsize=(5 * n, 5), squeeze=False)
        for ax, (title, gids, pts) in zip(axs[0], special):
            bb = np.array([polys[i].bounds for i in gids] + [[p[0], p[1], p[0], p[1]] for p in pts])
            pad = 200
            base(ax, title, (bb[:, 0].min() - pad, bb[:, 1].min() - pad, bb[:, 2].max() + pad, bb[:, 3].max() + pad))
            for gid in gids:
                g = L["geoms"][int(gid[1:]) - 1]
                for k, r in enumerate(rings(polys[gid])):
                    ax.plot(r[:, 0], r[:, 1], lw=1.6, label=(f"{gid}: FID {ranges(g['fids'])}; группы "
                                                             f"{', '.join(sorted(g['groups']))}") if k == 0 else None)
                c = polys[gid].centroid
                ax.plot(c.x, c.y, "k+", ms=10)
            for p in pts:
                ax.plot(p[0], p[1], "rx", ms=10, mew=2)
            ax.legend(loc="upper left", fontsize=6, framealpha=0.85)
        foot(fig, "+ — центроид контура; × — точка xcoord/ycoord записи.")
        save(fig, "05_disputed_objects.png")
    return files


# --------------------------------------------------------------------------------------------------- QGIS project
def qgis_project(out: Path, L: dict) -> dict:
    from qgis.core import (QgsApplication, QgsProject, QgsRasterLayer, QgsVectorLayer, QgsCoordinateReferenceSystem,
                           QgsSingleBandPseudoColorRenderer, QgsColorRampShader, QgsRasterShader, QgsStyle,
                           QgsGraduatedSymbolRenderer, QgsRendererRange, QgsFillSymbol, QgsRasterDemTerrainProvider,
                           QgsSingleSymbolRenderer, QgsLayerTreeGroup)
    from qgis.PyQt.QtGui import QColor
    app = QgsApplication.instance() or QgsApplication([], False)
    if not QgsApplication.instance().platform() or True:
        app.initQgis()
    project = QgsProject.instance()
    project.clear()
    qgz = out / "dma_skru1_surface.qgz"
    project.setFileName(str(qgz))
    project.setCrs(QgsCoordinateReferenceSystem(f"EPSG:{WORK_EPSG}"))
    project.setTitle("СКРУ-1: контуры ДМА на поверхности Copernicus GLO-30 (DSM)")
    project.writeEntry("Paths", "/Absolute", False)
    gpkg = out / "data" / "dma_linked.gpkg"
    root = project.layerTreeRoot()
    added = {}

    def raster(name, path, group):
        lyr = QgsRasterLayer(str(path), name)
        if not lyr.isValid():
            raise RuntimeError("invalid raster " + name)
        project.addMapLayer(lyr, False)
        group.addLayer(lyr)
        added[name] = lyr
        return lyr

    def vector(name, table, group, sql=None):
        lyr = QgsVectorLayer(f"{gpkg}|layername={table}", name, "ogr")
        if not lyr.isValid():
            raise RuntimeError("invalid vector " + name)
        if sql:
            lyr.setSubsetString(sql)
        project.addMapLayer(lyr, False)
        group.addLayer(lyr)
        added[name] = lyr
        return lyr

    g_qa = root.addGroup("Проверки и спорные объекты")
    g_theme = root.addGroup("Показатели на контурах (тематические слои, один видимый)")
    g_theme.setIsMutuallyExclusive(True)
    g_load = root.addGroup("Нагрузка exel_Load — отдельные варианты (природа нескольких записей не установлена)")
    g_load.setIsMutuallyExclusive(True)
    g_base = root.addGroup("Контуры")
    g_dem = root.addGroup("Рельеф: " + DEM_LABEL)
    vector("Перекрытия контуров", "overlaps", g_qa).renderer().symbol().setColor(QColor(220, 0, 0, 200))
    vector("Точки xcoord/ycoord (координатные группы)", "coord_groups", g_qa)
    added["Точки xcoord/ycoord (координатные группы)"].setFlags(added["Точки xcoord/ycoord (координатные группы)"].flags())
    root.findLayer(added["Точки xcoord/ycoord (координатные группы)"].id()).setItemVisibilityChecked(False)
    for t in ("qa_issues", "links", "value_records", "csv_rows", "source_features", "controls_osm_shafts"):
        vector(t, t, g_qa)
    ramp = QgsStyle.defaultStyle().colorRamp("Viridis")

    def graduated(lyr, field, lo=0.0, hi=1.0, n=5):
        rngs = []
        for i in range(n):
            a, b = lo + (hi - lo) * i / n, lo + (hi - lo) * (i + 1) / n
            sym = QgsFillSymbol.createSimple({"color": ramp.color(i / (n - 1)).name(), "outline_color": "#202020",
                                              "outline_width": "0.1"})
            rngs.append(QgsRendererRange(a, b, sym, f"{a:.2f}–{b:.2f}"))
        lyr.setRenderer(QgsGraduatedSymbolRenderer(field, rngs))

    first = True
    for t in THEMES:
        lyr = vector(f"{t} (вариант 1 из n; остальные — фильтр variant_no)", "contour_variants", g_theme, "variant_no = 1")
        graduated(lyr, t)
        root.findLayer(lyr.id()).setItemVisibilityChecked(first)
        first = False
    nmax = max(len(g["values"]) for g in L["geoms"])
    for k in range(1, nmax + 1):
        lyr = vector(f"exel_Load, вариант {k} (по возрастанию нагрузки)", "contour_variants", g_load, f"variant_no = {k}")
        graduated(lyr, "exel_Load")
        root.findLayer(lyr.id()).setItemVisibilityChecked(False)
    lyr = vector("Число записей на контур", "contours", g_load)
    sym = []
    for a, b, col in ((0, 0.5, "#dddddd"), (0.5, 1.5, "#fee391"), (1.5, 2.5, "#fe9929"), (2.5, 10, "#cc4c02")):
        sym.append(QgsRendererRange(a, b, QgsFillSymbol.createSimple({"color": col, "outline_color": "#303030",
                                                                       "outline_width": "0.1"}), f"{a + 0.5:.0f}" if b < 10 else "3+"))
    lyr.setRenderer(QgsGraduatedSymbolRenderer("n_value_records", sym))
    root.findLayer(lyr.id()).setItemVisibilityChecked(False)
    c = vector("Контуры (585 различных геометрий)", "contours", g_base)
    c.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": "#000000",
                                                                       "outline_width": "0.2"})))
    vector("Контуры без показателей", "contours", g_base, "has_attributes = 0").setRenderer(
        QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": "#1f3cff",
                                                             "outline_style": "dash", "outline_width": "0.3"})))
    vector("Граница поверхности (AOI)", "aoi", g_base).setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
        {"color": "0,0,0,0", "outline_color": "#808080", "outline_width": "0.4"})))
    hs = raster("Отмывка рельефа", out / "data" / "hillshade_utm40n_30m.tif", g_dem)
    dem = raster("DSM GLO-30, высота, м (EGM2008)", out / "data" / "dem_glo30_aoi_utm40n_30m.tif", g_dem)
    stats = dem.dataProvider().bandStatistics(1)
    shader = QgsColorRampShader(stats.minimumValue, stats.maximumValue, QgsStyle.defaultStyle().colorRamp("Spectral"))
    shader.setColorRampType(QgsColorRampShader.Interpolated)
    shader.classifyColorRamp(10)
    rs = QgsRasterShader()
    rs.setRasterShaderFunction(shader)
    dem.setRenderer(QgsSingleBandPseudoColorRenderer(dem.dataProvider(), 1, rs))
    dem.renderer().setOpacity(0.55)
    raster("Точность высоты HEM, м", out / "data" / "quality_hem_utm40n_30m.tif", g_dem)
    root.findLayer(added["Точность высоты HEM, м"].id()).setItemVisibilityChecked(False)
    raster("Маска заполнения FLM", out / "data" / "quality_flm_utm40n_30m.tif", g_dem)
    root.findLayer(added["Маска заполнения FLM"].id()).setItemVisibilityChecked(False)
    # terrain for 3D views and elevation profiles: the DSM, true heights (exaggeration is a 3D-view setting)
    terrain = QgsRasterDemTerrainProvider()
    terrain.setLayer(dem)
    terrain.setScale(1.0)
    terrain.setOffset(0.0)
    project.elevationProperties().setTerrainProvider(terrain)
    dem.elevationProperties().setEnabled(True)
    # QGIS 4.2 exposes no API to store a 3D map view in the project; every new 3D view (View → 3D Map View) and the
    # elevation profile take the project terrain, i.e. this DSM. The 2D layers shown in it drape on the terrain.
    three_d = "PROJECT_TERRAIN = DSM (open View → 3D Map View); no stored 3D view (no API in QGIS 4.2)"
    project.setPresetHomePath("")
    if not project.write():
        raise RuntimeError("QGIS project was not written")
    layers = [(l.name(), l.source()) for l in project.mapLayers().values()]
    project.clear()
    return {"file": qgz.name, "layers": len(layers), "three_d_view": three_d}


def verify_project_fresh(out: Path) -> dict:
    """The project opened by a fresh QGIS process (as a user would open it), not by the process that wrote it."""
    import subprocess
    r = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--verify-project", str(out)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                       env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    line = next((x for x in reversed(r.stdout.splitlines()) if x.startswith("{")), None)
    if r.returncode != 0 or line is None:
        return {"status": "FAIL", "returncode": r.returncode, "stderr": r.stderr[-1500:]}
    return json.loads(line)


def verify_project(out: Path) -> dict:
    """Open the written project in a fresh QgsProject: every layer valid, every data source relative."""
    from qgis.core import QgsProject
    qgz = out / "dma_skru1_surface.qgz"
    with zipfile.ZipFile(qgz) as z:
        name = next(n for n in z.namelist() if n.endswith(".qgs"))
        xml = z.read(name).decode("utf-8")
    import re
    sources = re.findall(r"<datasource>([^<]*)</datasource>", xml)
    absolute = [s for s in sources if re.match(r"^([A-Za-z]:|/|\\\\)", s)]
    p = QgsProject()
    ok = p.read(str(qgz))
    invalid = [l.name() for l in p.mapLayers().values() if not l.isValid()]
    res = {"read": bool(ok), "layers": len(p.mapLayers()), "invalid_layers": invalid,
           "datasources": len(sources), "absolute_datasources": absolute,
           "relative_examples": sorted(set(s.split("|")[0] for s in sources))[:6],
           "terrain_layer": (p.elevationProperties().terrainProvider().layer().name()
                             if p.elevationProperties().terrainProvider().layer() else None)}
    p.clear()
    res["status"] = "PASS" if ok and not invalid and not absolute and res["terrain_layer"] else "FAIL"
    return res


# ----------------------------------------------------------------------------------------------- content digests
def content_digest(p: Path) -> str:
    """Logical identity independent of container bytes (GPKG layers, raster pixels, PNG pixels)."""
    s = p.suffix.lower()
    h = hashlib.sha256()
    if s == ".gpkg":
        ds = gdal.OpenEx(str(p), gdal.OF_VECTOR | gdal.OF_READONLY)
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayer(i)
            d = lyr.GetLayerDefn()
            h.update(lyr.GetName().encode())
            h.update(json.dumps([(d.GetFieldDefn(k).GetName(), d.GetFieldDefn(k).GetTypeName())
                                 for k in range(d.GetFieldCount())]).encode())
            for f in lyr:
                g = f.GetGeometryRef()
                h.update(json.dumps([f.GetFID(), [f.GetField(k) for k in range(d.GetFieldCount())]],
                                    ensure_ascii=False, default=str).encode())
                h.update(bytes(g.ExportToIsoWkb()) if g else b"-")
        ds = None
    elif s in (".tif", ".tiff"):
        ds = gdal.Open(str(p))
        h.update(json.dumps([ds.GetGeoTransform(), ds.GetSpatialRef().ExportToWkt() if ds.GetSpatialRef() else None,
                             ds.GetRasterBand(1).GetNoDataValue()]).encode())
        h.update(ds.GetRasterBand(1).ReadAsArray().tobytes())
        ds = None
    elif s == ".qgz":
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(p) as zf:
            root = ET.fromstring(zf.read(next(n for n in zf.namelist() if n.endswith(".qgs"))))
        rows = sorted((ml.findtext("layername") or "", ml.findtext("datasource") or "", ml.findtext("provider") or "",
                       (ml.find("renderer-v2").get("type") if ml.find("renderer-v2") is not None else ""))
                      for ml in root.iter("maplayer"))
        h.update(json.dumps([root.findtext("title"), rows], ensure_ascii=False).encode())
    elif s == ".png":
        from PIL import Image
        h.update(np.asarray(Image.open(p).convert("RGBA")).tobytes())
    else:
        h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------------------- main
def build(res: Path, out: Path) -> dict:
    inp = load_inputs(res)
    L = link(inp["features"], inp["rows"])
    met = metric(L)
    centroid_offsets(L)
    ovl = overlaps(L)
    L["overlaps"] = ovl
    issues = qa_issues(L, ovl)
    aoi = aoi_bounds(L)
    (out / "data").mkdir(parents=True)
    (out / "previews").mkdir()
    prod = dem_products(inp, aoi, out)
    checks = dem_checks(L, prod)
    align = alignment(L, prod, inp["osm"])
    write_gpkg(out / "data" / "dma_linked.gpkg", L, ovl, issues, align, aoi)
    pngs = previews(out, L, ovl, prod, align, inp["osm"])
    proj = qgis_project(out, L)
    ver = verify_project_fresh(out)
    counts = {"csv_rows": len(L["rows"]), "value_records": len(L["values"]), "coord_groups": len(L["groups"]),
              "source_features": len(L["features"]), "empty_geometry_features": sum(f["empty"] for f in L["features"]),
              "features_without_attributes": sum(f["value_id"] is None for f in L["features"]),
              "distinct_geometries": len(L["geoms"]), "links": len(L["links"]),
              "contours_without_attributes": len({f["geom_id"] for f in L["features"]
                                                  if f["value_id"] is None and f["geom_id"]}),
              "contours_with_several_loads": sum(1 for g in L["geoms"]
                                                 if len({L["values"][int(v[1:]) - 1]["values"][2] for v in g["values"]}) > 1),
              "max_records_per_contour": max(len(g["values"]) for g in L["geoms"]),
              "k_squared_holds": sum(1 for c in L["groups"] if len(c["fids"]) == len(c["rows"]) ** 2),
              "overlaps": dict(Counter(o["kind"] for o in ovl)), "qa_issues": len(issues)}
    outputs = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "receipt.json")
    receipt = {"rule": RULE, "dataset_version": inp["manifest"].digest,
               "inputs": {"originals": {f.path: f.sha256 for f in inp["manifest"].files},
                          "conversion_receipts": {k: v["receipt_sha256"] for k, v in inp["proofs"].items()},
                          "dem": {f["path"]: f["sha256"] for f in inp["dem_intake"]["files"] if f["path"].endswith((".tif", ".kml"))},
                          "osm": inp["osm_intake"]["file"]["sha256"], "osm_snapshot": inp["osm_intake"]["timestamp_osm_base"]},
               "code_sha256": {f: sha((ROOT / f).read_bytes().replace(b"\r\n", b"\n")) for f in CODE_FILES},
               "runtime": {"gdal": gdal.VersionInfo("RELEASE_NAME"), "python": sys.version.split()[0]},
               "parameters": {"working_crs": f"EPSG:{WORK_EPSG}+{VERT_EPSG} (WGS 84 / UTM 40N + EGM2008 height)",
                              "pixel_m": PIXEL, "resampling": "bilinear (surface), nearest (masks), bilinear (HEM)",
                              "aoi_buffer_m": AOI_BUFFER, "aoi_utm": list(aoi), "native_clip_wgs84": prod["projwin_wgs84"],
                              "vertical_datum": "EGM2008 as delivered; Baltic 1977 transformation UNKNOWN — not applied"},
               "counts": counts, "transform_roundtrip_max_m": met["roundtrip_max_m"], "dem_checks": checks,
               "alignment": align, "qgis_project": proj, "qgis_verification": ver,
               "status": {"surface": "BUILT_ON_REAL_ELEVATIONS (DSM, not bare earth)", "geometry_admission": "DERIVATION",
                          "scientific_admission": "NOT_CHECKED"},
               "outputs": {p.relative_to(out).as_posix(): {"sha256": sha_file(p), "content_sha256": content_digest(p)}
                           for p in outputs}}
    (out / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1, default=str) + "\n",
                                      encoding="utf-8", newline="\n")
    return receipt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, help="new directory (never overwritten)")
    ap.add_argument("--verify-project", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--verify", type=Path, help="existing package: rebuild into --out and compare content digests")
    a = ap.parse_args()
    if a.verify_project:
        from qgis.core import QgsApplication
        app = QgsApplication([], False)
        app.initQgis()
        print(json.dumps(verify_project(a.verify_project), ensure_ascii=False, default=str))
        return 0
    if not a.out:
        ap.error("--out is required")
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    if a.out.exists():
        print(f"{a.out} exists: choose a new directory", file=sys.stderr)
        return 2
    rec = build(Path(res), a.out)
    if a.verify:
        old = json.loads((a.verify / "receipt.json").read_text(encoding="utf-8"))["outputs"]
        diff = sorted(k for k in set(old) | set(rec["outputs"])
                      if old.get(k, {}).get("content_sha256") != rec["outputs"].get(k, {}).get("content_sha256"))
        same_bytes = sorted(k for k in old if old[k]["sha256"] == rec["outputs"].get(k, {}).get("sha256"))
        print(json.dumps({"verify": "PASS" if not diff else "FAIL", "content_differs": diff,
                          "byte_identical": len(same_bytes), "files": len(old)}, ensure_ascii=False))
        return 1 if diff else 0
    print(json.dumps({k: rec[k] for k in ("counts", "transform_roundtrip_max_m", "qgis_verification")},
                     ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        try:
            from qgis.core import QgsApplication
            if QgsApplication.instance():
                QgsApplication.exitQgis()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
