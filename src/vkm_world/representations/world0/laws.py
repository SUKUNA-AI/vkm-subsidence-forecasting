"""Local laws used by World-0 microkernels (theory v0.2: FW §1, §9–10, §16; MS §13–24).

Every function is a *named, known* law or a closed-form consequence of one; none of them is new physics.
Status classes follow FW §1 and are recorded in ``LAW_SPECS``:

* KNOWN_PHYSICS / KNOWN_MATHEMATICS — textbook laws (Hooke, Norton, Darcy, Fourier, Terzaghi, Newton gravity);
* PHYSICAL_HYPOTHESIS — a law family that distinguishes worlds (e.g. hyperbolic backfill compaction, damage);
* DERIVATION — a closed-form solution derived here from the laws above (stated assumptions, tested);
* REPRESENTATION_CHOICE — a reduction device of the representation, not a material law (MS §11).

Units are SI (Pa, m, s, K) unless a parameter name says otherwise. Compression and convergence are positive.
Parameter VALUES are never stored here: they come from evidence (PRIVATE ``11_evidence_vnext``) or labelled
scenario ranges; ``evidence_ids`` link a law to its catalogue rows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

YEAR_S = 365.25 * 86400.0          # Julian year [s]
G_STD = 9.80665                    # standard gravity [m/s²] (CGPM 1901)
R_GAS = 8.314462618                # molar gas constant [J/(mol·K)] (CODATA 2018, exact)
G_NEWTON = 6.67430e-11             # Newtonian constant of gravitation [m³/(kg·s²)] (CODATA 2018)


@dataclass(frozen=True)
class LawSpec:
    law_id: str
    name: str
    family: str
    status: str
    equation: str
    role: str  # CAUSAL (enters the physical evolution) | PROBE (observation only) | REDUCTION (representation device)
    evidence_ids: tuple[str, ...] = field(default=())
    note: str = ""


LAW_SPECS: dict[str, LawSpec] = {}


def _law(spec: LawSpec) -> LawSpec:
    if spec.law_id in LAW_SPECS:
        raise ValueError(f"duplicate law id {spec.law_id}")
    LAW_SPECS[spec.law_id] = spec
    return spec


# ---------------------------------------------------------------------------------------------- elasticity
_law(LawSpec("W0-L-01", "Hooke isotropic linear elasticity", "ELASTICITY", "KNOWN_PHYSICS",
             "σ = λ tr(ε) I + 2μ ε", "CAUSAL", evidence_ids=('ML-01', 'ML-02')))


def shear_modulus(E: float, nu: float) -> float:
    return E / (2.0 * (1.0 + nu))


def bulk_modulus(E: float, nu: float) -> float:
    return E / (3.0 * (1.0 - 2.0 * nu))


def oedometric_modulus(E: float, nu: float) -> float:
    """Constrained (oedometric) modulus M = E(1−ν)/((1+ν)(1−2ν)): vertical stiffness of a laterally confined layer."""
    return E * (1.0 - nu) / ((1.0 + nu) * (1.0 - 2.0 * nu))


def plane_strain_modulus(E: float, nu: float) -> float:
    """E' = E/(1−ν²): uniaxial in-plane stiffness with free lateral face and ε_zz = 0 (plane strain pillar)."""
    return E / (1.0 - nu * nu)


def k0_elastic(nu: float) -> float:
    """Lateral stress ratio of an elastic laterally confined layer under gravity, λ = ν/(1−ν) (hypothesis 'K0')."""
    return nu / (1.0 - nu)


# ------------------------------------------------------------------------------------------- Norton creep
_law(LawSpec("W0-L-02", "Norton steady-state power-law creep (von Mises flow)", "CREEP_STEADY", "KNOWN_PHYSICS",
             "ε̇_cr = (3/2)·(p0/η_s)·(σ_eq/p0)^n · s/σ_eq", "CAUSAL",
             note="VKM form and n = 5 transferred from VKM-SRC-045 (Transfer TR-CREEP-ERMASHOV2015-SKRU1-SLICE-V6_2); "
                  "η_s is a scenario set FIELD/LAB, not one number", evidence_ids=('ML-03', 'ML-06', 'ML-10')))


