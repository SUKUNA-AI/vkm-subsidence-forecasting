"""Display draping must preserve a known vertical frame and fail closed at missing data/borders."""
import numpy as np
import pytest

from vkm_world.geometry_build.paraview_export import _drape, sample_grid, source_section_frame


def test_drape_uses_pixel_centres_and_true_unscaled_heights():
    grid = np.array([[100., 120.], [140., 160.]])
    transform = (0., 30., 0., 60., 0., -30.)
    assert sample_grid(grid, None, transform, 30, 30) == 130
    assert sample_grid(grid, None, transform, 15, 45) == 100
    assert sample_grid(grid, None, transform, 10, 45) is None
    assert _drape([(15, 45), (30, 30)], grid, None, transform) == [[15., 45., 100.], [30., 30., 130.]]


@pytest.mark.parametrize("missing", [-32767., np.nan])
def test_drape_never_replaces_missing_or_outside_heights_by_zero(missing):
    grid = np.array([[100., 120.], [missing, 160.]])
    transform = (0., 30., 0., 60., 0., -30.)
    assert sample_grid(grid, -32767, transform, 30, 30) is None
    assert _drape([(30, 30), (31, 30)], grid, -32767, transform) is None


def test_vertical_section_requires_explicit_metric_frame_and_axis():
    assert source_section_frame({"frame_id": "local-borehole-xy", "x_unit": "m", "y_unit": "m"}) is None
    section = {"frame_kind": "SOURCE_SECTION", "frame_id": "source-section-001",
               "source_x_unit": "m", "source_y_unit": "m", "source_y_axis": "UP"}
    assert source_section_frame(section) == {"id": "source-section-001", "y_sign": 1}
    assert source_section_frame({**section, "source_y_axis": "DOWN"}) == {"id": "source-section-001", "y_sign": -1}
    assert source_section_frame({**section, "source_y_axis": "UNKNOWN"}) is None
    assert source_section_frame({**section, "source_x_unit": "px"}) is None
