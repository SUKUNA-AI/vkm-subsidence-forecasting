# Локальный PostGIS и подготовка EDGE

Статус 14.11.2026: локальная техническая проверка выполнена; `EDGE_APPLIED=false`.
Документ не разрешает менять production. Следующий этап требует отдельного
разрешения владельца на backup, репетицию с закрытыми данными и переключение.

## Проверенный локальный рецепт

Скрипт `scripts/env_setup/postgis_local.py` использует уже запущенный Docker.
Закреплён официальный образ проекта Docker-PostGIS:

```text
postgis/postgis@sha256:c5d1ba4292ff7b4471ff41cd515c677633797b72cdd2a7bb691cadaab91f1a1f
```

Фактически: Linux amd64, PostgreSQL 18.6, PostGIS 3.7.0. Это образ проекта
PostGIS поверх официального PostgreSQL, не Docker Official Image PostGIS.
Docker-PostGIS распространяется под MIT, PostGIS — GPL-2.0-or-later;
базовые компоненты сохраняют собственные лицензии. В private proof сохранены
package copyright, версии и контрольные суммы компонентов.

Первый совместимый образ 18/3.6 также прошёл synthetic smoke, но заменён
поддерживаемым 18/3.7 после проверки актуального README upstream.
Остановленные контейнеры первой попытки и их отдельные тома сохранены;
они не являются текущим окружением.

```powershell
python scripts/env_setup/postgis_local.py setup --output work/postgis/setup-01
python scripts/env_setup/postgis_local.py verify --output work/postgis/verify-01
python scripts/env_setup/postgis_local.py roundtrip --fixture work/duckdb_to_postgis.json --output work/postgis/roundtrip-01
python scripts/env_setup/postgis_local.py stop --output work/postgis/stop-01
```

Каждой попытке назначается новый output. Секрет случайно создаётся в памяти и
передаётся через окружение процесса, без значения в аргументах/квитанциях.
SQL выполняется через `docker exec`, не требует сетевого DSN. Этот рецепт
не создаёт постоянный пароль для прикладного сетевого клиента.

Контейнер `vkm-env-20261114-postgis37` публикует только
`127.0.0.1:55438→5432`; `vkm-env-20261114-postgis37-restore` порт не публикует.
У каждого собственный одноимённый том с суффиксом `-data`, смонтированный
в `/var/lib/postgresql`. Лимиты каждого: 2 CPU, 1 GiB RAM, 256 процессов.
Скрипт проверяет image, label, точное имя тома, mount и port bindings перед
переиспользованием/остановкой. Native PostgreSQL Windows не используется.
Production mount и EDGE DSN в коде отсутствуют. `stop` оставляет тома и данные.
Удаление контейнеров/томов намеренно не автоматизировано: перед ручным удалением
проверить label и сохранить нужные receipts/dump; не использовать массовый prune.

База `vkm_fixture`, таблица `synthetic.geometry`: stable ID, WKB/geometry,
frame, units, отдельная JSON metadata. Point, ломаная из двух сегментов,
полигон с одним отверстием, NULL и отдельная точка local-m проверены с SRID 0.
Проверки: координаты, ST_Equals после WKB, площадь 96 px², исключение точки
из отверстия, NULL, owner, эффективные SELECT/запрещённый INSERT.
Операции площади/содержания выполняются внутри одной `SYNTHETIC_PIXEL/px` рамки.
Pixel/local координаты не объявляются географическими; `ST_SetSRID` не служит
преобразованием координат. Для WKB единицы/SRID сохраняются отдельно.

Custom `pg_dump` синтетической схемы восстановлен `pg_restore` во втором
изолированном экземпляре, роли восстановлены по определениям `pg_dumpall
--roles-only --no-role-passwords`. Сопоставлены строки, WKB, metadata, owners,
ACL и роли; SHA-256 канонического логического представления совпал.
Это проверка механизма восстановления синтетики, не production backup rehearsal.

Вход E19 — JSON `features` с `id, frame, unit, srid:0, wkb_hex`, прочитанный
после GPKG и DuckDB; выход `receipt.json/features` возвращает те же значения
и координаты/отверстия для DXF readback. Допуск координат общей цепи задаёт
владелец fixture; PostGIS дополнительно требует геометрического равенства.
Нарушения критериев дают ненулевой exit code. Синтетика не входит в evidence.

## Что установлено на EDGE только чтением

Проверено существующим SSH, SQL внутри `BEGIN READ ONLY` и выборочным
`docker inspect` без `Config.Env`. Сервер PostgreSQL 18.6, image tag
`postgres:18.6`, полный image ID и repository digest сохранены в private proof.
PGDATA: `/var/lib/postgresql/18/docker`; текущий named volume смонтирован
в `/var/lib/postgresql`. Compose найден по действующим labels; его SHA-256
сохранён без вывода `.env`. Контейнер не перезапускался в рамках работ.

Для целевой `vkm_ops` проверены extensions, все пользовательские схемы,
relations, владельцы, ACL, роли/членства. На момент снимка 87 обычных/partitioned
таблиц, установлен только `plpgsql`; PostGIS отсутствует. Не читались scientific
строки. Наблюдалась только текущая диагностическая psql-сессия;
это мгновенный снимок, не доказательство отсутствия фоновых читателей.
Списки и host-local пути находятся в private evidence, не в PUBLIC.

