"""Синтетические тесты формул сдвижения (vkm_world.physics.subsidence_formulas): предельные случаи, симметрия,
непрерывность кусочных формул, согласованность производных. Не используют данные СКРУ-1 и наблюдения."""
from __future__ import annotations

import math

import numpy as np
import pytest

from vkm_world.physics import subsidence_formulas as sf


def test_typical_curve_tables_are_consistent():
    curves = sf.typical_curves()
    for name, tab in curves.items():
        z = np.asarray(tab["z"])
        assert np.allclose(z, np.round(np.arange(0, 1.01, 0.1), 10)), name
        assert tab["source_id"].startswith("VKM-SRC-") and tab["page_id"].startswith(tab["source_id"] + ":p")
        if "S" in tab:
            S = np.asarray(tab["S"])
            assert S[0] == 1.0 and S[-1] == 0.0 and np.all(np.diff(S) <= 0), name
            S2 = np.asarray(tab["S2"])
            assert S2[1] < 0 < S2[-2], name  # вогнутость у максимума, выпуклость у границы
    # редакция «Указаний» (VKM-SRC-065 табл. 4) и режим 1 табл. 8.26 (VKM-SRC-037) — одни и те же S′, S″
    t4, m1 = curves["VKM_UKAZANIYA_T4_SRC065"], curves["VKM1992_T8_26_MODE1"]
    assert t4["S2"] == m1["S2"] and t4["S1_used"] == m1["S1"]
    assert t4["S1"][0] == 1.0 and t4["S1_used"][0] == 0.0  # напечатанное значение сохранено, расчётное — отдельно


def test_tabulated_slope_matches_differences_of_S():
    # S′ таблицы — модуль производной S по z (в пределах округления и «30-метрового интервала»)
    for name in ("VKM1992_T8_26_MODE1", "VKM_UKAZANIYA_T4_SRC065"):
        tab = sf.typical_curves()[name]
        S = np.asarray(tab["S"])
        S1 = np.asarray(tab.get("S1_used", tab["S1"]))
        central = -(S[2:] - S[:-2]) / 0.2
        assert np.max(np.abs(central - S1[1:-1])) < 0.35, name


def test_backfill_and_loading_factors():
    assert sf.backfill_factor(0.0, 0.18) == 1.0
    assert sf.backfill_factor(1.0, 0.18) == pytest.approx(0.18)
    assert sf.loading_factor_k(0.32) == pytest.approx(0.9)
    assert sf.loading_factor_k(0.3199999) == pytest.approx(0.9, rel=1e-6)  # непрерывность на C = 0,32
    assert sf.loading_factor_k(0.16) == pytest.approx(0.225)
    assert sf.loading_factor_k(0.1, seam="carnallite") == 1.0
    with pytest.raises(ValueError):
        sf.backfill_factor(1.2, 0.1)


def test_two_editions_of_maximum_subsidence_agree_for_rectangular_chambers():
    m, a, b = 5.5, 16.0, 11.0
    omega = a / (a + b)
    assert sf.eta_max_1992(m * a, a, b) == pytest.approx(sf.eta_max_current(m, omega, 1.0, 0.9))
    # закладка: одна и та же форма p = 1 − A(1 − B) в обеих редакциях
    assert sf.eta_max_1992(m * a, a, b, A=0.8, B=0.18) == pytest.approx(
        sf.eta_max_current(m, omega, sf.backfill_factor(0.8, 0.18), 0.9))


def test_incomplete_undermining_factor_limits_and_monotonicity():
    assert sf.incomplete_factor_f0(0.15 + 1e-9) == pytest.approx(0.0, abs=1e-3)
    assert sf.incomplete_factor_f0(1.4 - 1e-9) == pytest.approx(1.0, abs=1e-3)
    chis = np.linspace(0.16, 1.39, 50)
    f = [sf.incomplete_factor_f0(c) for c in chis]
    assert np.all(np.diff(f) > 0)
    assert sf.incomplete_factor_f0(0.1) == 0.0 and sf.incomplete_factor_f0(3.0) == 1.0


def test_undermining_chi_cap_and_regimes():
    H = 300.0
    assert sf.undermining_chi(30.0, 1e6, H) == 0.0  # выработка уже 0,15H — сдвижения нет
    # 2D-панель (D2 = ∞ → 1,4H): ширина 1,4H даёт χ = 1,12·(1,25H)/H = 1,4 — граница полной подработки
    assert sf.undermining_chi(1.4 * H - 1e-6, 1e9, H) == pytest.approx(1.12 * 1.25)
    assert sf.undermining_chi(0.8 * H, 1e9, H) < 1.4
    assert sf.undermining_chi(2.0 * H, 1e9, H) > 1.4  # обе стороны ≥ 1,4H — полная подработка, ограничение не нужно


