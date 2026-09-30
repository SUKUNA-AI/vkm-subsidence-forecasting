import pytest

from benchmarks.abc_completion_v1.topology import nonzero_edges,signed_area,split_closed_ring,safe_restructure


def test_split_at_existing_vertex_keeps_all_boundary_edges():
    ring=[(0,0),(2,0),(2,2),(0,0),(-2,0),(-2,-2),(0,0)]
    cycles=split_closed_ring(ring)
    assert len(cycles)==2 and nonzero_edges([ring])==nonzero_edges(cycles)
    assert nonzero_edges([ring],directed=True)==nonzero_edges(cycles,directed=True)
    assert {point for cycle in cycles for point in cycle}==set(ring)


def test_backtracking_branch_not_silently_dropped():
    with pytest.raises(ValueError,match='non-area branch'):
        split_closed_ring([(0,0),(2,0),(0,0),(0,2),(2,2),(0,0)])


def test_mixed_winding_requires_explicit_fill_rule():
    with pytest.raises(ValueError,match='mixed winding'):
        split_closed_ring([(0,0),(2,0),(2,2),(0,0),(-2,-2),(-2,0),(0,0)])


def test_crossing_segment_cannot_be_fixed_by_inventing_intersection():
    pytest.importorskip('shapely')
    geometry={'type':'Polygon','coordinates':[[[0,0],[2,2],[0,2],[2,0],[0,0]]]}
    repaired,reason=safe_restructure(geometry)
    assert repaired is None


def test_existing_vertex_split_gives_valid_point_touching_components():
    shapely=pytest.importorskip('shapely')
    from shapely.geometry import shape
    geometry={'type':'Polygon','coordinates':[[[0,0],[2,0],[2,2],[0,0],[-2,0],[-2,-2],[0,0]]]}
    repaired,reason=safe_restructure(geometry)
    assert repaired['type']=='MultiPolygon' and shape(repaired).is_valid


def test_hole_outside_exterior_is_not_reassigned_to_new_land():
    pytest.importorskip('shapely')
    geometry={'type':'Polygon','coordinates':[
        [[0,0],[4,0],[4,4],[0,4],[0,0]],[[6,6],[7,6],[7,7],[6,7],[6,6]]]}
    repaired,reason=safe_restructure(geometry)
    assert repaired is None and 'owner' in reason
