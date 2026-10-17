"""Synthetic image checks for a source diagram: known gaps stay gaps and graphical scales stay explicit."""
import hashlib
import json
import numpy as np
import pytest
from PIL import Image

from vkm_world.geometry_build.source_section import (affine_axis, build_source_section, continuous_segments,
                                                     digitise_section, sample_colour_band)


def test_colour_envelope_uses_column_median_and_preserves_a_real_gap():
    image = np.full((61, 81, 3), 255, dtype="uint8")
    colour = (50, 100, 150)
    image[20:31, 5:76] = colour
    # A gap wider than the +-3 window must not be filled or bridged by a polygon.
    image[:, 32:49] = 255
    # Narrow black survey line and a two-pixel colour speck are not band evidence in one column.
    image[20:31, 18] = 0
    image[5:7, 18] = colour
    band = sample_colour_band(image, [5, 1, 75, 59], colour)
    assert band["x_px"].tolist() == [5, 15, 25, 35, 45, 55, 65, 75]
    assert band["valid"].tolist() == [True, True, True, False, False, True, True, True]
    assert np.all(band["top_px"][band["valid"]] == 20)
    assert np.all(band["bottom_px"][band["valid"]] == 30)
    assert continuous_segments(band["valid"]) == [(0, 3), (5, 8)]
    assert band["qa"]["gap_fraction"] == .25
    assert band["qa"]["polygon_segments"] == 2


def test_colour_tolerance_is_euclidean_not_independent_rgb_channels():
    image = np.zeros((21, 21, 3), dtype="uint8")
    image[4:10, 1:20] = [110, 110, 110]  # distance sqrt(300) <18
    image[12:18, 1:20] = [118, 118, 118]  # each channel <=18, Euclidean distance >18
    band = sample_colour_band(image, [1, 1, 19, 19], [100, 100, 100])
    assert np.all(band["top_px"] == 4)
    assert np.all(band["bottom_px"] == 9)


def test_explicit_annotation_mask_cannot_create_a_false_geological_body():
    image = np.full((61, 81, 3), 255, dtype="uint8")
    colour = [50, 100, 150]
    image[40:51, 5:76] = colour
    image[10:21, 32:49] = colour  # A same-colour label/annotation above the real band.
    raw = sample_colour_band(image, [5, 1, 75, 59], colour)
    assert np.nanmin(raw["top_px"]) == 10  # Shows why the explicit graphical mask is necessary.
    masked = sample_colour_band(image, [5, 1, 75, 59], colour, exclude_bbox_pixels=[[30, 5, 50, 25]])
    assert np.all(masked["top_px"] == 40)
    assert np.all(masked["bottom_px"] == 50)
    assert masked["qa"]["excluded_matching_pixels"] == 17*11
    assert masked["qa"]["gap_fraction"] == 0


def test_explicit_axis_does_not_supply_a_map_or_vertical_datum():
    assert affine_axis([10, 60, 110], [[10, -50], [110, 950]]).tolist() == [-50, 450, 950]
    assert affine_axis([20, 70, 120], [[20, 100], [120, -100]]).tolist() == [100, 0, -100]
    with pytest.raises(ValueError):
        affine_axis([10], [[10, 1], [10, 2]])


def test_too_few_class_pixels_remain_unknown():
    image = np.full((21, 21, 3), 255, dtype="uint8")
    image[4:7, :, :] = [50, 100, 150]
    band = sample_colour_band(image, [1, 1, 19, 19], [50, 100, 150])
    assert not band["valid"].any()
    assert band["qa"]["gap_fraction"] == 1
    assert band["qa"]["polygon_segments"] == 0


def test_features_use_separate_metric_frame_without_bridging_colour_gaps():
    image = np.full((61, 81, 3), 255, dtype="uint8")
    image[20:31, 5:76] = [50, 100, 150]
    image[:, 32:49] = 255
    config = {"schema": "vkm.source_section_config/1", "plot_bbox_pixels": [5, 1, 75, 59],
              "source": {"document": "synthetic-source", "document_sha256": "0"*64},
              "frame": {"id": "SYNTHETIC_SECTION", "kind": "SOURCE_SECTION", "source_x_unit": "m",
                        "source_y_unit": "m", "source_y_axis": "UP", "x_origin_px": 5, "x_m_per_px": 2,
                        "y_origin_px": 25, "y_m_per_px": -1},
              "units": [{"id": "UNIT_A", "unit_as_printed": "A", "color_rgb": [50, 100, 150]}],
              "controls": [{"id": "PROJECTED", "label": "projected support", "xy_pixels": [[15, 10], [15, 50]],
                            "kind": "SOURCE_PROJECTED_SUPPORT_LINE"}]}
    features, qa, _ = digitise_section(image, config)
    polygons = [feature for feature in features if feature["geometry_type"] == "Polygon"]
    assert len(polygons) == 2
    assert max(x for x, y in polygons[0]["coordinates"]) == 40
    assert min(x for x, y in polygons[1]["coordinates"]) == 100
    assert all(feature["properties"]["frame_id"] == "SYNTHETIC_SECTION" for feature in features)
    assert all(feature["properties"]["map_trace"] == "UNKNOWN" for feature in features)
    control = next(feature for feature in features if feature["layer"] == "section_controls")
    assert control["coordinates"] == [[20., 15.], [20., -25.]]
    assert control["properties"]["semantic_role"] == "SOURCE_PROJECTED_SUPPORT_LINE"
    assert qa["units"]["UNIT_A"]["gap_fraction"] == .25
    assert digitise_section(image, config) == (features, qa, digitise_section(image, config)[2])


def test_changed_pinned_source_image_fails_before_creating_output(tmp_path):
    resources = tmp_path/"resources"
    resources.mkdir()
    Image.new("RGB", (20, 20), "white").save(resources/"diagram.png")
    (resources/"source.bin").write_bytes(b"synthetic source")
    config = {"image": "diagram.png", "source": {"document": "source.bin", "image_sha256": "0"*64,
              "document_sha256": hashlib.sha256(b"synthetic source").hexdigest()}}
    (resources/"config.json").write_text(json.dumps(config), encoding="utf-8")
    out = tmp_path/"output"
    with pytest.raises(ValueError, match="Source image SHA-256"):
        build_source_section(resources, "config.json", out)
    assert not out.exists()
