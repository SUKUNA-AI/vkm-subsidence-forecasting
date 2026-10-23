"""Synthetic tests of the World-0 microkernels (representation units, theory v0.2). No solver, no site data."""
from __future__ import annotations

import math

import numpy as np
import pytest

from vkm_world.representations.world0 import closure, gravity, influence, laws
from vkm_world.representations.world0.catalogue import KERNEL_TYPES, catalogue_csv, catalogue_json, catalogue_problems
from vkm_world.representations.world0.envelope import Microkernel
from vkm_world.representations.world0.kernels import (AquiferKernel, BackfillKernel, ConfinedSaltLayerKernel,
                                                       MinedCellKernel, PillarKernel, VZTKernel)
from vkm_world.representations.world0.mapdl_inputs import Layer, LayeredSlotModel, Material, PillarCellModel
from vkm_world.representations.world0.ports import PortKind, port_power_density

YR = laws.YEAR_S


# ------------------------------------------------------------------------------------------- catalogue
def test_catalogue_is_consistent():
    assert catalogue_problems() == []
    expected = {"MK-CHAMBER-VOID", "MK-PILLAR-IC", "MK-PILLAR-BARRIER", "MK-BACKFILL", "MK-MINED-CELL", "MK-LAYER-PKS",
                "MK-LAYER-SKZ", "MK-LAYER-SZ", "MK-LAYER-PDKS", "MK-VZT", "MK-LAYER-SMT", "MK-LAYER-TKT",
                "MK-LAYER-Q", "MK-SURFACE"}
    assert expected == set(KERNEL_TYPES)
    for kt in KERNEL_TYPES.values():
        assert kt.ports, kt.type_id
        assert kt.split_triggers, kt.type_id


def test_catalogue_exports_are_parseable():
    import csv
    import io
    import json
    rows = list(csv.DictReader(io.StringIO(catalogue_csv())))
    assert len(rows) == len(KERNEL_TYPES)
    assert json.loads(catalogue_json())["schema"] == "vkm.world0.kernel_types/1"


def test_no_universal_state_variables():
    """MS §46: damage, pressure, temperature only under their physics family."""
    for kt in KERNEL_TYPES.values():
        for v in kt.variables:
            if v.symbol in ("omega", "p", "p_k", "p_b", "p_f", "T"):
                assert v.family in ("DAMAGE", "HYDRO", "THERMAL_ACTIVE"), (kt.type_id, v.symbol)


# ------------------------------------------------------------------------------------------- envelope
def _pillar_instance(**kw):
    base = dict(kernel_id="K-1", representation_id="REP-W0-TEST", type_id="MK-PILLAR-IC",
                physical_entity_refs=({"entity_id": "PILLAR-1", "relation": "REPRESENTS"},),
                geometry_ref="GEOM-1", state_schema_version="1", active_families=("ELASTIC", "CREEP_STEADY"),
                state={"v_p": 0.0}, memory={"eps_cr": 0.0}, parameter_set_ref="PS-1", provenance="synthetic",
                task="surface subsidence", horizon_s=40 * YR, tolerance=0.05)
    base.update(kw)
    return Microkernel(**base)


def test_envelope_accepts_exact_active_state():
    k = _pillar_instance()
    assert k.state == {"v_p": 0.0}


@pytest.mark.parametrize("bad", [
    {"state": {"v_p": 0.0, "omega": 0.0}},                      # damage without the DAMAGE family
    {"memory": {}},                                              # missing memory of the active creep family
    {"state": {"v_p": 0.0, "sigma_p": 1.0}},                     # derived quantity stored as state
    {"active_families": ("ELASTIC", "HYDRO")},                   # family not available for the type
    {"state": {"v_p": -1.0}},                                    # bound violation
    {"physical_entity_refs": ({"entity_id": "P", "relation": "IS"},)},
    {"horizon_s": 0.0},
])
def test_envelope_rejects(bad):
    with pytest.raises(Exception):
        _pillar_instance(**bad)


