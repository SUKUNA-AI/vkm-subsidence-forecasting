# Qualification извлечения: frozen gold, независимая проверка и метрики

Реализован исполнимый контракт `vkm_evidence.qualification`. Он оценивает переданные immutable artifacts;
не запускает extractor, не читает корпус, не создаёт gold, не исправляет gold после неудачного результата.
Текущие tests используют только синтетические объекты. Реальная qualification корпуса **NOT_RUN**.

## Артефакты и порядок

1. Независимые авторы и reviewers готовят `GoldSet`. Каждая `GoldStratum` связана с точной версией оригинала,
   проверенными units, исходными локаторами, первичным provenance (`OriginSlice`) и receipt просмотра оригинала.
   Список объектов должен быть составлен по оригиналу, а не по outputs проверяемого detector.
2. `FrozenPlan` фиксирует SHA gold, точную identity extractor (код, config, model, tokenizer, chunking), producer/tuner
   actor IDs, provenance использованного tuning, strata, thresholds, минимальные denominators, confidence и правила
   сравнения. Порогов «99% для всего» и автоматического выбора удобного порога нет.
3. `FrozenRegistration` связывает plan SHA и gold SHA с `registered_at`, registrar и durable receipt. Регистрация
   должна предшествовать `PredictionSet.started_at`. Caller проверяет receipt по доверенному immutable журналу,
   используя `verify_registration`. Дата и hash, самостоятельно написанные evaluator, не заменяют такой проверки.
4. Extractor выпускает `PredictionSet`, связанный с plan SHA, identity extractor и фактическими producer actor IDs.
   После этого независимый reviewer создаёт `MatchAdjudication`: SHA plan/gold/predictions, дата проверки и
   однозначные пары gold object ↔ predicted object. Этот этап фиксирует эквивалентность исходных объектов при разных
   bbox/XML locator representations, не меняя gold. Полнота adjudication — отдельное утверждение reviewer.
5. `evaluate_qualification` считает метрики и gates. Изменение gold, predictions, thresholds или исходной версии
   ломает binding. Любое дальнейшее изменение требует новой явно зарегистрированной кампании, не скрытого re-score.

Registration verifier должен проверить содержание доверенного receipt, его append-only ancestry/порядок и право
registrar утверждать заморозку. Сверка дат внутри неподписанного JSON не доказывает реальное время регистрации.
Аналогично оригинальный review и provenance — проверенные человеческие утверждения, закреплённые gold SHA;
данный модуль не умеет самостоятельно подтвердить, что reviewer действительно видел оригинал.

## Strata и независимость

`StratumKey` содержит `source_id`, `source_sha256`, `format`, `quality`, `object_kind`. Для каждой strata задан
точный `unit_ids`, sampling basis и собственные thresholds. `UNKNOWN` quality — самостоятельная явно заданная
категория. Изменить source version, format, quality, object kind или unit denominator между gold и predictions нельзя.
Отсутствующая strata, FAILED/NOT_RUN или не проведённый original review блокируют qualification. Лёгкие страницы
не компенсируют плохой результат трудной strata: усредняющего общего score нет.

Автор gold, original reviewer и reviewer matching не могут совпадать с producer/tuning actors; сравнение стабильных
actor IDs нечувствительно к регистру. Caller отвечает за единый справочник principal IDs: два разных alias одного
человека/агента нельзя оформлять как независимых участников. Авторы исходной научной публикации и gold annotators —
разные роли; проверяется именно независимость авторов разметки от разработки/tuning проверяемого extractor.

Первичное происхождение проверяется существующим `lineage_overlap`:

| Provenance tuning ↔ evaluation | Допустимый scope |
|---|---|
| Разные проверенные primary origins | `INDEPENDENT_EXTRACTION_VALIDATION` |
| Один primary origin, явно непересекающиеся members | `SHARED_ORIGIN_HOLDOUT`; не независимость первоисточников |
| Пересекающиеся members или весь тот же primary origin | Только `TRANSFER_CONSISTENCY` |
| UNKNOWN/непроверенный lineage | Qualification блокируется |

При отсутствии tuning требуется `NO_TUNING_ATTESTED` с отдельным receipt SHA. Пустой список tuning origins сам по
себе не означает независимость. Gold и prediction описывают один и тот же original: их нельзя ошибочно требовать
получать из независимых источников. Проверяется отделение evaluation от использованного tuning.

