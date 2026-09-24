# Scenario v2 sequence acceptance — BLOCKED_NOT_FROZEN

Дата: 2026-09-24. Authority: frozen `sequence_windows.csv.gz`, 321766 mappings.
Network contract до реализации записан в
`docs/governance/SCENARIO_V2_REPRESENTATION_PROTOCOL.md` и
`configs/scenario_sequences_v2.json`; оба остаются draft.

Реализован lazy tensorizer с пятью numeric channels: settlement, rate, sigma,
actual elapsed days, missed campaigns. Все имена входят в frozen feature allowlist.
Max length 16; literal history IDs; chronological left padding; observation,
padding, missing-campaign и per-value-valid masks; no interpolation. Raw float64,
network float32 после fold-only imputation/scaling. Первый неизвестный rate/gap/count
сохраняется NaN до preprocessing, а padding остаётся zero после transform.
Delta_t первой записи усечённого window учитывает предыдущее реальное наблюдение
из полной causal history, даже если оно уже вне окна.

Blocker V2-REP-001: реальные elapsed days расходятся с frozen tabular predictor
у 31283 origins. Например, final real token имеет gap=112, а current origin
`days_since_previous_observation=111`. Детали и полная таблица — в
`SCENARIO_V2_ADAPTER_ACCEPTANCE_RU.md` и machine-readable blocker evidence.

**Что подтверждено:** независимая проверка всех 321766 frozen mappings (own point,
latest ≤16 observations, chronology/cutoff/final observation); train-only
preprocessing guard; deterministic повторный runtime для выбранных 128 windows;
инвариантность этих windows к изменению будущих observations; float32 finite transform
и zero padding на 32 validation windows; запрет evaluator в worker.

**Что не подтверждено:** полный runtime parity всех origins. Тест остановился на
первой batch 4096: 379 final-gap mismatches. Ошибочный тест не отключён, не заменён
на xfail и допуск не увеличен. Полная two-run representation acceptance не проведена.

Исторический C01 configuration требует `current_campaign_type` каждого token.
Разрешённая history table не даёт этот канал для ранних кампаний; догадка не
используется. `PackedRecurrentRegressor` принимает произвольный input_size и имеет
совместимый интерфейс (code inspection `gate_c1_models.py:33–92`). Но старые
SequenceModelSpec/worker/scalers/checkpoints не совместимы напрямую. C01 construction
smoke в отдельном torch runtime не завершался из-за остановки на blocker; forward
и training не запускались. Это не PASS neural migration.

Giant tensor dump не создавался. Compact draft split manifests находятся только
в ignored work directory; representation release не заморожен. Dataset, generator,
truth, seeds, mixture, evaluator и outer roles не изменены. Model calls = 0.

Следующий шаг — отдельное решение по versioned elapsed-days correction, затем
полный acceptance rerun и только после PASS отдельное задание на benchmark.
