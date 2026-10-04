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
| Recovery key | Новый ключ создан; владелец подтвердил отдельное хранение | OWNER_CONFIRMED_NOT_INDEPENDENTLY_INSPECTED; identity не передавалась на EDGE и не публиковалась |
| DOCX | Исправлены merge через пропущенную строку, неучтённые physical cells и опасные geometry bounds; native audit проходит producer → canon → accounting | Только synthetic документы; реальные источники, rendering, OCR и scientific review NOT_RUN |
| DOCX cache admission | Фактический byte format, source SHA и размер проверяются перед production reuse; DOCX под `.bin` не обходит child-stage gate через подменённый cached inspect | Поддержанные non-DOCX stage signature payloads сохранены; UNKNOWN/ZIP/IMAGE не получают successful native reuse |
| Operator CLI | Exit code зависит от action-specific terminal state; закрытый bootstrap не выдаёт обычный admission READY | Runtime drill и production switch NOT_RUN |
| GPU image | Первый offline native image собран из exact source и hashed inputs; ABI, зависимости и реальные cgroup limits 2 CPU/8 GiB/swap 0 проверены | Compile/ABI-only: model load, placement и inference NOT_RUN; старый m0 остаётся запущенным |

37 + 7 + 28 — **72 разных файла в трёх явно заданных наборах**. Старый drill
15 control files частично пересекается с ними и не прибавляется к этому числу.
Receipts: [scripts](../corpus_platform/receipts/ops_restore_2026-10-04.json),
[configs](../corpus_platform/receipts/encrypted_configs_restore_2026-10-04.json),
[review/provenance](../corpus_platform/receipts/review_provenance_restore_2026-10-04.json).
Ни одна из них не заменяет full `IndependentBootstrapBackup`.

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

Для прежнего exact commit `6b8a0b3` [hosted CI](https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/actions/runs/37208570223)
имеет три SUCCESS: World 626 PASS; Linux corpus 3432 PASS / 77 external NOT_RUN;
Windows 2346 PASS / 112 declared NOT_RUN / 50 external NOT_RUN. Этот run не
квалифицирует последующие изменения; их exact-commit CI проверяется отдельно.

## Следующие gates

1. **R2 originals:** metadata выделяет 272 файла, 2 122 899 923 bytes, включая
   три native DOC originals, а не только полученные DOCX. Нужны bounded encrypted
   batches, проверенный remote destination, independent read/decrypt/disk comparison
   и final source guards. Metadata inventory сам по себе не доказывает backup bytes.
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
   Пока NOT_RUN. Global CORE/DB production switch требует отдельного разрешения
   после квалификации и допускает согласованное короткое окно недоступности.
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
