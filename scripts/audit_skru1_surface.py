"""Audit existing DSM/ATL08 inputs without inventing a vertical tie or QC pass.

Run with the existing QGIS Python (GDAL, pyproj, numpy). All inputs are explicit;
the audit reads no historical work path implicitly and never downloads data.
Scientific accuracy metrics are separate from raster/mesh representation checks.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import platform


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def stats(values):
    """Empty samples remain unmeasured; no zero-error substitution."""
    values = [float(v) for v in values]
    if any(not math.isfinite(v) for v in values):
        raise ValueError("nonfinite metric input")
    n = len(values)
    return {"n": n, "bias_m": sum(values) / n if n else None,
            "mae_m": sum(abs(v) for v in values) / n if n else None,
            "rmse_m": math.sqrt(sum(v*v for v in values) / n) if n else None,
            "min_m": min(values) if n else None, "max_m": max(values) if n else None}


def exclusion_reasons(record):
    """Use-time gate: explicit evidence of each condition is required."""
    checks = {"independent_of_fit": "FIT_OR_INDEPENDENCE_UNKNOWN",
              "quality_pass": "QUALITY_UNKNOWN_OR_FAILED",
              "same_vertical_frame": "VERTICAL_FRAME_NOT_ESTABLISHED",
              "comparable_surface": "SURFACE_OPERATOR_NOT_ESTABLISHED",
              "epoch_comparable": "EPOCH_COMPARABILITY_NOT_ESTABLISHED",
              "product_version_identified": "PRODUCT_VERSION_UNKNOWN"}
    return [reason for key, reason in checks.items() if record.get(key) is not True]


def validation_metrics(records):
    accepted = [r for r in records if not exclusion_reasons(r)]
    errors = [r["prediction_m"] - r["reference_m"] for r in accepted]
    return {**stats(errors), "candidate_count": len(records),
            "excluded_count": len(records)-len(accepted),
            "status": "MEASURED_FOR_DECLARED_SCOPE" if accepted else "NOT_ESTABLISHED",
            "error_definition": "DSM minus independent comparable reference",
            "exclusion_counts": dict(Counter(reason for r in records for reason in exclusion_reasons(r)))}


def build(args):
    import numpy as np
    import pyproj
    from osgeo import gdal
    from pyproj.transformer import TransformerGroup
    gdal.UseExceptions()
    root = args.root.resolve()
    resources = args.resources_root.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    inputs = {}

    def logical(path):
        path = Path(path).resolve()
        if path.is_relative_to(resources):
            return "PRIVATE/" + path.relative_to(resources).as_posix()
        if path.is_relative_to(root):
            return "PUBLIC/" + path.relative_to(root).as_posix()
        raise ValueError("inputs must be inside declared PUBLIC/PRIVATE roots")

    def pin(path):
        path = Path(path).resolve()
        with path.open("rb") as f:
            if f.read(100).startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise ValueError("LFS pointer is not content: " + logical(path))
        inputs[logical(path)] = {"sha256": sha(path), "bytes": path.stat().st_size}
        return path

    def load(path):
        return read_json(pin(path))

    def raster(path):
        path = pin(path)
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        b = ds.GetRasterBand(1)
        arr = b.ReadAsArray()
        nodata = b.GetNoDataValue()
        valid = np.isfinite(arr)
        if nodata is not None:
            valid &= arr != nodata
        meta = {"path": logical(path), "shape_rows_cols": list(arr.shape),
                "geotransform": list(ds.GetGeoTransform()), "projection_wkt": ds.GetProjection(),
                "unit": b.GetUnitType(), "nodata": nodata, "valid_cells": int(valid.sum()),
                "invalid_cells": int((~valid).sum()), "metadata": ds.GetMetadata()}
        return ds, arr, valid, meta

    intake = load(args.intake)
    raw_layers = {}
    for f in intake["files"]:
        p = pin(args.intake.parent / f["path"])
        if sha(p) != f["sha256"]:
            raise ValueError("raw intake hash mismatch: " + logical(p))
        if p.suffix.lower() == ".tif":
            _, _, _, info = raster(p)
            raw_layers[p.name] = info

    ds, z, valid, raster_info = raster(args.raster)
    if not valid.all():
        raise ValueError("this audit requires complete source raster; NoData remains unfilled")
    gt = ds.GetGeoTransform()
    if gt[2] != 0 or gt[4] != 0:
        raise ValueError("rotated raster unsupported")
    masks, mask_info = {}, {}
    for code in ("wbm", "edm", "flm", "hem"):
        qds, a, v, meta = raster(args.quality_dir / ("quality_"+code+"_utm40n_30m.tif"))
        horizontal = ds.GetSpatialRef().Clone()
        quality_horizontal = qds.GetSpatialRef().Clone()
        horizontal.StripVertical()
        quality_horizontal.StripVertical()
        if a.shape != z.shape or qds.GetGeoTransform() != ds.GetGeoTransform() or not horizontal.IsSame(quality_horizontal):
            raise ValueError("quality grid not aligned with DSM: " + code)
        masks[code] = (a, v)
        if code == "hem":
            meta["source_random_sigma_m"] = {"n": int(v.sum()), "min": float(a[v].min()),
                "max": float(a[v].max()), "mean": float(a[v].mean()), "p95": float(np.percentile(a[v], 95))}
            meta["meaning"] = "Bilinearly resampled source random-height sigma; not propagated total uncertainty"
        else:
            ids, ns = np.unique(a, return_counts=True)
            meta["code_counts"] = {str(int(i)): int(n) for i, n in zip(ids, ns)}
        mask_info[code] = meta

    mesh = load(args.source_mesh)
    smooth = load(args.display_mesh)
    xyz = np.asarray(mesh["points"], float)
    display_xyz = np.asarray(smooth["points"], float)
    if xyz.shape != display_xyz.shape or not np.array_equal(xyz[:, :2], display_xyz[:, :2]):
        raise ValueError("display/source mesh point correspondence changed")
    if not np.array_equal(xyz[:, 2], z.reshape(-1)):
        raise ValueError("source TIN changed the raster centre elevations")
    representation = {"source_mesh_exact_raster_z": True, "vertices": len(xyz),
        "triangles": len(mesh["triangles"]), "display_minus_source": stats(display_xyz[:, 2]-xyz[:, 2]),
        "role": "representation displacement, not independent accuracy",
        "display_is_dtm": False, "source_is_dtm": False}
    dzdy, dzdx = np.gradient(z.astype(float), abs(gt[5]), abs(gt[1]))
    slope = np.hypot(dzdx, dzdy)
    map_rms = float(mesh["transform"]["steps_preserved"][1]["fit"]["fit"]["rms_m"])
    uncertainty = {"total_vertical_uncertainty_m": None, "status": "UNKNOWN",
        "slope_m_per_m_percentiles": {str(p):float(np.percentile(slope,p)) for p in (50,95,99)},
        "registration_map_in_fit_rms_m": map_rms,
        "registration_sensitivity_m": {str(p):float(np.percentile(slope*map_rms,p)) for p in (50,95,99)},
        "sensitivity_role": "First-order |gradient DSM|*map in-fit RMS from pinned source mesh transform; not independent XY sigma",
        "combination": "No RSS applied: correlations, vegetation/building bias, epoch and datum errors unknown"}

    sat = load(args.satellite)
    sat_raw_dir = args.satellite.parent
    source_documents = {}
    for name in sorted({r["source_json"] for r in sat["records"]}):
        source_documents[name] = load(sat_raw_dir/name)
        pin(sat_raw_dir / name.replace(".json", ".body"))
    records=[]
    for r in sat["records"]:
        raw = source_documents[r["source_json"]]
        beam = next(b for b in raw["series"] if b["beam"] == r["beam"])
        row = beam["lat_lon_elev_canopy"][r["raw_tuple_index_zero_based"]]
        if [r["latitude_deg"], r["longitude_deg"], r["elevation_ellipsoidal_m"]] != row[:3]:
            raise ValueError("normalized point no longer matches raw API response")
        if sha(sat_raw_dir/r["source_json"].replace(".json", ".body")) != r["source_body_sha256"]:
            raise ValueError("satellite raw HTTP body hash mismatch")
        x,y=r["easting_utm40n_m"],r["northing_utm40n_m"]
        col,row_i=int(math.floor((x-gt[0])/gt[1])),int(math.floor((y-gt[3])/gt[5]))
        inside=0<=row_i<z.shape[0] and 0<=col<z.shape[1]
        rec={"point_id":r["point_id"],"date":r["date"],"beam":r["beam"],"rgt":r["rgt"],
             "easting_m":x,"northing_m":y,"elevation_ellipsoidal_m":r["elevation_ellipsoidal_m"],
             "canopy_height_m":r["canopy_height_m"],"inside_raster":inside,
             "dsm_nearest_cell_EGM2008_m":float(z[row_i,col]) if inside else None,
             "wbm_code":int(masks["wbm"][0][row_i,col]) if inside else None,
             "edm_code":int(masks["edm"][0][row_i,col]) if inside else None,
             "flm_code":int(masks["flm"][0][row_i,col]) if inside else None,
             "independent_of_fit":True, "quality_pass":None, "same_vertical_frame":False,
             "comparable_surface":None, "epoch_comparable":None,"product_version_identified":False}
        rec["exclusion_reasons"]=";".join(exclusion_reasons(rec))
        records.append(rec)
    epochs={}
    for date in sorted({r["date"] for r in records}):
        rows=[r for r in records if r["date"]==date]
        xx=np.array([r["easting_m"] for r in rows]); yy=np.array([r["northing_m"] for r in rows])
        epochs[date]={"n":len(rows),"beam_counts":dict(Counter(r["beam"] for r in rows)),
            "bbox_utm40n_m":[float(xx.min()),float(yy.min()),float(xx.max()),float(yy.max())],
            "occupied_1km_cells":len({(int((r["easting_m"]-gt[0])//1000),int((gt[3]-r["northing_m"])//1000)) for r in rows}),
            "water_mask_counts":dict(Counter(str(r["wbm_code"]) for r in rows)),
            "canopy_missing":sum(r["canopy_height_m"] is None for r in rows),
            "validation":validation_metrics(rows)}
    # Network is disabled: report actual installed operations and missing grids.
    pyproj.network.set_network_enabled(False)
    operations=TransformerGroup("EPSG:4979","EPSG:4326+3855",always_xy=True,allow_ballpark=False)
    datum={"applied":False,"eligible_installed_operation":operations.best_available,
        "available_descriptions":[t.description for t in operations.transformers],
        "unavailable_operations":[{"name":t.name,"grids":[{"name":g.short_name,"available":g.available,"url":g.url} for g in t.grids]} for t in operations.unavailable_operations],
        "formula_if_verified":"H_EGM2008 = h_ellipsoid - N_EGM2008; datum/frame/epoch conventions must match",
        "mine_to_EGM2008":"UNKNOWN; absolute source height is not proof of Baltic 1977",
        "zero_offset_is_not_a_datum_operation":True}
    passport={"schema":"vkm.surface_audit/1","product":"Copernicus GLO-30 DSM",
        "acquisition_epoch_year_interval":[2011,2015],"per_cell_epoch":"UNKNOWN",
        "intake_download_date":intake.get("downloaded_at"),"source_release":intake.get("release"),
        "vertical_datum":"EGM2008 EPSG:3855", "geographic_encoding":"EPSG:4326; producer frame WGS84-G1150 per intake",
        "derived_horizontal_crs":"EPSG:32640", "local_horizontal_frame_role":"SCENARIO_GEOREGISTRATION_NOT_SURVEYED",
        "reference_time_state":"ACQUISITION_COMPOSITE_2011_2015; not premining and not survey2026",
        "source_surface":logical(args.raster),"source_mesh":logical(args.source_mesh),"display_surface":logical(args.display_mesh),
        "raw_layers":raw_layers,"derived_raster":raster_info,"quality_layers":mask_info,
        "representation":representation,"uncertainty":uncertainty,"vertical_operations":datum,
        "satellite":{"source":logical(args.satellite),"product":"OpenAltimetry ATL08 response; full HDF product not available in these inputs",
            "source_version":"UNKNOWN","elevation_statistic":"UNKNOWN", "quality_flags":"UNKNOWN_NOT_RETURNED",
            "vertical_reference":"WGS84 ellipsoid / ITRF2014 per existing passport; no conversion applied",
            "epochs":epochs,"validation":validation_metrics(records),"all_raw_rows_reproduced":True,
            "no_fit_points_used":True,"no_residual_based_selection":True},
        "uses":{"regional_visualization":"SUPPORTED_WITH_LIMITATIONS","height_footprint_diagnostics":"SUPPORTED_WITH_LIMITATIONS",
            "bare_earth_top_boundary":"NOT_ESTABLISHED","water_bottom":"UNKNOWN_NOT_CREATED",
            "premining_surface":"NOT_ESTABLISHED","subsidence_from_DSM_ATL08":"NOT_ESTABLISHED",
            "absolute_geology_integration":"BLOCKED_MISSING_DOCUMENTED_VERTICAL_TIE"},
        "surface_decisions":{"water":"retain flattened water; no bathymetry","vegetation":"retain DSM; bare ground unknown",
            "buildings":"retain DSM; building mask unknown","dumps":"retain; real body/load possible; geometry/epoch unknown",
            "smoothing":"display only; no ground classification or new accuracy"}}
    with (out/"satellite_eligibility.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
    np.savez_compressed(out/"surface_quality_masks.npz",water=masks["wbm"][0],edited=masks["edm"][0],
        filling=masks["flm"][0],hem_valid=masks["hem"][1],slope_m_per_m=slope)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(2, 2, figsize=(14, 10), constrained_layout=True)
    extent=(0, z.shape[1]*gt[1]/1000,0,z.shape[0]*abs(gt[5])/1000)
    im=axs[0,0].imshow(z,extent=extent,origin="upper",cmap="terrain")
    fig.colorbar(im,ax=axs[0,0],label="Высота DSM, м EGM2008")
    axs[0,0].set_title("Copernicus N59E056 · 30 м · 2011–2015 + заполнения")
    flags=np.where(masks["wbm"][0]>0,2,np.where(masks["edm"][0]!=1,1,0))
    from matplotlib.colors import ListedColormap, BoundaryNorm
    cmap=ListedColormap(["#dce6d6","#e4953d","#3f83bc"])
    im=axs[0,1].imshow(flags,extent=extent,origin="upper",cmap=cmap,norm=BoundaryNorm([-.5,.5,1.5,2.5],3))
    cb=fig.colorbar(im,ax=axs[0,1],ticks=[0,1,2]); cb.ax.set_yticklabels(["Не редактировано","Редактировано","Вода"])
    axs[0,1].set_title("Маски источника · вода не означает дно")
    axs[1,0].imshow(z,extent=extent,origin="upper",cmap="Greys",alpha=.3)
    for date in sorted(epochs):
        pts=[r for r in records if r["date"]==date]
        axs[1,0].scatter([(r["easting_m"]-gt[0])/1000 for r in pts],
            [(r["northing_m"]-(gt[3]+gt[5]*z.shape[0]))/1000 for r in pts],s=10 if len(pts)>2 else 60,label=date+" · "+str(len(pts)))
    axs[1,0].legend(fontsize=8); axs[1,0].set_title("ATL08: разные эпохи · качество UNKNOWN")
    for ax in [axs[0,0],axs[0,1],axs[1,0]]:
        ax.set_xlabel("Восток от левой границы, км");ax.set_ylabel("Север от нижней границы, км");ax.set_aspect("equal")
    centre=z.shape[0]//2
    mid_col=z.shape[1]//2
    sl=slice(max(0,mid_col-10),min(z.shape[1],mid_col+11))
    xx=np.arange(len(z[centre,sl]))*gt[1]
    axs[1,1].plot(xx,z[centre,sl],label="Исходный DSM",lw=1.5)
    axs[1,1].plot(xx,display_xyz[:,2].reshape(z.shape)[centre,sl],label="Сглаженный display",lw=1.5)
    axs[1,1].set_ylim(100, max(250,float(z.max())))
    axs[1,1].set_aspect("equal",adjustable="box")
    axs[1,1].set_xlabel("Расстояние в центральном 600-метровом окне, м")
    axs[1,1].set_ylabel("Высота, м EGM2008")
    axs[1,1].set_title("Профиль без вертикального преувеличения · ×1")
    axs[1,1].legend(fontsize=8,loc="upper left")
    fig.suptitle("СКРУ-1: паспорт поверхности и охват контроля · 17.10.2026\nUTM40N; DSM ≠ DTM; независимая точность не установлена",fontsize=16)
    fig.savefig(out/"surface_audit.png",dpi=150)
    plt.close(fig)
    (out/"surface_passport.json").write_text(json.dumps(passport,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    pin(Path(__file__))
    command="python scripts/audit_skru1_surface.py "+" ".join("--"+k.replace("_","-")+" "+logical(v) for k,v in vars(args).items() if k not in ("root","resources_root","out"))
    receipt={"schema":"vkm.receipt/1","inputs":inputs,"command_template":command+" --root <PUBLIC> --resources-root <PRIVATE> --out <NEW_OUTPUT>",
        "runtime":{"python":platform.python_version(),"numpy":np.__version__,"gdal":gdal.VersionInfo(),"pyproj":pyproj.__version__},
        "outputs":{p.name:{"sha256":sha(p),"bytes":p.stat().st_size} for p in sorted(out.iterdir()) if p.is_file() and p.name in ("surface_passport.json","surface_quality_masks.npz","satellite_eligibility.csv","surface_audit.png")},
        "scientific_accuracy":"NOT_ESTABLISHED","representation_integrity":"PASS","source_mutations":False}
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return {"source_cells":z.size,"satellite_epochs":{d:e["n"] for d,e in epochs.items()},"validation":passport["satellite"]["validation"],"representation":representation}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("root","resources-root","intake","raster","quality-dir","source-mesh","display-mesh","satellite","out"):
        p.add_argument("--"+name,type=Path,required=True)
    print(json.dumps(build(p.parse_args()),ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
