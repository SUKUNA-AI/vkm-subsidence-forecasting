# Native load witness и владелец visual llama

Статус на 04.10: выполнена изолированная offline сборка полного llama и wheel
чистого component commit `6b8a0b3658d098dfe77dc2254eda56ce6b9109eb`, включая
ABI CPython 3.13.15, pip check и native version. Этот образ предшествует
расширению placement и подтверждает только build/ABI. Изменённый native getter
placement дополнительно прошёл compile/link на полном pinned upstream в
отдельном контейнере без модели и GPU. Полный immutable образ нового компонента,
загрузка m0, функциональная эквивалентность и production admission **NOT_RUN**.
Текущий прямой llama deployment автоматически не меняется и не становится
квалифицированным от появления этих файлов.

## Граница владения

`OwnedHookChildOwner` — явно выбранная стратегия `NATIVE_LOADED_WITNESS_V1` с
собственной схемой `vkm-owned-hook-child-recipe/1`. Обычный `OwnedChildModelOwner`
продолжает требовать реальные mmap weights/mmproj. Отсутствие mmap не включает
новую стратегию автоматически. Нельзя attach к уже работающему процессу.

Перед собственным `Popen` устанавливаются Linux native file watches, фиксируются
SHA-256 и inode/signatures executable, native implementation и всех ресурсов.
После запуска проверяются фактические parent PID, start ticks, argv, executable,
network namespace и принадлежащий именно ребёнку loopback listener. Только после
этого тому же клиенту, который обслуживает inference, разрешён private challenge.
В production фактические executable/.so mappings ребёнка должны входить в
зафиксированный до запуска implementation inventory. Библиотека, загруженная
позднее вне этого inventory, закрывает квалификацию.
В production inventory/watch обязательно содержит также фактический resolved
`sys.executable` parent и все fixed bridge modules, включая placement reader.
Пропуск Python binary отклоняется до запуска native child.

Патч `0002-vkm-owned-loaded-witness.patch` относится к точному upstream commit
`4da6337767f973e2b4d0797e5b323d77d8565e4a`. Оригинальный заголовок
`vkm-loaded-lifetime.h` не зависит от llama/json. Один mutex удерживается на
всём `server_context_impl::load_model` и `destroy`; epoch повышается до изменения
состояния, а seal выполняется только после успешной полной инициализации.
`server_context::get_loaded_witness` вызывает getter под тем же mutex и читает
текущие model/context/mmproj/vocab handles и фактические resource paths.
Неготовая или занятая загрузкой граница возвращает 503. Проверяется связь
`llama_get_model(ctx_tgt) == model_tgt`.

Принимается только первый epoch=1, `capture_before_load=true`, точный upstream,
свежий nonce и неизменный live core ответа. Sleep/destroy/reload, неуспешная
загрузка, иной handle, source path или изменение файла делают прежнюю lease
непригодной. Восстановление прежних bytes не восстанавливает её автоматически.

## Протокол и serving

`VKM_OWNED_WITNESS_TOKEN` генерируется только владельцем перед spawn. Native private
mode требует его в `X-VKM-Witness-Authorization` и допускает только GET
`/health`, `/props`, `/__vkm_loaded_witness` и POST `/embedding`, `/embeddings`,
`/v1/embeddings`, `/tokenize`. Mutation endpoints LoRA, slots, control и router
закрыты. Пустой/неправильный private token запрещает запуск. Обычный upstream
режим без этой переменной сохраняет прежнее поведение. Secret не передаётся в argv.

`visual_owner_bridge` сам создаёт ребёнка и единственный прямой `httpx.Client`
без proxy environment, redirect, event hooks или другого transport. Внешний
loopback proxy использует именно этот клиент; разрешённые route names фиксированы
кодом. `/identity` требует отдельный pinned bearer и отдаёт только `NativeModelProof`.
Ответ inference полностью ограничен по размеру и буферизуется до повторной
проверки lease; invalidation во время запроса не выпускает ответ клиенту.
Streaming, произвольные URL, query arguments, duplicated JSON fields и body
выше лимита отклоняются. HTTP request lines, native errors и input bodies не
пишутся в журнал; outward errors имеют фиксированные сообщения.

Полный private witness с handles и локальными путями остаётся между parent и
child. Public-safe identity содержит только SHA-256, тип границы и состояния.
`tokenizer_binding=EMBEDDED_WEIGHTS_VOCAB` обозначает фактически загруженный vocab
в GGUF; его ресурсный hash равен weights. Отдельный gateway `tokenizer.json`
проверяет собственная gateway lease. Наличие JSON-файла возле модели не означает
его загрузку llama. MLP head и scoring остаются в существующем gateway.

## Установка и дальнейшие гейты

1. Подготовить утверждённый local-only runtime recipe: абсолютные прямые host
   paths остаются вне PUBLIC; все исходники bridge и implementation files
   должны иметь точные hashes. Нативный child сохраняет текущие pinned Q6_K
   weights/Q8_0 projector, embeddings/last pooling и существующие параметры
   placement. Новый список параметров или override требует отдельного профиля.
