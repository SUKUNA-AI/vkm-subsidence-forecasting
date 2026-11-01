"""Create a diagnostic EGM2008 raster from EGM96 heights using explicit local grids.

This is a geoid-model operation at unchanged longitude/latitude, not a survey
datum, horizontal-frame or epoch transformation. Source rasters are immutable.
Run with an existing GDAL/pyproj Python; no network grids are fetched implicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def convert(source, output, egm96_grid, egm2008_grid, *, declared_source_datum,
            declared_source_unit, declared_source_nodata, horizontal_frame_assumption, block_rows=128):
    from osgeo import gdal, osr
    import pyproj
    from pyproj import Transformer, enums, network

    gdal.UseExceptions()
    paths = [Path(p).resolve() for p in (source, egm96_grid, egm2008_grid)]
    output = Path(output).resolve()
    if not all(p.is_file() for p in paths):
        raise ValueError("source and both explicitly supplied geoid grids must exist")
    if output.exists() or output in paths:
        raise ValueError("a new output path is required; inputs/previous outputs are never overwritten")
    if (declared_source_datum != "EGM96" or declared_source_unit != "m"
            or not np.isfinite(declared_source_nodata) or not horizontal_frame_assumption.strip()
            or not isinstance(block_rows, int) or block_rows < 1):
        raise ValueError("EGM96, metres, finite product NoData declaration, horizontal-frame assumption and positive block size required")
    network.set_network_enabled(False)
    src = gdal.Open(str(paths[0]), gdal.GA_ReadOnly)
    if src.RasterCount != 1:
        raise ValueError("a single-band height raster is required")
    gt = src.GetGeoTransform()
    srs = osr.SpatialReference(wkt=src.GetProjection())
    reference = osr.SpatialReference(); reference.ImportFromEPSG(4326)
    if not srs.IsGeographic() or not srs.IsSameGeogCS(reference):
        raise ValueError("input must declare the WGS84 geographic horizontal CRS")
    declared_crs = pyproj.CRS.from_wkt(src.GetProjection())
    if declared_crs.is_compound:
        vertical = [part for part in declared_crs.sub_crs_list if part.is_vertical]
        if len(vertical) != 1 or vertical[0].to_epsg() != 5773:
            raise ValueError("embedded source vertical CRS contradicts the declared EGM96 heights")
    elif len(declared_crs.axis_info) != 2:
        raise ValueError("ellipsoidal/3D source heights are not EGM96 heights")
    if gt[2] != 0 or gt[4] != 0 or gt[1] <= 0 or gt[5] >= 0:
        raise ValueError("north-up geographic pixels are required")
    band = src.GetRasterBand(1)
    native_unit = band.GetUnitType().strip().lower()
    if native_unit and native_unit not in ("m", "metre", "meter", "metres", "meters"):
        raise ValueError("source band unit contradicts the declared metre heights")
    scale, offset = band.GetScale(), band.GetOffset()
    if scale not in (None, 1.) or offset not in (None, 0.):
        raise ValueError("scaled heights need a separately reviewed scale operation")
    native_nodata = band.GetNoDataValue()
    if native_nodata is not None and native_nodata != declared_source_nodata:
        raise ValueError("source NoData tag contradicts the declared product sentinel")
    nodata = declared_source_nodata
    source_mask_flags = band.GetMaskFlags()
    source_mask = band.GetMaskBand()
    # Check grid semantics as well as filenames: reversing the two arguments must fail.
    for grid_path, target_epsg in zip(paths[1:], ("5773", "3855")):
        grid = gdal.Open(str(grid_path), gdal.GA_ReadOnly)
        grid_band = grid.GetRasterBand(1)
        if (grid.RasterCount != 1
                or grid.GetMetadataItem("TYPE") != "VERTICAL_OFFSET_GEOGRAPHIC_TO_VERTICAL"
                or grid.GetMetadataItem("target_crs_epsg_code") != target_epsg
                or grid_band.GetUnitType().strip().lower() not in ("m", "metre", "meter")
                or grid_band.GetScale() not in (None, 1.) or grid_band.GetOffset() not in (None, 0.)):
            raise ValueError("geoid grid type, target CRS, units or scale do not match its declared role")
        grid_band = None; grid = None
    pipeline = ("+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
                f"+step +inv +proj=vgridshift +grids={paths[1].as_posix()} "
                f"+step +proj=vgridshift +grids={paths[2].as_posix()} "
                "+step +proj=unitconvert +xy_in=rad +xy_out=deg")
    transform = Transformer.from_pipeline(pipeline)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(output.stem + ".incomplete" + output.suffix)
    if pending.exists():
        raise ValueError("an incomplete previous output exists; inspect its outcome before retrying")
    dst = gdal.GetDriverByName("GTiff").Create(
        str(pending), src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32,
        options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3"])
    dst.SetGeoTransform(gt)
    dst.SetProjection(pyproj.CRS("EPSG:4326+3855").to_wkt())
    dst.SetMetadata({"SOURCE_VERTICAL_MODEL": "EGM96", "TARGET_VERTICAL_MODEL": "EGM2008",
                     "HEIGHT_OPERATION": "H2008 = H96 + N96 - N2008",
                     "HORIZONTAL_FRAME_ASSUMPTION": horizontal_frame_assumption,
                     "EPOCH_CORRECTION": "NOT_APPLIED", "SCIENTIFIC_ADMISSION": "NOT_GRANTED",
                     "INTENDED_USE": "DIAGNOSTIC_GEOID_MODEL_OPERATION_ONLY"})
    outband = dst.GetRasterBand(1); outband.SetNoDataValue(-9999.); outband.SetUnitType("m")
    count = 0; total = 0.; extrema = [float("inf"), float("-inf")]
    max_roundtrip = 0.; started = time.monotonic()
    for row in range(0, src.RasterYSize, block_rows):
        rows = min(block_rows, src.RasterYSize - row)
        values = band.ReadAsArray(0, row, src.RasterXSize, rows).astype(float)
        valid = np.isfinite(values) & (source_mask.ReadAsArray(0, row, src.RasterXSize, rows) != 0)
        if nodata is not None:
            valid &= values != nodata
        lat = gt[3] + (row + np.arange(rows) + .5) * gt[5]
        lon = gt[0] + (np.arange(src.RasterXSize) + .5) * gt[1]
        longitude, latitude = np.meshgrid(lon, lat)
        block = np.full(values.shape, -9999., dtype=np.float32)
        if valid.any():
            x, y, z = longitude[valid], latitude[valid], values[valid]
            _, _, converted = transform.transform(x, y, z, errcheck=True)
            converted = np.asarray(converted)
            if not np.isfinite(converted).all():
                raise ValueError("nonfinite transformed height; incomplete output retained")
            shift = converted - z
            count += len(z); total += float(shift.sum())
            extrema = [min(extrema[0], float(shift.min())), max(extrema[1], float(shift.max()))]
            # Check reversibility on a bounded subset; independent grid reads check the sign separately.
            take = np.linspace(0, len(z) - 1, min(17, len(z)), dtype=int)
            _, _, recovered = transform.transform(x[take], y[take], converted[take],
                                                   direction=enums.TransformDirection.INVERSE, errcheck=True)
            max_roundtrip = max(max_roundtrip, float(np.max(np.abs(recovered - z[take]))))
            block[valid] = converted.astype(np.float32)
        outband.WriteArray(block, 0, row)
    dst.FlushCache(); outband = None; dst = None; source_mask = None; band = None; src = None
    if count == 0 or max_roundtrip > 1e-8:
        raise ValueError("empty or noninvertible height operation; incomplete output retained")
    pending.rename(output)
    # A receipt is intentionally descriptive: geoid accuracy and physical epoch remain UNKNOWN.
    receipt = {"schema": "vkm.egm_height_model_operation/1", "status": "COMPLETED_DIAGNOSTIC_ONLY",
               "source_datum": "EGM96", "target_datum": "EGM2008", "formula": "H2008 = H96 + N96 - N2008",
               "declared_source_unit": declared_source_unit, "native_source_unit": native_unit,
               "declared_source_nodata": declared_source_nodata, "native_source_nodata": native_nodata,
               "source_mask_flags": source_mask_flags,
               "horizontal_frame_assumption": horizontal_frame_assumption,
               "epoch_correction": "NOT_APPLIED", "historic_datum_128_74": "UNRESOLVED",
               "geoid_operation_accuracy_m": "UNKNOWN", "scientific_admission": "NOT_GRANTED",
               "valid_pixels": count, "shift_m": {"min": extrema[0], "max": extrema[1], "mean": total / count},
               "max_inverse_error_m": max_roundtrip, "output_dtype": "Float32", "block_rows": block_rows,
               "runtime_s": time.monotonic() - started,
               "versions": {"gdal": gdal.VersionInfo(), "pyproj": pyproj.__version__, "proj": pyproj.proj_version_str},
               "inputs": [{"name": p.name, "sha256": sha256(p)} for p in paths],
               "output": {"name": output.name, "sha256": sha256(output)},
               "code_sha256": sha256(__file__),
               "pipeline": pipeline.replace(paths[1].as_posix(), "<EGM96_GRID>").replace(paths[2].as_posix(), "<EGM2008_GRID>")}
    output.with_suffix(output.suffix + ".receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("source", "output", "egm96-grid", "egm2008-grid"):
        parser.add_argument("--" + key, required=True, type=Path)
    parser.add_argument("--source-datum", required=True, choices=["EGM96"])
    parser.add_argument("--source-unit", required=True, choices=["m"])
    parser.add_argument("--source-nodata", required=True, type=float)
    parser.add_argument("--horizontal-frame-assumption", required=True)
    parser.add_argument("--block-rows", type=int, default=128)
    args = parser.parse_args()
    receipt = convert(args.source, args.output, args.egm96_grid, args.egm2008_grid,
                      declared_source_datum=args.source_datum,
                      declared_source_unit=args.source_unit, declared_source_nodata=args.source_nodata,
                      horizontal_frame_assumption=args.horizontal_frame_assumption, block_rows=args.block_rows)
    print(json.dumps({key: receipt[key] for key in ("status", "valid_pixels", "shift_m", "max_inverse_error_m")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