def eta_MPa_year_to_Pa_s(eta_MPa_year: float) -> float:
    return eta_MPa_year * 1.0e6 * YEAR_S


def norton_rate(sigma_eq: float | np.ndarray, eta_s: float, n: float, p0: float = 1.0e6):
    """Equivalent creep strain rate ε̇_eq = (p0/η_s)·(σ_eq/p0)^n [1/s]; η_s in Pa·s, p0 = 1 MPa (VKM-SRC-045 form)."""
    s = np.abs(np.asarray(sigma_eq, dtype=float))
    return (p0 / eta_s) * (s / p0) ** n


def mapdl_norton_c1(eta_s: float, n: float, p0: float = 1.0e6) -> float:
    """MAPDL TB,CREEP TBOPT=10 constant C1 for ε̇ = C1·σ^C2 with σ in Pa and time in the units of η_s."""
    return p0 ** (1.0 - n) / eta_s


def plane_strain_free_factor(n: float) -> float:
    """Steady Norton creep, plane strain, uniaxial in-plane load with a free lateral face (σ_xx = 0, ε_zz = 0).

    Incompressible creep gives σ_zz = σ_yy/2, σ_eq = (√3/2)|σ_yy| and ε̇_yy = (√3/2)^(n+1)·ε̇_uniax(σ_yy).
    DERIVATION (registered as W0-L-02a); tested against MAPDL in the kernel checks.
    """
    return (math.sqrt(3.0) / 2.0) ** (n + 1.0)


_law(LawSpec("W0-L-02a", "plane-strain free-face factor of Norton creep", "CREEP_STEADY", "DERIVATION",
             "ε̇_yy = (√3/2)^(n+1)·(p0/η_s)(σ/p0)^n", "CAUSAL"))

_law(LawSpec("W0-L-03", "Arrhenius temperature factor of creep", "TEMPERATURE_FACTOR", "KNOWN_PHYSICS",
             "f(T) = exp(−Q/R·(1/T − 1/T_ref))", "CAUSAL",
             note="Q and A for VKM salts: NOT_IN_CORPUS after 12 search routes (re-check A1); only NON_VKM values",
             evidence_ids=("ML-04", "ML-05")))


def arrhenius_factor(T: float, T_ref: float, Q: float) -> float:
    """Ratio of creep rates at T and T_ref (K) for activation energy Q [J/mol]."""
    return math.exp(-Q / R_GAS * (1.0 / T - 1.0 / T_ref))


# ------------------------------------------------------------------------------- hereditary (Volterra) creep
_law(LawSpec("W0-L-04", "hereditary creep with Abel kernel (Volterra, Rabotnov/Rzhanitsyn form)", "CREEP_HEREDITARY",
             "KNOWN_PHYSICS", "ε(t) = σ/E·[1 + δ·t^(1−α)/(1−α)] (constant σ)", "CAUSAL", evidence_ids=('ML-07', 'ML-08', 'ML-09')))


def abel_creep_compliance(t, E: float, delta: float, alpha: float):
    """Creep compliance J(t) of the Abel-kernel hereditary law K(t) = δ·t^(−α), 0 < α < 1 (t in the units of δ)."""
    t = np.asarray(t, dtype=float)
    return (1.0 + delta * np.power(np.maximum(t, 0.0), 1.0 - alpha) / (1.0 - alpha)) / E


