"""Gravity in its two roles, kept apart (theory v0.2, FW §15–16).

1. ``BODY_FORCE`` — mechanical body force ρg. CAUSAL: it loads the kernels and sets the initial vertical stress
   σ_v(z) = ∫ρ g dz. Only this role may enter the mechanics of a kernel.
2. ``POTENTIAL_FIELD`` — Newtonian potential, ∇²Φ = 4πGρ. PROBE: an observation operator (gravimetry over
   voids, pillars and backfill). It never drives the mechanics; it reads the density state of a trajectory.

The two roles are separate relation types (``GRAVITY_RELATIONS``) and separate functions in this module.
Coordinates of the potential-field operators: x horizontal, z positive DOWNWARD; g_z positive downward
(attraction of excess mass below the station). Units SI; 1 mGal = 1e-5 m/s², 1 μGal = 1e-8 m/s².
"""
from __future__ import annotations

import math
from enum import Enum

import numpy as np

from .laws import G_NEWTON, G_STD

MGAL = 1.0e-5
UGAL = 1.0e-8
FREE_AIR_GRADIENT = 3.086e-6   # normal free-air gradient of g, 0.3086 mGal/m [1/s²]


class GravityRole(str, Enum):
    BODY_FORCE = "BODY_FORCE"            # causal: ρg in the momentum balance
    POTENTIAL_FIELD = "POTENTIAL_FIELD"  # probe: Δg observed by a gravimeter


GRAVITY_RELATIONS = {
    "BODY_FORCE_LOADS": {"role": GravityRole.BODY_FORCE, "from": "density field ρ(r) and g",
                         "to": "momentum balance of every mechanical kernel; initial σ_v", "causal": True},
    "POTENTIAL_FIELD_OBSERVES": {"role": GravityRole.POTENTIAL_FIELD, "from": "density change Δρ(r,t), station position",
                                 "to": "ObservationWorld (gravimetry); never back to mechanics", "causal": False},
}


# ------------------------------------------------------------------------------- role 1: body force (causal)
def lithostatic_profile(thicknesses, densities, g: float = G_STD):
    """Vertical stress at the base of each layer of a column, σ_v = Σ ρ_i g h_i [Pa] (compression +).

    Returns (depth of each base [m], σ_v at each base [Pa]). Role BODY_FORCE.
    """
    h = np.asarray(thicknesses, float)
    rho = np.asarray(densities, float)
    if h.shape != rho.shape or np.any(h < 0) or np.any(rho <= 0):
        raise ValueError("thicknesses and densities must be same-length, non-negative and positive")
    return np.cumsum(h), np.cumsum(rho * g * h)


# ------------------------------------------------------------------------- role 2: potential field (probe)
def talwani_gz(x_obs, z_obs, vertices_xz, drho: float):
    """Vertical attraction of an infinite 2D body with polygonal cross-section (Talwani et al. 1959).

    ``vertices_xz`` — polygon (x, z) with z positive down, any orientation (normalised internally);
    ``drho`` — density contrast [kg/m³]. Returns g_z [m/s²] at stations (x_obs, z_obs). Role POTENTIAL_FIELD.
    Formula in the line-integral form g_z = 2GΔρ ∮ z dθ, evaluated edge by edge.
    """
    xo = np.atleast_1d(np.asarray(x_obs, float))
    zo = np.broadcast_to(np.asarray(z_obs, float), xo.shape)
    v = np.asarray(vertices_xz, float)
    if v.ndim != 2 or v.shape[1] != 2 or len(v) < 3:
        raise ValueError("polygon needs at least three (x, z) vertices")
    area2 = np.sum(v[:, 0] * np.roll(v[:, 1], -1) - np.roll(v[:, 0], -1) * v[:, 1])
    if area2 < 0:            # make the traversal counter-clockwise in the (x, z) frame
        v = v[::-1]
    out = np.zeros_like(xo)
    for k, (x0, z0) in enumerate(zip(xo, zo)):
        tot = 0.0
        for i in range(len(v)):
            x1, z1 = v[i, 0] - x0, v[i, 1] - z0
            x2, z2 = v[(i + 1) % len(v), 0] - x0, v[(i + 1) % len(v), 1] - z0
            tot += _edge_z_dtheta(x1, z1, x2, z2)
        out[k] = 2.0 * G_NEWTON * drho * tot
    return out