def test_reduction_q_continuity():
    for kind, top in (("tilt", 1.20), ("curvature", 1.25), ("strain", 1.50)):
        assert sf.reduction_q(10.0, kind) == 1.0
        assert sf.reduction_q(15.0 + 1e-9, kind) == pytest.approx(1.0, abs=1e-3)
        assert sf.reduction_q(45.0 - 1e-9, kind) == pytest.approx(top, abs=1e-3)
        assert sf.reduction_q(60.0, kind) == top


def test_strain_transition_coefficient():
    assert sf.strain_transition_me(2e-4) == 0.05
    assert sf.strain_transition_me(1e-4) == pytest.approx(0.05)
    assert sf.strain_transition_me(0.0) == pytest.approx(0.14)
    assert sf.strain_transition_me(-0.5e-4) == sf.strain_transition_me(0.5e-4)


def test_time_function_misprint_is_documented():
    # как напечатано, β_z (1.5) при C = 0,40 отрицательна и не стыкуется с (1.7); с «+» — стыкуется
    printed = sf.time_fraction_ukazaniya(100.0, 0.40, "as_printed")
    fixed = sf.time_fraction_ukazaniya(100.0, 0.40, "beta15_plus")
    upper = sf.time_fraction_ukazaniya(100.0, 0.40 + 1e-9)
    assert printed < 0
    assert fixed == pytest.approx(upper, rel=2e-3)
    assert sf.time_fraction_ukazaniya(100.0, 0.32) == pytest.approx(0.4)
    # C < 0,32: k_t = 0,2·t/100, t в годах (VKM-SRC-103 с. 26 (1.12)); 500 лет — полная реализация
    assert sf.time_fraction_ukazaniya(100.0, 0.2) == pytest.approx(0.2)
    assert sf.time_fraction_ukazaniya(40.0, 0.25) == pytest.approx(0.08)


@pytest.mark.parametrize("H,eta,s1max,printed", [(320, 2.5, 2.0, 10.1), (400, 2.5, 2.0, 8.1), (250, 2.5, 2.0, 13.0),
                                                 (250, 4.0, 2.0, 20.8), (320, 2.5, 4.0, 20.3), (250, 4.0, 4.0, 42.0)])
def test_published_vkm_tilt_examples(H, eta, s1max, printed):
    """Численный пример из корпуса: VKM-SRC-077 (Барях, Тенисон 2021) табл. 2, печ. с. 61 — i_max = η·S′max/(H·a),
    a = L/H = 1,54; S′max = 2 (C = 0,4) и 4 (C = 0,7) по рис. 3 (с. 59). Формула статьи — без коэффициента q_i."""
    L = 1.54 * H
    tilt_mm_per_m = 1000 * eta * s1max / L
    assert tilt_mm_per_m == pytest.approx(printed, abs=0.06 * printed)
    # то же из реализации: максимум |i| профиля при q = 1 (ред. 1992), панель полной подработки, L = H·(ctg50° + ctg55°)
    assert sf.half_trough_length(H, 50.0, 55.0) == pytest.approx(L, rel=1e-3)


def test_vzt_salt_layer_limit_scaling():
    base = sf.vzt_salt_layer_limit(300.0, 0.0, 30.0, 5.0, 0.4)
    assert base == pytest.approx(2 * 0.002 * 270.0 ** 2 / (30 * 5.0 * 0.4))
    assert sf.vzt_salt_layer_limit(300.0, 0.0, 7.0, 5.0, 0.4) > base  # плавная мульда — допускает более толстый слой
    assert sf.vzt_salt_layer_limit(300.0, 0.0, 30.0, 5.0, 0.4, A=1.0, B=0.2) == pytest.approx(base / 0.2)


def test_half_trough_length():
    assert sf.half_trough_length(100.0, 55.0, 55.0) == pytest.approx(2 * 100 / math.tan(math.radians(55)))
    assert sf.half_trough_length(100.0, 50.0, 55.0) > sf.half_trough_length(100.0, 55.0, 55.0)


@pytest.mark.parametrize("edition,curve", [("current", "VKM_UKAZANIYA_T4_SRC065"), ("1992", "VKM1992_T8_26_MODE2")])
def test_typical_profile_symmetry_flat_bottom_and_boundary(edition, curve):
    H, x1, x2, eta = 300.0, -400.0, 400.0, 2.0
    x = np.linspace(-1200, 1200, 2401)
    p = sf.typical_curve_profile(x, x1, x2, H, eta, curve, 55.0, 55.0, edition=edition)
    assert p.meta["mode"] == "full"
    assert np.allclose(p.s, p.s[::-1]) and np.allclose(p.tilt, -p.tilt[::-1]) and np.allclose(p.curvature,
                                                                                         p.curvature[::-1])
    assert p.s[np.abs(x) < 400 - H / math.tan(math.radians(55))].min() == pytest.approx(eta)  # плоское дно
    boundary = 400 + H / math.tan(math.radians(55))
    assert np.all(p.s[np.abs(x) >= boundary] == 0.0)
    # знак наклона совпадает со знаком численной производной там, где он заметен
    g = np.gradient(p.s, x)
    big = np.abs(p.tilt) > 0.2 * np.abs(p.tilt).max()
    assert np.all(np.sign(g[big]) == np.sign(p.tilt[big]))
    # растяжение у границы, сжатие у дна (знак ε = знак K)
    assert np.all(np.sign(p.eps_h[p.curvature != 0]) == np.sign(p.curvature[p.curvature != 0]))


