"""Расчётные формулы сдвижения земной поверхности из корпуса ВКМ (2D, главное сечение мульды).

Модуль реализует три семейства формул, чтобы сравнивать с ними численные (FEM) расчёты:

1. Нормативный метод типовых кривых ВКМ, две редакции:
   * «Указания» в действующей версии — по изложению VKM-SRC-065 (Щеголков, 2026) с. 28–33, 71 и VKM-SRC-012
     (Лебедева, 2023) с. 54–55: η = m0·ω·p·k (1.1), неполная подработка f0(χ) (1.9)–(1.10), наклон, кривизна,
     горизонтальная деформация (1.16)–(1.20), коэффициенты приведения к 15 м (1.15), типовая функция — табл. 4;
   * Методическое руководство 1990/1992 — VKM-SRC-037 §8.5.1 (печ. с. 337–339): η0 = 0,9·ΣS/(a + b) (8.135),
     закладка (8.136), ξ0 = a·η0 (8.137), (8.138)–(8.142), табл. 8.26 (три режима по C и A).
2. Функция влияния Кноте / Литвинишина (гауссова кривая, учебник VKM-SRC-208 с. 67, GENERAL_METHOD, угольные
   месторождения — для ВКМ это ANALOGUE) с горизонтальными сдвижениями по гипотезе Авершина U = B·i (там же, с. 66–67).
3. Служебные операторы: численные производные профиля, границы мульды по уровню, углы.

Все формулы — нормативная эмпирика или учебная теория, а не наблюдения. Тексты формул в корпусе
AUTO_EXTRACTED_UNREVIEWED; использованные здесь формы сверены по изображениям страниц (23.10.2026), отклонения от
напечатанного отмечены в docstring как DERIVATION. Единицы: метры, радианы внутри, углы на входе — в градусах.
Знаки: оседание s > 0 вниз; наклон i = ds/dx; кривизна K = d²s/dx² (K > 0 — выпуклость поверхности, растяжение);
горизонтальное сдвижение u > 0 на восток (+x); горизонтальная деформация ε = du/dx (> 0 — растяжение).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

DATA_FILE = Path(__file__).with_name("data") / "subsidence_typical_curves.json"
DELTA_Z = 0.1  # шаг аргумента типовых функций (табл. 4 VKM-SRC-065, табл. 8.26 VKM-SRC-037)


@lru_cache(maxsize=1)
def typical_curves() -> dict:
    """Таблицы типовых функций со ссылками на источники (data/subsidence_typical_curves.json)."""
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))["curves"]


def cot_deg(angle_deg: float) -> float:
    return 1.0 / math.tan(math.radians(angle_deg))


# ----------------------------------------------------------------------------------------------- амплитуда

def backfill_factor(A: float, B: float) -> float:
    """p = 1 − A·(1 − B): A — степень заполнения камер закладкой, B — коэффициент усадки закладки.

    VKM-SRC-065 с. 29 (к формуле (1.1)); VKM-SRC-037 (8.136). Без закладки p = 1.
    """
    if not (0.0 <= A <= 1.0 and 0.0 <= B <= 1.0):
        raise ValueError("A and B must lie in [0, 1]")
    return 1.0 - A * (1.0 - B)


def loading_factor_k(C: float, seam: str = "sylvinite") -> float:
    """Коэффициент k формулы (1.1) VKM-SRC-065 с. 29 (= множитель 0,9·{C/0,32}² формул (2.1)–(2.2) VKM-SRC-012 с. 54).

    1,0 — карналлитовые пласты; 0,9 — сильвинит и каменная соль при C ≥ 0,32; 0,9·(C/0,32)² — при C < 0,32.
    C — степень нагружения междукамерных целиков.
    """
    if seam == "carnallite":
        return 1.0
    if seam != "sylvinite":
        raise ValueError("seam must be 'sylvinite' (incl. rock salt) or 'carnallite'")
    if C < 0:
        raise ValueError("C must be non-negative")
    return 0.9 if C >= 0.32 else 0.9 * (C / 0.32) ** 2


def eta_max_current(m0: float, omega: float, p: float = 1.0, k: float = 0.9) -> float:
    """Максимальное оседание к концу процесса от одного пласта, «Указания» (действующая версия):
    η = m0·ω·p·k (1.1) VKM-SRC-065 с. 29; то же — (2.1)–(2.2) VKM-SRC-012 с. 54 (k = 0,9 при C ≥ 0,32).

    m0 — вынимаемая мощность, м; ω — коэффициент извлечения (доля площади сечения камер в сечении нетронутого массива
    в пределах m0 и межосевого расстояния); p — влияние закладки; k — loading_factor_k. Многопластовая выемка —
    сумма по пластам (VKM-SRC-065 с. 28).
    """
    if m0 < 0 or not (0 <= omega <= 1):
        raise ValueError("m0 >= 0 and 0 <= omega <= 1 required")
    return m0 * omega * p * k


def eta_max_1992(chamber_area_sum: float, a: float, b: float, A: float = 0.0, B: float = 0.0) -> float:
    """Конечное оседание по руководству 1990/1992, VKM-SRC-037 с. 337–338:
    η0 = 0,9·ΣS_i/(a + b) (8.135); с закладкой η0* = 0,9·S/(a + b)·[1 − A(1 − B)] (8.136; числитель напечатан «5»).

    S_i — площадь сечения очистной камеры, м²; a, b — ширина камер и целиков, м. Для прямоугольной камеры высотой m
    ΣS/(a + b) = m·a/(a + b) = m·ω (DERIVATION, XFV-SF-009).
    """
    if a <= 0 or b < 0:
        raise ValueError("a > 0 and b >= 0 required")
    return 0.9 * chamber_area_sum / (a + b) * backfill_factor(A, B)


def horizontal_max_1992(eta0: float, a_ratio: float) -> float:
    """ξ0 = a·η0 (8.137) VKM-SRC-037 с. 338; переходной коэффициент a = 0,2–0,26 (эмпирический)."""
    return a_ratio * eta0


def undermining_chi(D1: float, D2: float, H: float) -> float:
    """Коэффициент условий подработки χ = 1,12·√((D1 − 0,15H)(D2 − 0,15H))/H (1.10) VKM-SRC-065 с. 30.

    При D1 < 1,4H и D2 > 1,4H берётся D2 = 1,4H (там же). χ ≤ 0,15 — сдвижения нет; 0,15 < χ < 1,4 — неполная
    подработка; χ ≥ 1,4 — полная (с. 31). Для 2D-сечения бесконечная панель вдоль оси: D2 = ∞ → 1,4H.
    """
    if H <= 0:
        raise ValueError("H must be positive")
    d1, d2 = sorted((D1, D2))
    if d1 < 1.4 * H and d2 > 1.4 * H:
        d2 = 1.4 * H
    a, b = d1 - 0.15 * H, d2 - 0.15 * H
    if a <= 0 or b <= 0:
        return 0.0
    return 1.12 * math.sqrt(a * b) / H


def incomplete_factor_f0(chi: float) -> float:
    """f0 = 0,5948·(3,7694·(1,6708·χ^0,25 − 1) − χ) (1.9) VKM-SRC-065 с. 30, для 0,15 < χ < 1,4.

    Вне интервала: 0 (сдвижения нет) и 1 (полная подработка); на концах интервала формула сама даёт ≈ 0 и ≈ 1.
    """
    if chi <= 0.15:
        return 0.0
    if chi >= 1.4:
        return 1.0
    return 0.5948 * (3.7694 * (1.6708 * chi ** 0.25 - 1.0) - chi)


def time_fraction_ukazaniya(t: float, C: float, variant: str = "as_printed") -> float:
    """Доля конечного оседания ко времени t по «Указаниям» (1.2)–(1.7) VKM-SRC-065 с. 29–30 (только справочно).

    C < 0,32: 0,002·t (1.2); C ≥ 0,32: β_z·(0,01·t)^α_z (1.3), α_z, β_z — (1.4)–(1.7).
    Единицы t на странице не названы — UNKNOWN. Как напечатано, β_z (1.5) отрицательна при C = 0,40, а (1.7) уходит
    в минус при C > 0,60; вариант «beta15_plus» ставит «+» в (1.5) — тогда β_z непрерывна при C = 0,40 (0,852 и
    0,8525; DERIVATION). Знак в (1.7) непрерывностью не определяется — оставлен как напечатан.
    """
    if C < 0.32:
        return 0.002 * t
    if C <= 0.40:
        alpha = 1.0 - 11.9509 * (C - 0.32) ** 1.6803
        sign = 1.0 if variant == "beta15_plus" else -1.0
        beta = 0.4 + sign * 2.4616 * (C - 0.32) ** 0.6706
    elif C < 0.70:
        alpha = 0.8285 - 1.0440 * (C - 0.40) ** 0.5122
        beta = 0.8525 - 4.2500 * (C - 0.40)
    else:
        raise ValueError("C >= 0.70 is outside (1.4)–(1.7)")
    return beta * (0.01 * t) ** alpha


def vzt_salt_layer_limit(H: float, h: float, s2_max: float, m: float, omega: float, A: float = 0.0, B: float = 0.0,
                         eps_lim: float = 0.002) -> float:
    """Предельная мощность слоя каменной соли ВЗТ без трещины разрыва (8.144) VKM-SRC-037 печ. с. 341:
    [m_cj] = 2[ε_cj](0,9H + 0,65h_cj)² / (S″(Z)max·m·ω·(1 − A + A·B)).

    h — расстояние от слоя до кровли верхнего отрабатываемого пласта, м; s2_max — S″max по табл. 8.27
    (7 / 30 / 50 по режиму целиков); [ε_cj] = 0,002 без экспериментальных данных (норматив). Однопластовый вариант.
    """
    if s2_max <= 0 or m <= 0 or omega <= 0:
        raise ValueError("s2_max, m, omega must be positive")
    return 2.0 * eps_lim * (0.9 * H + 0.65 * h) ** 2 / (s2_max * m * omega * (1.0 - A + A * B))


# ------------------------------------------------------------------------------- геометрия и приведение

def half_trough_length(H: float, delta0_deg: float, psi_deg: float) -> float:
    """Длина краевой части (полумульды) при полной подработке L = H·(ctg δ0 + ctg ψ).

    VKM-SRC-037 с. 338 (δ0 = 50°, ψ = 55° — ред. 1990/1992); VKM-SRC-012 с. 54 (δ0 = 55°, ψ = 55° у постоянных
    границ, полная подработка — ред. 2022); VKM-SRC-065 (1.44) — та же форма с ψ0 при неполной подработке.
    """
    return H * (cot_deg(delta0_deg) + cot_deg(psi_deg))


def reduction_q(l0: float, kind: str) -> float:
    """Коэффициент приведения к 15-метровому интервалу q = 1 + β·√(l0/15 − 1) (1.15) VKM-SRC-065 с. 32.

    β = 0,1414 (наклон), 0,1768 (кривизна), 0,3535 (горизонтальная деформация); q = 1 при l0 ≤ 15 м;
    при l0 ≥ 45 м — 1,20 / 1,25 / 1,50.
    """
    beta = {"tilt": 0.1414, "curvature": 0.1768, "strain": 0.3535}[kind]
    if l0 <= 15.0:
        return 1.0
    if l0 >= 45.0:
        return {"tilt": 1.20, "curvature": 1.25, "strain": 1.50}[kind]
    return 1.0 + beta * math.sqrt(l0 / 15.0 - 1.0)


def strain_transition_me(K: float) -> float:
    """m_e — коэффициент перехода от кривизны к горизонтальной деформации (к (1.20) VKM-SRC-065 с. 33):
    0,05 при K·10⁴ ≥ 1; 0,05 + 0,09·(1 − K·10⁴)^3,8 при K·10⁴ < 1 (K в 1/м, берётся по модулю)."""
    k4 = abs(K) * 1e4
    return 0.05 if k4 >= 1.0 else 0.05 + 0.09 * (1.0 - k4) ** 3.8


# ----------------------------------------------------------------------------------------------- профили

@dataclass
class Profile:
    """Профиль главного сечения: x, оседание s, наклон i, кривизна K, сдвижение u, деформация ε (СИ)."""
    x: np.ndarray
    s: np.ndarray
    tilt: np.ndarray
    curvature: np.ndarray
    u_h: np.ndarray
    eps_h: np.ndarray
    meta: dict = field(default_factory=dict)

    def maxima(self) -> dict:
        return {"s_max_m": float(np.max(self.s)), "tilt_max": float(np.max(np.abs(self.tilt))),
                "curvature_max_per_m": float(np.max(np.abs(self.curvature))),
                "u_h_max_m": float(np.max(np.abs(self.u_h))), "eps_h_max": float(np.max(np.abs(self.eps_h)))}


def _interp(table: dict, key: str, z: np.ndarray) -> np.ndarray:
    return np.interp(z, np.asarray(table["z"], float), np.asarray(table[key], float))


def _flank_coordinates(x, x1, x2, H, delta0_deg, psi_deg):
    """z на краевых частях и знак фланга (+1 западный, −1 восточный, 0 — дно) для панели [x1, x2]."""
    D = x2 - x1
    if D <= 0:
        raise ValueError("panel must have x2 > x1")
    inner = H * cot_deg(psi_deg)
    if D >= 2.0 * inner:  # полная подработка: плоское дно между точками полных сдвижений
        L = half_trough_length(H, delta0_deg, psi_deg)
        xl, xr = x1 + inner, x2 - inner
        mode = "full"
    else:  # MODEL_CHOICE: максимум в центре, граница по δ0 (L = H·ctg δ0 + D/2: VKM-SRC-003 (4.13), VKM-SRC-065 с. 62)
        L = H * cot_deg(delta0_deg) + 0.5 * D
        xl = xr = 0.5 * (x1 + x2)
        mode = "incomplete"
    z = np.zeros_like(x, dtype=float)
    side = np.zeros_like(x, dtype=float)
    left, right = x < xl, x > xr
    z[left] = (xl - x[left]) / L
    z[right] = (x[right] - xr) / L
    side[left], side[right] = 1.0, -1.0
    return np.clip(z, 0.0, 1.0), side, L, mode


def typical_curve_profile(x, x1: float, x2: float, H: float, eta_m: float, curve: str,
                          delta0_deg: float, psi_deg: float, *, edition: str = "current",
                          strain_method: str = "me", B_avershin: float | None = None) -> Profile:
    """Профиль по методу типовых кривых для 2D-панели [x1, x2] (главное сечение, конец процесса сдвижения).

    edition = "current" («Указания» по VKM-SRC-065): s = η·S(z) (1.16); i = η·S′·q_i/L (1.17);
    K = η·S″·q_k/L² (1.18); ε = m_e·K·L (1.20); q — по (1.15) при l0 = L·Δz (с. 33).
    edition = "1992" (VKM-SRC-037 §8.5.1): s = η0·S(Z); i = η0·S′/L — в (8.138) напечатано L² (размерная опечатка,
    FC-042; принято /L, как в (1.17)); K = η0·S″/L² (8.139); без коэффициентов q (значения таблицы относятся к
    интервалу 30 м). strain_method = "F": u = η0·F(Z), ε = η0·F′(Z)/L (8.141)–(8.142) по столбцам «Итого» (подпись
    столбцов — интерпретация). strain_method = "avershin": u = B·i, ε = B·K (гипотеза Авершина, VKM-SRC-208 с. 66–67).
    Между узлами таблицы — линейная интерполяция (MODEL_CHOICE). Неполная подработка — см. _flank_coordinates.
    """
    x = np.asarray(x, dtype=float)
    tab = typical_curves()[curve]
    z, side, L, mode = _flank_coordinates(x, x1, x2, H, delta0_deg, psi_deg)
    S = _interp(tab, "S", z)
    S1 = _interp(tab, "S1_used" if "S1_used" in tab else "S1", z)
    S2 = _interp(tab, "S2", z)
    flank = side != 0
    if edition == "current":
        l0 = L * DELTA_Z
        qi, qk = reduction_q(l0, "tilt"), reduction_q(l0, "curvature")
    elif edition == "1992":
        qi = qk = 1.0
    else:
        raise ValueError("edition must be 'current' or '1992'")
    s = eta_m * S
    tilt = np.where(flank, side * eta_m * S1 * qi / L, 0.0)
    K = np.where(flank, eta_m * S2 * qk / L ** 2, 0.0)
    if strain_method == "me":
        me = np.array([strain_transition_me(k) for k in K])
        eps = me * K * L
        u = np.full_like(x, np.nan)
    elif strain_method == "F":
        itogo = typical_curves()["VKM1992_T8_26_ITOGO"]
        u = np.where(flank, side * eta_m * _interp(itogo, "F", z), 0.0)
        # F′ табл. ≈ −dF/dz: «−» у центра (сжатие), «+» у границы (растяжение) — совпадает со знаком ε здесь
        eps = np.where(flank, eta_m * _interp(itogo, "F1", z) / L, 0.0)
    elif strain_method == "avershin":
        if B_avershin is None:
            raise ValueError("B_avershin required")
        u, eps = B_avershin * tilt, B_avershin * K
    else:
        raise ValueError("strain_method must be 'me', 'F' or 'avershin'")
    meta = {"method": f"typical_curves/{edition}", "curve": curve, "L_m": L, "mode": mode, "q_tilt": qi,
            "q_curvature": qk, "delta0_deg": delta0_deg, "psi_deg": psi_deg, "H_m": H, "eta_m": eta_m,
            "strain_method": strain_method}
    return Profile(x, s, tilt, K, u, eps, meta)


def knothe_profile(x, x1: float, x2: float, H: float, w_max: float, beta_deg: float,
                   B_avershin: float | None = None) -> Profile:
    """Функция влияния Кноте / Литвинишина (гауссова), 2D-панель [x1, x2] — VKM-SRC-208 с. 67 (GENERAL_METHOD):
    W(x) = W_max/r·∫ exp(−π(x − ξ)²/r²) dξ по выработке, r = H/tg β (радиус главных влияний, β — граничный угол).

    Интеграл по панели: W = W_max/2·[erf(√π(x − x1)/r) − erf(√π(x − x2)/r)] (DERIVATION). Производные — аналитические.
    Горизонтальные сдвижения — по Авершину U = B·i (если задан B_avershin), иначе NaN.
    """
    x = np.asarray(x, dtype=float)
    r = H / math.tan(math.radians(beta_deg))
    sp = math.sqrt(math.pi)
    erf = np.vectorize(math.erf)
    a1, a2 = (x - x1) / r, (x - x2) / r
    s = 0.5 * w_max * (erf(sp * a1) - erf(sp * a2))
    g1, g2 = np.exp(-math.pi * a1 ** 2), np.exp(-math.pi * a2 ** 2)
    tilt = w_max / r * (g1 - g2)
    K = w_max / r ** 2 * (-2.0 * math.pi) * (a1 * g1 - a2 * g2)
    if B_avershin is None:
        u = np.full_like(x, np.nan)
        eps = np.full_like(x, np.nan)
    else:
        u, eps = B_avershin * tilt, B_avershin * K
    meta = {"method": "knothe", "r_m": r, "beta_deg": beta_deg, "H_m": H, "w_max": w_max}
    return Profile(x, s, tilt, K, u, eps, meta)


# ------------------------------------------------------------------------------------- служебные операторы

def profile_from_samples(x, s, u_h=None, meta: dict | None = None, base: float | None = None) -> Profile:
    """Профиль из численного решения (например, FEM).

    base = None — центральные разности по узлам (np.gradient). base = b (м) — разности на базе b, как у реперов
    профильной линии: i = [s(x + b/2) − s(x − b/2)]/b, K = [s(x + b) − 2s(x) + s(x − b)]/b²,
    ε = [u(x + b/2) − u(x − b/2)]/b (значения между узлами — линейная интерполяция). Нормативные показатели
    «Указаний» приведены к интервалу 15 м (VKM-SRC-065 (1.15)); приведение модельных деформаций к базе 15 м —
    конвенция MM-OBS-002 (VKM-SRC-038 с. 6).
    """
    x = np.asarray(x, dtype=float)
    s = np.asarray(s, dtype=float)
    u = None if u_h is None else np.asarray(u_h, dtype=float)
    if base is None:
        tilt = np.gradient(s, x)
        K = np.gradient(tilt, x)
        eps = None if u is None else np.gradient(u, x)
    else:
        h = 0.5 * base

        def at(v, xx):
            return np.interp(xx, x, v)
        tilt = (at(s, x + h) - at(s, x - h)) / base
        K = (at(s, x + base) - 2.0 * s + at(s, x - base)) / base ** 2
        eps = None if u is None else (at(u, x + h) - at(u, x - h)) / base
    if u is None:
        u = np.full_like(x, np.nan)
        eps = np.full_like(x, np.nan)
    return Profile(x, s, tilt, K, u, eps, dict(meta or {}) | ({"base_m": base} if base else {}))


def trough_edges(x, s, level: float) -> tuple[float, float] | None:
    """Крайние точки, где s пересекает уровень level (линейная интерполяция); None, если максимум ниже уровня."""
    x = np.asarray(x, dtype=float)
    s = np.asarray(s, dtype=float)
    above = np.nonzero(s >= level)[0]
    if above.size == 0:
        return None
    i0, i1 = int(above[0]), int(above[-1])

    def cross(i, j):
        return x[i] + (level - s[i]) * (x[j] - x[i]) / (s[j] - s[i]) if s[j] != s[i] else x[i]
    xl = cross(i0 - 1, i0) if i0 > 0 else x[0]
    xr = cross(i1, i1 + 1) if i1 < len(x) - 1 else x[-1]
    return float(xl), float(xr)


def angle_from_edge(H: float, distance_outside: float) -> float:
    """Угол (°) линии от края выработки (на глубине H) до точки на поверхности в distance_outside за краем."""
    return math.degrees(math.atan2(H, distance_outside))
