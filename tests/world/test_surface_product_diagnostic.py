"""Small independent checks for diagnostic raster operators; no site data or GDAL needed."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

SPEC = importlib.util.spec_from_file_location("surface_product_diagnostic",
                                             Path(__file__).resolve().parents[2]/"scripts/compare_surface_products.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_pixel_centres_and_rotated_grid_are_inverted_without_half_pixel_shift():
    assert M.pixel_center_indices(11., 19., (10., 2., 0., 20., 0., -2.)) == (0., 0.)
    # Cell centre (2.5,3.5) in a nonorthogonal affine grid.
    gt = (10., 2., .3, 20., .2, -3.)
    x, y = 10.+2.*2.5+.3*3.5, 20.+.2*2.5-3.*3.5
    np.testing.assert_allclose(M.pixel_center_indices(x, y, gt), [2., 3.], atol=1e-13)


def test_bilinear_matches_independent_plane_and_does_not_mix_nodata_with_zero():
    # f(c,r)=10+2c+3r has exact bilinear reproduction.
    assert M.bilinear_window([[10.,12.],[13.,15.]], .25, .75) == pytest.approx(12.75)
    assert M.bilinear_window([[0., -999.], [0., 0.]], .25, .75, -999.) is None
    assert M.bilinear_window([[0., np.nan], [np.nan, np.nan]], 0., 0.) == 0.


def test_scale_conflict_is_explicit_and_never_automatically_resolved_by_height():
    assert M.physical_value(150., .1, 0., "metadata") == 15.
    assert M.physical_value(150., .1, 0., "raw_as_m_scenario") == 150.
    assert M.physical_value(None, .1, 0., "metadata") is None
    with pytest.raises(ValueError):
        M.physical_value(150., .1, 0., "auto_best_fit")


def test_exact_residual_denominator_and_empty_population_remain_distinct():
    s = M.residual_stats([-1., 1., 3.])
    assert s["n"] == 3 and s["bias_m"] == 1.
    assert s["rmse_m"] == pytest.approx(np.sqrt(11/3))
    assert M.residual_stats([])["rmse_m"] is None
    with pytest.raises(ValueError):
        M.residual_stats([np.nan])


def test_relative_input_paths_cannot_escape_declared_roots(tmp_path):
    root = tmp_path/"root"; root.mkdir()
    file = root/"file.json"; file.write_text("{}")
    assert M.rooted_path("PRIVATE:file.json", {"PRIVATE": root}) == file
    with pytest.raises(ValueError, match="escapes"):
        M.rooted_path("PRIVATE:../file.json", {"PRIVATE": root})
    with pytest.raises(ValueError):
        M.rooted_path("C:/machine/file", {"PRIVATE": root})


@pytest.mark.parametrize("values,dx,dy", [([[1.]], 0., 0.), ([[1.,2.],[3.,4.]], -.1, .5)])
def test_invalid_bilinear_contract_fails(values, dx, dy):
    with pytest.raises(ValueError):
        M.bilinear_window(values, dx, dy)
