# Архив прежней архитектуры

После scientific reset 26.09.2026 и решений D-18/D-19 прежняя цепочка «скв. 75 → Physical World v1 (2D) → OGS reference case», реконструкции v3.x, synthetic stress lab v2/v2.1 и Gate A/B/C **retired**. Их runtime отсутствует в активном дереве; Git хранит полный исторический вариант без переписывания истории.

Исторический PUBLIC snapshot: ветка `legacy`, commit `d54025d4c47b79b864076a33a4ecf877174ec922`. Frozen results доступны по отдельным commit и SHA в [индексе legacy](LEGACY_INDEX_RU.md) и [машиночитаемом реестре](../../scripts/frozen_references.json). [Квитанция retirement](LEGACY_DATA_RETIREMENT_2026-09-25_RU.md) фиксирует причины и правила обращения с прежними данными.

Текущее состояние — [PROJECT_STATE_RU.md](../../PROJECT_STATE_RU.md), решения — [D-18/D-19](../governance/PHASE1_DESIGN_DECISIONS_RU.md). Старую архитектуру нельзя использовать как current design без прямого решения владельца. Первичные источники, корректная evidence, frozen references и retirement receipts сохраняются; возраст файла сам по себе не является основанием для удаления.

Аудит активного дерева 30.09.2026: [manifest](LEGACY_REMOVAL_MANIFEST_2026-09-30.md), [зависимости и SHA](LEGACY_REMOVAL_MANIFEST_2026-09-30.json). Единственный подтверждённый документ удалён отдельным cleanup commit `e20ca82`; прежние байты доступны в Git.