def test_port_power():
    assert port_power_density(PortKind.MECHANICAL, 2.0, 3.0) == 6.0
    with pytest.raises(ValueError):
        port_power_density(PortKind.CONTACT, 1.0, 1.0)


# ------------------------------------------------------------------------------------------- laws
def test_norton_units_match_mapdl_constant():
    eta = 7.58e7 * 1e6            # Pa·yr
    c1 = laws.mapdl_norton_c1(eta, 5.0)
    assert math.isclose(c1 * (10e6) ** 5, float(laws.norton_rate(10e6, eta, 5.0)), rel_tol=1e-12)


def test_plane_strain_element_reaches_steady_factor():
    eta, n = 1.0e14, 5.0          # Pa·yr (synthetic)
    t = np.linspace(0, 400, 401)
    e = laws.plane_strain_norton_element(t, 10e6, 7e9, 0.2, eta, n, substeps=50)
    late = (e[-1] - e[-101]) / 100
    expect = -laws.plane_strain_free_factor(n) * float(laws.norton_rate(10e6, eta, n))
    assert math.isclose(late, expect, rel_tol=2e-3)


def test_confined_layer_closed_form_matches_integration():
    k = ConfinedSaltLayerKernel(h=10.0, E=4e9, nu=0.3, eta=8.21e13, n=5.0)
    t = np.linspace(0, 50, 26)
    q_num = k.deviator_numeric(t, 4e6)
    q_cf = k.deviator(t, 4e6)
    assert np.max(np.abs(q_num - q_cf)) / 4e6 < 2e-3
    # isochoric creep is locked: the total extra shortening is bounded by 2(1−2ν)q0/E·h
    assert k.extra_shortening(1e9, 4e6) <= 10.0 * 2 * 0.4 * 4e6 / 4e9 + 1e-12


def test_circular_closure_thick_cylinder_limit():
    eta, n = 1e21, 5.0
    inf = laws.circular_opening_closure_rate(7e6, eta, n)
    assert math.isclose(laws.circular_opening_closure_rate(7e6, eta, n, a_over_R=1e-30), inf, rel_tol=1e-9)
    assert laws.circular_opening_closure_rate(7e6, eta, n, a_over_R=1 / 30) > inf


def test_terzaghi_reference_values():
    U = laws.terzaghi_degree([0.197, 0.848])
    assert abs(U[0] - 0.5) < 2e-3 and abs(U[1] - 0.9) < 2e-3


def test_backfill_laws_invert():
    s = np.array([0.0, 1e5, 1e6, 5e6])
    e = laws.backfill_strain(s, 50e6, 0.3)
    assert np.allclose(laws.backfill_stress(e, 50e6, 0.3), s)
    ev = laws.backfill_volumetric_strain(s, 0.25, 2e6)
    assert np.allclose(laws.backfill_mean_stress(ev, 0.25, 2e6), s)
    with pytest.raises(ValueError):
        laws.backfill_mean_stress(0.25, 0.25, 2e6)


def test_nnls_and_prony_memory():
    t = np.geomspace(1, 3.5e5, 300)
    J = laws.abel_creep_compliance(t, 1.0, 0.18, 0.7)
    taus = np.geomspace(0.1, 3.5e6, 10)
    w = laws.prony_fit_compliance(t, J, taus, 1.0)
    assert np.all(w >= 0)
    assert np.max(np.abs(laws.prony_compliance(t, 1.0, w, taus) - J) / J) < 0.01
    one = laws.prony_fit_compliance(t, J, np.array([8766.0]), 1.0)
    assert np.max(np.abs(laws.prony_compliance(t, 1.0, one, np.array([8766.0])) - J) / J) > 0.3


def test_kachanov_rupture():
    tr = laws.kachanov_rupture_time(2.0, 0.01, 3.0)
    assert laws.kachanov_damage(tr * 0.999, 2.0, 0.01, 3.0)[()] < 1.0
    assert laws.kachanov_damage(tr * 1.001, 2.0, 0.01, 3.0)[()] == 1.0


