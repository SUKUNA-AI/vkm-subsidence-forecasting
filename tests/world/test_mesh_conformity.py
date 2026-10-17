"""IER-02: area-preserving meshes must also have matching shared interfaces."""
import xml.etree.ElementTree as ET

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("shapely", minversion="2.1")
from shapely.affinity import rotate, translate
from shapely.geometry import MultiPolygon, Polygon, box

from vkm_world.geometry_build.linked_geology import layer_mesh, plan_triangles, write_vtu
from vkm_world.geometry_build.mesh_audit import (
    audit_layer_interface, audit_plan_triangles, audit_vertical_wedges,
)


NOTCHED = Polygon([(0, 0), (2, 0), (2, .2), (1, .5), (2, .8), (2, 2), (0, 2)])


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_review_notch_has_no_hanging_nodes_or_false_walls(angle):
    polygon = translate(rotate(NOTCHED, angle, origin=(0, 0)), xoff=101.25, yoff=-8.75)
    mesh = layer_mesh(polygon, lambda xy: 10+.2*xy[:, 0], 3, spacing=1)
    result = audit_vertical_wedges(mesh["points"], mesh["wedges"], polygon, faces=mesh["faces"])
    assert result["conforming"], result
    assert result["plan"]["hanging_node_edge_pairs"] == 0
    assert result["plan"]["false_boundary_edges"] == 0
    assert result["total_oriented_volume"] == pytest.approx(11.1)


def test_audit_detects_area_preserving_t_junction():
    # A square divided into a left half with an unsplit side, and a right half
    # whose extra midpoint is hidden from ordinary edge-count and area checks.
    triangles = np.array([
        [[0, 0], [1, 0], [1, 1]], [[0, 0], [1, 1], [0, 1]],
        [[1, 0], [2, 0], [1, .5]], [[2, 0], [2, 1], [1, .5]],
        [[2, 1], [1, 1], [1, .5]],
    ])
    result = audit_plan_triangles(triangles, box(0, 0, 2, 1))
    assert result["summed_area"] == 2
    assert result["nonmanifold_edges"] == 0
    assert result["hanging_node_edge_pairs"] == 1
    assert result["false_boundary_edges"] == 3
    assert not result["conforming"]


@pytest.mark.parametrize("spacing", [1., 1/3, .7])
def test_holes_disconnected_components_and_two_boundary_sides_conform(spacing):
    polygon = MultiPolygon([
        Polygon([(0, 0), (3, 0), (3, .2), (1, .5), (3, .8), (3, 3), (0, 3)],
                holes=[[(.1, 1.1), (.1, 1.8), (.7, 1.8), (.7, 1.1)]]),
        translate(NOTCHED, xoff=5.17),
    ])
    first = plan_triangles(polygon, spacing)
    assert np.array_equal(first, plan_triangles(polygon, spacing))
    result = audit_plan_triangles(first, polygon)
    assert result["conforming"], result


def test_exported_wedges_and_adjacent_material_interfaces(tmp_path):
    low = layer_mesh(NOTCHED, lambda xy: 10+.2*xy[:, 0], 3, spacing=1)
    high = layer_mesh(NOTCHED, lambda xy: 12+.2*xy[:, 0], 2, spacing=1)
    interface = audit_layer_interface(low["points"], low["wedges"], high["points"], high["wedges"])
    assert interface["conforming"], interface
    path = tmp_path/"notched.vtu"
    write_vtu(path, low, material_id=4)
    root = ET.parse(path)
    points = np.fromstring(root.find(".//Points/DataArray").text, sep=" ").reshape(-1, 3)
    cells = np.fromstring(root.find('.//Cells/DataArray[@Name="connectivity"]').text,
                          sep=" ", dtype=int).reshape(-1, 6)
    result = audit_vertical_wedges(points, cells, NOTCHED, winding="ogs")
    assert result["conforming"], result
    # A changed convention cannot silently pass through absolute determinants.
    assert not audit_vertical_wedges(points, cells, NOTCHED)["conforming"]


def test_interface_rejects_gap_and_alternate_tessellation():
    low = layer_mesh(NOTCHED, lambda xy: np.full(len(xy), 10.), 3, spacing=1)
    high = layer_mesh(NOTCHED, lambda xy: np.full(len(xy), 12.1), 2, spacing=1)
    result = audit_layer_interface(low["points"], low["wedges"], high["points"], high["wedges"])
    assert not result["conforming"]
    assert result["maximum_vertical_gap"] == pytest.approx(.1)
    other = layer_mesh(NOTCHED, lambda xy: np.full(len(xy), 12.), 2, spacing=.5)
    result = audit_layer_interface(low["points"], low["wedges"], other["points"], other["wedges"])
    assert not result["conforming"]
    assert result["unmatched_upper_triangles"] > 0


def test_audit_rejects_duplicate_cells_inversion_and_open_shell():
    mesh = layer_mesh(NOTCHED, lambda xy: np.full(len(xy), 10.), 3, spacing=1)
    duplicate = np.vstack([mesh["wedges"], mesh["wedges"][:1]])
    result = audit_vertical_wedges(mesh["points"], duplicate, NOTCHED)
    assert not result["conforming"]
    assert result["duplicate_wedges"] == 1
    inverted = mesh["wedges"].copy()
    inverted[0] = inverted[0, [3, 4, 5, 0, 1, 2]]
    assert not audit_vertical_wedges(mesh["points"], inverted, NOTCHED)["conforming"]
    result = audit_vertical_wedges(mesh["points"], mesh["wedges"], NOTCHED, faces=mesh["faces"][:-1])
    assert not result["conforming"]
    assert result["shell"]["nonmanifold_or_open_edges"] == 3
    reversed_face = mesh["faces"].copy()
    reversed_face[0] = reversed_face[0, ::-1]
    result = audit_vertical_wedges(mesh["points"], mesh["wedges"], NOTCHED, faces=reversed_face)
    assert not result["conforming"]
    assert result["shell"]["inconsistent_edge_winding"] == 3