def nnls(A: np.ndarray, b: np.ndarray, max_iter: int = 500) -> np.ndarray:
    """Non-negative least squares (Lawson–Hanson active set). Small problems only."""
    A = np.asarray(A, float)
    b = np.asarray(b, float)
    m, n = A.shape
    x = np.zeros(n)
    P: list[int] = []
    w = A.T @ (b - A @ x)
    it = 0
    while it < max_iter and np.any(w[[j for j in range(n) if j not in P]] > 1e-12 * max(1.0, np.abs(w).max())):
        it += 1
        Z = [j for j in range(n) if j not in P]
        j = Z[int(np.argmax(w[Z]))]
        P.append(j)
        while True:
            z = np.zeros(n)
            z[P] = np.linalg.lstsq(A[:, P], b, rcond=None)[0]
            if np.all(z[P] > 0):
                x = z
                break
            neg = [k for k in P if z[k] <= 0]
            alpha = min(x[k] / (x[k] - z[k]) for k in neg)
            x = x + alpha * (z - x)
            P = [k for k in P if x[k] > 1e-14]
            if not P:
                break
        w = A.T @ (b - A @ x)
    return x


_law(LawSpec("W0-L-04a", "Prony (generalised Kelvin) approximation of a memory kernel", "CREEP_HEREDITARY",
             "REPRESENTATION_CHOICE", "J(t) ≈ J0 + Σ_k w_k (1 − e^(−t/τ_k)), w_k ≥ 0", "REDUCTION",
             note="the number of modes is a representation choice (MS §14); the modes are memory state variables, "
                  "not material constants"))


def prony_fit_compliance(t: np.ndarray, J: np.ndarray, taus: np.ndarray, J0: float) -> np.ndarray:
    """Non-negative weights w_k with J(t) ≈ J0 + Σ w_k(1 − e^(−t/τ_k)) on the grid t (relative least squares)."""
    t = np.asarray(t, float)
    J = np.asarray(J, float)
    basis = 1.0 - np.exp(-t[:, None] / np.asarray(taus, float)[None, :])
    scale = 1.0 / np.maximum(np.abs(J), 1e-300)
    return nnls(basis * scale[:, None], (J - J0) * scale)


def prony_compliance(t, J0: float, weights: np.ndarray, taus: np.ndarray):
    t = np.asarray(t, float)
    return J0 + (np.asarray(weights)[None, :] * (1.0 - np.exp(-t[:, None] / np.asarray(taus)[None, :]))).sum(axis=1)


_law(LawSpec("W0-L-08b", "exponential volumetric compaction of hydraulic backfill (Hencky-type state equation)",
             "BACKFILL_COMPACTION", "PHYSICAL_HYPOTHESIS",
             "ε_v = ε_k·[1 − exp(−σ_m/σ_k)]; deviatoric part ε_dev = s/(2G*)", "CAUSAL", evidence_ids=("ML-18",),
             note="VKM form of VKM-SRC-044 eq. (7.13); ε_k, σ_k UNKNOWN in the corpus → labelled scenario only"))


def backfill_volumetric_strain(sigma_m, eps_k: float, sigma_k: float):
    """W0-L-08b: volumetric compaction of hydraulic backfill under mean stress σ_m (compression +)."""
    sm = np.maximum(np.asarray(sigma_m, float), 0.0)
    return eps_k * (1.0 - np.exp(-sm / sigma_k))


def backfill_mean_stress(eps_v, eps_k: float, sigma_k: float):
    """Inverse of W0-L-08b; ε_v ≥ ε_k is inadmissible (raises)."""
    e = np.asarray(eps_v, float)
    if np.any(e >= eps_k):
        raise ValueError("volumetric strain reached the compaction limit ε_k")
    return -sigma_k * np.log(1.0 - np.maximum(e, 0.0) / eps_k)


# --------------------------------------------------------------------------------------------- strength
_law(LawSpec("W0-L-05", "Mohr–Coulomb strength criterion", "STRENGTH", "KNOWN_PHYSICS",
             "σ1f = 2c·cosφ/(1−sinφ) + σ3·(1+sinφ)/(1−sinφ)", "CAUSAL", evidence_ids=('ML-11', 'ML-12', 'ML-15')))


