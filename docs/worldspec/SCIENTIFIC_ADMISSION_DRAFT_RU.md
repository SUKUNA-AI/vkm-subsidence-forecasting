# Допуск выбранного научного использования и DRAFT-протокол

`vkm_world.validation.scientific.admit_scientific_use` проверяет один явно выбранный контекст
использования существующих `MathModelRecord` и `MaterialParameter`. `READY` означает согласованность
контракта относительно предоставленных review-входов. Это не проверка физической истинности,
применимости решателя или качества прогноза. Формулы не вычисляются.

## Входы и закрытый контракт

`ScientificUseBinding` содержит SHA-256 WorldSpec, точной записи формулы и `ReviewIndex`, идентификатор
версии адаптера, роли существующих параметров, evidence ID одной экспериментальной/калибровочной
семьи, фактические целевые scale/scope, origin и условия stress/temperature/duration.

`ReviewIndex` — небольшой явный вход ответственного reviewer, а не новый каталог. Он задаёт разрешённые
source/evidence/law ID, hash выбранной проверенной формулы, время её доступности и точный набор hash
параметров одной семьи с материалом, методом, исходными scale/scope, условиями и диапазонами применения.
Review-статус не выводится из собственного `FACT` записи. Hash удостоверяет неизменность входа,
но не подтверждает качество evidence или полномочия поставщика review.

У каждого параметра той же семьи должен быть её evidence ID и locator в собственном
`quantity.provenance.sources`. Общей книги или одинакового текста условий недостаточно. Доступность
формулы, семейства, параметров и условий проверяется на origin. UNKNOWN, отсутствующие обязательные
условия, неизвестное время доступности и неподдержанная точная форма дают `UNDETERMINED`.
Существующие source/evidence/law ссылки проверяются перед научным использованием.
Явный `UnknownItem.blocks`, содержащий ID выбранного параметра, даёт `UNDETERMINED`; несвязанные
UNKNOWN объекты не запрещают выбранное использование. Непустой `conflicting_forms` выбранной формулы
также даёт `UNDETERMINED`: узкий профиль не имеет механизма разрешения конфликта и не угадывает его
разрешение по тексту. `known_variants` само по себе не означает конфликт.
Заданные scale/site_applicability принимаются только как точные существующие коды и сравниваются с
исходными scale/scope reviewed семьи. Неподдержанный текст и несовпадения дают `UNDETERMINED`.
При отсутствии этих полей достаточно явной reviewed семьи. Для точного `GENERAL` нужен непустой
hash-bound `FamilyReview.formula_specialization`; он не отменяет explicit-контекст или конфликт форм.
Точный `status=UNKNOWN` применимости формулы к СКРУ-1 запрещает READY для запрошенного СКРУ-1,
включая перенос туда, но не запрещает исходный NON_VKM контекст сам по себе.

Для выбранного параметра проверяется доступность внешнего metadata provenance и provenance его
quantity. Явные inputs и spatial.entity_id во внешних binding/review Quantity должны замыкаться на
существующие WorldSpec ID. Проверяются доступность и UNKNOWN только фактически потребляемых
предшественников, включая явные блокирующие gaps; несвязанные объекты остаются архивными.
Условия actual use проверяют Transfer относительно целевых осей binding, reviewed applicability —
относительно исходных осей calibration family. UNKNOWN оси не становятся придуманным контекстом.

Поддержаны только два версионированных точных контракта, включая точную строку `variables`:

| adapter_id | equation_plain | единицы параметров |
| --- | --- | --- |
| `creep-power-pa-s/1` | `strain_rate = A * stress ** n` | `A`: точно `Pa^-<n>/s`; `n`: безразмерная единица с factor=1 |
| `creep-normalized-pa-s/1` | `strain_rate = B * (stress / stress0) ** n` | `B`: известная strain-rate единица; `stress0`: известная stress единица; `n`: factor=1 |

`n` и `stress0` требуют положительного однозначного point-значения. Диапазоны коэффициентов могут
быть согласованы как контракт неопределённости; они не становятся готовым входом решателя.
Сравнение условий использует известные единицы и коэффициенты преобразования из `core.units`
к Pa, s, K. Для B и stress0 допускаются только известные, однозначные единицы с известным factor;
этот policy не даёт разрешение произвольной строке единиц. Исходные числа, единицы, ranges и статусы
сохраняются. Дискретная reviewed область условий не превращается в непрерывный интервал между
гипотезами: текущий адаптер возвращает `UNDETERMINED`.