def test_vkm_damage_kinetics_threshold():
    """W0-L-07b (VKM-SRC-047 form): no growth below the barrier, linear in L_ω above it."""
    assert laws.vkm_damage_rate(1.0, 3.0, 1.5e-5, 3.85, 5.5) == 0.0
    r = laws.vkm_damage_rate(5.0 / 3.0, 5.0, 1.5e-5, 3.85, 5.5)
    assert r > 0 and math.isclose(laws.vkm_damage_rate(5.0 / 3.0, 5.0, 3.0e-5, 3.85, 5.5), 2 * r)
    assert "RA-010" in laws.LAW_SPECS["W0-L-07b"].evidence_ids


def test_mohr_coulomb_and_vzt_margin():
    assert math.isclose(laws.mohr_coulomb_sigma1(0.0, 1e6, 30.0), 2e6 * math.cos(math.pi / 6) / 0.5)
    assert laws.vzt_strain_margin(0.001)[()] == 2.0


# ------------------------------------------------------------------------------------------- kernels
def test_pillar_kernel_uniaxial_exact():
    p = PillarKernel(h=5.5, E=6.895e9, nu=0.2, eta=7.58e13, n=5.0, plane_strain=False)
    v, e = p.respond_traction(np.array([0.0, 10.0]), lambda t: 10e6)
    assert math.isclose(v[-1] / 5.5, 10e6 / 6.895e9 + 1e5 / 7.58e13 * 1e6 * 10, rel_tol=1e-9)


def test_pillar_spring_soft_limit_equals_traction_control():
    p = PillarKernel(h=5.5, E=6.895e9, nu=0.2, eta=7.58e13, n=5.0)
    t = np.linspace(0, 40, 11)
    v1, s1, _ = p.respond_spring(t, 11e6, 1.0)
    v2, _ = p.respond_traction(t, lambda _: 11e6)
    assert np.allclose(v1, v2, rtol=1e-4)
    v3, s3, _ = p.respond_spring(t, 11e6, 1e10)           # stiff surroundings relax the pillar stress
    assert s3[-1] < s3[0] and v3[-1] < v1[-1]
    assert p.dissipation_rate(11e6) > 0


def test_mined_cell_tributary_and_backfill_share():
    p = PillarKernel(h=5.5, E=6.895e9, nu=0.2, eta=1.85e13, n=5.0, c_shape=0.04)
    cell = MinedCellKernel(p, k=0.51)
    t = np.linspace(0, 40, 41)
    r = cell.respond(t, 6.86e6)
    assert np.allclose(r["sigma_p"], 6.86e6 / 0.49, rtol=1e-6)
    fill = BackfillKernel(h_fill=5.3, gap0=0.2, E0=50e6, eps_m=0.4)
    rb = MinedCellKernel(p, k=0.51, backfill=fill).respond(t, 6.86e6)
    assert rb["sigma_b"][0] == 0.0
    assert rb["sigma_b"][-1] > 0.0 and rb["sigma_p"][-1] < r["sigma_p"][-1]
    assert rb["v"][-1] < r["v"][-1]


def test_aquifer_modes_match_terzaghi_and_closure():
    k = AquiferKernel(H_d=100.0, c_v=1.0, n_modes=200)
    zeta = np.linspace(0, 1, 4001)
    a0 = k.project(zeta, np.ones_like(zeta))
    Tv = np.array([0.1, 0.5])
    p_mean = k.mean_pressure(k.evolve(a0, Tv * 100.0 ** 2))
    assert np.allclose(1 - p_mean, laws.terzaghi_degree(Tv), atol=2e-3)
    # one reduced variable (mean pressure) is not enough: two profiles with the same mean diverge
    prof2 = np.sin(math.pi / 2 * zeta)
    prof2 *= np.trapezoid(np.ones_like(zeta), zeta) / np.trapezoid(prof2, zeta)
    res = closure.state_closure_test([np.ones_like(zeta), prof2], reduce=lambda p: [np.trapezoid(p, zeta)],
                                     future_response=lambda p: k.mean_pressure(k.evolve(k.project(zeta, p),
                                                                                       np.array([100.0, 500.0]))),
                                     tolerance=0.02, reduced_atol=1e-6)
    assert res.verdict == "STATE_INSUFFICIENT"


