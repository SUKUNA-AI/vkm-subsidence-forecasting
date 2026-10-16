"""Topology, finite geometry and source constraints of interpreted layer meshes."""
import pytest
np=pytest.importorskip('numpy')
pytest.importorskip('shapely',minversion='2.1')
from shapely.geometry import Polygon, box

from vkm_world.geometry_build.linked_geology import folded_roof, idw_values, layer_mesh, room_footprints
from vkm_world.geometry_build.linked_geology import write_vtu


def test_layer_preserves_hole_volume_and_watertight_boundary():
    polygon=Polygon([(0,0),(12,0),(12,10),(0,10)],holes=[[(3,3),(3,7),(7,7),(7,3)]])
    mesh=layer_mesh(polygon,lambda xy:10+.1*xy[:,0],3,spacing=4)
    assert mesh['volume_m3']==pytest.approx(312)
    points=mesh['points']; faces=mesh['faces']
    edges={}
    for face in faces:
        for a,b in zip(face,np.roll(face,-1)):
            edge=tuple(sorted((int(a),int(b))));edges[edge]=edges.get(edge,0)+1
    assert set(edges.values())=={2}
    # Signed exterior volume independently checks winding and physical thickness.
    volume=sum(np.dot(points[a],np.cross(points[b],points[c]))/6 for a,b,c in faces)
    assert volume==pytest.approx(mesh['volume_m3'])


def test_fold_respects_source_range_for_all_explicit_phases():
    xy=np.array([(x,y) for x in np.linspace(0,100,71) for y in np.linspace(0,60,35)])
    for phase in (0,1,2,3):
        roof=folded_roof(xy,(0,0,100,60),-90,-75,9,3,phase,33)
        assert roof.min()>=-90-1e-12 and roof.max()<=-75+1e-12


def test_rooms_respect_footprint_and_are_disjoint():
    polygon=Polygon([(0,0),(50,0),(50,50),(0,50)],holes=[[(15,15),(15,30),(30,30),(30,15)]])
    rooms=room_footprints(polygon,5.5,8,azimuth_deg=37,phase_fraction=.3)
    assert rooms and all(polygon.buffer(1e-8).covers(room) for room in rooms)
    assert all(a.intersection(b).area<1e-8 for i,a in enumerate(rooms) for b in rooms[i+1:])
    assert sum(r.area for r in rooms)<polygon.area


def test_invalid_layers_are_not_repaired_or_given_zero_thickness():
    with pytest.raises(ValueError):layer_mesh(box(0,0,2,2),lambda xy:xy[:,0],0)
    with pytest.raises(ValueError):folded_roof(np.zeros((2,2)),(0,0,1,1),2,1,3,1)


def test_idw_preserves_observations_and_refuses_conflicting_collocation():
    supports=[[0,0,3],[1,0,5],[0,1,7]]
    assert idw_values([[0,0],[1,0],[0,1]],supports).tolist()==[3,5,7]
    assert 3<=idw_values([[.2,.3]],supports)[0]<=7
    with pytest.raises(ValueError,match='conflicting'):
        idw_values([[0,0]],[[0,0,3],[0,0,5]])


def test_volume_adapter_uses_ogs_prism_order_and_material_ids(tmp_path):
    import xml.etree.ElementTree as ET
    mesh=layer_mesh(box(0,0,2,2),lambda xy:np.full(len(xy),5.),2.,spacing=2.)
    path=tmp_path/'mesh.vtu';write_vtu(path,mesh,material_id=7)
    root=ET.parse(path).getroot()
    cells=np.fromstring(root.find('.//Cells/DataArray[@Name="connectivity"]').text,sep=' ',dtype=int).reshape(-1,6)
    p=mesh['points'][cells]
    first=p[:,1,:2]-p[:,0,:2];second=p[:,2,:2]-p[:,0,:2]
    assert np.all(first[:,0]*second[:,1]-first[:,1]*second[:,0]<0)
    assert np.all(p[:,3:,2]-p[:,:3,2]>0)
    assert set(root.find('.//CellData/DataArray[@Name="MaterialIDs"]').text.split())=={'7'}


def test_dense_grid_preserves_disconnected_concave_support_and_holes():
    from shapely.ops import unary_union
    from shapely.geometry import MultiPolygon
    from vkm_world.geometry_build.linked_geology import plan_triangles
    a=Polygon([(0,0),(30,0),(30,6),(9,6),(9,20),(0,20)],
      holes=[[(2,2),(2,4),(5,4),(5,2)]])
    domain=MultiPolygon([a,box(40,1,45,7)])
    triangles=plan_triangles(domain,1.3)
    rebuilt=unary_union([Polygon(v) for v in triangles])
    assert rebuilt.symmetric_difference(domain).area<1e-8
    assert sum(Polygon(v).area for v in triangles)==pytest.approx(domain.area)


def test_grid_source_machine_epsilon_slivers_do_not_create_degenerate_exports(tmp_path):
    import xml.etree.ElementTree as ET
    poly=Polygon([(-6.2,0),(23.8,0),(23.8,20),(13.800000000000002,20),
      (13.8,10),(3.8,10),(3.8,20),(-6.2,20)])
    mesh=layer_mesh(poly,lambda xy:np.full(len(xy),3.),2.,spacing=10.)
    file=tmp_path/'sliver.vtu';write_vtu(file,mesh)
    tree=ET.parse(file);p=np.fromstring(tree.find('.//Points/DataArray').text,sep=' ').reshape(-1,3)
    c=np.fromstring(tree.find('.//Cells/DataArray[@Name="connectivity"]').text,sep=' ',dtype=int).reshape(-1,6)
    a=p[c[:,1],:2]-p[c[:,0],:2];b=p[c[:,2],:2]-p[c[:,0],:2]
    assert np.all(a[:,0]*b[:,1]-a[:,1]*b[:,0]<0)
