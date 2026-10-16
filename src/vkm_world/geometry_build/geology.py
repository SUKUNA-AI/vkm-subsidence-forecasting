"""Source-faithful borehole constraints, separate sections and bounded interval diagnostics.

No collar height is inferred from a DSM. Interval maps are scalar scenarios in a
local horizontal frame, never absolute geological surfaces or mine seam solids.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter,defaultdict
from pathlib import Path

import numpy as np

from .pipeline import pinned_files,verify_pins,write_json,inventory

BASE = Path("11_evidence_vnext/canonical")
CATALOG = BASE/"BOREHOLES/borehole_catalog.csv"
PICKS = BASE/"BOREHOLES/borehole_picks.csv"
SYMBOLS = BASE/"WORLD_PARAMETERS/curated/c4_map/skru1/skru1_boreholes_014_unique.csv"
COLUMN = BASE/"WORLD_PARAMETERS/curated/skru1_c1_column.csv"
SECTION = Path("12_work_exports/spatial_model_20261016/geology_source/section_011_v3.json")


def input_paths(resources_root:Path)->list[str]:
    config=json.loads((resources_root/SECTION).read_text(encoding="utf-8"))
    return sorted(str(p).replace("\\","/") for p in [CATALOG,PICKS,SYMBOLS,COLUMN,SECTION,
        SECTION.parent/"receipt.json",SECTION.parent/"receipt_v2.json",SECTION.parent/"receipt_v3.json",
        SECTION.parent/"section_011.json",SECTION.parent/"section_011_v2.json",
        Path(config["image"]),Path(config["source"]["document"])])


def number(value):
    try:
        v=float(value)
        return v if np.isfinite(v) else None
    except (TypeError,ValueError):
        return None


def qualified_matches(symbols,picks):
    """Use existing qualified catalogue IDs; no bare-number cross-mine matching."""
    by_well=defaultdict(list)
    for row in picks:
        by_well[row["borehole_id"]].append(row)
    return [{"symbol":s,"picks":by_well.get(s["borehole_id_catalogue"],[]),
             "identity_status":"EXISTING_QUALIFIED_SOURCE_INTERPRETATION",
             "accepted_physical_identity":False} for s in symbols
            if s["borehole_id_catalogue"] and s["catalogue_match"]=="HIGH"]


def unique_depths(rows):
    """Conflicting readings remain conflicts; repeated equal values do not add wells."""
    groups=defaultdict(list)
    for row in rows:
        if row["source_id"]=="VKM-SRC-012" and row["reference"]=="depth_below_collar" and row["units"]=="m":
            value=number(row["top_depth_m"])
            if value is not None:
                groups[(row["borehole_id"],row["unit_canonical_guess"])].append((value,row["pick_id"]))
    values,conflicts={},[]
    for key,items in groups.items():
        distinct={x[0] for x in items}
        if len(distinct)==1:
            values[key]={"depth_m":items[0][0],"pick_ids":[x[1] for x in items]}
        else:
            conflicts.append({"borehole_id":key[0],"unit":key[1],"readings":items,"status":"CONFLICT_NOT_AVERAGED"})
    return values,conflicts


def predict(xy,values,target,method):
    distance=np.linalg.norm(np.asarray(xy)-target,axis=1)
    if not len(distance):
        return None
    if distance.min()<1e-9:
        return float(values[np.argmin(distance)])
    if method=="nearest":
        return float(values[np.argmin(distance)])
    if method in {"idw1","idw2"}:
        weights=distance**(-int(method[-1]))
        return float(np.dot(weights,values)/weights.sum())
    if method=="linear":
        from scipy.interpolate import LinearNDInterpolator
        try:
            value=float(LinearNDInterpolator(xy,values)(np.asarray(target)[None,:])[0])
            return value if np.isfinite(value) else None
        except Exception as error:
            from scipy.spatial import QhullError
            if isinstance(error,QhullError):
                return None
            raise
    raise ValueError("unsupported diagnostic method")


def grouped_cv(observations):
    """Withhold one qualified well-ID group, including all its source readings."""
    predictions=[]
    for held in sorted({r["borehole_id"] for r in observations}):
        train=[r for r in observations if r["borehole_id"]!=held]
        test=[r for r in observations if r["borehole_id"]==held]
        if len(train)<2:
            continue
        xy=np.array([[r["x"],r["y"]] for r in train]); values=np.array([r["value_m"] for r in train])
        for row in test:
            target=np.array([row["x"],row["y"]])
            for method in ("nearest","idw1","idw2","linear"):
                estimate=predict(xy,values,target,method)
                predictions.append({"held_well":held,"method":method,"observed_m":row["value_m"],
                    "predicted_m":estimate,"error_m":None if estimate is None else estimate-row["value_m"],
                    "training_wells":sorted({r["borehole_id"] for r in train}),
                    "status":"NOT_PREDICTED_OUTSIDE_TRAIN_HULL_OR_DEGENERATE" if estimate is None else "DIAGNOSTIC_ONLY"})
    summary={}
    for method in ("nearest","idw1","idw2","linear"):
        rows=[r for r in predictions if r["method"]==method]; errors=[r["error_m"] for r in rows if r["error_m"] is not None]
        summary[method]={"predicted":len(errors),"not_predicted":len(rows)-len(errors),
            "rmse_m":float(np.sqrt(np.mean(np.square(errors)))) if errors else None,
            "mae_m":float(np.mean(np.abs(errors))) if errors else None}
    return {"holdout":"whole qualified borehole ID; all source rows at that well excluded",
            "selection":"methods fixed before scoring; no winner promoted to accepted geology",
            "predictions":predictions,"summary":summary}


def render_source_columns(numeric, path:Path)->dict:
    """Draw independent source scales under complete stable rc defaults, restoring caller style.

    Labels are shortened for display only. Source rows, references and numerical values are unchanged.
    A direct Agg canvas avoids changing a host application's selected plotting backend.
    """
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    groups=defaultdict(list)
    for row in numeric:
        reference=row["reference"]
        a,b=(number(row["top_depth_m"]),number(row["bottom_depth_m"])) if reference=="depth_below_collar" else (number(row["top_abs_m"]),number(row["bottom_abs_m"]))
        if a is not None:
            groups[(row["borehole_id"],reference)].append((a,b,row))
    if not groups:
        return {"columns":0,"shortened_labels":0}
    defaults={key:value for key,value in matplotlib.rcParamsDefault.items() if key!="backend"}
    defaults.update({"font.family":["DejaVu Sans"],"font.size":8,"text.usetex":False})
    shortened=0
    with matplotlib.rc_context(rc=defaults):
        columns=min(6,len(groups)); rows=(len(groups)+columns-1)//columns
        fig=Figure(figsize=(max(9,columns*2.5),rows*5.8+1.2),dpi=180)
        FigureCanvasAgg(fig)
        axes=fig.subplots(rows,columns,squeeze=False)
        for ax,((bid,reference),picks) in zip(axes.flat,sorted(groups.items())):
            for a,b,row in picks:
                ax.plot([0,1],[a,a],color="#2166ac",lw=1)
                label=" ".join((row["unit_canonical_guess"] or row["unit_as_printed"]).split())
                if len(label)>18:
                    label=label[:17]+"…"; shortened+=1
                ax.text(.5,a,label,transform=ax.get_yaxis_transform(),ha="center",va="center",fontsize=6.5,
                        clip_on=True,bbox={"facecolor":"white","edgecolor":"none","alpha":.8,"pad":.2})
                if b is not None:
                    ax.plot([.1,.1],[a,b],color="#999999",lw=3)
            depth=reference=="depth_below_collar"
            caption="depth m / collar UNKNOWN" if depth else "source height m / datum UNKNOWN"
            ax.set_title(bid+"\n"+caption,fontsize=8,pad=10)
            ax.set_xlim(0,1); ax.set_xticks([]); ax.grid(axis="y",alpha=.2)
            ax.tick_params(axis="y",labelsize=7,pad=2)
            if depth:
                ax.invert_yaxis()
        for ax in list(axes.flat)[len(groups):]:
            ax.set_visible(False)
        fig.suptitle("Source columns / convention conflict retained / independent scales / no DSM subtraction",fontsize=11,y=.975)
        fig.text(.5,.025,"Display labels shortened to 18 characters and clipped; full labels: source_picks.jsonl",
                 ha="center",fontsize=8)
        fig.subplots_adjust(left=.06,right=.985,bottom=.07,top=.91,wspace=.35,hspace=.30)
        fig.savefig(path,dpi=180,format="png",metadata={"Software":"VKM source columns renderer"})
        fig.clear()
    return {"columns":len(groups),"shortened_labels":shortened}


def build_geology_package(resources_root:Path,out:Path)->dict:
    from osgeo import ogr,osr
    from scipy.spatial import Delaunay,QhullError
    from .source_section import build_source_section

    resources_root,out=resources_root.resolve(),out.resolve()
    pins=pinned_files(resources_root,input_paths(resources_root))
    out.mkdir(parents=True,exist_ok=False)
    def read(path):
        with (resources_root/path).open(encoding="utf-8-sig",newline="") as stream:
            return list(csv.DictReader(stream))
    def jsonl(name,rows):
        with (out/(name+".jsonl")).open("w",encoding="utf-8",newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row,ensure_ascii=False,sort_keys=True,allow_nan=False)+"\n")
    catalog,picks,symbols,column=map(read,[CATALOG,PICKS,SYMBOLS,COLUMN])
    jsonl("source_catalogue",catalog); jsonl("source_picks",picks); jsonl("source_columns",column)
    joined=qualified_matches(symbols,picks)
    matches=[]
    numeric=[]
    for match in joined:
        s=match["symbol"]; rows=match["picks"]
        rows=[r for r in rows if r["units"]=="m" and any(number(r[k]) is not None for k in
                ("top_depth_m","bottom_depth_m","top_abs_m","bottom_abs_m"))]
        numeric.extend(rows)
        matches.append({"borehole_id":s["borehole_id_catalogue"],"symbol_source":s,
            "pick_ids":[r["pick_id"] for r in rows],"numeric_pick_rows":len(rows),
            "absolute_pick_rows":sum(any(number(r[k]) is not None for k in ("top_abs_m","bottom_abs_m")) for r in rows),
            "absolute_z_relation_to_DSM":"UNKNOWN","status":"DERIVATION",
            "identity_status":match["identity_status"],"accepted_physical_identity":False})
    jsonl("qualified_symbol_pick_links",matches)
    # The convention conflict is preserved as competing branches, without relabelling raw observations.
    alternatives=[{"id":"S012_TABLE_HEADER_ROOF","status":"MODEL_CHOICE","meaning":"table header roof convention",
                   "competing_evidence":"source prose p63 says floor; header p64/65 says roof"},
                  {"id":"S012_PROSE_FLOOR","status":"MODEL_CHOICE","meaning":"prose floor convention",
                   "competing_evidence":"source table header says roof"}]
    write_json(out/"interpretations.json",alternatives)
    depths,conflicts=unique_depths(picks)
    write_json(out/"pick_conflicts.json",conflicts)
    ogr.UseExceptions()
    gpkg=ogr.GetDriverByName("GPKG").CreateDataSource(str(out/"geology.gpkg"))
    local=osr.SpatialReference(); local.ImportFromWkt('LOCAL_CS["SKRU1_LOCAL_SH1 reconstructed drawing metres; survey datum UNKNOWN",UNIT["metre",1]]')
    def layer(name,geometry_type,features):
        lyr=gpkg.CreateLayer(name,local,geometry_type)
        fields=sorted({k for props,geometry in features for k in props})
        for key in fields:
            lyr.CreateField(ogr.FieldDefn(key,ogr.OFTString))
        for props,geometry in features:
            f=ogr.Feature(lyr.GetLayerDefn())
            for k,v in props.items():
                if v is not None:
                    f.SetField(k,json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else str(v))
            if geometry:
                f.SetGeometry(ogr.CreateGeometryFromJson(json.dumps(geometry)))
            lyr.CreateFeature(f)
        return lyr
    layer("checked_symbols_local",ogr.wkbPoint,[(
        {"record_id":"GEO-SYMBOL:"+s["key"],"borehole_id":s["borehole_id_catalogue"],"frame_kind":"LOCAL_HORIZONTAL",
         "frame_id":"SKRU1_LOCAL_SH1","status":"DERIVATION","review_status":s["review_status"],
         "position_source":s["position_source"],"absolute_Z":"UNKNOWN"},
        {"type":"Point","coordinates":[float(s["x_local_m"]),float(s["y_local_m"])]}) for s in symbols])
    intervals={"TKT_to_SMT":("ТКТ","СМТ"),"SMT_to_PKS":("СМТ","ПКС"),
               "SKZ_to_SZ":("СКЗ","СЗ"),"SZ_to_PDKS":("СЗ","ПдКС")}
    observations=[]; grids=[]; validations={}; interval_qc={}
    for interval,(first,second) in intervals.items():
        obs=[]
        for s in symbols:
            bid=s["borehole_id_catalogue"]
            a,b=depths.get((bid,first)),depths.get((bid,second))
            if s["catalogue_match"]!="HIGH" or a is None or b is None:
                continue
            value=b["depth_m"]-a["depth_m"]
            if value<0:
                conflicts.append({"borehole_id":bid,"interval":interval,"status":"FAILED_NEGATIVE_INTERVAL","value_m":value})
                continue
            obs.append({"record_id":"GEO-INTERVAL:"+bid+":"+interval,"borehole_id":bid,"interval_id":interval,
                "x":float(s["x_local_m"]),"y":float(s["y_local_m"]),"value_m":value,
                "pick_ids":a["pick_ids"]+b["pick_ids"],"status":"DERIVATION","frame_kind":"LOCAL_HORIZONTAL",
                "frame_id":"SKRU1_LOCAL_SH1","unit":"m interval, not absolute Z", "source_id":"VKM-SRC-012",
                "roof_branch":"thickness of first unit only if both picks are roofs and no missing intermediate unit",
                "floor_branch":"different unit attribution; same numerical boundary separation",
                "mine_attribution":"existing qualified plan interpretation; source table spans intermine area",
                "available_from":None})
        observations.extend(obs)
        validations[interval]=grouped_cv(obs)
        if len(obs)<3:
            interval_qc[interval]={"status":"BLOCKED_INSUFFICIENT_SUPPORT","wells":len(obs)}
            continue
        xy=np.array([[r["x"],r["y"]] for r in obs]); values=np.array([r["value_m"] for r in obs])
        try:
            hull=Delaunay(xy)
        except QhullError:
            interval_qc[interval]={"status":"BLOCKED_COLLINEAR_SUPPORT","wells":len(obs)}
            continue
        inside_count=0
        for y in np.arange(np.ceil(xy[:,1].min()/250)*250,xy[:,1].max()+1,250):
            for x in np.arange(np.ceil(xy[:,0].min()/250)*250,xy[:,0].max()+1,250):
                if hull.find_simplex([[x,y]])[0]<0:
                    continue
                inside_count+=1
                for method in ("nearest","idw1","idw2","linear"):
                    value=predict(xy,values,np.array([x,y]),method)
                    if value is None:
                        continue
                    grids.append({"record_id":f"GEO-GRID:{interval}:{method}:{x:g}:{y:g}","interval_id":interval,
                        "method":method,"x":float(x),"y":float(y),"value_m":value,"support_wells":[r["borehole_id"] for r in obs],
                        "nearest_support_m":float(np.linalg.norm(xy-[x,y],axis=1).min()),"frame_kind":"LOCAL_HORIZONTAL",
                        "frame_id":"SKRU1_LOCAL_SH1","unit":"m interval, not elevation","status":"INTERPOLATION",
                        "interpretation":"SCENARIO_UNPARTITIONED_STRUCTURE_UNKNOWN","grid_spacing_m":250,
                        "uncertainty":"source reading and structural uncertainty UNKNOWN; method spread is not full uncertainty"})
        interval_qc[interval]={"status":"DIAGNOSTIC_SCENARIO_ONLY","wells":len(obs),"inside_hull_grid_nodes":inside_count,
            "extrapolation":"NONE_OUTSIDE_CONVEX_HULL","geological_barriers":"UNKNOWN; scenario may cross unsurveyed structure",
            "absolute_elevation_surface":"NOT_BUILT"}
    for name,rows in [("interval_observations_local",observations),("interval_grid_local",grids)]:
        layer(name,ogr.wkbPoint,[(r,{"type":"Point","coordinates":[r["x"],r["y"]]}) for r in rows])
        jsonl(name,rows)
    layer("symbol_pick_links",ogr.wkbNone,[(r,None) for r in matches])
    layer("catalogue",ogr.wkbNone,[(r,None) for r in catalog])
    layer("picks",ogr.wkbNone,[(r,None) for r in picks])
    gpkg=None
    import sqlite3
    with sqlite3.connect(out/"geology.gpkg") as connection:
        connection.execute("update gpkg_contents set last_change='2000-01-01T00:00:00.000Z'")
    connection.close()
    write_json(out/"interval_validation.json",validations)
    write_json(out/"pick_conflicts.json",conflicts)
    # Depth and absolute columns are panels, not located beneath the global DSM.
    render_source_columns(numeric,out/"source_columns.png")
    section=build_source_section(resources_root,SECTION,out/"section")
    eligibility={"catalogue_rows":len(catalog),"pick_rows":len(picks),"checked_symbols":len(symbols),
        "qualified_symbol_links":len(matches),"symbols_with_numeric_picks":sum(r["numeric_pick_rows"]>0 for r in matches),
        "symbols_with_absolute_picks":sum(r["absolute_pick_rows"]>0 for r in matches),
        "absolute_surface_status":"BLOCKED_NO_3_COMPATIBLE_NONCOLLINEAR_WELLS_WITH_KNOWN_DATUM",
        "unknowns":["collar elevations for depth-only wells","source absolute datum vs EGM2008",
                    "map trace of SRC011 figure4.7a","structural domains for interval interpolation",
                    "stratigraphic package to mined-seam vertical relationship"],
        "intervals":interval_qc,"source_section":section,"status":"DERIVATION_WITH_EXPLICIT_UNKNOWNS",
        "scientific_admission":"NOT_CHECKED"}
    write_json(out/"eligibility.json",eligibility)
    verify_pins(resources_root,pins)
    receipt={"schema":"vkm.geology_package/1","inputs":pins,"counts":{"catalogue":len(catalog),"picks":len(picks),
        "symbol_links":len(matches),"interval_observations":len(observations),"interval_grid_records":len(grids)},
        "outputs_sha256":inventory(out),"code_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "status":"DERIVATION_WITH_EXPLICIT_UNKNOWNS","scientific_admission":"NOT_CHECKED"}
    write_json(out/"receipt.json",receipt)
    return receipt
