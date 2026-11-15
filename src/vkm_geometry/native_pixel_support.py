"""Exact continuous native-pixel support and support-constrained finite RDP.

Pure functions: no I/O, source extraction, thinning, graph tracing, label binding,
georeferencing or geological admission. Closed unit cells are the entire support
corridor; simplification never expands that corridor or inserts coordinates.
"""

import copy
import hashlib
import json
import math
from collections.abc import Sequence
from fractions import Fraction

__all__ = ["validate_polyline", "simplify_supported_path"]


SUPPORT_POLICY = "EXACT_CLOSED_SEGMENT_AGAINST_CLOSED_UNIT_PIXEL_CELL_UNION"


_MAX_CELLS = 100_000


_MAX_FRAME_SIZE = 2**31 - 1


_FRAME_VALUES = {
    "origin": "top-left image edge",
    "x_axis": "right",
    "y_axis": "down",
    "unit": "pixel",
    "crs": None,
    "registration_status": "NOT_GEOREFERENCED",
    "pixel_centres": "(column+0.5,row+0.5)",
}


_FORBIDDEN_FRAME_KEYS = {
    "epsg",
    "epsg_code",
    "metric_calibration",
    "metre_per_pixel",
    "meter_per_pixel",
    "meters_per_pixel",
}


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_copy(value, name):
    retained = copy.deepcopy(value)
    try:
        _canonical_json(retained)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(name + " must be deterministic JSON data") from exc
    return retained


def _is_int(value):
    return type(value) is int


def _is_coordinate(value):
    # Python integers are finite; converting them to double may overflow.
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _fraction(value):
    if type(value) is int:
        return Fraction(value, 1)
    numerator, denominator = value.as_integer_ratio()
    return Fraction(numerator, denominator)


def _fraction_data(value):
    return {"numerator": value.numerator, "denominator": value.denominator}


def _interval_data(start, end):
    return {"start": _fraction_data(start), "end": _fraction_data(end)}


def _validate_frame(frame):
    if type(frame) is not dict:
        raise ValueError("FRAME_DICT_REQUIRED")
    required = {"id", "width", "height", *_FRAME_VALUES}
    if not required.issubset(frame):
        raise ValueError("FRAME_REQUIRED_FIELDS_MISSING")
    if not isinstance(frame["id"], str) or not frame["id"]:
        raise ValueError("FRAME_ID_NONEMPTY_STRING_REQUIRED")
    for key in ("width", "height"):
        value = frame[key]
        if not _is_int(value) or not 0 < value <= _MAX_FRAME_SIZE:
            raise ValueError("FRAME_DIMENSIONS_POSITIVE_NONBOOL_INTEGERS_REQUIRED")
    for key, expected in _FRAME_VALUES.items():
        if type(frame[key]) is not type(expected) or frame[key] != expected:
            raise ValueError("INVALID_NATIVE_PIXEL_FRAME_FIELD_" + key.upper())
    for key in frame:
        if isinstance(key, str) and key.lower().replace("-", "_") in _FORBIDDEN_FRAME_KEYS:
            raise ValueError("EPSG_OR_METRIC_CALIBRATION_NOT_ADMITTED")
    return _json_copy(frame, "frame")


def _validate_sha(value, name):
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(name + " must be 64 hexadecimal characters")
    if any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError(name + " must be 64 hexadecimal characters")
    return value


def _admission(frame, source_sha256, expected_frame, expected_source_sha256):
    retained_frame = _validate_frame(frame)
    retained_source = _validate_sha(source_sha256, "source_sha256")
    expected_source = _validate_sha(expected_source_sha256, "expected_source_sha256")
    expected = _json_copy(expected_frame, "expected_frame")
    if _canonical_json(retained_frame) != _canonical_json(expected):
        raise ValueError("SOURCE_FRAME_PIN_MISMATCH")
    if retained_source != expected_source:
        raise ValueError("SOURCE_SHA256_PIN_MISMATCH")
    frame_sha256 = hashlib.sha256(_canonical_json(retained_frame).encode("utf-8")).hexdigest()
    return retained_frame, retained_source, frame_sha256


