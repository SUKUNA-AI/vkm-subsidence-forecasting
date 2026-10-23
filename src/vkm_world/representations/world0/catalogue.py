"""Catalogue of microkernel TYPES of the World-0 representation of SKRU-1 (theory v0.2, AG §3–15, §29–37; MS §6, §13–24).

A kernel type is a template of a reduced subsystem: which physical entities it represents and how
(REPRESENTS / REPRESENTS_PART_OF / AGGREGATES, CT §21), which reduced state x and memory z it carries under which
physics family, which parameters Θ it needs, which typed ports it exposes and which laws close it.

Rules applied (and checked by ``catalogue_problems``):

* state variables exist only under an active physics family (MS §46): damage, pore pressure, temperature are not
  universal variables;
* a quantity is either state, memory, parameter or derived — never two of them (FW §9, MS §25);
* the partition is a REPRESENTATION_CHOICE (AG §1): a type is not a claim that nature consists of kernels;
* hard split triggers (AG §9) are written per type, so that a coarse merge is only allowed after an external
  equivalence test (AG §16, §31).

Parameter evidence: ``evidence`` lists ids of PRIVATE canonical rows (``11_evidence_vnext/canonical/...``), public-safe
copies under ``evidence/``; an empty tuple means the slot is UNKNOWN for SKRU-1 (a scenario range must be labelled).
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass, field

from .laws import LAW_SPECS
from .ports import Port, PortKind

FAMILIES = {
    "ELASTIC": "linear elasticity (always active for solid kernels)",
    "CREEP_STEADY": "Norton steady creep of salt rocks (W0-L-02)",
    "CREEP_HEREDITARY": "hereditary/primary creep with memory modes (W0-L-04, W0-L-04a)",
    "DAMAGE": "creep damage / progressive pillar failure (W0-L-07)",
    "STRENGTH_CHECK": "strength criterion as a state-dependent check, no evolution (W0-L-05, W0-L-06)",
    "BACKFILL_COMPACTION": "compaction of granular backfill with contact gap (W0-L-08)",
    "HYDRO": "pore pressure and Darcy flow (W0-L-09, W0-L-10)",
    "THERMAL_ACTIVE": "temperature as a dynamic state (W0-L-11); default: prescribed field",
}

RELATIONS = ("REPRESENTS", "REPRESENTS_PART_OF", "AGGREGATES")


@dataclass(frozen=True)
class Var:
    """A reduced variable of a kernel type. ``kind`` ∈ STATE (quasi-static generalised coordinate), MEMORY
    (internal variable carrying history), DERIVED (computed from state + parameters, never stored)."""

    symbol: str
    unit: str
    meaning: str
    kind: str
    family: str = "ELASTIC"
    bounds: tuple[float | None, float | None] = (None, None)


@dataclass(frozen=True)
class ParamSlot:
    symbol: str
    unit: str
    meaning: str
    scale: str                  # MASSIF | EFFECTIVE | GEOMETRY | REPRESENTATION (identified from a finer representation)
    family: str = "ELASTIC"
    evidence: tuple[str, ...] = ()
    default_status: str = "UNKNOWN"   # status when no evidence row is linked: UNKNOWN | SCENARIO | MODEL_CHOICE | DERIVATION


@dataclass(frozen=True)
class KernelType:
    type_id: str
    name_ru: str
    represents: tuple[str, ...]
    relation: str
    phase: str
    material: str
    families: tuple[str, ...]
    variables: tuple[Var, ...]
    parameters: tuple[ParamSlot, ...]
    ports: tuple[Port, ...]
    laws: tuple[str, ...]
    split_triggers: tuple[str, ...]
    notes: str = ""
    stratigraphy: tuple[str, ...] = field(default=())

    def active_variables(self, families, kind: str | None = None) -> tuple[Var, ...]:
        fam = set(families) | {"ELASTIC"}
        return tuple(v for v in self.variables if v.family in fam and (kind is None or v.kind == kind))


M = PortKind.MECHANICAL
H = PortKind.HYDRAULIC
T = PortKind.THERMAL
C = PortKind.CONTACT

_SOLID_LAYER_PARAMS = (
    ParamSlot("E", "Pa", "Young's modulus of the massif (LAB→MASSIF only via Transfer/scenario)", "MASSIF"),
    ParamSlot("nu", "1", "Poisson's ratio", "MASSIF"),
    ParamSlot("rho", "kg/m3", "bulk density (body force, role BODY_FORCE; and Δρ for the gravity probe)", "MASSIF"),
    ParamSlot("h", "m", "layer thickness at the kernel (from the geometry realisation)", "GEOMETRY",
              default_status="DERIVATION"),
)
_SALT_CREEP = (
    ParamSlot("eta_s", "Pa*s", "Norton viscosity, scenario set FIELD/LAB (Transfer TR-CREEP-ERMASHOV2015)", "EFFECTIVE",
              family="CREEP_STEADY", default_status="SCENARIO"),
    ParamSlot("n", "1", "Norton exponent (n = 5, VKM-SRC-045)", "EFFECTIVE", family="CREEP_STEADY",
              default_status="SCENARIO"),
    ParamSlot("Q", "J/mol", "creep activation energy for the Arrhenius factor", "MASSIF", family="THERMAL_ACTIVE"),
)
_LAYER_VARS = (
    Var("q_v", "1", "mean vertical strain of the layer segment (generalised coordinate)", "STATE"),
    Var("q_b", "1/m", "mean curvature (bending mode) of the layer segment", "STATE"),
    Var("q_dev", "Pa", "deviator σ_v − σ_h; relaxes under Norton creep (W0-L-12)", "MEMORY", family="CREEP_STEADY"),
    Var("eps_cr", "1", "mean equivalent creep strain", "MEMORY", family="CREEP_STEADY", bounds=(0.0, None)),
    Var("sigma_v0", "Pa", "initial vertical stress σ_v0 = ∫ρg dz (role BODY_FORCE)", "DERIVED"),
)
_LAYER_PORTS = (
    Port("top", M, "upper interface"), Port("bottom", M, "lower interface"),
    Port("lateral", M, "neighbouring segments of the same layer (shear transfer, bending)"),
    Port("top_contact", C, "upper interface: bonded | Coulomb slip (clay interlayers)", active=False,
         note="activated by the layer-interface hypothesis"),
    Port("bottom_contact", C, "lower interface", active=False),
    Port("thermal", T, "geotherm; prescribed unless THERMAL_ACTIVE", active=False),
)


def _salt_layer(type_id, name, strat, notes):
    return KernelType(
        type_id=type_id, name_ru=name, represents=("LAYER",), relation="REPRESENTS_PART_OF",
        phase="SOLID", material="rock salt / potash salt rocks", stratigraphy=strat,
        families=("ELASTIC", "CREEP_STEADY", "CREEP_HEREDITARY", "STRENGTH_CHECK", "THERMAL_ACTIVE"),
        variables=_LAYER_VARS + (
            Var("z_k", "1", "hereditary creep modes (Prony), count = representation choice", "MEMORY",
                family="CREEP_HEREDITARY"),),
        parameters=_SOLID_LAYER_PARAMS + _SALT_CREEP + (
            ParamSlot("c", "Pa", "cohesion (Mohr–Coulomb)", "MASSIF", family="STRENGTH_CHECK"),
            ParamSlot("phi", "deg", "friction angle (Mohr–Coulomb)", "MASSIF", family="STRENGTH_CHECK"),
            ParamSlot("sigma_t", "Pa", "tensile strength", "MASSIF", family="STRENGTH_CHECK"),
        ),
        ports=_LAYER_PORTS + (Port("hydraulic", H, "q_n = 0: salt is a hydraulic barrier (part of MK-VZT)",
                                   active=False),),
        laws=("W0-L-01", "W0-L-16", "W0-L-02", "W0-L-12", "W0-L-04", "W0-L-04a", "W0-L-05", "W0-L-03"),
        split_triggers=("material/constitutive: salt (creeping) vs marl/clay (elastic-brittle)",
                        "strong gradient: above the panel edge (bending, shear) vs far field",
                        "regime: intact vs fractured (VZT integrity)",
                        "task: layer belongs to the water-protective strata (hydraulic graph)"),
        notes=notes)


KERNEL_TYPES: dict[str, KernelType] = {}


def _reg(kt: KernelType) -> KernelType:
    if kt.type_id in KERNEL_TYPES:
        raise ValueError(kt.type_id)
    KERNEL_TYPES[kt.type_id] = kt
    return kt


# ------------------------------------------------------------------------------------------ mined layer
_reg(KernelType(
    type_id="MK-CHAMBER-VOID", name_ru="камера (пустая выработка)", represents=("CHAMBER",), relation="REPRESENTS",
    phase="VOID", material="air (dry void)", families=("ELASTIC", "HYDRO"),
    variables=(
        Var("v_c", "m", "mean roof–floor convergence of the chamber", "STATE", bounds=(0.0, None)),
        Var("V", "m3/m", "void volume per unit chamber length, b·(h − v_c)", "DERIVED"),
        Var("p_f", "Pa", "fluid pressure in a flooded chamber (then the kernel becomes FLUID)", "STATE", family="HYDRO"),
        Var("m_f", "kg/m", "fluid mass per unit length (flooded chamber)", "STATE", family="HYDRO"),
    ),
    parameters=(ParamSlot("b", "m", "chamber width", "GEOMETRY", evidence=(), default_status="DERIVATION"),
                ParamSlot("h_c", "m", "chamber height (= mined thickness)", "GEOMETRY", default_status="DERIVATION"),
                ParamSlot("L_c", "m", "chamber length (plane strain if ≫ b)", "GEOMETRY", default_status="DERIVATION")),
    ports=(Port("roof", M, "traction-free face (t = 0)"), Port("floor", M, "traction-free face (t = 0)"),
           Port("walls", M, "faces of the adjacent pillars (t = 0)"),
           Port("closure", C, "roof meets floor or backfill when v_c → h_c or the gap closes", active=False),
           Port("water", H, "inflow / flooding", active=False)),
    laws=(),
    split_triggers=("phase: void vs solid (always a separate fine kernel)", "history: mining date t_m",
                    "event: backfilling (→ MK-BACKFILL), flooding (→ fluid state)"),
    notes="no constitutive law: a void is a boundary; its convergence is set by pillars, roof and floor. In a coarse "
          "representation the chamber is hidden inside MK-MINED-CELL (AG §29, §31)."))

_reg(KernelType(
    type_id="MK-PILLAR-IC", name_ru="междукамерный целик", represents=("PILLAR",), relation="REPRESENTS",
    phase="SOLID", material="sylvinite (seam КрII; АБ / В in other seams)",
    families=("ELASTIC", "CREEP_STEADY", "CREEP_HEREDITARY", "DAMAGE", "STRENGTH_CHECK", "THERMAL_ACTIVE"),
    variables=(
        Var("v_p", "m", "mean vertical shortening of the pillar", "STATE", bounds=(0.0, None)),
        Var("eps_cr", "1", "mean axial creep strain of the pillar", "MEMORY", family="CREEP_STEADY", bounds=(0.0, None)),
        Var("z_k", "1", "primary/hereditary creep modes", "MEMORY", family="CREEP_HEREDITARY"),
        Var("omega", "1", "scalar damage", "MEMORY", family="DAMAGE", bounds=(0.0, 1.0)),
        Var("w_eff", "m", "effective load-bearing width (spalling of the pillar walls)", "STATE", family="DAMAGE"),
        Var("sigma_p", "Pa", "mean vertical pillar stress (port traction / (1 − k))", "DERIVED"),
        Var("C", "1", "loading degree σ_p/σ_пр", "DERIVED", family="STRENGTH_CHECK"),
    ),
    parameters=(
        ParamSlot("w", "m", "pillar width", "GEOMETRY", default_status="DERIVATION"),
        ParamSlot("h_p", "m", "pillar height (= mined thickness)", "GEOMETRY", default_status="DERIVATION"),
        ParamSlot("E", "Pa", "Young's modulus of the pillar rock", "MASSIF"),
        ParamSlot("nu", "1", "Poisson's ratio", "MASSIF"),
        ParamSlot("rho", "kg/m3", "density", "MASSIF"),
        ParamSlot("eta_s", "Pa*s", "Norton viscosity (FIELD: SKRU-1 pillars 12 ЗП КрII; LAB)", "EFFECTIVE",
                  family="CREEP_STEADY", default_status="SCENARIO"),
        ParamSlot("n", "1", "Norton exponent", "EFFECTIVE", family="CREEP_STEADY", default_status="SCENARIO"),
        ParamSlot("c_shape", "1", "creep shape factor of the reduced pillar law (identified from fine FE)",
                  "REPRESENTATION", family="CREEP_STEADY", default_status="DERIVATION"),
        ParamSlot("sigma_pr", "Pa", "pillar strength (shape effect, clay interlayers)", "MASSIF",
                  family="STRENGTH_CHECK"),
        ParamSlot("K_dl", "1", "long-term strength ratio σ∞/σ_пр", "MASSIF", family="STRENGTH_CHECK"),
        ParamSlot("L_omega", "1/(s*MPa)", "damage rate L_ω of W0-L-07b (LAB; creep vs relaxation fits differ ≈87×)",
                  "EFFECTIVE", family="DAMAGE", evidence=("RA-012", "RA-013", "RA-018", "RA-022")),
        ParamSlot("p_star", "Pa", "damage threshold p* of W0-L-07b (LAB 3.85 MPa)", "EFFECTIVE", family="DAMAGE",
                  evidence=("RA-010", "RA-019")),
        ParamSlot("sigma_u_star", "Pa", "σ_u* of W0-L-07b (LAB 5.5 / 5.9 MPa)", "EFFECTIVE", family="DAMAGE",
                  evidence=("RA-011", "RA-020")),
        ParamSlot("B_dmg", "1/s", "damage rate constant of the alternative Kachanov law (UNKNOWN)", "EFFECTIVE",
                  family="DAMAGE"),
        ParamSlot("m_dmg", "1", "damage stress exponent of the alternative Kachanov law (UNKNOWN)", "EFFECTIVE",
                  family="DAMAGE"),
    ),
    ports=(Port("top", M, "pillar–roof contact face"), Port("bottom", M, "pillar–floor contact face"),
           Port("top_contact", C, "friction / clay interlayer at the roof contact", active=False),
           Port("bottom_contact", C, "friction / clay interlayer at the floor contact", active=False),
           Port("walls", M, "free faces to chambers (t = 0) or confinement by backfill"),
           Port("thermal", T, "prescribed geotherm, Arrhenius factor", active=False)),
    laws=("W0-L-01", "W0-L-16", "W0-L-02", "W0-L-02a", "W0-L-06", "W0-L-05", "W0-L-07b", "W0-L-07", "W0-L-04",
          "W0-L-03"),
    split_triggers=("constitutive: creeping pillar vs elastic roof/void", "regime: core vs damaged edges at C → 1",
                    "history: extraction epoch of the adjacent chambers", "backfill contact on the walls"),
    notes="fine option: core + damaged edges + contacts (AG §30); the reduced law uses c_shape identified from FE"))

_reg(KernelType(
    type_id="MK-PILLAR-BARRIER", name_ru="ленточный / барьерный (межпанельный, охранный) целик",
    represents=("PILLAR",), relation="REPRESENTS", phase="SOLID", material="sylvinite / salt",
    families=("ELASTIC", "CREEP_STEADY", "DAMAGE", "STRENGTH_CHECK"),
    variables=(
        Var("v_p", "m", "mean vertical shortening", "STATE", bounds=(0.0, None)),
        Var("eps_cr", "1", "mean creep strain", "MEMORY", family="CREEP_STEADY", bounds=(0.0, None)),
        Var("y_w", "m", "width of the yielded edge zone", "STATE", family="DAMAGE", bounds=(0.0, None)),
        Var("sigma_p", "Pa", "mean vertical stress", "DERIVED"),
    ),
    parameters=(
        ParamSlot("w", "m", "barrier width (w/h ≫ 1)", "GEOMETRY", default_status="DERIVATION"),
        ParamSlot("h_p", "m", "height", "GEOMETRY", default_status="DERIVATION"),
        ParamSlot("E", "Pa", "Young's modulus", "MASSIF"), ParamSlot("nu", "1", "Poisson's ratio", "MASSIF"),
        ParamSlot("eta_s", "Pa*s", "Norton viscosity", "EFFECTIVE", family="CREEP_STEADY", default_status="SCENARIO"),
        ParamSlot("n", "1", "Norton exponent", "EFFECTIVE", family="CREEP_STEADY", default_status="SCENARIO"),
        ParamSlot("c_shape", "1", "creep shape factor (confined core: ≪ 1)", "REPRESENTATION", family="CREEP_STEADY",
                  default_status="DERIVATION"),
    ),
    ports=(Port("top", M, "roof contact"), Port("bottom", M, "floor contact"),
           Port("walls", M, "free or confined faces")),
    laws=("W0-L-01", "W0-L-02", "W0-L-05", "W0-L-07"),
    split_triggers=("regime: confined core (w/h ≫ 1) vs slender inter-chamber pillar — different c_shape",
                    "task: barrier separates panels → boundary of the coarse cells"),
    notes="same material laws as MK-PILLAR-IC; a different TYPE because the reduced law differs (confinement)"))

_reg(KernelType(
    type_id="MK-BACKFILL", name_ru="закладка камеры", represents=("BACKFILL",), relation="REPRESENTS",
    phase="GRANULAR (+ brine for hydraulic fill)", material="halite tailings backfill",
    families=("ELASTIC", "BACKFILL_COMPACTION", "HYDRO"),
    variables=(
        Var("eps_b", "1", "compaction strain of the fill", "STATE", family="BACKFILL_COMPACTION", bounds=(0.0, None)),
        Var("delta_n", "m", "gap between fill top and roof (contact opening)", "STATE", family="BACKFILL_COMPACTION",
            bounds=(0.0, None)),
        Var("eps_b_max", "1", "largest past compaction (unloading branch)", "MEMORY", family="BACKFILL_COMPACTION"),
        Var("p_b", "Pa", "pore (brine) pressure in hydraulic fill", "STATE", family="HYDRO"),
        Var("age", "s", "age a = t − t_fill", "DERIVED"),
        Var("sigma_b", "Pa", "vertical fill stress", "DERIVED"),
    ),
    parameters=(
        ParamSlot("k_fill", "1", "filling coefficient (k_зап) = fill height / chamber height", "GEOMETRY"),
        ParamSlot("eps_k", "1", "limiting volumetric compaction ε_k of W0-L-08b (VKM form; UNKNOWN)", "MASSIF",
                  family="BACKFILL_COMPACTION"),
        ParamSlot("sigma_k", "Pa", "compaction stress scale σ_k of W0-L-08b (UNKNOWN)", "MASSIF",
                  family="BACKFILL_COMPACTION"),
        ParamSlot("G_star", "Pa", "shear modulus of the fill in W0-L-08b (0–82.5 MPa, VKM-SRC-044 рис. 105)",
                  "EFFECTIVE", family="BACKFILL_COMPACTION"),
        ParamSlot("E_age", "Pa", "deformation modulus vs age (0.02→0.19 GPa over 1–29 yr, VKM-SRC-266; conflict "
                  "C-09)", "MASSIF", family="BACKFILL_COMPACTION"),
        ParamSlot("E0", "Pa", "initial modulus of the alternative hyperbolic law W0-L-08", "MASSIF",
                  family="BACKFILL_COMPACTION"),
        ParamSlot("eps_m", "1", "limiting strain of W0-L-08", "MASSIF", family="BACKFILL_COMPACTION"),
        ParamSlot("E_u", "Pa", "unloading modulus", "MASSIF", family="BACKFILL_COMPACTION"),
        ParamSlot("rho_b", "kg/m3", "fill density (body force; Δρ for gravity probe)", "MASSIF"),
        ParamSlot("K_b", "m/s", "hydraulic conductivity of the fill", "MASSIF", family="HYDRO"),
        ParamSlot("t_fill", "s", "backfilling date (history)", "GEOMETRY"),
    ),
    ports=(Port("top_contact", C, "unilateral contact with the roof: δn ≥ 0, p_c ≥ 0, δn·p_c = 0"),
           Port("walls", M, "lateral confinement of the pillar walls"),
           Port("floor", M, "floor"), Port("drainage", H, "brine drainage of hydraulic fill", active=False)),
    laws=("W0-L-08b", "W0-L-08", "W0-L-10", "W0-L-16"),
    split_triggers=("phase: granular fill vs salt (always separate in a fine representation, AG §34)",
                    "history: backfill age", "regime: open gap vs loaded fill"),
    notes="the fill carries load only after the gap closes; its age/maturity is derived, not a free parameter"))

_reg(KernelType(
    type_id="MK-MINED-CELL", name_ru="эффективный элемент отработанного пласта (целик + камера [+ закладка])",
    represents=("PILLAR", "CHAMBER", "BACKFILL"), relation="AGGREGATES", phase="MIXED",
    material="effective mined layer", families=("ELASTIC", "CREEP_STEADY", "BACKFILL_COMPACTION", "DAMAGE"),
    variables=(
        Var("v", "m", "mean convergence of the cell (World-0 v_i)", "STATE", bounds=(0.0, None)),
        Var("eps_cr", "1", "creep strain of the hidden pillar", "MEMORY", family="CREEP_STEADY"),
        Var("eps_b_max", "1", "largest past fill compaction", "MEMORY", family="BACKFILL_COMPACTION"),
        Var("omega", "1", "damage of the hidden pillar", "MEMORY", family="DAMAGE"),
        Var("sigma_cell", "Pa", "mean vertical traction on the cell = (1−k)σ_p + kσ_b", "DERIVED"),
    ),
    parameters=(
        ParamSlot("a", "m", "cell width (pillar + chamber spacing)", "GEOMETRY", default_status="DERIVATION"),
        ParamSlot("k", "1", "extraction ratio of the cell", "GEOMETRY"),
        ParamSlot("h", "m", "mined thickness", "GEOMETRY", default_status="DERIVATION"),
        ParamSlot("pillar_params", "-", "parameters of MK-PILLAR-IC", "MASSIF"),
        ParamSlot("fill_params", "-", "parameters of MK-BACKFILL", "MASSIF", family="BACKFILL_COMPACTION"),
    ),
    ports=(Port("roof", M, "mean roof traction / displacement of the cell"),
           Port("floor", M, "mean floor traction / displacement")),
    laws=("W0-L-06", "W0-L-02", "W0-L-02a", "W0-L-08b", "W0-L-08"),
    split_triggers=("external equivalence fails (E_ext > ε) against the fine pillar/chamber/fill kernels",
                    "different extraction history or backfill regime inside the cell"),
    notes="World-0 candidate law v̇ = h·A·φ(σ − P(v, z)) is a RESEARCH_CANDIDATE; the merge is admissible only after "
          "the external-equivalence test (AG §16, §31)"))

# ------------------------------------------------------------------------------------------ salt layers
_reg(_salt_layer("MK-LAYER-PKS", "покровная каменная соль (ПКС)", ("ПКС",),
                 "upper rock salt; part of the water-protective strata (hydraulic barrier)"))
_reg(_salt_layer("MK-LAYER-SKZ", "сильвинито-карналлитовая зона (СКЗ, пласты Б–К)", ("СКЗ",),
                 "carnallite-bearing zone: faster creep and lower strength than sylvinite; becomes a MINED layer "
                 "where seam В is extracted (then MK-MINED-CELL / MK-PILLAR-IC of carnallite)"))
_reg(_salt_layer("MK-LAYER-SZ", "сильвинитовая зона (СЗ: пласты А–КрII и межпластовая соль)", ("СЗ",),
                 "contains the immediate roof (between КрII and the next contact) and floor of the mined seam; "
                 "pillar–roof interaction lives on its contact port"))
_reg(_salt_layer("MK-LAYER-PDKS", "подстилающая каменная соль (ПдКС)", ("ПдКС",),
                 "floor of the mined layer; its compliance adds floor heave to the convergence"))

# --------------------------------------------------------------------------------------- hydraulic barrier
_reg(KernelType(
    type_id="MK-VZT", name_ru="водозащитная толща (ВЗТ) — гидравлический барьер",
    represents=("LAYER",), relation="AGGREGATES", phase="SOLID (+ fluid in fractures)",
    material="salt and clay–marl layers between the upper mined seam and the aquifer",
    stratigraphy=("СКЗ над верхним отрабатываемым пластом (на СКРУ-1 — над В)", "ПКС", "ПП / низ СМТ"),
    families=("HYDRO", "DAMAGE", "STRENGTH_CHECK"),
    variables=(
        Var("d_i", "1", "integrity state of each sublayer (0 intact … 1 hydraulically connected)", "STATE",
            family="DAMAGE", bounds=(0.0, 1.0)),
        Var("dh", "m", "head difference across the barrier (aquifer head − mine level)", "STATE", family="HYDRO"),
        Var("K_eff", "m/s", "effective vertical conductivity (series of sublayers)", "DERIVED", family="HYDRO"),
        Var("q_leak", "m/s", "leakage flux into the workings", "DERIVED", family="HYDRO"),
        Var("margin", "1", "integrity margin of the criterion (strain or strength based)", "DERIVED",
            family="STRENGTH_CHECK"),
    ),
    parameters=(
        ParamSlot("h_i", "m", "sublayer thicknesses; SKRU-1: ВЗТ 100–110 m above the roof of seam В with section types "
                  "by block (VKM-SRC-252, 2013)", "GEOMETRY", family="HYDRO", evidence=("RA-039", "RA-040"),
                  default_status="DERIVATION"),
        ParamSlot("K_intact_i", "m/s", "conductivity of intact sublayers (salt ≈ 0)", "MASSIF", family="HYDRO"),
        ParamSlot("K_frac_i", "m/s", "conductivity after fracturing", "MASSIF", family="HYDRO"),
        ParamSlot("crit", "-", "integrity criterion and its limit (from evidence)", "MASSIF", family="STRENGTH_CHECK"),
    ),
    ports=(Port("aquifer", H, "top: head/pressure of the ТКТ aquifer, flux q_n"),
           Port("workings", H, "bottom: atmospheric pressure in the workings, inflow"),
           Port("deformation", M, "input: strains/curvatures of the mechanical layer kernels it aggregates")),
    laws=("W0-L-15", "W0-L-09", "W0-L-10", "W0-L-05"),
    split_triggers=("multiplex topology: one kernel in the hydraulic graph, several in the mechanical graph",
                    "regime: a connected fracture path changes the hydraulic topology (AG §14)"),
    notes="mechanical adjacency ≠ hydraulic connectivity (FW §11, AG §36): the VZT kernel lives in the hydraulic "
          "layer of the multiplex graph and reads deformation from the mechanical layer kernels"))

_reg(KernelType(
    type_id="MK-LAYER-SMT", name_ru="соляно-мергельная толща (СМТ: мергели, глины с солью; пачки ГГ, ПП)",
    represents=("LAYER",), relation="REPRESENTS_PART_OF", phase="SOLID", material="marl, clay–marl, gypsum–clay",
    stratigraphy=("СМТ", "ГГ", "ПП"), families=("ELASTIC", "STRENGTH_CHECK", "DAMAGE", "HYDRO"),
    variables=(
        Var("q_v", "1", "mean vertical strain", "STATE"), Var("q_b", "1/m", "mean curvature", "STATE"),
        Var("omega", "1", "fracturing/damage", "MEMORY", family="DAMAGE", bounds=(0.0, 1.0)),
        Var("p", "Pa", "pore pressure (only where saturated and permeable)", "STATE", family="HYDRO"),
        Var("sigma_v0", "Pa", "initial vertical stress", "DERIVED"),
    ),
    parameters=_SOLID_LAYER_PARAMS + (
        ParamSlot("c", "Pa", "cohesion", "MASSIF", family="STRENGTH_CHECK"),
        ParamSlot("phi", "deg", "friction angle", "MASSIF", family="STRENGTH_CHECK"),
        ParamSlot("sigma_t", "Pa", "tensile strength", "MASSIF", family="STRENGTH_CHECK"),
        ParamSlot("K", "m/s", "hydraulic conductivity", "MASSIF", family="HYDRO"),
    ),
    ports=_LAYER_PORTS + (Port("hydraulic", H, "low-permeability flux to/from the aquifer", active=False),),
    laws=("W0-L-01", "W0-L-16", "W0-L-05", "W0-L-09", "W0-L-10"),
    split_triggers=("constitutive: elastic-brittle (no salt creep)", "hydraulic: part of the barrier or not",
                    "regime: fractured above the panel edges"),
    notes="properties for SKRU-1 largely UNKNOWN (normative «мергель» values transferred by assumption)"))

_reg(KernelType(
    type_id="MK-LAYER-TKT", name_ru="терригенно-карбонатная толща (ТКТ) — водоносный горизонт",
    represents=("LAYER",), relation="REPRESENTS_PART_OF", phase="POROUS SOLID + WATER",
    material="limestone, marl, sandstone, clay (aquifer)", stratigraphy=("ТКТ",),
    families=("ELASTIC", "HYDRO", "STRENGTH_CHECK"),
    variables=(
        Var("q_v", "1", "mean vertical strain", "STATE"), Var("q_b", "1/m", "mean curvature", "STATE"),
        Var("p_k", "Pa", "pore-pressure modes (consolidation; count = representation choice)", "MEMORY",
            family="HYDRO"),
        Var("p_mean", "Pa", "mean pore pressure (from the modes)", "DERIVED", family="HYDRO"),
        Var("sigma_eff", "Pa", "effective stress σ − αp", "DERIVED", family="HYDRO"),
    ),
    parameters=_SOLID_LAYER_PARAMS + (
        ParamSlot("K", "m/s", "hydraulic conductivity", "MASSIF", family="HYDRO"),
        ParamSlot("S_s", "1/m", "specific storage", "MASSIF", family="HYDRO"),
        ParamSlot("alpha_B", "1", "Biot coefficient", "MASSIF", family="HYDRO"),
        ParamSlot("h0", "m", "initial head of the aquifer", "MASSIF", family="HYDRO"),
    ),
    ports=_LAYER_PORTS + (Port("hydraulic_top", H, "recharge from Q / surface", active=True),
                          Port("hydraulic_bottom", H, "leakage to MK-VZT", active=True)),
    laws=("W0-L-01", "W0-L-16", "W0-L-09", "W0-L-10"),
    split_triggers=("phase: two-phase (solid + water) vs dry salt", "hydraulic compartment boundaries",
                    "regime: drawdown (consolidation active) vs static head"),
    notes="pore pressure is a dynamic state only under the HYDRO family; static head otherwise"))

_reg(KernelType(
    type_id="MK-LAYER-Q", name_ru="четвертичные глины и суглинки (Q)", represents=("LAYER",),
    relation="REPRESENTS_PART_OF", phase="SOIL (unsaturated/saturated)", material="clay, loam",
    stratigraphy=("Q",), families=("ELASTIC", "HYDRO"),
    variables=(Var("q_v", "1", "mean vertical strain", "STATE"),
               Var("p_k", "Pa", "consolidation modes of saturated clay", "MEMORY", family="HYDRO")),
    parameters=_SOLID_LAYER_PARAMS + (ParamSlot("K", "m/s", "hydraulic conductivity", "MASSIF", family="HYDRO"),
                                      ParamSlot("S_s", "1/m", "specific storage", "MASSIF", family="HYDRO")),
    ports=(Port("surface", M, "top = MK-SURFACE (traction-free)"), Port("bottom", M, "to ТКТ"),
           Port("lateral", M, "neighbouring segments"), Port("hydraulic", H, "recharge, seasonal", active=False)),
    laws=("W0-L-01", "W0-L-16", "W0-L-10"),
    split_triggers=("material: soil vs rock", "observation: benchmarks are founded in it (seasonal noise)"),
    notes="properties for SKRU-1 UNKNOWN in the corpus → labelled scenario"))

_reg(KernelType(
    type_id="MK-SURFACE", name_ru="дневная поверхность (граничное ядро)", represents=("SURFACE",),
    relation="REPRESENTS", phase="BOUNDARY", material="-", families=("ELASTIC",),
    variables=(Var("u_s", "m", "surface displacement modes (output of the overburden operators)", "DERIVED"),),
    parameters=(ParamSlot("dsm", "m", "topography (DSM realisation)", "GEOMETRY", default_status="DERIVATION"),),
    ports=(Port("mechanical", M, "traction-free: t = 0; u_s is read by observation operators"),
           Port("hydraulic", H, "atmospheric pressure / recharge", active=False),
           Port("thermal", T, "surface temperature (prescribed)", active=False)),
    laws=(),
    split_triggers=("observation-aware split only (AG §15): benchmarks, GNSS, SAR pixels are ObservationWorld "
                    "entities, not kernels (AG §37)",),
    notes="carries no dynamics of its own: the surface displacement is G·(a·v) of the mined-layer kernels"))


# ------------------------------------------------------------------------------------------------ checks
def catalogue_problems(types: dict[str, KernelType] | None = None) -> list[str]:
    """Structural rules of the catalogue (empty list = OK)."""
    probs: list[str] = []
    for tid, kt in (types or KERNEL_TYPES).items():
        if kt.relation not in RELATIONS:
            probs.append(f"{tid}: relation {kt.relation}")
        for f in kt.families:
            if f not in FAMILIES:
                probs.append(f"{tid}: unknown family {f}")
        syms = [v.symbol for v in kt.variables]
        if len(set(syms)) != len(syms):
            probs.append(f"{tid}: duplicate variable symbols")
        psyms = {p.symbol for p in kt.parameters}
        clash = psyms & set(syms)
        if clash:
            probs.append(f"{tid}: {sorted(clash)} both parameter and state (FW §9)")
        for v in kt.variables:
            if v.kind not in ("STATE", "MEMORY", "DERIVED"):
                probs.append(f"{tid}: {v.symbol} kind {v.kind}")
            if v.family not in kt.families:
                probs.append(f"{tid}: variable {v.symbol} under inactive family {v.family}")
        for p in kt.parameters:
            if p.family not in kt.families:
                probs.append(f"{tid}: parameter {p.symbol} under inactive family {p.family}")
        for law in kt.laws:
            if law not in LAW_SPECS:
                probs.append(f"{tid}: unknown law {law}")
        if kt.phase not in ("VOID", "BOUNDARY") and not kt.active_variables(kt.families, "STATE"):
            probs.append(f"{tid}: a material kernel without any state variable")
    return probs


def catalogue_rows() -> list[dict]:
    """Flat rows (one per kernel type) for CSV export."""
    rows = []
    for kt in KERNEL_TYPES.values():
        rows.append({
            "type_id": kt.type_id, "name_ru": kt.name_ru, "represents": ";".join(kt.represents),
            "relation": kt.relation, "phase": kt.phase, "material": kt.material,
            "stratigraphy": ";".join(kt.stratigraphy), "families": ";".join(kt.families),
            "state": ";".join(f"{v.symbol}[{v.unit}]@{v.family}" for v in kt.variables if v.kind == "STATE"),
            "memory": ";".join(f"{v.symbol}[{v.unit}]@{v.family}" for v in kt.variables if v.kind == "MEMORY"),
            "derived": ";".join(f"{v.symbol}[{v.unit}]" for v in kt.variables if v.kind == "DERIVED"),
            "parameters": ";".join(f"{p.symbol}[{p.unit}|{p.scale}]@{p.family}" for p in kt.parameters),
            "ports": ";".join(f"{p.name}:{p.kind.value}{'' if p.active else '(inactive)'}" for p in kt.ports),
            "laws": ";".join(kt.laws), "split_triggers": " | ".join(kt.split_triggers), "notes": kt.notes,
        })
    return rows


def catalogue_csv() -> str:
    rows = catalogue_rows()
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def catalogue_json() -> str:
    def conv(kt: KernelType):
        d = asdict(kt)
        d["ports"] = [{"name": p.name, "kind": p.kind.value, "face": p.face, "active": p.active, "note": p.note}
                      for p in kt.ports]
        return d
    return json.dumps({"schema": "vkm.world0.kernel_types/1", "types": [conv(k) for k in KERNEL_TYPES.values()],
                       "families": FAMILIES}, ensure_ascii=False, indent=1) + "\n"
