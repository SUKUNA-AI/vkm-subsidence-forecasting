# Ограниченное извлечение semantic candidates

`vkm_evidence.extraction` выполняет один ограниченный batch по выбранным точным
текстовым spans канонического snapshot. Результат — `UNREVIEWED` evidence candidates
и отдельный receipt. Модуль не публикует журнал, не выдаёт scientific admission,
не меняет исходные объекты и не запускает корпуса автоматически.

## Доверенные входы

Оператор создаёт `ExtractionPlan`: exact `ObjectRef` со span/fragment SHA,
hash текущих source policies, actor, timezone-aware recorded_at, output policy,
AccessContext, seed, ModelPin и обязательные бюджеты. Actor/time/access/identity
не принимаются из ответа модели. Output policy обязана сохранять ограничения
всех источников. TARGET, TEST_SEALED, SEALED и RESTRICTED закрыты для этого общего
адаптера даже при более широких правах оператора.

`CanonTextSource` использует настоящий `CanonStore`. Policy проверяется до чтения
текста. Перед материализацией строки выполняется ограниченный SQL-запрос её
размера; затем проверяются snapshot, source/content hashes, extraction signature,
generation, locator, bounds и SHA исходного фрагмента. Полный объект и выбранные
фрагменты имеют отдельные byte limits. Политика и текст проверяются повторно
перед POST и перед выдачей результата.

Это работа с извлечённым canonical text, не подтверждение оригинального скана:
ошибка OCR может честно перейти в непроверенного кандидата. Проверка первичного
оригинала и scientific-use gates остаются обязательными отдельными этапами.

## Модельный интерфейс

`FixedModelClient` использует фиксированные authenticated `GET /identity` и
`POST /v1/chat/completions` у pinned endpoint. Redirects, environmental proxies,
credentials в URL и незашифрованный удалённый endpoint запрещены. LOCAL допускает
только loopback; удалённый endpoint классифицируется как CLOUD.

Native identity должна иметь schema `vkm-evidence-model/1` и поля `model`,
`weights_sha256`, `tokenizer_sha256`, `prompt_format_sha256`, `native` и
`idempotency=REPLAY_EXACT_RESPONSE_V1`. `native` — настоящий `NativeModelProof`
из существующего `LoadedModelLease` на стороне сервиса, владеющего загруженной
моделью. Полный identity hash закрепляет ModelPin; веса/tokenizer проверяются
также внутри native resources. Sidecar JSON, `/models`, имя модели и `/readyz`
не заменяют этот контракт. Старые внешние model endpoints остаются BLOCKED,
пока оператор не интегрирует и отдельно не квалифицирует этот обработчик.

Tokenizer загружается из реальных pinned `tokenizer.json` bytes через уже
закреплённый пакет tokenizers. Перед запросом считается сериализованное содержимое
messages/schema плюс явный template overhead. Значение overhead и соответствие
native prompt format требуют отдельной квалификации endpoint; расчёт не объявлен
точной оценкой токенов любого произвольного chat template. Дополнительно проверяются
native usage ответа, `max_tokens`, bytes/deadline. Общий CPU worker ограничен hard
memory/time limits; ресурсы внешнего model server квалифицируются отдельно.

Source text передаётся JSON-данными в user message, отдельно от фиксированного
system prompt. Tools пусты, `tool_choice=none`, tool/function calls в ответе
отклоняются. Источник не может выбирать shell, Python, URL, prompt или endpoint.
Эти границы предотвращают исполнение команды, но не доказывают семантическую
корректность LLM. Всё предложенное остаётся непроверенным.

## Типизация и dispositions

Поддержаны MENTION, CLAIM, OBSERVATION, OBSERVATION_SET, ENTITY, FORMULA, EVENT,
ENTITY_LINK. Каждый candidate ссылается на exact Unicode substring выбранного
input; substring и literal должны совпадать. Model local IDs преобразуются в
детерминированные IDs, зависящие от exact request. ObservationSet допускает только
свои typed Observation children с точными VersionRef; entity link — только свои
Entity и `POSSIBLE_SAME_ENTITY`, без разрешения тождества.

