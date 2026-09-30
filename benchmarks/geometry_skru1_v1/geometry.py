"""Source geometry and diagnostic registration; no solver, interpolation or guessed labels.

Input/output coordinates are explicit. GeoJSON in source coordinates is a local
interchange container and MUST NOT be interpreted as RFC 7946 longitude/latitude.
"""
from __future__ import annotations

import hashlib
import json
import math
import io
import struct
from datetime import date
from typing import Any

import numpy as np

VERSION = "geometry-skru1/0.1"


def inspect_emf(data: bytes) -> dict:
    """Inspect actual EMF records; a vector container may contain only a bitmap."""
    offset, records, bitmaps = 0, [], []
    while offset + 8 <= len(data):
        record_type, size = struct.unpack_from("<II",data,offset)
        if size < 8 or offset + size > len(data):
            raise ValueError("invalid EMF record size")
        record=data[offset:offset+size]
        records.append({"offset":offset,"type":record_type,"size":size})
        if record_type == 81:  # EMR_STRETCHDIBITS
            if size < 80: raise ValueError("truncated EMR_STRETCHDIBITS")
            bmi_off,bmi_size,bits_off,bits_size=struct.unpack_from("<4I",record,48)
            if min(bmi_off,bits_off) < 80 or max(bmi_off+bmi_size,bits_off+bits_size) > size:
                raise ValueError("EMF bitmap spans outside record")
            bmi=record[bmi_off:bmi_off+bmi_size]; bits=record[bits_off:bits_off+bits_size]
            header=struct.pack("<2sIHHI",b"BM",14+len(bmi)+len(bits),0,0,14+len(bmi))
            bitmap=header+bmi+bits
            x,y=struct.unpack_from("<2i",record,24); width,height=struct.unpack_from("<2i",record,72)
            bitmaps.append({"record_index":len(records)-1,"bitmap_bmp":bitmap,"bitmap_sha256":hashlib.sha256(bitmap).hexdigest(),
                            "dest_rect_emf":[x,y,x+width,y+height],"raster_transform_status":"SOURCE_DEST_EXTENTS_PRESERVED"})
        offset += size
    if offset != len(data): raise ValueError("trailing/unparsed EMF bytes")
    vector_types={2,3,4,5,6,7,8,27,42,43,44,45,46,47,54,55,56,59,60,61,62,64,65,66,67,68,85,86,87,88,89,90,91,92}
    present={r["type"] for r in records}
    # Conservative classification: an unknown render record must never silently
    # become "raster-only" just because its primitive is absent from our allowlist.
    return {"record_inventory":records,"bitmaps":bitmaps,
            "content_class":"RASTER_ONLY_IN_EMF_CONTAINER" if bitmaps and present <= {1,14,81} else "VECTOR_OR_MIXED_REQUIRES_NATIVE_RENDERER",
            "vector_primitive_records":sum(r["type"] in vector_types for r in records)}


