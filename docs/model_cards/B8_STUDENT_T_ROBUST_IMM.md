# Model Card: B8 Student-t robust IMM

## Роль и статус

- Model ID: `B8_student_t_robust_imm`.
- Семейство: B7 dynamics с bounded-influence Student-t observation update.
- Environment: `b6_cpu`.
- Suite v4 role: `context_only_comparator`.
- Primary eligibility: не рассматривается как новая замена; B7 остаётся
  preregistered primary.

## Назначение

B8 проверяет узкую гипотезу: может ли robust innovation likelihood уменьшить
влияние noise/outliers, не меняя динамику B7. Degrees of freedom и остальные
параметры были заморожены до expanded B6 comparison.

## Train-only evidence

| Метрика | Значение |
|---|---:|
| Rolling pooled MAE | 5,748 мм/год |
| B1 skill | +8,92% |
| Audit-tail MAE, full core | **5,831 мм/год** |
| Equal-profile macro MAE | 5,798 мм/год |
| Equal-zone macro MAE | **4,928 мм/год** |
| Worst-zone MAE | 8,920 мм/год |
| Pooled transition MAE | 9,572 мм/год |
| Volatile-or-gap MAE | 7,068 мм/год |
| 95% conformal coverage | 0,953 |
| Conformal WIS | 3,912 |

B8 лучше B7 на audit tail и немного лучше по equal-zone macro, но хуже по
pooled rolling, equal-profile macro, worst-zone и transition metrics.
Profile-cluster sensitivity delta `B8 − B7` равна +0,109 мм/год, а интервал
[−0,065; 0,268] пересекает ноль. Текущие 14 profiles не дают устойчивого
основания объявить одну из моделей внешне лучшей.

## Ограничения

- Robust observation update не подтвердил требуемое преимущество на
  volatile-or-gap.
- Выигрыш на одной audit date не является основанием для нового tuning loop.
- Модель наследует зависимость от коротких histories и широкие intervals.
- Использование допустимо только как immutable context comparator suite v4.
