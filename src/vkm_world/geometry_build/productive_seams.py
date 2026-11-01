"""Source-constrained stratigraphic feasibility, never automatic geological admission.

Absolute layer bounds describe one explicit interpretation of a *complete*
parent interval. They are not proportions, independent priors or observations at
the target site. No parent geometry is changed and no failed world is resampled.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PartitionInterpretation:
    interpretation_id: str
    parent_roof: str
    parent_floor: str
    ordered_layers: tuple[str, ...]
    source_locators: tuple[str, ...]
    closure_basis: str
    target_scope: str
    unresolved_conditions: tuple[str, ...]

    def __post_init__(self):
        texts = (self.interpretation_id, self.parent_roof, self.parent_floor,
                 self.closure_basis, self.target_scope, *self.ordered_layers,
                 *self.source_locators, *self.unresolved_conditions)
        if not all(isinstance(v, str) and v.strip() for v in texts):
            raise ValueError("explicit nonempty interpretation, scope, closure and provenance required")
        if (not self.ordered_layers or len(set(self.ordered_layers)) != len(self.ordered_layers)
                or len(self.source_locators) != len(self.ordered_layers)):
            raise ValueError("unique ordered layers and one source locator per layer required")
        if self.parent_roof == self.parent_floor:
            raise ValueError("distinct parent endpoints required")


def bounded_stratigraphic_partition(parent_roof, parent_floor, lower_m, upper_m,
                                    interpretation: PartitionInterpretation, *, closure_tolerance_m=1e-10):
    """Construct one deterministic witness of bounded-layer feasibility.

    Parents have shape (world, location), bounds (layer,) or
    (world, layer, location). Missing bounds/parents stay UNKNOWN (NaN). A
    feasible total H must lie in [sum(lower), sum(upper)]. Starting at lower
    bounds, residual H is allocated from top to bottom up to each upper bound.
    This declared MODEL_CHOICE proves existence under the supplied rectangular
    constraints only. It is NOT a sample, fitted partition or probability law.
    A disclosed closure tolerance covers decimal/float arithmetic only; any
    emitted endpoint residual is returned, never distributed into the layers.

    A complete world is emitted only when every location is feasible. Other
    worlds keep NaN interfaces/thicknesses, with diagnostic masks/reasons; IDs
    and rows are retained. For a shared P1 TIN, ordered nonnegative nodal
    thicknesses prove containment throughout each triangle. This claim does
    not extend to independently interpolated/nonlinear surfaces or to an
    unrepresented interval. A residual interval requires its own named layer,
    bounds and provenance, never silent absorption into a neighbouring layer.
    """
    if not isinstance(interpretation, PartitionInterpretation):
        raise TypeError("PartitionInterpretation required")
    if not np.isfinite(closure_tolerance_m) or closure_tolerance_m < 0:
        raise ValueError("finite nonnegative closure tolerance required")
    top, bottom = np.asarray(parent_roof, float), np.asarray(parent_floor, float)
    if top.ndim != 2 or bottom.shape != top.shape or not top.size:
        raise ValueError("equal nonempty (world, location) parent arrays required")
    shape = (top.shape[0], len(interpretation.ordered_layers), top.shape[1])

    def bounds(values):
        a = np.asarray(values, float)
        if a.shape == (shape[1],):
            a = a[None, :, None]
        try:
            return np.broadcast_to(a, shape)
        except ValueError as exc:
            raise ValueError("bounds require (layer,) or (world, layer, location)") from exc

    lower, upper = bounds(lower_m), bounds(upper_m)
    known = (np.isfinite(top) & np.isfinite(bottom)
             & np.isfinite(lower).all(axis=1) & np.isfinite(upper).all(axis=1))
    h = top - bottom
    reasons = {
        "unknown_input": ~known,
        "invalid_layer_bounds": ((lower < 0) | (upper < lower)).any(axis=1),
        "inverted_parent": h < 0,
        "parent_thinner_than_layer_minima": h < lower.sum(axis=1) - closure_tolerance_m,
        "parent_thicker_than_layer_maxima": h > upper.sum(axis=1) + closure_tolerance_m,
    }
    feasible_locations = ~np.logical_or.reduce(list(reasons.values()))
    feasible_worlds = feasible_locations.all(axis=1)
    emitted = np.broadcast_to(feasible_worlds[:, None], top.shape)
    thickness = lower.copy()
    remaining = h - lower.sum(axis=1)
    for j in range(shape[1]):
        addition = np.minimum(np.maximum(remaining, 0), upper[:, j] - lower[:, j])
        thickness[:, j] += addition
        remaining -= addition
    thickness = np.where(emitted[:, None, :], thickness, np.nan)
    interfaces = np.concatenate((top[:, None], top[:, None] - np.cumsum(thickness, axis=1)), axis=1)
    interfaces = np.where(emitted[:, None, :], interfaces, np.nan)
    return {
        "feasible_locations": feasible_locations, "feasible_worlds": feasible_worlds,
        "violations": reasons, "thickness_m": thickness, "interfaces_m": interfaces,
        "interpretation": interpretation,
        "construction_status": "MODEL_CHOICE; deterministic feasibility witness; no probability law",
        "allocation_rule": "lower bounds then top-to-bottom residual allocation; no parent repair",
        "closure_tolerance_m": float(closure_tolerance_m),
        "closure_error_m": np.where(emitted, interfaces[:, -1] - bottom, np.nan),
        "mechanical_admission": "NOT_ADMITTED",
        "remaining_prerequisites": interpretation.unresolved_conditions,
    }


def fixed_seam_pre_admission(roof, floor, parent_roof, parent_floor, *,
                             tolerance_m, world_ids, interpretation_id, source_locators):
    """Retain every world and disclose complete-location envelope diagnostics."""
    from .gp_ensemble import seam_envelope_admissibility

    if (not isinstance(interpretation_id, str) or not interpretation_id.strip()
            or not source_locators
            or not all(isinstance(s, str) and s.strip() for s in source_locators)):
        raise ValueError("explicit interpretation and source locators required")
    result = seam_envelope_admissibility(roof, floor, parent_roof, parent_floor, tolerance_m)
    ids = tuple(world_ids)
    if (len(ids) != len(result["valid_worlds"]) or len(set(ids)) != len(ids)
            or not all(isinstance(s, str) and s.strip() for s in ids)):
        raise ValueError("one unique nonempty world ID per input world required")
    result.update(world_ids=ids, interpretation_id=interpretation_id,
                  source_locators=tuple(source_locators), mechanical_admission="NOT_ADMITTED",
                  selection_policy="all input worlds retained; no repair, rejection or resampling")
    return result
