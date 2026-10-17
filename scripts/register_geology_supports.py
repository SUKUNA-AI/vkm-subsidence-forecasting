"""Place interpreted geological supports on a preserved DSM as an explicit scenario.

Input rows use physical_id,x,y in SKRU1_LOCAL_SH1. Optional alternative_xy_m
contains alternative local positions. Central height is bilinear sampling of the
original projected DSM; sensitivity envelopes are extrema of source cells within
declared local radii, never confidence intervals or validation errors.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def affine(points, matrix):
    points = np.asarray(points, dtype=float)
    return np.column_stack((points, np.ones(len(points)))) @ np.asarray(matrix).T


def local_to_utm(points, terrain):
    from pyproj import Transformer
    steps = terrain["transform"]["steps_preserved"]
    step = next(s for s in steps if s["from"] == "SKRU1_LOCAL_SH1")
    sh5 = affine(points, step["matrix"])
    geo = next(s for s in steps if s["from"] == "SKRU2_LOCAL_SH5")["fit"]
    en = affine(sh5, geo["fit"]["M"])
    gr = geo["graticule"]
    u, v = en[:, 0] / geo["m_per_px"]["x"], -en[:, 1] / geo["m_per_px"]["y"]
    lon = gr["lon_at_u0"] + (u-gr["u0_px"]) * gr["deg_per_px_lon"]
    lat = gr["lat_at_v0"] - (v-gr["v0_px"]) * gr["deg_per_px_lat"]
    e, n = Transformer.from_crs("OGC:CRS84", "EPSG:32640", always_xy=True).transform(lon,lat)
    return np.column_stack((e,n))


def bilinear(z, gt, e, n):
    """No extrapolation or implicit NoData fill; pixel-centre convention."""
    c, r = (e-gt[0])/gt[1]-.5, (n-gt[3])/gt[5]-.5
    if c < 0 or r < 0 or c > z.shape[1]-1 or r > z.shape[0]-1:
        return None
    c0, r0 = int(np.floor(c)), int(np.floor(r))
    c1, r1 = min(c0+1,z.shape[1]-1), min(r0+1,z.shape[0]-1)
    dc, dr = c-c0, r-r0
    vals=np.array([z[r0,c0],z[r0,c1],z[r1,c0],z[r1,c1]])
    if not np.isfinite(vals).all():
        return None
    return float(vals @ np.array([(1-dr)*(1-dc),(1-dr)*dc,dr*(1-dc),dr*dc]))


def sensitivity(xyz, hem, hem_valid, xy, radius):
    """Source-cell envelope in local metric coordinates; not a CI."""
    d2=np.sum((xyz[:,:2]-xy)**2,axis=1)
    sel=d2 <= radius*radius
    if not sel.any():
        return {"radius_m":float(radius),"n_cells":0,"min_m":None,"max_m":None,"hem_max_m":None}
    hv=sel & hem_valid & np.isfinite(hem)
    return {"radius_m":float(radius),"n_cells":int(sel.sum()),
            "min_m":float(xyz[sel,2].min()),"max_m":float(xyz[sel,2].max()),
            "hem_valid_n":int(hv.sum()),"hem_max_m":float(hem[hv].max()) if hv.any() else None,
            "meaning":"source-cell extrema inside selected XY radius; sensitivity, not CI"}


def polygon_mask(points, polygon, allowance=0.):
    """Convex map envelope plus deterministic buffer; no stochastic claim."""
    p=np.asarray(points,float); poly=np.asarray(polygon,float)
    if np.array_equal(poly[0],poly[-1]): poly=poly[:-1]
    crosses=[]; distances=[]
    for a,b in zip(poly,np.roll(poly,-1,axis=0)):
        edge=b-a; rel=p-a
        crosses.append(edge[0]*rel[:,1]-edge[1]*rel[:,0])
        t=np.clip((rel@edge)/(edge@edge),0,1)
        distances.append(np.linalg.norm(rel-t[:,None]*edge,axis=1))
    crosses=np.array(crosses)
    return np.all(crosses>=-1e-8,axis=0)|np.all(crosses<=1e-8,axis=0)|(np.min(distances,axis=0)<=allowance)


def sample_many(rows, terrain, raster, gt, radii=(200.,400.), offsets=(0.,)):
    xyz=np.asarray(terrain["points"],float)
    pd=terrain["point_data"]
    hem=np.asarray(pd["source_HEM_random_sigma_m"],float)
    hem_valid=np.asarray(pd["source_HEM_valid"],bool)
    placed=[]
    if len({r["physical_id"] for r in rows})!=len(rows):
        raise ValueError("duplicate physical support; merge evidence without duplicating geometry")
    for row in rows:
        xy=np.array(row.get("sh1_xy_m",[row.get("x"),row.get("y")]),float)
        if not np.isfinite(xy).all():
            raise ValueError("nonfinite support coordinates")
        e,n=local_to_utm([xy],terrain)[0]
        central=bilinear(raster,gt,e,n)
        envs=[sensitivity(xyz,hem,hem_valid,xy,float(rad)) for rad in radii]
        polyenvs=[]
        for scenario in row.get("placement_scenarios",[]):
            sel=polygon_mask(xyz[:,:2],scenario["sh1_polygon"],scenario.get("extra_discrepancy_allowance_m",0))
            valid=sel & hem_valid & np.isfinite(hem)
            polyenvs.append({"id":scenario["id"],"n_cells":int(sel.sum()),
                "min_m":float(xyz[sel,2].min()) if sel.any() else None,
                "max_m":float(xyz[sel,2].max()) if sel.any() else None,
                "hem_max_m":float(hem[valid].max()) if valid.any() else None,
                "includes_extra_discrepancy_allowance_m":scenario.get("extra_discrepancy_allowance_m",0),
                "meaning":"source-cell extrema in registered source-reading polygon and explicit buffer; not CI"})
        nearest=int(np.argmin(np.sum((xyz[:,:2]-xy)**2,axis=1)))
        alternatives=[]
        for alt in row.get("alternative_xy_m",[]):
            ax,ay=(alt["x"],alt["y"]) if isinstance(alt,dict) else alt[:2]
            ae,an=local_to_utm([[ax,ay]],terrain)[0]
            alternatives.append({"x":float(ax),"y":float(ay),"terrain_height_m":bilinear(raster,gt,ae,an)})
        heights=[a["terrain_height_m"] for a in alternatives if a["terrain_height_m"] is not None]
        base=polyenvs[0] if polyenvs else (envs[0] if envs else {"min_m":central,"max_m":central})
        mins=[v for v in [central,base["min_m"],*heights] if v is not None]
        maxs=[v for v in [central,base["max_m"],*heights] if v is not None]
        q={**row,"x":float(xy[0]),"y":float(xy[1]),"easting_utm40n_m":float(e),"northing_utm40n_m":float(n),
           "terrain_height_m":central,"terrain_min_m":min(mins) if mins else None,"terrain_max_m":max(maxs) if maxs else None,
           "xy_status":row.get("xy_status","INFERRED_LOCATION"),
           "terrain_status":"ENGINEERING_ASSUMPTION_DSM_COLLAR" if central is not None else "OUTSIDE_DSM_NO_EXTRAPOLATION",
           "vertical_frame":"EGM2008_SAMPLED_DSM_SCENARIO_NOT_OBSERVED_COLLAR",
           "surface_operator":"bilinear evaluation of preserved projected source DSM",
           "source_cell_nearest":{"index":nearest,"height_m":float(xyz[nearest,2]),
             "distance_local_m":float(np.linalg.norm(xyz[nearest,:2]-xy)),
             "hem_m":float(hem[nearest]) if hem_valid[nearest] else None},
           "sensitivity_envelopes":envs,"source_polygon_sensitivity":polyenvs,"alternative_position_samples":alternatives,
           "offset_scenarios":[{"offset_m":float(o),"height_m":central+o if central is not None else None,
             "status":"ENGINEERING_ASSUMPTION_NOT_DATUM_OPERATION"} for o in offsets],
           "unknown_components":["actual collar vs DSM object","acquisition epoch vs borehole epoch","full horizontal registration error","mine datum","systematic vegetation/building bias"],
           "geometry_formula":"z_contact_scenario = terrain_height_m + explicit_offset_m - source_depth_m",
           "accuracy_claim":False}
        placed.append(q)
    return placed


def build(args):
    from osgeo import gdal
    gdal.UseExceptions()
    root=args.root.resolve()
    paths=[args.placements,args.source_mesh,args.raster,Path(__file__)]
    def logical(p):
        return Path(p).resolve().relative_to(root).as_posix()
    raw=json.loads(args.placements.read_text(encoding="utf-8-sig"))
    rows=raw["rows"]
    terrain=json.loads(args.source_mesh.read_text(encoding="utf-8-sig"))
    if terrain["coordinates"]["x_y_frame"]!="SKRU1_LOCAL_SH1":
        raise ValueError("terrain is not in requested local frame")
    ds=gdal.Open(str(args.raster),gdal.GA_ReadOnly)
    z=ds.ReadAsArray().astype(float)
    nodata=ds.GetRasterBand(1).GetNoDataValue()
    if nodata is not None: z[z==nodata]=np.nan
    xyz=np.asarray(terrain["points"],float)
    if not np.array_equal(xyz[:,2],z.reshape(-1)):
        raise ValueError("source mesh is not exactly the declared raster")
    computed=local_to_utm(xyz[:,:2],terrain)
    original=np.column_stack((terrain["point_data"]["source_easting_UTM40N_m"],terrain["point_data"]["source_northing_UTM40N_m"]))
    maxerr=float(np.linalg.norm(computed-original,axis=1).max())
    if maxerr>.001:
        raise ValueError("forward chain does not reproduce raster coordinates")
    placed=sample_many(rows,terrain,z,ds.GetGeoTransform(),args.radii,args.offsets)
    out=args.out
    out.mkdir(parents=True,exist_ok=True)
    obj={"schema":"vkm.geology_support_placement/1","frame":"SKRU1_LOCAL_SH1","vertical_frame":"EGM2008_DSM_SCENARIO",
         "role":"ENGINEERING_ASSUMPTION; missing collars initialized from source DSM; not measured height tie",
         "rows":placed,"placement_provenance":{k:v for k,v in raw.items() if k!="rows"},
         "terrain_transform":terrain["transform"],"transform_roundtrip_max_m":maxerr,
         "roundtrip_meaning":"implementation integrity only; not survey accuracy",
         "vertical_scenarios":{"baseline_offset_m":0,"geoid_conversion":"NOT_APPLICABLE_TO_EGM2008_DSM; ellipsoid branch needs a specified grid and source frame",
             "site_offset":"explicit offsets only; none estimated from geology or validation points",
             "sensitivity":"separate spatial range and HEM, no RSS or probabilistic confidence assigned"}}
    dest=out/"support_placement.json"
    dest.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    receipt={"schema":"vkm.receipt/1","inputs":{logical(p):sha(p) for p in paths},
      "output":{"path":logical(dest),"sha256":sha(dest)},
      "command":"python scripts/register_geology_supports.py --root <PUBLIC> --placements "+logical(args.placements)+" --source-mesh "+logical(args.source_mesh)+" --raster "+logical(args.raster)+" --radii "+",".join(map(str,args.radii))+" --offsets "+",".join(map(str,args.offsets))+" --out <OUTPUT>",
      "counts":{"rows":len(placed),"central_inside_dsm":sum(r["terrain_height_m"] is not None for r in placed)},
      "source_dsm_preserved":True,"transformation_integrity_max_m":maxerr,"scientific_accuracy":"NOT_ESTABLISHED"}
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return receipt["counts"]


def csv_floats(value):
    return [float(v) for v in value.split(",")]


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("root","placements","source-mesh","raster","out"):
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--radii",type=csv_floats,default=[200.,400.])
    p.add_argument("--offsets",type=csv_floats,default=[0.])
    args=p.parse_args()
    if not args.radii or min(args.radii)<0: p.error("radii must be nonnegative")
    print(json.dumps(build(args),indent=2))