def _sequence(value, name):
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(name + " must be a finite sequence")
    return value


def _validate_cells(cells, width, height):
    _sequence(cells, "cells")
    if len(cells) > _MAX_CELLS:
        raise ValueError("BOUNDED_CELL_MASK_LIMIT_EXCEEDED")
    result = []
    seen = set()
    for cell in cells:
        _sequence(cell, "cell")
        if len(cell) != 2:
            raise ValueError("CELL_MUST_CONTAIN_EXACTLY_X_Y")
        x, y = cell
        if not _is_int(x) or not _is_int(y):
            raise ValueError("CELL_COORDINATES_NONBOOL_INTEGERS_REQUIRED")
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError("CELL_OUTSIDE_SOURCE_FRAME")
        point = (x, y)
        if point in seen:
            raise ValueError("DUPLICATE_CELLS_NOT_ALLOWED")
        seen.add(point)
        result.append(point)
    return sorted(result)


def _prepare_support(cells, blocked_cells, frame):
    raw = _validate_cells(cells, frame["width"], frame["height"])
    blocked = _validate_cells(blocked_cells, frame["width"], frame["height"])
    raw_set = set(raw)
    if not set(blocked).issubset(raw_set):
        raise ValueError("BLOCKED_CELLS_MUST_BE_EXPLICIT_SUBSET_OF_RAW_CELLS")
    blocked_set = set(blocked)
    effective = [cell for cell in raw if cell not in blocked_set]
    return raw, blocked, effective


def _validate_path(path):
    _sequence(path, "path")
    if not len(path):
        raise ValueError("NONEMPTY_PATH_REQUIRED")
    retained = []
    exact = []
    for point in path:
        _sequence(point, "path point")
        if len(point) != 2 or not all(_is_coordinate(value) for value in point):
            raise ValueError("PATH_POINTS_REQUIRE_TWO_FINITE_NONBOOL_NUMBERS")
        retained.append([point[0], point[1]])
        exact.append((_fraction(point[0]), _fraction(point[1])))
    return retained, exact


def _clip_segment_to_cell(start, end, cell):
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    lower, upper = Fraction(0), Fraction(1)
    for origin, delta, minimum, maximum in (
        (x0, dx, Fraction(cell[0]), Fraction(cell[0] + 1)),
        (y0, dy, Fraction(cell[1]), Fraction(cell[1] + 1)),
    ):
        if delta == 0:
            if origin < minimum or origin > maximum:
                return None
            continue
        first = (minimum - origin) / delta
        second = (maximum - origin) / delta
        if first > second:
            first, second = second, first
        lower = max(lower, first)
        upper = min(upper, second)
        if lower > upper:
            return None
    if lower > upper:
        return None
    return lower, upper


def _segment_proof(segment_index, native_start, native_end, start, end, effective_cells):
    clips = []
    for cell in effective_cells:
        interval = _clip_segment_to_cell(start, end, cell)
        if interval is not None:
            clips.append((interval[0], interval[1], cell))
    clips.sort(key=lambda item: (item[0], item[1], item[2]))
    covered = []
    for lower, upper, _cell in clips:
        if covered and lower <= covered[-1][1]:
            covered[-1][1] = max(covered[-1][1], upper)
        else:
            covered.append([lower, upper])
    uncovered = []
    cursor = Fraction(0)
    for lower, upper in covered:
        if lower > cursor:
            uncovered.append((cursor, lower))
        cursor = max(cursor, upper)
    if cursor < 1:
        uncovered.append((cursor, Fraction(1)))
    supported = (
        bool(covered)
        and covered[0][0] == 0
        and covered[-1][1] == 1
        and not uncovered
    )
    return {
        "segment_index": segment_index,
        "native_start": native_start,
        "native_end": native_end,
        "supported": supported,
        "covered_intervals": [_interval_data(lower, upper) for lower, upper in covered],
        "uncovered_intervals": [_interval_data(lower, upper) for lower, upper in uncovered],
        "cell_clip_intervals": [{
            "cell_xy": list(cell),
            "interval": _interval_data(lower, upper),
        } for lower, upper, cell in clips],
    }


