"""Check actual coordinates at the boundary between views and calculation inputs.

Preserving native elevations does not establish a datum or geological accuracy.
Only an explicitly documented vertical operation may change a calculation frame.
"""
from __future__ import annotations

import numpy as np


NATIVE = "NATIVE_INTERPRETATION"
TRANSFORMED = "DATUM_TRANSFORMED"
DISPLAY = "DISPLAY_ONLY"


def _points(value):
    result = np.asarray(value, dtype=np.float64)
    if result.ndim != 2 or result.shape[1] != 3 or not np.isfinite(result).all():
        raise ValueError("finite XYZ coordinates required")
    return result


def vertical_offset(operation, source_frame, target_frame):
    """Validate the supported constant operation; never infer an unknown offset.

The operation's source records must document geodetic control, not a difference
between a DSM and a geological interpretation. Variable operations need their
own implementation and independent evidence; they are not approximated here.
"""
    if not operation or operation.get("kind") != "VERTICAL_OFFSET":
        raise ValueError("documented VERTICAL_OFFSET required")
    if operation.get("from_crs") != source_frame or operation.get("to_crs") != target_frame:
        raise ValueError("vertical operation direction/frame mismatch")
    if operation.get("basis") != "INDEPENDENT_GEODETIC_CONTROL":
        raise ValueError("DSM closure is not a geodetic operation")
    if not operation.get("evidence_ids") or not operation.get("control_points"):
        raise ValueError("vertical operation requires evidence and control points")
    offset = operation.get("parameters", {}).get("offset_m")
    if offset is None or not np.isfinite(offset):
        raise ValueError("unknown vertical offset cannot be replaced by zero")
    return float(offset)


def geometry_coordinates(native, *, role, source_frame, target_frame=None,
                         operation=None, display_delta=None):
    """Create a separate representation without mutating the native input."""
    points = _points(native).copy()
    if not source_frame or source_frame == "UNKNOWN":
        raise ValueError("an identified source frame is required, even if its datum is unknown")
    if role == NATIVE:
        if operation is not None or (target_frame is not None and target_frame != source_frame):
            raise ValueError("native coordinates cannot claim a different frame")
    elif role == TRANSFORMED:
        if not target_frame or target_frame == source_frame:
            raise ValueError("distinct target frame required")
        points[:, 2] += vertical_offset(operation, source_frame, target_frame)
    elif role == DISPLAY:
        delta = np.asarray(display_delta, dtype=np.float64)
        if delta.shape != (len(points),) or not np.isfinite(delta).all():
            raise ValueError("one finite display offset per vertex required")
        points[:, 2] += delta
    else:
        raise ValueError("unknown geometry role")
    return points


def calculation_coordinates(points, native, *, role, source_frame, target_frame,
                            operation=None, tolerance_m=1e-8):
    """Use-time gate for a future calculation adapter, not solver qualification.

Compare real Points with the declared operation. Extra OriginalSourceZ arrays
or a native label cannot make a warped mesh pass this gate.
"""
    if role == DISPLAY:
        raise ValueError("DISPLAY_ONLY geometry is forbidden in calculation export")
    if not np.isfinite(tolerance_m) or tolerance_m < 0 or tolerance_m > 1e-4:
        raise ValueError("coordinate tolerance must be finite, nonnegative and at most 1e-4 m")
    if not target_frame or target_frame == "UNKNOWN":
        raise ValueError("calculation frame must be explicitly identified")
    expected = geometry_coordinates(native, role=role, source_frame=source_frame,
                                    target_frame=target_frame, operation=operation)
    actual = _points(points)
    if actual.shape != expected.shape or not np.allclose(actual, expected, rtol=0, atol=tolerance_m):
        raise ValueError("Points disagree with the declared height operation")
    return actual.copy()
