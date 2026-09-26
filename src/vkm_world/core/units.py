"""Unit METADATA for WorldSpec records (no numerical conversion engine).

Sources print units in many forms (``т/м3``, ``тс/м³``, ``кгс/см2``, ``МПа``, ``мм/год`` …).
Phase 1 stores values *as printed* and needs to know, for validation only:

* which physical dimension a printed unit denotes;
* which canonical SI unit it maps to and with what exact factor (for the later local
  computational phase — nothing is converted here);
* which unit strings are ambiguous and must be resolved from context
  (e.g. ``т/м3`` may be a density [t/m³] or a unit weight [tf/m³]).

``check_unit(variable, unit)`` returns a list of problems instead of raising, so catalogues can
record unit doubts in the visual/OCR QA ledger.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Dimension(str, Enum):
    LENGTH = "length"
    AREA = "area"
    VOLUME = "volume"
    TIME = "time"
    MASS = "mass"
    DENSITY = "density"                 # kg/m3
    UNIT_WEIGHT = "unit_weight"         # N/m3
    STRESS = "stress"                   # Pa
    STRAIN = "strain"                   # dimensionless (can be %, mm/m)
    STRAIN_RATE = "strain_rate"         # 1/s
    VELOCITY = "velocity"               # m/s (also subsidence rate mm/yr)
    ANGLE = "angle"
    TEMPERATURE = "temperature"
    PERMEABILITY = "permeability"       # m2
    HYDRAULIC_CONDUCTIVITY = "hydraulic_conductivity"  # m/s
    FREQUENCY = "frequency"
    CONDUCTIVITY_EL = "electrical_conductivity"        # S/m
    DIMENSIONLESS = "dimensionless"
    CREEP_COEFFICIENT = "creep_coefficient"            # law-specific, e.g. 1/(Pa^n s), s^(alpha-1)
    OTHER = "other"


@dataclass(frozen=True)
class UnitInfo:
    canonical: str            # canonical SI unit string
    factor: float | None      # multiply printed value by factor to get canonical (None = not linear / ambiguous)
    dimension: Dimension
    ambiguous_with: tuple[Dimension, ...] = ()
    note: str = ""


def _n(s: str) -> str:
    s = s.strip().lower().replace("³", "3").replace("²", "2").replace(" ", "")
    s = s.replace("ё", "е").replace("^", "")
    return s


_G = 9.80665
# printed (normalised) → UnitInfo. Factors are exact definitions; ambiguity is explicit.
_UNITS: dict[str, UnitInfo] = {
    # length / area / volume
    "м": UnitInfo("m", 1.0, Dimension.LENGTH), "m": UnitInfo("m", 1.0, Dimension.LENGTH),
    "мм": UnitInfo("m", 1e-3, Dimension.LENGTH), "mm": UnitInfo("m", 1e-3, Dimension.LENGTH),
    "см": UnitInfo("m", 1e-2, Dimension.LENGTH), "cm": UnitInfo("m", 1e-2, Dimension.LENGTH),
    "км": UnitInfo("m", 1e3, Dimension.LENGTH), "km": UnitInfo("m", 1e3, Dimension.LENGTH),
    "м2": UnitInfo("m2", 1.0, Dimension.AREA), "m2": UnitInfo("m2", 1.0, Dimension.AREA),
    "км2": UnitInfo("m2", 1e6, Dimension.AREA), "га": UnitInfo("m2", 1e4, Dimension.AREA),
    "м3": UnitInfo("m3", 1.0, Dimension.VOLUME), "m3": UnitInfo("m3", 1.0, Dimension.VOLUME),
    # time
    "с": UnitInfo("s", 1.0, Dimension.TIME), "s": UnitInfo("s", 1.0, Dimension.TIME),
    "ч": UnitInfo("s", 3600.0, Dimension.TIME), "h": UnitInfo("s", 3600.0, Dimension.TIME),
    "сут": UnitInfo("s", 86400.0, Dimension.TIME), "сутки": UnitInfo("s", 86400.0, Dimension.TIME),
    "d": UnitInfo("s", 86400.0, Dimension.TIME),
    "год": UnitInfo("s", 365.25 * 86400.0, Dimension.TIME, note="Julian year"),
    "лет": UnitInfo("s", 365.25 * 86400.0, Dimension.TIME, note="Julian year"),
    "yr": UnitInfo("s", 365.25 * 86400.0, Dimension.TIME, note="Julian year"),
    # mass / density / unit weight
    "т": UnitInfo("kg", 1e3, Dimension.MASS), "кг": UnitInfo("kg", 1.0, Dimension.MASS),
    "кг/м3": UnitInfo("kg/m3", 1.0, Dimension.DENSITY), "kg/m3": UnitInfo("kg/m3", 1.0, Dimension.DENSITY),
    "г/см3": UnitInfo("kg/m3", 1e3, Dimension.DENSITY), "g/cm3": UnitInfo("kg/m3", 1e3, Dimension.DENSITY),
    "т/м3": UnitInfo("kg/m3", 1e3, Dimension.DENSITY, ambiguous_with=(Dimension.UNIT_WEIGHT,),
                     note="Soviet texts often write т/м3 for unit weight in tf/m3 (=9.80665 kN/m3); resolve by context"),
    "тс/м3": UnitInfo("N/m3", 1e3 * _G, Dimension.UNIT_WEIGHT),
    "кн/м3": UnitInfo("N/m3", 1e3, Dimension.UNIT_WEIGHT), "kn/m3": UnitInfo("N/m3", 1e3, Dimension.UNIT_WEIGHT),
    "мн/м3": UnitInfo("N/m3", 1e6, Dimension.UNIT_WEIGHT),
    # stress
    "па": UnitInfo("Pa", 1.0, Dimension.STRESS), "pa": UnitInfo("Pa", 1.0, Dimension.STRESS),
    "кпа": UnitInfo("Pa", 1e3, Dimension.STRESS), "kpa": UnitInfo("Pa", 1e3, Dimension.STRESS),
    "мпа": UnitInfo("Pa", 1e6, Dimension.STRESS), "mpa": UnitInfo("Pa", 1e6, Dimension.STRESS),
    "гпа": UnitInfo("Pa", 1e9, Dimension.STRESS), "gpa": UnitInfo("Pa", 1e9, Dimension.STRESS),
    "кгс/см2": UnitInfo("Pa", _G * 1e4, Dimension.STRESS), "кг/см2": UnitInfo("Pa", _G * 1e4, Dimension.STRESS,
                                                                              note="kgf/cm2 in Soviet usage"),
    "тс/м2": UnitInfo("Pa", _G * 1e3, Dimension.STRESS),
    "т/м2": UnitInfo("Pa", _G * 1e3, Dimension.STRESS, ambiguous_with=(Dimension.OTHER,),
                     note="usually tf/m2 (stress); mass per area otherwise"),
    "мн/м2": UnitInfo("Pa", 1e6, Dimension.STRESS),
    # strain
    "%": UnitInfo("1", 1e-2, Dimension.DIMENSIONLESS, ambiguous_with=(Dimension.STRAIN,)),
    "мм/м": UnitInfo("1", 1e-3, Dimension.STRAIN), "1e-3": UnitInfo("1", 1e-3, Dimension.STRAIN),
    "-": UnitInfo("1", 1.0, Dimension.DIMENSIONLESS), "1": UnitInfo("1", 1.0, Dimension.DIMENSIONLESS),
    "д.ед.": UnitInfo("1", 1.0, Dimension.DIMENSIONLESS), "доли": UnitInfo("1", 1.0, Dimension.DIMENSIONLESS),
    # rates
    "1/с": UnitInfo("1/s", 1.0, Dimension.STRAIN_RATE), "с-1": UnitInfo("1/s", 1.0, Dimension.STRAIN_RATE),
    "1/s": UnitInfo("1/s", 1.0, Dimension.STRAIN_RATE), "1/сут": UnitInfo("1/s", 1 / 86400.0, Dimension.STRAIN_RATE),
    "1/год": UnitInfo("1/s", 1 / (365.25 * 86400.0), Dimension.STRAIN_RATE),
    "мм/год": UnitInfo("m/s", 1e-3 / (365.25 * 86400.0), Dimension.VELOCITY),
    "мм/сут": UnitInfo("m/s", 1e-3 / 86400.0, Dimension.VELOCITY),
    "м/с": UnitInfo("m/s", 1.0, Dimension.VELOCITY), "m/s": UnitInfo("m/s", 1.0, Dimension.VELOCITY),
    "км/с": UnitInfo("m/s", 1e3, Dimension.VELOCITY), "km/s": UnitInfo("m/s", 1e3, Dimension.VELOCITY),
    "м/нс": UnitInfo("m/s", 1e9, Dimension.VELOCITY), "см/нс": UnitInfo("m/s", 1e7, Dimension.VELOCITY),
    "m/ns": UnitInfo("m/s", 1e9, Dimension.VELOCITY),
    "м/сут": UnitInfo("m/s", 1 / 86400.0, Dimension.HYDRAULIC_CONDUCTIVITY, ambiguous_with=(Dimension.VELOCITY,)),
    # angle, temperature, frequency, EM, permeability
    "°": UnitInfo("rad", 3.141592653589793 / 180, Dimension.ANGLE), "град": UnitInfo("rad", 3.141592653589793 / 180, Dimension.ANGLE),
    "рад": UnitInfo("rad", 1.0, Dimension.ANGLE),
    "°c": UnitInfo("K", None, Dimension.TEMPERATURE, note="affine: K = °C + 273.15"),
    "k": UnitInfo("K", 1.0, Dimension.TEMPERATURE),
    "°c/100м": UnitInfo("K/m", 1e-2, Dimension.OTHER, note="geothermal gradient"),
    "гц": UnitInfo("Hz", 1.0, Dimension.FREQUENCY), "мгц": UnitInfo("Hz", 1e6, Dimension.FREQUENCY),
    "ггц": UnitInfo("Hz", 1e9, Dimension.FREQUENCY), "mhz": UnitInfo("Hz", 1e6, Dimension.FREQUENCY),
    "см/м": UnitInfo("S/m", 1.0, Dimension.CONDUCTIVITY_EL, note="Siemens per metre (См/м)"),
    "мсм/м": UnitInfo("S/m", 1e-3, Dimension.CONDUCTIVITY_EL),
    "ом·м": UnitInfo("ohm*m", 1.0, Dimension.OTHER, note="resistivity"),
    "м2 (проницаемость)": UnitInfo("m2", 1.0, Dimension.PERMEABILITY),
    "д": UnitInfo("m2", 9.869233e-13, Dimension.PERMEABILITY, note="darcy"),
    "мд": UnitInfo("m2", 9.869233e-16, Dimension.PERMEABILITY, note="millidarcy"),
}

# variable → allowed dimensions
VARIABLE_DIMENSIONS: dict[str, tuple[Dimension, ...]] = {
    "density": (Dimension.DENSITY,),
    "unit_weight": (Dimension.UNIT_WEIGHT, Dimension.DENSITY),  # DENSITY allowed only via the ambiguous т/м3
    "youngs_modulus": (Dimension.STRESS,), "deformation_modulus": (Dimension.STRESS,),
    "shear_modulus": (Dimension.STRESS,), "bulk_modulus": (Dimension.STRESS,),
    "poisson_ratio": (Dimension.DIMENSIONLESS,),
    "ucs": (Dimension.STRESS,), "tensile_strength": (Dimension.STRESS,), "cohesion": (Dimension.STRESS,),
    "long_term_strength": (Dimension.STRESS,), "stress": (Dimension.STRESS,),
    "friction_angle": (Dimension.ANGLE,), "boundary_angle": (Dimension.ANGLE,), "angle_of_draw": (Dimension.ANGLE,),
    "dip": (Dimension.ANGLE,),
    "thickness": (Dimension.LENGTH,), "depth": (Dimension.LENGTH,), "elevation": (Dimension.LENGTH,),
    "chamber_width": (Dimension.LENGTH,), "pillar_width": (Dimension.LENGTH,), "chamber_height": (Dimension.LENGTH,),
    "subsidence": (Dimension.LENGTH,), "subsidence_max": (Dimension.LENGTH,),
    "subsidence_rate": (Dimension.VELOCITY,), "convergence_rate": (Dimension.VELOCITY, Dimension.STRAIN_RATE),
    "strain": (Dimension.STRAIN, Dimension.DIMENSIONLESS), "strain_rate": (Dimension.STRAIN_RATE,),
    "extraction_ratio": (Dimension.DIMENSIONLESS,), "loading_degree": (Dimension.DIMENSIONLESS,),
    "backfill_ratio": (Dimension.DIMENSIONLESS,), "K0_lambda": (Dimension.DIMENSIONLESS,),
    "vp": (Dimension.VELOCITY,), "vs": (Dimension.VELOCITY,), "em_velocity": (Dimension.VELOCITY,),
    "permittivity": (Dimension.DIMENSIONLESS,), "conductivity": (Dimension.CONDUCTIVITY_EL,),
    "frequency": (Dimension.FREQUENCY,), "temperature": (Dimension.TEMPERATURE,),
    "permeability": (Dimension.PERMEABILITY,), "hydraulic_conductivity": (Dimension.HYDRAULIC_CONDUCTIVITY,),
    "duration": (Dimension.TIME,), "delay": (Dimension.TIME,),
}


def lookup(unit: str) -> UnitInfo | None:
    return _UNITS.get(_n(unit))


def check_unit(variable: str | None, unit: str | None) -> list[str]:
    """Problems with a (variable, printed unit) pair; empty list = OK. Unknown variables are not judged."""
    if unit is None or not str(unit).strip():
        return ["missing unit"]
    info = lookup(unit)
    if info is None:
        if re.search(r"[a-zа-я]", _n(unit)):
            return [f"unknown unit string '{unit}' (add to vkm_world.core.units or record as UNIT_DOUBT)"]
        return [f"unparseable unit '{unit}'"]
    probs: list[str] = []
    if info.ambiguous_with:
        probs.append(f"ambiguous unit '{unit}': {info.dimension.value} or "
                     f"{'/'.join(d.value for d in info.ambiguous_with)} — {info.note or 'resolve by context'}")
    if variable and variable in VARIABLE_DIMENSIONS:
        allowed = VARIABLE_DIMENSIONS[variable]
        if info.dimension not in allowed and not set(info.ambiguous_with) & set(allowed):
            probs.append(f"unit '{unit}' ({info.dimension.value}) incompatible with variable '{variable}' "
                         f"(expects {'/'.join(d.value for d in allowed)})")
    return probs


def is_hard_error(problems: list[str]) -> bool:
    """Ambiguity is a QA flag, not an error; incompatibility/missing/unknown are errors."""
    return any(not p.startswith("ambiguous") for p in problems)
