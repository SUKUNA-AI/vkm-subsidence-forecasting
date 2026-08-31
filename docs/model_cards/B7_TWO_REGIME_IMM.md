# Model Card: B7 two-regime IMM

## Роль и статус

- Model ID: `B7_two_regime_imm`.
- Семейство: switching state-space / two-regime interacting multiple model.
- Suite v4 role: **primary**.
- Gate B6 status: frozen comparator, `PASS_NO_NEW_PRIMARY` fallback winner.
- Claim scope: `train_only_internal_research`.
- External/final quality claim: запрещён до one-shot future/external holdout.

## Назначение

B7 прогнозирует operational settlement rate на следующий planned campaign.
Два режима представляют stable dynamics и damped acceleration/transition.
Модель предназначена для малой нерегулярной longitudinal выборки, где у точки
короткая история, campaign gaps меняются, а пространственные группы не должны
запоминаться estimator-ом.

## Данные и ограничения доступа

Gate B6 использует только 911 origins `t1_v1/train`. Оценка выполнена на 11
forward rolling dates, 42 spatio-temporal leave-profile-out и 12
leave-zone-out folds. Historical validation, раскрытый T1 test и новый
holdout не загружались. Feature, split и model specifications защищены
SHA-256 в suite v4.

## Замороженная спецификация

Ключевые параметры: `q_stable=0.5`, `q_transition=200`,
`p_stable_stay=0.99`, `p_transition_stay=0.75`, stable acceleration retention
0.2/year и transition retention 0.95/year. Полный exact parameter record
хранится в `artifacts/governance/final_candidate_suite_v4.json`; повторный
подбор на уже виденных данных запрещён.

## Train-only evidence

| Метрика | Значение |
|---|---:|
| Rolling pooled MAE | **5,640 мм/год** |
| Rolling median fold MAE | 5,744 мм/год |
| B1 skill | **+10,64%** |
| Equal-profile macro MAE | **5,676 мм/год** |
| Equal-zone macro MAE | **4,975 мм/год** |
| Worst-zone MAE | **8,478 мм/год** |
| Pooled transition MAE | **9,169 мм/год** |
| Accelerating MAE | **11,491 мм/год** |
| Volatile-or-gap MAE | 7,005 мм/год |
| 95% conformal coverage | **0,951** |
| 95% conformal mean width | 51,405 мм/год |
| Conformal WIS | **3,788** |

B7 лучше B1 на 10 из 11 rolling dates и в 13 из 14 profiles. Profile-cluster
sensitivity interval для дельты `B1 − B7` равен [0,375; 0,996] мм/год.
Этот интервал является sensitivity evidence при малом числе clusters, а не
i.i.d. confidence interval.

## Риски и ограничения

- Max absolute rolling error достигает 105,300 мм/год; тяжёлый хвост остаётся.
- На focused date 2023-01-17 B7 хуже B1.
- Volatile-or-gap улучшение относительно B1 минимально; модель не решает все
  gap/noise cases.
- 95% intervals широкие, что отражает ограниченный объём и зависимость данных.
- Четыре zones недостаточны для сильной spatial inference.
- Модель не должна обновляться по результату будущего holdout.

## Артефакт и воспроизводимость

Full-train artifact:
`artifacts/model_selection/t1_b6_expanded_v1/full_train_primary.joblib`.
Manifest фиксирует train sample-ID hash, model spec hash, selected-parameter
hash, environment и artifact SHA-256. Перед применением обязательна проверка
suite v4 и independent validation report.