def test_vzt_series_barrier_is_weakest_link():
    K_salt, K_frac = 1e-13, 1e-5
    k = VZTKernel((10, 75, 31), (K_salt,) * 3, (K_frac,) * 3, [1, 1, 0])
    assert k.controlling_layer() == 2
    assert k.K_eff() < 1e-11
    k.d = [1, 1, 1]
    assert math.isclose(k.K_eff(), K_frac, rel_tol=1e-9)


# ------------------------------------------------------------------------------------------- closure / influence
def test_external_equivalence_metrics():
    a = np.array([1.0, 2.0, 3.0])
    b = np.array([1.0, 2.5, 3.0])
    assert closure.external_equivalence(a, b) == 0.5
    assert math.isclose(closure.external_equivalence(a, b, "mahalanobis", R=np.eye(3) * 0.25), 1.0)
    with pytest.raises(ValueError):
        closure.state_closure_test([1.0, 2.0], reduce=lambda x: [x], future_response=lambda x: [x], tolerance=1)


def test_sign_structure_detects_cooperative_and_not():
    n = 12
    K = 2 * np.eye(n) - 0.9 * (np.eye(n, k=1) + np.eye(n, k=-1))
    s = influence.sign_structure(K)
    assert s["z_matrix"] and s["m_matrix"] and s["cooperative_world0"]
    K2 = K + 0.1 * (np.eye(n, k=2) + np.eye(n, k=-2))
    s2 = influence.sign_structure(K2)
    assert not s2["z_matrix"] and s2["first_positive_lag"] == 2
    Ks, asym = influence.stiffness_from_compliance(np.linalg.inv(K))
    assert np.allclose(Ks, K) and asym < 1e-12


def test_knothe_fit_recovers_radius():
    x = np.linspace(-2000, 2000, 2001)
    g = 3.0 * laws.knothe_influence(x - 50.0, 400.0)
    f = influence.knothe_fit(x, g, depth=320.0)
    assert abs(f["r"] - 400.0) / 400.0 < 0.01 and abs(f["x0"] - 50.0) < 1.0
    assert abs(f["r_moment"] - 400.0) / 400.0 < 0.01 and abs(f["tan_beta"] - 0.8) < 0.01


# ------------------------------------------------------------------------------------------- gravity
def test_gravity_roles_are_separate():
    assert gravity.GRAVITY_RELATIONS["BODY_FORCE_LOADS"]["causal"] is True
    assert gravity.GRAVITY_RELATIONS["POTENTIAL_FIELD_OBSERVES"]["causal"] is False
    assert laws.LAW_SPECS["W0-L-17"].role == "PROBE" and laws.LAW_SPECS["W0-L-16"].role == "CAUSAL"
    _, sv = gravity.lithostatic_profile([100.0, 200.0], [2000.0, 2200.0])
    assert math.isclose(sv[-1], (2000 * 100 + 2200 * 200) * laws.G_STD)


def test_talwani_and_prism_against_closed_forms():
    th = np.linspace(0, 2 * math.pi, 720, endpoint=False)
    poly = np.c_[10 * np.cos(th), 100 + 10 * np.sin(th)]
    x = np.array([0.0, 60.0])
    assert np.allclose(gravity.talwani_gz(x, 0.0, poly, -2000.0),
                       gravity.horizontal_cylinder_gz(x, 10.0, 100.0, -2000.0), rtol=1e-3)
    g = gravity.prism_gz(np.array([0.0, 40.0]), 0.0, 0.0, (-1, 1, -1, 1, 99, 101), 1000.0)
    assert np.allclose(g, gravity.point_mass_gz(np.array([0.0, 40.0]), 8000.0, 100.0), rtol=1e-3)
    assert gravity.free_air_change(1.0) == pytest.approx(3.086e-6)


