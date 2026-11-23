"""World inputs of the GPU ensemble: zone tables (ladder step 3), creep Transfer (step 7), initial stress hypotheses.

All numbers come from the two delivered JSON records — ``step3_materials.json`` (G_solver, statuses and sources per
zone) and ``creep_transfer.json`` (TR-CREEP-ERMASHOV2015-SKRU1-SLICE-V6_2). Nothing is invented here: this module
only maps them onto element fields, with the ensemble factors applied explicitly (scenario ranges are defined in
``ensemble_design.py``).
"""
from __future__ import annotations

import json
from pathlib import Path

import torch

G_ACC = 9.81
KRII_ZONES = (6, 14, 15, 16)
LITHOLOGY = {3: "rock_salt", 4: "carnallite", 5: "sylvinite", 6: "sylvinite", 7: "sylvinite", 8: "rock_salt",
             14: "sylvinite", 15: "sylvinite", 16: "sylvinite"}
LITH_INDEX = {"sylvinite": 0, "rock_salt": 1, "carnallite": 2}


def base_zone(z: int) -> int:
    return 6 if z in KRII_ZONES else z


class Tables:
    def __init__(self, materials_json: Path, transfer_json: Path):
        self.materials = json.loads(Path(materials_json).read_text(encoding="utf-8"))
        self.transfer = json.loads(Path(transfer_json).read_text(encoding="utf-8"))
        self.zrow = {r["zone"]: r for r in self.materials["zones"]}
        lab_E = {}
        for z, r in self.zrow.items():
            e = r["E_Pa"]
            # LAB-based zones: M10 = 1.0 × LAB; fixed zones have the same value in every scenario
            lab_E[z] = (e["M10"], e["M10"] != e["M03"], e["NORM"])
        self.lab_E = lab_E
        self.n = float(self.transfer["n"]["value"])
        self.p0 = float(self.transfer["p0_MPa"]) * 1e6
        assert self.transfer["zones"]["15"]["lithology"] == "sylvinite"

    def eta(self, scen: str, lith: str) -> float:
        return float(self.transfer["eta_s_MPa_yr"][scen][lith]["value"])

    def rho(self, z: int) -> float:
        """ρ exactly as the MAPDL/OGS drivers (γ/g for normative unit weights; the JSON value is rounded)."""
        r = self.zrow[base_zone(z)]
        st = r["status_rho"]
        if "0,022 МН" in st:
            return 0.022e6 / G_ACC
        if "0,021 МН" in st:
            return 0.021e6 / G_ACC
        if "0,020 МН" in st:
            return 0.020e6 / G_ACC
        return float(r["rho_kg_m3"])


def element_fields(tab: Tables, zone: torch.Tensor, E_factor: torch.Tensor, eta_s: torch.Tensor,
                   norm_moduli: torch.Tensor | None = None):
    """Per-element E, ν, ρ, A from zone ids (B,nx,nz), modulus factor (B,) on LAB-based zones, viscosities
    eta_s (B, 3) [sylvinite, rock salt, carnallite] in MPa·yr (A = 1/ηs). ``norm_moduli`` (B,) bool: use the
    normative set instead of LAB × factor (scenario NORM)."""
    dt = torch.float64
    dev = zone.device
    B = zone.shape[0]
    E = torch.zeros(zone.shape, dtype=dt, device=dev)
    nu = torch.zeros_like(E)
    rho = torch.zeros_like(E)
    A = torch.zeros_like(E)
    for z in torch.unique(zone).tolist():
        bz = base_zone(int(z))
        r = tab.zrow[bz]
        msk = zone == z
        e_lab, is_lab, e_norm = tab.lab_E[bz]
        Ez = torch.full((B,), e_lab, dtype=dt, device=dev)
        if is_lab:
            Ez = Ez * E_factor.to(dt)
            if norm_moduli is not None:
                Ez = torch.where(norm_moduli, torch.full_like(Ez, e_norm), Ez)
        E = torch.where(msk, Ez[:, None, None], E)
        nu = torch.where(msk, torch.full_like(E, r["nu"]), nu)
        rho = torch.where(msk, torch.full_like(E, tab.rho(z)), rho)
        lith = LITHOLOGY.get(int(z))
        if lith is not None:
            Az = 1.0 / eta_s[:, LITH_INDEX[lith]].to(dt)
            A = torch.where(msk, Az[:, None, None], A)
    return E, nu, rho, A


def lambda_field(nu: torch.Tensor, k0: torch.Tensor) -> torch.Tensor:
    """λ per element: ν/(1−ν) where k0 (B,) is True (hypothesis K0), else 1 (L1, lithostatic)."""
    lk = nu / (1.0 - nu)
    return torch.where(k0[:, None, None], lk, torch.ones_like(nu))
