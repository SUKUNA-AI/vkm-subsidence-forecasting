"""Withdraw an absolute collar hypothesis while retaining observed depth gaps.

This is a derived use-time view, never a correction of the source depths.
Bounds relative to an unknown collar cannot constrain an absolute contact.
Only differences between exact depths and existing thickness observations
remain informative in that frame. A single depth has no translation-invariant
information; its exclusion is explicit in the returned ledger.
"""
from __future__ import annotations

from dataclasses import replace
import numpy as np

from .contact_predictor import SupportSet


def withdraw_absolute_anchors(supports: SupportSet, physical_ids):
    ids = set(physical_ids)
    unknown = ids - set(supports.ids)
    if unknown:
        raise ValueError(f"unknown support IDs: {sorted(unknown)}")
    lower, upper = supports.depth_lower.copy(), supports.depth_upper.copy()
    thickness = [dict(t) for t in supports.thickness]
    ledger = []
    for row, well in enumerate(supports.ids):
        if well not in ids:
            continue
        points = np.flatnonzero(supports.point_mask()[row])
        derived = []
        for top, bottom in zip(points[:-1], points[1:]):
            gap = float(lower[row, bottom] - lower[row, top])
            item = {"row": row, "top": int(top), "bottom": int(bottom),
                    "lower": gap, "upper": gap,
                    "id": f"RELATIVE_DEPTH_GAP:{well}:{top}:{bottom}"}
            thickness.append(item)
            derived.append(item["id"])
        ledger.append({"physical_id": well, "absolute_anchor": "WITHDRAWN",
                       "original_depths_preserved_in_parent": True,
                       "derived_gap_ids": derived,
                       "excluded_depth_bound_contacts": [supports.contacts[c] for c in range(len(supports.contacts))
                           if np.isfinite(lower[row, c]) or np.isfinite(upper[row, c])],
                       "reason": "collar translation unknown; only exact depth differences retained"})
        lower[row] = upper[row] = np.nan
    independent = None if supports.independent_collar is None else supports.independent_collar.copy()
    if independent is not None:
        independent[[i for i, well in enumerate(supports.ids) if well in ids]] = np.nan
    return replace(supports, ids=list(supports.ids), groups=list(supports.groups),
                   xy=supports.xy.copy(), collar=supports.collar.copy(), contacts=list(supports.contacts),
                   depth_lower=lower, depth_upper=upper,
                   thickness=thickness, independent_collar=independent), ledger
