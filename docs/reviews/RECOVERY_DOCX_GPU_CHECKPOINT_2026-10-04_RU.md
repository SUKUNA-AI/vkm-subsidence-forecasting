# Recovery, DOCX и GPU owner: проверенная точка продолжения

Дата: 04.10.2026. Продолжение [предыдущего отчёта](MODEL_OWNERS_TABLES_RECOVERY_2026-10-04_RU.md).
Изменения выполняются в рабочей ветке и draft PR #10. Этот отчёт не объявляет
полную runtime qualification, завершение корпуса или научный допуск.

## Завершено и проверено

| Область | Результат | Граница доказательства |
|---|---|---|
| Operational recovery | Все 37 ранее выявленных скриптов скопированы на EDGE и восстановлены отдельным чтением; SHA исходных, backup и восстановленных файлов совпали | Это точный набор скриптов, а не полный UNBACKED_UNIQUE inventory |
| Конфигурации | 7 файлов зашифрованы age; на EDGE передан только ciphertext; независимое чтение, authenticated decrypt и все disk hashes проверены | Не подтверждает полную конфигурацию старого Docker runtime |
| Review/provenance | 28 файлов PRIVATE, 1 672 195 bytes, получили encrypted copy и проверенный restore; оригиналы проверены повторно под удерживаемыми handles | Mutable files проверены как guarded Windows bytes; atomic snapshot и исключение прежних mmap writers не доказаны |
| R2 originals | 272 файла, 2 122 899 923 bytes, зашифрованы в девяти bounded batches; отдельное чтение с EDGE, authenticated decrypt и сравнение всех restored disk hashes прошли. Проверен также отдельный encrypted recovery control | 269 зарегистрированных версий и три native DOC companions; это immutable expected versions, а не atomic current generation или полный UNBACKED_UNIQUE |
| R1 evidence/exports | Дополнительно защищены и восстановлены 1 453 зафиксированные byte versions, 149 547 309 bytes, 23 batches и отдельный encrypted recovery control. Вместе с 28 точными ранее восстановленными версиями R0 покрыты все 1 481 записи выбранного набора | Физические файлы mutable; доказаны конкретные bytes под guards, не atomic generation, полнота workspace или научный допуск |
| Recovery key | Новый ключ создан; владелец подтвердил отдельное хранение | OWNER_CONFIRMED_NOT_INDEPENDENTLY_INSPECTED; identity не передавалась на EDGE и не публиковалась |
| DOCX | Исправлены merge через пропущенную строку, неучтённые physical cells и опасные geometry bounds; native audit проходит producer → canon → accounting | Только synthetic документы; реальные источники, rendering, OCR и scientific review NOT_RUN |
| DOCX cache admission | Фактический byte format, source SHA и размер проверяются перед production reuse; DOCX под `.bin` не обходит child-stage gate через подменённый cached inspect | Поддержанные non-DOCX stage signature payloads сохранены; UNKNOWN/ZIP/IMAGE не получают successful native reuse |
| Operator CLI | Exit code зависит от action-specific terminal state; закрытый bootstrap не выдаёт обычный admission READY | Runtime drill и production switch NOT_RUN |
| GPU image | Первый offline native image собран из exact source и hashed inputs; ABI, зависимости и реальные cgroup limits 2 CPU/8 GiB/swap 0 проверены | Compile/ABI-only: model load, placement и inference NOT_RUN; старый m0 остаётся запущенным |

37 + 7 + 28 — **72 разных файла в трёх явно заданных наборах**. Старый drill
15 control files частично пересекается с ними и не прибавляется к этому числу.
Receipts: [scripts](../corpus_platform/receipts/ops_restore_2026-10-04.json),
[configs](../corpus_platform/receipts/encrypted_configs_restore_2026-10-04.json),
[review/provenance](../corpus_platform/receipts/review_provenance_restore_2026-10-04.json),
[R2 originals](../corpus_platform/receipts/originals_restore_2026-10-04.json),
[R1 evidence versions](../corpus_platform/receipts/evidence_versions_restore_2026-10-04.json).
Ни одна из них не заменяет full `IndependentBootstrapBackup`.

R2 завершён за 154,118 секунды. Источники перепроверены под retained handles после
каждого restore; суммарно выполнено пять чтений исходных bytes. Отдельный encrypted
control содержит mapping и инструменты восстановления; его authenticated restore
также проверен. В PUBLIC опубликована только whitelist aggregate projection.
Независимый challenger проверил exact plan/union/helper bindings и 29 negative
publisher cases. Payload и recovery identity не читались моделью и не публиковались.

