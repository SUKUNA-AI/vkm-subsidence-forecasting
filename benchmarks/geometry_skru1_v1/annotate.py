"""Materialize explicitly reviewed PRIVATE source-image annotations.

Coordinates and literal readings are supplied in a separate ignored JSON file;
this public module contains no corpus readings or mine coordinates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image
import geometry as G
from run import overlay,write,sha,local_file


def materialize(repo: Path,annotations: dict,out: Path):
    grouped={}; reviews=[]; inputs={}
    for obj in annotations["objects"]:
        image=local_file(repo,obj["source_image"]); inputs[obj["source_image"]]=sha(image)
        geom=obj["geometry"]
        if geom["type"] == "Polygon":
            errors=G.validate_polygon(geom["coordinates"])
            if errors: raise ValueError(f"annotation {obj['id']}: invalid topology {errors}")
        props={"stable_id":obj["id"],"key":obj["figure_key"],"source_id":obj["source_id"],
               "source_sha256":obj.get("source_sha256","UNKNOWN"),"image_file":obj["source_image"],
               "image_sha256":sha(image),"source_object_id":obj["id"],"source_frame_id":obj["figure_key"],
               "coordinate_reference":"SOURCE_PIXELS_Y_DOWN","coordinate_units":"px",
               "extraction_method":"EXPLICIT_SOURCE_IMAGE_DIGITIZATION","epistemic_status":"DERIVATION",
               "verification":"ASTRA_REVIEW_REQUIRED","review_stage":"CLOUD_VISION_REVIEWED_SOURCE_GRAPHIC_PENDING_INDEPENDENT_ACCEPTANCE",
               "semantic_class":obj["semantic_class"],"label":obj.get("label","UNKNOWN"),
               "label_object_match":obj.get("label_object_match","UNKNOWN"),
               "valid_from":"UNKNOWN","valid_to":"UNKNOWN","depth":"UNKNOWN",
               "registration_to_mine_grid":"NOT_ACCEPTED","scope":obj.get("scope","UNKNOWN"),
               "source_role":obj.get("source_role","PUBLISHED_SOURCE_GRAPHIC_NOT_FIELD_OBSERVATION"),
               "attributes":obj.get("attributes",{}),"provenance":obj["provenance"],
               "uncertainty":{"geometry_in_metres":"UNKNOWN","digitization_error_px":"UNKNOWN",
                              "manual_vertex_selection":"MODEL_CHOICE; review source overlay"}}
        group=(obj["figure_key"],obj["semantic_class"],obj["source_image"])
        grouped.setdefault(group,[]).append(G.feature(geom,props))
        crop_file=out/"review_crops"/(obj["id"]+".png");crop_file.parent.mkdir(parents=True,exist_ok=True)
        Image.open(image).crop(tuple(obj["provenance"]["crop_coordinates"])).save(crop_file)
        reviews.append({"key":obj["figure_key"],"source_id":obj["source_id"],"object_id":obj["id"],
                        "full_image":obj["source_image"],"crop_coordinates":obj["provenance"]["crop_coordinates"],
                        "exact_crop":crop_file.relative_to(repo).as_posix(),"exact_crop_sha256":sha(crop_file),
                        "question":obj["review_question"],"semantic_class":obj["semantic_class"],
                        "label":obj.get("label","UNKNOWN"),"status":"ASTRA_REVIEW_REQUIRED",
                        "reason":"Explicit source-image reading requires independent acceptance; source-only geometry, not metric evidence"})
    layers=[]; outputs={}
    for (key,kind,image_file),features in grouped.items():
        name=key+"_reviewed_"+kind
        file=out/"layers"/(name+".geojson"); write(file,G.collection(features,"SOURCE_PIXELS_Y_DOWN",source_frame_id=key))
        graphic=out/"overlays"/(name+".png"); overlay(Image.open(repo/image_file),features,graphic)
        layers.append({"name":name,"file":file.relative_to(repo).as_posix(),"sha256":sha(file),"feature_count":len(features),
                       "source_frame_id":key,"coordinate_reference":"SOURCE_PIXELS_Y_DOWN","coordinate_units":"px",
                       "geometry_type":features[0]["geometry"]["type"],"semantic_class":kind,
                       "overlay":graphic.relative_to(repo).as_posix(),"overlay_sha256":sha(graphic)})
        outputs[file.relative_to(repo).as_posix()]=sha(file);outputs[graphic.relative_to(repo).as_posix()]=sha(graphic)
    return {"layers":layers,"reviews":reviews,"inputs":inputs,"outputs":outputs}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--annotations",default="work/geometry_2026-09-29/manual_source_annotations.json")
    parser.add_argument("--out",default="work/geometry_2026-09-29")
    args=parser.parse_args();repo=Path.cwd().resolve();out=local_file(repo,args.out)
    if not out.is_relative_to(repo/"work"):raise ValueError("annotations/outputs must stay PRIVATE in work")
    source=local_file(repo,args.annotations)
    if not source.is_relative_to(repo/"work"):raise ValueError("literal annotation inputs must stay PRIVATE")
    annotations=json.loads(source.read_text(encoding="utf-8"));result=materialize(repo,annotations,out)
    result.update({"schema":"vkm.explicit_source_annotations/1","annotation_file":args.annotations,"annotation_sha256":sha(source),
                   "command":"python -B benchmarks/geometry_skru1_v1/annotate.py","source_geometry_only":True,
                   "world_registration":"NOT_ACCEPTED","models_executed":0,"solver_executed":0})
    write(out/"reviewed_annotations_receipt.json",result)
    write(out/"reviewed_layers_manifest.json",{"layers":result["layers"],"required_independent_review":result["reviews"]})
    print(json.dumps({"layers":len(result["layers"]),"features":sum(x['feature_count'] for x in result['layers'])}))


if __name__ == "__main__":main()
