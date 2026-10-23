"""World-0 microkernels (theory v0.2: FW §7, §27; AG; MS; design ``docs/worldspec/WORLD_KERNELS_DESIGN_RU.md`` §4).

Scope of this package (decision of the user, 23.10.2026): the kernels themselves and their PAIRWISE interactions —
types, local laws, ports, per-kernel checks, influence coefficients K_ij and the surface operator G from
high-fidelity runs. Assembling a whole-world graph and any neural network are deliberately NOT here.

Modules:
* ``ports``      — typed ports (mechanical, hydraulic, thermal, contact) and port power;
* ``laws``       — named local laws with status (KNOWN_PHYSICS / DERIVATION / PHYSICAL_HYPOTHESIS / ...);
* ``catalogue``  — kernel types of SKRU-1 (state, memory, parameters, ports, laws, split triggers);
* ``envelope``   — microkernel instance envelope K_i = {G, Θ, x, m, P, B, U} with schema validation;
* ``kernels``    — executable reduced kernels (pillar, backfill, mined cell, confined salt layer, aquifer, VZT, surface);
* ``closure``    — external equivalence, state closure test, dissipation check;
* ``influence``  — K = C⁻¹, G = S·C⁻¹, sign structure (G5, RC-T1), Knothe comparator;
* ``gravity``    — body force (causal) vs potential field (probe), never mixed (FW §16);
* ``mapdl_inputs`` — APDL generators of the identification runs (solver as adapter).
"""
from .catalogue import KERNEL_TYPES, catalogue_problems  # noqa: F401
from .laws import LAW_SPECS  # noqa: F401
