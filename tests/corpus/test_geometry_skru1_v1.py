"""Synthetic source-geometry/registration checks; no corpus values or coordinates."""
from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location("source_geometry", Path(__file__).resolve().parents[2] / "benchmarks/geometry_skru1_v1/geometry.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)


def test_similarity_controls_predict_with_loo_and_preserve_rotation_scale():
    points=np.array([[0,0],[10,0],[0,10],[10,10],[4,3]],dtype=float)
    angle=.3; matrix=np.array([[2*np.cos(angle),-2*np.sin(angle),31],[2*np.sin(angle),2*np.cos(angle),-17],[0,0,1]])
    target=G.apply_transform(points.tolist(),matrix.tolist())
    result=G.registration_qa(points.tolist(),target.tolist())
    assert np.allclose(result["matrix"],matrix,atol=1e-10)
    assert result["rms"] < 1e-10
    assert result["leave_one_out"]["rms"] < 1e-10
    assert result["acceptance"] == "DIAGNOSTIC_ONLY_UNTIL_CONTROL_IDENTITIES_VERIFIED"


def test_affine_requires_reason_and_rejects_collinear_points():
    with pytest.raises(ValueError,match="justification"):
        G.fit_transform([[0,0],[1,0],[0,1]],[[0,0],[2,0],[0,1]],"affine")
    with pytest.raises(ValueError,match="collinear"):
        G.fit_transform([[0,0],[1,0],[2,0]],[[0,0],[2,0],[4,0]],"affine","source anisotropy")


def test_minimum_control_count_does_not_invent_loo_evidence():
    result=G.registration_qa([[0,0],[1,0]],[[4,7],[6,7]])
    assert result["leave_one_out"]["computed_count"] == 0
    assert result["leave_one_out"]["rms"] is None


def test_projective_normalization_at_large_coordinate_magnitudes():
    source=np.array([[1e5,2e5],[1e5+1000,2e5],[1e5,2e5+1000],[1e5+1000,2e5+1000],[1e5+500,2e5+600]])
    m=[[1.2,.1,100],[-.05,.9,-200],[2e-7,-1e-7,1]]
    target=G.apply_transform(source.tolist(),m)
    fit=G.registration_qa(source.tolist(),target.tolist(),"projective","document photographed with perspective")
    assert fit["max_residual"] < 1e-6
    assert fit["leave_one_out"]["max"] < 1e-6


def test_native_clip_rotation_paint_and_tolerance_are_preserved():
    payload={"rotation":90,"coordinate_system":"PDF_UNROTATED_PT","bbox_page_pt":[0,0,10,10],"drawings":[
        {"type":"clip","level":0,"scissor":[1,1,9,9],"items":[["re",[1,1,9,9],1]]},
        {"type":"s","level":1,"seqno":7,"items":[["l",[-1,5],[12,5]]]}]}
    features,issues=G.native_paths(payload,{"key":"synthetic"})
    assert not issues
    assert features[0]["geometry"]["coordinates"] == [[[1.,5.],[9.,5.]]]
    assert features[0]["properties"]["rotation_preserved"] == 90
    assert features[0]["properties"]["paint_order"] == 7


def test_native_complex_clip_never_silently_claims_applied():
    payload={"drawings":[{"type":"clip","level":0,"scissor":[0,0,8,8],"items":[["l",[0,0],[8,8]]]},
                          {"type":"s","level":1,"items":[["l",[0,4],[10,4]]]}]}
    features,issues=G.native_paths(payload,{"key":"synthetic"})
    assert features[0]["properties"]["clip_status"] == "UNAPPLIED_COMPLEX_CLIP"
    assert issues[0]["status"] == "ASTRA_REVIEW_REQUIRED"


def test_cubic_flatten_reaches_tolerance_and_keeps_endpoints():
    control=[[0,0],[0,10],[10,10],[10,0]]
    line=G.flatten_cubic(control,tolerance=.01)
    assert line[0] == [0,0] and line[-1] == [10,0]
    assert len(line)>20
    with pytest.raises(ValueError): G.flatten_cubic(control,tolerance=0)


def test_raster_roi_no_gui_leak_and_holes_preserved():
    cv2=pytest.importorskip("cv2")
    image=np.full((120,120,3),255,dtype=np.uint8)
    cv2.rectangle(image,(10,10),(90,90),(255,0,0),2)
    cv2.rectangle(image,(20,100),(90,115),(255,0,0),2)
    mask=G.raster_mask(image,"red_line",[0,0,120,95])
    assert not mask[100:].any()
    regions=G.enclosed_regions(mask,{"key":"synthetic"})
    assert len(regions) == 1
    assert regions[0]["properties"]["semantic_class"] == "UNKNOWN_ENCLOSED_REGION"
    assert regions[0]["properties"]["label_object_match"] == "UNKNOWN"


def test_self_crossing_topology_and_plan_execution_detected():
    assert "SELF_INTERSECTION" in G.validate_polygon([[[0,0],[2,2],[0,2],[2,0],[0,0]]])
    findings=G.temporal_qa([{"id":"toy","mining_start":2000,"mining_end":1999,"backfill_year":1998,
                            "source_role":"PLANNED","execution_status":"EXECUTED"}])
    assert {f["kind"] for f in findings} == {"MINING_END_BEFORE_START","BACKFILL_BEFORE_MINING","PLAN_PROMOTED_TO_EXECUTION"}


def test_unreadable_attribute_is_unknown_and_id_is_deterministic():
    attr=G.attribute("UNREADABLE","SYNTHETIC",{"row":1},"TRUNCATED")
    assert attr["epistemic_status"] == "UNKNOWN"
    assert G.stable_id("toy",attr) == G.stable_id("toy",dict(reversed(list(attr.items()))))


@pytest.mark.parametrize("shape",[(2,1,4),(2,4)])
def test_hough_wheel_shapes_keep_four_endpoints(monkeypatch,shape):
    cv2=pytest.importorskip("cv2")
    result=np.array([[1,2,30,40],[50,60,70,80]]).reshape(shape)
    monkeypatch.setattr(cv2,"HoughLinesP",lambda *args,**kwargs:result)
    features=G.line_segments(np.zeros((100,100),dtype=np.uint8),{"key":"synthetic"})
    assert [f["geometry"]["coordinates"] for f in features] == [[[1,2],[30,40]],[[50,60],[70,80]]]


def test_rotated_native_crop_uses_unrotated_bbox():
    payload={"rotation":90,"bbox_page_pt":[100,0,110,10],"bbox_native_pt":[0,0,10,10],
             "drawings":[{"type":"s","level":0,"seqno":2,"items":[["l",[-2,3],[12,3]]]}]}
    features,issues=G.native_paths(payload,{"key":"synthetic"})
    assert features[0]["geometry"]["coordinates"] == [[[0.,3.],[10.,3.]]]


def test_printed_coordinates_with_unknown_units_never_become_metres():
    result=G.collection([],"LOCAL_TABLE_COORDINATE_UNITS_UNKNOWN")
    assert result["coordinate_reference"]["coordinate_units"] == "UNKNOWN"
    assert result["coordinate_reference"]["authority"] == "UNKNOWN"


def test_emf_unknown_render_record_never_promoted_to_raster_only():
    bitmap=bytearray(124)
    struct.pack_into('<II',bitmap,0,81,len(bitmap))
    struct.pack_into('<4I',bitmap,48,80,40,120,4)
    struct.pack_into('<2i',bitmap,72,1,1)
    struct.pack_into('<IiiHHII',bitmap,80,40,1,1,1,32,0,4)
    raw=struct.pack('<II',1,8)+bytes(bitmap)+struct.pack('<II',14,8)
    result=G.inspect_emf(raw)
    assert result['content_class']=='RASTER_ONLY_IN_EMF_CONTAINER'
    assert result['bitmaps'][0]['bitmap_bmp'][:2]==b'BM'
    mixed=raw+struct.pack('<II',41,8)
    assert G.inspect_emf(mixed)['content_class']=='VECTOR_OR_MIXED_REQUIRES_NATIVE_RENDERER'
    with pytest.raises(ValueError,match='trailing'):
        G.inspect_emf(raw+b'\x00')


def test_private_region_hash_and_bounds_protect_source_frame(tmp_path):
    import json
    loader_spec=importlib.util.spec_from_file_location("private_source_regions",Path(__file__).resolve().parents[2]/"benchmarks/geometry_skru1_v1/source_regions.py")
    regions=importlib.util.module_from_spec(loader_spec); loader_spec.loader.exec_module(regions)
    path=tmp_path/"regions.json"
    payload={"schema":"vkm.private_source_regions/1","images":{"toy":{"image_sha256":"synthetic-hash","rois":[["map",[1,2,9,8],"dark_line"]]}},"correspondences":[]}
    path.write_text(json.dumps(payload),encoding="utf-8")
    config=regions.load_regions(path)
    with pytest.raises(ValueError,match="hash mismatch"):
        regions.regions_for(config,"toy",10,10,"changed-image")
    with pytest.raises(ValueError,match="outside"):
        regions.regions_for(config,"toy",5,5,"synthetic-hash")
    assert regions.regions_for(config,"toy",10,10,"synthetic-hash")[0] == payload["images"]["toy"]["rois"]
    assert regions.regions_for(config,"other",10,10,"another-hash") == ([("source",[0,0,10,10],"dark_line")],None)


def test_private_region_legend_and_correspondence_validation(tmp_path):
    import json
    loader_spec=importlib.util.spec_from_file_location("private_source_regions",Path(__file__).resolve().parents[2]/"benchmarks/geometry_skru1_v1/source_regions.py")
    regions=importlib.util.module_from_spec(loader_spec); loader_spec.loader.exec_module(regions)
    config={"schema":"vkm.private_source_regions/1","images":{"toy":{"image_sha256":"synthetic-hash","rois":[["map",[0,0,10,10],"red_line"]],"palette":{"roi":[0,0,10,10],"points":[[0,0]],"part":"toy"}}},"correspondences":[]}
    with pytest.raises(ValueError,match="legend"):
        regions.regions_for(config,"toy",10,10,"synthetic-hash")
    config["correspondences"]=[{"source":"toy","target":"missing"}]
    path=tmp_path/"regions.json"; path.write_text(json.dumps(config),encoding="utf-8")
    with pytest.raises(ValueError,match="unconfigured"):
        regions.load_regions(path)
