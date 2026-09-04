#!/usr/bin/env python
"""Build Russian Gate C1 report and model catalog from validated artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable
from uuid import uuid4

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_RELATIVE = Path("artifacts/model_selection/t1_gate_c1_compact_screen_v1")
REPORT_RELATIVE = Path("docs/reports/GATE_C1_COMPACT_SEQUENCE_SCREEN_RU.md")
CATALOG_RELATIVE = Path("docs/governance/MODEL_CATALOG_C1.md")
NOTEBOOK_RELATIVE = Path("notebooks/09_gate_c1_compact_sequence_screen.ipynb")
DEEP_MODELS = (
    "C01_compact_gru",
    "C02_compact_lstm",
    "C03_causal_tcn",
    "C04_probabilistic_gru_student_t",
)
COMPARATORS = (
    "B1_persistence_last_rate",
    "B7_two_regime_imm",
    "B8_student_t_robust_imm",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md_table(headers: Iterable[str], rows: Iterable[Iterable[object]]) -> str:
    header = [str(value) for value in headers]
    materialized = [[str(value).replace("|", "\\|") for value in row] for row in rows]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in materialized)
    return "\n".join(lines)


def fmt(value: object, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def canonical_aggregate(aggregate: pd.DataFrame) -> pd.DataFrame:
    deep = aggregate.loc[
        aggregate["model_id"].isin(DEEP_MODELS)
        & aggregate["aggregation"].eq("mean_of_fixed_seeds")
    ]
    comparators = aggregate.loc[aggregate["model_id"].isin(COMPARATORS)]
    result = pd.concat((comparators, deep), ignore_index=True)
    if len(result) != 7 or result["model_id"].nunique() != 7:
        raise RuntimeError("Canonical Gate C1 metric rows are incomplete")
    return result.sort_values(["mae", "model_id"], kind="mergesort")


def build_report(
    *,
    validation: dict,
    admission: dict,
    ledger: dict,
    protocol: dict,
    environment: dict,
    hardware: dict,
    canonical: pd.DataFrame,
    folds: pd.DataFrame,
    seeds: pd.DataFrame,
    native: pd.DataFrame,
    screening: pd.DataFrame,
    compute: pd.DataFrame,
    checkpoints: pd.DataFrame,
    execution_incident: dict,
) -> str:
    deep = canonical.loc[canonical["model_id"].isin(DEEP_MODELS)]
    best = deep.iloc[0]
    b1 = canonical.loc[canonical["model_id"].eq("B1_persistence_last_rate")].iloc[0]
    b7 = canonical.loc[canonical["model_id"].eq("B7_two_regime_imm")].iloc[0]
    admitted = admission["admitted_model_ids"]
    admitted_text = ", ".join(f"`{item}`" for item in admitted) if admitted else "ни одной"
    metric_rows = []
    statuses = screening.set_index("model_id")["status"].to_dict()
    for row in canonical.itertuples(index=False):
        metric_rows.append(
            (
                f"`{row.model_id}`",
                "context-only" if row.model_id in COMPARATORS else statuses[row.model_id],
                fmt(row.mae),
                fmt(row.median_fold_mae),
                fmt(row.rmse),
                fmt(row.bias),
                fmt(100.0 * row.b1_skill, 1) + "%",
                fmt(row.max_fold_mae_ratio_vs_b1, 2),
            )
        )
    seed_rows = [
        (
            f"`{row.model_id}`",
            fmt(row.seed_mae_mean),
            fmt(row.seed_mae_iqr),
            fmt(100.0 * row.seed_mae_cv, 2) + "%",
            fmt(row.seed_mae_range),
            fmt(row.ensemble_mae),
            str(int(row.dates_improved_vs_b1)),
            str(int(row.dates_improved_vs_b7)),
        )
        for row in seeds.itertuples(index=False)
    ]
    compute_rows = [
        (
            f"`{row.model_id}`",
            str(int(row.logical_inner_evaluations)),
            str(int(row.physical_inner_fits_executed)),
            str(int(row.physical_inner_fits_reused)),
            str(int(row.outer_refits)),
            f"{int(row.parameter_count_min)}–{int(row.parameter_count_max)}",
            f"{int(row.epoch_count_min)}–{int(row.epoch_count_max)}",
            fmt(row.outer_fit_seconds, 1),
            fmt(row.peak_vram_mb, 1),
        )
        for row in compute.itertuples(index=False)
    ]
    c04_native = native.loc[native["scope"].eq("seed_aggregate")].sort_values("seed")
    native_rows = [
        (
            str(int(row.seed)),
            fmt(row.crps),
            fmt(row.nll),
            fmt(100.0 * row.coverage_50, 1) + "%",
            fmt(100.0 * row.coverage_80, 1) + "%",
            fmt(100.0 * row.coverage_95, 1) + "%",
            fmt(row.mean_interval_width),
        )
        for row in c04_native.itertuples(index=False)
    ]
    worst_deep = []
    deep_folds = folds.loc[
        folds["model_id"].isin(DEEP_MODELS)
        & folds["aggregation"].eq("mean_of_fixed_seeds")
    ]
    for model_id, frame in deep_folds.groupby("model_id", sort=True):
        row = frame.sort_values(["mae", "target_date"], ascending=[False, True]).iloc[0]
        worst_deep.append((f"`{model_id}`", str(row["target_date"]), fmt(row["mae"]), fmt(row["fold_mae_ratio_vs_b1"], 2)))
    checks = screening.set_index("model_id")
    admission_rows = [
        (
            f"`{model_id}`",
            str(checks.loc[model_id, "status"]),
            "PASS" if bool(checks.loc[model_id, "pooled_mae_within_10_percent_b1"]) else "FAIL",
            "PASS" if bool(checks.loc[model_id, "median_fold_mae_within_10_percent_b1"]) else "FAIL",
            "PASS" if bool(checks.loc[model_id, "no_fold_exceeds_2x_b1"]) else "FAIL",
            "да" if bool(checks.loc[model_id, "admitted_to_c2"]) else "нет",
        )
        for model_id in DEEP_MODELS
    ]
    checkpoint_inner = checkpoints.loc[checkpoints["role"].eq("inner")]
    checkpoint_outer = checkpoints.loc[checkpoints["role"].eq("outer")]
    if (
        len(checkpoints) != 3860
        or len(checkpoint_inner) != 3640
        or len(checkpoint_outer) != 220
        or not checkpoints["keep_top_k"].eq(5).all()
        or checkpoints["outer_labels_used_for_ranking"].astype(bool).any()
    ):
        raise RuntimeError("Gate C1 checkpoint evidence is incomplete or unsafe")
    benchmark = execution_incident["incidents"][-1]["matched_runtime_benchmark"]
    mean_reduction = 100.0 * (
        1.0 - float(benchmark["new_mean_fit_seconds_including_checkpointing"])
        / float(benchmark["old_mean_fit_seconds"])
    )
    median_reduction = 100.0 * (
        1.0 - float(benchmark["new_median_fit_seconds_including_checkpointing"])
        / float(benchmark["old_median_fit_seconds"])
    )
    lines = [
        "# Gate C1: пятиseedовый compact sequence temporal screen",
        "",
        "## Краткий результат",
        "",
        f"Gate C1 завершён со статусом **`{validation['status']}`**; независимый validator выполнил {validation['check_count']} проверок, failures — {validation['failed_checks']}. Все четыре заранее зарегистрированные архитектуры получили терминальный научный статус. В Gate C2 допущено: **{admitted_text}**.",
        "",
        f"Лучшая deep-модель по canonical mean-of-five-seeds — `{best['model_id']}` с pooled rolling MAE **{float(best['mae']):.3f} мм/год**. Для контекста B1 даёт **{float(b1['mae']):.3f} мм/год**, а действующий primary suite v4 B7 — **{float(b7['mae']):.3f} мм/год**. Это не внешняя оценка и не основание для производственного заявления: научная граница результата — `train_only_internal_research`.",
        "",
        "## 1. Научная и информационная граница",
        "",
        "В C1 использовались только sequence manifests, построенные поверх 911 строк `t1_v1/train`. Model-facing worker не мог принимать пути к историческому validation, раскрытому test или будущему holdout. Outer-validation targets были присоединены отдельным scorer ровно после проверки и hash freeze всех 44 unlabeled shards.",
        "",
        f"- C0 content contract: `{admission['gate_c0_contract_content_sha256']}`;",
        f"- C1 config SHA-256: `{admission['gate_c1_config_sha256']}`;",
        f"- C1 code SHA-256: `{admission['gate_c1_code_sha256']}`;",
        f"- environment SHA-256: `{admission['environment_sha256']}`;",
        f"- outer-label access events: `{ledger['access_event']}`;",
        "- `historical_validation_loaded=false`;",
        "- `current_test_loaded=false`;",
        "- `new_holdout_seen=false`;",
        "- `profile_zone_transition_audit_executed=false`;",
        "- `suite_v5_created=false`.",
        "",
        "## 2. Дизайн эксперимента",
        "",
        "Frozen plan включает 11 rolling-origin outer folds, по три forward-only inner folds, 56 grid configurations и пять seeds `42117–42121`. Полный logical tuning inventory содержит 9 240 evaluations; безопасный hash-keyed cache сохраняет полную логическую трассу, выполняя 3 640 уникальных inner fits. Затем выполнены 220 outer refits.",
        "",
        "Canonical point prediction каждой deep-модели — арифметическое среднее пяти fixed-seed predictions. Для Student-t GRU распределения не усредняются в псевдо-Student-t: C1 публикует native diagnostics по каждому seed, а ensemble — только point mean.",
        "",
        "## 3. Temporal результаты",
        "",
        "На pooled temporal evidence действующий B7 остаётся сильнейшим comparator. C01 находится практически на уровне B1 и потому проходит широкий admission-порог, но этот допуск не означает превосходства над B7 или готовности стать primary.",
        "",
        "![Сравнение pooled temporal MAE](../../artifacts/model_selection/t1_gate_c1_compact_screen_v1/figures/01_ensemble_temporal_mae.png)",
        "",
        md_table(
            ("Модель", "Статус", "MAE", "Median fold MAE", "RMSE", "Bias", "B1 skill", "Worst fold / B1"),
            metric_rows,
        ),
        "",
        "Показатели рассчитаны на одинаковых 595 rolling outer origins. R² остаётся описательной статистикой и не используется для admission. Поскольку target допускает отрицательные и близкие к нулю значения, MAPE/sMAPE не применялись.",
        "",
        "### Худший outer fold каждой deep-архитектуры",
        "",
        md_table(("Модель", "Target date", "MAE", "Отношение к B1"), worst_deep),
        "",
        "Покампанийная траектория показывает сильную неоднородность ошибки: улучшение на отдельных датах соседствует с локальными провалами. Поэтому pooled MAE нельзя интерпретировать без fold-level guardrails.",
        "",
        "![MAE по 11 rolling-origin target dates](../../artifacts/model_selection/t1_gate_c1_compact_screen_v1/figures/02_rolling_mae_by_target_date.png)",
        "",
        "## 4. Seed stability",
        "",
        md_table(
            ("Модель", "Mean seed MAE", "IQR", "CV", "Range", "Ensemble MAE", "Дат лучше B1", "Дат лучше B7"),
            seed_rows,
        ),
        "",
        "Пороги seed IQR ≤ 0,50 мм/год и CV ≤ 10% публикуются как заранее определённая диагностика будущей suite-v5 eligibility. Они не добавлялись задним числом к temporal admission C1.",
        "",
        "Среднее пяти фиксированных seeds улучшает MAE относительно медианного одиночного seed у всех четырёх architectures; при этом C04 имеет наибольший полный seed range.",
        "",
        "![Устойчивость по пяти фиксированным seeds](../../artifacts/model_selection/t1_gate_c1_compact_screen_v1/figures/03_seed_stability.png)",
        "",
        "## 5. Native Student-t diagnostics",
        "",
        md_table(("Seed", "CRPS", "NLL", "Coverage 50%", "Coverage 80%", "Coverage 95%", "Mean width"), native_rows),
        "",
        "Эти интервалы являются native outputs C04 и пока не сопоставимы с общим conformal wrapper. Conformal calibration, mixture handling и conditional coverage остаются Gate C2.",
        "",
        "## 6. Программный temporal admission",
        "",
        md_table(("Модель", "Статус", "Pooled ≤ 1,10 B1", "Median ≤ 1,10 B1", "Worst ≤ 2,00 B1", "Допуск C2"), admission_rows),
        "",
        "Низкое качество классифицируется как `REJECTED_TEMPORAL_SCREEN`, а не как software failure. `FAIL_PROTOCOL` резервируется для leakage, hash/schema/environment mismatch или неполного незарегистрированного выполнения.",
        "",
        "## 7. Вычислительная трасса",
        "",
        md_table(("Модель", "Logical inner", "Physical inner", "Cache reuse", "Outer refits", "Параметры", "Эпохи", "Outer fit, с", "Peak VRAM, MB"), compute_rows),
        "",
        "C01 даёт лучший deep MAE, но среди выбранных outer specifications является одновременно крупнейшей и самой дорогой по суммарному времени refit. Это допустимо для C2 screening, однако не создаёт преимущества над почти бесплатным B7 comparator.",
        "",
        "![MAE относительно числа параметров и времени outer refit](../../artifacts/model_selection/t1_gate_c1_compact_screen_v1/figures/04_mae_vs_complexity.png)",
        "",
        f"Execution authority: `{environment['status']}`; Python `{hardware['python_version']}`, Torch `{hardware['torch_version']}`, CUDA `{hardware['torch_cuda_version']}`, GPU `{hardware['gpu_name']}`, driver `{hardware['gpu_driver_version']}`. Полная среда, wheel hashes, GPU/driver/CUDA capture и determinism smoke сохранены рядом с C1 artifacts.",
        "",
        "### Checkpoint policy и CUDA-ускорение",
        "",
        f"Независимая инвентаризация подтвердила **{format(len(checkpoints), ',').replace(',', chr(160))}** manifests: {format(len(checkpoint_inner), ',').replace(',', chr(160))} inner fits и {format(len(checkpoint_outer), ',').replace(',', chr(160))} outer refits. Каждый fit хранит пять полных training states; recovery state фиксируется после каждой завершённой стадии в 50 эпох и на terminal epoch. Inner rank 1 выбирается по frozen early-stopping metric с tie-break по более ранней эпохе. Outer refit не ранжируется по outer labels: сохраняются последние пять эпох, а выбирается заранее зафиксированная final epoch.",
        "",
        f"В matched benchmark `{benchmark['model_id']}` / `{benchmark['fold_id']}` на {int(benchmark['fits_each_run'])} одинаковых fits векторизованный CUDA-путь с fused AdamW и device-side validation снизил mean fit time с {float(benchmark['old_mean_fit_seconds']):.3f} до {float(benchmark['new_mean_fit_seconds_including_checkpointing']):.3f} с (**{mean_reduction:.1f}%**, {float(benchmark['mean_speedup_ratio']):.2f}×), а median — с {float(benchmark['old_median_fit_seconds']):.3f} до {float(benchmark['new_median_fit_seconds_including_checkpointing']):.3f} с (**{median_reduction:.1f}%**, {float(benchmark['median_speedup_ratio']):.2f}×). Новое время уже включает top-5 checkpoint I/O; сравнивались те же model, fold, grids, inner folds и seeds.",
        "",
        f"Полное насыщение 16 ГиБ VRAM на этой геометрии не ожидается и не является корректным критерием качества реализации: максимум зарегистрированной tensor allocation равен {float(compute['peak_vram_mb'].max()):.1f} MB, крупнейшая configuration содержит только {int(compute['parameter_count_max'].max())} параметров, длина последовательности не превышает 16, batch size заморожен на 32, а одновременно разрешён один deterministic GPU worker. Искусственное увеличение batch или параллельный запуск folds изменили бы frozen execution semantics либо ослабили воспроизводимость. Полученное ускорение связано с устранением CPU/GPU synchronization overhead, а не с попыткой занять всю память видеокарты.",
        "",
        "## 8. Ограничения и следующий этап",
        "",
        "C1 не проверяет leave-profile-out, leave-zone-out, transition/gap regimes или conformal calibration. Он не создаёт suite v5 и не меняет suite v4. Следующий допустимый этап — Gate C2 только для моделей из `c2_admission_manifest.json`, с B1/B7/B8 как неизменяемыми context comparators. После C2 заранее замораживается suite v5 или fallback B7, и лишь затем возможна однократная оценка на новом real future/external holdout.",
        "",
        "## 9. Reader-facing figures",
        "",
        "1. `artifacts/model_selection/t1_gate_c1_compact_screen_v1/figures/01_ensemble_temporal_mae.png`;",
        "2. `artifacts/model_selection/t1_gate_c1_compact_screen_v1/figures/02_rolling_mae_by_target_date.png`;",
        "3. `artifacts/model_selection/t1_gate_c1_compact_screen_v1/figures/03_seed_stability.png`;",
        "4. `artifacts/model_selection/t1_gate_c1_compact_screen_v1/figures/04_mae_vs_complexity.png`.",
        "",
        "Все рисунки построены только из сохранённых machine artifacts после независимой валидации; model-training calls при reporting равны нулю.",
        "",
        f"Protocol freeze content/config SHA-256: `{protocol['config_sha256']}`.",
        "",
    ]
    return "\n".join(lines)


def build_catalog(
    *, registry: dict, canonical: pd.DataFrame, screening: pd.DataFrame, compute: pd.DataFrame
) -> str:
    metrics = canonical.set_index("model_id")
    status = screening.set_index("model_id")
    resources = compute.set_index("model_id")
    registry_by_id = {item["model_id"]: item for item in registry["models"]}
    sections = [
        "# Model Catalog C1",
        "",
        "Каталог фиксирует только четыре обязательные compact sequence-архитектуры Gate C1. Числа относятся к nested temporal screen внутри `t1_v1/train`; spatial/transition/calibration evidence отсутствует до Gate C2.",
        "Все adapters используют единый work-only checkpoint contract: recovery state после 50-epoch стадии и terminal epoch, top-5 по inner objective, fixed final epoch для outer refit. Outer labels не влияют на checkpoint selection.",
        "",
    ]
    for model_id in DEEP_MODELS:
        spec = registry_by_id[model_id]
        metric = metrics.loc[model_id]
        screen = status.loc[model_id]
        resource = resources.loc[model_id]
        sections.extend(
            [
                f"## `{model_id}`",
                "",
                f"- Family: `{spec['family']}`; probabilistic: `{str(spec['probabilistic']).lower()}`.",
                f"- Objective: `{spec['training_objective']}`; selection: `{spec['selection_objective']}`.",
                f"- Frozen configurations: `{len(spec['parameter_grid'])}`; fixed seeds: `{', '.join(str(item) for item in spec['seeds'])}`.",
                f"- Input: пять numeric channels, train-fitted one-hot `current_campaign_type`, три masks; identifiers только metadata.",
                f"- Canonical ensemble MAE: **{float(metric['mae']):.3f} мм/год**; median fold MAE: **{float(metric['median_fold_mae']):.3f} мм/год**; B1 skill: **{100.0 * float(metric['b1_skill']):.1f}%**.",
                f"- Parameter count across selected outer specs: `{int(resource['parameter_count_min'])}–{int(resource['parameter_count_max'])}`; outer epochs: `{int(resource['epoch_count_min'])}–{int(resource['epoch_count_max'])}`.",
                f"- Temporal status: **`{screen['status']}`**; admitted to C2: `{str(bool(screen['admitted_to_c2'])).lower()}`.",
                f"- Model spec SHA-256: `{spec['spec_sha256']}`.",
                "- Claim boundary: `train_only_internal_research`; final/external quality claim prohibited.",
                "",
            ]
        )
    sections.extend(
        [
            "## Frozen context comparators",
            "",
            "`B1_persistence_last_rate`, `B7_two_regime_imm` и `B8_student_t_robust_imm` перенесены из frozen B6 artifacts на exact 595-origin universe. Они не участвуют в deep-model admission как новые кандидаты и не перенастраиваются.",
            "",
        ]
    )
    return "\n".join(sections)


def build_model_card(
    *,
    model_id: str,
    registry: dict,
    canonical: pd.DataFrame,
    screening: pd.DataFrame,
    compute: pd.DataFrame,
    seeds: pd.DataFrame,
) -> str:
    spec = next(item for item in registry["models"] if item["model_id"] == model_id)
    metric = canonical.set_index("model_id").loc[model_id]
    screen = screening.set_index("model_id").loc[model_id]
    resource = compute.set_index("model_id").loc[model_id]
    stability = seeds.set_index("model_id").loc[model_id]
    probabilistic_note = (
        "Модель возвращает Student-t loc/scale/df и native quantiles по каждому seed. "
        "Canonical ensemble содержит только point mean; объединённое распределение не публикуется в C1."
        if bool(spec["probabilistic"])
        else "Модель возвращает только point prediction; интервалы требуют общего conformal wrapper в Gate C2."
    )
    return "\n".join(
        [
            f"# Model card: `{model_id}`",
            "",
            "## Назначение",
            "",
            "Compact sequence comparator для nested temporal screening задачи T1. Модель не является производственной и не оценивалась на новом future/external holdout.",
            "",
            "## Спецификация",
            "",
            f"- Family: `{spec['family']}`; probabilistic: `{str(spec['probabilistic']).lower()}`.",
            f"- Training objective: `{spec['training_objective']}`; selection objective: `{spec['selection_objective']}`.",
            f"- Frozen grid: `{len(spec['parameter_grid'])}` configurations; parameter limit: `{spec['parameter_count_limit']}`.",
            f"- Seeds: `{', '.join(str(item) for item in spec['seeds'])}`; environment: `{spec['environment_id']}`.",
            f"- Model spec SHA-256: `{spec['spec_sha256']}`.",
            "",
            "## Входы и leakage boundary",
            "",
            "Пять numeric channels, train-fitted one-hot `current_campaign_type`, padding/observation/missing masks и фактическая длина. point/profile/zone/campaign IDs не передаются в tensor. Preprocessing и target scaling fit выполняются только по train role; historical validation, disclosed test и holdout недоступны worker.",
            "",
            "## Temporal evidence",
            "",
            f"- Canonical mean-of-five-seeds MAE: **{float(metric['mae']):.3f} мм/год**; RMSE: **{float(metric['rmse']):.3f} мм/год**.",
            f"- Median fold MAE: **{float(metric['median_fold_mae']):.3f} мм/год**; maximum fold/B1 ratio: **{float(metric['max_fold_mae_ratio_vs_b1']):.3f}**.",
            f"- B1 skill: **{100.0 * float(metric['b1_skill']):.1f}%**; seed MAE IQR: **{float(stability['seed_mae_iqr']):.3f} мм/год**; seed CV: **{100.0 * float(stability['seed_mae_cv']):.2f}%**.",
            f"- Temporal screen status: **`{screen['status']}`**; admitted to C2: `{str(bool(screen['admitted_to_c2'])).lower()}`.",
            "",
            "## Вычислительная трасса",
            "",
            f"Logical inner evaluations: `{int(resource['logical_inner_evaluations'])}`; physical inner fits: `{int(resource['physical_inner_fits_executed'])}`; cache reuse: `{int(resource['physical_inner_fits_reused'])}`; outer refits: `{int(resource['outer_refits'])}`. Selected outer parameter count: `{int(resource['parameter_count_min'])}–{int(resource['parameter_count_max'])}`; epoch count: `{int(resource['epoch_count_min'])}–{int(resource['epoch_count_max'])}`.",
            "Для каждого fit сохранены пять полных training states и recovery checkpoint. Inner prediction восстановлен из rank 1 по frozen objective; outer prediction — из preregistered final epoch без доступа к outer labels.",
            "",
            "## Неопределённость",
            "",
            probabilistic_note,
            "",
            "## Ограничения и запрещённые выводы",
            "",
            "C1 не содержит leave-profile, leave-zone, transition/gap или common conformal evidence и не создаёт suite v5. Статус `PASSED_TEMPORAL_SCREEN` означает только право перейти в C2. Нельзя утверждать окончательную, промышленную или внешнюю точность модели.",
            "",
        ]
    )


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    artifact = root / ARTIFACT_RELATIVE
    required = {
        "validation": artifact / "validation_report.json",
        "admission": artifact / "c2_admission_manifest.json",
        "ledger": artifact / "outer_label_access_ledger.json",
        "protocol": artifact / "protocol_freeze.json",
        "environment": artifact / "environment" / "execution_authority.json",
        "hardware": artifact / "environment" / "hardware_report.json",
        "registry": artifact / "model_registry.json",
        "aggregate": artifact / "temporal_aggregate_metrics.csv",
        "folds": artifact / "temporal_fold_metrics.csv",
        "seeds": artifact / "seed_stability_metrics.csv",
        "native": artifact / "student_t_native_metrics.csv",
        "screening": artifact / "screening_register.csv",
        "compute": artifact / "compute_resource_inventory.csv",
        "figures": artifact / "figure_manifest.json",
        "visual_qa": artifact / "visual_qa_report.json",
        "notebook": artifact / "notebook_execution_report.json",
        "artifact_inventory": artifact / "artifact_inventory.csv",
        "execution_incident": artifact / "execution_incident_register.json",
        "checkpoints": artifact / "checkpoint_inventory.csv",
    }
    missing = [path.relative_to(root).as_posix() for path in required.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Validated Gate C1 reader inputs missing: {missing}")
    objects = {
        key: json.loads(path.read_text(encoding="utf-8"))
        for key, path in required.items()
        if path.suffix == ".json"
    }
    if objects["validation"].get("status") != "PASS_C1_TEMPORAL_SCREEN":
        raise RuntimeError("Refusing to publish reader material before independent validation passes")
    if objects["visual_qa"].get("manual_visual_review_completed") is not True:
        raise RuntimeError("Refusing to publish reader material before manual figure review")
    frames = {
        key: pd.read_csv(path)
        for key, path in required.items()
        if path.suffix == ".csv"
    }
    canonical = canonical_aggregate(frames["aggregate"])
    report = build_report(
        validation=objects["validation"],
        admission=objects["admission"],
        ledger=objects["ledger"],
        protocol=objects["protocol"],
        environment=objects["environment"],
        hardware=objects["hardware"],
        canonical=canonical,
        folds=frames["folds"],
        seeds=frames["seeds"],
        native=frames["native"],
        screening=frames["screening"],
        compute=frames["compute"],
        checkpoints=frames["checkpoints"],
        execution_incident=objects["execution_incident"],
    )
    catalog = build_catalog(
        registry=objects["registry"],
        canonical=canonical,
        screening=frames["screening"],
        compute=frames["compute"],
    )
    report_path = root / REPORT_RELATIVE
    catalog_path = root / CATALOG_RELATIVE
    model_card_root = root / "docs" / "model_cards" / "gate_c1"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    model_card_root.mkdir(parents=True, exist_ok=True)
    write_text_atomic(root, report_path, report)
    write_text_atomic(root, catalog_path, catalog)
    model_card_paths = []
    for model_id in DEEP_MODELS:
        model_card_path = model_card_root / f"{model_id}.md"
        write_text_atomic(
            root,
            model_card_path,
            build_model_card(
                model_id=model_id,
                registry=objects["registry"],
                canonical=canonical,
                screening=frames["screening"],
                compute=frames["compute"],
                seeds=frames["seeds"],
            ),
        )
        model_card_paths.append(model_card_path)
    manifest = {
        "schema_version": 1,
        "gate": "C1_COMPACT_SEQUENCE_TEMPORAL_SCREEN",
        "status": "PASS",
        "scientific_scope": "train_only_internal_research",
        "sources": {
            key: {"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)}
            for key, path in required.items()
        },
        "outputs": [
            {"path": report_path.relative_to(root).as_posix(), "sha256": sha256_file(report_path)},
            {"path": catalog_path.relative_to(root).as_posix(), "sha256": sha256_file(catalog_path)},
            *[
                {"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)}
                for path in model_card_paths
            ],
        ],
        "model_training_calls": 0,
    }
    manifest_path = artifact / "reader_materials_manifest.json"
    write_text_atomic(
        root,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    inventory_path = artifact / "reporting_artifact_inventory.csv"
    candidates = sorted(
        path
        for path in artifact.rglob("*")
        if path.is_file() and path != inventory_path
    )
    candidates.extend(
        [
            root / NOTEBOOK_RELATIVE,
            report_path,
            catalog_path,
            *model_card_paths,
        ]
    )
    inventory = pd.DataFrame(
        [
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(set(candidates))
        ]
    )
    write_text_atomic(root, inventory_path, inventory.to_csv(index=False, lineterminator="\n"))
    print(
        json.dumps(
            {
                "status": "PASS",
                "outputs": 2 + len(model_card_paths),
                "inventory_rows": len(inventory),
                "model_training_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def write_text_atomic(root: Path, path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    work = root / "work" / "gate_c1_reporting"
    work.mkdir(parents=True, exist_ok=True)
    temporary = work / f"{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