def mohr_coulomb_sigma1(sigma3: float, c: float, phi_deg: float) -> float:
    """Major principal stress at failure (compression +) for confinement σ3, cohesion c and friction angle φ."""
    s = math.sin(math.radians(phi_deg))
    return 2.0 * c * math.cos(math.radians(phi_deg)) / (1.0 - s) + sigma3 * (1.0 + s) / (1.0 - s)


def mohr_coulomb_margin(sigma1: float, sigma3: float, c: float, phi_deg: float) -> float:
    """Strength ratio σ1f(σ3)/σ1 (> 1: below the criterion)."""
    return mohr_coulomb_sigma1(sigma3, c, phi_deg) / sigma1


_law(LawSpec("W0-L-06", "tributary-area pillar load (Turner–Shevyakov)", "PILLAR_LOAD", "KNOWN_PHYSICS",
             "σ_p = σ_v0/(1 − k), k = extraction ratio", "CAUSAL",
             note="upper bound of the mean pillar stress for a wide panel; checked against FE in the pair runs", evidence_ids=('ML-13', 'ML-14')))


def tributary_pillar_stress(sigma_v0: float, k: float) -> float:
    if not 0.0 <= k < 1.0:
        raise ValueError("extraction ratio must be in [0, 1)")
    return sigma_v0 / (1.0 - k)


def loading_degree(sigma_p: float, sigma_strength: float) -> float:
    """Pillar loading degree C = σ_p/σ_пр (VKM practice); σ_пр is the pillar strength of the same scale."""
    return sigma_p / sigma_strength


# ------------------------------------------------------------------------------------------------ damage
_law(LawSpec("W0-L-07", "Kachanov–Rabotnov creep damage", "DAMAGE", "PHYSICAL_HYPOTHESIS",
             "ω̇ = B·(σ/(1−ω))^m, ω ∈ [0, 1)", "CAUSAL",
             note="family of the damage world; B, m for SKRU-1 pillars UNKNOWN unless evidence rows give them", evidence_ids=('ML-16', 'ML-17')))


def kachanov_damage(t, sigma: float, B: float, m: float):
    """ω(t) at constant nominal stress σ: (1−ω)^(m+1) = 1 − (m+1)·B·σ^m·t; ω = 1 at t_r = 1/((m+1)Bσ^m)."""
    t = np.asarray(t, float)
    base = 1.0 - (m + 1.0) * B * sigma ** m * t
    return np.where(base > 0.0, 1.0 - np.power(np.maximum(base, 0.0), 1.0 / (m + 1.0)), 1.0)


def kachanov_rupture_time(sigma: float, B: float, m: float) -> float:
    return 1.0 / ((m + 1.0) * B * sigma ** m)


_law(LawSpec("W0-L-07b", "barrier-type damage kinetics of salt rocks (VKM form)", "DAMAGE", "PHYSICAL_HYPOTHESIS",
             "ω̇ = L_ω·⟨p − p*·[1 − (σ_u/σ_u*)²]⟩ (VKM-SRC-047 eq. 5.74–5.76)", "CAUSAL",
             evidence_ids=("ML-16", "RA-009", "RA-010", "RA-011", "RA-012", "RA-013", "RA-018", "RA-019", "RA-020",
                           "RA-022"),
             note="LAB, VKM_REGIONAL: p* = 3.85 MPa, σ_u* = 5.5 (salt) / 5.9 (sylvinite) MPa; L_ω0 differs ≈87× between "
                  "the creep and relaxation fits (CF-RA-03); 20-minute tests — extrapolation to years not justified by "
                  "the source; sign convention of p as in the source"))


def vkm_damage_rate(p: float, sigma_u: float, L_omega: float, p_star: float, sigma_u_star: float) -> float:
    """W0-L-07b as printed: ω̇ = L_ω·⟨p − p*[1 − (σ_u/σ_u*)²]⟩ (Macaulay bracket ⟨x⟩ = max(x, 0))."""
    return L_omega * max(0.0, p - p_star * (1.0 - (sigma_u / sigma_u_star) ** 2))


