# Реальные metadata-наблюдения: unique recovery, 04.10.2026

Статус: **BOUNDED_OBSERVATIONS_COMPLETE / CONTROL_COPY_VERIFIED / CONTROL_RESTORE_VERIFIED**.
Наблюдения выполнены 04.10.2026, 12:34–12:43 UTC. Это ограниченная проверка существующих
файлов, topology и backup receipts, а не утверждение полноты резервного копирования.
Оригиналы и production не изменялись. После metadata-проверки создан новый закрытый
backup namespace EDGE и отдельный локальный restore-каталог для 15 контрольных
файлов. Отчёт и public-safe receipt добавлены в рабочую ветку PUBLIC.

## Подтверждённое покрытие CORE → EDGE

Проверены оба существующих control manifest, их агрегированный состав и свежий EDGE
receipt. SHA сжатых manifests повторно совпали в конце проверки; CURRENT CORE
сохранил `snap-20260929T175107Z-574daaac`. Corpus bytes повторно не хешировались.

| Проверка | Реальное наблюдение |
|---|---|
| CORE source manifest | 04.10, 02:00 MSK; 485 643 файлов; 38 907 626 565 bytes |
| EDGE snapshot | `2026-10-04`; окончание 03:20 MSK; `DONE / PASS` |
| Общий file-set digest | `204df86a851a7de0a7dcb4046489b87020199e103eb5c0a563d9ef75f849e0d7` |
| Сверка receipt | `equal=485643`, missing/corrupt/extra = 0 |
| EDGE full rehash | Receipt сообщает 485 643 файлов / 38 907 626 565 bytes; cache conflicts = 0 |
| CORE source hashing | 164 файлов прочитаны заданием; 485 479 hashes взяты из cache |
| Повторная проверка control bytes | Оба manifest SHA неизменны; metadata стабильна во время чтения |

CORE compressed manifest SHA:
`5a210a0783095427783b695d211393dc287020711cd9e02a1e0380d9f80cf1e1`.
EDGE compressed manifest SHA:
`407f0d4beaa47dd5f72eb99b38fba89f24fc5ac26963148ad205d6bdb763e39d`.
Различие compressed SHA ожидаемо: host-specific file metadata различаются;
file-set digest совпадает.

Фактический набор: 470 315 artifacts, 13 565 canonical files, 1 351 derived files,
один DuckDB, 408 receipts, два logs и root marker. Это подтверждает существующий
backup receipt для конкретного snapshot. Не выполнена новая независимая сверка
всех текущих source bytes и не выполнено восстановление этого snapshot.

**Runtime gap:** deployed set всё ещё не включает `accounting/`, а `receipts/*`
целиком помечены volatile. В текущем коде
[manifest builder](../../infra/edge/backup/vkm_manifest.py) accounting уже включён,
а blanket volatile для receipts снят. Это **CODE_FIXED_NOT_DEPLOYED**; metadata
проверка не выкладывала исправления. Отсутствие accounting в старом наборе не
доказывает потерю существующих accounting данных, но такой набор нельзя принять
для новой publication, которой требуется accounting closure.

## Workstation: реальные файлы вне этого backup set

CORE backup читает только свой canonical root. Windows work, PRIVATE, WSL work и
host configuration в его фактический manifest не входят. Найдены:

| Ограниченный root / группа | Файлов в наблюдении | Bytes в наблюдении | Полнота |
|---|---:|---:|---|
| Windows: выбранные roots, включая PRIVATE | 12 653 | 7 077 387 157 | Частично |
| PRIVATE versioned tree | 3 100 | 2 398 489 083 | Все учтённые файлы tracked; `.git` и исключённые деревья не проверены |
| Исторический intake work | 36 | 8 062 265 | Полный обход объявленного небольшого root |
| Geometry work | 1 787 | 1 974 350 152 | Достигнут entry budget |
| CAD work | 1 334 | 1 463 889 057 | Достигнут entry budget |
| WSL: выбранные roots | 14 816 | 46 904 529 352 | Частично |
| WSL work | 11 383 | 31 523 956 434 | Остались 4 048 queued directories |
| WSL NAV | 174 | 3 912 126 286 | Ограничена глубина |
| WSL accelerator work | 2 312 | 10 223 941 638 | Ограничена глубина |

Эти объёмы — **нижняя граница наблюдённых файлов, а не объём уникальных данных**.
Среди них могут быть воспроизводимые результаты, повторные копии, caches и
synthetic tests. По size/mtime невозможно доказать byte equality или отсутствие
иной копии. Физически прочитано 0 bytes содержимого инвентаризованных files.