## Метрики и denominators

Для позитивных strata обязательны discovery recall, discovery precision и classification accuracy. Для отдельной
негативной выборки `NEGATIVE_UNITS` используется `EMPTY_UNIT_ACCURACY`; неопределённая precision `0/0` не становится 1.

| Метрика | Numerator | Denominator |
|---|---|---|
| DISCOVERY_RECALL | Однозначно найденные gold objects | Все gold objects |
| DISCOVERY_PRECISION | Однозначно сопоставленные predictions | Все predicted objects, включая лишние |
| CLASSIFICATION_ACCURACY | Совпавший класс сопоставленных objects | Все matched gold objects; unmatched gold указан отдельно |
| EMPTY_UNIT_ACCURACY | Проверенные пустые units без ложных объектов | Все пустые gold units |
| NUMERIC/GRID/FORMULA/ENTITY/TEMPORAL_EXACT | Точные совпадения ожидаемых field slots | Все gold slots плюс неожиданные predicted slots данного типа |

В exact metrics отдельно выдаются `missing`, `incorrect`, `unexpected`. Пропущенный объект не удаляет его поля из
gold denominator; лишние числа/формулы/сущности не исчезают из error accounting. Если в artifacts обнаружен exact
домен без frozen threshold для него, qualification блокируется. Thresholds нельзя убрать после просмотра ошибок.
Квалифицирован только перечисленный `qualified_scope`; discovery-only план ничего не утверждает о числах или сетке.

Точные значения представлены отдельными строгими типами:

- `NumericField`: исходный literal, VALUE/EMPTY/MISSING/UNREADABLE/NOT_APPLICABLE, конечный decimal string и unit.
  Неизвестная единица не становится `m`, пустая ячейка не становится нулём. Exact сохраняет запись/precision:
  `1.20` и `1.2` здесь различаются; fuzzy numerical equivalence не подмешивается в эту метрику.
- `GridField`: размеры и все cells, включая empty, row/column spans. Проверка отвергает holes, overlaps и выход
  за границу. Она проходит по границам spans, не разворачивает гигантские merged areas в миллиарды ячеек.
- `FormulaField`: исходное представление OMML/MathML/LaTeX/text/image-only и symbol bindings. Exact не доказывает
  алгебраическую эквивалентность выражений и не подменяет domain assumptions.
- `EntityField`: identity, scope, RESOLVED/AMBIGUOUS/UNKNOWN и candidates. Совпадающий номер зоны в другом руднике
  не является точным entity match.
- `TemporalField`: существующий `TemporalSupport`, раздельно event/measurement/processing/publication/available_from,
  precision и notes. Извлечение UNKNOWN без подмены может быть точным; научная пригодность времени проверяется отдельно.

Каждая измеренная пропорция имеет точные numerator/denominator, point и двусторонний Wilson interval с заранее
заданным confidence. `n=0` означает NOT_RUN/неопределённую пропорцию; minimum denominator обязателен. Gate использует
заранее выбранный POINT или WILSON_LOWER. Например, 100/100 при confidence 0.95 даёт нижнюю границу примерно 0.963;
один удачный объект не доказывает тот же уровень надёжности.

Wilson здесь описательный Bernoulli interval, **не поправка на корреляцию ячеек, страниц или повторных публикаций**.
Отчёт явно отмечает `DESCRIPTIVE_BERNOULLI_NOT_CLUSTER_ADJUSTED`. Для population inference нужен отдельный заранее
обоснованный sampling/cluster design; числовая граница сама по себе не доказывает независимость trials. Модуль не
выводит результаты за пределы проверенных strata/source versions и не выдаёт bootstrap/cluster CI за Wilson.

## Callable и production gate

```python
report = evaluate_qualification(
    plan, gold, predictions, adjudication, registration,
    verify_registration=trusted_journal_verifier,
)

ready = qualification_gate(
    report,
    approved_plan,
    expected_prediction_sha256,
    require_corpus=True,
    required_scope=required_metric_map,
)
```

`evaluate_qualification` чистая функция: она не пишет files, не вызывает модель, не исправляет artifacts.
Report содержит SHA всех входов, каждый frozen gate, отдельные strata, причины отказа и собственный
`report_sha256 = record_hash(report_without_report_sha256)`. Оригинальные значения и цитаты в receipt не копируются.
Верификатор по умолчанию отсутствует; без явного `verify_registration(...)=True` итог не PASS.

