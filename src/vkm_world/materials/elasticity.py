"""Elastic tensor conversions, independent of a mine or a solver deck.

The orthotropic convention below was previously embedded in the PRIVATE OGS
plane-strain generator. It describes a full 3D material compliance and does not
carry that generator's retired one-borehole world or its layer aggregation.
"""
from __future__ import annotations

import numpy as np


def orthotropic_compliance(young, poisson, shear):
    """Return the 3D Kelvin compliance in the OGS orthotropic convention.

    ``young=(E1,E2,E3)``, ``poisson=(nu12,nu23,nu13)`` and
    ``shear=(G12,G23,G13)`` use material axes 1,2,3. Kelvin order is
    (11,22,33,12,23,13), with sqrt(2) shear components. Thus S_ij=-nu_ij/E_i,
    reciprocal nu_ji=nu_ij*E_j/E_i and each shear diagonal is 1/(2*G_ij).

    This is an algebraic conversion, not a material-admission decision: callers
    must check units, provenance, scale and positive definiteness before use.
    In particular an unstable input must remain detectable by that caller.
    """
    e1, e2, e3 = (float(value) for value in young)
    n12, n23, n13 = (float(value) for value in poisson)
    g12, g23, g13 = (float(value) for value in shear)
    compliance = np.zeros((6, 6))
    compliance[:3, :3] = [[1 / e1, -n12 / e1, -n13 / e1],
                          [-n12 / e1, 1 / e2, -n23 / e2],
                          [-n13 / e1, -n23 / e2, 1 / e3]]
    compliance[3, 3], compliance[4, 4], compliance[5, 5] = 1 / (2 * g12), 1 / (2 * g23), 1 / (2 * g13)
    return compliance
