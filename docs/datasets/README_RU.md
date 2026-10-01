# Нативные datasets: реестр, инспекция и проверяемое преобразование

`vkm_datasets` — отдельный слой логических наборов и неизменяемых версий. Он не заменяет
`SOURCE_REGISTER`: зарегистрированный документ остаётся документом, а версия набора может ссылаться
на несколько `VKM-SRC-*` через `source_ids`. Один набор может состоять из TAB/DAT/MAP/ID/IND,
MIF/MID, workbook, проекта и явно перечисленных дополнительных файлов.

## Контракт версии

`DatasetVersion` включает logical `dataset_id`, список относительных файлов с размером и SHA-256,
entrypoints, owner, licence/restrictions, received_at с timezone, data_valid_at, available_from,
source_ids и hashes предыдущих версий. Неизвестные даты остаются `null`. Дата получения не
заменяет дату состояния данных или доступность прогнозисту.

Идентичность версии — SHA-256 канонического UTF-8 JSON всего manifest, включая access policy.
Порядок файлов нормализован; `Registry` сохраняет версии как `<dataset_id>/<full-digest>.json`.
Полностью записанный/fsynced manifest публикуется через hard link с запретом замены существующего
файла (NTFS/POSIX). Повторная регистрация тех же байтов идемпотентна; изменение требует новой версии.
На файловой системе без hard-link support операция завершается ошибкой, не ослабляя атомарность.
Изменение policy также создаёт новую версию. Здесь нет mutable CURRENT или удаления истории.
На POSIX синхронизируются также созданные каталоги и directory entry manifest; повторная регистрация
повторяет file/directory sync. Pending-файлы сохраняются, автоматической очистки нет. На Windows
выполняется file fsync; durability каталогов остаётся NOT_QUALIFIED. Физическое отключение питания
не квалифицировано ни на одной платформе.

`discover` читает только перечисленные entrypoints и их MapInfo companion-файлы. Native spatial TAB
требует DAT/MAP/ID, IND включается при наличии; MIF требует MID. Неспространственный TAB разрешается
отдельным параметром создания manifest; GIS adapter квалифицирован для пространственного TAB.
Другие TAB-варианты (raster, seamless, external table) отклоняются. QGS/QGZ регистрируются как
оригинальные байты; этот пакет не исполняет проекты и не разрешает их внешние слои.

Все trust-boundary проверки используют свежий SHA-256. Paths не допускают traversal, symlinks,
junctions и Windows aliases; case-ambiguous наборы отклоняются и на Linux. Оригиналы не копируются,
не переименовываются и не изменяются. Их резервное хранение — отдельный обязательный intake gate.

## Access и экспериментальная роль

Enums импортируются из общего `vkm_corpus.contracts.access_vocab` без тяжёлых зависимостей:

- access: PUBLIC / PRIVATE_CLOUD_ALLOWED / PRIVATE_LOCAL_ONLY / RESTRICTED / SEALED;
- role: INPUT / CALIBRATION / VALIDATION / TEST_SEALED / TARGET / UNKNOWN.

`Policy` требует ссылку на решение владельца. До открытия содержимого `require(destination)`
проверяет направление local/cloud/public. PRIVATE не становится PUBLIC; LOCAL_ONLY не выдаётся
в cloud. Общая инспекция/конверсия всегда отказывает для SEALED, RESTRICTED и TEST_SEALED.
Для evaluator нужен отдельный protocol, а не флаг обхода. Разрешённое чтение TARGET не является
слепой независимой проверкой. Классификация не выводится автоматически из имён колонок.

Для смешанных файлов сначала требуется локальная классификация и отдельная допущенная проекция:
пакет не умеет безопасно выдавать разрешённые строки из workbook, содержащего TEST_SEALED. Такой
workbook целиком закрывается для общей инспекции. Решение о будущих sealed scopes нельзя заменить
обещанием, что поиск ключевого слова «оседание» найдёт все каналы утечки.

## Инспекция XLSX/XLS

`inspect_workbook` по умолчанию выдаёт metadata, без cell values, formula expressions и defined-name
expressions. `include_values=True` сохраняет нативное содержимое при разрешённой policy.