## Порядок будущего применения — не исполнен

1. Повторно сверить текущий image/digest, PGDATA/mount, version, collation,
   extensions, roles/ACL, tablespaces, RLS, publications/subscriptions, объёмы
   и свободное место. Зафиксировать writers/readers и их версии/DSN без секретов.
   Выделить окно и согласованный предел простоя после измеренной репетиции;
   обещание «5–10 секунд» для полного dump/restore недопустимо.
2. Защитить полный backup: `pg_dump -Fc --dbname=vkm_ops` и аналогично для
   каждой другой пользовательской БД; `pg_dumpall --globals-only` с достаточными
   правами. Host/user/port и защищённый `PGPASSFILE` задаёт существующий private
   launcher из принятого DSN secret file; полный DSN/пароль не передавать в argv
   и не печатать. Globals с password hashes, `.env`, TLS keys и конфиги хранить
   зашифрованно в PRIVATE с ограниченными правами. Не публиковать их.
   Сохранить Compose/config/pg_hba/pg_ident/auto.conf, permissions, версии,
   checksum, команды, exit code и stderr. Снимок globals и каждой БД не
   является единым межбазовым snapshot; при межбазовых связях quiesce нужен
   до всех финальных выгрузок. Не копировать живой PGDATA обычным `cp`.
3. Восстановить backup в новый изолированный PG18/PostGIS 3.7 instance с
   отдельным новым томом, loopback и без production mounts. Не подключать
   оригинальный том вторым сервером. Сначала проверить SQL globals и создать
   нужные роли/параметры; bootstrap-конфликт разрешить явно. Затем восстановить
   полные dumps с owners и ACL (`pg_restore --exit-on-error`), создать PostGIS
   в целевой БД под администратором. При восстановлении extension проверять
   namespace/owner/version и совместимость всех фактических расширений.
4. Сравнить состав объектов/схем, exact counts, sequence last_value/is_called,
   ownership, ACL/default ACL, role membership, constraints/indexes/RLS,
   extensions и large objects. Сделать детерминированные дайджесты каждого
   набора строк с явным order/serialization и единым snapshot; для больших
   таблиц — потоковый SHA-256, не только count/sum. Сверить `pg_restore -l`,
   stderr и отсутствие неразрешённых предупреждений. Не использовать только
   размеры или байтовый hash dump как доказательство равенства содержимого.
   Прогнать принятые read-only запросы каждого текущего corpus/scientific
   reader на восстановленной БД, без подключения их writers к production.
5. Измерить длительность backup/restore/проверок и объём; получить отдельное
   разрешение на конкретный cutover. Остановить/дренировать writers, запретить
   новые записи согласованным способом; дождаться завершения транзакций.
   Зафиксировать точку T0, финальный согласованный backup и digests.
   Проверить `pg_locks`: `pg_dump` держит ACCESS SHARE и может ждать/блокировать
   конфликтующий DDL; запрещён параллельный DDL. `CREATE EXTENSION` выполняет
   DDL/catalog locks в целевой БД, требует admin; выполнять только в окне
   после репетиции. Остановка сервера обрывает соединения и требует их reconnect.
6. Восстановить финальную копию в новый том, проверить, переключить readers
   и writers, затем выполнить read-only smoke и одну согласованную проверку
   записи. Старый экземпляр сохранить выключенным/недоступным для записи.
   Не менять image старого контейнера с действующим томом: upstream отмечает
   смену OS/glibc и рекомендует полный dump/restore для предотвращения проблем
   с текстовыми индексами. Формат volume PG18 также сохраняется явно.

## Возврат и новые записи

До разрешения записей на новом сервере rollback — вернуть старый проверенный
endpoint/Compose/image/digest и старый том, затем read-only smoke, после чего
возобновить writers. При любых несовпадениях до cutover старый сервер остаётся
единственным источником записи; новый изолирован.

После новых committed записей нельзя просто включить старую копию: это потеряет
изменения после T0. Снова остановить writers, сохранить новый сервер и финальный
dump, сравнить транзакции/идентификаторы и sequences. Предпочтителен repair-forward.
Возврат с сохранением новых данных требует отдельной проверенной репетиции
обратного full logical dump/restore в совместимый PG18 с нужными расширениями
либо заранее подготовленной и проверенной CDC/репликации. Старая БД без PostGIS
не примет новые spatial objects. Нельзя автоматически replay WAL другой ветки
поверх старого логического dump; непрерывность WAL/PITR отдельно не квалифицирована.
RPO=0 после T0 не заявлен, пока сохранение новых записей не доказано.

## Доказательства и границы

Private receipts текущей поставки находятся в `work/local_setup_20261114/edge_postgis/`:
`edge_inventory_receipt.json`, `e13_attempt2_37/receipt.json`,
`e19_postgis_readback.json`, `extension_create_proof.txt`, `sources.json`.
Финальный handoff связывает актуальный script SHA с повторной проверкой.
Дампы/host paths не коммитятся. Production backup, restore, reader qualification,
lock/downtime measurement, cutover и rollback с новыми записями — NOT_RUN.

Источники: [Docker-PostGIS, версии и volume layout](https://github.com/postgis/docker-postgis),
[PostgreSQL 18 pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html),
[SQL backup/globals](https://www.postgresql.org/docs/18/backup-dump.html),
[WAL/PITR](https://www.postgresql.org/docs/18/continuous-archiving.html).
