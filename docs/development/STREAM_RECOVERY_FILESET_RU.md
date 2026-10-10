# Потоковый transport для закреплённых оригиналов

`infra/recovery/stream_fileset.py` реализует потоковые stages для отдельного,
заранее утверждённого backup executor. Импорт не читает оригиналы или ключи,
не запускает age, SSH, модель и background job. Новая библиотека не заменяет
согласование точного manifests, маршрута и compute budget.

## Граница результата

Проверенная metadata R2: 272 оригинала и native companions, 2 122 899 923 байта;
наибольший файл — 96 179 474 байта. Для каждого есть ровно один объявленный
SHA-256. Это сведения предыдущего metadata inventory, а не новая сверка bytes.

Backup производится batches максимум 64 файла и 256 MiB. Версии каждого файла
закреплены заранее ожидаемым SHA; batches поэтому могут закрываться независимо.
Это не атомарный snapshot всего live filesystem, current evidence или scientific
generation. Повторное чтение изменённого оригинала должно остановиться, а не
переписать ожидаемый SHA. Полный R2 closure появляется только после проверки всех
batches и точного полного manifest. Этот transport не квалифицирует bootstrap
backup или научный допуск сам по себе.

## Контракт и bounded memory

`plan_batches(manifest, sizes, manifest_sha256=...)` работает только с metadata.
Все source items должны быть immutable, с expected SHA, без copy locators.
Нет обхода directory tree, intake, archive extraction или выбора команд из
payload. Header содержит только точные logical IDs, размеры, SHA и parent manifest
hash. Порядок закреплён в header; case aliases и повторные IDs запрещены.

Поток: `VKMSTRM1`, uint32 длины header, canonical JSON до 128 KiB, последовательные
raw file bytes, `VKMEND01`, EOF. Source и transport blocks — до 64 KiB. Нет
base64 payload, whole-file read или `communicate()` с большим output. Известный
frozen `PinnedFileSet` сохраняет свои две первоначальные проверки с bounded 1 MiB
chunks. Новый wrapper использует удерживаемые handles и проверяет native identity,
path identity, expected SHA и EOF до/после каждого объекта. Он не вызывает
`read_pinned`, возвращающий весь файл.

Узкий wrapper сейчас зависит от private handle seam frozen helper. До переноса
или обновления helper требуется targeted qualification этого seam и exact hashes
обоих inventory modules в executable intent. Будущий public метод helper должен
быть эквивалентен `_source_chunks`, без ослабления checks.

## Исполняемый порядок для утверждённого executor

1. Закрепить exact raw SHA полного R2 metadata manifest, environmental roots,
   expected sizes/SHA, code/helper/tool hashes, exact Python version, публичный
   age recipient, independent EDGE failure domain, disk reserve и timeout.
   Не включать identity bytes, исходные paths или содержимое в PUBLIC receipt.
2. Подготовить отдельный metadata-only plan. При разбиении перенести только нужные
   source items и roots в batch manifest. Header сохраняет hash полного manifest.
3. Открыть `PinnedFileSet` с retained handles до завершения restore и финального
   source guard. Для стандартной последовательности зарезервировать минимум
   `5 × Σ(size + 1)` source-read bytes: два начальных hash passes, stream, recheck
   после encryption, финальный recheck. Время helper — максимум 300 секунд.
4. `encrypt_pinned`: pinned age executable, stdin stream, exclusive ciphertext
   output, watchdog deadline/output/disk reserve, exit 0, fsync, fresh ciphertext
   hash, source recheck. Cipher budget — 256 MiB + 2 MiB. Identity не используется.
5. Создать immutable intent: batch header/capture hash, cipher size/hash, recipient,
   hashes transport/helpers/remote program/tools, exact remote Python, target и
   бюджеты. Все context-dependent paths остаются в защищённом локальном intent.
6. Сформировать `remote_program('copy', ...)` и передать validated request в
   `transfer_cipher`. Transport заново формирует fixed program, не принимает
   произвольный script. SSH ограничен host alias `edge`, BatchMode и keepalive;
   credentials остаются существующему SSH. На EDGE передаётся только ciphertext.
   В remote manifest сохраняется typed `stream_header`: IDs, sizes и hashes,
   без original paths. Это позволяет восстановить approved header после потери
   workstation; exact cipher/header/manifest pins остаются обязательными.
7. Remote writer открывает все directories FD-relative с `O_NOFOLLOW`; namespace
   и файлы создаются исключительно. Вход ограничен exact cipher size/hash и EOF;
   fsync cipher → manifest → directory → `COMMITTED` → directory/parent.
   Неполная попытка остаётся без `COMMITTED` и не перезаписывается.
   Before IO проверяются exact UID/GID/device/inode root и parent, private root
   mode 0700 и безопасный parent 0755. Новая namespace получает 0700, committed
   namespace — 0500; cipher, manifest и marker — 0400. Restore проверяет эти
   owner/device/mode identities снова. Pathname alone не допускает root.
8. Отдельный SSH restore читает manifest и `COMMITTED`, проверяет regular inode,
   nlink, size, полный cipher SHA и identity до/после stream. Локальный output
   exclusive; transport сверяет его с expected cipher SHA и exit status.