Перенос при реальном изменении контекста требует совпадения всех четырёх осей `Transfer` с фактическим
source и запрошенным target, разрешённого явного статуса, непустых method/rationale. ANALOGUE сам по
себе не разрешает использование на СКРУ-1. Архивный неполный Transfer остаётся допустимым при хранении.
Запрос исходных scale/scope использует исходный статус и не применяет сохранённый перенос в будущий
контекст. Общий `MaterialParameter.use_errors()` проверяет этот контракт переноса; специфические
ограничения формулы, семьи, единиц и времени проверяет научный consumer.

Результат содержит стабильные diagnostics: code, severity, логическое поле/роль и SHA-256 входа.
`receipt_matches_current` повторно проверяет реальные входы; исторического `READY` недостаточно.
Существующая public запись каталога без binding/review остаётся `UNDETERMINED`; никакие reviewed
семьи или значения для неё автоматически не создаются.

## Исполняемый DRAFT consumer

`vkm_world.validation.draft.check_draft_protocol` использует текущий scientific receipt, существующие
ObservationDataset, availability/leakage guards, детерминированные splits и объявленные метрики.
`DraftProtocol(experiment_id="SYNTH-DRAFT")` допустим при хранении. Незаданные обязательные объявления
дают `NOT_READY` и коды `DRAFT_MISSING_<FIELD>`; несовместимый контракт или неверные типы дают `BLOCKED`.

Для `READY_DRAFT` нужны scientific_question, experiment_version, t0, политика origins, правило/steps
горизонта, правило доступности входов, hash binding, SYNTHETIC data_kind, все пять ролей calibration/
nroy/filtering/validation/test с явным rationale, правило независимости, code/data hashes, выбранный
dataset, features, metrics/acceptance limits, splitter, samples и плановый schedule. Неиспользуемые роли
объявляются `NOT_APPLICABLE` с rationale и пустыми ссылками. Фактически выбранные dataset и samples
должны быть включены в validation роль. Используемые роли не пересекаются по sample ID и объявленной
линии происхождения. Это проверка декларируемой независимости, а не статистическое доказательство.
Линия происхождения сохраняет evidence ID и точную комбинацию source/locator/pages одновременно:
удаление evidence ID из второй копии той же extraction location не создаёт независимость.
UNKNOWN, явно блокирующий выбранные datasets, USED dataset-роли или их заявленные входные,
system/CRS/spatial зависимости, даёт `NOT_READY`; несвязанный gap не запрещает DRAFT.

t0 совпадает с binding.origin. Каждый sample следует указанной origin policy; фактический горизонт
попадает в проверенную duration область binding. Доступность dataset и feature-источников проверяется
на фактическом sample.origin. Target выбирается из объявленного планового schedule, а не по успешности
измерения. Splitter допускает rolling-origin/forward-only или leave-one-line-out; random split и
использование sealed test samples в development folds блокируются. MAE/RMSE/absolute bias имеют
объявленные максимальные limits, но оценки здесь не вычисляются.
Metadata используемых calibration/NROY/filtering datasets проверяется на t0; validation metadata —
на фактических forecast origins. Общие observation system/CRS/spatial объявления доступны на t0.
Это доступность входного контракта, отдельно от более позднего поступления target labels.
Для обеих split-стратегий training labels должны быть доступны до validation origin. Чистое
пространственное LOLO-группирование без forward-only условия не получает forecast READY_DRAFT;
самостоятельный spatial API остаётся архивно допустимым. Новый hybrid splitter не создаётся.
Consumer один раз копирует world/binding/review до admission и использует те же собственные snapshots;
исходные mutable объекты после проверки admission повторно не читаются.

Code hashes привязаны к actual public scientific/draft, leakage, splits, metrics, provenance/Transfer,
units, materials, mathmeta.models, observations.catalog и прямым WorldSpec/core validation/hash helpers
под логическими именами. Это точный набор прямых public helpers, а не фиксация всего runtime/environment. Data hashes
относятся к metadata-записям ObservationDataset; строка values_location участвует в hash, но содержимое
файла не читается и не считается проверенным. REAL данные этим consumer заблокированы. Он не читает
sealed labels, не выполняет prediction, калибровку, NROY, фильтрацию, обучение или benchmark.
Полный протокол остаётся `DRAFT`, никогда автоматически не становится предрегистрированным.

## Offline CLI

```bash
python scripts/check_scientific_use.py --world work/world.json --binding work/binding.json \
  --review work/review.json --protocol work/protocol.json
```

Без `--protocol` проверяется scientific admission. Необязательный `--admission` проверяет предыдущий
receipt на актуальных входах. CLI публикует только агрегированные status/codes/counts/hashes; значения,
источники, quotations и пути входных файлов не публикуются. Exit code 0 — `READY` или `READY_DRAFT`,
2 — `NOT_READY`, `UNDETERMINED` или `BLOCKED`.

Синтетические позитивные/негативные примеры и e2e CLI проверки:
`tests/world/test_scientific_admission.py`. Они не изменяют числовые значения public научных каталогов.
