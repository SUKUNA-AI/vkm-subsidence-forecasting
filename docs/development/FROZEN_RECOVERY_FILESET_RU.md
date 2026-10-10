# Закреплённый recovery file set

`infra/recovery/frozen_inventory.py` дополняет существующий explicit inventory.
Он удерживает одновременно не более 64 source handles, читает два полных прохода
bytes, проверяет SHA-256, file identity и native Windows ChangeTime. Caller может
читать только эти закреплённые bytes и повторно проверить их после copy/restore.
Содержимое и machine locators не выводятся в receipt.

Это **guarded explicit file set**, не atomic filesystem snapshot. Windows handles
запрещают обычные новые write/delete opens и сохраняют ancestors; существующие
writable mmap и привилегированные writers этим не исключены. POSIX не заявляет
writer exclusion. Новые/исключённые файлы и полнота directory tree не входят
в denominator. Наличие hash не доказывает semantic uniqueness или backup.

По умолчанию mutable sources отклоняются. Явный режим
`GUARDED_WINDOWS_BYTES_ONLY` допускает только Windows snapshot **содержимого
заявленных файлов**, сохраняя original mutable declarations. Он не получает
права называться filesystem snapshot. Minimum budget резервирует два полных
прохода до чтения; каждый дополнительный copy/read/recheck расходует тот же
жёсткий budget. Missing paths, drift, links/reparse и time/byte limits fail closed.

Реальный backup executor обязан:

1. Закрепить точный manifest, code closure, размеры, hashes и failure domains.
2. Удержать source handle context до **после** independent restore и последнего
   full-byte/native-identity recheck. Не выдавать PASS до этого guard.
3. Создать новый отдельный destination, не overwrite существующие backups;
   использовать FD-relative `NOFOLLOW/O_EXCL`, owner/device/free-space guards.
4. Проверить скопированные bytes; manifest и `COMMITTED` публиковать последними
   после fsync. Incomplete attempt без marker не является backup.
5. Восстановить именно remote copy отдельным чтением в новый destination и
   повторно проверить каждый файл. Original paths не перезаписывать.

04.10 восстановлены все 37 ранее выявленных operational scripts и отдельно
7 mutable host configurations. Для configs использован portable
[age v1.3.2](https://github.com/FiloSottile/age/releases/tag/v1.3.2): archive и
executables закреплены SHA, authenticated ciphertext проверен восстановлением
и отрицательным tamper probe. На EDGE передан только ciphertext. Identity не
передавалась; владелец подтвердил отдельное хранение recovery key. Это human
attestation, не independent inspection офлайн-носителя. Sigsum verification не
выполнялась; источник executable — official HTTPS GitHub release asset digest.

[Scripts receipt](../corpus_platform/receipts/ops_restore_2026-10-04.json) и
[encrypted configs receipt](../corpus_platform/receipts/encrypted_configs_restore_2026-10-04.json)
не квалифицируют полный WSL/work inventory, canonical restore или typed bootstrap.
Current tool может быть строже execution-time версии; именно закреплённые bytes
обоих исполненных capture versions сохранены в ignored evidence package.

Затем выполнен отдельный encrypted drill 28 PRIVATE review/provenance records,
1 672 195 original bytes: source capture, independent EDGE read, authenticated
decrypt, все 28 disk hashes и final source recheck. Все source handles удержаны
до финального guard; private source contents/hashes/locators отсутствуют в
[public aggregate receipt](../corpus_platform/receipts/review_provenance_restore_2026-10-04.json).
Набор физически не пересекается с первоначальными 44 scripts/config candidates.
Это не snapshot всего evidence generation: другие файлы и новые записи не входят
в denominator, общая quiescence producer не доказана.

Recovery identity и decrypted restore outputs остаются в private ignored roots.
Не добавлять их в Git, не читать моделью и не размещать identity вместе с remote
encrypted backup. При потере identity ciphertext не даёт восстановимость.