PUBLIC и PRIVATE HEAD присутствуют в локальных cached remote branch refs.
Fresh remote Git/LFS verification не выполнялась. Поэтому Git-tracked original
не объявлен утраченным или единственной копией; независимость и полнота его
Git/LFS backup остаются **NOT_VERIFIED**.

## Явный UNBACKED_UNIQUE список

Existing [unique inventory tool](../../infra/recovery/unique_inventory.py) получил
manifest из **44 реально существующих regular files**:

- 37 неотслеживаемых operational scripts, 61 367 bytes. В том числе исторические
  launchers `c0_preconditions.sh`–`c8_restart_smoke.sh`, а также scripts подготовки
  и публикации результатов. Их пути отсутствуют в текущем Git index.
- 7 действующих host configuration files, 70 934 bytes. Содержимое не читалось;
  они требуют отдельной проверки secrets и защищённой схемы хранения.

Metadata-plan: `READY`, `bytes_read=0`, все 44 — `REGULAR`,
`recovery_classification=UNBACKED_UNIQUE`, `backup_status=NOT_VERIFIED`.
`READY` относится только к возможному следующему bounded verify, не к backup.
Для mutable configs verify дополнительно потребует quiescent snapshot.

Здесь UNBACKED_UNIQUE имеет строгое значение инструмента: authoritative/canonical
candidate **без доказанной независимой копии**. Это не доказательство, что нигде
нет другой версии файла. Семантическая уникальность каждого script и равенство
версированному replacement не проверялись. До этого проверки нельзя удалять
исторические файлы как «воспроизводимые».

Manifest SHA:
`33bcdbc054ae9d72b33571a2b570877e7bfe26cf57ee5376d748db3b7c96db2b`.
Metadata-plan SHA:
`2453c7a2c10edd7547876f67c6eb7cb430f33bf0929489d387ace2a6c984043f`.
Полные locators, metadata и reasons сохранены только в ignored local evidence.

## Реальные failure domains и бюджет

| Логический domain | Наблюдение | Следствие |
|---|---|---|
| WORKSTATION data | Отдельный физический NVMe; свободно около 532,1 GB | Это исходный диск PUBLIC/PRIVATE/work |
| WORKSTATION system + second partition | Другой физический NVMe; system и второй раздел — один диск | Между этими двумя разделами нет защиты от отказа диска |
| WSL Arch | VHD расположен на system domain; Linux видит отдельную virtual ext4 | Linux device ID сам по себе не означает отдельный физический диск |
| CORE | Отдельный host, SATA/LVM/ext4; свободно около 269,0 GB | Отдельный от WORKSTATION/EDGE host |
| EDGE | Отдельный host, NVMe/LVM/ext4; свободно около 341,1 GB | Подходит как кандидат независимого от workstation диска/host |

Разделение host/disk не доказывает отдельные электропитание, помещение, credentials
или защиту от ransomware. Эти failure-domain assertions потребуют решения владельца.

Actual EDGE nightly budget — **60 GB**, store — **39,035 GB**, reserve — **100 GB**.
Template 180 GB не является действующим бюджетом. Нельзя незаметно добавить десятки
GB локальных outputs в существующий nightly retention namespace. Для unique backup
нужны отдельные namespace, quota и retention, не затрагивающие canonical snapshots.

## Выполненный маленький independent copy / restore

Выполнен frozen intent для **15 файлов / 34 470 bytes**:
девять исторических c0–c8 launchers, conversion script, две operational/review receipts,
один public configuration example и два metadata receipts этой проверки.
Actual credential/config secrets исключены. Перед transfer локально проверены
UTF-8 и bounded regex secret scan: `BOUNDED_NO_MATCH`. Это не доказательство
отсутствия произвольных секретов. Содержимое файлов в PUBLIC не перенесено.
Полный canonical review journal в объявленном
ограниченном наборе не идентифицирован; две receipts не подменяют его backup.

Target — новый отдельный recovery namespace на наблюдённом EDGE filesystem;
restore — новый ignored workstation directory. Перед записью проверены ownership,
device, отсутствие symlinks и свободный reserve EDGE. Созданы только новый
recovery namespace и новые файлы; существующие файлы не перезаписывались.

Фиксированные пределы: 15 файлов, source ≤ 1 MiB, суммарное чтение ≤ 8 MiB,
transfer ≤ 2 MiB, target/restore ≤ 1 MiB каждый, wall time ≤ 300 s,
EDGE free после операции ≥ 100 GB. Фактическая последовательность:

1. Fresh SHA каждого exact source и повторный stat; pin окончательного inventory.
2. Локальный bounded secret scan без вывода значений; при positive/unknown —
   отдельное решение о шифровании, не отправка открытых host secrets.
