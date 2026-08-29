# Независимая проверка данных и базовых моделей СКРУ-1 v3.1

## Вердикт

**CONDITIONAL GO для разработки baseline-пайплайна.**

**NO-GO для финального тюнинга сложной модели, производственных выводов и заявления реальной точности.**

Табличная целостность, пространственное покрытие, формулы маркшейдерских производных и синтетическая метрология прошли проверку. Основной блокирующий слой — временной генератор: 91 из 98 рабочих реперов маркированы как ускоряющиеся, в тестовом периоде большинство уже затухает, а следующий цикл почти полностью определяется последней скоростью.

## Ключевые числа

- Таблиц: 49; строк по всем уровням: 109,489.
- Формульные расхождения: tilt 5.373e-13; curvature 7.772e-15; rate 1.187e-10; strain 1.016e-13.
- Нивелирование: RMSE 0.773 мм, coverage95 98.8%.
- GNSS: RMSE 4.511 мм, coverage95 97.9%.
- InSAR: RMSE 8.181 мм, coverage95 97.8%.
- Gross-error recall: 86.2%; пропущено 4 внедрённых ошибки.
- Moran I: settlement 0.908, k_z,T 0.959, k_o 0.567, seismic 0.950.
- Последняя скорость → следующая истинная скорость: r=0.9948.
- Наблюдаемое → истинное приращение: r=0.99977.
- Шумовой пол 2025: MAE 0.781 мм.

## Временной тест 2025

| Модель | MAE hidden truth, мм | MAE observed, мм |
|---|---:|---:|
| Kalman q=100 | 0.548 | 1.105 |
| Mean last 3 rates | 0.612 | 1.145 |
| Last rate | 0.938 | 1.434 |
| HGB annualized rate | 2.332 | 2.553 |
| HGB direct increment | 23.192 | 23.217 |

Kalman выбран как контрольный baseline, потому что учитывает неравные интервалы и uncertainty. Conformal-интервал 90%: prediction ±2.993 мм; test coverage 95.6%.

## Главные дефекты данных

1. **Regime imbalance:** {'accelerating': 91, 'uniform_creep': 7}.
2. **Late-stage mismatch:** median velocity slope 2023–2025 = -0.680 мм/год².
3. **Extreme nominal tail:** >250 мм/год 17.1%; >400 мм/год 9.9%; max 656.6.
4. **Focused campaigns are fake:** all cycles observe all 126 points.
5. **Leakage:** terminal settlement map and generator parameters are exported near model features.
6. **Static provenance lost at point level.**
7. **GNSS selection bias:** top 30 WORK settlement points + 12 REF.
8. **InSAR pseudo-replication:** 100 points inherit only 43 survey trajectories.
9. **Stress design bias:** 36 scenarios on 12 high-anchor points.
10. **No real SKRU-1 cycles:** production claims remain impossible.

## Stress test

Best oracle-QC MAE = 3.832 мм (Last observed rate). Best F1 for early acceleration jump = 0.378. Thus low nominal MAE does not prove early-warning capability.

## Required baseline formulation

Predict annualized rate and integrate over the known next-cycle horizon:

v_hat(k+1) -> delta_eta_hat(k+1) = v_hat(k+1) * delta_t(k+1).

Direct raw-increment models fail when cycle spacing changes.

## Recommendation

Keep only two mandatory controls:

1. local linear-trend Kalman q=100;
2. mean of last three annualized rates.

HGB remains diagnostic. Do not tune it deeply until data v3.2 adds balanced regimes, time-varying process stages, rare localized acceleration, genuine focused campaigns, long gaps, point-level provenance/uncertainty, independent InSAR field and stratified GNSS.

Detailed checks, metrics, predictions and feature contract are in `tables/`; plots in `figures/`; model configs in `models/`.