9. `decrypt_restore`: только локальный age process читает явно указанный identity
   file. Данные восстанавливаются в новую private directory как
   `item-<logical_id>.bytes`, с exact approved header и SHA каждого объекта.
   Даже полностью разобранный plaintext не принят до успешного age exit:
   authentication failure оставляет staging без ready/commit статуса.
10. Повторно hash restored disk files. Выполнить `pinned.recheck()` и только затем
    записать exclusive durable итоговый receipt. Retained source handles закрыть
    после этого. Global closure — exact union всех успешных batches без пропусков
    и дублей; хранение originals и key escrow проверяются отдельно.
11. Отдельная immutable encrypted control capsule содержит original-path mapping,
    intent/headers, original batch captures/restore receipts и exact executor/helper
    sources. Она имеет собственный pinned batch и separate authenticated restore.
    Original counts/bytes и control counts/bytes записываются отдельно. Capsule
    не содержит свой hash/receipt, identity, credentials или model binaries.
    Полный closure создаётся после control restore. Наличие такого closure не
    означает snapshot изменяемого current generation или scientific admission.

## Восстановление после потери workstation

1. На новом доверенном хосте получить off-host owner key и exact software versions;
   identity не публиковать и не передавать на EDGE.
2. Найти сохранённую control namespace по public-safe closure pin. Открыть её
   manifest и `COMMITTED` через FD-relative helper и проверить exact manifest/cipher
   digest по независимому closure pin. Header находится в bound remote manifest.
   Если independent pin недоступен, отметка provenance остаётся NOT_VERIFIED:
   age encryption сама не аутентифицирует автора backup.
3. Отдельным SSH восстановить control ciphertext, затем выполнить local age -d
   с protocol/header/hash checks и обязательным age exit 0. Control восстанавливается
   только как logical-ID file. Parse JSON запрещает duplicate fields; порядок
   восстановления не использует paths из payload для archive extraction.
4. Из authenticated control получить exact original headers, remote namespaces,
   cipher/manifest pins и original-path mapping. Путь назначения и доступ утверждает
   оператор; проверить portable relative locators и containment на новом root.
5. Каждый original batch восстановить отдельным SSH и age process в новые
   exclusive logical-ID files; сверить все SHA/sizes, полный union IDs и counts.
   Только после этого отдельно разложить bytes по validated original paths без
   overwrite. Не запускать intake или научный импорт в рамках восстановления.
6. Сохранить новое restore receipt. Исполнение этого disaster drill пока NOT_RUN;
   runbook описывает процедуру, не подменяет её доказательство.

## Ограничения и приёмка

Deadline и budgets проверяются в каждом loop; watchdog завершает blocked external
process. Remote SIGALRM default прекращает процесс независимо от blocked read/write;
он задаётся до IO и не полагается на смерть SSH-клиента. Remote Python получает
exact 256 MiB RLIMIT_AS. Child outputs идут на диск или через bounded reads,
stderr payloads подавлены. Размеры, disk reserve и tool identity проверяются отдельно.

Remote Python дополнительно имеет exact bound ELF path/SHA/size/device/inode/owner/
mode. `/proc/self/exe` должен совпадать с realpath фиксированного `/usr/bin/python3`;
retained actual executable FD хешируется bounded chunks до IO и после copy/restore,
а pathname и native identity перепроверяются. Это доказательство происхождения
исполняемого ELF, не full loaded-memory/stdlib/dependency identity.

Для Windows executor `memory_limit_bytes` задаёт native Job Object cap каждому
age/SSH process и его descendants. Child создаётся suspended, limits считываются
обратно, assignment должен пройти до resume. BREAKAWAY и fallback отсутствуют;
KILL_ON_JOB_CLOSE ограничивает orphan descendants. Существующие ctypes ABI из
`vkm_jobs.procs` должны иметь exact source pin. Без явного cap (`None`) библиотека
не заявляет OS-level memory bound; это только совместимость protocol tests.
Unsupported platform для explicit Windows cap fail-closed. Целевой R2 cap —
512 MiB на внешний process tree, remote Python — 256 MiB. Это compute contract,
не измерение фактической памяти age/SSH на originals.

`private_directory` создаёт exclusive staging с protected user/SYSTEM DACL на
Windows и проверяет его native readback; дочерние output наследуют доступ.
POSIX получает exact 0700. Это не защита от того же пользователя или администратора.

Synthetic tests проверяют actual retained handles, exact stream round trip,
пустой файл, short writes, corruption/truncation/extra bytes, duplicate IDs/JSON,
traversal, deadlines/budgets, отсутствие overwrite и отказ при age exit failure.
POSIX tests исполняют fixed remote helper только на синтетических локальных files,
без SSH: проверяют durability marker, symlinks, owner/device/inode/permissions и
actual blocked read/write termination. Windows tests проверяют actual bounded
allocation и assignment refusal before execute. Другие OS-specific cases
помечаются `NOT_RUN`, а не PASS.

Actual age encryption/decryption, identity/key escrow, EDGE transfer, restore,
R2 original byte reads, полный recovery closure и bootstrap qualification —
`NOT_RUN`, пока их отдельный executor не выполнен и receipts не проверены.