R1 завершён за 60,582 секунды. Для каждого batch прошли отдельное чтение с EDGE,
authenticated decrypt, проверка restored disk SHA и финальная проверка source bytes.
Пять проверочных чтений исходных файлов выполнены под удерживаемыми handles.
Отдельный recovery control объёмом 5 468 747 bytes также восстановлен и проверен.
Его содержимое, paths источников, индивидуальные private hashes и ключ не публикуются.
R0 вычтен только при полном совпадении size+SHA; эти 28 файлов повторно не прибавляются.

DOCX regression: **223 PASS Linux; 221 PASS Windows + 2 explicit NOT_RUN**
(symlink privilege, Linux RLIMIT_AS). Main agent независимо повторил эти selections.
Frozen recovery helper: **47 PASS Windows**, Linux synthetic regression также PASS.
Проверки используют малые fixtures, без scientific originals и GPU inference.
[CPU receipt](../corpus_platform/receipts/recovery_docx_cpu_2026-10-04.json) сохраняет
точные code/JUnit hashes и отдельные NOT_RUN/failed статусы.
Runbooks: [DOCX](../development/DOCX_NATIVE_GRID_RU.md),
[frozen fileset](../development/FROZEN_RECOVERY_FILESET_RU.md).

Новый [owner CPU receipt](../corpus_platform/receipts/native_owner_cpu_2026-10-04.json)
фиксирует independently repeated selections: owner/stream Linux 421 PASS / 2 Windows
NOT_RUN; Windows 378 PASS / 45 явных Linux/compiler NOT_RUN; optional observer
276 PASS на каждой ОС; status gate 259 PASS на каждой ОС; final factory/input/policy
selection 219 PASS на каждой ОС. Эти числа относятся к пересекающимся selections
и не складываются. Mandatory World на новой CPU policy: Linux 648 PASS.
Оба operator/bootstrap factory передают фактический `app.state.service`, проверенный
новыми positive/negative seam tests. Отдельно проверен отказ status gate: разрешён
только intentionally disabled text при exact native late fallback identity; отказ
visual или другой зависимости остаётся FAIL. Actual model load/inference не выполнен.

Обязательный World: Linux 626 PASS; Windows 625 PASS / 1 FAIL WinError 1314
из-за отсутствующего права создать symlink. Failed attempt сохранён, настройки ОС
не менялись, fake PASS/skip не добавлен. PUBLIC+PRIVATE verifier: 36 PASS,
7 SKIPPED_REF_UNAVAILABLE, exit 0; отсутствующие historical refs остаются явно учтёнными.

Exact commit `2cd790d` имеет три SUCCESS в [hosted CI](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/actions/runs/37219691073):
World 626 PASS; Linux corpus 3563 PASS / 77 external NOT_RUN;
Windows 2508 PASS / 112 declared NOT_RUN / 50 external NOT_RUN. Прежний
[run на 6b8a0b3](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/actions/runs/37208570223)
сохранён. Эти runs не квалифицируют последующие native placement, cached build
или stream transport изменения; их exact-commit CI проверяется отдельно.

## Следующие gates

Последующая проверка exact commit `bd81589` закрыла две CI-регрессии: расположение
`RerankNativeProfile` вне shared vocabulary и устаревшую опубликованную схему native
witness. Все три [hosted jobs](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/actions/runs/37228787130)
завершились SUCCESS: World 648 PASS; Linux corpus 3832 PASS / 2 NOT_RUN; Windows
2779 PASS / 134 NOT_RUN. [Отдельная квитанция](../corpus_platform/receipts/native_owner_hosted_ci_2026-10-04.json)
сохраняет границу exact commit: текущий незакоммиченный first-LIVE код и actual GPU
qualification этим run не проверены. Основной GPU WORKSTATION не использовался.

Новый [EDGE software image](../corpus_platform/receipts/edge_owner_image_2026-10-04.json)
собран именно из `bd81589`: все 472 code members wheel byte-exact по raw Git blobs.
Actual build exit 0, 47,663 s; образ закреплён по digest, ABI и cgroups проверены.
Это воспроизводимая производная проверенного native cache, не независимая cold
build. Models/inference NOT_RUN; прежний сервис сборка не меняла.

### Итог последнего часа EDGE — 05.10, около 00:10 МСК

Software/device и typed recipe preflight фактически пройдены. Последующий
[SHADOW attempt](../corpus_platform/receipts/edge_owner_shadow_attempt_2026-10-04.json)
**FAILED**: после CUDA preflight новый owner вышел с exit 2, причины по имеющейся
generic diagnostic не установлены. Загрузка могла быть начата; её завершение,
loaded identity, target placement и inference не подтверждены. Child-death и
restart qualification не выполнены. Новый image и stopped candidate сохранены.

