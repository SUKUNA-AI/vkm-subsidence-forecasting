# Gate C: протокол sequence-моделей T1

- Статус документа: `FROZEN_PROTOCOL_BEFORE_MODEL_TRAINING`;
- версия представления: `t1_train_gate_c_v1`;
- область утверждений: `train_only_internal_research`.

## 1. Governance-решение до запуска Gate C

`final_candidate_suite_v4.json` остаётся неизменяемой историей Gate B6. Её
primary — `B7_two_regime_imm`. Gate C не изменяет suite v4 и не переписывает
`configs/final_holdout_v3.yaml`.

До появления или открытия любых новых target values Gate C вправе создать
`final_candidate_suite_v5.json` только по заранее замороженному nested
train-only протоколу:

- если ни одна deep sequence-модель не проходит все критерии, primary suite v5
  автоматически остаётся `B7_two_regime_imm`;
- если модель проходит все критерии, она может заменить B7 до появления
  holdout labels;
- после появления или однократного открытия holdout менять primary запрещено;
- для suite v5 должна быть создана новая версия holdout policy и intake record;
  версия v3 сохраняется без изменений как доказательство предыдущей границы.

Отсутствующий future/external holdout имеет статус `PENDING_DATA`. Gate C его
не имитирует и не заменяет старым раскрытым test.

## 2. Научная граница Gate C0

Gate C0 замораживает данные, masks, folds, architecture registry, environment
и правила выбора. В нём нет обучения, подбора гиперпараметров, early stopping,
предсказаний или заявлений о качестве deep-моделей. Единственный допустимый
model-facing источник — 911 origins из `t1_v1/train`.

Запрещены:

- исторический `t1_v1/validation`;
- раскрытый `t1_v1/test`;
- synthetic holdout как замена реальной финальной проверки;
- random row split, обычный `KFold` и shuffle split;
- использование target-date labels внешнего fold для подбора параметров или
  числа эпох;
- передача идентификаторов point/profile/zone/campaign в сеть;
- внешние pretrained/foundation checkpoints, облачные model API и любые
  сетевые обращения worker во время обучения.

## 3. Контракт временной последовательности

Для каждого forecast origin строится история одной рабочей точки из
`leveling_adjusted_epochs.csv`. Она включает только фактически наблюдавшиеся
эпохи с датой не позже `current_date`, включая наблюдение origin. Целевая эпоха
и все будущие кампании отсутствуют.

Замороженная геометрия:

- minimum history: 3 наблюдения;
- maximum history внутри `t1_v1/train`: 16 наблюдений;
- fixed tensor length: 16;
- padding: слева;
- для будущей истории длиннее 16 наблюдений заранее задано удаление самых
  старых token, без изменения последних 16;
- `observation_id = {point_id}::{campaign_id}` используется только как metadata;
- sequence hash рассчитывается из упорядоченных observation IDs, дат,
  разрешённых каналов и missing mask.

Каналы сети повторяют семантику уже разрешённых полей исполняемого
`formal_feature_contract.csv`:

1. `last_settlement_mm`;
2. `last_rate_mm_y`;
3. `current_standard_uncertainty_mm`;
4. `days_since_previous_observation`;
5. `missing_campaigns_since_previous`;
6. `current_campaign_type`.

`padding_mask`, `observation_mask` и `missing_campaign_mask` передаются как
structural masks, а не как свободные estimator features. В sequence artifact
идентификаторы присутствуют только для аудита и resampling. Runtime guard
проверяет, что их нет в матрице сети.

Fold provenance нормализован, а не продублирован в каждой строке: каждый
`sample_id` соединяется с неизменяемыми outer/inner assignments B5; отдельный
`fold_sequence_contracts.csv` фиксирует hashes пар `sample_id + sequence_hash`,
held-out profile/zone и forward-only доказательство каждого fold.

## 4. Preprocessing и leakage boundary

Imputer, scaler и categorical encoder обучаются заново внутри train-role
каждого outer или inner fold. Padding rows не участвуют в fit. Structural
grouping keys передаются отдельно от estimator matrix.

