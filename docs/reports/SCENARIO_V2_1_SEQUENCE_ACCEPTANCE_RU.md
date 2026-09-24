# Scenario v2.1 sequence acceptance

**PASS_REPRESENTATION_FROZEN**, 2026-09-24.
Authority: `data/scenario_simulation_v2_1/manifest.json`, SHA256 `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95`.

Проверены все **321766 origins/windows**, без остановки на первой batch.
Authoritative history IDs соблюдены буквально: ≤16 actual observations,
chronological order, own point, cutoff ≤ current_date, final observation=current.
No interpolation. Left padding, observation/padding/missing-campaign/per-value-valid
masks проверены для всех batches. Unknown first rate/gap/count остаются NaN до
preprocessing. Real elapsed gap включает predecessor вне усечённого окна.

**1608830 exact final-token comparisons**, **0 mismatch**, по пяти channels:
last_settlement_mm, last_rate_mm_y, current_standard_uncertainty_mm,
days_since_previous_observation, missing_campaigns_since_previous.

Raw dtype float64; normalized network input float32; padding zero после transform.
State каждого fold получен только из его fit origins, по real tokens. Повторение
одного history token в нескольких training windows — явная прежняя convention.
Normalizers не используют targets, calibration/evaluation или validation windows.
Masks и identifiers не нормируются; IDs никогда не входят в network x.

Canonical streaming runtime hash:
`037e0755e0dff73dfa2a41326c83a9eebb53430a1e58152db96ee3daeabde27a`.
Canonical record: values float64 little-endian [16,5], length int64 little-endian,
padding/observation/missing masks uint8 [16], value-valid uint8 [16,5]; frozen
sample order, no alignment padding. Это hash всего runtime, а не небольшой sample.
Giant tensor dump не создан. Две сборки и final copy идентичны по всем 50 файлам,
включая manifests/windows mapping references/folds и 42 scaler states.

Representation manifest:
`0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d`.
Frozen window identity (independent IDs):
`48d49b3213dbf887467f47e581e744a4e9fad2a773598f938a66127fd25660db`.

## C01 scope

Создан новый input spec `C01_ARCHITECTURE_V2_1_FIVE_NUMERIC_CHANNELS`:
PackedRecurrentRegressor(input_size=5, hidden_size=16, layers=1, dropout=0, cell=gru).
Успешно выполнен untrained constructor; input [batch,16,5], weight_ih_l0 [48,5],
сигнатура masks/lengths совместима. Torch 2.13.0+cu130.
Seed 0 относится только к воспроизводимому construction smoke, не benchmark seeds.

Token-level current_campaign_type не придумывался. Старый Gate C1 worker/config,
SequenceModelSpec, checkpoints и scalers не переиспользуются. Это architecture
construction compatibility, **не historical C01 reproduction** и не готовый
training runner. Forward=0, fit=0, inference/scoring=0, checkpoint loads=0.

Подробные folds, boundary, tests, команды и ограничения — в
`SCENARIO_V2_1_ADAPTER_ACCEPTANCE_RU.md`; machine receipts —
`artifacts/data_quality/scenario_representation_v2_1/`.
Dataset v2 остаётся immutable; старые BLOCKED reports/evidence не переписаны.
Model experiments требуют отдельного задания после review.
