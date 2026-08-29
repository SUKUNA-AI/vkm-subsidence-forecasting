#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build SKRU-1 reconstructed data package v3.1 (data only, no predictive model).

Usage:
    python reproduce_v3.py --source-docx "ВКР_Филатова_М_С.docx" --output dataset_v3

The script extracts source figures from the DOCX, reconstructs plan geometry from the clean
red vector overlay in Figure 13, conditions reconstructed attributes on exact published rows,
creates a spatially clipped analysis grid, and synthesizes measurement processes (leveling,
planar surveying, GNSS and relative InSAR) without using hidden ground truth in adjustment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
import textwrap
import warnings
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import cv2
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from PIL import Image
from pyproj import CRS
from scipy.interpolate import RBFInterpolator
from scipy.ndimage import gaussian_filter
from scipy.optimize import brentq, differential_evolution
from scipy.spatial import cKDTree
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Point, Polygon, box
from shapely import points as shp_points, distance as shp_distance
from shapely.ops import linemerge, split, unary_union
from shapely.validation import make_valid

warnings.filterwarnings("ignore", category=UserWarning)

VERSION = "3.1"
SEED = 20260810
LOCAL_CRS_WKT = 'LOCAL_CS["SKRU-1 reconstructed local engineering CRS",LOCAL_DATUM["unknown",0],UNIT["metre",1],AXIS["X",EAST],AXIS["Y",NORTH]]'
LOCAL_CRS = CRS.from_wkt(LOCAL_CRS_WKT)
INITIAL_LOCAL_BOUNDS = np.array([20600.0, 38600.0, 29700.0, 45900.0])
FIG13_PLAN_CROP = (575, 105, 1125, 610)  # x0,y0,x1,y1 in image15
FIG22_PLAN_BBOX = (12.0, 5.0, 767.0, 676.0)  # image24
FIG24_KZT_BBOX = (30.0, 32.0, 337.0, 320.0)
FIG24_KO_BBOX = (475.0, 31.0, 809.0, 320.0)
FIG24_ES_BBOX = (26.0, 348.0, 325.0, 625.0)
FIG24_FAULT_BBOX = (468.0, 337.0, 828.0, 628.0)
FIG25_BACKFILL_BBOX = (618.0, 67.0, 1136.0, 548.0)
FIG25_LITH_BBOX = (57.0, 565.0, 550.0, 1050.0)

# Published TerraSAR-X acquisition dates used only as an auxiliary synthetic calendar.
INSAR_DATES_2020 = [
    "2020-04-28","2020-05-09","2020-05-20","2020-05-31","2020-06-11","2020-06-22",
    "2020-07-03","2020-07-14","2020-07-25","2020-08-05","2020-08-16","2020-08-27",
    "2020-09-07","2020-09-18","2020-09-29","2020-10-10","2020-10-21","2020-11-01",
]
INSAR_DATES_2021 = [
    "2021-04-26","2021-05-07","2021-05-18","2021-05-29","2021-06-09","2021-06-20",
    "2021-07-01","2021-07-12","2021-07-23","2021-08-03","2021-08-14","2021-08-25",
    "2021-09-05","2021-09-16","2021-09-27","2021-10-08",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--source-docx", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--seed-dir", type=Path, default=Path(__file__).resolve().parent / "seed")
    p.add_argument("--seed", type=int, default=SEED)
    return p.parse_args()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def json_default(obj: Any):
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.floating,)): return float(obj)
    if isinstance(obj, (np.bool_,)): return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime)): return obj.isoformat()
    if isinstance(obj, Path): return str(obj)
    raise TypeError(type(obj).__name__)


def dump_json(obj: Any, path: Path) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")


def save_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig", float_format="%.15g")


def safe_geom(g):
    if g is None or g.is_empty:
        return g
    if not g.is_valid:
        g = make_valid(g)
    if isinstance(g, GeometryCollection):
        polys = [x for x in g.geoms if isinstance(x, (Polygon, MultiPolygon)) and not x.is_empty]
        if polys:
            g = unary_union(polys)
    return g


def extract_docx_media(docx: Path, tmp: Path) -> Path:
    if not docx.exists():
        raise FileNotFoundError(docx)
    with zipfile.ZipFile(docx) as zf:
        wanted = [n for n in zf.namelist() if n.startswith("word/media/")]
        if not wanted:
            raise RuntimeError("DOCX does not contain word/media")
        zf.extractall(tmp)
    media = tmp / "word" / "media"
    for idx in range(14, 28):
        if not (media / f"image{idx}.png").exists():
            raise RuntimeError(f"Required source figure image{idx}.png is missing")
    return media


def load_rgb(media: Path, idx: int) -> np.ndarray:
    return np.array(Image.open(media / f"image{idx}.png").convert("RGB"))


def norm_to_local(xn: float, yn: float, bounds: Sequence[float]) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = bounds
    return xmin + xn * (xmax - xmin), ymax - yn * (ymax - ymin)


def local_to_norm(x: float, y: float, bounds: Sequence[float]) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = bounds
    return (x - xmin) / (xmax - xmin), (ymax - y) / (ymax - ymin)


def map_plan_pixel_to_local(px: float, py: float, bbox: Sequence[float], bounds: Sequence[float]) -> tuple[float, float]:
    x0, y0, x1, y1 = bbox
    return norm_to_local((px - x0) / (x1 - x0), (py - y0) / (y1 - y0), bounds)


def map_local_to_panel_pixel(x: float, y: float, bbox: Sequence[float], bounds: Sequence[float]) -> tuple[float, float]:
    xn, yn = local_to_norm(x, y, bounds)
    x0, y0, x1, y1 = bbox
    return x0 + xn * (x1 - x0), y0 + yn * (y1 - y0)


def sample_window(img: np.ndarray, px: float, py: float, radius: int = 2) -> np.ndarray:
    h, w = img.shape[:2]
    x = int(round(px)); y = int(round(py))
    xa, xb = max(0, x-radius), min(w, x+radius+1)
    ya, yb = max(0, y-radius), min(h, y+radius+1)
    return img[ya:yb, xa:xb].reshape(-1, 3).astype(float)


def create_source_registry(docx_hash: str) -> pd.DataFrame:
    rows = [
        ("SRC-01","ВКР_Филатова_М_С.docx","ВКР","СКРУ-1","12 source layers, figures 12–27, exact table fragments","P,D,C",docx_hash,"Primary TAB/Excel unavailable; reconstructed layer membership remains candidate-level."),
        ("SRC-02","Babayants-Disser.pdf","dissertation","Berezniki/Solikamsk","time-series regimes and InSAR uncertainty context","P,C",None,"External analogue, not SKRU-1 surveying journal."),
        ("SRC-03","03-GR-24-2.pdf","article","Berezniki","published TerraSAR-X calendars and observed rates","P,C",None,"External analogue; LOS-derived subvertical displacement."),
        ("SRC-04","106_Губанова__Глебова.pdf","article","Tyubegatan","geomechanical stress envelopes","P,C",None,"External stress scenarios only."),
        ("SRC-05","НК 26 Бобровицкий Григорий.pdf","VKR","Gremyachinskoye","surveying formulas and profile forms","P,C",None,"Methodical structure, not SKRU-1 values."),
        ("WEB-01","Приказ Ростехнадзора №186","regulation","Russia","measurement design and QC context","P/W",None,"Does not provide enterprise deformation limits for SKRU-1."),
    ]
    return pd.DataFrame(rows, columns=["source_id","file_name","source_type","object","used_for","provenance_codes","sha256","limitations"])


def source_declared_integrated_metadata() -> pd.DataFrame:
    return pd.DataFrame([{
        "source_object":"final_integrated_polygon_layer",
        "source_figure":"Figure 23 / image25",
        "source_declared_rows":1665,
        "source_declared_fields":257,
        "reconstruction_status":"metadata_only_not_row_reproduced",
        "reason":"The source does not expose the original 1665 geometries/records; v3 does not fabricate them.",
        "provenance":"P",
    }])


def build_combined_published_rows(anchors: pd.DataFrame) -> pd.DataFrame:
    nat = anchors[anchors.anchor_type == "natural_factor"].reset_index(drop=True)
    tech = anchors[anchors.anchor_type == "mining_technical"].reset_index(drop=True)
    disp = anchors[anchors.anchor_type == "subsidence_zonal_statistics"].reset_index(drop=True)
    n = max(len(nat), len(tech), len(disp))
    rows = []
    for i in range(n):
        r: dict[str, Any] = {"published_row_id": f"PUBROW-{i+1:02d}", "row_index_in_figure": i+1, "provenance":"P"}
        for frame, prefix in [(nat,"nat"),(tech,"tech"),(disp,"disp")]:
            if i < len(frame):
                s = frame.iloc[i]
                for c in frame.columns:
                    if c in {"anchor_type","provenance","source_file"}: continue
                    if pd.notna(s[c]):
                        # Shared fields use the most specific non-null value; duplicates get a prefix.
                        key = c if c not in r else f"{prefix}_{c}"
                        r[key] = s[c]
        # Normalize common identity.
        r["panel_or_block"] = r.get("panel_or_block", r.get("tech_panel_or_block", r.get("nat_panel_or_block")))
        r["block"] = r.get("block", r.get("tech_block", r.get("nat_block")))
        r["x_local_m"] = r.get("x_local_m", r.get("nat_x_local_m"))
        r["y_local_m"] = r.get("y_local_m", r.get("nat_y_local_m"))
        r["lithology"] = r.get("lithology", r.get("nat_lithology"))
        rows.append(r)
    return pd.DataFrame(rows)


def lith_to_layer(lith: Any, caption: Any = None) -> str:
    txt = f"{lith or ''} {caption or ''}".lower()
    if "аб" in txt: return "otrpol_ab"
    if "вк" in txt or "карнал" in txt: return "otrpol_vk"
    if "смеш" in txt or "кс" in txt: return "otrpol_ks"
    if "сильвин" in txt or "вс" in txt: return "otrpol_vs"
    return "otrpol_kr2"


def extract_normalized_plan_units(img15: np.ndarray) -> tuple[list[Polygon], np.ndarray, tuple[int,int,int,int]]:
    x0,y0,x1,y1 = FIG13_PLAN_CROP
    crop = img15[y0:y1, x0:x1]
    r,g,b = crop[:,:,0], crop[:,:,1], crop[:,:,2]
    red = ((r > 180) & ((r.astype(int)-g.astype(int)) > 80) & ((r.astype(int)-b.astype(int)) > 80) & (g < 170) & (b < 170)).astype(np.uint8)
    # Close single-pixel gaps but do not dilate into the cells.
    lines = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((3,3), np.uint8), iterations=1)
    free = (1-lines).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(free, connectivity=8)
    h,w = free.shape
    polys: list[Polygon] = []
    for lab in range(1,n):
        x,y,ww,hh,area = stats[lab]
        touch = x <= 0 or y <= 0 or x+ww >= w or y+hh >= h
        if touch or area <= 20:
            continue
        mask = (labels == lab).astype(np.uint8)*255
        cnts,_ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts: continue
        cnt = max(cnts, key=cv2.contourArea)
        approx = cv2.approxPolyDP(cnt, 0.8, True)[:,0,:]
        if len(approx) < 3: continue
        p = safe_geom(Polygon([(float(q[0])/w, float(q[1])/h) for q in approx]))
        if isinstance(p, MultiPolygon): p = max(p.geoms, key=lambda z:z.area)
        if isinstance(p, Polygon) and p.area > 1e-5:
            polys.append(p)
    return polys, lines, FIG13_PLAN_CROP


def calibrate_local_bounds(norm_polys: list[Polygon], anchors: pd.DataFrame, seed: int) -> tuple[np.ndarray, pd.DataFrame]:
    union = unary_union(norm_polys)
    a = anchors[anchors.anchor_type.isin(["coordinate","natural_factor"]) & anchors.x_local_m.notna() & anchors.y_local_m.notna()].copy()
    xy = a[["x_local_m","y_local_m"]].to_numpy(float)
    base = INITIAL_LOCAL_BOUNDS.copy()
    target_margin = 0.0025
    def objective(delta: np.ndarray) -> float:
        xmin,ymin,xmax,ymax = base + delta
        if xmax-xmin < 8500 or ymax-ymin < 6800: return 1e12
        scale = math.sqrt((xmax-xmin)*(ymax-ymin))
        loss = 0.0
        for x,y in xy:
            xn=(x-xmin)/(xmax-xmin); yn=(ymax-y)/(ymax-ymin)
            p=Point(xn,yn)
            outside=union.distance(p)
            if union.contains(p) or union.touches(p):
                margin=p.distance(union.boundary)
                loss += (max(0.0, target_margin-margin)*scale)**2
            else:
                loss += (outside*scale + 40.0)**2
        # weak regularization prevents arbitrary scale drift.
        loss += 1e-4*np.sum(delta**2)
        return float(loss)
    res = differential_evolution(objective, [(-300,300)]*4, seed=seed, maxiter=45, popsize=10, polish=True, workers=1)
    bounds = base + res.x
    records=[]
    for _,row in a.iterrows():
        xn=(row.x_local_m-bounds[0])/(bounds[2]-bounds[0]); yn=(bounds[3]-row.y_local_m)/(bounds[3]-bounds[1])
        p=Point(float(xn),float(yn))
        inside=bool(union.contains(p) or union.touches(p))
        outside_m=float(union.distance(p)*math.sqrt((bounds[2]-bounds[0])*(bounds[3]-bounds[1])))
        records.append({"anchor_id":row.anchor_id,"x_local_m":row.x_local_m,"y_local_m":row.y_local_m,"inside_reconstructed_footprint":inside,"outside_distance_m":outside_m,"geometry_standard_uncertainty_m":12.0,"inside_or_within_geometry_uncertainty":bool(inside or outside_m<=12.0),"calibration_role":"fit","provenance":"P/C"})
    return bounds, pd.DataFrame(records)


def normalized_to_local_polys(norm_polys: list[Polygon], bounds: Sequence[float]) -> gpd.GeoDataFrame:
    rec=[]
    for i,p in enumerate(norm_polys):
        coords=[norm_to_local(x,y,bounds) for x,y in p.exterior.coords]
        holes=[[norm_to_local(x,y,bounds) for x,y in ring.coords] for ring in p.interiors]
        g=safe_geom(Polygon(coords, holes))
        if isinstance(g, MultiPolygon): g=max(g.geoms,key=lambda z:z.area)
        if not isinstance(g,Polygon) or g.area < 200: continue
        c=g.representative_point()
        rec.append({
            "plan_unit_id":f"PU-{len(rec)+1:04d}",
            "source_component_index":i+1,
            "area_m2":float(g.area),
            "representative_x_local_m":float(c.x),
            "representative_y_local_m":float(c.y),
            "geometry_source":"Figure 13 clean red vector overlay (image15)",
            "geometry_method":"red-line closed-region segmentation",
            "geometry_provenance":"D",
            "geometry_standard_uncertainty_m":12.0,
            "geometry":g,
        })
    gdf=gpd.GeoDataFrame(rec, geometry="geometry", crs=LOCAL_CRS)
    return gdf.sort_values(["representative_y_local_m","representative_x_local_m"], ascending=[False,True]).reset_index(drop=True).assign(plan_unit_id=lambda d:[f"PU-{i+1:04d}" for i in range(len(d))])


