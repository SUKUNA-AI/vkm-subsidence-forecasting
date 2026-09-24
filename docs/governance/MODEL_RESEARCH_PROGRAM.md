# Model research programme

Текущий scope и порядок работы:
[каноническое состояние](../CANONICAL_RESEARCH_STATE_RU.md).

R1 фиксирует варианты будущего model comparison, R2 ограничивает
интерпретацию stress-lab данных. После R2 приоритет исследования изменён:
сначала evidence-backed physical branch, затем новый model benchmark.

Текущая последовательность:

1. Physical Evidence Consolidation.
2. Physical World v1 contract.
3. Первый ограниченный 2D/2.5D OpenGeoSys reference case.
4. Physical ensemble / mechanistic robustness layer.
5. Только после этого — новый preregistered comparison B1/Kalman/IMM/R1 comparators.

Ни число моделей, ни neural architecture не являются доказательством качества.
Model selection не использует evaluation truth; historical results остаются
привязаны к своему dataset/commit. IMM — кинематическая модель, не
геомеханически откалиброванный prior.

v2.1 остаётся controlled factorial/adversarial stress benchmark и не
превращается задним числом в physical dataset.

Старый широкий B/C model programme и его результаты сохранены в
[истории c5da110](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/tree/c5da1103154c29e2ffe9c52bbceacaa032f1690f).
Он не задаёт обязательный список новых запусков и не разрешает повторное
использование раскрытого test/evaluator truth для настройки.
