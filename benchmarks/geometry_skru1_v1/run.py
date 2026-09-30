"""Run deterministic source-geometry extraction on the current private inventory.

All data, source coordinates and literal tables stay in git-ignored work/.
This does not create a calibrated PhysicalWorld or execute a solver.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import geometry as G
from source_regions import load_regions, regions_for, validate_rectangle

REQUIRED_LAYERS=("source_figures","control_points","control_point_matches","transforms","mine_boundaries","shafts",
                 "boreholes","profile_lines","panels","blocks","mining_zones","anomalies","geology_sections","stratigraphy","conflicts")
SOURCE_IDS={"VKM-SRC-011","VKM-SRC-012","VKM-SRC-014","VKM-SRC-064","VKM-SRC-197","VKM-SRC-252"}
PRIORITY={"F023_007","F023_014","F023_024","F023_025","F023_026"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path,value: object) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8",newline="\n")


def local_file(repo: Path,value: str) -> Path:
    path=(repo/value).resolve()
    if not path.is_relative_to(repo.resolve()): raise ValueError("input escaped repository")
    return path


def overlay(image: Image.Image,features: list[dict],path: Path,native: dict | None = None) -> None:
    rgba=image.convert("RGBA"); layer=Image.new("RGBA",image.size,(0,0,0,0)); draw=ImageDraw.Draw(layer)
    def project(points):
        if native is None: return [tuple(map(float,p)) for p in points]
        box=native["bbox_page_pt"]
        matrix=native.get("page_rotation_matrix",[1,0,0,1,0,0])
        a,b,c,d,e,f=matrix
        mapped=[(a*p[0]+c*p[1]+e,b*p[0]+d*p[1]+f) for p in points]
        return [((p[0]-box[0])*image.width/(box[2]-box[0]),(p[1]-box[1])*image.height/(box[3]-box[1])) for p in mapped]
    for index,ft in enumerate(features):
        geom=ft.get("geometry")
        if not geom: continue
        typ,coords=geom["type"],geom["coordinates"]
        colour=(10,100,255,150) if index%2 else (255,0,160,150)
        lines=coords if typ in ("Polygon","MultiLineString") else ([coords] if typ == "LineString" else [])
        for line in lines:
            points=project(line)
            if len(points)>=2: draw.line(points,fill=colour,width=2)
        if typ == "Point":
            x,y=project([coords])[0]; draw.ellipse((x-4,y-4,x+4,y+4),outline=(255,0,0,230),width=2)
    path.parent.mkdir(parents=True,exist_ok=True)
    Image.alpha_composite(rgba,layer).convert("RGB").save(path)


def image_correspondence(source: Image.Image,target: Image.Image,source_roi: list[int],target_roi: list[int]) -> dict:
    """ORB supplies diagnostic proposals, never approved engineering GCP."""
    import cv2
    cv2.setRNGSeed(0)
    a,b=np.asarray(source.convert("RGB")),np.asarray(target.convert("RGB"))
    ma=np.zeros(a.shape[:2],np.uint8); mb=np.zeros(b.shape[:2],np.uint8)
    x0,y0,x1,y1=source_roi; ma[y0:y1,x0:x1]=255
    x0,y0,x1,y1=target_roi; mb[y0:y1,x0:x1]=255
    orb=cv2.ORB_create(nfeatures=1600,scaleFactor=1.15,nlevels=12,edgeThreshold=12,fastThreshold=10)
    ka,da=orb.detectAndCompute(cv2.cvtColor(a,cv2.COLOR_RGB2GRAY),ma)
    kb,db=orb.detectAndCompute(cv2.cvtColor(b,cv2.COLOR_RGB2GRAY),mb)
    if da is None or db is None: return {"status":"NO_MATCHES","controls":[]}
    raw=cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da,db,k=2)
    matches=[m for pair in raw if len(pair)==2 for m,n in [pair] if m.distance < .7*n.distance]
    if len(matches)<4: return {"status":"INSUFFICIENT_MATCHES","controls":[],"candidate_count":len(matches)}
    points_a=np.array([ka[m.queryIdx].pt for m in matches],dtype=float)
    points_b=np.array([kb[m.trainIdx].pt for m in matches],dtype=float)
    matrix,inliers=cv2.estimateAffinePartial2D(points_a,points_b,method=cv2.RANSAC,ransacReprojThreshold=3,maxIters=5000,confidence=.999)
    if matrix is None: return {"status":"NO_CONSISTENT_SIMILARITY","controls":[]}
    ids=np.flatnonzero(inliers[:,0])
    # Distinct controls; correlated texture matches are not independent evidence.
    controls=[]
    for i in ids:
        p,q=points_a[i],points_b[i]
        if any(np.linalg.norm(p-np.asarray(c["source"]))<8 for c in controls): continue
        controls.append({"source":p.tolist(),"target":q.tolist(),"verification":"AUTO_EXTRACTED_UNREVIEWED",
                         "semantic_identity":"UNKNOWN","hamming_distance":float(matches[i].distance)})
    if len(controls)<3: return {"status":"INSUFFICIENT_DISTINCT_CONTROLS","controls":controls}
    result=G.registration_qa([c["source"] for c in controls],[c["target"] for c in controls])
    return {"status":"ASTRA_REVIEW_REQUIRED","controls":controls,"diagnostic_transform":result,
            "candidate_count":len(matches),"ransac_inlier_count":len(ids),"coordinate_units":"target_source_image_px",
            "registration_to_mine_grid":"BLOCKED_NO_VERIFIED_LABEL_OBJECT_MATCH",
            "warning":"RANSAC-selected texture matches; LOO is conditional and cannot prove scientific identity or physical accuracy"}


def gold_attributes(repo: Path,base_jobs: dict) -> tuple[list[dict],list[dict],list[dict]]:
    points,records,issues=[],[],[]
    for filename in sorted((repo/"work/figure_readings_2026-09-29/claude").glob("F023_*_claude.csv")):
        key=filename.stem.removesuffix("_claude"); job=base_jobs.get(key,{})
        for row_index,row in enumerate(csv.DictReader(filename.open(encoding="utf-8-sig")),1):
            row_id=G.stable_id("GOLDROW",[key,row_index,row])
            attrs={}
            for column,value in row.items():
                status="TRUNCATED" if "[обрезано]" in value else row.get("status","VERIFIED_BY_EYE")
                locator={"figure_key":key,"gold_file":filename.relative_to(repo).as_posix(),"gold_sha256":sha(filename),"row":row_index,"column":column,
                         "source_image_sha256":job.get("original_sha256","UNKNOWN")}
                attrs[column]=G.attribute(value or "UNKNOWN","VKM-SRC-023",locator,status,
                    source_role="SECONDARY_SOURCE_TRANSCRIPTION",scope="SKRU1_TABLE_SCOPE",scale="AS_PRINTED")
            record={"id":row_id,"key":key,"attributes":attrs,"verification":row.get("status","VERIFIED_BY_EYE"),
                    "mining_start":"UNKNOWN","mining_end":"UNKNOWN","backfill_year":"UNKNOWN",
                    "source_role":"SECONDARY_SOURCE","execution_status":"UNKNOWN"}
            records.append(record)
            xcol=next((c for c in row if c in ("xcoord","exel_X_coord")),None)
            ycol=next((c for c in row if c in ("ycoord","exel_Y_coord")),None)
            if xcol and ycol and not row.get("status","").startswith("LOW_CONFIDENCE"):
                xy=[float(row[xcol].replace(",",".")),float(row[ycol].replace(",","."))]
                points.append(G.feature({"type":"Point","coordinates":xy},
                    {"stable_id":row_id,"source_id":"VKM-SRC-023","key":key,"source_object_id":row_id,
                     "coordinate_reference":"LOCAL_TABLE_COORDINATE_UNITS_UNKNOWN","coordinate_units":"UNKNOWN","coordinate_units_verified":False,
                     "method":"VERIFIED_GOLD_TABLE_COORDINATES","verification":"VERIFIED_BY_EYE",
                     "epistemic_status":"DERIVATION","attributes":attrs,"role":"TARGET_COORDINATE_NOT_YET_MATCHED_TO_SOURCE_PIXEL",
                     "label_object_match":"UNKNOWN","valid_from":"UNKNOWN","valid_to":"UNKNOWN"}))
            elif xcol and ycol:
                issues.append({"key":key,"reason":"Half-cropped gold coordinate row excluded from GCP","row":row_index,
                               "status":"ASTRA_REVIEW_REQUIRED","downstream_impact":"registration"})
    return points,records,issues


def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument("--inventory",default="work/figure_readings_2026-09-29/v2/inventory.json")
    parser.add_argument("--out",default="work/geometry_2026-09-29"); parser.add_argument("--targeted",action="store_true")
    parser.add_argument("--source-regions",default="work/geometry_2026-09-29/source_regions.json")
    args=parser.parse_args(); repo=Path.cwd().resolve(); out=local_file(repo,args.out)
    if not out.is_relative_to(repo/"work"): raise ValueError("private geometry output must stay in ignored work/")
    if (out/"qgis_import_manifest.json").exists(): raise ValueError("frozen geometry package exists; select a new --out version")
    regions_path=local_file(repo,args.source_regions)
    if not regions_path.is_relative_to(repo/"work"): raise ValueError("source-region coordinates must remain in ignored work/")
    source_regions=load_regions(regions_path)
    out.mkdir(parents=True,exist_ok=True)
    inventory=json.loads(local_file(repo,args.inventory).read_text(encoding="utf-8"))
    additional=out/"additional_native_inventory.json"
    if args.targeted:
        from targeted import extract
        resources=Path(os.environ.get("VKM_RESOURCES_ROOT",repo/"vkm-subsidence-forecasting_resourses"))
        extract(repo,out,resources)
    if additional.exists(): inventory+=json.loads(additional.read_text(encoding="utf-8"))
    jobs=json.loads((repo/"work/figure_readings_2026-09-29/jobs.json").read_text(encoding="utf-8"))
    base_jobs={j["key"]:j for j in jobs}
    selected=[r for r in inventory if r["key"] in PRIORITY or r["source_id"] in SOURCE_IDS]
    layers=[]; reviews=[]; figures=[]; inputs={args.inventory:sha(local_file(repo,args.inventory)),args.source_regions:sha(regions_path)}; stats=[]
    for filename in sorted((repo/"work/figure_readings_2026-09-29/claude").glob("F023_*_claude.csv")):
        inputs[filename.relative_to(repo).as_posix()]=sha(filename)
    for name in ("geometry.py","run.py","source_regions.py","targeted.py"):
        source=repo/"benchmarks/geometry_skru1_v1"/name
        inputs[source.relative_to(repo).as_posix()]=sha(source)
    if additional.exists(): inputs[additional.relative_to(repo).as_posix()]=sha(additional)
    if (out/"targeted_receipt.json").exists(): inputs[(out/"targeted_receipt.json").relative_to(repo).as_posix()]=sha(out/"targeted_receipt.json")
    images={}; registry={}; outputs={}
    def add_layer(name: str,features: list[dict],frame: str,system: str,image: Image.Image | None = None,native: dict | None = None) -> None:
        path=out/"layers"/(name+".geojson")
        write(path,G.collection(features,system,source_frame_id=frame,world_registration="TABLE_COORDINATE_ONLY_UNITS_AND_CRS_UNKNOWN" if system == "LOCAL_TABLE_COORDINATE_UNITS_UNKNOWN" else "UNREGISTERED_SOURCE_LAYER"))
        item={"name":name,"file":path.relative_to(repo).as_posix(),"sha256":sha(path),"feature_count":len(features),
              "source_frame_id":frame,"coordinate_reference":system,"coordinate_units":"m" if "LOCAL_ENGINEERING" in system else ("px" if "PIXEL" in system else "UNKNOWN" if "UNKNOWN" in system else "pt")}
        if image is not None:
            op=out/"overlays"/(name+".png"); overlay(image,features,op,native); item["overlay"]=op.relative_to(repo).as_posix(); item["overlay_sha256"]=sha(op)
        layers.append(item)
        outputs[item["file"]]=item["sha256"]
        if item.get("overlay"): outputs[item["overlay"]]=item["overlay_sha256"]

    for rec in selected:
        key=rec["key"]; image_path=local_file(repo,rec["source_image"])
        job=base_jobs.get(key)
        if job and job.get("original_format") in ("png","jpeg","jpg","gif"):
            image_path=repo/"work/figure_readings_2026-09-29"/job["original_file"]
        native=None
        if rec.get("native_object"):
            native_path=local_file(repo,rec["native_object"]); inputs[rec["native_object"]]=sha(native_path)
            native=json.loads(native_path.read_text(encoding="utf-8"))
        if key == "F023_007":
            emf_path=repo/"work/figure_readings_2026-09-29/images/F023_007_image8.emf"
            emf=G.inspect_emf(emf_path.read_bytes()); bitmap=emf["bitmaps"][0]
            inputs[emf_path.relative_to(repo).as_posix()]=sha(emf_path)
            image_path=out/"original_objects/F023_007_original_DIB.png"; image_path.parent.mkdir(parents=True,exist_ok=True)
            original=Image.open(io.BytesIO(bitmap["bitmap_bmp"])).convert("RGB"); original.save(image_path)
            raw=out/"original_objects/F023_007_original_DIB.bmp"; raw.write_bytes(bitmap["bitmap_bmp"])
            payload={k:v for k,v in emf.items() if k != "bitmaps"}
            payload["bitmap"]={k:v for k,v in bitmap.items() if k != "bitmap_bmp"}
            payload["bitmap_dimensions"]=[original.width,original.height]; payload["source_sha256"]=sha(emf_path)
            payload["geology_depth_axis"]="UNKNOWN_COLUMN_NOT_TO_SCALE_AND_BREAKS_VISIBLE"
            write(out/"original_objects/F023_007_EMF_RECORDS.json",payload)
            reviews.append({"key":key,"source_id":rec["source_id"],"full_image":image_path.relative_to(repo).as_posix(),
                            "exact_crop":image_path.relative_to(repo).as_posix(),"crop_coordinates":[0,0,original.width,original.height],
                            "question":"Confirm stratigraphic boundaries, labels and scale breaks on original 385x729 bitmap; EMF has no vector paths",
                            "reason":"Raster-only EMF; upsampling does not add source information","current_variants":["source pixel column only"],
                            "uncertainty":"absolute depth geometry UNKNOWN","downstream_impact":"geology/WorldSpec","status":"ASTRA_REVIEW_REQUIRED"})
        inputs[image_path.relative_to(repo).as_posix()]=sha(image_path)
        image=Image.open(image_path).convert("RGB"); images[key]=image; registry[key]=rec
        page_number=int(rec.get("page_id","p0").split("p")[-1]) if rec.get("page_id","").split("p")[-1].isdigit() else None
        source_role=("MODEL_INS_OUTPUT_NOT_FIELD_OBSERVATION" if rec["source_id"] == "VKM-SRC-197" and page_number in (13,14)
                     else "PUBLISHED_MODEL_DIAGNOSTIC_NOT_ENGINEERING_GCP" if rec["source_id"] == "VKM-SRC-197" and page_number in (9,11)
                     else "SOURCE_DOCUMENT_GRAPHIC_NOT_ACCEPTED_EVIDENCE")
        base={"key":key,"source_id":rec["source_id"],"source_sha256":rec.get("source_sha256","UNKNOWN"),"source_role":source_role,
              "image_sha256":sha(image_path),"image_file":image_path.relative_to(repo).as_posix(),"locator":rec.get("locator",{}),
              "page":rec.get("page_id",rec.get("locator",{}).get("page_id","UNKNOWN")),
              "figure":rec.get("figure_number","UNKNOWN"),"source_frame_id":key,
              "mine_attribution":rec.get("mine_attribution","UNREVIEWED"),"scope":rec.get("scope","UNKNOWN"),
              "valid_from":"UNKNOWN","valid_to":"UNKNOWN","depth":"UNKNOWN","coordinate_reference":"SOURCE_PIXELS_Y_DOWN",
              "attributes":{},"uncertainty":{"field_metre_error":"UNKNOWN","registration_error":"UNREGISTERED"}}
        figures.append({"key":key,"source_id":rec["source_id"],"image":base["image_file"],"image_sha256":base["image_sha256"],
                        "dimensions":[image.width,image.height],"source_sha256":base["source_sha256"],"frame":key,
                        "coordinate_reference":"SOURCE_PIXELS_Y_DOWN","represented_date":"UNKNOWN","depth":"UNKNOWN","source_role":source_role})
        figure_count=0
        if native is not None:
            native_base={**base,"coordinate_reference":"PDF_UNROTATED_PT","native_file":rec["native_object"]}
            paths,issues=G.native_paths(native,native_base)
            for issue in issues: reviews.append({**issue,"key":key,"source_id":rec["source_id"],"full_image":base["image_file"],
                                                "exact_crop":base["image_file"],"crop_coordinates":[0,0,image.width,image.height],
                                                "question":issue["reason"],"uncertainty":"native clip/primitive","downstream_impact":"source geometry"})
            add_layer(key+"_native",paths,key+"_PDF_UNROTATED_PT","PDF_UNROTATED_PT",image,native)
            figure_count+=len(paths)
        rgb=np.asarray(image)
        region_specs,palette=regions_for(source_regions,key,image.width,image.height,base["image_sha256"])
        for part,roi,mode in region_specs:
            pbase={**base,"part":part,"crop_coordinates":roi,"crop_to_source":[1,0,0,1,0,0],
                   "method_choices":{"roi":"VISUALLY_SELECTED_SOURCE_REGION","mask":mode,"no_morphological_gap_closure":True}}
            mask=G.raster_mask(rgb,mode,roi)
            regions=G.enclosed_regions(mask,pbase,minimum_area=40)
            segments=G.line_segments(mask,pbase,minimum_length=max(20,min(image.size)//30))
            # For very large native/raster sources, preserve line segments and enclosed cells as separate candidates.
            add_layer(key+"_"+part+"_linework",segments,key,"SOURCE_PIXELS_Y_DOWN",image)
            add_layer(key+"_"+part+"_regions",regions,key,"SOURCE_PIXELS_Y_DOWN",image)
            figure_count+=len(segments)+len(regions)
        if palette is not None:
            ps=palette
            for colour_index,(x,y) in enumerate(ps["points"],1):
                colour=np.median(rgb[y-2:y+3,x-2:x+3].reshape(-1,3),axis=0).round().astype(int).tolist()
                mask=G.raster_mask(rgb,"legend_colour",ps["roi"],colour=colour,lab_tolerance=18)
                feats=G.contours(mask,{**base,"part":ps["part"],"legend_colour_rgb":colour,"legend_swatch_source_px":[x,y],
                                      "literal_class":"UNKNOWN_PENDING_LITERAL_REVIEW","lab_tolerance":18},
                                 minimum_area=30,semantic_class=f"LEGEND_SWATCH_{colour_index}_UNREVIEWED")
                add_layer(key+f"_legend{colour_index}",feats,key,"SOURCE_PIXELS_Y_DOWN",image)
                figure_count+=len(feats)
        reviews.append({"key":key,"source_id":rec["source_id"],"page":base["page"],"figure":base["figure"],
                        "full_image":base["image_file"],"exact_crop":base["image_file"],"crop_coordinates":[0,0,image.width,image.height],
                        "question":"Verify topology, mine attribution, scale and label→object correspondence before assigning source candidates to zones/panels/boreholes",
                        "current_variants":["unregistered source linework","unregistered enclosed regions"],"uncertainty":"semantics/local registration UNKNOWN",
                        "downstream_impact":"B/C consolidation; all source geometry preserved","status":"ASTRA_REVIEW_REQUIRED"})
        stats.append({"key":key,"source_id":rec["source_id"],"features":figure_count,"native":native is not None,"dimensions":[image.width,image.height]})
        print(key,figure_count,"source candidates",flush=True)
    points,records,gold_issues=gold_attributes(repo,base_jobs); reviews+=gold_issues
    add_layer("control_points",points,"SKRU1_TABLE_COORDINATE_UNITS_UNVERIFIED","LOCAL_TABLE_COORDINATE_UNITS_UNKNOWN")
    write(out/"zone_attributes_gold.json",{"records":records,"rule":"panel+zone+seam identities remain distinct; all fields retain independent provenance"})
    matches=[]
    for pair in source_regions["correspondences"]:
        source_key,target_key=pair["source"],pair["target"]
        if source_key not in images or target_key not in images: continue
        source_roi=validate_rectangle(pair["source_roi"],*images[source_key].size)
        target_roi=validate_rectangle(pair["target_roi"],*images[target_key].size)
        match=image_correspondence(images[source_key],images[target_key],source_roi,target_roi)
        match.update({"source":source_key,"target":target_key,"source_image_sha256":sha(local_file(repo,registry[source_key]["original_object"])),
                      "target_image_sha256":sha(local_file(repo,registry[target_key]["original_object"]))})
        matches.append(match)
        # Concrete exact crops for every proposed control, to support later strong-vision acceptance.
        crops=[]
        for index,c in enumerate(match.get("controls",[])):
            item={"index":index,"verification":"AUTO_EXTRACTED_UNREVIEWED"}
            for side,key in (("source",source_key),("target",target_key)):
                x,y=c[side]; im=images[key]; box=[max(0,int(x)-30),max(0,int(y)-30),min(im.width,int(x)+31),min(im.height,int(y)+31)]
                crop=out/"review_crops"/(f"gcp_{source_key}_{target_key}_{index:03d}_{side}.png"); crop.parent.mkdir(parents=True,exist_ok=True); im.crop(tuple(box)).save(crop)
                item[side]={"key":key,"crop":crop.relative_to(repo).as_posix(),"coordinates":box,"sha256":sha(crop)}
            crops.append(item)
        match["review_crops"]=crops
        for side,key in (("source",source_key),("target",target_key)):
            controls=[G.feature({"type":"Point","coordinates":c[side]}, {"stable_id":f"ORB_{i:03d}_{side}","key":key}) for i,c in enumerate(match.get("controls",[]))]
            path=out/"overlays"/f"registration_controls_{source_key}_{target_key}_{side}.png"; overlay(images[key],controls,path)
            match[f"{side}_controls_overlay"]={"file":path.relative_to(repo).as_posix(),"sha256":sha(path)}
        reviews.append({"key":f"{source_key}_to_{target_key}","source_id":registry[source_key]["source_id"],"full_image":registry[source_key]['source_image'],
                        "exact_crops":crops,"question":"Are the ORB proposals the same persistent graphic objects? Approve or reject each before using registration",
                        "current_variants":["similarity from automatically proposed image matches"],"uncertainty":"unverified semantic identity; target coordinate unit pixels",
                        "downstream_impact":"source-layer alignment only, never mine-grid registration","status":"ASTRA_REVIEW_REQUIRED"})
    write(out/"source_image_registration.json",matches)
    write(out/"temporal_qa.json",{"findings":G.temporal_qa(records),"comparison_status":"BLOCKED_UNREVIEWED_ZONE_IDENTITY_AND_EVENT_DATES",
                                  "unknown_dates":len(records),"plan_252":"PLANNED_NOT_EXECUTED"})
    write(out/"ASTRA_REVIEW_REQUIRED.json",{"cases":reviews,"status":"ASTRA_REVIEW_REQUIRED","all_extraction":"AUTO_EXTRACTED_UNREVIEWED"})
    write(out/"source_figures.json",figures)
    required={name:{"status":"POPULATED" if name == "control_points" else "BLOCKED_OR_METADATA_ONLY",
                    "reason":"semantic identity or metric/section transform not yet reviewed; no invented feature"} for name in REQUIRED_LAYERS}
    required["source_figures"]={"status":"POPULATED_METADATA","file":"work/geometry_2026-09-29/source_figures.json"}
    required["control_points"]["file"]="work/geometry_2026-09-29/layers/control_points.geojson"
    write(out/"layers_manifest.json",{"schema":"vkm.source_geometry_layers/1","layers":layers,"required_layers":required,
          "import_rule":"One source frame per layer. Pixel/page/engineering coordinates must never share a metric CRS. GeoJSON source coords are not WGS84.",
          "world_status":"UNREGISTERED_SOURCE_GEOMETRY_WITH_UNKNOWN_DEPTH_AND_TIME","source_figures":len(figures),"stats":stats})
    dxf_status="NOT_AVAILABLE_IN_CURRENT_PYTHON"
    if importlib.util.find_spec("ezdxf"):
        import ezdxf
        for item in layers:
            data=json.loads(local_file(repo,item["file"]).read_text(encoding="utf-8")); doc=ezdxf.new(); msp=doc.modelspace()
            for ft in data["features"]:
                geom=ft["geometry"]
                if not geom: continue
                if geom["type"] == "Point": msp.add_point(geom["coordinates"])
                else:
                    lines=geom["coordinates"] if geom["type"] in ("Polygon","MultiLineString") else [geom["coordinates"]]
                    for line in lines: msp.add_lwpolyline(line)
            path=out/"dxf"/(item["name"]+".dxf"); path.parent.mkdir(parents=True,exist_ok=True); doc.saveas(path)
        dxf_status="EXPORTED_SOURCE_COORDINATES_PER_LAYER"
    accuracy=["# Точность и ограничения geometry v1", "",
       "Извлечены исходные графические объекты и контуры. Физическая геопривязка к местной сетке не принята: отсутствуют проверенные соответствия label → object. Значения 20–30 м, ±10 м и ±1 год не являются доказанными ошибками.", "",
       "Растр: координаты в пикселях исходного объекта, направление Y вниз. LAB threshold 18 и contour simplification 0.75 px — MODEL_CHOICE; линии Hough имеют max gap 2 px. Эффект порога и утраты мелких областей требует vision QA. Чёрная линия не автоматически граница пласта/зоны.", "",
       "Native PDF: unrotated page points; сохраняются paint order, page rotation, clip/group provenance. Прямоугольные clip/crop применены; сложные clips явно UNAPPLIED_COMPLEX_CLIP и в очереди ревью. Cubic Bezier chord tolerance 0.1 pt; перевод в метры UNKNOWN.", "",
       "Рис. 7: оригинальный EMF содержит только HEADER + STRETCHDIBITS + EOF. Извлечён исходный DIB 385×729; прежний PNG 1647×3199 — увеличение, не новые данные. Колонка с разрывами и непропорциональными высотами не преобразуется в глубины по пикселям.", "",
       "Проверенные табличные x/y импортированы как target points в исходных числовых координатах: UNIT_UNKNOWN / CRS_AUTHORITY_UNKNOWN. В headers xcoord/ycoord и exel_X/Y_coord единица не напечатана; диапазон чисел не доказывает метры. Повторы zone IDs в разных panel/seam не сливаются. Ненадёжная обрезанная строка исключена. Это проверка чтения вторичного источника, не полевое измерение.", "",
       "## Регистрация изображений", ""]
    for m in matches:
        qa=m.get("diagnostic_transform",{})
        accuracy.append(f"{m['source']} → {m['target']}: {m['status']}; controls={len(m.get('controls',[]))}; RMS={qa.get('rms','UNKNOWN')} target px; max={qa.get('max_residual','UNKNOWN')}; LOO RMS={qa.get('leave_one_out',{}).get('rms','UNKNOWN')}. Метрическая точность UNKNOWN. RANSAC-selected controls делают LOO условной диагностикой; independent feature identity не доказана.")
    accuracy+= ["", "Бюджет ошибки: source resolution px/pt известна; extraction threshold и flatten tolerance записаны; semantic correspondence, physical registration, digitization accuracy in metres и dates UNKNOWN. Каждый source layer имеет overlay; его наличие не означает приёмку.", ""]
    (out/"ACCURACY.md").write_text("\n".join(accuracy),encoding="utf-8",newline="\n")
    report=["# Геометрия и геология: фактический результат", "",
            f"Обработано {len(figures)} объектов из заданных seeds; извлечено {sum(s['features'] for s in stats)} source geometry candidates. GeoJSON и наложения лежат в layers/ и overlays/. Видеокарта, CAD и solver не запускались.", "",
            f"Рис. 13, 23, 24, 25: извлечены linework, enclosed regions и классы цветов, выбранные по swatch легенды. Числа zone/panel и semantic colours не угаданы. Из gold tables импортировано {len(points)} target-coordinate rows; mine-grid registration блокирована до проверки identity.", "",
            "Планы 014/064 и рисунки 197 сохранены как отдельные source layers. 197 хранит рисунки полосами raster XObjects; полосы собраны по исходным bbox/placements, все embedded fragments сохранены. Согласованная область и временная история ещё требуют проверки.", "",
            "Геология 011/012 сохраняется в исходных pixel/page координатах без недоказанных surfaces. Рис.7 — raster-only EMF; оригинальная колонка и record inventory сохранены. Native DOCX tables 1.1/1.2 источника252 извлечены с gridSpan/vMerge, XML hashes и локаторами ячеек; данные PLANNED, не EXECUTED, не evidence до ревью.", "",
            f"Очередь сильного vision-ревью: {len(reviews)} cases в ASTRA_REVIEW_REQUIRED.json. GCP proposals снабжены exact crops и пиксельными рамками. DXF: {dxf_status}. GeoPackage интегрирует QGIS worker; импортировать source frames отдельно и не назначать EPSG/метры пикселям.", "",
            "Это исполненный source extraction, но не полная приёмка B/C: неизвестные идентичности объектов, координатные системы, временные даты и depth calibration не заполнены правдоподобными значениями. Остальные объекты inventory не объявлены просмотренными или geometry-complete.", ""]
    (out/"REVIEW_GEO_RU.md").write_text("\n".join(report),encoding="utf-8",newline="\n")
    accepted={p for folder in ("layers","overlays","review_crops","original_objects") for p in (out/folder).glob("*") if p.is_file()}
    accepted.update(out/name for name in ("zone_attributes_gold.json","source_image_registration.json","temporal_qa.json","ASTRA_REVIEW_REQUIRED.json","source_figures.json","layers_manifest.json","ACCURACY.md","REVIEW_GEO_RU.md") if (out/name).exists())
    if (out/"targeted_receipt.json").exists():
        target_receipt=json.loads((out/"targeted_receipt.json").read_text(encoding="utf-8"))
        accepted.update(repo/p for p in target_receipt["outputs"])
        accepted.add(out/"targeted_receipt.json")
    for path in sorted(accepted): outputs[path.relative_to(repo).as_posix()]=sha(path)
    receipt={"schema":"vkm.geometry_extraction/1","tool":G.VERSION,"command":"python -B benchmarks/geometry_skru1_v1/run.py --targeted" if args.targeted else "python -B benchmarks/geometry_skru1_v1/run.py",
             "inputs":inputs,"outputs":outputs,"statistics":{"figures":len(figures),"features":sum(s['features'] for s in stats),"gold_coordinates":len(points),"review_cases":len(reviews)},
             "verification":"AUTO_EXTRACTED_UNREVIEWED_WITH_VERIFIED_TABLE_TARGET_POINTS","registration_to_mine_grid":"NOT_ACCEPTED",
             "models_executed":0,"solver_executed":0,"gpu_jobs":0,"dxf":dxf_status,
             "numpy_version":np.__version__,"python_version":sys.version.split()[0]}
    write(out/"receipt.json",receipt)
    print(json.dumps(receipt["statistics"],ensure_ascii=False),flush=True)


if __name__ == "__main__": main()