Bindings моделей вычисляются как `record_hash(validated_model)`: canonical UTF-8 JSON со всеми schema defaults,
отсортированными ключами и без NaN. Это относится к plan/gold/prediction/adjudication/registration SHA.
Отдельно `BoundFile.sha256` и ключ `quality_gates` — SHA-256 **исходных bytes файла**. Эти два уровня не смешиваются:
reader сначала проверяет file bytes, затем разбирает модель и вычисляет canonical model hash. `report_sha256`
исключает собственное поле, а SHA файла report включает его вместе со всем сохранённым JSON.

`qualification_gate` получает approved plan и expected prediction SHA извне отчёта, сверяет selfhash, bindings,
population, scope, каждый gate и заново вычисляет статистику. Он не допускает synthetic PASS в production:
SYNTHETIC выдаёт только `SYNTHETIC_ONLY`; CORPUS — `QUALIFIED_FOR_FROZEN_SCOPE`. Вызов с `require_corpus=False`
предназначен для qualification самого механизма на синтетике. Runtime gate, проверяющий только `status=PASS`,
недостаточен: gate типа corpus qualification обязан вызывать этот predicate с доверенным frozen plan.

В update runtime это подключено через `RuntimeConfig.quality_gates`: ключ — SHA файла qualification report,
значение — `QualityGateBinding(plan_artifact, prediction_artifact, required_scope)`. Artifact aliases указывают на
pinned bytes approved plan/predictions. `_gate` разбирает строгие модели и вызывает predicate с `require_corpus=True`.
Quality report без такой binding отвергается; назначенный quality gate не может подменяться generic infrastructure
receipt. Поле `required_scope` обязательно и не допускает пустого списка metrics у strata.

Hash integrity не аутентифицирует произвольные writable файлы. Artifact reader должен проверять SHA самого report
file по зарегистрированному receipt и брать approved plan/predictions из доверенного хранилища, а не из присланных
полей report. Модуль не добавляет новый authoritative database или неявный источник доверия.

## Статусы и границы

- Нет gold/predictions/adjudication: NOT_RUN, qualification NOT_QUALIFIED.
- Есть artifacts, но отсутствует strata, original review, независимость, preregistration или прошедший threshold:
  BLOCKED, qualification NOT_QUALIFIED. FAILED extraction также не становится «мало объектов, зато accuracy=100%».
- Все gates выполнены: PASS в строго указанном scope и population.

Во всех случаях `scientific_admission=NOT_ESTABLISHED`, `field_validation=NOT_ESTABLISHED`. Qualification extraction
не заменяет semantic review по оригиналам, независимый field benchmark, доступность данных во времени, access policy
или честное указание TARGET visibility. Просмотр targets может быть разрешён владельцем, но это не делает тест слепым.

Синтетические tests: `tests/evidence/test_qualification.py`. Они покрывают frozen bindings, actor overlap,
source/format/quality drift, пропуски и false positives, пять exact domains, zero denominator, negative units,
Wilson bounds, partial shared origins, запрет synthetic→CORPUS PASS и независимую проверку report gate.

## Операторский CLI

`vkm-evidence qualification` читает только явно заданные JSON artifacts. Он не запускает extraction, не читает
original corpus, не создаёт регистрацию и не публикует результат в production. Каждый вход должен иметь отдельный
операторский pin SHA-256 **байтов файла**. Внутри моделей по-прежнему используются canonical model hashes.

```text
vkm-evidence qualification
  --plan approved/plan.json --plan-sha256 <plan-file-sha256>
  --gold approved/gold.json --gold-sha256 <gold-file-sha256>
  --predictions run/predictions.json --predictions-sha256 <prediction-file-sha256>
  --adjudication approved/adjudication.json --adjudication-sha256 <adjudication-file-sha256>
  --registration approved/registration.json --registration-sha256 <registration-file-sha256>
  --trusted-registration-receipt trusted/preregistration.json
  --trusted-registration-sha256 <trusted-receipt-file-sha256>
  --trusted-registrar <approved-registrar-id>
  --output receipts/qualification.json
```