def _edge_z_dtheta(x1, z1, x2, z2) -> float:
    """∫ z dθ along the straight edge (x1,z1)→(x2,z2) seen from the origin, θ = atan2(z, x)."""
    dx, dz = x2 - x1, z2 - z1
    L2 = dx * dx + dz * dz
    if L2 == 0.0:
        return 0.0
    # p(s) = p1 + s·d, dθ/ds = cross/r², cross = x1·z2 − x2·z1; integrating z(s)·cross/r(s)² over s ∈ [0, 1]
    # gives ∫ z dθ = (cross/L²)·(dz·ln(r2/r1) − dx·Δθ)
    cross = x1 * z2 - x2 * z1
    r1 = math.hypot(x1, z1)
    r2 = math.hypot(x2, z2)
    if r1 == 0.0 or r2 == 0.0:
        return 0.0
    dth = math.atan2(z2, x2) - math.atan2(z1, x1)
    if dth > math.pi:
        dth -= 2 * math.pi
    elif dth < -math.pi:
        dth += 2 * math.pi
    return cross / L2 * (dz * math.log(r2 / r1) - dx * dth)


def prism_gz(x_obs, y_obs, z_obs, bounds, drho: float):
    """Vertical attraction of a right rectangular prism (Nagy 1966; Plouff 1976 closed form).

    ``bounds`` = (x1, x2, y1, y2, z1, z2), z positive down. Returns g_z [m/s²]. Role POTENTIAL_FIELD.
    """
    xo = np.atleast_1d(np.asarray(x_obs, float))
    yo = np.broadcast_to(np.asarray(y_obs, float), xo.shape)
    zo = np.broadcast_to(np.asarray(z_obs, float), xo.shape)
    x1, x2, y1, y2, z1, z2 = bounds
    out = np.zeros_like(xo)
    for k in range(xo.size):
        tot = 0.0
        for i, X in enumerate((x1 - xo[k], x2 - xo[k])):
            for j, Y in enumerate((y1 - yo[k], y2 - yo[k])):
                for m, Z in enumerate((z1 - zo[k], z2 - zo[k])):
                    sgn = (-1) ** (i + j + m)
                    r = math.sqrt(X * X + Y * Y + Z * Z)
                    t = 0.0
                    if r > 0:
                        if Y + r > 0:
                            t += X * math.log(Y + r)
                        if X + r > 0:
                            t += Y * math.log(X + r)
                        if Z != 0:
                            t -= Z * math.atan(X * Y / (Z * r))
                    tot += sgn * t
        out[k] = G_NEWTON * drho * tot
    return out


def point_mass_gz(x_obs, mass: float, depth: float):
    """g_z of a point mass at depth below the station line (check of the prism formula)."""
    x = np.asarray(x_obs, float)
    return G_NEWTON * mass * depth / (x * x + depth * depth) ** 1.5


def horizontal_cylinder_gz(x_obs, radius: float, depth: float, drho: float):
    """g_z of an infinite horizontal circular cylinder (check of the Talwani formula)."""
    x = np.asarray(x_obs, float)
    return 2.0 * math.pi * G_NEWTON * drho * radius ** 2 * depth / (x * x + depth * depth)


def free_air_change(subsidence: float | np.ndarray):
    """Gravity change at a station lowered by ``subsidence`` metres (positive down): +0.3086 mGal/m."""
    return FREE_AIR_GRADIENT * np.asarray(subsidence, float)


def bouguer_slab_gz(drho: float, thickness: float) -> float:
    """Infinite horizontal slab, Δg = 2πGΔρt."""
    return 2.0 * math.pi * G_NEWTON * drho * thickness