def stable_id(namespace: str, value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return namespace + "-" + hashlib.sha256(data.encode("utf-8")).hexdigest()[:20]


def feature(geometry: dict | None, properties: dict) -> dict:
    properties = dict(properties)
    properties.setdefault("stable_id", stable_id("GEO", [geometry, properties]))
    return {"type": "Feature", "id": properties["stable_id"], "geometry": geometry, "properties": properties}


def collection(features: list[dict], coordinate_system: str, **metadata: Any) -> dict:
    units = "px" if "PIXEL" in coordinate_system else ("m" if "LOCAL_ENGINEERING" in coordinate_system else "UNKNOWN" if "UNKNOWN" in coordinate_system else "pt")
    if coordinate_system == "PER_FEATURE_SOURCE_FRAME": units = "PER_FEATURE"
    return {"type": "FeatureCollection", "features": features,
            "coordinate_reference": {"status": coordinate_system, "authority": "UNKNOWN",
                                     "axis_order": "x,y", "coordinate_units": units},
            "metadata": metadata}


def point(value: Any) -> list[float]:
    if isinstance(value, dict):
        return [float(value["x"]), float(value["y"])]
    return [float(value[0]), float(value[1])]


def _distance_to_chord(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    delta = b - a
    length2 = float(delta @ delta)
    if length2 == 0:
        return float(np.linalg.norm(p - a))
    t = float(np.clip((p - a) @ delta / length2, 0, 1))
    return float(np.linalg.norm(p - (a + t * delta)))


def flatten_cubic(points: list, tolerance: float = 0.1, max_depth: int = 20) -> list[list[float]]:
    """Adaptive de Casteljau; records a bounded chord tolerance in source units."""
    if tolerance <= 0 or not math.isfinite(tolerance):
        raise ValueError("positive finite flatten tolerance required")
    p = np.asarray([point(v) for v in points], dtype=float)
    if p.shape != (4, 2) or not np.isfinite(p).all():
        raise ValueError("four finite Bezier control points required")
    def split(q: np.ndarray, depth: int) -> list[list[float]]:
        deviation = max(_distance_to_chord(q[1], q[0], q[3]), _distance_to_chord(q[2], q[0], q[3]))
        if deviation <= tolerance:
            return [q[0].tolist(), q[3].tolist()]
        if depth >= max_depth:
            raise ValueError("Bezier flattening failed to reach declared tolerance")
        a, b, c = (q[:-1] + q[1:]) / 2
        d, e = (a + b) / 2, (b + c) / 2
        m = (d + e) / 2
        return split(np.array([q[0], a, d, m]), depth + 1)[:-1] + split(np.array([m, e, c, q[3]]), depth + 1)
    return split(p, 0)


def clip_segment(a: list, b: list, box: list) -> tuple[list, list] | None:
    """Liang–Barsky rectangular clipping in unrotated source coordinates."""
    x0, y0, x1, y1 = map(float, box)
    dx, dy = b[0] - a[0], b[1] - a[1]
    lo, hi = 0.0, 1.0
    for p, q in [(-dx, a[0]-x0), (dx, x1-a[0]), (-dy, a[1]-y0), (dy, y1-a[1])]:
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            lo = max(lo, t)
        else:
            hi = min(hi, t)
        if lo > hi:
            return None
    return [a[0] + lo*dx, a[1] + lo*dy], [a[0] + hi*dx, a[1] + hi*dy]


def clip_polyline(line: list, box: list) -> list[list[list[float]]]:
    parts = []
    active = []
    for a, b in zip(line, line[1:]):
        segment = clip_segment(a, b, box)
        if segment is None:
            if len(active) >= 2:
                parts.append(active)
            active = []
            continue
        c, d = segment
        if active and not np.allclose(active[-1], c, atol=1e-8):
            if len(active) >= 2:
                parts.append(active)
            active = []
        if not active:
            active = [c]
        active.append(d)
    if len(active) >= 2:
        parts.append(active)
    return parts


def _primitive(item: list, tolerance: float) -> list:
    kind = item[0]
    if kind == "l":
        return [point(item[1]), point(item[2])]
    if kind == "c":
        return flatten_cubic(item[1:5], tolerance)
    if kind == "re":
        rect = item[1]
        if isinstance(rect, dict):
            rect = [rect[k] for k in ("x0", "y0", "x1", "y1")]
        x0, y0, x1, y1 = map(float, rect)
        vertices = [[x0,y0], [x1,y0], [x1,y1], [x0,y1], [x0,y0]]
        return list(reversed(vertices)) if len(item) > 2 and item[2] < 0 else vertices
    if kind == "qu":
        q = item[1]
        if len(q) == 8 and isinstance(q[0], (int,float)):
            ul, ur, ll, lr = [q[i:i+2] for i in range(0,8,2)]
        else:
            ul, ur, ll, lr = map(point, q)
        return [ul, ur, lr, ll, ul]
    raise ValueError(f"unsupported native primitive: {kind}")


def native_paths(payload: dict, base: dict, tolerance: float = 0.1) -> tuple[list[dict], list[dict]]:
    """Preserve paint order/group/clip state. Complex clips are explicitly unresolved.

    PyMuPDF get_drawings(extended=True) already expresses primitives in unrotated
    page coordinates. Never reapply page rotation or object CTM to those vertices.
    """
    paths, issues, stack = [], [], []
    figure_box = payload.get("bbox_native_pt")
    if figure_box is None and payload.get("rotation",0) == 0:
        figure_box = payload.get("bbox_page_pt")
    for index, drawing in enumerate(payload.get("drawings", [])):
        level = int(drawing.get("level", 0))
        stack = [s for s in stack if s["level"] < level]
        kind = drawing.get("type", "UNKNOWN")
        if kind in ("clip", "group"):
            stack.append({"index": index, "level": level, "kind": kind, "raw": drawing})
            continue
        current_clips = [s for s in stack if s["kind"] == "clip"]
        simple_clips = [s["raw"]["scissor"] for s in current_clips
                        if s["raw"].get("scissor") and all(it[0] == "re" for it in s["raw"].get("items", []))]
        complex_clips = [s["index"] for s in current_clips if not s["raw"].get("scissor")
                         or any(it[0] != "re" for it in s["raw"].get("items", []))]
        lines = []
        for ordinal, item in enumerate(drawing.get("items", [])):
            try:
                parts = [_primitive(item, tolerance)]
                for box in ([figure_box] if figure_box else []) + simple_clips:
                    parts = [p for line in parts for p in clip_polyline(line, box)]
                lines.extend(parts)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                issues.append({"drawing": index, "primitive": ordinal, "reason": str(exc), "status": "ASTRA_REVIEW_REQUIRED"})
        if not lines:
            continue
        props = {**base, "source_object_id": f"{base['key']}:drawing:{index}", "paint_order": drawing.get("seqno", index),
                 "method": "NATIVE_PDF_PATHS", "epistemic_status": "DERIVATION", "verification": "AUTO_EXTRACTED_UNREVIEWED",
                 "native_type": kind, "semantic_class": "UNKNOWN", "rotation_preserved": payload.get("rotation", 0),
                 "native_coordinate_system": payload.get("coordinate_system", "PDF_UNROTATED_PT"),
                 "clip_state": [{k:s[k] for k in ("index", "level", "kind")} for s in stack],
                 "clip_status": "UNAPPLIED_COMPLEX_CLIP" if complex_clips else "RECTANGULAR_CLIPS_APPLIED",
                 "native_payload": base.get("native_file", "UNKNOWN"), "native_style": {k:drawing.get(k) for k in
                    ("color", "fill", "width", "dashes", "fill_opacity", "stroke_opacity", "closePath", "even_odd", "layer")},
                 "flatten_tolerance_source_units": tolerance}
        if complex_clips:
            issues.append({"drawing": index, "reason": "Complex clip preserved but not geometrically applied", "clip_indexes": complex_clips,
                           "status": "ASTRA_REVIEW_REQUIRED"})
        geom = {"type": "MultiLineString", "coordinates": lines}
        paths.append(feature(geom, props))
    return paths, issues


def raster_mask(rgb: np.ndarray, mode: str, roi: list[int] | None = None, colour: list[int] | None = None,
                lab_tolerance: float = 18.0) -> np.ndarray:
    import cv2
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("RGB image required")
    values = rgb.astype(np.int16)
    if mode == "red_line":
        mask = ((values[:,:,0] > 150) & (values[:,:,0]-values[:,:,1] > 65) & (values[:,:,0]-values[:,:,2] > 50))
    elif mode == "dark_line":
        mask = values.max(axis=2) < 110
    elif mode == "legend_colour":
        if colour is None:
            raise ValueError("explicit sampled legend colour required")
        image_lab = cv2.cvtColor(rgb.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
        target = cv2.cvtColor(np.array(colour, dtype=np.float32).reshape(1,1,3) / 255, cv2.COLOR_RGB2LAB)[0,0]
        mask = np.linalg.norm(image_lab-target, axis=2) <= lab_tolerance
    else:
        raise ValueError("unknown raster mask mode")
    out = mask.astype(np.uint8) * 255
    if roi:
        x0,y0,x1,y1 = map(int,roi)
        clipped = np.zeros_like(out)
        clipped[max(0,y0):min(out.shape[0],y1),max(0,x0):min(out.shape[1],x1)] = out[max(0,y0):min(out.shape[0],y1),max(0,x0):min(out.shape[1],x1)]
        out = clipped
    return out


def contours(mask: np.ndarray, base: dict, *, minimum_area: float = 20, epsilon: float = 0.75,
             semantic_class: str = "UNKNOWN", method: str = "RASTER_MASK_CONTOUR") -> list[dict]:
    """Trace connected pixels including holes; does not call a cell a mine zone."""
    import cv2
    found, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    output = []
    def ring(contour: np.ndarray) -> list:
        pts = cv2.approxPolyDP(contour, epsilon, True).reshape(-1,2).astype(float).tolist()
        return pts+[pts[0]] if len(pts) >= 3 else []
    for idx, contour in enumerate(found):
        if hierarchy[0,idx,3] != -1 or abs(cv2.contourArea(contour)) < minimum_area:
            continue
        exterior = ring(contour)
        if not exterior:
            continue
        rings, child = [exterior], hierarchy[0,idx,2]
        while child != -1:
            child_ring = ring(found[child])
            if child_ring and abs(cv2.contourArea(found[child])) >= 1:
                rings.append(child_ring)
            child = hierarchy[0,child,0]
        props = {**base, "source_object_id": f"{base['key']}:component:{idx}", "method": method,
                 "epistemic_status": "DERIVATION", "verification": "AUTO_EXTRACTED_UNREVIEWED",
                 "semantic_class": semantic_class, "topology_status": "CANDIDATE_PIXEL_COMPONENT",
                 "pixel_area": abs(float(cv2.contourArea(contour))), "hole_count": len(rings)-1,
                 "simplification_tolerance_px": epsilon, "label_object_match": "UNKNOWN"}
        output.append(feature({"type":"Polygon", "coordinates": rings},props))
    return output


def enclosed_regions(mask: np.ndarray, base: dict, minimum_area: float = 40) -> list[dict]:
    """Candidate cells enclosed by visible linework; open gaps stay open."""
    import cv2
    inverse = 255 - mask
    count, labels, stats, _ = cv2.connectedComponentsWithStats(inverse, connectivity=4)
    out = []
    h,w = mask.shape
    for n in range(1,count):
        x,y,bw,bh,area = map(int,stats[n])
        if area < minimum_area or x == 0 or y == 0 or x+bw >= w or y+bh >= h:
            continue
        component = (labels == n).astype(np.uint8)*255
        out.extend(contours(component, {**base,"pixel_component":n}, minimum_area=minimum_area,
                            semantic_class="UNKNOWN_ENCLOSED_REGION", method="VISIBLE_LINE_ENCLOSURE"))
    return out


def line_segments(mask: np.ndarray, base: dict, minimum_length: int = 30) -> list[dict]:
    import cv2
    detected = cv2.HoughLinesP(mask, 1, np.pi/1800, threshold=25, minLineLength=minimum_length, maxLineGap=2)
    output = []
    # OpenCV wheel builds return either Nx1x4 or Nx4; preserve all four endpoints.
    for n, values in enumerate(np.asarray(detected).reshape(-1,4).tolist() if detected is not None else []):
        x0,y0,x1,y1 = values
        output.append(feature({"type":"LineString","coordinates":[[x0,y0],[x1,y1]]},
            {**base,"source_object_id":f"{base['key']}:hough:{n}","method":"HOUGH_LINES_P",
             "verification":"AUTO_EXTRACTED_UNREVIEWED","epistemic_status":"DERIVATION","semantic_class":"UNKNOWN",
             "maximum_gap_px":2,"interpretation":"candidate source graphic; not assigned to workings/section boundaries"}))
    return output


def fit_transform(source: list, target: list, kind: str = "similarity", justification: str | None = None) -> dict:
    s,t = np.asarray(source,dtype=float),np.asarray(target,dtype=float)
    if s.ndim != 2 or s.shape[1] != 2 or t.shape != s.shape or not np.isfinite(s).all() or not np.isfinite(t).all():
        raise ValueError("matching finite Nx2 control points required")
    if kind not in ("similarity","affine","projective"):
        raise ValueError("unsupported transform")
    if kind != "similarity" and not justification:
        raise ValueError("affine/projective requires explicit source distortion justification")
    minimum = {"similarity":2,"affine":3,"projective":4}[kind]
    if len(s) < minimum or len(np.unique(s,axis=0)) < minimum:
        raise ValueError("insufficient distinct controls")
    x,y = s.T
    if kind == "similarity":
        a = np.zeros((2*len(s),4)); a[0::2] = np.column_stack([x,-y,np.ones(len(s)),np.zeros(len(s))]); a[1::2] = np.column_stack([y,x,np.zeros(len(s)),np.ones(len(s))])
        b=t.reshape(-1); p,_,rank,_ = np.linalg.lstsq(a,b,rcond=None)
        if rank != 4: raise ValueError("degenerate similarity controls")
        matrix=np.array([[p[0],-p[1],p[2]],[p[1],p[0],p[3]],[0,0,1]])
    elif kind == "affine":
        a=np.column_stack([s,np.ones(len(s))]); p,_,rank,_=np.linalg.lstsq(a,t,rcond=None)
        if rank != 3: raise ValueError("collinear affine controls")
        matrix=np.vstack([p.T,[0,0,1]])
    else:
        # Normalized DLT avoids instability at engineering-coordinate magnitudes.
        def norm(v: np.ndarray) -> tuple[np.ndarray,np.ndarray]:
            centre=v.mean(0); radius=np.linalg.norm(v-centre,axis=1).mean()
            if radius == 0: raise ValueError("degenerate projective controls")
            z=math.sqrt(2)/radius; m=np.array([[z,0,-z*centre[0]],[0,z,-z*centre[1]],[0,0,1]])
            return apply_transform(v.tolist(),m.tolist()),m
        sn,ns=norm(s); tn,nt=norm(t); a=[]
        for (sx,sy),(tx,ty) in zip(sn,tn):
            a.extend([[-sx,-sy,-1,0,0,0,tx*sx,tx*sy,tx],[0,0,0,-sx,-sy,-1,ty*sx,ty*sy,ty]])
        a=np.asarray(a); _,_,vh=np.linalg.svd(a,full_matrices=True)
        if np.linalg.matrix_rank(a) < 8: raise ValueError("degenerate projective controls")
        matrix=np.linalg.inv(nt)@vh[-1].reshape(3,3)@ns
        if abs(matrix[2,2]) < 1e-14: raise ValueError("projective normalization unstable")
        matrix/=matrix[2,2]
    if abs(np.linalg.det(matrix)) < 1e-14:
        raise ValueError("singular transform")
    predictions=apply_transform(s.tolist(),matrix.tolist()); residuals=np.linalg.norm(predictions-t,axis=1)
    result={"kind":kind,"matrix":matrix.tolist(),"justification":justification,"control_count":len(s),
            "residuals":residuals.tolist(),"rms":float(np.sqrt(np.mean(residuals**2))),"max_residual":float(residuals.max()),
            "spatial_distribution":{"source_bbox":[*s.min(0).tolist(),*s.max(0).tolist()],
                                    "covariance_eigenvalues":np.linalg.eigvalsh(np.cov(s.T)).tolist() if len(s)>2 else [0,0]}}
    return result


def apply_transform(values: list, matrix: list) -> np.ndarray:
    p=np.asarray(values,dtype=float); m=np.asarray(matrix,dtype=float)
    q=np.column_stack([p,np.ones(len(p))])@m.T
    if np.any(np.abs(q[:,2]) < 1e-14):
        raise ValueError("projective point at infinity")
    return q[:,:2]/q[:,2,None]


def registration_qa(source: list, target: list, kind: str = "similarity", justification: str | None = None) -> dict:
    result=fit_transform(source,target,kind,justification)
    n=len(source); loo=[]
    for i in range(n):
        indices=[j for j in range(n) if j != i]
        try:
            fold=fit_transform([source[j] for j in indices],[target[j] for j in indices],kind,justification)
            predicted=apply_transform([source[i]],fold["matrix"])[0]
            error=float(np.linalg.norm(predicted-np.asarray(target[i])))
            loo.append({"index":i,"error":error,"predicted":predicted.tolist(),"status":"COMPUTED"})
        except ValueError as exc:
            loo.append({"index":i,"error":None,"status":"UNIDENTIFIABLE_WITH_POINT_REMOVED","reason":str(exc)})
    errors=[p["error"] for p in loo if p["error"] is not None]
    result["leave_one_out"]={"points":loo,"rms":float(np.sqrt(np.mean(np.square(errors)))) if errors else None,
                             "max":max(errors) if errors else None,"computed_count":len(errors),
                             "status":"COMPLETE" if len(errors)==n else "PARTIAL_OR_UNIDENTIFIABLE"}
    result["acceptance"]="DIAGNOSTIC_ONLY_UNTIL_CONTROL_IDENTITIES_VERIFIED"
    return result


def attribute(value: Any, source_id: str, locator: dict, status: str, **extra: Any) -> dict:
    """Every literal/derived attribute has independent provenance; UNKNOWN stays explicit."""
    return {"value":value,"source_id":source_id,"locator":locator,"verification":status,
            "epistemic_status":"UNKNOWN" if value in (None,"UNKNOWN","UNREADABLE") else "DERIVATION",
            **extra}


def temporal_qa(records: list[dict]) -> list[dict]:
    """Check contradictions; plan dates never become execution dates."""
    findings=[]
    for r in records:
        start,end,back=r.get("mining_start"),r.get("mining_end"),r.get("backfill_year")
        if isinstance(start,int) and isinstance(end,int) and end < start:
            findings.append({"id":r.get("id"),"kind":"MINING_END_BEFORE_START"})
        if isinstance(start,int) and isinstance(back,int) and back < start:
            findings.append({"id":r.get("id"),"kind":"BACKFILL_BEFORE_MINING"})
        if r.get("source_role") == "PLANNED" and r.get("execution_status") == "EXECUTED":
            findings.append({"id":r.get("id"),"kind":"PLAN_PROMOTED_TO_EXECUTION"})
    return findings


def validate_polygon(rings: list) -> list[str]:
    """Data-free topology checks; intersections require exact segment tests."""
    def orient(a,b,c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def intersects(a,b,c,d):
        u,v,w,z=orient(a,b,c),orient(a,b,d),orient(c,d,a),orient(c,d,b)
        return u*v < 0 and w*z < 0
    issues=[]
    for ring in rings:
        if len(ring) < 4 or ring[0] != ring[-1]: issues.append("RING_NOT_CLOSED"); continue
        if not np.isfinite(np.asarray(ring,dtype=float)).all(): issues.append("NONFINITE_COORDINATE"); continue
        n=len(ring)-1
        for i in range(n):
            for j in range(i+2,n):
                if i == 0 and j == n-1: continue
                if intersects(ring[i],ring[i+1],ring[j],ring[j+1]): issues.append("SELF_INTERSECTION"); break
            if issues and issues[-1] == "SELF_INTERSECTION": break
    return sorted(set(issues))
