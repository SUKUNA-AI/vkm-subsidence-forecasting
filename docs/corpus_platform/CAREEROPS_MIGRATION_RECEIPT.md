# Вывод CareerOps из работы — квитанция

Дата: 28.09.2026 (MSK). Исполнитель: координатор VKM Corpus Platform v0. Основание: постановка §8 и §54, решение
пользователя D1 «Да, выводить» (28.09, CareerOps оказался живой автоматизацией, а не остатками), план агента B
([AGENT_B_CLEANUP_PLAN.md](../implementation_work/AGENT_B_CLEANUP_PLAN.md)) с решениями CP-20
([журнал](../implementation_work/COORDINATOR_DECISIONS.md)). Хосты — роли (CORE, EDGE, WORKSTATION); адреса, логины,
домен и секреты здесь не приводятся.

## 1. Порядок и время

| Шаг | EDGE | CORE | WORKSTATION |
|---|---|---|---|
| INVENTORY | 00:45–01:07 (агент B) | 00:45–01:07 (агент B) | — |
| EXPORT (пауза таймеров 01:23) | 01:23–01:30 | 01:26 | — |
| VERIFY (V1–V7, G1) | — | — | 01:31–01:33 |
| STOP | 01:33 (SeaweedFS остановлен, холодный снимок; G1b PASS) | 01:36 (VM выключена) | — |
| DISABLE | 01:33 (таймеры, restart=no) | 01:36 (autostart VM выключен) | — |
| REMOVE | 03:44 | 03:45 | 03:47 (алиас и ключ VM) |

Выдержка между DISABLE и REMOVE — около 2 ч 10 мин (решение CP-20 сократить выдержку с 24 ч; проверки выживания
после STOP и DISABLE — PASS).

## 2. Архив