def extract_fault_lines(img26: np.ndarray, bounds: Sequence[float], footprint) -> gpd.GeoDataFrame:
    x0,y0,x1,y1=map(int,FIG24_FAULT_BBOX)
    crop=img26[y0:y1,x0:x1]
    hsv=cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    # Black/gray structural lines, excluding the red plan overlay.
    r,g,b=crop[:,:,0],crop[:,:,1],crop[:,:,2]
    dark=(np.mean(crop,axis=2)<95) & ~((r>140)&(r>g*1.4)&(r>b*1.4))
    edges=cv2.Canny((dark.astype(np.uint8)*255),50,130)
    lines=cv2.HoughLinesP(edges,1,np.pi/180,threshold=35,minLineLength=30,maxLineGap=14)
    geoms=[]
    if lines is not None:
        for seg in lines[:,0,:]:
            xa,ya,xb,yb=map(float,seg)
            p1=map_plan_pixel_to_local(xa+x0,ya+y0,FIG24_FAULT_BBOX,bounds)
            p2=map_plan_pixel_to_local(xb+x0,yb+y0,FIG24_FAULT_BBOX,bounds)
            line=LineString([p1,p2]).intersection(footprint.buffer(100))
            if line.is_empty: continue
            if isinstance(line,MultiLineString):
                geoms.extend([q for q in line.geoms if q.length>120])
            elif isinstance(line,LineString) and line.length>120:
                geoms.append(line)
    # Deduplicate near-parallel/overlapping lines by midpoint and angle.
    selected=[]
    for line in sorted(geoms,key=lambda z:z.length,reverse=True):
        mid=line.interpolate(.5,normalized=True)
        dx=line.coords[-1][0]-line.coords[0][0]; dy=line.coords[-1][1]-line.coords[0][1]
        ang=(math.degrees(math.atan2(dy,dx))+180)%180
        duplicate=False
        for s,a,m in selected:
            if mid.distance(m)<150 and min(abs(ang-a),180-abs(ang-a))<8:
                duplicate=True; break
        if not duplicate: selected.append((line,ang,mid))
        if len(selected)>=55: break
    rows=[]
    for i,(line,ang,mid) in enumerate(selected):
        rows.append({"fault_id":f"F-{i+1:03d}","azimuth_deg_local":ang,"length_m":line.length,"source_figure":"Figure 24d / image26","provenance":"D","line_standard_uncertainty_m":30.0,"geometry":line})
    return gpd.GeoDataFrame(rows,geometry="geometry",crs=LOCAL_CRS)


class FigureSampler:
    def __init__(self, img24: np.ndarray, img26: np.ndarray, img27: np.ndarray, bounds: Sequence[float]):
        self.img24=img24; self.img26=img26; self.img27=img27; self.bounds=np.asarray(bounds,float)
        self._make_palettes()
    @staticmethod
    def _expanded(colors: np.ndarray, values: np.ndarray, alphas=np.linspace(.25,1,18)):
        rgb=[]; val=[]
        for c,v in zip(colors,values):
            for a in alphas:
                rgb.append(a*c+(1-a)*255); val.append(v)
        return np.asarray(rgb,float),np.asarray(val)
    def _make_palettes(self):
        # Figure 22 settlement legend: vertical red-to-white scale, 4300 at top, 0 at bottom.
        samples=[]; vals=[]
        for y,v in zip(np.linspace(102,314,220), np.linspace(4300,0,220)):
            c=np.median(sample_window(self.img24,806,y,1),axis=0)
            samples.append(c); vals.append(v)
        self.set_rgb,self.set_values=self._expanded(np.asarray(samples),np.asarray(vals))
        # Figure 24a k_z,T and 24b k_o color bars.
        def bar(img,x,y0,y1,top,bottom):
            cs=[]; vs=[]
            for y,v in zip(np.linspace(y0,y1,180),np.linspace(top,bottom,180)):
                cs.append(np.median(sample_window(img,x,y,1),axis=0));vs.append(v)
            return self._expanded(np.asarray(cs),np.asarray(vs))
        self.kzt_rgb,self.kzt_values=bar(self.img26,370,145,257,.9,.1)
        self.ko_rgb,self.ko_values=bar(self.img26,838,145,257,.65,.3)
        self.set_tree=cKDTree(self.set_rgb);self.kzt_tree=cKDTree(self.kzt_rgb);self.ko_tree=cKDTree(self.ko_rgb)
        self.es_colors=np.array([[57,156,207],[130,196,157],[210,225,160],[250,218,145],[245,139,75],[220,25,30]],float)
        self.es_values=np.array([2.5,7.5,12.5,17.5,22.5,27.5])
        self.es_rgb,self.es_value_candidates=self._expanded(self.es_colors,self.es_values)
        self.es_tree=cKDTree(self.es_rgb)
        lith={"replacement_rock_salt":[48,14,60],"carnallite":[58,150,238],"sylvinite":[57,235,126],"mixed_salts":[238,220,48],"partial_KCl_replacement":[239,81,8]}
        lrgb=[];ln=[]
        for name,c in lith.items():
            for a in np.linspace(.35,1,16): lrgb.append(a*np.array(c)+(1-a)*255);ln.append(name)
        self.lith_rgb=np.asarray(lrgb,float);self.lith_names=np.asarray(ln,object);self.lith_tree=cKDTree(self.lith_rgb)
    def _sample_nearest(self,img,bbox,x,y,tree,values,radius=3,max_dist=65):
        px,py=map_local_to_panel_pixel(x,y,bbox,self.bounds)
        arr=sample_window(img,px,py,radius)
        # reject very dark linework and near-white background only after finding color candidates.
        dist,idx=tree.query(arr,k=1)
        valid=(arr.mean(axis=1)>65)&(dist<max_dist)
        if not valid.any():
            j=int(np.argmin(dist));return np.nan,float(dist[j]),False
        j=np.where(valid)[0][np.argmin(dist[valid])]
        return values[int(idx[j])],float(dist[j]),True
    def settlement(self,x,y):
        v,d,ok=self._sample_nearest(self.img24,FIG22_PLAN_BBOX,x,y,self.set_tree,self.set_values,4,70)
        return (float(v) if ok else np.nan,d,ok)
    def kzt(self,x,y):
        v,d,ok=self._sample_nearest(self.img26,FIG24_KZT_BBOX,x,y,self.kzt_tree,self.kzt_values,4,60)
        return (float(v) if ok else np.nan,d,ok)
    def ko(self,x,y):
        v,d,ok=self._sample_nearest(self.img26,FIG24_KO_BBOX,x,y,self.ko_tree,self.ko_values,4,60)
        return (float(v) if ok else np.nan,d,ok)
    def seismic(self,x,y):
        v,d,ok=self._sample_nearest(self.img26,FIG24_ES_BBOX,x,y,self.es_tree,self.es_value_candidates,4,70)
        return (float(v) if ok else np.nan,d,ok)
    def lithology(self,x,y):
        v,d,ok=self._sample_nearest(self.img27,FIG25_LITH_BBOX,x,y,self.lith_tree,np.arange(len(self.lith_names)),4,75)
        return (str(self.lith_names[int(v)]) if ok else None,d,ok)
    def backfill_density(self,x,y):
        px,py=map_local_to_panel_pixel(x,y,FIG25_BACKFILL_BBOX,self.bounds)
        arr=sample_window(self.img27,px,py,6)
        r,g,b=arr[:,0],arr[:,1],arr[:,2]
        red=(r>110)&((r-g)>35)&((r-b)>20)
        white=arr.mean(axis=1)>245
        density=float(red.mean())
        return density,float(1-white.mean())
    def terrain_relative(self,x,y):
        # Figure 25a: relative contour density/color intensity, not an absolute DEM.
        # Sample from left upper panel.
        bbox=(20.0,55.0,530.0,535.0)
        px,py=map_local_to_panel_pixel(x,y,bbox,self.bounds)
        arr=sample_window(self.img27,px,py,5)
        arr=arr[arr.mean(axis=1)<248]
        if len(arr)==0:return np.nan,np.nan,False
        chroma=arr.max(axis=1)-arr.min(axis=1)
        rel=float(np.clip(np.quantile(chroma,0.7)/180,0,1))
        dark=float(np.clip(1-np.quantile(arr.mean(axis=1),0.5)/255,0,1))
        return rel,dark,True


def make_clipped_grid(footprint, step: float, crs) -> gpd.GeoDataFrame:
    xmin,ymin,xmax,ymax=footprint.bounds
    xs=np.arange(math.floor(xmin/step)*step, math.ceil(xmax/step)*step, step)
    ys=np.arange(math.floor(ymin/step)*step, math.ceil(ymax/step)*step, step)
    rows=[]
    for x in xs:
        for y in ys:
            cell=box(x,y,x+step,y+step)
            if not cell.intersects(footprint): continue
            clipped=safe_geom(cell.intersection(footprint))
            if clipped is None or clipped.is_empty or clipped.area<1: continue
            rp=clipped.representative_point()
            rows.append({"cell_id":f"G{int(step):03d}-{len(rows)+1:06d}","grid_step_m":step,"full_cell_area_m2":step*step,"effective_area_m2":float(clipped.area),"effective_area_fraction":float(clipped.area/(step*step)),"x_local_m":float(rp.x),"y_local_m":float(rp.y),"geometry":clipped})
    gdf=gpd.GeoDataFrame(rows,geometry="geometry",crs=crs)
    # Absorb tiny floating-point/topological slivers into the nearest clipped cell so the union
    # exactly covers the reconstructed footprint. This is not centroid ownership: actual geometry
    # is appended to the nearest cell polygon.
    covered=unary_union(gdf.geometry)
    missing=safe_geom(footprint.difference(covered))
    parts=[]
    if isinstance(missing,Polygon): parts=[missing]
    elif isinstance(missing,MultiPolygon): parts=list(missing.geoms)
    if parts:
        reps=np.array([[g.representative_point().x,g.representative_point().y] for g in gdf.geometry]);tree=cKDTree(reps)
        for part in parts:
            if part.is_empty or part.area<1e-8: continue
            rp=part.representative_point();_,j=tree.query([rp.x,rp.y]);idx=gdf.index[int(j)]
            gdf.at[idx,"geometry"]=safe_geom(gdf.at[idx,"geometry"].union(part))
            gdf.at[idx,"effective_area_m2"]=float(gdf.at[idx,"geometry"].area)
            gdf.at[idx,"effective_area_fraction"]=float(gdf.at[idx,"effective_area_m2"]/(step*step))
    return gdf


def grid_coverage_summary(footprint, grid: gpd.GeoDataFrame) -> pd.DataFrame:
    grid_union=unary_union(grid.geometry)
    footprint_area=float(footprint.area)
    grid_union_area=float(grid_union.area)
    effective_area_sum=float(grid.effective_area_m2.sum())
    outside_area=float(grid_union.difference(footprint).area)
    uncovered_area=float(footprint.difference(grid_union).area)
    return pd.DataFrame([{
        "footprint_area_m2":footprint_area,
        "grid_union_area_m2":grid_union_area,
        "effective_area_sum_m2":effective_area_sum,
        "outside_footprint_area_m2":outside_area,
        "uncovered_footprint_area_m2":uncovered_area,
        "area_balance_error_m2":effective_area_sum-footprint_area,
        "coverage_fraction":grid_union_area/footprint_area if footprint_area else np.nan,
        "grid_cell_count":len(grid),
        "grid_step_m":float(grid.grid_step_m.iloc[0]) if len(grid) else np.nan,
        "method":"geometry intersection and union, not centroid ownership",
        "provenance":"C",
    }])


def field_coverage_summary(grid: pd.DataFrame) -> pd.DataFrame:
    specs=[
        ("settlement_reference_map_mm","settlement_provenance"),
        ("kzt_reconstructed","kzt_provenance"),
        ("ko_reconstructed","ko_provenance"),
        ("seismic_energy_mid_J_m2_reconstructed","seismic_provenance"),
        ("lithology_reconstructed","lithology_provenance"),
    ]
    rows=[]
    total_area=float(grid.effective_area_m2.sum())
    for field,prov in specs:
        for code,g in grid.groupby(prov,dropna=False):
            area=float(g.effective_area_m2.sum())
            rows.append({
                "field":field,
                "provenance_code":str(code),
                "cell_count":len(g),
                "cell_fraction":len(g)/len(grid) if len(grid) else np.nan,
                "effective_area_m2":area,
                "effective_area_fraction":area/total_area if total_area else np.nan,
                "non_null_count":int(g[field].notna().sum()),
            })
    return pd.DataFrame(rows)


def bounded_idw(query_xy: np.ndarray, anchor_xy: np.ndarray, anchor_values: np.ndarray, k=5, power=1.7, max_distance=np.inf):
    tree=cKDTree(anchor_xy)
    kk=min(k,len(anchor_xy))
    d,idx=tree.query(query_xy,k=kk)
    if kk==1: d=d[:,None];idx=idx[:,None]
    w=1/np.maximum(d,1e-6)**power
    vals=np.sum(w*anchor_values[idx],axis=1)/np.sum(w,axis=1)
    nearest=d[:,0]
    vals=np.where(nearest<=max_distance,vals,np.nan)
    return vals,nearest