def test_tabulated_extrema_are_interpolation_knots():
    """Экстремумы табл. 8.26 (звёздочки): S′ = 4,0 и 5,5 при Z = 0,35, S″ = +26 при Z = 0,45 — узлы интерполяции."""
    c = sf.typical_curves()
    z = np.array([0.35, 0.45])
    assert sf._interp(c["VKM1992_T8_26_MODE2"], "S1", z)[0] == pytest.approx(4.0)
    assert sf._interp(c["VKM1992_T8_26_MODE2"], "S2", z)[1] == pytest.approx(26.0)
    assert sf._interp(c["VKM1992_T8_26_MODE3"], "S1", z)[0] == pytest.approx(5.5)
    # обычные узлы не сдвигаются
    assert sf._interp(c["VKM1992_T8_26_MODE2"], "S1", np.array([0.3, 0.4])) == pytest.approx([2.4, 3.5])
    # наибольший наклон профиля = η·S′max/L при z = 0,35
    H, eta = 320.0, 2.0
    L = sf.half_trough_length(H, 50, 55)
    xl = -300 + H / math.tan(math.radians(55))
    x = np.array([xl - 0.35 * L])
    p = sf.typical_curve_profile(x, -300, 300, H, eta, "VKM1992_T8_26_MODE3", 50, 55, edition="1992",
                                 strain_method="F")
    assert p.tilt[0] == pytest.approx(eta * 5.5 / L)