Логический путь: `<ARCHIVE_DRIVE>:\VKM_ARCHIVE\careerops_2026-09-28\` на разделе данных WORKSTATION (не git, не
облако), 317 MB, подкаталоги `edge/`, `core/`, `vm/`, `workstation/`, в каждом — `SHA256SUMS`. Секреты (cookies и
токены профилей внешнего сервиса, env-файлы с ключами, `.env` PostgreSQL, deploy-ключ GitHub, cloud-init `user-data`)
не архивировались: их имена перечислены в `SECRETS_NOT_ARCHIVED*.txt`.

| Файл | Байт | SHA-256 |
|---|---:|---|
| `edge/careerops.dump` (pg_dump custom, `-Z 6`) | 5002193 | `ae44e20683c5eedbef9693ccd2ecd490184824e2f9f83732fab718d7a3764b3a` |
| `edge/careerops_schema.sql` | 32739 | `fce40b5799c28b7bbccbe0c9ff199a5d0a0b5540f1fa3ed7d6a2f7c5622d553d` |
| `edge/careerops_data.sql` | 38461616 | `234b08337b1699f67489a3f8a65ba4662e6ad6eca34e1e2d8d5422cb48e5d9e0` |
| `edge/globals_no_passwords.sql` | 674 | `f5529bd8d69dfe5f30056797c361e8d6607caa03648e6bd64b78ee364c1c868c` |
| `edge/careerops-raw.objects.tar.zst` (объекты S3 + манифест) | 112644989 | `bcfd1de25e9590283db1f8b66a655942aa18a55b8a2cb546ec6c7942bd5bb125` |
| `edge/seaweedfs_cold.tar.zst` (холодный снимок каталога SeaweedFS) | 145139146 | `4c7d2d861ce17491d3fd1348208ed88636806c68c92bc8d2510e2e9196a2931b` |
| `edge/careerops-app.bundle` (git, все refs, включая stash) | 629767 | `276260f20c03425425bc5c67ac50f4b70d26e6bf7367016859206f01354de318` |
| `edge/careerops-reranker-src.bundle` (исходники KEEP-реранкера) | 726658 | `de40875c975e16dbaf2f4480fc004021dea80ea7c20bca5d9ef4eb91097e76a9` |
| `edge/careerops-app-runtime.tar.zst` | 1983639 | `195a9c013f8753c4865a71bb01fffe23ca7e2cc4586377a0e88893bd5fbb4520` |
| `edge/careerops-workspace.tar.zst` | 35874 | `2ccf2983d883877755f91888e248e89156792df75cbbdd44054955d540372a6d` |
| `edge/careerops-home-exports.tar.zst` | 27081219 | `3dc867e7214076debe42b948bef53184e3e7554ab38202b968519cd670798c32` |
| `edge/careerops-state-hh.tar.zst` | 18636 | `e2c77552952f451ed605749817acc15bd716d58619ff3d38194e4433e6cdba6b` |
| `edge/careerops-configs-nonsecret.tar.zst` (юниты, страховые копии compose/env KEEP) | 2105 | `83f0406a6f2c6a422a7ab04eee40899399292c06b275eb1cc2f355c3bbd0a0b5` |
| `core/careerops-core-vm-and-units.tar.zst` (XML домена VM, юнит и скрипт правила br0) | 3180 | `ac97f37682c6654e218a6bf3ca3e320acb4560d38a767a92db83837fb7b9606c` |
| `core/careerops-youtrack-mcp.tar.zst` (код без `.venv`) | 17082 | `e53c58eea93552e5afcf04c6f74c07df63308e56f7f8124c41901c4e9e98fa50` |
| `vm/k8s-bootstrap-small.tar.gz` (мелкие файлы bootstrap VM) | 222166 | `179a2a49fde718be401691ffeb56dcc8735514537a48fa665c98f2ac0f60c112` |

Прочие файлы комплекта (счётчики, списки, версии, квитанции V4/V7, pre-state) — в `SHA256SUMS` каталогов.
`workstation/` — ключ бывшей VM и копии SSH-конфига до очистки (ключ не удалён, а перенесён сюда).

## 3. Проверки (ворота G1, G1b)

| ID | Проверка | Результат |
|---|---|---|
| V1 | SHA-256 всех файлов на WORKSTATION, непустота | PASS: edge 26 + холодный снимок, core 4, vm 3 |
| V2 | дамп читается `pg_restore` 18.4 на WORKSTATION | PASS (113 строк TOC, exit 0) |
| V3 | состав дампа | PASS: TABLE 11, TABLE DATA 11, SEQUENCE 5, INDEX+CONSTRAINT (без FK) 31 = 31 живых |
| V4 | тестовое восстановление в одноразовый контейнер без сети | PASS: 11 таблиц, 28 819 строк, 31 индекс — совпадает с живой БД |
| V5 | полнота S3 | PASS: `careerops-raw` 61 017 объектов, 1 567 664 861 байт = листинг; `careerops-lake`, `careerops-artifacts` пусты |
| V6 | архивы читаются на WORKSTATION | PASS: все `.tar.zst` (bsdtar с zstd), 61 017 объектов в архиве объектов |
| V7 | git bundles | PASS: `careerops-app` — 7 refs, включая `refs/stash`; `careerops-reranker-src` — 3 refs |
| G1b | холодный снимок SeaweedFS | PASS: sha совпал, 109 записей |
| перед DROP | счётчики строк не изменились с экспорта; сессий к БД нет | PASS |

Версии: сервер PostgreSQL 18.6, `pg_dump` 18.6, `pg_restore` WORKSTATION 18.4.

## 4. Что удалено

| Элемент | Хост | Действие |
|---|---|---|
| таймеры и сервисы `careerops-hh-{dispatcher,materializer,planner}` | EDGE | пауза → disable → юниты удалены |
| SeaweedFS (4 контейнера, сеть, анонимный volume, образ `chrislusf/seaweedfs:4.41`, данные) и бакеты `careerops-raw`, `careerops-lake`, `careerops-artifacts` | EDGE | удалено; порт 8333 закрыт |
| БД `careerops` и роль `careerops_app` | EDGE | `DROP DATABASE`, `DROP ROLE` |
| образы `hh-worker-careerops-hh-worker`, `hh_applicant_tool` и 7 безымянных сборок `hh-worker`, сеть `hh-applicant-tool_default` | EDGE | удалено (по меткам compose) |
| `/srv/careerops/{app,backups}`, `/etc/careerops/{hh,s3,seaweedfs}`, `/var/lib/careerops/hh`, 6 объектов `~/careerops-*`, клоны `~/workspace/careerops`, `~/workspace/infra/careerops` | EDGE | удалено по списку |
| deploy-ключ GitHub и блок `Host github-careerops` | EDGE | удалено (копия конфига — `~/.ssh/config.bak.2026-09-28`) |
| VM `k8s-cp01` (домен, диск, пул и каталог cloud-init) и базовый cloud-образ | CORE | удалено |
| `careerops-br0-firewall` (юнит, скрипт, правило `DOCKER-USER`) | CORE | удалено после VM; правило снято |
| `youtrack-mcp` (код) и `/etc/careerops` (токен) | CORE | удалено |
| `~/k8s-admin.pub` | CORE | удалено |
| SSH-алиас `k8s-cp01`, записи `known_hosts` | WORKSTATION | удалено; ключ перенесён в архив |
| промежуточные копии архива на EDGE и CORE | EDGE, CORE | удалены после проверки архива WORKSTATION |

## 5. Что сохранено (KEEP) и проверки выживания

- PostgreSQL-сервер (контейнер с историческим именем `careerops-postgres`): compose и `.env` перенесены в нейтральный
  каталог `~/workspace/infra/postgres/` (R5.0), healthcheck переведён на БД `postgres` (R1b, один перезапуск 5–10 с до
  создания БД VKM); bootstrap-суперпользователь сервера сохранён (его нельзя удалить). Добавлены БД и роль `vkm_ops`
  (control plane VKM, без привилегий суперпользователя).
- Text reranker `jina-reranker-v3.5` (контейнер `careerops-reranker`, его конфиг, кэш весов, compose, исходники):
  не перезапускался — `StartedAt` и `RestartCount = 0` совпадают с базовой линией на каждом шаге.
- SSH и сеть: профили NetworkManager, мост `br0`, доверие CORE↔EDGE — не тронуты.
- YouTrack и Caddy — SHARED (другие проекты и пользователи), оставлены (D2).

| Проверка | После STOP/DISABLE | После REMOVE |
|---|---|---|
| K1 реранкер `/readyz`, ревизия `e8a93f33` | PASS | PASS |
| K2 реранкер считает | PASS (185 токенов) | — |
| K3 реранкер и PostgreSQL без перезапуска | PASS | PASS (PostgreSQL — плановый перезапуск R1b) |
| K4 PostgreSQL принимает подключения | PASS | PASS; `vkm_ops` на месте |
| K5 ssh, docker, containerd, NVIDIA, NetworkManager | PASS | PASS |
| K-CORE: службы, адрес на `br0`, маршрут, YouTrack 200, Neo4j/OpenSearch healthy | PASS | PASS |
| K-WS: SSH WORKSTATION → CORE и → EDGE; CORE → EDGE по LAN | PASS | PASS |

Находка вне задачи: SSH-алиасы `core-lan`/`edge-lan` на серверах указывают на старую подсеть, которой нет ни на одном
интерфейсе (сломаны до начала работ); доверие CORE↔EDGE по действующим LAN-адресам работает.

## 6. Соответствие постановке §54

| Критерий | Статус |
|---|---|
| inventory сохранён | PASS — [инвентарь B](../implementation_work/AGENT_B_INFRA_INVENTORY.md), сырые данные — git-ignored рабочий каталог |
| CareerOps DB exported | PASS — custom + schema/data SQL + globals без паролей |
| checksum записан | PASS — раздел 2 и `SHA256SUMS` |
| копия на Windows существует | PASS — архивный каталог WORKSTATION |
| export проверен | PASS — V1–V7, G1b |
| old DB removed после verify | PASS |
| SeaweedFS removed | PASS |
| old CareerOps services removed | PASS |
| text reranker survives | PASS |
| PostgreSQL server survives | PASS |
| SSH/network survives | PASS |

## 7. Действия пользователя (секреты не архивировались)

1. Отозвать deploy-ключ в настройках репозитория CareerOps на GitHub.
2. Отозвать токен YouTrack, которым пользовался `youtrack-mcp`; при желании удалить проект CareerOps и
   GitHub-интеграцию CareerOps внутри YouTrack.
3. Завершить сессии трёх профилей внешнего сервиса (cookies и токены лежали только на EDGE и удалены вместе с
   `/srv/careerops`).
4. По желанию — удалить снимки `~/recovery` на CORE и EDGE (08.09; вероятно, содержат старые копии конфигурации
   CareerOps, D10).
5. Технический долг безопасности (D8, не менялся): вход по паролю в sshd, text-реранкер слушает все интерфейсы,
   YouTrack опубликован в Интернет, tailnet с чужими устройствами.
