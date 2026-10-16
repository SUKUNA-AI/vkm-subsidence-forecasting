"""Display filtering must not invent data, offset a plane, or alter source values."""
import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
from vkm_world.geometry_build.terrain_display import smooth_raster_values


def test_constant_dsm_is_preserved_exactly_without_mutation():
    source = np.full((9, 11), 17.25)
    before = source.copy()
    result = smooth_raster_values(source, 9, 11, 1.2)
    assert np.array_equal(result, before)
    assert np.array_equal(source, before)
    assert result is not source and result.dtype == np.float64


def test_smoothing_reduces_pixel_roughness_and_preserves_broad_affine_trend():
    y, x = np.mgrid[:41, :47]
    trend = 5 + 0.2 * x + 0.1 * y
    source = trend + np.where((x + y) % 2, 2.0, -2.0)
    result = smooth_raster_values(source, 41, 47, 1.2)
    assert np.mean(np.abs(np.diff(result, axis=0))) < np.mean(np.abs(np.diff(source, axis=0))) / 10
    assert np.max(np.abs(result[6:-6, 6:-6] - trend[6:-6, 6:-6])) < 1e-5
    assert result.min() >= source.min() and result.max() <= source.max()
    assert abs(result.mean() - source.mean()) < 1e-12


def test_flat_raster_roundtrip_matches_two_dimensional_order():
    source = np.arange(63, dtype=np.float64).reshape(7, 9)
    flat = smooth_raster_values(source.ravel(), 7, 9, 0.8)
    assert flat.shape == (63,)
    assert np.array_equal(flat.reshape(7, 9), smooth_raster_values(source, 7, 9, 0.8))


@pytest.mark.parametrize("values,rows,cols,sigma", [
    ([1, 2, 3], 2, 2, 1.0), ([1, float("nan")], 1, 2, 1.0),
    ([1, 2], 1, 2, 0), ([1, 2], 1, 2, float("inf")),
    ([1, 2], True, 2, 1.0), ([1, 2], 1.0, 2, 1.0),
])
def test_incomplete_or_invalid_dsm_is_rejected(values, rows, cols, sigma):
    with pytest.raises(ValueError):
        smooth_raster_values(values, rows, cols, sigma)
