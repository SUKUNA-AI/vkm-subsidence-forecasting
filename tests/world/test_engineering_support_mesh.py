"""Inserting a borehole on a shared edge must refine both incident cells."""
import importlib.util
from pathlib import Path
import sys

import numpy as np
from shapely.geometry import Polygon

from vkm_world.geometry_build.mesh_audit import audit_plan_triangles


def test_support_insertion_keeps_internal_edge_conforming():
    scripts=Path(__file__).resolve().parents[2]/"scripts"
    sys.path.insert(0,str(scripts))
    try:
        spec=importlib.util.spec_from_file_location("engineering_builder",scripts/"build_engineering_geology.py")
        builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    finally:
        sys.path.remove(str(scripts))
    original=np.array([[[0,0],[2,0],[2,2]],[[0,0],[2,2],[0,2]]],float)
    supports=np.array([[1,1],[.2,1],[2,1]])
    result=builder.insert_support_vertices(original,supports)
    audit=audit_plan_triangles(result,Polygon([(0,0),(2,0),(2,2),(0,2)]))
    assert audit["conforming"]
    assert audit["summed_area"]==4
    for point in supports:
        assert np.any(np.all(result.reshape(-1,2)==point,axis=1))
