# Native load witness и владелец visual llama

Статус реализации: код и CPU synthetic checks. Реальная сборка полного llama,
загрузка m0, GPU residency, функциональная эквивалентность и production admission
**NOT_RUN**. Текущий прямой llama deployment автоматически не меняется и не
становится квалифицированным от появления этих файлов.

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
2. Собрать патчи вместе с оригинальным header на точном upstream. Docker recipe
   уже копирует header и включает его hash в PATCHES_SHA256, но image не строился.
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