3. Повторная проверка destination parent/device, прав и отсутствия target;
   exclusive создание нового namespace в рамках разрешённой backup/restore задачи.
4. Copy через directory FD и `O_EXCL`; полные target hashes и source recheck.
   Manifest и `COMMITTED` записаны последними, выполнен fsync; незавершённый
   namespace без marker не принимается. Existing overwrite и cleanup отсутствуют.
5. Restore именно с EDGE copy в отдельное новое место; свежие original/backup/restore
   hashes и проверка, что original не изменился.
6. Раздельные copy/restore receipts с фактическими file identities/failure domains.

**Copy и restore VERIFIED:** 15/15 source/EDGE/restored SHA-256 совпали; оригиналы
после copy и restore сохранили hashes и native Windows change stamps. Восстановление
прочитало именно EDGE copy отдельным SSH вызовом, затем проверило локальные bytes.
Время исполнителя — 0,609 s, без времени интерактивного approval/preflight.

Frozen inventory SHA:
`4843cf1ae812cfae256cd08d4f803edea4f3bcd75cd4be541b2a6d1dc6b79d14`.
EDGE backup manifest SHA:
`cc204f4ac8dcdd146f0a720c55b5dd935fbbd16098a44d9a4266dc08ff678b0f`.
Public-safe [receipt](../corpus_platform/receipts/control_restore_2026-10-04.json)
содержит IDs/hashes и ограничения, без содержимого и machine locators.

Из 44 metadata candidates этим drill покрыты 10 operational scripts; 34 остальных
candidate не получили новую verified copy. Два review/control receipt, example и
две metadata-записи входят дополнительно. Набор не покрывает целиком WSL/work/PRIVATE
и не даёт права утверждать восстановление 38 GB canonical snapshot.

## Можно ли existing backup receipt использовать для typed bootstrap?

Напрямую — **нет**. Он подтверждает старый canonical snapshot и может быть входным
evidence для exact legacy tuple. Контракт `IndependentBootstrapBackup` в
[bootstrap.py](../../src/vkm_corpus/update/bootstrap.py) дополнительно требует:

- конкретные `candidate_sha256`, `legacy_topology_sha256`, frozen `inventory_sha256`;
- byte-complete inventory actual candidate stores и необходимых control files,
  policy/access/authority/runtime/recipe/selectors и всех канонических review records;
- совпадающие counts/bytes и отдельные copy/restore proofs;
- `source_untouched=true` в реальном restore, независимый failure domain;
- scope `BOOTSTRAP_SHADOW_PRODUCTION` и свежую проверку BoundFile chain.

Существующий nightly receipt не содержит этих pins и не является restore proof.
Маленький контрольный drill тоже не удовлетворяет этому full inventory contract.
Нельзя пересоздать typed PASS простым переименованием старого JSON или изменением
scope. Code-only update сам по себе не требует пересчёта embeddings: следует
сохранить точные существующие bytes и добавить недостающее control closure.

## Evidence и границы проверки

Ignored evidence package: `work/production_data/recovery_inventory_20261004/`.
Основные записи: `windows_metadata.json`, `wsl_metadata.json`,
`windows_topology.json`, `git_heads.json`, `core_metadata.json`, `edge_metadata.json`,
`core_stability.json`, `edge_stability.json`, `explicit_unique_manifest.json`,
`explicit_unique_plan.json`, `selection_evidence.json`,
`independent_control_copy_intent.json`. Paths и host metadata не перенесены в PUBLIC.

Windows обход ограничен 13 958 observed entries, WSL — 21 005; установлен hard
budget 36 000 entries/host и 75 s. Глубина и исключённые directories записаны
поштучно; caches, venv, synthetic test trees и `.git` не обходились полностью.
Remote control manifests ограничены 32 MiB compressed, 128 MiB decompressed metadata,
500 000 records и 60 s/host; оба manifest прочитаны полностью в этих пределах.
После проверки повторно прочитаны только их compressed bytes, не corpus objects.

Дополнительные ignored evidence: `frozen_control_copy.json`, `copy_receipt.json`,
`restore_receipt.json`, executor и отдельный restore-каталог. Их нельзя публиковать
как public-safe содержимое; у receipt выше сохранены только разрешённые поля.

Не выполнялись: cleanup, полный corpus hashing/restore, runtime deployment,
model/OCR/GPU workloads и изменение существующей nightly конфигурации. Выполнено
только bounded чтение и независимый copy/restore 15 control files. Full unique
coverage и typed bootstrap backup остаются незавершёнными gates.
