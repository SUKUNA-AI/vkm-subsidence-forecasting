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
[R2 originals](../corpus_platform/receipts/originals_restore_2026-10-04.json).
Ни одна из них не заменяет full `IndependentBootstrapBackup`.

R2 завершён за 154,118 секунды. Источники перепроверены под retained handles после
каждого restore; суммарно выполнено пять чтений исходных bytes. Отдельный encrypted
control содержит mapping и инструменты восстановления; его authenticated restore
также проверен. В PUBLIC опубликована только whitelist aggregate projection.
Независимый challenger проверил exact plan/union/helper bindings и 29 negative
publisher cases. Payload и recovery identity не читались моделью и не публиковались.

DOCX regression: **223 PASS Linux; 221 PASS Windows + 2 explicit NOT_RUN**
(symlink privilege, Linux RLIMIT_AS). Main agent независимо повторил эти selections.
Frozen recovery helper: **47 PASS Windows**, Linux synthetic regression также PASS.
Проверки используют малые fixtures, без scientific originals и GPU inference.
[CPU receipt](../corpus_platform/receipts/recovery_docx_cpu_2026-10-04.json) сохраняет
точные code/JUnit hashes и отдельные NOT_RUN/failed статусы.
Runbooks: [DOCX](../development/DOCX_NATIVE_GRID_RU.md),
[frozen fileset](../development/FROZEN_RECOVERY_FILESET_RU.md).

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

1. **R2 originals:** byte recovery закрыт только для зафиксированных 272 версий.
   Recovery receipt не предоставляет bootstrap или scientific admission.
2. **Остальная recovery closure:** R1 evidence/exports и R3 scientific work;
   семь конфликтов исторических hashes требуют разрешения, а не произвольного выбора.
   Полный inventory не завершён; Git/LFS metadata не доказывает удалённые payloads.
3. **Actual GPU placement:** native owner должен читать реальные target weight
   buffers под тем же load/destroy mutex. Requested layer count и startup log
   не доказывают offload; CUDA_Host не является GPU allocation. Scope нового proof
   ограничен target weights; mmproj/context/kernels/performance остаются NOT_PROVEN.
4. **Narrow EDGE replacement:** после source freeze и actual device/placement
   gates остановить разрешённый владельцем старый visual m0, загрузить новый GPU
   owner и проверить тот же inference process. Старые container/image/weights
   удерживаются для restart fallback. CPU-only model rehearsal не выполнять.
5. **Full isolated qualification:** pinned text owner, matching CORE API/MCP,
   actual 49 tools, restart, failed-switch, restore и coherent previous rollback.
   Пока NOT_RUN. Владелец разрешил CORE/EDGE switch после успешной квалификации;
   короткое окно недоступности согласовано. GPU основного ПК запрещено использовать.
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
