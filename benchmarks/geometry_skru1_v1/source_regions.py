"""Read PRIVATE source-image regions without publishing corpus coordinates."""
from __future__ import annotations

import json
from pathlib import Path


def load_regions(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "vkm.private_source_regions/1":
        raise ValueError("unsupported PRIVATE source-region schema")
    if not isinstance(data.get("images"), dict) or not isinstance(data.get("correspondences"), list):
        raise ValueError("source-region images and correspondences are required")
    for pair in data["correspondences"]:
        if pair.get("source") not in data["images"] or pair.get("target") not in data["images"]:
            raise ValueError("correspondence refers to an unconfigured source image")
    return data


def validate_rectangle(rectangle: list, width: int, height: int) -> list[int]:
    if (not isinstance(rectangle, list) or len(rectangle) != 4
            or any(type(value) is not int for value in rectangle)):
        raise ValueError("source region must contain four integer pixel bounds")
    x0, y0, x1, y1 = rectangle
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("source region is outside the original image")
    return rectangle


def regions_for(config: dict, key: str, width: int, height: int, image_sha256: str) -> tuple[list, dict | None]:
    item = config["images"].get(key)
    if item is None:
        return [("source", [0, 0, width, height], "dark_line")], None
    if item.get("image_sha256") != image_sha256:
        raise ValueError("PRIVATE source-region image hash mismatch")
    regions = item.get("rois")
    if not isinstance(regions, list) or not regions:
        raise ValueError("configured source image requires explicit regions")
    for part, rectangle, mode in regions:
        if not isinstance(part, str) or mode not in ("dark_line", "red_line"):
            raise ValueError("invalid source-region name or mask")
        validate_rectangle(rectangle, width, height)
    palette = item.get("palette")
    if palette is not None:
        validate_rectangle(palette["roi"], width, height)
        for point in palette["points"]:
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(value) is not int for value in point)
                    or not (2 <= point[0] < width-2 and 2 <= point[1] < height-2)):
                raise ValueError("legend sample is outside the original image")
    return regions, palette