# ------------------------------------------------------------------------------------------- APDL decks
def _small_column():
    m = Material("S", 4e9, 0.3, 2200.0)
    return [Layer("FLOOR", 50.0, m, 4), Layer("SEAM", 5.0, m, 2), Layer("ROOF", 50.0, m, 4)]


def test_slot_model_loads_and_deck():
    mdl = LayeredSlotModel(_small_column(), "SEAM", panel=40.0, n_cells=4, half_width=200.0, dx_in=2.5, dx_max=20.0)
    f = mdl.cell_forces(1)
    roof = sum(v for nid, v in f if v < 0)
    assert math.isclose(roof, -1e6 * 10.0)            # unit traction × cell width
    assert math.isclose(sum(v for _, v in f), 0.0, abs_tol=1e-6)
    deck = mdl.deck("T")
    assert deck.count("SOLVE") == 4 and "PLANE182" in deck and ":\\" not in deck
    ax = LayeredSlotModel(_small_column(), "SEAM", panel=40.0, n_cells=4, half_width=200.0, dx_in=2.5, axisym=True)
    fr = sum(v for _, v in ax.cell_forces(0) if v < 0)
    assert math.isclose(fr, -1e6 * math.pi * 10.0 ** 2, rel_tol=1e-9)   # unit traction × disc area (360°)


def test_fine_mode_excavation_forces_balance():
    xs = np.linspace(-20, 20, 17)
    mdl = LayeredSlotModel(_small_column(), "SEAM", panel=40.0, n_cells=2, half_width=200.0, dx_max=20.0, x_panel=xs,
                           pillar_intervals=((-5.0, 5.0),), sigma0=7e6)
    fx = sum(v for _, c, v in mdl.excavation_forces() if c == "FX")
    fy = sum(v for _, c, v in mdl.excavation_forces() if c == "FY")
    assert abs(fx) < 1e-6 and abs(fy) < 1e-6


def test_rerun_never_reads_stale_outputs(tmp_path, monkeypatch):
    """A failed rerun in the same directory must not leave the previous results behind (review of PR #29)."""
    import sys

    from vkm_world.representations.mapdl_batch import run_deck
    (tmp_path / "case.inp").write_text("/BATCH\n", encoding="ascii")
    for stale in ("history.txt", "ls001.txt", "compliance.npz", "receipt.json", "case.out", "case.rst"):
        (tmp_path / stale).write_text("old", encoding="ascii")
    monkeypatch.setenv("VKM_MAPDL_EXE", sys.executable)        # python rejects '-np' → a failing "solver" run
    rec = run_deck(tmp_path, n_proc=1, timeout_s=60)
    assert rec["exit_code"] != 0
    assert (tmp_path / "case.inp").exists()
    for stale in ("history.txt", "ls001.txt", "compliance.npz", "receipt.json", "case.rst"):
        assert not (tmp_path / stale).exists()
    assert "history.txt" not in rec["result_files_sha256"]
    assert set(rec["stale_outputs_removed"]) >= {"history.txt", "ls001.txt", "compliance.npz", "receipt.json"}


def test_pillar_cell_deck_has_ladder_steps():
    m = Material("P", 6.9e9, 0.2, 2100.0, 7.58e7, 5.0)
    cell = PillarCellModel(a=27.0, w=16.8, h=5.5, t_roof=20.0, t_floor=20.0, pillar=m, roof=m, floor=m, sigma0=6.86e6,
                           history=[(1.0, 6.86e6, 4)], fill=Material("F", 50e6, 0.25, 1700.0), fill_height=5.0)
    d = cell.deck("CELL")
    for token in ("INISTATE,SET,DTYP,EPEL", "EKILL,ALL", "TB,CREEP", "CONTA172", "history,txt"):
        assert token in d