Early stopping разрешён только на последних допустимых inner rolling folds,
которые целиком лежат в outer train и в `t1_v1/train`. Формулировка
`validation` в описании Gate C всегда означает
`inner_rolling_validation_within_t1_v1_train`; она никогда не означает
исторический `t1_v1/validation` и никогда не означает outer validation labels.

После выбора конфигурации и числа эпох по inner evidence модель переобучается
на полном outer train с медианой выбранных inner best epochs. Outer validation
открывается только для выдачи prediction соответствующего fold.

## 5. Замороженный compact screen

Обязательный первый уровень:

- `C01_compact_gru`;
- `C02_compact_lstm`;
- `C03_causal_tcn`;
- `C04_probabilistic_gru_student_t`.

Условный уровень при прохождении C0 guards и лимита 100 000 параметров:

- `C05_tsmixer_compact`;
- `C06_tft_compact`, без embedding идентификаторов.

Формально исключены по геометрии данных:

- N-BEATS и N-HiTS: потребовали бы недоказанной регуляризации календарной
  сетки или интерполяции при 3–16 нерегулярных наблюдениях;
- PatchTST: длина контекста недостаточна для осмысленного patching;
- iTransformer: число origins и каналов недостаточно для устойчивой
  attention-модели в заданном параметрическом бюджете.

Это pre-screening result, а не пропущенная работа. При изменении данных может
быть создана новая версия протокола, но `t1_train_gate_c_v1` не меняется.

Каждая обучаемая модель использует пять seeds: `42117`–`42121`. Frozen
comparators B1, B7 и B8 не перенастраиваются.

## 6. Последовательность будущего Gate C1

1. 11 rolling-origin outer folds, три forward-only inner folds внутри каждого;
2. temporal screen на exact expected sample IDs;
3. 42 spatio-temporal leave-profile-out folds;
4. 12 spatio-temporal leave-zone-out folds;
5. transition audit, scaled conformal calibration только из inner OOF residuals;
6. seed stability, parameter/time/RAM/VRAM audit;
7. suite v5 eligibility и B7 fallback;
8. новая holdout policy до появления labels;
9. future/external holdout открывается один раз.

## 7. Критерии suite v5

Новая модель может заменить B7 только при одновременном выполнении всех
условий из `configs/gate_c.yaml`, включая:

- rolling MAE и audit-tail MAE не хуже B7 более чем на 2%;
- pooled transition improvement против B1 не менее 10%;
- volatile-or-gap не хуже лучшего из B1/B7;
- leave-profile, leave-zone и worst-zone degradation не более 5%;
- 95% conformal coverage от 0,90 до 0,97;
- одинаковый знак улучшения минимум для 7 из 11 дат и 8 из 14 профилей;
- IQR seed-level MAE не более 0,50 мм/год и CV не более 10%;
- все пять seeds, environment, leakage и reproducibility checks завершены.

Выбор выполняется лексикографически, без непрозрачного weighted score:
rolling MAE, transition MAE, worst-zone MAE, 95% WIS, seed MAE IQR, fit time,
parameter count, model ID. Если eligible deep-моделей нет, результат
`PASS_NO_NEW_PRIMARY` и B7 остаётся primary.

## 8. Воспроизводимая среда

Gate C использует отдельный `requirements/gate_c_torch.lock.txt`, CPython 3.13
и официальный PyTorch wheel для CUDA 13.0. Среда не содержит внешних
pretrained-model dependencies. Для каждого фактического запуска C1 будут записаны exact `pip freeze`, hardware /
driver / CUDA capture, determinism report и environment ID в prediction rows.

Все временные checkpoints разрешены только в `work/`. В `artifacts/`
публикуются лишь проверенные manifests, predictions, metrics, reports и
замороженный full-train artifact выбранной модели.

## 9. Допустимые статусы

- `PASS_PROTOCOL_FROZEN` — Gate C0 manifests, hashes и guards воспроизводимы;
- `PASS_NEW_INTERNAL_PRIMARY` — будущая deep-модель прошла все frozen gates;
- `PASS_NO_NEW_PRIMARY` — ни одна не прошла, B7 сохранён;
- `FAIL_PROTOCOL` — leakage, broken manifests, environment mismatch,
  incomplete folds или невоспроизводимость.

Низкое качество модели не является software failure. Нарушение протокола
является.