XLSX разбирается непосредственно как OOXML, без запуска Excel:

- все sheets, включая hidden/veryHidden, native cell addresses, hidden rows/columns и merged ranges;
- формула и её cached value одновременно, формульные attributes (включая shared/array references);
- native type, raw lexical value, resolved string, style index, custom number formats, date system;
- defined names, workbook relationships и SHA-256 каждого package part, включая неинтерпретированные;
- явные memory/decompression/cell guards; XML DTD/entities запрещены; архив не распаковывается на диск.

Числовая дата остаётся исходным serial вместе со style/date system. Decimal comma, ведущие нули,
похожие на обрезанные строки, ошибки Excel и пустые cells не «исправляются» эвристикой. Внешние
relationships только описываются; сеть, макросы и пересчёт формул не запускаются. Поддерживается
transitional OOXML namespace; неизвестный namespace отклоняется, а не даёт пустой PASS.

XLS требует уже установленного `xlrd`, квалифицирован для BIFF8. Сохраняет cells/cached values,
стили/форматы, hidden metadata, merged ranges, а также исходные FORMULA/shared/array records и
связанные CONTINUE/STRING records с byte offsets, hashes и RPN bytes при `include_values=True`.
BIFF dependencies/defined names также имеют нативные locators. Это `BIFF_RPN_UNINTERPRETED`:
пакет не утверждает, что восстановил формулу в infix/LaTeX или проверил её смысл. Старые BIFF
версии требуют отдельной квалификации и завершаются явной ошибкой. Формулы не вычисляются.

`Limits` настраивается явно для memory budget операции. Значения по умолчанию: 128 MiB вход,
256 MiB распакованные package parts, 32 MiB один part, 250 000 cells. Превышение означает отказ,
а не частичный workbook с успешным verdict. Для больших файлов нужен отдельный budget/streaming
профиль; автоматического неограниченного повышения памяти нет.

## Прямое преобразование GIS

`inspect_vector` и `convert_to_gpkg` выполняются в уже установленном GDAL Python runtime.
GDAL/PyQGIS не импортируются при обычном импорте пакета. API не устанавливает зависимости,
не вызывает shell и не меняет MCP configs.

Маршрут: TAB/MIF/GPKG → **GPKG напрямую**. Нет промежуточного GeoJSON/Shapefile, назначения EPSG,
перепроекции, MakeValid или интерполяции. Дополнительная проверка запрещает GDAL читать companion,
который не вошёл в неизменяемый manifest.

Проверяются все layer names, порядок/имена/types/subtypes полей, feature counts, атрибуты и WKB
каждой геометрии (хеш последовательности), CRS equivalence, invalid geometry и заявленные составные
ключи. Если keys не объявлены, receipt содержит `key_validation=NOT_RUN`; уникальность не выдумывается.
FID исходных features также сохраняются явно (`preserveFID=True`) и проверяются отдельным хешем
последовательности FID. Имя исходной FID column, исходные field/key metadata остаются в receipt и
native metadata; имя служебного FID column нового GPKG может отличаться и не подменяет исходную схему.
Переупорядочение/переписывание геометрии также приводит к отказу точного round trip; tolerant режим
или автоматическая «починка» не добавлены.

NonEarth остаётся LOCAL. Неизвестная CRS остаётся UNKNOWN, включая undefined placeholders GPKG;
появление EPSG у неизвестной CRS — отказ. `PASS_CONVERSION` означает совпадение с интерпретацией
оригинала данным GDAL, а не доказательство правильности исходного charset/CRS или научный admission.
Native charset/coordinate declarations сохраняются отдельно. Scientific admission всегда NOT_CHECKED.

MapInfo styles и исходные field metadata/CRS сохраняются в `native_metadata.json` и внутри
GPKG в `_vkm_native_metadata`: они не теряются молча, если GDAL не представляет их нативно в GPKG.
Особенности отображения style в конкретном GIS остаются отдельной ручной проверкой.

