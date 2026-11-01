"""Small synthetic MAPDL verification cases; never a site/material admission.

Units: Pa, m, kg; creep time is Julian years (density has no role in that case).
The elastic activation case uses SI gravity [m/s2] and a dimensionless static
load-step counter, with no physical time derivative. The Norton exponent is one so that
the two-stage uniaxial solution is independent closed-form verification. All
parameter values are MODEL_CHOICE, not transferred salt properties. No remesh,
plasticity, damage, fluid or field-observation interpretation is covered.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CreepStage:
    duration_yr: float
    compression_pa: float

    def __post_init__(self):
        if not math.isfinite(self.duration_yr) or self.duration_yr <= 0:
            raise ValueError("stage duration must be positive and finite")
        if not math.isfinite(self.compression_pa) or self.compression_pa <= 0:
            raise ValueError("compressive stress must be positive and finite")


@dataclass(frozen=True)
class TwoStageCreep:
    """Unit cube, free lateral faces, nu=0, uniform axial compression.

    The transfer operator is native MAPDL multiframe restart on the same mesh.
    Total axial strain = sigma/E + eps_cr; eps_cr = integral(sigma/eta dt).
    Across an instantaneous load change eps_cr is continuous. Elastic load-jump
    work is the change in sigma**2/(2E); creep work is integral(sigma**2/eta dt).
    """

    first: CreepStage = CreepStage(1.0, 2.0e6)
    second: CreepStage = CreepStage(1.0, 1.0e6)
    young_pa: float = 1.0e9
    viscosity_pa_yr: float = 1.0e10
    substeps: int = 20

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in (self.young_pa, self.viscosity_pa_yr)):
            raise ValueError("modulus and viscosity must be positive and finite")
        if isinstance(self.substeps, bool) or not isinstance(self.substeps, int) or self.substeps < 1:
            raise ValueError("substeps must be a positive integer")

    def reference(self, *, reset_memory: bool = False) -> dict[str, float]:
        a, b = self.first, self.second
        memory_a = a.compression_pa * a.duration_yr / self.viscosity_pa_yr
        eps = b.compression_pa * b.duration_yr / self.viscosity_pa_yr
        if not reset_memory:
            eps += memory_a
        energy = b.compression_pa ** 2 / (2 * self.young_pa)  # volume = 1 m3
        diss = (a.compression_pa ** 2 * a.duration_yr + b.compression_pa ** 2 * b.duration_yr) / self.viscosity_pa_yr
        return {"time_yr": a.duration_yr + b.duration_yr,
                "uy_m": -(b.compression_pa / self.young_pa + eps),
                "creep_strain": eps, "reaction_n": b.compression_pa,
                "elastic_energy_j": energy, "creep_dissipation_j": diss,
                "external_work_j": energy + diss, "retained_memory": memory_a}

    def _stage(self, stage: CreepStage, end: float) -> list[str]:
        return ["NSEL,S,LOC,Y,1", f"F,ALL,FY,{-stage.compression_pa / 4:.16e}", "ALLSEL",
                f"TIME,{end:.16e}", f"NSUBST,{self.substeps},{self.substeps},{self.substeps}", "SOLVE"]

    def deck(self, mode: str = "full") -> str:
        """full/prefix are fresh runs; restart requires the prefix job checkpoint.

        Prefix writes all state files before exiting. Restart must use the same
        jobname in a new process and a copy of the prefix directory (not /CLEAR
        followed by fresh material construction or INISTATE reconstruction).
        """
        if mode not in {"full", "prefix", "restart"}:
            raise ValueError("mode must be full, prefix or restart")
        if mode == "restart":
            lines = ["/BATCH", "RESUME,case,rdb", "/SOLU",
                     "ANTYPE,STATIC,RESTART,1,,CONTINUE"]
        else:
            lines = _cube(self.young_pa) + [f"TB,CREEP,1,,,10", f"TBDATA,1,{1/self.viscosity_pa_yr:.16e},1,0",
                     "FINISH", "/SOLU", "ANTYPE,STATIC", "NLGEOM,OFF", "NROPT,FULL", "KBC,1",
                     "RATE,ON", "AUTOTS,OFF", "OUTRES,ALL,ALL", "RESCONTROL,DEFINE,ALL,LAST"]
            lines += self._stage(self.first, self.first.duration_yr)
        if mode != "prefix":
            lines += self._stage(self.second, self.first.duration_yr + self.second.duration_yr)
        lines += _extract("history")
        return "\n".join(lines + ["FINISH", "/EXIT,NOSAVE"]) + "\n"


def _cube(young_pa: float) -> list[str]:
    # SOLID185 right-handed brick with default formulation, free lateral expansion.
    return ["/BATCH", "/PREP7", "ET,1,SOLID185", f"MP,EX,1,{young_pa:.16e}", "MP,PRXY,1,0",
            "MP,DENS,1,2000", "N,1,0,0,0", "N,2,1,0,0", "N,3,1,1,0", "N,4,0,1,0",
            "N,5,0,0,1", "N,6,1,0,1", "N,7,1,1,1", "N,8,0,1,1", "E,1,2,3,4,5,6,7,8",
            "NSEL,S,LOC,X,0", "D,ALL,UX,0", "NSEL,S,LOC,Z,0", "D,ALL,UZ,0",
            "NSEL,S,LOC,Y,0", "D,ALL,UY,0", "ALLSEL"]


def _extract(name: str, *, creep: bool = True) -> list[str]:
    # One result row per converged substep; compression-positive creep, reactions
    # sum bottom-node RFY. SENE is the actual solver element elastic energy.
    lines = ["FINISH", "/POST1", "*GET,NSETS,ACTIVE,0,SET,NSET", "*DIM,RES,ARRAY,NSETS,7",
             "*DO,K,1,NSETS", "SET,,,,,,,K", "*GET,TT,ACTIVE,0,SET,TIME", "RES(K,1)=TT",
             "RES(K,2)=UY(3)", "REA=0"]
    for nid in (1, 2, 5, 6):
        lines += [f"*GET,RONE,NODE,{nid},RF,FY", "REA=REA+RONE"]
    lines += ["RES(K,3)=REA", "ETABLE,ERAS", "ETABLE,ENE,SENE", "SSUM", "*GET,ENEALL,SSUM,,ITEM,ENE",
              "RES(K,4)=ENEALL"]
    if creep:
        lines += ["ETABLE,CRY,EPCR,Y", "*GET,CRONE,ELEM,1,ETAB,CRY", "RES(K,5)=-CRONE",
                  "ETABLE,CDEN,NL,CREQ", "*GET,CREQ,ELEM,1,ETAB,CDEN", "RES(K,6)=CREQ"]
    else:
        # Sum all nodal vertical reactions for gravity/mass verification.
        lines += ["RTOTAL=0"]
        for nid in range(1, 9):
            lines += [f"*GET,RONE,NODE,{nid},RF,FY", "RTOTAL=RTOTAL+RONE"]
        lines += ["RES(K,5)=RTOTAL", "ETABLE,SYY2,S,Y", "*GET,SYTWO,ELEM,2,ETAB,SYY2", "RES(K,6)=SYTWO"]
    lines += ["ETABLE,SYY,S,Y", "*GET,SYONE,ELEM,1,ETAB,SYY", "RES(K,7)=SYONE", "*ENDDO",
              f"*CFOPEN,{name},txt", "*VWRITE,RES(1,1),RES(1,2),RES(1,3),RES(1,4),RES(1,5),RES(1,6),RES(1,7)",
              "(7E24.15)", "*CFCLOS"]
    return lines


def activation_deck() -> str:
    """Two overlaid elastic cubes, kill one then reactivate under gravity.

    Both cubes have density 2000 kg/m3. First impose -0.001 m shortening,
    then remove the second cube, then reactivate it at the HELD displacement,
    then unload to zero. Birth reference = displacement at activation (a declared
    MODEL_CHOICE); the born cube is initially strain-free, then gains tensile
    strain on unloading. EALIVE never preserves its earlier strain history.
    Gravity is ACEL=10 in the positive y direction (inertia load is downward).
    Active mass/reaction: 4000/40000, 2000/20000, 4000/40000, 4000/40000 kg/N.
    Elastic energy: 1000, 500, 500, 500 J. Killing removes 500 J of stored energy;
    this is removed-material energy, not viscous dissipation. Activation while
    holding displacement adds no elastic energy or boundary work. Unloading
    exchanges energy between the original and newly born cube; net elastic
    work is zero. No creep law is assigned to birth/death elements.
    """
    lines = _cube(1e9) + ["E,1,2,3,4,5,6,7,8", "FINISH", "/SOLU", "ANTYPE,STATIC",
             "NROPT,FULL", "NLGEOM,OFF", "KBC,1", "OUTRES,ALL,ALL", "ACEL,0,10,0",
             "NSEL,S,LOC,Y,1", "D,ALL,UY,-0.001", "ALLSEL", "TIME,1", "SOLVE",
             "NSUBST,1,1,1", "EKILL,2", "TIME,2", "SOLVE", "EALIVE,2", "TIME,3", "SOLVE",
             "NSEL,S,LOC,Y,1", "D,ALL,UY,0", "ALLSEL", "TIME,4", "SOLVE"]
    lines += _extract("activation", creep=False)
    return "\n".join(lines + ["FINISH", "/EXIT,NOSAVE"]) + "\n"


def activation_reference() -> dict[str, list[float]]:
    """Independent unit-cube spring balance, tension-positive element stresses.

    Original cube stress = E*u. The born cube stress after activation =
    E*(u-u_birth), u_birth=-0.001 m; its pre-kill memory is discarded.
    Reaction sum measures gravity mass independently of opposing imposed loads.
    """
    return {"time": [1, 2, 3, 4], "uy": [-0.001, -0.001, -0.001, 0],
            "bottom_reaction_n": [2020000, 1010000, 1020000, -980000],
            "elastic_energy_j": [1000, 500, 500, 500],
            "gravity_reaction_n": [40000, 20000, 40000, 40000],
            "second_stress_y_pa": [-1e6, 0, 0, 1e6],
            "first_stress_y_pa": [-1e6, -1e6, -1e6, 0]}
