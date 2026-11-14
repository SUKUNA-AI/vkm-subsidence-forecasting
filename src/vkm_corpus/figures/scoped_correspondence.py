"""Additive SDB-02 scope correction. Source and historical link rows are immutable.

Similarity is checked in the common source-page plane; an absent NAV unit or
epoch is never inferred from a close curve. A scoped match cannot confirm a
complete published curve, let alone a figure. No DB/network writes.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import Counter


VERSION = "VKM-GEO-01-MA-SDB02-SCOPE-1"
PAGE_DY_TOLERANCE_PT = 0.75
NAV_MAX_INTERPOLATION_GAP_PT = 4.0
CANONICAL_MAX_INTERPOLATION_GAP_PT = 24.0


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_json(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def identity_guard(left, right):
    """Return independent semantic conflicts; no page-only identity shortcut."""
    bad = []
    for field in ("source_id", "page_id", "figure", "panel", "frame_id"):
        a, b = left.get(field), right.get(field)
        if not a or not b:
            bad.append("MISSING_" + field.upper())
        elif a != b:
            bad.append("DIFFERENT_" + field.upper())
    for field in ("epoch", "unit"):
        a, b = left.get(field), right.get(field)
        if a and b and a != b:
            bad.append("DIFFERENT_" + field.upper())
        elif not a or not b:
            bad.append("UNVERIFIED_" + field.upper())
    a, b = left.get("source_sha256"), right.get("source_sha256")
    if not a or not b or a != b:
        bad.append("SOURCE_BYTES_UNVERIFIED")
    return bad


def _check_points(points):
    seen = set()
    for p in points:
        if type(p["index"]) is not int or p["index"] < 0 or p["index"] in seen:
            raise ValueError("duplicate/invalid point index")
        seen.add(p["index"])
        if any(type(p[k]) not in (int, float) or not math.isfinite(p[k]) for k in ("x", "y")):
            raise ValueError("non-finite/invalid coordinate")
    if not points:
        raise ValueError("empty trace")


def split_trace(points, max_gap):
    """Keep original sequence. Do not connect reversals, duplicate X or large gaps."""
    if (type(max_gap) not in (int, float) or max_gap <= 0
            or (type(max_gap) is float and not math.isfinite(max_gap))):
        raise ValueError("invalid interpolation gap")
    _check_points(points)
    segments, breaks, current = [], [], []
    for p in points:
        if current:
            q = current[-1]
            dx = p["x"] - q["x"]
            if dx <= 0 or dx > max_gap:
                segments.append(current)
                breaks.append({"left_index": q["index"], "right_index": p["index"], "dx_pt": dx,
                               "reason": "X_REVERSES_OR_DUPLICATES" if dx <= 0 else "UNBRIDGED_GAP"})
                current = []
        current.append(p)
    if current:
        segments.append(current)
    return segments, breaks


def _at_x(segments, x):
    results = []
    for segment in segments:
        if len(segment) < 2 or not segment[0]["x"] <= x <= segment[-1]["x"]:
            continue
        xs = [p["x"] for p in segment]
        j = min(max(bisect.bisect_left(xs, x), 1), len(xs) - 1)
        a, b = segment[j - 1], segment[j]
        y = a["y"] + (b["y"] - a["y"]) * (x - a["x"]) / (b["x"] - a["x"])
        results.append((y, [a["index"], b["index"]]))
    # A curve that folds/mixes at one X is ambiguous, never choose the nearest branch.
    return results[0] if len(results) == 1 else None


def compare_scoped(canonical, nav, window, *, tolerance=PAGE_DY_TOLERANCE_PT):
    # Booleans/strings are not numeric bounds; the tolerance stays positive.
    # NaN/Infinity must never turn an arbitrary offset into geometry support.
    if (type(tolerance) not in (int, float)
            or (type(tolerance) is float and not math.isfinite(tolerance)) or tolerance <= 0
            or len(window) != 2
            or any(type(v) not in (int, float) or (type(v) is float and not math.isfinite(v)) for v in window)
            or not window[0] < window[1]):
        raise ValueError("invalid scope/tolerance")
    cs, cb = split_trace(canonical, CANONICAL_MAX_INTERPOLATION_GAP_PT)
    ns, nb = split_trace(nav, NAV_MAX_INTERPOLATION_GAP_PT)

    def direction(source, target):
        checked, accepted, rejected, outside, unavailable = [], [], [], [], []
        for p in source:
            if not window[0] <= p["x"] <= window[1]:
                outside.append(p["index"])
                continue
            at = _at_x(target, p["x"])
            if at is None:
                unavailable.append(p["index"])
                continue
            y, segment = at
            rec = {"index": p["index"], "x_page_pt": p["x"], "abs_dy_pt": abs(p["y"] - y),
                   "other_bracketing_indices": segment}
            checked.append(rec)
            (accepted if rec["abs_dy_pt"] <= tolerance else rejected).append(rec)
        return {"checked": checked, "accepted": accepted, "rejected": rejected,
                "outside_scope_indices": outside, "unavailable_within_scope_indices": unavailable,
                "max_checked_abs_dy_pt": max((r["abs_dy_pt"] for r in checked), default=None)}

    c2n, n2c = direction(canonical, ns), direction(nav, cs)
    accepted_c = [r["index"] for r in c2n["accepted"]]
    accepted_n = [r["index"] for r in n2c["accepted"]]
    # The new decision names selected point support, not an unverified continuous interval.
    return {"comparison_frame": "SOURCE_PAGE_PT_X_RIGHT_Y_DOWN", "scope_window_page_x_pt": window,
            "tolerance_abs_dy_pt": tolerance,
            "nav_interpolation_max_gap_pt": NAV_MAX_INTERPOLATION_GAP_PT,
            "canonical_interpolation_max_gap_pt": CANONICAL_MAX_INTERPOLATION_GAP_PT,
            "canonical_to_nav": c2n, "nav_to_canonical": n2c,
            "canonical_trace_breaks": cb, "nav_trace_breaks": nb,
            "scoped_canonical_point_indices": accepted_c, "scoped_nav_point_indices": accepted_n,
            "whole_trace_identity": False,
            "scoped_geometry_support": len(accepted_c) >= 2 and len(accepted_n) >= 2,
            "complete_series_or_figure": False,
            "scope_note": "Only listed point indices and their checked interpolation brackets; no extrapolation across gaps; no claim for other segments."}


def validate_overlay(rows):
    keys = [r["correction_id"] for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate correction ID")
    for row in rows:
        if row.get("whole_trace_identity") is not False or row.get("scientific_admission") != "NOT_GRANTED":
            raise ValueError("scope correction cannot grant whole identity or admission")
        if not row.get("old_row_sha256") or not row.get("basis") or not row.get("canonical_series_id"):
            raise ValueError("missing correction provenance")


def apply_overlay(historical_rows, corrections):
    """Return a new projection, refuse stale bases, and preserve every original field."""
    validate_overlay(corrections)
    base = {r["canonical_series_id"]: r for r in historical_rows}
    if len(base) != len(historical_rows):
        raise ValueError("duplicate historical series")
    grouped = {}
    for c in corrections:
        sid = c["canonical_series_id"]
        if sid not in base or sha256_json(base[sid]) != c["old_row_sha256"]:
            raise ValueError("stale/missing historical row")
        grouped.setdefault(sid, []).append(c)
    result = []
    for sid, r in sorted(base.items()):
        history = sorted(grouped.get(sid, []), key=lambda c: c["precedence"])
        record = dict(r, historical_link_status=r["link_status"], historical_link_type=r.get("link_type"),
                      whole_trace_identity_permitted=False, scope_corrections=history)
        if history:
            record["link_status"] = history[-1]["new_scope_status"]
            record["link_type"] = "SCOPED_GEOMETRY_ONLY" if history[-1]["precedence"] == 30 else "HISTORICAL_EVIDENCE_SCOPE_UNVERIFIED"
        result.append(record)
    return result


def coverage_errors(rows, expected_keys, *, key):
    keys = [r[key] for r in rows]
    counts = Counter(keys)
    return {"missing": sorted(set(expected_keys) - set(keys)),
            "unexpected": sorted(set(keys) - set(expected_keys)),
            "duplicates": sorted(k for k, v in counts.items() if v != 1)}
