# Model Card: Z01 ElasticNet

## Роль и статус

- Model ID: `Z01_elastic_net`.
- Семейство: regularized linear regression.
- Environment: `b6_cpu`.
- Feature view: `SAFE_ALL`.
- Selected parameters: `alpha=0.01`, `l1_ratio=0.9`.
- Suite v4 role: interpretable `context_only`.
- Primary eligibility: **не пройдена**.

## Назначение

ElasticNet служит интерпретируемым small-data control: он проверяет, даёт ли
линейная разреженная комбинация разрешённых признаков устойчивое улучшение без
state-space assumptions. Все imputation, encoding и scaling fit только на
соответствующем train; IDs не попадают в estimator matrix.

## Train-only evidence

| Метрика | Значение |
|---|---:|
| Rolling pooled MAE | 6,222 мм/год |
| B1 skill | +1,41% |
| Audit-tail MAE, full core | 6,529 мм/год |
| Equal-profile macro MAE | 7,330 мм/год |
| Equal-zone macro MAE | 8,344 мм/год |
| Worst-zone MAE | 12,948 мм/год |
| Pooled transition MAE | 9,762 мм/год |
| Volatile-or-gap MAE | 6,818 мм/год |
| 95% conformal coverage | 0,951 |
| Conformal WIS | 4,093 |

## Решение eligibility

Модель прошла broad temporal screen, но не может заменить B7. Она нарушает
gates rolling MAE, audit-tail MAE, transition improvement, leave-profile,
leave-zone, worst-zone и sign consistency. Profile-cluster sensitivity delta
`Z01 − B7` равна +0,582 мм/год с интервалом [0,072; 1,190], поэтому имеющееся
train-only evidence систематически не поддерживает замену B7.

## Допустимое использование

- интерпретируемый context-only comparator;
- sanity check линейной зависимости признаков и target;
- воспроизводимый baseline для последующих Gate C/D моделей.

Запрещено объявлять Z01 финальной моделью, менять primary по будущему holdout
или перенастраивать Z01 по историческому validation/раскрытому test.

## Ограничения

Линейная форма не описывает switching dynamics и хуже переносится на новые
profiles/zones. Коэффициенты отражают ассоциации внутри feature contract, а не
причинные эффекты. Correlated/repeated origins ограничивают классическую
интерпретацию standard errors; модель оценивается prediction-first метриками.
