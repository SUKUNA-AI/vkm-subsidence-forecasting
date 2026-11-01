"""Versioned positive depth links (MODEL_CHOICE, never physical bounds).

The opt-in squareplus top link has at most linear growth. Thus a latent law
with a finite second moment retains that moment after this link. This module
does not establish posterior propriety, MCMC convergence or field accuracy.
The five interval fields retain their existing ReLU/absence semantics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
from types import MappingProxyType

import numpy as np

LEGACY_LAW = "legacy_log_top_relu_intervals_v1"
SQUAREPLUS_LAW = "squareplus_top_relu_intervals_v1"
MEDIAN_SCALE_RULE = "median_valid_corrected_absolute_top_depths_v1"


def _positive_scale(scale_m):
    if not np.isscalar(scale_m) or not math.isfinite(float(scale_m)) or float(scale_m) <= 0:
        raise ValueError("finite positive length scale required")
    return float(scale_m)


def squareplus(latent, scale_m):
    """Float64 D=s/2*(Y+hypot(Y,2)), evaluated without cancellation.

    Y is dimensionless and s is a length. Unrepresentable depths fail closed;
    no clipping, floor, maximum depth or replacement of nonfinite values.
    """
    s = _positive_scale(scale_m)
    if np.iscomplexobj(latent):
        raise ValueError("real latent values required")
    y = np.asarray(latent, dtype=float)
    if not np.isfinite(y).all():
        raise ValueError("finite latent values required")
    h = np.hypot(y, 2.0) / 2.0
    positive = y >= 0
    out = np.empty_like(y)
    with np.errstate(over="ignore", under="ignore", divide="ignore", invalid="ignore"):
        out[positive] = s * (y[positive] / 2.0 + h[positive])
        out[~positive] = s / (h[~positive] - y[~positive] / 2.0)
    if not np.isfinite(out).all() or np.any(out <= 0):
        raise FloatingPointError("squareplus depth is not representable as positive finite float64")
    return out


def inverse_squareplus(depth, scale_m):
    """Dimensionless Y=D/s-s/D for finite positive D (no depth floor)."""
    s = _positive_scale(scale_m)
    if np.iscomplexobj(depth):
        raise ValueError("real depths required")
    d = np.asarray(depth, dtype=float)
    if not np.isfinite(d).all() or np.any(d <= 0):
        raise ValueError("finite positive corrected top depths required; no implicit floor")
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        out = d / s - s / d
    if not np.isfinite(out).all():
        raise FloatingPointError("inverse squareplus latent value is not representable as float64")
    return out


def squareplus_torch(latent, scale_m):
    """Same link on a torch device, explicitly in float64; torch is optional."""
    import torch

    s = _positive_scale(scale_m)
    y = torch.as_tensor(latent)
    if y.is_complex():
        raise ValueError("real latent values required")
    y = y.to(dtype=torch.float64)
    if not bool(torch.isfinite(y).all()):
        raise ValueError("finite latent values required")
    h = torch.hypot(y, torch.full_like(y, 2.0)) / 2.0
    positive = y >= 0
    out = torch.empty_like(y)
    out[positive] = s * (y[positive] / 2.0 + h[positive])
    out[~positive] = s / (h[~positive] - y[~positive] / 2.0)
    if not bool(torch.isfinite(out).all()) or bool((out <= 0).any()):
        raise FloatingPointError("squareplus depth is not representable as positive finite float64")
    return out


def inverse_squareplus_torch(depth, scale_m):
    """Torch float64 inverse; keeps the input device and fails closed."""
    import torch

    s = _positive_scale(scale_m)
    d = torch.as_tensor(depth)
    if d.is_complex():
        raise ValueError("real depths required")
    d = d.to(dtype=torch.float64)
    if not bool(torch.isfinite(d).all()) or bool((d <= 0).any()):
        raise ValueError("finite positive corrected top depths required; no implicit floor")
    out = d / s - s / d
    if not bool(torch.isfinite(out).all()):
        raise FloatingPointError("inverse squareplus latent value is not representable as float64")
    return out


@dataclass(frozen=True)
class PositiveFieldLaw:
    """Opt-in law and pinned length scale; default preserves the legacy path.

    ``from_corrected_top`` receives only valid absolute TOP supports. Caller
    applies its reading mask first (in particular, relative-only 74 is excluded).
    Input hashes identify the package/mask/shift used to derive the scale.
    """

    law_id: str = LEGACY_LAW
    top_scale_m: float | None = None
    scale_rule: str | None = None
    scale_input_sha256: dict = field(default_factory=dict)
    top_support_count: int | None = None

    def __post_init__(self):
        if self.law_id not in (LEGACY_LAW, SQUAREPLUS_LAW):
            raise ValueError("unknown positive field law")
        if self.law_id == SQUAREPLUS_LAW:
            _positive_scale(self.top_scale_m)
            if not self.scale_rule:
                raise ValueError("explicit scale rule required for squareplus")
        elif self.top_scale_m is not None or self.scale_rule is not None:
            raise ValueError("legacy link has no squareplus scale")
        for name, digest in self.scale_input_sha256.items():
            if not name or not re.fullmatch(r"[0-9a-fA-F]{64}", str(digest)):
                raise ValueError("named SHA256 scale input pins required")
        object.__setattr__(self, "scale_input_sha256", MappingProxyType(dict(self.scale_input_sha256)))

    @classmethod
    def from_corrected_top(cls, depths_m, *, input_sha256):
        d = np.asarray(depths_m, dtype=float)
        if d.ndim != 1 or len(d) < 4 or not np.isfinite(d).all() or np.any(d <= 0):
            raise ValueError("at least four finite positive masked corrected TOP readings required")
        if not input_sha256:
            raise ValueError("scale derivation requires input SHA256 pins")
        # Median is scale-equivariant; it is a declared data-derived MODEL_CHOICE,
        # not a geological threshold or a tuning step against generated outcomes.
        ordered = np.sort(d)
        mid = len(d) // 2
        s = float(ordered[mid] if len(d) % 2 else ordered[mid - 1] + (ordered[mid] - ordered[mid - 1]) / 2)
        return cls(SQUAREPLUS_LAW, s, MEDIAN_SCALE_RULE, dict(input_sha256), len(d))

    def encode_top(self, corrected_depth_m):
        if self.law_id == LEGACY_LAW:
            return np.log(np.asarray(corrected_depth_m, dtype=float))
        return inverse_squareplus(corrected_depth_m, self.top_scale_m)

    def decode_top(self, latent):
        if self.law_id == LEGACY_LAW:
            return np.exp(latent)
        return squareplus(latent, self.top_scale_m)

    def describe(self):
        return {"law_id": self.law_id, "status": "MODEL_CHOICE", "top_scale_m": self.top_scale_m,
                "scale_rule": self.scale_rule, "scale_input_sha256": dict(self.scale_input_sha256),
                "top_support_count": self.top_support_count, "top_latent_units": "dimensionless",
                "depth_units": "m", "interval_law": "H=max(Y,0); Y in metres; absence is Y<=0",
                "physical_upper_bound_m": None,
                "second_moment_condition": "proper latent posterior with finite second moment; "
                    "at least4 nondegenerate exact readings, SPD covariance; positive probability of censor event",
                "field_validation": False, "censoring_convergence": "NOT_ESTABLISHED_BY_LINK"}

    def generation_identity(self, *, seed, batch, parameter_chunk, censoring_sweeps,
                            input_sha256, module_sha256, rng, precision):
        """Metadata required in addition to law/scale; seed alone is not a world ID."""
        if not input_sha256 or not re.fullmatch(r"[0-9a-fA-F]{64}", str(module_sha256)):
            raise ValueError("generation input and module SHA256 pins required")
        for name, digest in input_sha256.items():
            if not name or not re.fullmatch(r"[0-9a-fA-F]{64}", str(digest)):
                raise ValueError("named generation input SHA256 pins required")
        if seed is None or batch < 1 or parameter_chunk < 1 or censoring_sweeps < 0 or not rng or not precision:
            raise ValueError("explicit batch/chunk/sweeps/RNG/precision required")
        return {"positive_field_law": self.describe(), "seed": seed, "realization_batch": batch,
                "parameter_chunk": parameter_chunk, "censoring_sweeps": censoring_sweeps,
                "input_sha256": dict(input_sha256), "module_sha256": module_sha256,
                "law_module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "rng": rng, "precision": precision,
                "world_id_scope": "row ID is local to these settings and hashed outputs; seed alone is insufficient"}
