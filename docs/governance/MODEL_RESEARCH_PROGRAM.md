# Model research programme

Текущий scope и порядок работы:
[каноническое состояние](../CANONICAL_RESEARCH_STATE_RU.md).
R1 фиксирует варианты будущего сравнения, R2 ограничивает интерпретацию данных.
Следующий самостоятельный протокол должен исследовать устойчивость B1/IMM
и допущенных R1 comparators на v2.1, с mechanism/parameter holdouts,
train-only preprocessing и заранее определёнными критериями.

Ни число моделей, ни neural architecture не являются доказательством качества.
Model selection не использует evaluation truth; historical results остаются
привязаны к своему dataset/commit. IMM — кинематическая модель, не
геомеханически откалиброванный prior. Physical worlds требуют недостающих
источников из R2; текущая готовность WEAK.

Старый широкий B/C model programme и его результаты сохранены в
[истории c5da110](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/tree/c5da1103154c29e2ffe9c52bbceacaa032f1690f).
Он не задаёт обязательный список новых запусков и не разрешает повторное
использование раскрытого test для настройки.