# ------------------------------------------------------------------------------------------------ backfill
_law(LawSpec("W0-L-08", "hyperbolic compaction of granular backfill", "BACKFILL_COMPACTION", "PHYSICAL_HYPOTHESIS",
             "σ_b = E0·ε/(1 − ε/ε_m), 0 ≤ ε < ε_m; unloading linear with E_u (memory ε_max)", "CAUSAL",
             note="general form for loose fill (crushable granular material); VKM backfill parameters per evidence "
                  "rows or UNKNOWN → scenario", evidence_ids=('ML-19',)))


def backfill_stress(eps, E0: float, eps_m: float):
    """Loading branch of the hyperbolic compaction law (compression +); ε ≥ ε_m is inadmissible (raises)."""
    eps = np.asarray(eps, float)
    if np.any(eps >= eps_m):
        raise ValueError("backfill strain reached the limiting compaction ε_m")
    return np.where(eps > 0.0, E0 * eps / (1.0 - eps / eps_m), 0.0)


def backfill_strain(sigma, E0: float, eps_m: float):
    """Inverse of the loading branch: ε = σ/(E0 + σ/ε_m)."""
    sigma = np.asarray(sigma, float)
    return np.where(sigma > 0.0, sigma / (E0 + sigma / eps_m), 0.0)


def backfill_tangent(eps: float, E0: float, eps_m: float) -> float:
    return E0 / (1.0 - eps / eps_m) ** 2


_law(LawSpec("W0-L-15", "normative safety conditions of the water-protective strata (VZT)", "VZT_CRITERION",
             "ENGINEERING_ASSUMPTION",
             "m_c = Σm_i ≥ [m] (10 | 20 m); horizontal strain of a VZT layer ε_cj ≤ [ε_cj] = 0.002 (no test data)",
             "CAUSAL", evidence_ids=("ML-26",),
             note="normative criterion («Указания», cited by VKM-SRC-037 §8.5.2): a design rule, not a physical law"))


def vzt_strain_margin(eps_h, eps_lim: float = 0.002):
    """Ratio [ε]/|ε_h| of the normative VZT criterion (> 1: criterion satisfied)."""
    e = np.abs(np.asarray(eps_h, float))
    return np.where(e > 0, eps_lim / np.maximum(e, 1e-300), np.inf)


# ------------------------------------------------------------------------------------------- hydraulics
_law(LawSpec("W0-L-09", "Darcy law", "DARCY", "KNOWN_PHYSICS", "q = −K·∇h = −(k/μ)(∇p − ρ_w g)", "CAUSAL", evidence_ids=('ML-21',)))
_law(LawSpec("W0-L-10", "Terzaghi effective stress and 1D consolidation", "EFFECTIVE_STRESS_CONSOLIDATION",
             "KNOWN_PHYSICS", "σ' = σ − α·p; ∂p/∂t = c_v ∂²p/∂z², c_v = K/S_s", "CAUSAL", evidence_ids=('ML-22',)))


def permeability_to_conductivity(k: float, mu: float = 1.0e-3, rho_w: float = 1000.0, g: float = G_STD) -> float:
    return k * rho_w * g / mu


def series_conductivity(thicknesses, conductivities) -> float:
    """Effective vertical hydraulic conductivity of layers in series (harmonic, thickness-weighted)."""
    h = np.asarray(thicknesses, float)
    K = np.asarray(conductivities, float)
    if np.any(K <= 0):
        return 0.0
    return float(h.sum() / (h / K).sum())


def darcy_flux(K: float, head_drop: float, length: float) -> float:
    """Specific discharge through a layer of thickness ``length`` under head drop ``head_drop`` [m/s]."""
    return K * head_drop / length