Числа, диапазоны, decimal comma и альтернативы сохраняются в original_value;
quantity остаётся None. Site/scale — UNSTATED, provenance — UNKNOWN, даты
доступности не выводятся из raw date или времени ingestion. Формула UNPARSED,
Entity CANDIDATE, lineage UNKNOWN. Event class — явная непроверенная гипотеза
модели PHYSICAL/INFORMATION, state UNKNOWN и неизвестные временные оси.
Предикаты FACT/review/independence/SKRU1 моделью не назначаются.

Для каждого распознанного массива candidates сохраняется disposition. Один
некорректный span/type/reference удерживает весь batch: корректные соседи HELD,
некорректные REJECTED, records пусты. Для массива сверх budget disposition
покрывает весь наблюдённый `[0, n)` диапазон; malformed envelope имеет
candidate_count=null. Пустой результат — отсутствие обнаруженных кандидатов,
не доказательство полноты. `completeness=NOT_ESTABLISHED` и
`scientific_admission=NOT_ESTABLISHED` фиксированы.

## Durable CAS и retry

`PrivateCAS` требует runtime вне PUBLIC, включая ignored `work/`. Никакие цитаты,
raw responses и prompt payloads не записываются в отслеживаемое дерево. Выход
нуждается в операторских ACL; файловые mode не объявлены заменой Windows ACL.
Symlinks/junctions и необычные/множественно связанные входные файлы отвергаются.
Объекты immutable и проверяются SHA при чтении. No cleanup выполняется.

Request key связывает план, actual source fragment bytes/identities/policy,
model/tokenizer/template pins, prompt/schema/rule/seed, adapter bytes и версии
Python/parser/tokenizer/HTTP/DB runtime. Пакет окружения и source producer
квалифицируются существующим production guard; этот module hash не заменяет
полную code/dependency qualification.

CAS сохраняет exact request, raw HTTP response, typed batch и receipt. Request
marker ссылается на raw SHA и создаётся до разбора кандидатов: повтор после потери
ACK использует те же bytes. POST передаёт request SHA как Idempotency-Key;
native endpoint обязан возвращать тот же ответ после потери HTTP ACK. До и после
cache hit проверяются текущие policy/source/model/tokenizer identities. Повреждённый
или частично записанный immutable объект блокирует retry; оператор разбирает его
отдельно, молчаливого перезаписывания нет.

CAS blob сам по себе не является успешным receipt регистрации. Только завершённый
worker result после финальных fences разрешает передать candidates следующему
этапу. Policy revocation во время обработки блокирует выдачу; внешняя передача
регулируется также неизменяемым операторским endpoint/access profile.

## Production worker

Публичная Python entry point: `run_extraction_job(ExtractionJob(...), output_dir)`.
Job связывает canonical DuckDB, source-policy file и tokenizer как
`ExtractionFile(path, sha256, max_bytes)`, credential_file задаётся оператором.
Все runtime paths абсолютные и не являются репозиторными defaults. Output не
пересекается с immutable inputs/credential; повтор требует тех же exact job bytes.

Запускается только фиксированный `python -m vkm_evidence.extraction --worker` через
существующий `bounded_subprocess`: Linux process group, RLIMIT_AS, timeout, без
stdout/stderr с текстом/секретами. Worker ставит NativeFileWatch до чтения/hashing
канонической БД/policy/tokenizer/credential; проверяет hashes и fences до/после.
Неподдержанный filesystem/OS, отсутствующие model identity/tokenizer/budgets или
ошибка enforcement закрывают запуск. In-process PRODUCTION также требует hard
RLIMIT_AS; mock transport допустим только в SYNTHETIC. Windows portable contract
tests не являются квалификацией production execution.

В этой реализации выполняются только synthetic CanonStore + mock HTTP тесты.
Реальный model hook, physical endpoint resource qualification, semantic corpus
campaign, независимый gold set и экспертная проверка originals — NOT_RUN.
