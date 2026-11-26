# A03 — Саморевью, активные зависимости и очистка
26.11.2026. [Активный этап](../planning/AMHF_ACTIVE_STAGE_RU.md), [A01](A01_AMHF_ALGORITHM_SPEC_RU.md), [A02](A02_IMPLEMENTATION_RECALC_VERIFICATION_RU.md).

## 1. Исправления математического и алгоритмического проекта
| Прежний риск | Решение | Проверка |
|---|---|---|
| Формальная DAE без условий | T01 фиксирует ветвь, R_q, начальные данные и события, алгоритмическая tangent не равна непрерывной | согласованность и контроль active set |
| Один универсальный закон оседания | физическая память/контакты/границы; простые функции baseline | пути нагрузок, G1 |
| Гессиан из P1/TIN или редкой сети | T03 слабое/гладкое восстановление и разрешимость | manufactured solutions, шум, 1D/2D masks |
| Уравнение максимума при нескольких extrema | локальное невырожденное условие; активные ветви; logsumexp нижний | crossing, boundary, plateau |
| G3 смешивает перенос и форму | Q-норма, ограниченное движение, regularity, causal ablation | масштабирование, миграция, шум и датум |
| Новая память вместо существующей | H8 exact coefficient/support storage; перенос только qualified | restart и memory load/work parity |
| Непроверенный K/G и Schur | единицы, baseline, locality, far interactions, режим | fine/coarse, ports, goals |
| NROY принят как реализованный | существующий design guard; отдельные calibration/screening, bounded inverse | leakage/empty/unresolved tests |
| SVD без масштаба и nuisance | whiten, project nuisance, future sensitivity | rank, alternative trajectories |
| Сценарный диапазон как вероятностный | sample range/partial coverage, conditional future scenarios | independent validation |
| BF16 для всей механики | FP64 reference, mixed selective, fallback | long memory/energy/near-singularity |
| Все гипотезы и старые критерии обязательны | точечная зависимость и research extensions PARK | actual handoff |
Идентичность formulas не доказывает научную новизну. RC-T2A, RC-T4, RC-T5, G1, G3, G5 remain TEST_REQUIRED. Теория v0.3 не признана доказанной. Независимое научное ревью и точные страницы литературы остаются открыты.

## 2. Маршрут инструкций
AGENTS — постоянные научные правила + ссылка на один current stage. CLAUDE — указатель, не дублировать. PROMPT_GPT_AGENTS — вход AMHF, не R3. README/PROJECT_STATE — актуальная навигация + явно датированные исторические факты. WORLD_KERNELS_DESIGN — v0.2 архитектура и исторические solver roles, current stage overrides. VALIDATION_POLICY — сохранить guards; разработка не равна открытию test/ML. Согласовать PRIVATE AGENTS, CLAUDE, README/CURRENT_STATE и карту входов. Локальные вложенные AGENTS, .codex/.mcp, skills, harness, CI/queue/entrypoints проверить на workstation; remote review не доказывает их полноту. Не удалять универсальные bridge modules только из-за PARK.

## 3. Legacy и расчёты
Проверен исторический LEGACY_REMOVAL_MANIFEST_2026-09-30: src/skru1, старые Gate, v3.x/bootstrap retired исторически; не повторять удаление отсутствующих путей. Сохранить frozen_references, verify_canonical_repository, build_evidence_from_legacy, run_kit, действующие validation/corpus dependencies, индексы и receipts. KEEP HOT: готовый C1/C4, необходимый PRE и модели, память/observations, новые H8. KEEP ARCHIVE: принятые уникальные траектории, failed logs, raw originals, публикации. PARK: OGS/MFront, CAD спецчасти, GPR, старые viewer и предыдущие кампании вне зависимости. CLEAN_CANDIDATE: проверенные дубли, scratch, cache, повторные распаковки, воспроизводимые матрицы, ненужные локальные preview. Удаление только после фактического локального manifest (path, scope, owner, ID, bytes allocated, hash, links/inode, dependencies, replacement, readback/restart, reason, proposed disposition) и явного согласия владельца. Никакого git clean/prune/gc, массовых удалений, изменения frozen. Учитывать hardlinks; не суммировать старые логические байты как текущую экономию.

## 4. Материалы
PUBLIC math/physics/observation registries — вход к формулам, не список реализованных моделей. PRIVATE карта AMHF_INPUTS_AND_MATERIALS_RU.md — конкретные SRC и расчётные поставки, не дублировать корпуса. SRC-234/235,237/239,213 обязательны как адресная литературная работа C2/C3/C5; проверять перенос, не выдумывать свойства. Оригинальные книги могут быть LFS pointers; локальный corpus MCP и проверенные страницы — правильный маршрут. Теория и алгоритм могут развиваться параллельно, но статусы источников/проверок не смешивать.
