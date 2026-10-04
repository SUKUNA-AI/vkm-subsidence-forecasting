# Изолированный native visual owner: сборка и квалификация

Этот каталог готовит отдельный GPU image с actual owner proxy и native load-time witness.
Копирование image labels или сохранённого digest не доказывает загруженную модель.
Действующие CORE, gateway и базы этим compose не переключаются.

## Неизменяемые входы

- Native source: `4da6337767f973e2b4d0797e5b323d77d8565e4a`; полный Git tar
  `eef03eca6f90860158bc1b237845a60492520270ab88b023f9f5cafefcb8d5f1`.
- CUDA devel/runtime 13.2.1 Ubuntu 24.04 и Python Bookworm закреплены полными
  digest в Dockerfile и verifier. Реальные image IDs проверяются до сборки.
- Python base фактически содержит CPython **3.13.15**. Перенос `/usr/local`
  в CUDA runtime считается допустимым только после реального ABI/stdlib guard,
  установки hashed wheels без сети, `pip check` и проверки compiled extensions.
- Две patches и native header из `infra/edge/llama-server` входят в полный
  SHA-256 inventory. Источник PUBLIC component — чистый Git archive точного commit;
  wheel из dirty working tree не получает identity этого commit.
- Apt archives и cp313 wheels скачиваются отдельно из официальных источников,
  затем входят в `inputs.json`, `SHA256SUMS` и `dependencies.lock.txt`.

`verify_inputs.py --root <new-context> --manifest-sha256 <approved-raw-SHA256>`
проверяет исходные bytes против каждого pinned Git blob, состав контекста, все
software hashes и общий лимит source/software **1 GiB**. В compile context
попадает только проверенный tar и перечисленные artifacts; `.git`, hooks,
configs, модели, receipts и logs исключены `.dockerignore`.

Отдельный `Dockerfile.cached-native` использует exact уже собранный native stage
`sha256:c0c0280b8f59e040fadaba3f14bea0f9d8dae93c86fd9de7ca76f8203006609a`.
Это производная сборка из проверенного кэша, а не повторный полный cold build.
`verify_native_cache.py` до compilation сверяет все 3648 исходных файлов,
compiler/CMake configuration, 43 binary/dependency файла и 10 library aliases.
`native-cache-plan.json` разрешает только три overlays: server-context и два
headers. После overlays и compilation исходная closure проверяется повторно.
Кэш не подменяет новый clean PUBLIC wheel, его dependency lock и Python base.
Четыре cached-route inputs допускаются verifier только вместе и с exact hashes;
candidate source hashes связаны с теми headers и cpp, которые входят в context.
Cold manifest не разрешает неучтённый cached delta. Host-injected `libcuda.so.1`
до реального GPU preflight остаётся `RUNTIME_NOT_PROVEN`.

## Сборка

Перед сборкой независимый проверяющий принимает exact raw manifest hash,
base IDs/digests, все build ARGs и clean wheel producer receipt. Сеть сборки
запрещена. Native compilation: sm75, не более двух CPU и 8 GiB, timeout
1800 секунд; staging не более 5 GiB, свободный диск не менее 100 GB.
Фактическое соблюдение cgroup budget требуется проверить у выбранного Docker
builder; наличие flags в команде само по себе этого не доказывает.
Native stage дополнительно проверяет реальные cgroup v2 `memory.max=8589934592`,
`memory.swap.max=0`, `cpu.max=200000 100000` до установки или compilation и
сохраняет `BUILD_CGROUP_LIMITS.txt` в image.

Успешная сборка проверяет stdlib/ABI, dependency closure и `llama-server --version`.
`ldd` direct executable не должен содержать missing dependencies. CUDA backend
и host-injected driver дополнительно проверяются в GPU container перед load;
`--allow-shlib-undefined` не разрешает скрывать отсутствующий runtime dependency.

## GPU runtime и fallback

Отдельный project `vkm-owner-gpu-20261004`; child loopback `28083`, owner proxy
`28084`. До запуска доказать отсутствие listeners и принадлежность всех объектов
этому project. UID 10001 должен читать новые control/token files и существующие
readonly weights без изменения permissions оригиналов.
`gpu_preflight.py` принимает только exact approved `-ngl 20`, `--fit off`,
`CUDA_VISIBLE_DEVICES=0` и исходные остальные настройки. Он до model load
проверяет CUDA backend через `ldd` и реальную native `--list-devices` с одной
GTX 1650 / CUDA0. Его файл тоже входит в watched implementation inventory.
Все implementation bytes, включая resolved Python binary, сначала проходят
SHA-256 и mutation watch: непроверенный executable не вызывается даже для
`--list-devices`. Вывод metadata ограничен при чтении pipe, а deadline завершает
owned process group. После actual model load strict target-weight placement
проверяется до открытия внешнего proxy; HOST-only, неверные или неполные группы
не получают listener. Generic model proof сохраняет `gpu_residency=NOT_PROVEN`.

Entrypoint требует отдельный `--workload-profile SHADOW|LIVE` и
`--workload-binding-sha256`. Binding — SHA-256 UTF-8 JSON с sorted keys,
separators `(',', ':')` и полями `schema_version=vkm-visual-gpu-workload-binding/1`,
`profile`, `recipe_sha256`, `proxy_port`, `child_port=28083`.
SHADOW разрешает proxy 28084; LIVE — только 18083. Выбранный профиль и точные raw
recipe bytes проверяются до native exec и сохраняются в startup receipt.
Этот compose закреплён за SHADOW. LIVE требует отдельной reviewed command/recipe
и нового actual process proof; shadow receipt не квалифицирует live listener.
Host network сохраняется явно: он **не запрещает сетевой egress**. Native child
слушает loopback, а HTTP owner разрешает только свои ограниченные routes.

До остановки старого m0 сохранить защищённую exact fallback recipe для
существующего retained container: выбранные ID/state/image/mount/argv поля
без Env/Labels, SHA-256 weights/mmproj и restart по exact container ID.
Полный config/Env имеет статус `NOT_READ_NOT_COPIED`; этот receipt подтверждает
`SAME_HOST_FALLBACK_CAPTURE_NOT_INDEPENDENT_BACKUP`, не cold recreation или
независимое восстановление. Заблокированное чтение Env не повторяется.
Старые container/image/weights не удалять. Сначала новый image готов и проверен;
затем короткое окно stop старого m0 → start нового owner. При ошибке новый owner
остановить и запустить именно сохранённый старый container без изменения config.

Для реального GPU acceptance нужны CUDA load/offload и exact layer configuration,
тот же child PID/start lifetime, authenticated identity и inference через proxy,
restart/death invalidation и model-file-change отрицательный тест на отдельной
копии или synthetic control; оригинальные weights не менять. Silent CPU fallback
не допускается. CPU model execution не входит в разрешённый профиль.

Успешный isolated proof не квалифицирует старый gateway endpoint. Его будущая
маршрутизация должна пройти отдельную проверку и направляться на owner proxy.
Обновление CORE/DB не входит в этот recipe.

## Статусы

CPU tests verifier проверяют сохранность и admission входов; они не выполняют
build, inference или GPU qualification. Actual image ABI, CUDA dependency/load,
proof, inference, restart, invalidation и fallback имеют `NOT_RUN` до сохранения
реальных receipts. Text owner требует собственного полного snapshot и точного
runtime; отсутствие этого snapshot не заменяется dummy или visual qualification.
