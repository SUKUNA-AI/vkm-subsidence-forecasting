"""World-0 microkernels (theory v0.2: FW §7, §27; AG; MS; design ``docs/worldspec/WORLD_KERNELS_DESIGN_RU.md`` §4).

This package contains typed reduced subsystems, their ports and local laws,
coupled state and memory evolution, and explicitly qualified representations.
The former pairwise-only scope is historical. Coupled scalar graphs and
constitutive histories have separate physical assumptions and validation
limits; their execution does not establish full tensor or field accuracy.
Neural-network training and forecasting benchmarks are outside this scope.

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
* ``constitutive_history`` — coupled tensor histories with accepted/trial memory and events;
* ``dynamic_schur`` — dynamic condensation with its own retained memory;
* ``scalar_graph`` / ``scalar_graph_torch`` — fixed scalar worlds and Galerkin representations;
* ``abel_energy`` / ``abel_energy_history`` — scoped hereditary-energy checks;
* ``tl_core`` / ``finite_poromechanics`` — explicit finite-deformation hypotheses and local checks.
"""
from .catalogue import KERNEL_TYPES, catalogue_problems  # noqa: F401
from .laws import LAW_SPECS  # noqa: F401