def _path_proof(path, raw, blocked, effective, frame, source_sha256, frame_sha256):
    retained, exact = _validate_path(path)
    segments = [
        _segment_proof(index, retained[index], retained[index + 1], exact[index], exact[index + 1], effective)
        for index in range(len(retained) - 1)
    ]
    if len(retained) == 1:
        point = exact[0]
        clips = [
            (Fraction(0), Fraction(1), cell)
            for cell in effective
            if _clip_segment_to_cell(point, point, cell) is not None
        ]
        supported = bool(clips)
        point_proof = {
            "segment_index": None,
            "native_start": retained[0],
            "native_end": retained[0],
            "supported": supported,
            "covered_intervals": [_interval_data(Fraction(0), Fraction(1))] if supported else [],
            "uncovered_intervals": [] if supported else [_interval_data(Fraction(0), Fraction(1))],
            "cell_clip_intervals": [{
                "cell_xy": list(cell),
                "interval": _interval_data(Fraction(0), Fraction(1)),
            } for _lower, _upper, cell in clips],
        }
        segments = [point_proof]
    uncovered_indices = [
        proof["segment_index"] for proof in segments
        if not proof["supported"]
    ]
    return {
        "schema": "vkm.native_pixel_polyline_support/1",
        "supported": not uncovered_indices,
        "frame": copy.deepcopy(frame),
        "source_sha256": source_sha256,
        "source_frame_sha256": frame_sha256,
        "observed_support_cells": [list(cell) for cell in raw],
        "blocked_cells": [list(cell) for cell in blocked],
        "effective_support_cell_count": len(effective),
        "path": retained,
        "path_segment_count": len(retained) - 1,
        "uncovered_segment_indices": uncovered_indices,
        "segments": segments,
        "policy": {
            "name": SUPPORT_POLICY,
            "pixel_cell": "[x,x+1]x[y,y+1]",
            "parameter_domain": "[0,1]",
            "arithmetic": "EXACT_RATIONAL",
            "epsilon": None,
            "dilation": None,
            "tolerance_expansion": False,
            "endpoint_only": False,
            "degenerate_point_membership": True,
            "nearest_source_centre_upper_bound_px": math.sqrt(0.5),
            "bound_basis": "ANY_POINT_IN_CLOSED_UNIT_PIXEL_LE_SQRT_1_2_FROM_ITS_CENTRE",
        },
    }


def validate_polyline(path, support_cells, *, frame, source_sha256,
                      expected_frame, expected_source_sha256, blocked_cells=()):
    """Validate every segment against the exact closed union of native pixels."""
    retained_frame, retained_source, frame_sha256 = _admission(
        frame, source_sha256, expected_frame, expected_source_sha256
    )
    raw, blocked, effective = _prepare_support(support_cells, blocked_cells, retained_frame)
    return _path_proof(
        path, raw, blocked, effective, retained_frame, retained_source, frame_sha256
    )