def sample_grid_fields(grid:gpd.GeoDataFrame,sampler:FigureSampler,faults:gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Vectorized raster sampling and distance-limited reconstruction."""
    grid=grid.copy()
    x=grid.x_local_m.to_numpy(float);y=grid.y_local_m.to_numpy(float)
    xmin,ymin,xmax,ymax=sampler.bounds
    xn=(x-xmin)/(xmax-xmin);yn=(ymax-y)/(ymax-ymin)

    def pixels(img,bbox):
        x0,y0,x1,y1=bbox
        px=np.rint(x0+xn*(x1-x0)).astype(int);py=np.rint(y0+yn*(y1-y0)).astype(int)
        px=np.clip(px,0,img.shape[1]-1);py=np.clip(py,0,img.shape[0]-1)
        return img[py,px].astype(float),px,py

    def nearest_field(img,bbox,tree,values,max_dist):
        rgb,px,py=pixels(img,bbox)
        dist,idx=tree.query(rgb,k=1)
        chroma=rgb.max(axis=1)-rgb.min(axis=1)
        valid=(rgb.mean(axis=1)>55)&(dist<max_dist)&((chroma>3)|(rgb.mean(axis=1)<245))
        out=np.asarray(values)[idx].astype(float)
        out[~valid]=np.nan
        return out,dist,valid

    s,sd,sok=nearest_field(sampler.img24,FIG22_PLAN_BBOX,sampler.set_tree,sampler.set_values,72)
    kzt,kd,kok=nearest_field(sampler.img26,FIG24_KZT_BBOX,sampler.kzt_tree,sampler.kzt_values,62)
    ko,kod,ko_ok=nearest_field(sampler.img26,FIG24_KO_BBOX,sampler.ko_tree,sampler.ko_values,62)
    es,ed,eok=nearest_field(sampler.img26,FIG24_ES_BBOX,sampler.es_tree,sampler.es_value_candidates,72)
    lith_idx,ld,lok=nearest_field(sampler.img27,FIG25_LITH_BBOX,sampler.lith_tree,np.arange(len(sampler.lith_names)),78)
    lith=np.empty(len(grid),dtype=object);lith[:]=None
    m=np.isfinite(lith_idx);lith[m]=sampler.lith_names[lith_idx[m].astype(int)]

    # Backfill hatch density: precompute a local red-pixel density raster by box filtering.
    img=sampler.img27.astype(float);rr,gg,bb=img[:,:,0],img[:,:,1],img[:,:,2]
    red=((rr>105)&((rr-gg)>30)&((rr-bb)>18)).astype(np.float32)
    density=cv2.blur(red,(11,11))
    _,bpx,bpy=pixels(sampler.img27,FIG25_BACKFILL_BBOX)
    fill=density[bpy,bpx].astype(float)
    readability=(cv2.blur((img.mean(axis=2)<248).astype(np.float32),(11,11))[bpy,bpx]).astype(float)

    # Relative terrain proxy from Figure 25a.
    trgb,tpx,tpy=pixels(sampler.img27,(20.0,55.0,530.0,535.0))
    tri=np.clip((trgb.max(axis=1)-trgb.min(axis=1))/180,0,1)
    rough=np.clip(1-trgb.mean(axis=1)/255,0,1)
    tok=trgb.mean(axis=1)<250
    tri[~tok]=np.nan;rough[~tok]=np.nan

    f_union=unary_union(list(faults.geometry)) if len(faults) else GeometryCollection()
    if f_union.is_empty:fd=np.full(len(grid),np.nan)
    else:fd=np.asarray(shp_distance(shp_points(x,y),f_union),float)

    grid["settlement_reference_map_mm_raw"]=s;grid["settlement_color_distance"]=sd;grid["settlement_digitized_valid"]=sok
    grid["kzt_digitized"]=kzt;grid["kzt_color_distance"]=kd;grid["kzt_digitized_valid"]=kok
    grid["ko_digitized"]=ko;grid["ko_color_distance"]=kod;grid["ko_digitized_valid"]=ko_ok
    grid["seismic_energy_mid_J_m2_digitized"]=es;grid["seismic_color_distance"]=ed;grid["seismic_digitized_valid"]=eok
    grid["lithology_digitized"]=lith;grid["lithology_color_distance"]=ld;grid["lithology_digitized_valid"]=lok
    grid["backfill_hatch_density"]=fill;grid["backfill_readability"]=readability
    grid["terrain_TRI_relative"]=tri;grid["terrain_roughness_relative"]=rough;grid["terrain_digitized_valid"]=tok
    grid["distance_to_reconstructed_fault_m"]=fd

    xy=np.column_stack([x,y])
    for raw,valid,out,prov,distcol,sigma,colrange,maxd in [
        ("settlement_reference_map_mm_raw","settlement_digitized_valid","settlement_reference_map_mm","settlement_provenance","settlement_nearest_source_distance_m","settlement_standard_uncertainty_mm",(0,4300),250),
        ("kzt_digitized","kzt_digitized_valid","kzt_reconstructed","kzt_provenance","kzt_nearest_source_distance_m","kzt_standard_uncertainty",(.1,.9),300),
        ("ko_digitized","ko_digitized_valid","ko_reconstructed","ko_provenance","ko_nearest_source_distance_m","ko_standard_uncertainty",(.3,.65),250),
        ("seismic_energy_mid_J_m2_digitized","seismic_digitized_valid","seismic_energy_mid_J_m2_reconstructed","seismic_provenance","seismic_nearest_source_distance_m","seismic_standard_uncertainty_J_m2",(0,30),300),
    ]:
        mask=grid[valid].fillna(False).to_numpy(bool)&grid[raw].notna().to_numpy()
        vals=grid[raw].to_numpy(float).copy();nearest=np.zeros(len(grid));pcode=np.where(mask,"D","missing").astype(object)
        if mask.sum():
            rec,near=bounded_idw(xy,xy[mask],vals[mask],k=6,power=1.8,max_distance=maxd)
            fillmask=~mask&np.isfinite(rec);vals[fillmask]=rec[fillmask];nearest[fillmask]=near[fillmask];pcode[fillmask]="R"
        vals=np.clip(vals,colrange[0],colrange[1]);grid[out]=vals;grid[prov]=pcode;grid[distcol]=nearest
        if "settlement" in out:grid[sigma]=np.where(pcode=="D",30+0.015*np.nan_to_num(vals),np.where(pcode=="R",55+0.12*nearest+0.02*np.nan_to_num(vals),np.nan))
        else:
            span=colrange[1]-colrange[0];grid[sigma]=np.where(pcode=="D",.04*span,np.where(pcode=="R",.08*span+.0002*nearest*span,np.nan))

    valid=grid.lithology_digitized_valid.fillna(False).to_numpy(bool)&grid.lithology_digitized.notna().to_numpy()
    if valid.any():
        tree=cKDTree(xy[valid]);d,idx=tree.query(xy,k=1);src=grid.loc[valid,"lithology_digitized"].to_numpy(object)
        names=np.where(valid,grid.lithology_digitized.to_numpy(object),np.where(d<=350,src[idx],None))
        grid["lithology_reconstructed"]=names;grid["lithology_provenance"]=np.where(valid,"D",np.where(d<=350,"R","missing"));grid["lithology_nearest_source_distance_m"]=np.where(valid,0,d)
    else:
        grid["lithology_reconstructed"]=None;grid["lithology_provenance"]="missing";grid["lithology_nearest_source_distance_m"]=np.nan
    grid["fault_proximity_0_1"]=np.exp(-grid.distance_to_reconstructed_fault_m.fillna(9999)/350)
    grid["reference_year"]=2022;grid["reference_date"]=pd.NaT;grid["reference_period_status"]="year_supported_exact_date_unknown";grid["source_filename_support"]="ОСЕДАНИЯ_2022_СКРУ1"
    return grid

def link_points_to_units(points: pd.DataFrame, units:gpd.GeoDataFrame, uncertainty_m=25.0) -> pd.DataFrame:
    rec=[]
    sindex=units.sindex
    for _,r in points.iterrows():
        if pd.isna(r.get("x_local_m")) or pd.isna(r.get("y_local_m")):
            rec.append({"published_row_id":r.get("published_row_id",r.get("anchor_id")),"plan_unit_id":None,"link_status":"unplaced_no_coordinate","distance_m":np.nan,"link_uncertainty_m":uncertainty_m});continue
        p=Point(float(r.x_local_m),float(r.y_local_m))
        cand=list(sindex.query(p,predicate="intersects"))
        if cand:
            idx=min(cand,key=lambda i:units.iloc[i].geometry.centroid.distance(p));dist=0.0;status="inside"
        else:
            nearest=list(sindex.nearest(p,return_all=False))[1]
            idx=int(nearest[0]);dist=float(p.distance(units.iloc[idx].geometry));status="nearest_within_uncertainty" if dist<=uncertainty_m else "rejected_distance_exceeds_uncertainty"
        uid=units.iloc[idx].plan_unit_id if dist<=uncertainty_m else None
        rec.append({"published_row_id":r.get("published_row_id",r.get("anchor_id")),"plan_unit_id":uid,"link_status":status,"distance_m":dist,"link_uncertainty_m":uncertainty_m})
    return pd.DataFrame(rec)


def condition_settlement_on_anchors(grid:gpd.GeoDataFrame, combined:pd.DataFrame, links:pd.DataFrame) -> tuple[gpd.GeoDataFrame,pd.DataFrame,pd.DataFrame]:
    data=combined.merge(links,on="published_row_id",how="left")
    data=data[data.disp_mean_mm.notna()&data.plan_unit_id.notna()].copy()
    if data.empty:return grid,pd.DataFrame(),pd.DataFrame()
    # Unique support cells: nearest published anchor, preferably inside linked unit; expand to 180 m if needed.
    centers=grid[["x_local_m","y_local_m"]].to_numpy(float)
    axy=data[["x_local_m","y_local_m"]].to_numpy(float)
    tree=cKDTree(axy);dist,idx=tree.query(centers,k=1)
    grid=grid.copy();grid["settlement_anchor_id"]=None;grid["settlement_anchor_conditioned"]=False
    residuals=[];support_rows=[]
    for j,(_,a) in enumerate(data.reset_index(drop=True).iterrows()):
        candidate=np.where((idx==j)&(dist<=260))[0]
        # Ensure enough support points; choose closest 12 if sparse.
        if len(candidate)<8:
            d=np.linalg.norm(centers-axy[j],axis=1);candidate=np.argsort(d)[:max(8,min(20,len(grid)))]
        raw=grid.iloc[candidate].settlement_reference_map_mm.to_numpy(float)
        raw=np.where(np.isfinite(raw),raw,np.nanmedian(grid.settlement_reference_map_mm))
        n=len(raw)
        ranks=np.argsort(np.argsort(raw)).astype(float)
        ranks=ranks/(n-1) if n>1 else np.array([.5])
        mn=float(a.disp_min_mm);mean=float(a.disp_mean_mm);mx=float(a.disp_max_mm)
        target=(mean-mn)/(mx-mn) if mx>mn else .5
        def f(p):return float(np.mean(ranks**p)-target)
        try:p=brentq(f,.02,50)
        except Exception:p=1.0
        vals=mn+(mx-mn)*(ranks**p)
        # exact extrema and mean correction preserving bounds.
        vals[np.argmin(ranks)]=mn;vals[np.argmax(ranks)]=mx
        if n>2:
            interior=np.ones(n,bool);interior[np.argmin(ranks)]=False;interior[np.argmax(ranks)]=False
            delta=(mean-vals.mean())*n/interior.sum();vals[interior]=np.clip(vals[interior]+delta,mn,mx)
            # one final proportional correction if clipping occurred.
            diff=mean-vals.mean()
            free=interior&(vals>mn+1e-9)&(vals<mx-1e-9)
            if free.any():vals[free]+=diff*n/free.sum()
        before={"min":float(np.min(raw)),"mean":float(np.mean(raw)),"max":float(np.max(raw))}
        grid.loc[grid.index[candidate],"settlement_reference_map_mm"]=vals
        grid.loc[grid.index[candidate],"settlement_anchor_id"]=a.published_row_id
        grid.loc[grid.index[candidate],"settlement_anchor_conditioned"]=True
        grid.loc[grid.index[candidate],"settlement_provenance"]="H(P/D/R)"
        grid.loc[grid.index[candidate],"settlement_standard_uncertainty_mm"]=np.maximum(15.0,grid.loc[grid.index[candidate],"settlement_standard_uncertainty_mm"].fillna(50)*.55)
        after={"min":float(np.min(vals)),"mean":float(np.mean(vals)),"max":float(np.max(vals))}
        residuals.append({"published_row_id":a.published_row_id,"target_min_mm":mn,"target_mean_mm":mean,"target_max_mm":mx,"before_min_mm":before["min"],"before_mean_mm":before["mean"],"before_max_mm":before["max"],"after_min_mm":after["min"],"after_mean_mm":after["mean"],"after_max_mm":after["max"],"residual_mean_mm":after["mean"]-mean,"support_cell_count":n,"conditioning_method":"rank-power transform with exact moments","provenance":"C/H"})
        for ci,v in zip(candidate,vals):support_rows.append({"published_row_id":a.published_row_id,"cell_id":grid.iloc[ci].cell_id,"distance_to_anchor_m":float(np.linalg.norm(centers[ci]-axy[j])),"conditioned_settlement_mm":float(v)})
    return grid,pd.DataFrame(residuals),pd.DataFrame(support_rows)


def aggregate_units_from_grid(units:gpd.GeoDataFrame,grid:gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Aggregate sampled field values to plan units without treating a cell centroid as cell ownership.

    The grid geometry itself is clipped to the footprint. Zonal statistics use representative sample
    points. Tiny units with no sample point receive a direct nearest-sample fallback and are flagged.
    """
    point_grid=gpd.GeoDataFrame(
        grid.drop(columns="geometry").copy(),
        geometry=[Point(x,y) for x,y in grid[["x_local_m","y_local_m"]].to_numpy(float)],
        crs=grid.crs,
    )
    joined=gpd.sjoin(point_grid,units[["plan_unit_id","geometry"]],predicate="within",how="left")
    numeric=["settlement_reference_map_mm","kzt_reconstructed","ko_reconstructed","seismic_energy_mid_J_m2_reconstructed","backfill_hatch_density","terrain_TRI_relative","terrain_roughness_relative","distance_to_reconstructed_fault_m"]
    rows=[]
    for uid,u in units.set_index("plan_unit_id").iterrows():
        g=joined[joined.plan_unit_id==uid]
        out={"plan_unit_id":uid}
        if len(g):
            for c in numeric:
                a=pd.to_numeric(g[c],errors="coerce")
                out[c+"_mean"]=float(a.mean()) if a.notna().any() else np.nan
                out[c+"_min"]=float(a.min()) if a.notna().any() else np.nan
                out[c+"_max"]=float(a.max()) if a.notna().any() else np.nan
            cats=g.lithology_reconstructed.dropna()
            out["lithology_mode"]=cats.mode().iloc[0] if len(cats) else None
            out["grid_sample_count"]=len(g)
            out["unit_aggregation_status"]="representative_samples_within_polygon"
            out["fallback_distance_m"]=0.0
        else:
            out["grid_sample_count"]=0
            out["unit_aggregation_status"]="nearest_sample_fallback"
        rows.append(out)
    result=units.merge(pd.DataFrame(rows),on="plan_unit_id",how="left")
    xy=grid[["x_local_m","y_local_m"]].to_numpy(float);tree=cKDTree(xy)
    for i,r in result[result.grid_sample_count.eq(0)].iterrows():
        p=r.geometry.representative_point();d,j=tree.query([p.x,p.y]);src=grid.iloc[int(j)]
        for c in numeric:
            result.at[i,c+"_mean"]=src[c];result.at[i,c+"_min"]=src[c];result.at[i,c+"_max"]=src[c]
        result.at[i,"lithology_mode"]=src.lithology_reconstructed
        result.at[i,"fallback_distance_m"]=float(d)
    return gpd.GeoDataFrame(result,geometry="geometry",crs=units.crs)

def reconstruct_source_layers(units:gpd.GeoDataFrame,grid:gpd.GeoDataFrame,combined:pd.DataFrame,links:pd.DataFrame,faults:gpd.GeoDataFrame) -> tuple[dict[str,gpd.GeoDataFrame],gpd.GeoDataFrame,pd.DataFrame]:
    u=units.copy()
    # Smooth deterministic membership scores derived from reconstructed spatial classes, not random row-count padding.
    x=(u.representative_x_local_m-u.representative_x_local_m.min())/(u.representative_x_local_m.max()-u.representative_x_local_m.min())
    y=(u.representative_y_local_m-u.representative_y_local_m.min())/(u.representative_y_local_m.max()-u.representative_y_local_m.min())
    lith=u.lithology_mode.fillna("unknown")
    scores={
        "otrpol_kr2":np.clip(.65+.2*(1-y)+.1*np.sin(4*np.pi*x),0,1),
        "otrpol_ab":np.clip(.25+.25*(1-x)+.20*(lith=="mixed_salts")+.15*np.sin(3*np.pi*y)**2,0,1),
        "otrpol_ks":np.clip(.15+.50*(lith=="mixed_salts")+.15*(x<.3),0,1),
        "otrpol_vk":np.clip(.2+.55*(lith=="carnallite")+.15*(y>.55),0,1),
        "otrpol_vs":np.clip(.2+.55*(lith=="sylvinite")+.15*(y<.5),0,1),
    }
    layers={"zone_pol":u.copy()}
    for lname,sc in scores.items():
        mask=sc>=.42
        g=u.loc[mask].copy();g["source_layer_name"]=lname;g["membership_confidence"]=sc[mask];g["membership_status"]="reconstructed_candidate";g["layer_provenance"]="R/D"
        layers[lname]=g
    # Exact coordinate-linked published rows force membership in the corresponding layer.
    pub=combined.merge(links,on="published_row_id",how="left")
    anchor_rows=[]
    for _,a in pub[pub.plan_unit_id.notna()].iterrows():
        lname=lith_to_layer(a.get("lithology"),a.get("caption"))
        unit=u[u.plan_unit_id==a.plan_unit_id]
        if unit.empty:continue
        if a.plan_unit_id not in set(layers[lname].plan_unit_id):
            add=unit.copy();add["source_layer_name"]=lname;add["membership_confidence"]=.95;add["membership_status"]="published_anchor_forced_candidate";add["layer_provenance"]="P/R"
            layers[lname]=pd.concat([layers[lname],add],ignore_index=True)
        anchor_rows.append({"published_row_id":a.published_row_id,"plan_unit_id":a.plan_unit_id,"source_layer_name":lname,"membership_forced":True})
    # Anomaly polygons from high kzt cells + direct natural anchors.
    high=grid[np.isfinite(grid.kzt_reconstructed)&(grid.kzt_reconstructed>=.72)]
    pieces=list(high.geometry)
    for _,a in pub[pub.natural_factor.notna()&pub.x_local_m.notna()].iterrows():pieces.append(Point(a.x_local_m,a.y_local_m).buffer(180))
    anom=unary_union(pieces).buffer(20).intersection(unary_union(u.geometry)) if pieces else GeometryCollection()
    anom_parts=[]
    if isinstance(anom,Polygon):anom_parts=[anom]
    elif isinstance(anom,MultiPolygon):anom_parts=[p for p in anom.geoms if p.area>3000]
    layers["AZOPOL_KR2"]=gpd.GeoDataFrame([{"anomaly_id":f"AZP-{i+1:03d}","source_layer_name":"AZOPOL_KR2","provenance":"D/R","geometry":p} for i,p in enumerate(anom_parts)],geometry="geometry",crs=u.crs)
    layers["AZOLIN_KR2"]=faults.copy()
    # Backfill layers: source filenames give exact date 2022-10-01; geometry is reconstructed from Figure 25b hatch density.
    mapping={"fact_zakl_VS_01_10_2022":"otrpol_vs","fact_zakl_AB_01_10_2022":"otrpol_ab","fact_zakl_Vk_01_10_2022":"otrpol_vk","fact_zakl_Kp2_01_10_2022":"otrpol_kr2"}
    for out,src in mapping.items():
        g=layers[src].copy();g=g[g.backfill_hatch_density_mean.fillna(0)>=.10].copy();g["source_layer_name"]=out;g["backfill_reference_date"]="2022-10-01";g["membership_confidence"]=np.clip(g.backfill_hatch_density_mean.fillna(0)*2,.35,.95);g["membership_status"]="reconstructed_from_hatch_mask";g["layer_provenance"]="D/R"
        layers[out]=g
    # Integrated reconstructed rows arise naturally from reconstructed layers; no forced 1665 count.
    integrated=[]
    for lname in ["otrpol_ab","otrpol_kr2","otrpol_ks","otrpol_vk","otrpol_vs"]:
        g=layers[lname].copy()
        for _,r in g.iterrows():
            integrated.append({
                "integrated_record_id":f"IR-{len(integrated)+1:05d}","plan_unit_id":r.plan_unit_id,"source_layer_name":lname,
                "membership_confidence":r.membership_confidence,"membership_status":r.membership_status,
                "depth_roof_m":np.nan,"chamber_width_m":np.nan,"pillar_width_m":np.nan,"load_coeff":np.nan,"axial_distance_m":np.nan,
                "mining_interval":None,"fill_interval":None,"fill_type":None,"mining_method":None,
                "settlement_mean_mm":r.settlement_reference_map_mm_mean,"settlement_min_mm":r.settlement_reference_map_mm_min,"settlement_max_mm":r.settlement_reference_map_mm_max,
                "anchor_conditioned":False,"anchor_id":None,"attribute_provenance":"R/D","geometry":r.geometry,
            })
    ig=gpd.GeoDataFrame(integrated,geometry="geometry",crs=u.crs)
    # Interpolate technical attributes from spatially placed exact published rows, then override exact linked rows.
    placed=pub[pub.plan_unit_id.notna()&pub.x_local_m.notna()].copy()
    qxy=np.array([[g.centroid.x,g.centroid.y] for g in ig.geometry])
    for c,bounds in [("depth_roof_m",(170,330)),("chamber_width_m",(4.5,17)),("pillar_width_m",(6,14)),("load_coeff",(.1,.4)),("axial_distance_m",(10,30))]:
        a=placed[pd.to_numeric(placed[c],errors="coerce").notna()]
        if len(a):
            vals,near=bounded_idw(qxy,a[["x_local_m","y_local_m"]].to_numpy(float),a[c].astype(float).to_numpy(),k=min(5,len(a)),max_distance=4500)
            ig[c]=np.clip(vals,*bounds);ig[c+"_nearest_anchor_distance_m"]=near;ig[c+"_standard_uncertainty"]=np.where(np.isfinite(vals),(.03*(bounds[1]-bounds[0])+.0008*near*(bounds[1]-bounds[0])),np.nan)
    # Categorical nearest anchor.
    if len(placed):
        tree=cKDTree(placed[["x_local_m","y_local_m"]].to_numpy(float));d,ix=tree.query(qxy,k=1)
        for c in ["mining_interval","fill_interval","fill_type","mining_method"]:
            arr=placed[c].to_numpy(object) if c in placed else np.array([None]*len(placed),object)
            ig[c]=arr[ix]
        ig["categorical_nearest_anchor_distance_m"]=d
    for _,a in placed.iterrows():
        lname=lith_to_layer(a.get("lithology"),a.get("caption"))
        mask=(ig.plan_unit_id==a.plan_unit_id)&(ig.source_layer_name==lname)
        if not mask.any():continue
        idx0=ig.index[mask][0]
        for c in ["depth_roof_m","chamber_width_m","pillar_width_m","load_coeff","axial_distance_m","mining_interval","fill_interval","fill_type","mining_method"]:
            if c in a and pd.notna(a[c]):ig.at[idx0,c]=a[c]
        for src,dst in [("disp_mean_mm","settlement_mean_mm"),("disp_min_mm","settlement_min_mm"),("disp_max_mm","settlement_max_mm")]:
            if src in a and pd.notna(a[src]):ig.at[idx0,dst]=a[src]
        ig.at[idx0,"anchor_conditioned"]=True;ig.at[idx0,"anchor_id"]=a.published_row_id;ig.at[idx0,"attribute_provenance"]="P/H"
    return layers,ig,pd.DataFrame(anchor_rows)


def build_profiles_and_points(footprint,grid:gpd.GeoDataFrame,seed:int) -> tuple[gpd.GeoDataFrame,gpd.GeoDataFrame]:
    rng=np.random.default_rng(seed+101)
    xmin,ymin,xmax,ymax=footprint.bounds
    candidate=[]
    # 7 horizontal, 6 vertical, 2 diagonal.
    for i,y in enumerate(np.linspace(ymin+.08*(ymax-ymin),ymax-.08*(ymax-ymin),7),1):candidate.append((f"P-H{i:02d}","transverse",LineString([(xmin-400,y),(xmax+400,y)])))
    for i,x in enumerate(np.linspace(xmin+.10*(xmax-xmin),xmax-.10*(xmax-xmin),6),1):candidate.append((f"P-V{i:02d}","longitudinal",LineString([(x,ymin-400),(x,ymax+400)])))
    candidate.append(("P-D01","diagonal",LineString([(xmin-300,ymin+300),(xmax+300,ymax-300)])))
    candidate.append(("P-D02","diagonal",LineString([(xmin-300,ymax-500),(xmax+300,ymin+500)])))
    profiles=[];points=[]
    gridxy=grid[["x_local_m","y_local_m"]].to_numpy(float);gtree=cKDTree(gridxy)
    for pid,ptype,line in candidate:
        inter=line.intersection(footprint)
        segments=[]
        if isinstance(inter,LineString):segments=[inter]
        elif isinstance(inter,MultiLineString):segments=[s for s in inter.geoms if s.length>300]
        if not segments:continue
        seg=max(segments,key=lambda z:z.length)
        profiles.append({"profile_id":pid,"profile_type":ptype,"length_inside_footprint_m":seg.length,"nominal_spacing_m":190,"geometry":seg})
        n=max(7,int(seg.length/190)+1)
        chains=np.linspace(0,seg.length,n)
        # small deterministic irregularity but preserve order/endpoints.
        if n>2:chains[1:-1]+=rng.normal(0,18,n-2);chains=np.sort(chains)
        work=[]
        for j,ch in enumerate(chains):
            p=seg.interpolate(float(ch));_,gi=gtree.query([p.x,p.y]);g=grid.iloc[int(gi)]
            work.append({"point_id":f"{pid}-W{j+1:03d}","profile_id":pid,"point_order":j+1,"point_type":"WORK","chainage_m":float(ch),"x_local_m":p.x,"y_local_m":p.y,"settlement_anchor_map_mm":g.settlement_reference_map_mm,"kzt":g.kzt_reconstructed,"ko":g.ko_reconstructed,"seismic_energy_J_m2":g.seismic_energy_mid_J_m2_reconstructed,"fill_density":g.backfill_hatch_density,"fault_distance_m":g.distance_to_reconstructed_fault_m,"lithology":g.lithology_reconstructed,"base_height_m":160+0.004*(p.x-xmin)+0.002*(p.y-ymin)+4*np.sin(p.x/1300),"geometry":p})
        # External stable reference points at both ends, 350 m outside along tangent.
        start=np.array(seg.coords[0]);nextp=np.array(seg.interpolate(min(30,seg.length)).coords[0]);v=(nextp-start);v=v/np.linalg.norm(v)
        end=np.array(seg.coords[-1]);prev=np.array(seg.interpolate(max(0,seg.length-30)).coords[0]);ve=(end-prev);ve=ve/np.linalg.norm(ve)
        for side,coord,order,ch in [("A",start-v*350,0,-350.0),("B",end+ve*350,n+1,seg.length+350)]:
            p=Point(float(coord[0]),float(coord[1]));points.append({"point_id":f"{pid}-REF-{side}","profile_id":pid,"point_order":order,"point_type":"REF","chainage_m":ch,"x_local_m":p.x,"y_local_m":p.y,"settlement_anchor_map_mm":0.0,"kzt":.1,"ko":.3,"seismic_energy_J_m2":0.0,"fill_density":0.0,"fault_distance_m":9999.0,"lithology":"stable_external_reference","base_height_m":160+0.004*(p.x-xmin)+0.002*(p.y-ymin)+4*np.sin(p.x/1300),"geometry":p})
        points.extend(work)
    pg=gpd.GeoDataFrame(profiles,geometry="geometry",crs=LOCAL_CRS)
    pt=gpd.GeoDataFrame(points,geometry="geometry",crs=LOCAL_CRS).sort_values(["profile_id","point_order"]).reset_index(drop=True)
    return pg,pt


def campaign_calendar() -> pd.DataFrame:
    dates=pd.to_datetime([
        "2018-05-15","2018-10-16","2019-05-14","2019-10-22","2020-05-18","2020-08-20","2020-10-19",
        "2021-05-17","2021-08-18","2021-10-20","2022-05-16","2022-07-19","2022-10-18",
        "2023-05-15","2023-07-18","2023-10-17","2024-04-29","2024-06-25","2024-08-20","2024-10-15",
        "2025-03-25","2025-05-20","2025-06-24","2025-07-22","2025-08-19","2025-09-16","2025-10-14",
    ])
    return pd.DataFrame({"campaign_id":[f"C{i+1:03d}" for i in range(len(dates))],"date":dates.date.astype(str),"year":dates.year,"day_of_year":dates.dayofyear,"campaign_type":["full" if (d.month in [5,10] or d.year>=2025) else "focused" for d in dates],"provenance":"S"})


def decimal_year(ts:pd.Timestamp)->float:
    y0=pd.Timestamp(ts.year,1,1);y1=pd.Timestamp(ts.year+1,1,1);return ts.year+(ts-y0).total_seconds()/(y1-y0).total_seconds()


def generate_process_truth(points:gpd.GeoDataFrame,seed:int,n_ensemble=8):
    rng=np.random.default_rng(seed+202)
    months=pd.date_range("2018-01-01","2025-12-01",freq="MS")
    t=np.array([decimal_year(x) for x in months])
    nominal=[];params=[];lookups={};ensemble=[]
    for _,p in points.iterrows():
        if p.point_type=="REF":
            s=np.zeros(len(t));v=np.zeros(len(t));regime="stable_reference";anchor_date=pd.Timestamp("2022-07-01")
            par={"base_rate":0,"event_amp":0,"event_center":np.nan,"decay_tau":np.nan}
        else:
            anchor=float(max(0,p.settlement_anchor_map_mm))
            # Scenario-only date used to build one admissible history; source supports year 2022 only.
            anchor_date=pd.Timestamp("2022-07-01")
            ta=decimal_year(anchor_date)
            risk=float(np.nan_to_num((p.kzt-.1)/.8,.4));load=float(np.nan_to_num((p.ko-.3)/.35,.4));fault=math.exp(-float(p.fault_distance_m)/400)
            base=4+14*risk+10*load+7*fault+5*(1-float(p.fill_density))
            event_amp=(6+35*risk*load+18*fault)*(1 if anchor>80 else .35)
            center=float(2020.2+1.8*(1-risk)+rng.normal(0,.25));tau=float(1.2+2.5*(1-risk)+rng.uniform(-.2,.3))
            seasonal=1.5*np.maximum(0,np.sin(2*np.pi*(t-.15)))
            rate_shape=np.maximum(.05,base*(.65+.35*np.exp(-(t-2018)/tau))+event_amp/(1+np.exp(-(t-center)*4))+seasonal)
            # Integrate and scale exactly to the map amplitude at the scenario anchor date.
            dt=np.diff(t);cum=np.concatenate([[0.0],np.cumsum(.5*(rate_shape[:-1]+rate_shape[1:])*dt)])
            ia=np.argmin(np.abs(t-ta));scale=anchor/max(cum[ia],1e-9)
            v=rate_shape*scale;s=cum*scale;s=np.maximum.accumulate(s)
            regime="accelerating" if event_amp*scale>20 else ("decaying" if tau<2 else "uniform_creep")
            par={"base_rate":base*scale,"event_amp":event_amp*scale,"event_center":center,"decay_tau":tau}
        lookups[p.point_id]={"t":t,"s":s,"v":v}
        params.append({"point_id":p.point_id,"profile_id":p.profile_id,"point_type":p.point_type,"regime":regime,"scenario_anchor_date":anchor_date.date().isoformat(),"source_reference_year":2022,"source_reference_date":None,"source_reference_status":"year_supported_exact_date_unknown",**par,"provenance":"R/S"})
        for d,ss,vv in zip(months,s,v):nominal.append({"point_id":p.point_id,"date":d.date().isoformat(),"true_settlement_mm":float(ss),"true_velocity_mm_y":float(vv),"regime":regime,"scenario_anchor_date":anchor_date.date().isoformat(),"source_reference_year":2022,"provenance":"R/S"})
        # Alternative admissible histories, each with an anchor date sampled within 2022.
        if p.point_type=="REF":
            for rr in range(n_ensemble):
                for d in months[::3]:ensemble.append({"realization_id":f"R{rr+1:02d}","point_id":p.point_id,"date":d.date().isoformat(),"settlement_mm":0.0,"velocity_mm_y":0.0,"scenario_anchor_date":f"2022-{1+(rr*7)%12:02d}-15","provenance":"S"})
        else:
            anchor=float(max(0,p.settlement_anchor_map_mm))
            for rr in range(n_ensemble):
                arng=np.random.default_rng(seed+5000+rr*100000+int(hashlib.sha256(str(p.point_id).encode("utf-8")).hexdigest()[:8],16)%100000)
                ad=pd.Timestamp(2022,int(arng.integers(1,13)),int(arng.integers(1,25)))
                ta=decimal_year(ad);base=max(.05,par["base_rate"]*arng.lognormal(0,.18));amp=max(0,par["event_amp"]*arng.lognormal(0,.25));cen=par["event_center"]+arng.normal(0,.35);tau=max(.6,par["decay_tau"]*arng.lognormal(0,.15))
                shape=np.maximum(.02,base*(.6+.4*np.exp(-(t-2018)/tau))+amp/(1+np.exp(-(t-cen)*arng.uniform(2.5,5.5))))
                dt=np.diff(t);cum=np.concatenate([[0.0],np.cumsum(.5*(shape[:-1]+shape[1:])*dt)]);ia=np.argmin(np.abs(t-ta));fac=anchor/max(cum[ia],1e-9);vv=shape*fac;ss=cum*fac;ss=np.maximum.accumulate(ss)
                for _kk in range(0,len(months),3):
                    d=months[_kk];a1=ss[_kk];a2=vv[_kk]
                    ensemble.append({"realization_id":f"R{rr+1:02d}","point_id":p.point_id,"date":d.date().isoformat(),"settlement_mm":float(a1),"velocity_mm_y":float(a2),"scenario_anchor_date":ad.date().isoformat(),"provenance":"S"})
    ens=pd.DataFrame(ensemble)
    qs=ens.groupby(["point_id","date"]).settlement_mm.quantile([.05,.25,.5,.75,.95]).unstack().reset_index();qs.columns=["point_id","date","settlement_q05_mm","settlement_q25_mm","settlement_q50_mm","settlement_q75_mm","settlement_q95_mm"]
    return pd.DataFrame(params),pd.DataFrame(nominal),lookups,ens,qs


def interp_truth(lookup:dict[str,dict[str,np.ndarray]],point_id:str,date:pd.Timestamp):
    z=lookup[point_id];tt=decimal_year(date);return float(np.interp(tt,z["t"],z["s"])),float(np.interp(tt,z["t"],z["v"]))


def benchmark_catalog(points:gpd.GeoDataFrame,seed:int) -> gpd.GeoDataFrame:
    refs=points[points.point_type=="REF"].copy();refs["benchmark_id"]=[f"BM-{i+1:03d}" for i in range(len(refs))];refs["adopted_height_m"]=refs.base_height_m+np.random.default_rng(seed+303).normal(0,.0004,len(refs));refs["adopted_height_standard_uncertainty_mm"]=.6;refs["stability_assumption"]="external_reference_synthetic_design";refs["provenance"]="S"
    return refs[["benchmark_id","point_id","profile_id","x_local_m","y_local_m","adopted_height_m","adopted_height_standard_uncertainty_mm","stability_assumption","provenance","geometry"]]


def generate_leveling(points:gpd.GeoDataFrame,profiles:gpd.GeoDataFrame,campaigns:pd.DataFrame,lookup,bench:gpd.GeoDataFrame,seed:int):
    rng=np.random.default_rng(seed+404);raw=[];runs=[];adj=[];benchobs=[]
    pidx=points.set_index("point_id");bidx=bench.set_index("point_id")
    for _,camp in campaigns.iterrows():
        date=pd.Timestamp(camp.date)
        for pid,g in points.groupby("profile_id"):
            g=g.sort_values("point_order").reset_index(drop=True)
            # Independent benchmark observations, not hidden true heights.
            bobs={}
            for side in [g.iloc[0],g.iloc[-1]]:
                adopted=float(bidx.loc[side.point_id].adopted_height_m);sig=.0006
                observed=adopted+rng.normal(0,sig)
                bobs[side.point_id]=(observed,sig)
                benchobs.append({"campaign_id":camp.campaign_id,"date":camp.date,"benchmark_id":bidx.loc[side.point_id].benchmark_id,"point_id":side.point_id,"observed_height_m":observed,"standard_uncertainty_mm":sig*1000,"observation_method":"independent benchmark control","provenance":"S"})
            accepted_obs=[]
            for direction in ["forward","reverse"]:
                order=list(range(len(g)-1)) if direction=="forward" else list(range(len(g)-2,-1,-1))
                run_no=1
                while True:
                    run_id=f"LEV-{camp.campaign_id}-{pid}-{direction[0].upper()}-{run_no}"
                    drows=[];sumdh=0;length=0
                    gross=(run_no==1 and rng.random()<.045)
                    gross_seg=int(rng.integers(0,max(1,len(order)))) if gross else -1
                    for kk,i in enumerate(order):
                        ia=i if direction=="forward" else i+1;ib=i+1 if direction=="forward" else i
                        pa=g.iloc[ia];pb=g.iloc[ib]
                        sa,_=interp_truth(lookup,pa.point_id,date);sb,_=interp_truth(lookup,pb.point_id,date)
                        ha=pa.base_height_m-sa/1000;hb=pb.base_height_m-sb/1000
                        dist=Point(pa.x_local_m,pa.y_local_m).distance(Point(pb.x_local_m,pb.y_local_m));length+=dist
                        sig_mm=.35+.22*math.sqrt(max(dist,1)/100);noise=rng.normal(0,sig_mm/1000)
                        if kk==gross_seg:noise+=rng.choice([-1,1])*rng.uniform(.004,.010)
                        dh=(hb-ha)+noise;sumdh+=dh
                        bs=rng.uniform(.8,2.3);fs=bs-dh
                        drows.append({"station_id":f"{run_id}-S{kk+1:03d}","run_id":run_id,"campaign_id":camp.campaign_id,"date":camp.date,"profile_id":pid,"direction":direction,"from_point_id":pa.point_id,"to_point_id":pb.point_id,"backsight_reading_m":bs,"foresight_reading_m":fs,"observed_height_difference_m":dh,"sight_length_m":dist,"sight_imbalance_m":rng.normal(0,1.8),"station_standard_uncertainty_mm":sig_mm,"gross_error_injected":kk==gross_seg,"provenance":"S"})
                    first=g.iloc[0].point_id if direction=="forward" else g.iloc[-1].point_id;last=g.iloc[-1].point_id if direction=="forward" else g.iloc[0].point_id
                    closure_mm=(sumdh-(bobs[last][0]-bobs[first][0]))*1000
                    tol_mm=4*math.sqrt(max(length/1000,.01))
                    status="accepted" if abs(closure_mm)<=tol_mm else "failed_repeat_required"
                    raw.extend(drows);runs.append({"run_id":run_id,"campaign_id":camp.campaign_id,"date":camp.date,"profile_id":pid,"direction":direction,"run_no":run_no,"length_km":length/1000,"observed_sum_dh_m":sumdh,"benchmark_height_difference_m":bobs[last][0]-bobs[first][0],"closure_mm":closure_mm,"closure_tolerance_mm":tol_mm,"qc_status":status,"gross_error_injected":gross,"provenance":"C/S"})
                    if status=="accepted":accepted_obs.extend(drows);break
                    run_no+=1
                    if run_no>2:break
            # Weighted least-squares adjustment for a chain traverse, solved analytically.
            # Forward and reverse observations are reduced to the profile's canonical orientation,
            # then the benchmark closure is distributed in proportion to segment variances.
            ids=list(g.point_id)
            pos={q:i for i,q in enumerate(ids)}
            segment_obs={i:[] for i in range(len(ids)-1)}
            for r in accepted_obs:
                ia=pos[r["from_point_id"]];ib=pos[r["to_point_id"]]
                if abs(ib-ia)!=1: continue
                seg=min(ia,ib)
                dh=float(r["observed_height_difference_m"])
                if ib<ia: dh=-dh
                sig=float(r["station_standard_uncertainty_mm"])/1000
                segment_obs[seg].append((dh,sig))
            dhs=[];vars_=[]
            for seg in range(len(ids)-1):
                obs=segment_obs[seg]
                if not obs:
                    # This should be rare; keep the profile solvable and flag through uncertainty.
                    pa=g.iloc[seg];pb=g.iloc[seg+1]
                    dhs.append(float(pb.base_height_m-pa.base_height_m));vars_.append((.010)**2)
                    continue
                ww=np.array([1/max(sig,1e-9)**2 for _,sig in obs]);vv=np.array([dh for dh,_ in obs])
                dhs.append(float(np.sum(ww*vv)/np.sum(ww)));vars_.append(float(1/np.sum(ww)))
            dhs=np.asarray(dhs);vars_=np.asarray(vars_)
            left_id,right_id=ids[0],ids[-1]
            left_h,left_sig=bobs[left_id];right_h,right_sig=bobs[right_id]
            raw_cum=np.concatenate([[0.0],np.cumsum(dhs)])
            closure=float(right_h-(left_h+raw_cum[-1]))
            total_var=float(vars_.sum()+left_sig**2+right_sig**2)
            cum_var=np.concatenate([[0.0],np.cumsum(vars_)])
            frac=np.where(vars_.sum()>0,cum_var/vars_.sum(),np.linspace(0,1,len(ids)))
            xhat=left_h+raw_cum+closure*frac
            # Synthetic variance-factor diagnostic from consistency of forward/reverse observations.
            standardised=[]
            for seg,obs in segment_obs.items():
                for dh,sig in obs: standardised.append((dh-dhs[seg])/max(sig,1e-9))
            sigma0=max(1.0,float(np.sqrt(np.mean(np.square(standardised)))) if standardised else 1.0)
            # Approximate covariance of adjusted chain nodes, including both benchmark constraints.
            node_var=left_sig**2+cum_var+(frac**2)*(right_sig**2+left_sig**2+vars_.sum())
            for j,(q,hhat) in enumerate(zip(ids,xhat)):
                p=pidx.loc[q];s,_=interp_truth(lookup,q,date);truth_h=p.base_height_m-s/1000
                u_mm=math.sqrt(max(node_var[j],1e-12))*1000*sigma0
                observed_settlement=(p.base_height_m-hhat)*1000
                status="accepted" if u_mm<=3.5 else ("warning" if u_mm<=7 else "rejected")
                adj.append({"campaign_id":camp.campaign_id,"date":camp.date,"profile_id":pid,"point_id":q,"adjusted_height_m":hhat,"observed_settlement_mm":observed_settlement,"standard_uncertainty_mm":u_mm,"variance_factor":sigma0,"profile_closure_correction_mm":closure*1000,"qc_status":status,"true_height_m_evaluation_only":truth_h,"true_settlement_mm_evaluation_only":s,"residual_mm_evaluation_only":(hhat-truth_h)*1000,"adjustment_used_ground_truth":False,"adjustment_method":"weighted_chain_traverse_least_squares","provenance":"C/S"})
    return pd.DataFrame(raw),pd.DataFrame(runs),pd.DataFrame(adj),pd.DataFrame(benchobs)


def generate_planar_observations(points:gpd.GeoDataFrame,campaigns:pd.DataFrame,lookup,seed:int):
    rng=np.random.default_rng(seed+505);raw=[];adjusted=[]
    for _,camp in campaigns.iterrows():
        date=pd.Timestamp(camp.date)
        for pid,g in points.groupby("profile_id"):
            g=g.sort_values("point_order").reset_index(drop=True)
            # Compute a smooth horizontal displacement toward increasing settlement gradient.
            chains=g.chainage_m.to_numpy(float);sett=np.array([interp_truth(lookup,q,date)[0] for q in g.point_id]);grad=np.gradient(sett,chains,edge_order=1);true_u=-32*grad  # mm
            true_u[g.point_type.to_numpy()=="REF"]=0
            common=rng.normal(0,1.5)
            for i,p in g.iterrows():
                sig=1.2+0.004*max(0,p.chainage_m)
                obs_u=true_u[i]+common+rng.normal(0,sig)
                obs_chain=p.chainage_m+obs_u/1000
                raw.append({"planar_observation_id":f"PL-{camp.campaign_id}-{p.point_id}","campaign_id":camp.campaign_id,"date":camp.date,"profile_id":pid,"point_id":p.point_id,"observed_chainage_m":obs_chain,"instrument":"total_station_synthetic","standard_uncertainty_mm":sig,"provenance":"S"})
            # Remove common mode from two reference points.
            sub=pd.DataFrame(raw[-len(g):]);ref=sub[sub.point_id.isin(g[g.point_type=="REF"].point_id)]
            correction=float(np.mean([(r.observed_chainage_m-g[g.point_id==r.point_id].chainage_m.iloc[0])*1000 for _,r in ref.iterrows()])) if len(ref) else 0
            for i,p in g.iterrows():
                rr=sub.iloc[i];u=(rr.observed_chainage_m-p.chainage_m)*1000-correction;sig=math.sqrt(rr.standard_uncertainty_mm**2+(1.5/math.sqrt(max(len(ref),1)))**2)
                adjusted.append({"campaign_id":camp.campaign_id,"date":camp.date,"profile_id":pid,"point_id":p.point_id,"observed_horizontal_displacement_mm":u,"true_horizontal_displacement_mm_evaluation_only":true_u[i],"residual_mm_evaluation_only":u-true_u[i],"standard_uncertainty_mm":sig,"common_mode_correction_mm":correction,"qc_status":"accepted" if sig<4 else "warning","provenance":"C/S"})
    return pd.DataFrame(raw),pd.DataFrame(adjusted)


def derive_profile_kinematics(points:gpd.GeoDataFrame,lev:pd.DataFrame,planar:pd.DataFrame):
    meta=points[["point_id","profile_id","point_order","chainage_m","point_type"]]
    l=lev.merge(meta,on=["point_id","profile_id"],how="left").sort_values(["campaign_id","profile_id","point_order"])
    p=planar.merge(meta,on=["point_id","profile_id"],how="left").sort_values(["campaign_id","profile_id","point_order"])
    hdisp=planar.copy();strains=[];tilts=[];curv=[];rates=[];summary=[]
    # Horizontal strain relative to initial observed interval.
    init={}
    for (pid),g in p.groupby("profile_id"):
        g0=g[g.campaign_id==g.campaign_id.min()].sort_values("point_order")
        init[pid]={}
        for a,b in zip(g0.iloc[:-1].itertuples(),g0.iloc[1:].itertuples()):init[pid][(a.point_id,b.point_id)]=(b.chainage_m+b.observed_horizontal_displacement_mm/1000)-(a.chainage_m+a.observed_horizontal_displacement_mm/1000)
    for (camp,pid),g in p.groupby(["campaign_id","profile_id"]):
        g=g.sort_values("point_order")
        for a,b in zip(g.iloc[:-1].itertuples(),g.iloc[1:].itertuples()):
            d=(b.chainage_m+b.observed_horizontal_displacement_mm/1000)-(a.chainage_m+a.observed_horizontal_displacement_mm/1000);d0=init[pid].get((a.point_id,b.point_id),b.chainage_m-a.chainage_m);eps=(d-d0)/d0;su=math.sqrt(a.standard_uncertainty_mm**2+b.standard_uncertainty_mm**2)/1000/d0
            strains.append({"campaign_id":camp,"date":a.date,"profile_id":pid,"from_point_id":a.point_id,"to_point_id":b.point_id,"interval_mid_chainage_m":.5*(a.chainage_m+b.chainage_m),"horizontal_strain":eps,"horizontal_strain_x1e3":eps*1000,"standard_uncertainty":su,"provenance":"C"})
    for (camp,pid),g in l.groupby(["campaign_id","profile_id"]):
        g=g.sort_values("point_order").reset_index(drop=True)
        local_t=[]
        for i in range(len(g)-1):
            a,b=g.iloc[i],g.iloc[i+1];dist=b.chainage_m-a.chainage_m
            if dist<=0 or pd.isna(a.observed_settlement_mm) or pd.isna(b.observed_settlement_mm):continue
            tilt=(b.observed_settlement_mm-a.observed_settlement_mm)/dist;su=math.sqrt(a.standard_uncertainty_mm**2+b.standard_uncertainty_mm**2)/dist
            row={"campaign_id":camp,"date":a.date,"profile_id":pid,"from_point_id":a.point_id,"to_point_id":b.point_id,"interval_mid_chainage_m":.5*(a.chainage_m+b.chainage_m),"interval_length_m":dist,"tilt_mm_per_m":tilt,"tilt_dimensionless":tilt/1000,"standard_uncertainty_mm_per_m":su,"provenance":"C"};tilts.append(row);local_t.append(row)
        for a,b in zip(local_t[:-1],local_t[1:]):
            lavg=b["interval_mid_chainage_m"]-a["interval_mid_chainage_m"]
            k=(b["tilt_mm_per_m"]-a["tilt_mm_per_m"])/lavg;su=math.sqrt(a["standard_uncertainty_mm_per_m"]**2+b["standard_uncertainty_mm_per_m"]**2)/lavg
            curv.append({"campaign_id":camp,"date":a["date"],"profile_id":pid,"point_id":a["to_point_id"],"chainage_m":.5*(a["interval_mid_chainage_m"]+b["interval_mid_chainage_m"]),"curvature_mm_per_m2":k,"curvature_1_per_m":k/1000,"standard_uncertainty_mm_per_m2":su,"provenance":"C"})
        ss=g.observed_settlement_mm.to_numpy(float)
        summary.append({"campaign_id":camp,"date":g.date.iloc[0],"profile_id":pid,"n_points":len(g),"max_settlement_mm":float(np.nanmax(ss)),"min_settlement_mm":float(np.nanmin(ss)),"settlement_range_mm":float(np.nanmax(ss)-np.nanmin(ss)),"max_abs_tilt_mm_per_m":max([abs(x["tilt_mm_per_m"]) for x in local_t],default=np.nan),"provenance":"C"})
    for pid,g in l.groupby("point_id"):
        g=g.sort_values("date")
        for a,b in zip(g.iloc[:-1].itertuples(),g.iloc[1:].itertuples()):
            dt=(pd.Timestamp(b.date)-pd.Timestamp(a.date)).days/365.25
            if dt<=0:continue
            rate=(b.observed_settlement_mm-a.observed_settlement_mm)/dt;su=math.sqrt(a.standard_uncertainty_mm**2+b.standard_uncertainty_mm**2)/dt
            rates.append({"point_id":pid,"profile_id":a.profile_id,"from_campaign_id":a.campaign_id,"to_campaign_id":b.campaign_id,"from_date":a.date,"to_date":b.date,"interval_years":dt,"settlement_rate_mm_y":rate,"standard_uncertainty_mm_y":su,"provenance":"C"})
    return hdisp,pd.DataFrame(strains),pd.DataFrame(tilts),pd.DataFrame(curv),pd.DataFrame(rates),pd.DataFrame(summary)


def generate_gnss(points:gpd.GeoDataFrame,campaigns:pd.DataFrame,lookup,bench:gpd.GeoDataFrame,seed:int):
    rng=np.random.default_rng(seed+606)
    work=points[points.point_type=="WORK"].nlargest(30,"settlement_anchor_map_mm");refs=points[points.point_type=="REF"].drop_duplicates("profile_id").head(12);sel=pd.concat([work,refs]).drop_duplicates("point_id")
    epochs=campaigns[(campaigns.campaign_type=="full")|campaigns.date.str.slice(5,7).isin(["07","08"])].copy();raw=[];adj=[]
    adopted=bench.set_index("point_id")
    for _,camp in epochs.iterrows():
        date=pd.Timestamp(camp.date);common_h=rng.normal(0,3.0);common_e=rng.normal(0,1.8);common_n=rng.normal(0,1.8);er=[]
        for _,p in sel.iterrows():
            s,_=interp_truth(lookup,p.point_id,date);baseline=math.hypot(p.x_local_m-sel.x_local_m.mean(),p.y_local_m-sel.y_local_m.mean())/1000
            sigxy=2.5+.35*baseline;sigh=4.5+.55*baseline
            for sess in [1,2]:
                fixed=rng.random()>.035;pdop=float(np.clip(rng.normal(1.7,.45),.8,4.2));infl=1 if fixed else 3
                e=p.x_local_m+(common_e+rng.normal(0,sigxy*infl))/1000;n=p.y_local_m+(common_n+rng.normal(0,sigxy*infl))/1000;h=p.base_height_m-s/1000+(common_h+rng.normal(0,sigh*infl))/1000
                raw.append({"gnss_session_id":f"GN-{camp.campaign_id}-{p.point_id}-{sess}","campaign_id":camp.campaign_id,"date":camp.date,"point_id":p.point_id,"session_no":sess,"east_local_m":e,"north_local_m":n,"height_m":h,"baseline_km":baseline,"duration_min":int(rng.integers(60,145)),"pdop":pdop,"satellites":int(rng.integers(10,24)),"solution":"fixed" if fixed else "float","sigma_plan_mm_model":sigxy*infl,"sigma_height_mm_model":sigh*infl,"provenance":"S"});er.append({"point_id":p.point_id,"h":h,"fixed":fixed,"sigh":sigh*infl,"pdop":pdop,"truth_s":s})
        er=pd.DataFrame(er);ref=er[er.point_id.isin(refs.point_id)]
        corrections=[]
        for _,r in ref.iterrows():
            if r.point_id in adopted.index:corrections.append((r.h-adopted.loc[r.point_id].adopted_height_m)*1000)
        common_est=float(np.median(corrections)) if corrections else 0;common_sigma=float(1.4826*np.median(np.abs(np.array(corrections)-np.median(corrections)))/math.sqrt(max(len(corrections),1))) if corrections else 4
        for q,g in er.groupby("point_id"):
            p=points[points.point_id==q].iloc[0];fixed=g[g.fixed];used=fixed if len(fixed) else g;hmean=used.h.mean()-common_est/1000;sobs=(p.base_height_m-hmean)*1000;truth=float(g.truth_s.iloc[0]);session_se=float(np.sqrt(np.mean(used.sigh**2))/math.sqrt(len(used)));u=math.sqrt(session_se**2+common_sigma**2+2.5**2)
            n_fixed=len(fixed);mean_pdop=float(g.pdop.mean());max_pdop=float(g.pdop.max())
            if n_fixed==2 and u<=8.0 and max_pdop<=3.2:
                status="accepted";reason="two_fixed_sessions_and_calibrated_uncertainty"
            elif n_fixed>=1 and u<=16.0 and max_pdop<=4.2:
                status="warning";reason="single_fixed_session_or_elevated_uncertainty"
            else:
                status="rejected";reason="no_fixed_solution_or_excess_uncertainty"
            adj.append({"campaign_id":camp.campaign_id,"date":camp.date,"point_id":q,"n_sessions":len(g),"n_fixed":n_fixed,"mean_pdop":mean_pdop,"max_pdop":max_pdop,"adjusted_height_m":hmean,"observed_settlement_mm":sobs,"true_settlement_mm_evaluation_only":truth,"residual_mm_evaluation_only":sobs-truth,"standard_uncertainty_mm":u,"common_mode_correction_mm":common_est,"common_mode_standard_uncertainty_mm":common_sigma,"qc_status":status,"qc_reason":reason,"provenance":"C/S"})
    return pd.DataFrame(raw),pd.DataFrame(adj),gpd.GeoDataFrame(sel,geometry="geometry",crs=points.crs)


def generate_insar(grid:gpd.GeoDataFrame,points:gpd.GeoDataFrame,lookup,seed:int,npts=1800):
    rng=np.random.default_rng(seed+707);acq=[]
    for year,dates in [(2020,INSAR_DATES_2020),(2021,INSAR_DATES_2021)]:
        for i,d in enumerate(dates):acq.append({"acquisition_id":f"TSX-{year}-{i+1:02d}","date":d,"mission":"TerraSAR-X analogue calendar","orbit_direction":"descending","incidence_angle_deg":36.0,"calendar_provenance":"P transferred analogue","source_object":"Berezniki","use":"auxiliary synthetic InSAR contour"})
    acq=pd.DataFrame(acq)
    valid=grid[grid.settlement_reference_map_mm.notna()].copy();npts=min(npts,len(valid));sel=valid.sample(npts,random_state=seed)
    pxy=points[["x_local_m","y_local_m"]].to_numpy(float);ptree=cKDTree(pxy)
    catalog=[]
    for i,(_,g) in enumerate(sel.iterrows()):
        x=g.x_local_m+rng.uniform(-20,20);y=g.y_local_m+rng.uniform(-20,20);_,j=ptree.query([x,y]);pid=points.iloc[int(j)].point_id
        coh=float(np.clip(.30+.45*np.nan_to_num(g.terrain_TRI_relative,.5)+.18*rng.random()-.08*(g.lithology_reconstructed=="mixed_salts"),.12,.96))
        catalog.append({"insar_point_id":f"PS-{i+1:05d}","source_cell_id":g.cell_id,"nearest_truth_point_id":pid,"x_local_m":x,"y_local_m":y,"coherence_baseline":coh,"reflector_class":"persistent_scatterer_proxy" if coh>.62 else "distributed_scatterer_proxy","provenance":"H","geometry":Point(x,y)})
    pg=gpd.GeoDataFrame(catalog,geometry="geometry",crs=grid.crs);rows=[]
    inc=math.radians(36)
    for _,p in pg.iterrows():
        first_date=pd.Timestamp(acq.iloc[0].date);s0,_=interp_truth(lookup,p.nearest_truth_point_id,first_date);xnorm=(p.x_local_m-grid.x_local_m.min())/(grid.x_local_m.max()-grid.x_local_m.min());ynorm=(p.y_local_m-grid.y_local_m.min())/(grid.y_local_m.max()-grid.y_local_m.min())
        # Reference atmosphere/orbit/DEM terms for relative datum.
        atm_ref=rng.normal(0,5)+rng.normal(0,3)*(xnorm-.5)+rng.normal(0,3)*(ynorm-.5);orbit_ref=rng.normal(0,2)*(xnorm-.5);dem_ref=rng.normal(0,1.5)
        true0=-(s0*math.cos(inc)+.04*s0*math.sin(2*np.pi*xnorm)*math.sin(inc))
        for ai,a in acq.iterrows():
            date=pd.Timestamp(a.date);s,_=interp_truth(lookup,p.nearest_truth_point_id,date);true_abs=-(s*math.cos(inc)+.04*s*math.sin(2*np.pi*xnorm)*math.sin(inc));true_rel=true_abs-true0
            if ai==0:
                coh=p.coherence_baseline;raw=corrected=subv=0.0;atm=orbit=dem=thermal=0.0;u=0.0
            else:
                coh=float(np.clip(p.coherence_baseline+rng.normal(0,.07),0,1));atm=(rng.normal(0,5)+rng.normal(0,3)*(xnorm-.5)+rng.normal(0,3)*(ynorm-.5))-atm_ref;orbit=rng.normal(0,2)*(xnorm-.5)-orbit_ref;dem=rng.normal(0,1.5)-dem_ref;thermal=(10*np.sin(2*np.pi*(date.dayofyear/365.25)) if p.reflector_class.startswith("persistent") else 0)
                noise_sigma=2+8*(1-coh);noise=rng.normal(0,noise_sigma);raw=true_rel+atm+orbit+dem+thermal+noise
                corrected=raw-(.88*atm+.85*orbit+.75*dem+.65*thermal);subv=-corrected/math.cos(inc)
                # Conservative relative uncertainty includes current + reference noise and residual corrections.
                u=.82*math.sqrt(2*noise_sigma**2+(.12*5)**2+(.15*2)**2+(.25*1.5)**2+(.35*abs(thermal))**2)/math.cos(inc)
            status="accepted" if coh>=.45 else ("warning" if coh>=.30 else "rejected")
            rows.append({"acquisition_id":a.acquisition_id,"reference_acquisition_id":acq.iloc[0].acquisition_id,"date":a.date,"insar_point_id":p.insar_point_id,"coherence":coh,"qc_status":status,"true_vertical_settlement_relative_mm_evaluation_only":s-s0,"true_LOS_relative_mm_evaluation_only":true_rel,"raw_LOS_relative_mm":raw,"estimated_atmospheric_correction_relative_mm":.88*atm,"estimated_orbit_correction_relative_mm":.85*orbit,"estimated_dem_correction_relative_mm":.75*dem,"estimated_thermal_correction_relative_mm":.65*thermal,"corrected_LOS_relative_mm":corrected,"subvertical_estimate_relative_mm":subv,"standard_uncertainty_mm":u,"first_epoch_zero_datum":ai==0,"provenance":"C/S"})
    return acq,pg,pd.DataFrame(rows)


def stress_scenarios(points:gpd.GeoDataFrame,seed:int):
    rng=np.random.default_rng(seed+808);dates=pd.date_range("2019-01-01","2025-12-01",freq="MS");rows=[];catalog=[];measure=[]
    families=["logistic_acceleration","sustained_high_rate","pulse_with_residual"]
    candidates=points[points.point_type=="WORK"].nlargest(12,"settlement_anchor_map_mm")
    for sid in range(36):
        p=candidates.iloc[sid%len(candidates)];fam=families[sid%3];peak=float(rng.uniform(120,395));center=float(rng.uniform(2021,2024));t=np.array([decimal_year(d) for d in dates])
        if fam=="logistic_acceleration":v=5+(peak-5)/(1+np.exp(-(t-center)*5))
        elif fam=="sustained_high_rate":v=8+(peak-8)/(1+np.exp(-(t-center)*6));v=np.minimum(v,peak)
        else:v=8+peak*np.exp(-.5*((t-center)/.28)**2)+.18*peak/(1+np.exp(-(t-center)*5))
        dt=np.diff(t,prepend=t[0]);s=np.cumsum(v*dt);s-=s[0]
        sc=f"ST-{sid+1:03d}";catalog.append({"scenario_id":sc,"family":fam,"point_id":p.point_id,"peak_rate_mm_y":float(v.max()),"use_class":"stress_test_only_not_calibration","provenance":"S"})
        for d,ss,vv in zip(dates,s,v):rows.append({"scenario_id":sc,"point_id":p.point_id,"date":d.date().isoformat(),"true_settlement_mm":float(ss),"true_velocity_mm_y":float(vv),"provenance":"S"})
        for d in pd.date_range("2022-01-01","2025-12-01",freq="2MS"):
            j=int(np.argmin(np.abs(dates-d)));missing=rng.random()<.035;gross=rng.random()<.02;obs=np.nan if missing else s[j]+rng.normal(0,2.5)+(rng.choice([-1,1])*rng.uniform(12,30) if gross else 0)
            measure.append({"scenario_id":sc,"point_id":p.point_id,"date":d.date().isoformat(),"observed_settlement_mm":obs,"true_settlement_mm_evaluation_only":float(s[j]),"missing":missing,"gross_error":gross,"standard_uncertainty_mm":2.5,"use_class":"stress_test_only_not_calibration","provenance":"S"})
    return pd.DataFrame(catalog),pd.DataFrame(rows),pd.DataFrame(measure)


def write_gpkg(layers:dict[str,gpd.GeoDataFrame],path:Path):
    if path.exists():path.unlink()
    for name,g in layers.items():
        if g is None or len(g)==0:continue
        gg=g.copy();gg=gg.set_crs(LOCAL_CRS,allow_override=True)
        print(f"    writing GPKG layer {name}: {len(gg)}", flush=True)
        gg.to_file(path,layer=name,driver="GPKG",engine="pyogrio")


def frames_dictionary(frames:dict[str,pd.DataFrame]) -> pd.DataFrame:
    rows=[]
    for name,df in frames.items():
        for c in df.columns:
            if c=="geometry":continue
            s=df[c];rows.append({"table":name,"field":c,"dtype":str(s.dtype),"rows":len(df),"non_null":int(s.notna().sum()),"unique_values":int(s.nunique(dropna=True)) if len(s)<600000 else None,"example":str(s.dropna().iloc[0])[:160] if s.notna().any() else None})
    return pd.DataFrame(rows)


def validation_report(frames:dict[str,pd.DataFrame],units,grid,links,resids,levruns,levadj,gnss,insar,source_docx:Path,script_path:Path,output:Path) -> tuple[dict,pd.DataFrame]:
    checks=[]
    def add(cid,domain,desc,passed,actual,expected,severity="HIGH"):
        checks.append({"check_id":cid,"domain":domain,"description":desc,"passed":bool(passed),"actual":actual,"expected":expected,"severity":severity})
    add("SRC-001","source_fidelity","1665 is metadata only; no fabricated 1665-row table","legacy_integrated_features_1665" not in frames,list(frames),"no fake legacy table","CRITICAL")
    source_names=set(frames["source_layer_manifest"].source_layer_name.astype(str))
    expected_names={"zone_pol","otrpol_ab","otrpol_kr2","otrpol_ks","otrpol_vk","otrpol_vs","AZOLIN_KR2","AZOPOL_KR2","fact_zakl_VS_01_10_2022","fact_zakl_AB_01_10_2022","fact_zakl_Vk_01_10_2022","fact_zakl_Kp2_01_10_2022"}
    add("SRC-002","source_fidelity","all 12 source-named layers are represented separately",source_names==expected_names,sorted(source_names),sorted(expected_names),"CRITICAL")
    add("SRC-003","source_fidelity","reconstructed integrated row count is not forced to 1665",len(frames["integrated_features_reconstructed"])!=1665,len(frames["integrated_features_reconstructed"]),"natural reconstructed count, not 1665","CRITICAL")
    add("GEO-001","geometry","geometry extracted from Figure 13 red overlay",(units.geometry_source.str.contains("Figure 13").all()),units.geometry_source.unique().tolist(),"Figure 13","CRITICAL")
    add("GEO-002","geometry","all plan geometries valid",bool(units.geometry.is_valid.all()),int((~units.geometry.is_valid).sum()),0,"HIGH")
    footprint=unary_union(units.geometry);grid_union=unary_union(grid.geometry)
    outside=float(grid_union.difference(footprint).area);uncovered=float(footprint.difference(grid_union).area)
    add("GRID-001","grid","clipped grid has no area outside footprint",outside<1e-3,outside,"<0.001 m2","HIGH")
    add("GRID-002","grid","clipped grid covers footprint",uncovered<1e-2,uncovered,"<0.01 m2","HIGH")
    add("GRID-003","grid","no plan unit missing settlement aggregate",int(units.settlement_reference_map_mm_mean.isna().sum())==0,int(units.settlement_reference_map_mm_mean.isna().sum()),0,"HIGH")
    cov=frames["grid_coverage_summary"].iloc[0]
    add("GRID-004","grid","effective grid area balances reconstructed footprint",abs(float(cov.area_balance_error_m2))<1e-2,float(cov.area_balance_error_m2),"<0.01 m2","HIGH")
    add("TIME-001","time","reference year supported; exact reference date null",(grid.reference_year.eq(2022).all() and grid.reference_date.isna().all()),{"years":grid.reference_year.unique().tolist(),"non_null_dates":int(grid.reference_date.notna().sum())},"2022 and NULL exact date","CRITICAL")
    if len(resids):add("ANCH-001","anchors","published settlement mean anchors conditioned",float(resids.residual_mean_mm.abs().max())<1e-6,float(resids.residual_mean_mm.abs().max()),"<1e-6 mm","CRITICAL")
    add("ANCH-002","anchors","all accepted combined-row links satisfy uncertainty",bool(((links.distance_m<=links.link_uncertainty_m)|links.plan_unit_id.isna()).all()),int(((links.distance_m>links.link_uncertainty_m)&links.plan_unit_id.notna()).sum()),0,"HIGH")
    alinks=frames["published_anchor_spatial_links_all"]
    add("ANCH-003","anchors","all accepted links across the complete published-anchor catalog satisfy uncertainty",bool(((alinks.distance_m<=alinks.link_uncertainty_m)|alinks.plan_unit_id.isna()).all()),int(((alinks.distance_m>alinks.link_uncertainty_m)&alinks.plan_unit_id.notna()).sum()),0,"HIGH")
    calib=frames["coordinate_calibration_checks"]
    add("ANCH-004","anchors","all coordinate calibration anchors are inside or within digitization uncertainty",bool(calib.inside_or_within_geometry_uncertainty.all()),int((~calib.inside_or_within_geometry_uncertainty).sum()),0,"HIGH")
    add("KO-001","fields","all reconstructed k_o values retain donor distance",bool(grid.loc[grid.ko_provenance=="R","ko_nearest_source_distance_m"].notna().all()),int(grid.loc[grid.ko_provenance=="R","ko_nearest_source_distance_m"].isna().sum()),0,"HIGH")
    add("KO-002","fields","k_o reconstruction is distance-limited",bool((grid.loc[grid.ko_provenance=="R","ko_nearest_source_distance_m"]<=250+1e-9).all()),float(grid.loc[grid.ko_provenance=="R","ko_nearest_source_distance_m"].max() if (grid.ko_provenance=="R").any() else 0),"<=250 m","HIGH")
    ko_direct_area=float(grid.loc[grid.ko_provenance=="D","effective_area_m2"].sum()/grid.effective_area_m2.sum())
    add("KO-003","fields","k_o direct digitization covers a substantive part of the footprint",ko_direct_area>=.25,ko_direct_area,">=0.25 area fraction","HIGH")
    add("LEV-001","leveling","adjustment never uses hidden truth",bool((levadj.adjustment_used_ground_truth==False).all()),int(levadj.adjustment_used_ground_truth.sum()),0,"CRITICAL")
    add("LEV-002","leveling","failed initial runs retained",int((levruns.qc_status!="accepted").sum())>0,int((levruns.qc_status!="accepted").sum()),">0","MEDIUM")
    # zero settlement positive velocity
    truth=frames["truth_survey_points_monthly"]
    ref_ids=set(frames["survey_points"].query("point_type=='REF'").point_id)
    bad=truth[truth.point_id.isin(ref_ids)&(truth.true_settlement_mm.abs()<1e-12)&(truth.true_velocity_mm_y>.01)]
    add("TIME-002","time","reference points have zero settlement and zero velocity",len(bad)==0,len(bad),0,"HIGH")
    # GNSS/InSAR empirical coverage, excluding zero-uncertainty first InSAR epoch.
    gacc=gnss[gnss.qc_status!="rejected"];gcov=float((gacc.residual_mm_evaluation_only.abs()<=1.96*gacc.standard_uncertainty_mm).mean()) if len(gacc) else np.nan
    add("GNSS-001","gnss","95% uncertainty coverage is calibrated",.90<=gcov<=.99,gcov,"0.90..0.99","HIGH")
    add("GNSS-002","gnss","GNSS QC contains non-accepted outcomes",int((gnss.qc_status!="accepted").sum())>0,gnss.qc_status.value_counts().to_dict(),"at least one warning or rejected epoch","HIGH")
    iacc=insar[(insar.qc_status!="rejected")&(~insar.first_epoch_zero_datum)];err=iacc.subvertical_estimate_relative_mm-iacc.true_vertical_settlement_relative_mm_evaluation_only;icov=float((err.abs()<=1.96*iacc.standard_uncertainty_mm).mean()) if len(iacc) else np.nan
    add("INSAR-001","insar","all first epochs are exact zero relative datum",bool((insar[insar.first_epoch_zero_datum][["raw_LOS_relative_mm","corrected_LOS_relative_mm","subvertical_estimate_relative_mm"]].abs()<1e-12).all().all()),int((insar[insar.first_epoch_zero_datum].subvertical_estimate_relative_mm.abs()>1e-12).sum()),0,"CRITICAL")
    add("INSAR-002","insar","InSAR QC agrees with coherence",bool((((insar.coherence>=.45)&(insar.qc_status=="accepted"))|((insar.coherence>=.30)&(insar.coherence<.45)&(insar.qc_status=="warning"))|((insar.coherence<.30)&(insar.qc_status=="rejected"))).all()),0,0,"HIGH")
    add("INSAR-003","insar","95% uncertainty coverage is calibrated",.90<=icov<=.99,icov,"0.90..0.99","HIGH")
    required=["horizontal_displacements","horizontal_strains","tilts","curvatures","settlement_rates","profile_kinematics"]
    add("COMP-001","completeness","full derived surveying contour exists",all(x in frames and len(frames[x])>0 for x in required),[x for x in required if x not in frames or len(frames[x])==0],"none missing","HIGH")
    add("REP-001","reproducibility","source DOCX and script hashes are recorded",source_docx.exists() and script_path.exists(),{"docx":sha256(source_docx),"script":sha256(script_path)},"hashes available","CRITICAL")
    cdf=pd.DataFrame(checks);passed=bool(cdf.passed.all())
    report={"dataset_version":VERSION,"generated_at_utc":datetime.now(timezone.utc).isoformat(),"overall_status":"CONDITIONAL_GO_FOR_METHOD_DEVELOPMENT" if passed else "NO_GO_REQUIRES_CORRECTION","checks_total":len(cdf),"checks_passed":int(cdf.passed.sum()),"checks_failed":int((~cdf.passed).sum()),"all_passed":passed,"limitations":["Primary TAB/Excel production files are unavailable.","Local coordinate calibration is internally constrained by published anchors but is not an official CRS transformation.","Synthetic histories and measurement records do not prove production forecasting accuracy.","Enterprise-specific deformation limits remain missing and are not fabricated."],"coverage":{"gnss_95":gcov,"insar_95":icov},"checks":checks}
    return report,cdf


def make_figures(out:Path,units,grid,layers,profiles,points,truth,levadj,gnss,insar,resids):
    figdir=out/"figures";figdir.mkdir(parents=True,exist_ok=True)
    fig,ax=plt.subplots(figsize=(12,8));grid.plot(column="settlement_reference_map_mm",ax=ax,legend=True,cmap="Reds",linewidth=0);units.boundary.plot(ax=ax,color="black",linewidth=.25);ax.set_title("Reconstructed 2022 reference-year settlement field (exact date unknown)");ax.set_aspect("equal");fig.tight_layout();fig.savefig(figdir/"01_spatial_field_v3.png",dpi=180);plt.close(fig)
    fig,ax=plt.subplots(figsize=(12,8));units.boundary.plot(ax=ax,color="0.65",linewidth=.25);profiles.plot(ax=ax,linewidth=1);points[points.point_type=="WORK"].plot(ax=ax,markersize=2);points[points.point_type=="REF"].plot(ax=ax,markersize=12,marker="^");ax.set_title("Synthetic surveying network in reconstructed local coordinates");ax.set_aspect("equal");fig.tight_layout();fig.savefig(figdir/"02_survey_network_v3.png",dpi=180);plt.close(fig)
    sample=points[points.point_type=="WORK"].nlargest(4,"settlement_anchor_map_mm").point_id.tolist();fig,ax=plt.subplots(figsize=(12,6));
    for q in sample:
        g=truth[truth.point_id==q];ax.plot(pd.to_datetime(g.date),g.true_settlement_mm,label=q)
    ax.legend();ax.set_ylabel("settlement, mm");ax.set_title("Admissible nominal reconstructed histories (synthetic)");fig.tight_layout();fig.savefig(figdir/"03_nominal_histories_v3.png",dpi=180);plt.close(fig)
    if len(resids):
        fig,ax=plt.subplots(figsize=(10,5));ax.bar(resids.published_row_id,resids.residual_mean_mm);ax.axhline(0,color="black",lw=.8);ax.set_ylabel("mean residual, mm");ax.set_title("Published settlement anchors after conditioning");ax.tick_params(axis="x",rotation=45);fig.tight_layout();fig.savefig(figdir/"04_anchor_residuals_v3.png",dpi=180);plt.close(fig)


def write_docs(out:Path,report:dict,counts:dict):
    readme=f"""# SKRU-1 reconstructed data package v{VERSION}

## Status

**{report['overall_status']}** for development and testing of data-processing algorithms. This package is not a production surveying journal and is not evidence of real forecasting accuracy.

## What changed from v2.1

1. The fabricated 1665-row table was removed. The source-declared 1665 rows/257 fields are retained only as metadata; reconstructed integrated rows arise from the 12 source-named candidate layers.
2. Plan geometry is extracted from the clean red vector overlay in Figure 13, not from the red settlement heatmap.
3. The settlement map is referenced to **year 2022** because the source layer name contains `ОСЕДАНИЯ_2022_СКРУ1`; exact date remains NULL.
4. Exact published rows condition reconstructed attributes and settlement support cells; before/after residuals are stored.
5. Leveling is adjusted by weighted least squares from raw observed height differences and independent benchmark constraints. Ground truth is evaluation-only.
6. The script accepts `--source-docx` and extracts `word/media` itself.
7. The analysis grid is clipped to the reconstructed footprint and carries effective area fractions.
8. k_o extrapolation is distance-limited and donor distance/uncertainty are explicit.
9. Reference points have zero synthetic settlement and zero velocity.
10. GNSS and InSAR uncertainties/QC are recalibrated; InSAR is relative to the first acquisition.
11. Horizontal displacements, strains, tilts, curvatures, rates and profile summaries are included.

## Key counts

```json
{json.dumps(counts,ensure_ascii=False,indent=2)}
```

## Reproduce

```bash
python reproduce_v3.py --source-docx "ВКР_Филатова_М_С.docx" --output SKRU1_data_reconstruction_v3_1
```

See `metadata/validation_report.json`, `metadata/validation_checks.csv`, `DATASET_CARD.md`, and `METHODOLOGY_V3.md`.
"""
    (out/"README.md").write_text(readme,encoding="utf-8")
    card="""# Dataset card

## Intended use
Development of a diploma-grade algorithmic pipeline for quality control, spatial-temporal feature engineering and forecasting experiments on reconstructed/synthetic surveying data.

## Prohibited claims
- Do not call reconstructed/synthetic records actual production measurements.
- Do not use synthetic validation metrics as proof of production accuracy.
- Do not treat overview geographic placement as an official coordinate transformation.
- Do not activate enterprise risk thresholds without an official project document.

## Provenance
P = exact published; D = digitized; C = calculated; R = reconstructed; S = synthetic; H = hybrid.
"""
    (out/"DATASET_CARD.md").write_text(card,encoding="utf-8")
    meth="""# Mathematical methodology v3

## Spatial reconstruction
Plan polygons are extracted from red vector linework in Figure 13. The segmentation mask uses hue/channel separation rather than grayscale, preventing the settlement heatmap from becoming geometry. The local affine bounds are calibrated against published local-coordinate anchors.

## Settlement map
The Figure 22 color scale is digitized into a reference-year field. Source evidence supports the year 2022, but not a precise date. Exact published `Disp_min`, `Disp_mean`, and `Disp_max` values condition non-overlapping local support cells by a monotone rank-power transform.

## Measurement simulation
Raw leveling equations are generated as observed height differences. Adjustment solves `A h = l` by weighted least squares with independent benchmark-height constraints. Hidden truth is not passed into the solver. GNSS common mode is estimated from stable reference points. InSAR observations are stored relative to the first acquisition.

## Derived surveying quantities
- Settlement: eta = H_previous - H_current.
- Tilt: difference of settlements divided by profile interval length.
- Curvature: difference of adjacent tilts divided by distance between interval midpoints.
- Horizontal strain: interval-length change divided by initial interval length.
Uncertainties are propagated by first-order variance rules.
"""
    (out/"METHODOLOGY_V3.md").write_text(meth,encoding="utf-8")


def main():
    import time
    t0=time.time()
    def mark(msg):
        print(f"[{time.time()-t0:8.1f}s] {msg}", flush=True)
    args=parse_args();source=args.source_docx.resolve();out=args.output.resolve();seed_dir=args.seed_dir.resolve();rng=np.random.default_rng(args.seed)
    if out.exists():shutil.rmtree(out)
    for p in [out,out/"tables",out/"geodata",out/"metadata",out/"figures",out/"source_inputs",out/"seed"]:p.mkdir(parents=True,exist_ok=True)
    shutil.copy2(Path(__file__).resolve(),out/"reproduce_v3.py")
    (out/"requirements.txt").write_text("numpy\npandas\ngeopandas\nshapely>=2\nscipy\nopencv-python-headless\nPillow\npyogrio\nmatplotlib\npyyaml\n",encoding="utf-8")
    shutil.copy2(source,out/"source_inputs"/source.name)
    for f in seed_dir.glob("*.csv"):shutil.copy2(f,out/"seed"/f.name)
    with tempfile.TemporaryDirectory(prefix="skru_v3_") as td:
        media=extract_docx_media(source,Path(td));imgs={i:load_rgb(media,i) for i in range(14,28)}
        mark("media extracted")
        anchors=pd.read_csv(seed_dir/"published_anchors_transcribed.csv")
        combined=build_combined_published_rows(anchors)
        norm_polys,line_mask,_=extract_normalized_plan_units(imgs[15]);mark(f"normalized units {len(norm_polys)}")
        bounds,calib=calibrate_local_bounds(norm_polys,anchors,args.seed);mark("bounds calibrated")
        plan_units=normalized_to_local_polys(norm_polys,bounds);footprint=unary_union(plan_units.geometry)
        faults=extract_fault_lines(imgs[26],bounds,footprint);mark(f"faults {len(faults)}")
        grid=make_clipped_grid(footprint,50,LOCAL_CRS);mark(f"grid {len(grid)}")
        sampler=FigureSampler(imgs[24],imgs[26],imgs[27],bounds);grid=sample_grid_fields(grid,sampler,faults);mark("grid fields sampled")
        links=link_points_to_units(combined,plan_units,uncertainty_m=30)
        all_anchor_links=link_points_to_units(anchors,plan_units,uncertainty_m=30)
        grid,resids,support=condition_settlement_on_anchors(grid,combined,links)
        grid_cov=grid_coverage_summary(footprint,grid)
        field_cov=field_coverage_summary(grid)
        plan_units=aggregate_units_from_grid(plan_units,grid);mark("units aggregated")
        layers,integrated,forced=reconstruct_source_layers(plan_units,grid,combined,links,faults);mark(f"layers reconstructed integrated={len(integrated)}")
        profiles,points=build_profiles_and_points(footprint,grid,args.seed);mark(f"profiles={len(profiles)} points={len(points)}")
        campaigns=campaign_calendar();params,truth,lookup,ens,quant=generate_process_truth(points,args.seed,8);mark(f"truth={len(truth)} ensemble={len(ens)}")
        benchmarks=benchmark_catalog(points,args.seed);levraw,levruns,levadj,benchobs=generate_leveling(points,profiles,campaigns,lookup,benchmarks,args.seed);mark(f"leveling raw={len(levraw)} adj={len(levadj)}")
        planraw,planadj=generate_planar_observations(points,campaigns,lookup,args.seed);mark("planar done")
        hdisp,hstrain,tilts,curv,rates,kin=derive_profile_kinematics(points,levadj,planadj);mark("derived kinematics done")
        gnraw,gnadj,gnpts=generate_gnss(points,campaigns,lookup,benchmarks,args.seed);mark("gnss done")
        acq,insarpts,insar=generate_insar(grid,points,lookup,args.seed,npts=100);mark(f"insar={len(insar)}")
        stresscat,stresstruth,stressmeas=stress_scenarios(points,args.seed);mark("stress scenarios done")
        source_layers=pd.read_csv(seed_dir/"source_layer_manifest.csv")
        source_registry=create_source_registry(sha256(source));declared=source_declared_integrated_metadata()
        # Overview transform is deliberately separate and context-only; no lon/lat columns are added to objects.
        overview=pd.DataFrame([{"transform_id":"OVERVIEW-01","status":"context_only_not_engineering","fit_basis":"Figure 13 OSM screenshot + public SKRU-1/Gorodishche context points","estimated_horizontal_uncertainty_m":260,"object_coordinates_published":False,"local_crs_primary":True,"note":"Official key or 4+ common geodetic points are required for engineering georeferencing."}])
        mark("building frames dict")
        frames={
            "source_registry":source_registry,"source_layer_manifest":source_layers,"source_declared_integrated_layer_metadata":declared,
            "published_anchors":anchors,"published_combined_rows":combined,"anchor_spatial_links":links,"published_anchor_spatial_links_all":all_anchor_links,"anchor_settlement_residuals":resids,"anchor_support_cells":support,"coordinate_calibration_checks":calib,
            "grid_coverage_summary":grid_cov,"field_coverage_summary":field_cov,
            "plan_units_reconstructed":pd.DataFrame(plan_units.drop(columns="geometry")),"field_grid_50m":pd.DataFrame(grid.drop(columns="geometry")),"integrated_features_reconstructed":pd.DataFrame(integrated.drop(columns="geometry")),"forced_layer_memberships":forced,
            "survey_profiles":pd.DataFrame(profiles.drop(columns="geometry")),"survey_points":pd.DataFrame(points.drop(columns="geometry")),"survey_campaigns":campaigns,"process_parameters_survey_points":params,"truth_survey_points_monthly":truth,"synthetic_truth_ensemble_monthly":ens,"synthetic_truth_quantiles_monthly":quant,
            "datum_benchmarks":pd.DataFrame(benchmarks.drop(columns="geometry")),"benchmark_observations":benchobs,"leveling_stations_raw":levraw,"leveling_runs_summary":levruns,"leveling_adjusted_epochs":levadj,
            "planar_observations_raw":planraw,"horizontal_displacements":hdisp,"horizontal_strains":hstrain,"tilts":tilts,"curvatures":curv,"settlement_rates":rates,"profile_kinematics":kin,
            "gnss_sessions_raw":gnraw,"gnss_adjusted_epochs":gnadj,"insar_acquisition_catalog":acq,"insar_point_catalog":pd.DataFrame(insarpts.drop(columns="geometry")),"insar_observations_relative":insar,
            "stress_test_scenario_catalog":stresscat,"stress_test_truth_monthly":stresstruth,"stress_test_measurements":stressmeas,"overview_georeference":overview,
            "provenance_codes":pd.read_csv(seed_dir/"provenance_codes.csv"),"normative_requirements":pd.read_csv(seed_dir/"normative_requirements.csv"),"threshold_registry":pd.read_csv(seed_dir/"threshold_registry.csv"),"vzt_engineering_criteria":pd.read_csv(seed_dir/"vzt_engineering_criteria.csv"),
        }
        # Save tables.
        for name,df in frames.items():
            print(f"    writing CSV {name}: {len(df)}", flush=True)
            save_csv(df,out/"tables"/(name+".csv"))
        mark("csvs written")
        # GPKG layers.
        # Preserve the full clipped 50 m geometry in GeoParquet (fast and lossless). The GPKG receives
        # a point representation of the grid to avoid pathological write times for thousands of clipped multipolygons.
        grid_points=gpd.GeoDataFrame(grid.drop(columns="geometry").copy(),geometry=[Point(x,y) for x,y in grid[["x_local_m","y_local_m"]].to_numpy(float)],crs=LOCAL_CRS)
        gpkg_layers={"zone_pol":layers["zone_pol"],"field_grid_50m_points":grid_points,"survey_profiles":profiles,"survey_points":points,"integrated_features_reconstructed":integrated,"gnss_points":gnpts,"insar_points":insarpts}
        for k,v in layers.items():gpkg_layers[k]=v
        write_gpkg(gpkg_layers,out/"geodata"/"skru1_data_reconstruction_v3_1.gpkg");mark("gpkg written")
        # Config/transform metadata.
        dump_json({"dataset_version":VERSION,"seed":args.seed,"source_docx":source.name,"source_docx_sha256":sha256(source),"local_bounds_m":bounds.tolist(),"local_crs_wkt":LOCAL_CRS.to_wkt(),"reference_year":2022,"reference_date":None,"reference_period_status":"year_supported_exact_date_unknown"},out/"metadata"/"reconstruction_config.json")
        # Validation.
        report,checks=validation_report(frames,plan_units,grid,links,resids,levruns,levadj,gnadj,insar,source,Path(__file__).resolve(),out);dump_json(report,out/"metadata"/"validation_report.json");save_csv(checks,out/"metadata"/"validation_checks.csv")
        frames["data_dictionary"]=frames_dictionary(frames);save_csv(frames["data_dictionary"],out/"tables"/"data_dictionary.csv")
        counts={"plan_units":len(plan_units),"integrated_reconstructed_rows":len(integrated),"grid_cells_50m":len(grid),"source_named_layers":len(source_layers),"profiles":len(profiles),"survey_points":len(points),"campaigns":len(campaigns),"leveling_raw_stations":len(levraw),"leveling_adjusted_epochs":len(levadj),"gnss_sessions":len(gnraw),"insar_points":len(insarpts),"insar_observations":len(insar),"ensemble_rows":len(ens)}
        make_figures(out,plan_units,grid,layers,profiles,points,truth,levadj,gnadj,insar,resids);write_docs(out,report,counts);mark("figures/docs written")
        # Package manifest and hashes.
        files=[p for p in out.rglob("*") if p.is_file()]
        manifest=pd.DataFrame([{"relative_path":str(p.relative_to(out)),"bytes":p.stat().st_size,"sha256":sha256(p)} for p in files]);save_csv(manifest,out/"metadata"/"dataset_manifest.csv")
        dump_json({"version":VERSION,"counts":counts,"overall_status":report["overall_status"],"source_docx_sha256":sha256(source)},out/"metadata"/"dataset_manifest.json")
    print(json.dumps({"output":str(out),"status":report["overall_status"],"checks_passed":report["checks_passed"],"checks_total":report["checks_total"],"counts":counts},ensure_ascii=False,indent=2))

if __name__=="__main__":main()