def effective_stress(sigma: float, p: float, alpha: float = 1.0) -> float:
    return sigma - alpha * p


def consolidation_coefficient(K: float, S_s: float) -> float:
    """c_v = K/S_s [m²/s]; K hydraulic conductivity [m/s], S_s specific storage [1/m]."""
    return K / S_s


def terzaghi_degree(Tv, n_terms: int = 200):
    """Average degree of consolidation U(T_v) of the Terzaghi 1D problem (exact series, n_terms terms)."""
    Tv = np.atleast_1d(np.asarray(Tv, float))
    m = np.arange(n_terms)
    M = math.pi * (2 * m + 1) / 2.0
    return 1.0 - (2.0 / M ** 2 * np.exp(-np.outer(Tv, M ** 2))).sum(axis=1)


def terzaghi_pressure(zeta, Tv: float, n_terms: int = 200):
    """Excess pressure ratio p/p0 at ζ = z/H_d (drained at ζ = 0, impermeable at ζ = 1)."""
    zeta = np.asarray(zeta, float)
    m = np.arange(n_terms)
    M = math.pi * (2 * m + 1) / 2.0
    return (2.0 / M * np.sin(np.outer(zeta, M)) * np.exp(-M ** 2 * Tv)).sum(axis=1)


# ------------------------------------------------------------------------------------------------ thermal
_law(LawSpec("W0-L-11", "Fourier heat conduction", "HEAT_CONDUCTION", "KNOWN_PHYSICS",
             "q_T = −λ_T ∇T; ρc ∂T/∂t = ∇·(λ_T ∇T)", "CAUSAL",
             note="T is a prescribed field in World-0 unless the thermal port is activated (MS §20)",
             evidence_ids=("ML-23", "RA-003", "RA-004", "RA-007", "RA-008")))


def geotherm(z_depth, T_surface: float, gradient: float):
    """Steady 1D geotherm T(z) = T0 + G·z (z depth, m; G in K/m)."""
    return T_surface + gradient * np.asarray(z_depth, float)


def diffusion_time(length: float, diffusivity: float) -> float:
    """Characteristic conduction/diffusion time L²/κ [s]."""
    return length * length / diffusivity


# ------------------------------------------------------------------------- closed-form kernel derivations
_law(LawSpec("W0-L-12", "Norton relaxation of the deviator in a laterally confined layer", "CREEP_STEADY",
             "DERIVATION", "q̇ = −E/(2(1−ν))·(p0/η)(q/p0)^n; Δε_v = 2(1−2ν)·(q0 − q)/E", "CAUSAL",
             note="consequence of W0-L-01 + W0-L-02 under ε_h = 0, σ_v = const; q = σ_v − σ_h"))


def oedometric_norton_q(t, q0: float, E: float, nu: float, eta: float, n: float, p0: float = 1.0e6):
    """Deviator q(t) = σ_v − σ_h of a confined Norton layer at constant σ_v (closed form, n > 1)."""
    t = np.asarray(t, float)
    c = E / (2.0 * (1.0 - nu) * eta)
    x0 = q0 / p0
    return p0 * np.power(x0 ** (1.0 - n) + (n - 1.0) * c * t, 1.0 / (1.0 - n))


def oedometric_norton_vertical_strain(t, q0: float, E: float, nu: float, eta: float, n: float, p0: float = 1.0e6):
    """Vertical creep-induced strain increment of the confined layer (compression +)."""
    return 2.0 * (1.0 - 2.0 * nu) * (q0 - oedometric_norton_q(t, q0, E, nu, eta, n, p0)) / E


_law(LawSpec("W0-L-13", "steady creep closure of a long circular opening in a Norton medium", "CREEP_STEADY",
             "DERIVATION", "u̇_a/a = (√3/2)·(p0/η)·[(√3/n)·Δp/p0]^n", "CAUSAL",
             note="incompressible steady state, plane strain, hydrostatic far field; a check of the void-kernel law"))