Первичный fallback восстановил исходный retained m0; main независимо проверил
exact container/image, новый PID и native `/health=200` в 00:00:58 МСК.
Baseline работает. Повторная recovery завершилась ValueError и оставила
консервативный FAILED receipt; он сохранён без изменения. Это отдельный дефект
диагностики/восстановления, его root cause пока UNRESOLVED. Холодное восстановление
retained baseline данным запуском не квалифицировано.

Повторную GPU загрузку в этом сеансе не запускали. Следующий bounded drill требует
source-owned stage/exception-class diagnostics без raw credentials/payload,
локализации startup и recovery отказов, точного нового packet и fresh acceptance.
Контроллер также должен раньше замечать завершившийся candidate вместо ожидания
полного load timeout. Никаких новых model/solver/OCR/corpus campaigns не запускать
из этого checkpoint автоматически.

Отдельно challenger опроверг прежнюю v3 гарантию first-LIVE: переименование
legacy containers сохраняет Compose labels, а same-project convergence может
удалить originals вместе с writable layers. Опасный create удалён, production
запрещён до host effects. [Защитная CPU-проверка](../corpus_platform/receipts/first_live_protective_cpu_2026-10-05.json):
164 PASS Windows и 164 PASS Linux по exact LF source, независимо повторены main.
**FIRST_LIVE_NOT_READY**, требуется reviewed fixed Engine-create adapter и actual
original-ID preservation drill; CORE runtime/boot/switch/full49 NOT_RUN.

R3 capture не завершён: actual attempt остановился на metadata guard родительского
каталога Windows Python (WinError 5), до чтения scientific bytes; output capture
не создан. Сохранён failed attempt. Следующий сеанс: отдельно квалифицировать
существующий runtime guard, не ослабляя read fences, затем повторить exact frozen
capture. Остальные UNBACKED_UNIQUE и full restore closure остаются незавершёнными.

1. **R2 originals:** byte recovery закрыт только для зафиксированных 272 версий.
   Recovery receipt не предоставляет bootstrap или scientific admission.
2. **Остальная recovery closure:** R3 scientific work;
   семь конфликтов исторических hashes требуют разрешения, а не произвольного выбора.
   Полный inventory не завершён; Git/LFS metadata не доказывает удалённые payloads.
3. **Actual GPU placement:** source-owned contract и negative CPU tests готовы;
   actual image/device preflight PASS, model qualification ATTEMPTED_FAILED.
   Native owner по контракту читает реальные target weight
   buffers под тем же load/destroy mutex. Requested layer count и startup log
   не доказывают offload; CUDA_Host не является GPU allocation. Scope нового proof
   ограничен target weights; mmproj/context/kernels/performance остаются NOT_PROVEN.
4. **Narrow EDGE replacement:** после source freeze и actual device/placement
   gates повторить bounded SHADOW drill после диагностики отказа; затем загрузить новый GPU
   owner и проверить тот же inference process. Старые container/image/weights
   удерживаются для restart fallback. CPU-only model rehearsal не выполнять.
5. **Full isolated qualification:** explicit visual-only gateway profile сохраняет
   отключённый text owner и действующий API → late fallback. Proof требует actual
   ApiService и RETRIEVAL от того же observer; strict v1 по-прежнему требует оба owner.
   Нужны matching CORE API/MCP,
   actual 49 tools, restart, failed-switch, restore и coherent previous rollback.
   Пока NOT_RUN. Владелец разрешил CORE/EDGE switch после успешной квалификации;
   короткое окно недоступности согласовано. GPU основного ПК запрещено использовать.
   Первый LIVE baseline требует отдельного typed initialization boundary и
   durable закрытия legacy ingress; shadow startup authority не переносится в live.
6. **Научные данные:** полная DOCUMENT fidelity, реальные table/formula bindings,
   source-backed claims/entities/observation sets, конфликты и обе временные оси;
   independent stratified gold и сверка используемых данных по оригиналам; затем S26.
7. **Эксплуатация:** required GitHub checks, current MCP/config qualification,
   coverage/review/generation/backup server status. Внешний канал уведомлений отложен.

Проверяемый порядок: recovery bytes → pinned owner source/image → actual GPU owner
→ full isolated qualification → separately authorized production switch. Схемы,
adapters и независимая научная приёмка развиваются параллельно, сохраняя UNKNOWN.

Старый visual m0 на этой точке работает. Production selectors, CORE stores,
canonical corpus и scientific statuses не переключались. PUBLIC main не слит,
GitHub settings не менялись. `.mcp.json.example` владельца не входит в изменения.
