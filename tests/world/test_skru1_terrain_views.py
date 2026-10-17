import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
import pytest

path=Path(__file__).resolve().parents[2]/"scripts/build_skru1_terrain_views.py"
spec=importlib.util.spec_from_file_location("skru1_terrain_views",path)
views=importlib.util.module_from_spec(spec);spec.loader.exec_module(views)


def test_dsm_centres_heights_and_upward_faces_are_preserved():
    z=np.array([[100.,110.,120.],[130.,140.,150.]])
    xyz,triangles,rows,cols=views.raster_mesh(z,(1000.,30.,0.,3000.,0.,-30.))
    assert xyz[0].tolist()==[1015.,2985.,100.]
    assert np.array_equal(xyz[:,2],z.ravel())
    assert len(triangles)==4 and len(xyz)==6
    normals=np.cross(xyz[triangles[:,1]]-xyz[triangles[:,0]],xyz[triangles[:,2]]-xyz[triangles[:,0]])
    assert (normals[:,2]>0).all()


def test_missing_dsm_cells_are_not_zero_filled_or_bridged():
    z=np.ones((3,3))*100;z[1,1]=-32767
    xyz,triangles,_,_=views.raster_mesh(z,(0.,30.,0.,90.,0.,-30.),-32767)
    assert len(xyz)==8 and len(triangles)==0
    assert (xyz[:,2]==100).all()
    with pytest.raises(ValueError):
        views.raster_mesh(z,(0.,30.,1.,90.,0.,-30.),-32767)


def test_landxml_uses_north_east_height_without_axis_swap_or_height_shift(tmp_path):
    z=np.array([[100.,110.],[120.,130.]])
    xyz,triangles,_,_=views.raster_mesh(z,(1000.,30.,0.,3000.,0.,-30.))
    path=tmp_path/"surface.xml"
    report=views.write_landxml(path,xyz,triangles)
    ns="{http://www.landxml.org/schema/LandXML-1.2}"
    first=ET.parse(path).find(".//"+ns+"P")
    assert list(map(float,first.text.split()))==[2985.,1015.,100.]
    assert report=={"xml_read_back":"PASS","points":4,"faces":2,"native_import":"NOT_RUN"}