def _squared_distance_to_chord(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return (point[0] - start[0]) ** 2 + (point[1] - start[1]) ** 2
    offset_x, offset_y = point[0] - start[0], point[1] - start[1]
    dot = dx * offset_x + dy * offset_y
    if dot <= 0:
        return offset_x * offset_x + offset_y * offset_y
    if dot >= length_squared:
        to_end_x, to_end_y = point[0] - end[0], point[1] - end[1]
        return to_end_x * to_end_x + to_end_y * to_end_y
    cross = dx * offset_y - dy * offset_x
    return cross * cross / length_squared


def simplify_supported_path(path, support_cells, *, frame, source_sha256,
                            expected_frame, expected_source_sha256, tolerance,
                            blocked_cells=()):
    """Apply RDP only when the complete retained chord has exact pixel support."""
    if not _is_coordinate(tolerance) or tolerance < 0:
        raise ValueError("TOLERANCE_FINITE_NONNEGATIVE_NUMBER_REQUIRED")
    retained_frame, retained_source, frame_sha256 = _admission(
        frame, source_sha256, expected_frame, expected_source_sha256
    )
    raw, blocked, effective = _prepare_support(support_cells, blocked_cells, retained_frame)
    original_proof = _path_proof(
        path, raw, blocked, effective, retained_frame, retained_source, frame_sha256
    )
    original = original_proof["path"]
    exact = [(_fraction(point[0]), _fraction(point[1])) for point in original]
    base = {
        "schema": "vkm.support_constrained_rdp/1",
        "frame": copy.deepcopy(retained_frame),
        "source_sha256": retained_source,
        "source_frame_sha256": frame_sha256,
        "tolerance": tolerance,
        "original_path": copy.deepcopy(original),
        "original_support_proof": original_proof,
        "blocked_cells": [list(cell) for cell in blocked],
        "new_coordinates_inserted": False,
        "tolerance_expands_support_corridor": False,
    }
    if not original_proof["supported"]:
        return {
            **base,
            "path": original,
            "supported": False,
            "status": "ORIGINAL_PATH_UNSUPPORTED",
            "simplification_performed": False,
            "original_vertex_indices": list(range(len(original))),
            "rdp_checks": [],
            "support_proof": original_proof,
        }
    if tolerance == 0:
        return {
            **base,
            "path": original,
            "supported": True,
            "status": "DISABLED_ZERO_TOLERANCE_ORIGINAL_VERTICES_EXACT",
            "simplification_performed": False,
            "original_vertex_indices": list(range(len(original))),
            "rdp_checks": [],
            "support_proof": original_proof,
        }
    if len(original) > 1 and exact[0] == exact[-1]:
        return {
            **base,
            "path": original,
            "supported": True,
            "status": "REJECTED_CLOSED_LOOP_TOPOLOGY_PRESERVED",
            "simplification_performed": False,
            "original_vertex_indices": list(range(len(original))),
            "rdp_checks": [],
            "support_proof": original_proof,
        }
    if len(original) <= 2:
        return {
            **base,
            "path": original,
            "supported": True,
            "status": "PATH_TOO_SHORT_FOR_RDP",
            "simplification_performed": False,
            "original_vertex_indices": list(range(len(original))),
            "rdp_checks": [],
            "support_proof": original_proof,
        }

    tolerance_squared = _fraction(tolerance) ** 2
    kept = {0, len(original) - 1}
    checks = []
    stack = [(0, len(original) - 1)]
    while stack:
        first, last = stack.pop()
        chord_proof = _segment_proof(
            first, original[first], original[last], exact[first], exact[last], effective
        )
        maximum_index = first + 1
        maximum_distance = Fraction(-1)
        for index in range(first + 1, last):
            distance = _squared_distance_to_chord(exact[index], exact[first], exact[last])
            if distance > maximum_distance:
                maximum_distance = distance
                maximum_index = index
        chord_supported = chord_proof["supported"]
        if chord_supported and maximum_distance <= tolerance_squared:
            decision = "RETAIN_SUPPORTED_CHORD"
        else:
            decision = "SPLIT_UNSUPPORTED_CHORD" if not chord_supported else "SPLIT_RDP_DISTANCE"
            kept.add(maximum_index)
            stack.append((maximum_index, last))
            stack.append((first, maximum_index))
        checks.append({
            "start_vertex_index": first,
            "end_vertex_index": last,
            "maximum_squared_distance": _fraction_data(maximum_distance),
            "tolerance_squared": _fraction_data(tolerance_squared),
            "chord_supported": chord_supported,
            "decision": decision,
            "chord_uncovered_intervals": chord_proof["uncovered_intervals"],
        })
    indices = sorted(kept)
    simplified = [copy.deepcopy(original[index]) for index in indices]
    final_proof = _path_proof(
        simplified, raw, blocked, effective, retained_frame, retained_source, frame_sha256
    )
    if not final_proof["supported"]:
        raise AssertionError("SUPPORT_CONSTRAINED_RDP_INTERNAL_SUPPORT_FAILURE")
    return {
        **base,
        "path": simplified,
        "supported": True,
        "status": "SUPPORT_CONSTRAINED_RDP_COMPLETE",
        "simplification_performed": len(indices) != len(original),
        "original_vertex_indices": indices,
        "rdp_checks": checks,
        "support_proof": final_proof,
    }
