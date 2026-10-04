# PW-03: load-time identity владельцев моделей

Дата проверки: 04.10.2026. Статус действующих text/visual owners: **UNWIRED**.
Добавлены исполняемые Python/owned-child границы и CPU qualification. Это не
доказательство текущих загруженных production моделей, GPU residency, parity,
готовности всего serving или научного допуска.

## Фактическая topology

- Text inference идёт в существующий `careerops-reranker`. [EDGE Compose](../../infra/edge/compose.yml)
  явно оставляет его вне VKM Compose project. Actual code — CareerOps
  `src/careerops_reranker/runtime.py` и `server.py`: `JinaRerankerRuntime.load`
  загружает tokenizer/model, записывает `model._tokenizer`, сохраняет `_model`,
  `_tokenizer`, `_prompt_formatter`; HTTP inference вызывает этот runtime.
  `readyz` возвращает revision/config identity, но не hash загруженных bytes.
  Authenticated `/identity` отсутствует. CareerOps в этой работе не изменялся.
- Visual inference идёт непосредственно в `llama-server` на отдельном loopback
  порту. [Dockerfile](../../infra/edge/llama-server/Dockerfile) закрепляет upstream
  `4da6337767f973e2b4d0797e5b323d77d8565e4a`, entrypoint — сам native binary.
  Существующий patch пропускает `lm_head` при embeddings; identity hook он не
  добавляет. Новый Python hook пока не является entrypoint этого образа.
- [Gateway lease](../../src/vkm_corpus/update/remote_rerank.py) запрашивает
  authenticated `/identity` через **тот же HTTP client**, который используется
  для inference, и сверяет его endpoint. Нельзя направить identity в другой
  сервис или добавить receipt-sidecar, оставив inference в старом процессе.

## Реализованная граница text

[TextRuntimeOwner](../../src/vkm_corpus/update/model_owner.py) вызывается внутри
actual model owner вокруг настоящего runtime loader, до публичного admission.
Он использует `LoadedModelLease` с watch-before-hash-before-load и проверяет:

1. Before-load serving getter пуст: уже установленный runtime нельзя
   квалифицировать ретроспективно.
2. Actual runtime loader создаёт модель/tokenizer/formatter; runtime и formatter
   code входят в inventory, установленный до загрузки. Source actual model
   class дополнительно проверяет существующий `LoadedModelLease`.
3. Установленный runtime — тот, который читает inference handler. `_model`,
   `_tokenizer`, `model._tokenizer` и formatter не могут быть заменены.
4. Config, dependency versions, процесс и ресурсные bytes остаются привязаны к
   загрузке. Любая обнаруженная подмена permanently invalidates owner; возврат
   прежнего object не возобновляет lease.
5. `serving()` проверяет границу до и после inference. Сам interface
   произвольный inference не запускает.

Actual CareerOps wiring должно заменить вызов `JinaRerankerRuntime.load` в
`serve` на этот load-time owner, использовать getter actual HTTP server runtime
и guard actual `runtime.rerank`. Нужен полный immutable local inventory weights,
tokenizer и remote custom code; скачивание/HF mutable cache aliases в загрузке
не квалифицируются. В том же owner handler требуется authenticated `/identity`
с no-store и отказом при закрытой/invalid lease. Изменение revision, dtype,
device, truncation, scoring и request contract не требуется.

Это будущая интеграция external owner. Наличие harness в PUBLIC её не заменяет.

## Реализованная граница owned native child

`OwnedChildModelOwner.start` сам запускает **нового собственного child**. API
`attach(pid)` отсутствует. Installed owner code задаёт recipe; corpus/generation
manifest и HTTP payload не могут выбирать command/loader/executable.

До spawn устанавливаются native mutation watches и проверяются exact hashes
полного recipe inventory. Проверяются ordinary files, отсутствие symlink и
hardlink aliases, explicit loopback/model arguments и bounded environment.
Spawn использует literal argv, без shell, не наследует environment и не
перезапускает child автоматически. Startup bounded; failure останавливает только
этот собственный child/session.

После spawn proof связывает:

- exact serving client object и endpoint;
- child PID, parent PID, start ticks, actual cmdline и executable inode;
- общую network namespace и actual child-owned loopback listening socket;
- actual readable mappings weights **и** mmproj с device/inode исходных файлов;
- mutation watches, bytes pins, recipe и parent identity на срок lease.

Исчезновение mmap, listener, child, client или исходной identity permanently
закрывает lease. File-read, hash, readyz, наличие fd или сохранённый digest не
подменяют mmproj mmap. `functional_qualification=NOT_RUN` и
`gpu_residency=NOT_PROVEN` сохраняются в typed identity независимо от успеха
resource/process boundary.

Этот вариант применим только когда actual native implementation удерживает
нужные mappings. Если mmproj читается в отдельные buffers и mapping отсутствует,
qualification закрывается: требуется native hook. Нельзя ослаблять проверку до
«mmproj file существует» для получения зелёного статуса.

## Native llama seam для следующей интеграции

В [закреплённом server-context.cpp](https://raw.githubusercontent.com/ggml-org/llama.cpp/4da6337767f973e2b4d0797e5b323d77d8565e4a/tools/server/server-context.cpp)
actual load находится в `server_context_impl::load_model`: после
`common_init_from_params` actual `model_tgt`/`ctx_tgt` принимаются только при
nonnull; mmproj загружает `mtmd_init_from_file`, результат сохраняется как `mctx`.
`destroy` сбрасывает model/context и освобождает `mctx`; sleeping вызывает destroy,
а resume повторяет load. Hook должен связывать эти реальные handles с captured
inputs и инвалидироваться на destroy/sleep/reload, а не выдавать snapshot startup
log. Это конкретный native seam, **не реализованный C++ patch**.

Для direct `/identity` необходимо добавить authenticated handler в том же
native serving process и bytes/mutation/load-lifecycle proof. Альтернатива —
владеть новым child в actual inference owner и проксировать inference через
тот же связанный client; endpoint/config changes тогда требуют отдельной полной
qualification. Python harness нельзя включить как соседний endpoint текущему
direct llama-server.

## CPU evidence и незапущенные gates

[Owner tests](../../tests/corpus/test_model_owner.py): **43 PASS Linux**, **28 PASS
Windows / 15 Linux NOT_RUN** в адресных прогонах 04.10. Linux использует tiny
synthetic child, procfs, local inotify и mappings синтетических файлов.
Windows проверяет typed recipes/text object bindings, без Linux kernel proof.
OS bytecode caches в окончательной квалификации разделены.

Negative cases: другой serving object/runtime/tokenizer/formatter/client,
retrospective installed runtime, resource mutation и восстановленный mtime,
неполный/ambiguous recipe, sidecar endpoint, child exit, listener replacement,
mmproj без mmap, foreign ready listener, hardlink alias и incorrect file hashes.
Это qualification границ, не actual model quality evaluation.

Остаются **NOT_RUN / OPEN**:

- wiring actual CareerOps loader/handler и direct llama native owner;
- compile/build новых immutable owner images;
- actual deployment и native proof реальных weights/tokenizer/mmproj;
- actual load/functional/VRAM/parity и 49-tool serving acceptance;
- qualification process restart, native reload/sleep, backup/restore с реальными
  immutable images и policy;
- live promotion и разрешённое владельцем production switch.

Ни metadata schema, ни synthetic child, ни этот документ не предоставляют
ordinary READY или scientific admission.