2. Собрать патчи вместе с обоими оригинальными headers на точном upstream.
   Recipe копирует headers и включает их hashes в PATCHES_SHA256. Образ с новым
   placement getter требует нового чистого component commit и build manifest.
   Проверить полный compile/link, actual native endpoint и реальную mapped
   implementation closure, включая загружаемые CUDA/ggml/системные библиотеки.
3. В отдельном образе/entrypoint с установленным wheel запустить
   `python -m vkm_corpus.update.visual_owner_bridge --recipe <private-json> --recipe-sha256 <sha>`.
   Native child port и proxy port различны; gateway использует proxy port.
   Current compose не переключён. Его прежний unauthenticated direct-child
   healthcheck несовместим с private mode; новый recipe должен проверять proxy.
4. Выполнить native identity acceptance и функциональные parity/negative checks
   существующего m0 route на отдельно утверждённых входах; сохранить receipts.
   До них нельзя объявлять модель READY, менять serving generation или считать
   scientific admission пройденным. Ни witness, ни успешные synthetic tests не
   подтверждают GPU residency или качество reranking.
5. При отказе закрыть owner и его дочернюю process group. Вернуть предыдущий
   утверждённый deployment recipe отдельной процедурой operator rollback;
   автоматического повторного attach/reload в этой реализации нет.

SYNTHETIC recipe не может открыть внешний proxy listener или экспортировать
production NativeModelProof через `/identity`. Проверки предполагают неизменяемый
operator-owned image и файлы на поддерживаемой Linux local filesystem, а не
защиту от привилегированного вмешательства в память процесса. Windows, DrvFS и
сетевые файловые системы не квалифицированы для этой native boundary.

## Фактическое размещение target weights

Расширение `vkm-native-target-weight-placement/1` читает `tensors_by_name` именно
загруженного `model_tgt`, связанные `ctx_tgt`, настоящие tensor data/buffer/base,
buffer type/device, размеры allocations и цепочки `view_src`/offsets. Чтение
выполняется внутри того же lifecycle mutex и входит в live witness с nonce/epoch.
Значения tensor не читаются. `no_alloc`, null/zero storage, циклические или
слишком длинные views, выходы за storage span, split/meta/unsupported device и
противоречивые buffer identities закрывают witness. Requested `n_gpu_layers`,
load logs и оценки scheduler memory не используются как подтверждение.

Классификация GPU требует backend device типа GPU **и** обоих отрицательных
host-флагов buffer/buffer-type. `CUDA_Host` остаётся HOST даже при device CUDA.
Группа output определяется принадлежностью tensor настоящим model output,
output_norm или output_b; при tied weights одно имя может обозначать разные
allocations, поэтому occurrences различаются по `(name, tensor)`. Если один
tensor принадлежит и input, и output, обе роли сохраняются как explicit aliases;
его размещение не становится двумя независимыми allocations. Группы `blk.N`,
`output` и `other` показывают GPU/HOST/MIXED; allocations
считаются один раз по buffer identity. SHA-256 включает все per-tensor bindings,
shape/type, view offsets и identities, поэтому перестановка при сохранении
counts/bytes тоже инвалидирует прежнюю lease.

Authenticated `/placement` использует тот же pinned credential, owner и serving
boundary, что `/identity`. Наружу выдаются typed summary и hashes instance,
process, witness и placement; raw pointers, tensor names и native paths не
выдаются. В production отсутствие placement не допускается; synthetic transport
не экспортирует production placement или identity. Ответ защищён повторной
проверкой owner до и после формирования, как inference.

Контракт доказывает только `LLAMA_TARGET_WEIGHT_PLACEMENT`: mmproj placement,
context allocations, kernel execution, производительность и полная GPU residency
остаются `NOT_PROVEN`. Generic `NativeModelProof.gpu_residency` не повышается.
Отдельный guard профиля требует полный inventory групп: actual effective и
total layer count 28; GPU `blk.9`…`blk.27` и `output` на CUDA0; HOST `blk.0`…`blk.8`
и input/other. Это следует из pinned upstream `llama-model.cpp`:
`i_gpu_start=max(n_layer_all+1-n_gpu_layers,0)`, а output получает bucket
`n_layer_all`. При `-ngl 20` число GPU buckets равно 20. Пропущенная HOST группа,
замена одной GPU группы другой при том же count или MIXED группа не проходят
приёмку. Одна запись в argv не выполняет guard. CPU-only модель для runtime
rehearsal не запускается.

## Выполненные проверки

- Синтетические Python tests: `tests/corpus/test_visual_owner_bridge.py` и
  прежний `tests/corpus/test_model_owner.py`; повторные чтения, malformed/duplicate
  witness, epoch/reload, actual parent/listener, changed paths/handles/files,
  заменённый client/auth/transport, dropped in-flight response и factory guards.
- Оригинальный CPU C++ test `tests/native/test_loaded_lifetime.cpp` компилируется
  с C++17, `-Wall -Wextra -Werror -pthread` и проверяет mutex, live getter,
  failed-load/destroy invalidation, epoch и auth helpers. Полный llama не связан
  с этим test binary.
- `patch --dry-run` проходит на четырёх полных cached upstream файлах с
  независимо проверенными Git blob SHA. Это подтверждает применимость diff,
  но не заменяет полный build/runtime test.
