"""Scientific display geometry must not bridge holes or silently lose concavities."""
import importlib.util
from pathlib import Path

import pytest

np=pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("shapely")
from shapely.geometry import Polygon

spec=importlib.util.spec_from_file_location("skru1_geology_views",Path(__file__).parents[2]/"scripts/build_skru1_geology_views.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_source_hole_is_preserved_with_complete_area():
    poly=Polygon([(0,0),(8,0),(8,8),(0,8)],holes=[[(2,2),(2,6),(6,6),(6,2)]])
    triangles=[Polygon(t) for t in module.polygon_triangles(poly)]
    assert sum(t.area for t in triangles)==pytest.approx(poly.area)
    assert all(poly.covers(t) for t in triangles)


def test_invalid_source_is_rejected_instead_of_repaired():
    poly=Polygon([(0,0),(2,2),(0,2),(2,0)])
    with pytest.raises(ValueError,match="invalid source polygon"):
        module.polygon_triangles(poly)


def test_concave_coverage_is_complete_or_explicitly_rejected():
    # The caller must never get a plausible but incomplete concave surface.
    rng=np.random.default_rng(23)
    for _ in range(20):
        angle=np.sort(rng.uniform(0,2*np.pi,10)); radius=rng.uniform(.2,2,10)
        poly=Polygon(np.column_stack([np.cos(angle)*radius,np.sin(angle)*radius]))
        if not poly.is_valid:
            continue
        try:
            triangles=[Polygon(t) for t in module.polygon_triangles(poly)]
        except ValueError as error:
            assert "complete source polygon" in str(error)
        else:
            assert sum(t.area for t in triangles)==pytest.approx(poly.area)