def circular_opening_closure_rate(dp: float, eta: float, n: float, p0: float = 1.0e6,
                                  a_over_R: float = 0.0) -> float:
    """Steady radial closure strain rate u̇_a/a [1/s] of a circular opening under pressure drop Δp = p_R − p_i.

    ``a_over_R`` > 0: thick cylinder with the pressure applied at the outer radius R (the integral of the
    equilibrium equation runs from a to R): Δp is divided by (1 − (a/R)^(2/n)).
    """
    corr = 1.0 - a_over_R ** (2.0 / n) if a_over_R > 0 else 1.0
    return (math.sqrt(3.0) / 2.0) * (p0 / eta) * ((math.sqrt(3.0) / n) * dp / (p0 * corr)) ** n


def plane_strain_norton_element(times, sigma: float, E: float, nu: float, eta: float, n: float, p0: float = 1.0e6,
                                substeps: int = 200):
    """Reference solution of one plane-strain element, σ_yy = −σ, σ_xx = 0, ε_zz = 0, Norton creep (von Mises).

    σ_zz follows from ε_zz = 0: σ_zz = ν·σ_yy − E·ε^cr_zz (tension +). Explicit RK2 on the creep strains.
    Returns ε_yy(t) (tension +). DERIVATION check of W0-L-02a including the elastic transient.
    """
    t = np.asarray(times, float)
    ecr = np.zeros(3)                  # xx, yy, zz creep strains
    out = np.empty_like(t)

    def rates(e):
        sxx, syy = 0.0, -sigma
        szz = nu * syy - E * e[2]
        m = (sxx + syy + szz) / 3.0
        s = np.array([sxx - m, syy - m, szz - m])
        seq = math.sqrt(1.5 * float(s @ s))
        if seq == 0:
            return np.zeros(3)
        return 1.5 * (p0 / eta) * (seq / p0) ** n * s / seq

    def eyy(e):
        sxx, syy = 0.0, -sigma
        szz = nu * syy - E * e[2]
        return (syy - nu * (sxx + szz)) / E + e[1]

    out[0] = eyy(ecr)
    for i in range(1, len(t)):
        dt = (t[i] - t[i - 1]) / substeps
        for _ in range(substeps):
            k1 = rates(ecr)
            k2 = rates(ecr + dt * k1)
            ecr = ecr + 0.5 * dt * (k1 + k2)
        out[i] = eyy(ecr)
    return out


# --------------------------------------------------------------------- influence-function comparators
_law(LawSpec("W0-L-16", "gravity as body force: lithostatic vertical stress", "GRAVITY", "KNOWN_PHYSICS",
             "σ_v(z) = ∫ρ g dz (initial stress, momentum balance)", "CAUSAL"))
_law(LawSpec("W0-L-17", "gravity as potential field: Newtonian attraction of density contrasts", "GRAVITY",
             "KNOWN_PHYSICS", "∇²Φ = 4πGρ; Δg_z = G∫Δρ z/r³ dV (Talwani 2D polygon; Nagy/Plouff prism)", "PROBE",
             evidence_ids=("ML-27",),
             note="observation operator only (FW §16); formulas from textbooks outside the corpus (GENERAL_METHOD); "
                  "ML-27 = field gravimetry at other VKM sites, not SKRU-1"))
_law(LawSpec("W0-L-14", "Knothe (Gaussian) surface influence function", "INFLUENCE_FUNCTION", "KNOWN_MATHEMATICS",
             "g(x) = (1/r)·exp(−π x²/r²), tanβ = H/r", "REDUCTION",
             note="used only as a comparator for influence operators computed by FE; it is a FORECAST_METHOD shape, "
                  "not a law of the representation", evidence_ids=('ML-24', 'ML-25')))


def knothe_influence(x, r: float):
    x = np.asarray(x, float)
    return np.exp(-math.pi * x * x / (r * r)) / r