def test_matlab_check_rejects_empty_or_partial_files(tmp_path):
    """Сверка MATLAB ↔ Python (scripts/formulas_vs_fem.py) не «проходит» на пустом, неполном или чужом файле."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "formulas_vs_fem", Path(__file__).resolve().parents[2] / "scripts" / "formulas_vs_fem.py")
    fvf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fvf)
    x = np.linspace(-500, 500, 41)
    norm = {"K": sf.knothe_profile(x, -100, 100, 300.0, 1.0, 55.0)}
    header = "case,x,s,tilt,curvature,u_h,eps_h\n"

    def rows(name, prof, n=None):
        n = len(x) if n is None else n
        return "".join(f"{name},{x[j]:.4f},{prof.s[j]:.12e},{prof.tilt[j]:.12e},{prof.curvature[j]:.12e},"
                       f"{prof.u_h[j]:.12e},{prof.eps_h[j]:.12e}\n" for j in range(n))
    empty, partial, alien, full = (tmp_path / f for f in ("e.csv", "p.csv", "a.csv", "f.csv"))
    empty.write_text(header, encoding="utf-8")
    partial.write_text(header + rows("K", norm["K"], 10), encoding="utf-8")
    alien.write_text(header + rows("OTHER", norm["K"]), encoding="utf-8")
    full.write_text(header + rows("K", norm["K"]), encoding="utf-8")
    assert not fvf.compare_with_matlab(empty, norm, x)["pass"]
    assert not fvf.compare_with_matlab(partial, norm, x)["pass"]
    assert not fvf.compare_with_matlab(alien, norm, x)["pass"]
    assert not fvf.compare_with_matlab(tmp_path / "missing.csv", norm, x)["pass"]
    good = fvf.compare_with_matlab(full, norm, x)
    assert good["pass"] and good["problems"] == []


def test_maxima_are_strict_json():
    """Величина, не заданная методом (u у «Указаний»), — None, а не NaN: сводки должны быть строгим JSON."""
    import json
    x = np.linspace(-1000, 1000, 401)
    p = sf.typical_curve_profile(x, -300, 300, 320.0, 2.0, "VKM_UKAZANIYA_T4_SRC065", 55, 55)
    m = p.maxima()
    assert m["u_h_max_m"] is None and m["eps_h_max"] > 0
    json.dumps(m, allow_nan=False)


def test_typical_profile_incomplete_undermining_peaks_at_centre():
    H = 300.0
    x = np.linspace(-800, 800, 1601)
    p = sf.typical_curve_profile(x, -100.0, 100.0, H, 1.0, "VKM_UKAZANIYA_T4_SRC065", 55.0, 55.0)
    assert p.meta["mode"] == "incomplete"
    assert p.s[800] == pytest.approx(1.0) and p.s.argmax() == 800
    assert p.meta["L_m"] == pytest.approx(100 + H / math.tan(math.radians(55)))


def test_knothe_limits_volume_and_derivatives():
    H, beta, w = 300.0, 55.0, 1.5
    r = H / math.tan(math.radians(beta))
    x = np.linspace(-3000, 3000, 12001)
    wide = sf.knothe_profile(x, -1500, 1500, H, w, beta)
    assert wide.s[6000] == pytest.approx(w, rel=1e-9)  # ширина ≫ r — полная подработка
    D = 400.0
    p = sf.knothe_profile(x, -D / 2, D / 2, H, w, beta, B_avershin=0.16 * H)
    assert np.trapezoid(p.s, x) == pytest.approx(w * D, rel=1e-6)  # ядро нормировано: объём мульды = W_max·D
    assert np.allclose(p.s, p.s[::-1])
    assert np.allclose(np.gradient(p.s, x), p.tilt, atol=1e-6 * w / r * 50)
    assert np.allclose(np.gradient(p.tilt, x), p.curvature, atol=1e-4 * w / r ** 2 * 50)
    assert np.allclose(p.u_h, 0.16 * H * p.tilt)
    # край панели в полубесконечном пределе: оседание над краем = W_max/2
    edge = sf.knothe_profile(np.array([0.0]), 0.0, 1e6, H, w, beta)
    assert edge.s[0] == pytest.approx(w / 2)


def test_python_matches_matlab_reference():
    """Две независимые реализации (Python и MATLAB пакет matlab/+vkm/+subs) дают одни и те же профили.

    Эталон пишет matlab/tests/export_reference_profiles.m (R2025b, 23.10.2026); здесь те же случаи на Python."""
    import csv
    from pathlib import Path
    ref = Path(__file__).with_name("data") / "subsidence_matlab_reference.csv"
    rows = list(csv.DictReader(ref.open(encoding="utf-8")))
    x = np.arange(-1500.0, 1500.0 + 1e-9, 25.0)
    H, eta = 320.0, 2.0
    cases = {
        "T4_current_me": sf.typical_curve_profile(x, -300, 300, H, eta, "VKM_UKAZANIYA_T4_SRC065", 55, 55),
        "M2_1992_F": sf.typical_curve_profile(x, -300, 300, H, eta, "VKM1992_T8_26_MODE2", 50, 55, edition="1992",
                                              strain_method="F"),
        "M3_1992_avershin": sf.typical_curve_profile(x, -300, 300, H, eta, "VKM1992_T8_26_MODE3", 50, 55,
                                                     edition="1992", strain_method="avershin", B_avershin=0.16 * H),
        "T4_incomplete": sf.typical_curve_profile(x, -100, 100, H, eta, "VKM_UKAZANIYA_T4_SRC065", 55, 55),
        "knothe_55": sf.knothe_profile(x, -300, 300, H, eta, 55, B_avershin=0.16 * H),
    }
    for name, prof in cases.items():
        got = [r for r in rows if r["case"] == name]
        assert len(got) == len(x), name
        for key in ("s", "tilt", "curvature", "u_h", "eps_h"):
            m = np.array([float(r[key]) for r in got])
            p = getattr(prof, key)
            assert np.array_equal(np.isnan(m), np.isnan(p)), (name, key)
            if np.all(np.isnan(p)):
                continue  # величина не задана методом (например, u для «Указаний») в обеих реализациях
            scale = max(np.nanmax(np.abs(p)), 1e-30)
            assert np.nanmax(np.abs(m - p)) <= 1e-9 * scale, (name, key)


def test_profile_operators():
    x = np.linspace(-10, 10, 201)
    s = np.maximum(0.0, 1.0 - np.abs(x) / 5.0)
    assert sf.trough_edges(x, s, 0.5) == pytest.approx((-2.5, 2.5))
    assert sf.trough_edges(x, s, 2.0) is None
    prof = sf.profile_from_samples(x, s, u_h=0.1 * np.gradient(s, x))
    assert prof.maxima()["s_max_m"] == 1.0
    assert sf.angle_from_edge(100.0, 100.0) == pytest.approx(45.0)
    # разности на базе: для параболы s = c·x² наклон 2cx и кривизна 2c точны при любой базе
    xx = np.linspace(-100, 100, 401)
    par = sf.profile_from_samples(xx, 1e-4 * xx ** 2, u_h=0.5 * xx, base=15.0)
    inner = np.abs(xx) < 80
    assert np.allclose(par.tilt[inner], 2e-4 * xx[inner]) and np.allclose(par.curvature[inner], 2e-4)
    assert np.allclose(par.eps_h[inner], 0.5) and par.meta["base_m"] == 15.0