Новая output directory резервируется эксклюзивно вне original quarantine. Работа идёт в собственном
pending-каталоге. **Последним публикуется `receipt.json` — commit marker.** Если процесс прерван,
его отсутствие означает `BLOCKED / INCOMPLETE_BUNDLE_REQUIRES_NEW_ATTEMPT`, а не PASS; следующая
попытка использует новый явно выбранный output directory. Pending-каталог и bytes сохраняются.
`data.gpkg` появляется только при успешной проверке. Отказ оставляет `candidate.gpkg` (если создан)
и REJECTED receipt. Существующие bundles никогда не заменяются. `verify_conversion_bundle` проверяет
commit marker, ожидаемую dataset version, request fingerprint, свежие hashes всех outputs и совпадение
native metadata внутри SQLite с sidecar. Нельзя принимать GPKG только
по существованию файла или имени каталога. Rollback — выбор прежней допущенной версии, без удаления bytes.

Receipt v2 хранит точную identity запроса: manifest/policy, entrypoint, destination, составные keys,
memory limits, version и SHA-256 implementation files, GDAL release/build/configuration fingerprints.
Завершённый PASS можно повторно получить в том же каталоге **только** при полном совпадении identity,
новой проверке SHA-256 всех оригиналов и свежей проверке bundle. GDAL conversion повторно не запускается.
Другие входы/context/rules/config — отказ без перезаписи. Сбой после публикации marker, до подтверждения
directory sync, даёт `COMMIT_ACK_UNCERTAIN_RETRY_IDENTICAL_INVOCATION`; неизменённый повтор повторяет
проверки и синхронизацию. REJECTED receipt не превращается в successful retry.

GPKG и metadata fsync выполняются до durable записи receipt. На POSIX синхронизируются staging/output
и родительские каталоги до и после публикации marker. Любая ошибка sync/hash/rename не возвращает PASS.
Windows receipt содержит `directories=NOT_QUALIFIED`, а везде `power_loss_qualification=NOT_RUN`:
успешный receipt подтверждает программную последовательность, не завершённый физический power-loss drill.
Workbook inspector ничего не публикует: его JSON stdout не является durable intake receipt.

## CLI

`python -m vkm_datasets.cli --help` (или установленный `vkm-datasets`) предоставляет:

- `manifest`: explicit root/entrypoints, policy, owner/licence/time → JSON stdout;
- `verify`: свежая сверка manifest с original bytes;
- `register`: проверка bytes и append-only manifest registration;
- `inspect-workbook`, `inspect-vector`: metadata inspection;
- `convert`: новый проверенный conversion bundle.
- `verify-bundle`: commit marker, expected version и свежие output hashes.

Ошибки и REJECTED возвращают ненулевой exit. Для проекта не добавлялось ни одной production dataset
version и не запускалось преобразование пользовательских оригиналов.

## Проверки

`tests/datasets/test_native.py` — лёгкая синтетика: версии/replay, policy, path guards, fresh hash даже
при прежнем size/mtime, 257 колонок XLSX, кириллица, hidden sheet/cells, formulas/cached values,
decimal comma/empty/error/date-like values, memory guards, XML entities, неизвестные units и GCP response.

`tests/datasets/test_dataset_runtime.py` запускается только при заданном `VKM_DATASETS_GDAL_PYTHON`, указывающем
на существующий Python/wrapper с GDAL, xlrd и (только для генерации synthetic XLS) xlwt. Отсутствие
runtime → SKIP/NOT_RUN. Standalone runner создаёт только синтетику в переданном пустом scratch root:
TAB/CP1251/NonEarth/styles, MIF/MID, 257-column GPKG с UNKNOWN CRS, duplicate composite key,
invalid polygon, injected comparison failure, hash tamper и BIFF8 XLS с hidden sheet/formula records.
`test_durable_conversion.py` выполняет CPU fault injection для file/receipt/directory sync, прерывания
публикации, потерянного подтверждения и identical retry; проверяет fresh hashes при прежнем size/mtime,
изменение policy/engine/rules/config и embedded metadata. Sparse-FID test использует существующий GDAL
только на синтетике; FID 5/500 должны сохраниться.

GDAL использует encoding names вроде CP1251, а не MapInfo charset name WindowsCyrillic; в synthetic
writer отключено изменение field names. См. [официальный MapInfo driver contract](https://gdal.org/en/stable/drivers/vector/mitab.html).
