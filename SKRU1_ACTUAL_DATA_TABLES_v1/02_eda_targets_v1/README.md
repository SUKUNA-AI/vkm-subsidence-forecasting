# SKRU-1 v3.2 — EDA и target specification v1

Пакет содержит воспроизводимый EDA, formal target frames, feature contract, early-warning labels, профильные outputs, sanity baselines, issue register и validation checks.

Главный файл для обучения primary regression: `target_tables/next_planned_features.csv` + строки `target_available=True` из `target_tables/next_planned_operational_targets.csv`.

`evaluation_only`-таблицы предназначены только для проверки synthetic consistency и не передаются estimator.