Это одна команда, разбитая на строки для чтения; shell continuation зависит от оболочки. Родительский каталог
`--output` должен существовать и контролироваться оператором. CLI принимает максимум 16 MiB на каждый input и report,
отвергает duplicate JSON keys, non-finite constants, special files, symlinks и reparse points в файле или его родителях.
В stdout идут только статус, population, hashes и границы qualification; оригинальные значения, source IDs, локаторы,
actor IDs и машинные пути не выводятся. Ошибки не печатают Pydantic payload или содержимое файла.

Доверенный preregistration receipt — **отдельный заранее одобренный файл**, содержащий строго следующие поля:

```json
{
  "schema_version": "vkm-qualification-preregistration-receipt/1",
  "plan_sha256": "<canonical-FrozenPlan-sha256>",
  "gold_sha256": "<canonical-GoldSet-sha256>",
  "registered_at": "2026-10-01T00:00:00Z",
  "registrar": "<approved-registrar-id>"
}
```

Значения с угловыми скобками — placeholders, не допустимые реальные hashes. SHA-256 байтов этого receipt должен
совпадать одновременно с операторским `--trusted-registration-sha256` и
`FrozenRegistration.durable_receipt_sha256`. Plan, gold, UTC timestamp и registrar должны точно совпасть с регистрацией;
registrar также сверяется с отдельным `--trusted-registrar`. Зарегистрированное время должно предшествовать
`PredictionSet.started_at`, что дополнительно проверяет evaluator.

Источником доверия остаётся оператор: approved pins и registrar нужно получать из защищённого журнала/каталога
замороженных кампаний, а не из присланного evaluator набора файлов. Как и `RuntimeConfig.quality_gates`, эти настройки
нельзя отдавать проверяемому extractor для самоподтверждения. CLI проверяет binding receipt, но не доказывает самостоятельно
историческую неизменность журнала, фактическое время регистрации или полномочия человека. Поэтому в report стоит
`registration_trust=OPERATOR_PINNED_RECEIPT` и
`append_only_ancestry=OPERATOR_ATTESTED_NOT_AUTOMATICALLY_PROVEN`. Подписать новый JSON старой датой и самостоятельно
вычислить его hash недостаточно для доверенной preregistration. Без receipt проверка выдаёт BLOCKED, а не PASS.

Report дополняется `operator_verification` с byte pins всех входов и receipt; эти поля покрыты `report_sha256`.
Публикация: файл полностью записывается и fsync-ится во временном файле того же каталога, затем атомарно связывается
с конечным именем без возможности замены. Два конкурентных разных отчёта не могут оба занять одно имя. Повторный вызов
с идентичным report идемпотентен; другой report по тому же пути отклоняется. На POSIX fsync каталога обязателен;
на Windows `directory_durability=NOT_QUALIFIED`. Реальная проверка аварийного отключения питания — `NOT_RUN` на обеих
платформах. При сбое после публикации, но до подтверждения durability, CLI возвращает ошибку; идентичный повтор проверяет
сохранённые байты и повторяет fsync. Остаточный `.qualification-*.tmp` после аварийного завершения не считается report.

| Результат | Exit code | Значение |
|---|---|---|
| PASS | 0 | Только population и scope зарегистрированного плана |
| NOT_RUN / BLOCKED | 2 | Недостаточно входов или не пройден gate; report сохраняется, если план и output заданы |
| FAILED | 1 | Bad pin/schema/path, превышение bounds, конфликт immutable output или ошибка I/O |

Без аргументов `vkm-evidence qualification` возвращает NOT_RUN/2. `--help` выводит справку и ничего не оценивает.
Отсутствие `--output` при переданном плане — ошибка: CLI не подтверждает PASS без сохранённого report. Synthetic PASS
остаётся `SYNTHETIC_ONLY`; stdout всегда сообщает `production_ready=false`. Даже CORPUS qualification относится только
к frozen scope и не заменяет runtime/production gates. Для подключения к runtime оператор отдельно утверждает SHA
файла report, approved plan/predictions и required scope по описанному выше контракту.

CLI tests: `tests/evidence/test_qualification_cli.py`, только синтетика. Включены отсутствие preregistration, changed
pins/gold, binding времени и registrar, пропущенные/failed artifacts, запрет synthetic→CORPUS, sanitized errors,
неизменяемые retries, concurrent publication, symlink/FIFO и injected fsync failures.
