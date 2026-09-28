# AGENT B — инвентаризация инфраструктуры (VKM Corpus Platform v0, Phase 0)

Дата: 28.09.2026. Агент: B (INFRASTRUCTURE AUDITOR). Режим: **только чтение и диагностика**. На CORE, EDGE, VM и
WORKSTATION ничего не остановлено, не удалено, не перезапущено и не изменено. Единственные «активные» действия —
read-only SQL (`default_transaction_read_only=on`), `weed shell` без изменяющих команд, HTTP GET к health-эндпоинтам и
один тестовый `POST /v1/rerank` к текстовому реранкеру (без побочных эффектов).

Гигиена этого файла: хосты названы ролями (CORE, EDGE, WORKSTATION, VM `k8s-cp01`); IP-адреса, MAC, логины, серийные
номера, публичный домен и секреты не приводятся. Домашний каталог администратора на серверах обозначен `~`.
Публичный домен — `<public-domain>`. Архивный раздел WORKSTATION — `<ARCHIVE_DRIVE>`. Сырые данные инвентаря (с адресами
и именами, но без значений секретов) лежат в git-ignored каталоге `work/corpus_platform/phase0/agent_b/raw/`
(по файлу на хост и раздел, перечень — §18).

Классы элементов: **KEEP** — сохранить; **CAREEROPS_ONLY** — используется только CareerOps, выводится по плану
`AGENT_B_CLEANUP_PLAN.md`; **SHARED** — носит имя CareerOps или обслуживает CareerOps, но нужен и дальше (удалять нельзя);
**UNKNOWN** — назначение не доказано, без решения координатора не трогать.

## 0. Главное

1. **CareerOps на EDGE — не «остатки», а живая система.** Три systemd-таймера (каждые 5 и 10 минут и ежедневно в 07:30)
   запускают диспетчер, материализатор S3→PostgreSQL и планировщик; диспетчер по расписанию запускает эфемерный
   worker-контейнер, который выполняет действия во внешнем веб-сервисе от имени пользователя по трём профилям.
   Последняя запись в БД — 27.09 около 22:42 (за ~3 ч до инвентаря), последний объект в S3 — 28.09. Остановка
   CareerOps — решение пользователя (§17, D1).
2. **KEEP-объекты носят имя CareerOps.** PostgreSQL-сервер — это контейнер `careerops-postgres` (compose-проект
   `postgres`, compose-файл и `.env` лежат в `~/workspace/careerops/postgres/`), его bootstrap-суперпользователь —
   `careerops_admin`. Текстовый Jina reranker — контейнер `careerops-reranker` с конфигом `/etc/careerops/reranker/env`,
   кэшем весов `/var/lib/careerops/reranker-cache` и исходниками в клоне монорепозитория CareerOps. Активные сетевые
   профили LAN — `careerops-br0*` (CORE) и `careerops-lan-edge` (EDGE). SSH-доверие CORE↔EDGE — ключи
   `id_ed25519_careerops_lan`. Следовательно, cleanup только по явному allowlist: **никаких** `rm -rf /etc/careerops`,
   `rm -rf /var/lib/careerops`, `rm -rf ~/workspace/careerops*` целиком, удаления NM-профилей и SSH-ключей.
3. **PostgreSQL уже в Docker, а не native** (18.6, стабильно работает 19 дней). По постановке §41 переносить не нужно.
4. **YouTrack — SHARED**, а не CareerOps-only: 6 проектов (проект CareerOps — один из них), не меньше 3 человеческих
   учёток, GitHub-интеграции с тремя репозиториями. По умолчанию KEEP; удалить можно только по решению пользователя.
   YouTrack опубликован в Интернет через Caddy на EDGE.
5. **VM `k8s-cp01` (на CORE) — брошенный и не инициализированный control plane Kubernetes** (kubelet enabled, но
   inactive; etcd и подов нет; в истории только `kubeadm config`). CAREEROPS_ONLY. Других VM и хостов CareerOps нет.
6. **`$VKM_DATA_ROOT` → CORE, `/srv/vkm/data`** (ext4 на LVM, SATA SSD, свободно 377 GB). Подробности — §15.
7. **Порты для Neo4j, OpenSearch, API и MCP на CORE свободны**, для visual reranker на EDGE свободен 18083 (§16).
   Конфликтов нет, если не занимать 8080 и 18080 на CORE (YouTrack).

## 1. Хосты и железо

| Роль | ОС / ядро | CPU | RAM / swap | GPU | Uptime |
|---|---|---|---|---|---|
| CORE | Debian 13.6 (trixie), 6.12.107+deb13-amd64 | AMD Ryzen 7 5800X, 8C/16T | 31 GiB (≈23 GiB доступно) / 23 GiB swapfile | AMD RX 580 (Ellesmere), Mesa OpenCL/Vulkan, ROCm нет — как ускоритель не использовать | 18 дн. |
| EDGE | Debian 13.6 (trixie), 6.12.107+deb13-amd64 | AMD Ryzen 5 4600H, 6C/12T | 30 GiB (≈27 GiB доступно) / 23 GiB swapfile | NVIDIA GTX 1650 Mobile 4 GB (TU117M) + iGPU Vega; драйвер 595.91.07, CUDA driver API 13.2; persistence mode on; power limit 50 W | 19 дн. |
| VM `k8s-cp01` (гость на CORE) | Debian 13 cloud, 6.12.107+deb13-cloud-amd64 | 2 vCPU | 4 GiB / без swap | — | 18 дн. |
| WORKSTATION | Windows 11 | i7-14700KF (по брифу) | 64 GB (по брифу) | RTX 5070 Ti 16 GB (по брифу) | — |

EDGE — ноутбук, работает от сети (батарея 100 %); логин-менеджер настроен файлом `logind.conf.d/90-homelab.conf`.
На CORE работает ручное управление вентиляторами (`core-fanctl`, `core-casefan-apply`).

## 2. Диски и свободное место

| Хост | Носитель | Разметка | ФС `/` | Занято | Свободно | Прочее |
|---|---|---|---|---|---|---|
| CORE | 1 × SATA SSD 512 GB (Apacer AS350, DRAM-less; 11,7 тыс. ч работы, ≈16,7 TB записано) | `/boot` ext4 1 GB; LVM VG `core-vg` 476 GB (24 GB не распределено) → LV `root` 452 GB | ext4 | 45 GB (11 %) | **377 GB** | inodes 1 %; `/tmp` — tmpfs |
| EDGE | 1 × NVMe 512 GB (Samsung MZVLQ512; 6,1 тыс. ч; износ 9 %; 48 TB записано; ошибок нет) | EFI 1 GB; `/boot` 1 GB; LVM VG `edge-vg` 475 GB (24 GB не распределено) → LV `root` 451 GB | ext4 | 56 GB (14 %) | **366 GB** | inodes 2 % |
| VM `k8s-cp01` | qcow2 32 GiB виртуально / 5,85 GiB фактически | vda1 ext4 | ext4 | 4,3 GB | 26 GB | — |
| WORKSTATION | NVMe 1 TB (раздел с рабочими клонами) + NVMe 2 TB (системный раздел и раздел данных) | — | NTFS | — | ≈745 GB (1 TB), ≈142 GB (системный), ≈790–840 GB (данные) | виртуальный диск облачной синхронизации — **не для архива** |

Крупные потребители места:

- CORE: `/var/lib/libvirt/images` 6,2 GB (диск VM 5,9 GB и базовый cloud-образ 0,34 GB), `/var/lib/docker` 3,5 GB
  (образ YouTrack 3,15 GB), `/var/log/journal` 2,3 GB, `/srv/youtrack` 1,4 GB (из них `host-archives` 1,3 GB),
  `/home` 1,9 GB.
- EDGE: `/var/lib/docker` 9,2 GB, `/usr/local/cuda-13.2` 4,8 GB, `/opt/nvidia` (Nsight) 2,4 GB,
  `/var/lib/careerops` 2,3 GB (кэш весов реранкера), `/srv/careerops` 2,0 GB (SeaweedFS 1,6 GB, приложение 385 MB),
  `/home` 1,8 GB (из них CareerOps-экспорты ≈0,2 GB и снимки восстановления 0,64 GB).

## 3. Сеть

| Хост | Интерфейсы | Профили NetworkManager | Firewall | Прочее |
|---|---|---|---|---|
| CORE | LAN через мост `br0` (порт — физический NIC), `docker0` (down), docker-мост `youtrack_default`, `vnet0` (tap VM в `br0`), `tailscale0` | `careerops-br0` (bridge, активен, несёт LAN-адрес хоста), `careerops-br0-port-<nic>` (порт моста), `careerops-lan-core` (старый static-профиль, неактивен, autoconnect=yes) | ufw установлен, но inactive; nftables — только цепочки Docker и Tailscale; INPUT policy accept; FORWARD policy drop (Docker); в `DOCKER-USER` правило ACCEPT `br0→br0` от `careerops-br0-firewall` | Tailscale активен (tailnet с устройствами других пользователей); DNS — роутер; локальных DNS/DHCP нет |
| EDGE | LAN `eno1` (static), Wi-Fi (выкл.), docker-мосты `postgres_default`, `careerops-seaweedfs_seaweed`, `hh-applicant-tool_default` (без контейнеров) | `careerops-lan-edge` (ethernet, активен, несёт LAN-адрес хоста), Wi-Fi-профиль без autoconnect | ufw inactive; nftables — только цепочки Docker; INPUT policy accept | Tailscale установлен, `tailscaled` inactive; DNS — роутер |

Слушающие порты (кроме avahi 5353/udp и локальных сокетов containerd):

| Хост | Порт | Процесс | Привязка | Назначение | Класс |
|---|---|---|---|---|---|
| CORE | 22/tcp | sshd | все адреса | администрирование | KEEP |
| CORE | 8080/tcp | docker-proxy → YouTrack | только LAN-адрес CORE | YouTrack (к нему проксирует Caddy на EDGE) | SHARED |
| CORE | 18080/tcp | docker-proxy → YouTrack | 127.0.0.1 | локальный доступ к YouTrack | SHARED |
| CORE | 41641/udp, tailnet-TCP | tailscaled | tailnet | Tailscale | KEEP |
| EDGE | 22/tcp | sshd | все адреса | администрирование | KEEP |
| EDGE | 80/tcp, 443/tcp+udp | caddy | все адреса | публичный вход `<public-domain>` → YouTrack | SHARED |
| EDGE | 2019/tcp | caddy admin | 127.0.0.1 | админ-API Caddy | SHARED |
| EDGE | 5432/tcp | docker-proxy → PostgreSQL | LAN-адрес EDGE и 127.0.0.1 | PostgreSQL | KEEP (сервер) |
| EDGE | 8333/tcp | docker-proxy → SeaweedFS S3 | LAN-адрес EDGE и 127.0.0.1 | S3 CareerOps | CAREEROPS_ONLY |
| EDGE | 18082/tcp | python (`careerops_reranker`, host network) | **0.0.0.0** | Jina text reranker | KEEP |

Наблюдения по безопасности (вне задачи B, для координатора): на обоих хостах `PasswordAuthentication yes` и
`PermitRootLogin without-password`; реранкер слушает 0.0.0.0 без аутентификации; YouTrack доступен из Интернета (в логах
есть анонимные обращения); в tailnet CORE есть устройства других пользователей. Отсюда правило для новых сервисов VKM:
публиковать порты только на `127.0.0.1` и LAN-адрес хоста, не на `0.0.0.0` и не через Caddy.

## 4. Docker и Compose

Обе машины: Docker 26.1.5 (пакет Debian `docker.io`), Compose 2.26.1, overlay2, cgroup systemd, `live-restore: true`.
EDGE дополнительно: NVIDIA Container Toolkit 1.20.0, runtime `nvidia` в `daemon.json` (runtime по умолчанию — runc,
GPU выдаётся через device requests), юниты `nvidia-cdi-refresh`. Swarm не используется.

| Хост | Compose-проект | Контейнеры (образ) | Compose-файл | Данные | Порты | Класс |
|---|---|---|---|---|---|---|
| CORE | `youtrack` | `youtrack` (`jetbrains/youtrack:2026.1.12024`) | `~/workspace/infra/youtrack/compose.yml` | bind `/srv/youtrack/{data,conf,logs,backups}` | LAN:8080, 127.0.0.1:18080 | SHARED |
| EDGE | `postgres` | `careerops-postgres` (`postgres:18.6`; mem 6 GiB, 4 CPU) | `~/workspace/careerops/postgres/compose.yml` + `.env` (имена переменных: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`) | volume `postgres_postgres_data` (100 MB) | LAN:5432, 127.0.0.1:5432 | SHARED: сервер KEEP, БД `careerops` — CAREEROPS_ONLY |
| EDGE | `careerops-reranker` | `careerops-reranker` (`careerops-reranker:2.14-cu132-fp16`, 6,4 GB); host network, GPU, read-only rootfs, `cap_drop: ALL`, no-new-privileges | `~/workspace/careerops-reranker-deploy/compose.yml`; env-файл `/etc/careerops/reranker/env` | bind `/var/lib/careerops/reranker-cache` (2,3 GB) | 18082 | KEEP |
| EDGE | `careerops-seaweedfs` | `…-master-1`, `…-volume-1`, `…-filer-1`, `…-s3-1` (`chrislusf/seaweedfs:4.41`) | два файла: `~/workspace/infra/careerops/seaweedfs/compose.yml` (master/volume/filer, 29.08) и `~/workspace/careerops/infra/compose/seaweedfs/compose.yml` (s3, 08.09) | bind `/srv/careerops/seaweedfs/{master,volume,filer}` (1,6 GB); `/etc/careerops/seaweedfs/s3.json`; анонимный volume `402ca5e9…` (12 KB) | LAN:8333, 127.0.0.1:8333 | CAREEROPS_ONLY |
| EDGE | `hh-worker` | контейнера нет (эфемерный, запускается диспетчером; `restart: "no"`, host network) | `/srv/careerops/app/infra/compose/hh-worker/compose.yml` | bind в `/srv/careerops/app/...`, env из `/etc/careerops/hh/` | — | CAREEROPS_ONLY |
| EDGE | `hh-applicant-tool` | контейнера нет; осталась сеть `hh-applicant-tool_default` | `/srv/careerops/app/hh-applicant-tool/docker-compose.yml` | — | — | CAREEROPS_ONLY |

Образы:

| Хост | Образ | Размер | Класс | Обоснование |
|---|---|---|---|---|
| EDGE | `careerops-reranker:2.14-cu132-fp16` | 6,4 GB | KEEP | работающий text reranker |
| EDGE | `pytorch/pytorch:2.14.0-cuda13.2-cudnn9-runtime` | 6,21 GB (слои общие с образом реранкера) | KEEP | база сборки реранкера; кандидат в базу для m0 |
| EDGE | `postgres:18.6` | 456 MB | KEEP | работающий PostgreSQL |
| EDGE | `chrislusf/seaweedfs:4.41` | 263 MB | CAREEROPS_ONLY | только SeaweedFS |
| EDGE | `hh-worker-careerops-hh-worker:latest` и 7 безымянных сборок (`<none>`; метка compose-проекта `hh-worker`, сервис `careerops-hh-worker`) | 174–216 MB каждый (слои частично общие) | CAREEROPS_ONLY | метки compose доказывают принадлежность |
| EDGE | `hh_applicant_tool:latest` | 1,56 GB | CAREEROPS_ONLY | метка compose-проекта `hh-applicant-tool` |
| EDGE | `ubuntu:latest`, `hello-world:latest` | 78 MB, 10 KB | UNKNOWN | общие образы, к CareerOps не привязаны |
| CORE | `jetbrains/youtrack:2026.1.12024` | 3,15 GB | SHARED | YouTrack |
| CORE | `postgres:18.6` | 456 MB | UNKNOWN | контейнера нет; пригоден для проверочного восстановления дампа |
| CORE | `hello-world:latest` | 10 KB | UNKNOWN | тестовый |

Volumes: EDGE `postgres_postgres_data` (100 MB, KEEP), `402ca5e9…` (12 KB, анонимный `/data` контейнера s3,
CAREEROPS_ONLY), `6611dacd…` (12 KB, 0 ссылок, создан 29.08 — вероятно, от ранней пробы SeaweedFS; UNKNOWN, сирота).
На CORE volumes нет. `docker system df` на EDGE показывает 8,45 GB «reclaimable», но основная часть — база pytorch
(KEEP), поэтому «reclaimable» ≠ «можно удалить»; `docker system prune` запрещён.

## 5. systemd, таймеры, cron

Пользовательские (не пакетные) юниты:

| Хост | Юнит | Что делает | Состояние | Класс |
|---|---|---|---|---|
| CORE | `core-fanctl.service`, `core-casefan-apply.service` | управление вентиляторами | running / enabled | KEEP |
| CORE | `tailscaled-rekick.service` + `.timer`, drop-in `tailscaled.service.d/override.conf` | поздний перезапуск tailscaled после загрузки | enabled | KEEP |
| CORE | `careerops-br0-firewall.service` + `/usr/local/sbin/careerops-br0-firewall` | вставляет в `DOCKER-USER` правило ACCEPT `br0→br0` (иначе FORWARD drop Docker режет мостовой трафик VM); `WantedBy=docker.service`, `PartOf=docker.service` | active (exited), enabled | CAREEROPS_ONLY (нужен, пока в `br0` есть VM) |
| EDGE | `careerops-hh-dispatcher.service` + `.timer` | каждые 5 мин: `python -m careerops_scheduler.dispatcher` из `/srv/careerops/app/.venv`; при наступлении срока пакета запускает worker-контейнер | enabled; последний запуск 28.09 00:47 («nothing_due») | CAREEROPS_ONLY |
| EDGE | `careerops-hh-materializer.service` + `.timer` | каждые 10 мин: S3 RAW → PostgreSQL (`scripts/materialize_hh_pending.py --limit 10`) | enabled; 28.09 00:48: discovered=241, eligible=217, pending=0 | CAREEROPS_ONLY |
| EDGE | `careerops-hh-planner.service` + `.timer` | ежедневно 07:30 (Europe/Moscow): план запусков на день, пишет в S3 | enabled; последний 27.09 07:30, следующий 28.09 07:30 | CAREEROPS_ONLY |

Все CareerOps-сервисы EDGE запускаются от имени администратора, `EnvironmentFile=/etc/careerops/hh/{env,scheduler.env}`
(имена переменных: S3 endpoint/bucket/prefix/ключи, PostgreSQL DSN, режим и флаг внешних записей, окно расписания и т. п.).

Прочие работающие сервисы (KEEP): CORE — ssh, docker, containerd, libvirtd, virtlogd, virtlockd, tailscaled,
NetworkManager, smartmontools, avahi, cron, timesyncd; EDGE — ssh, docker, containerd, caddy, nvidia-persistenced,
NetworkManager, smartmontools, avahi, cron, timesyncd, bluetooth, ModemManager. Упавших юнитов нет. Пользовательских
(`--user`) юнитов и таймеров нет. Остальные таймеры — стандартные Debian (apt, logrotate, fstrim, e2scrub, man-db,
dpkg-db-backup).

cron:

| Хост | Запись | Класс |
|---|---|---|
| CORE | root: `15 4 * * * /usr/local/bin/backup-youtrack-host.sh` — tar `conf` и `backups` YouTrack, его compose-файла и юнитов tailscale в `/srv/youtrack/host-archives/`, хранение 14 дней | SHARED (часть YouTrack) |
| EDGE | пользовательских crontab нет; `/etc/cron.d` — только пакетные `anacron`, `e2scrub_all` | — |

## 6. PostgreSQL (EDGE)

- Установка: Docker-контейнер `careerops-postgres`, образ `postgres:18.6` (Debian 18.6-1.pgdg13+2), compose-проект
  `postgres`; `PGDATA=/var/lib/postgresql/18/docker` в volume `postgres_postgres_data`; restart `unless-stopped`;
  работает с 08.09. Native-пакетов PostgreSQL и `pg_lsclusters` нет ни на EDGE, ни на CORE; `psql`/`pg_dump` на хосте
  нет — клиенты только внутри контейнера.
- Healthcheck: `pg_isready -U <superuser> -d careerops` каждые 5 с. После удаления БД `careerops` `pg_isready`
  останется «healthy», но сервер будет писать FATAL в журнал каждые 5 с (см. план, R1b).
- Настройки: `listen_addresses='*'`, порт 5432, `max_connections=100`, `shared_buffers=128MB`, `work_mem=4MB`,
  `ssl=off`, `password_encryption=scram-sha-256`, `wal_level=replica`, архивирование WAL выключено, лог в stderr
  (json-file Docker; в compose нет ротации логов).
- pg_hba (структура): `local all all trust` (только внутри контейнера); `host all all 127.0.0.1/32 и ::1 scram-sha-256`;
  `host all all all scram-sha-256` (доступ из LAN по паролю); replication — аналогично для local и loopback.
- Базы:

| БД | Размер | Владелец | Содержимое | Класс |
|---|---|---|---|---|
| `careerops` | 45 MB | `careerops_admin` | схема `careerops` (+ пустая `public`): 11 таблиц, 31 индекс, 5 sequences; views, функций и large objects нет; расширение только plpgsql; ≈28,8 тыс. строк на момент инвентаря (счётчики растут, пока работают таймеры; по таблицам — raw `edge_08_postgres_db_careerops.txt`) | CAREEROPS_ONLY |
| `postgres` | 7,7 MB | `careerops_admin` | пользовательских таблиц нет | KEEP |
| `template0`, `template1` | 7,5 MB | `careerops_admin` | шаблоны | KEEP |

- Роли (без хешей): `careerops_admin` — superuser, bootstrap-роль сервера (создана из `POSTGRES_USER`; удалить нельзя,
  переименовать можно только из-под другого суперпользователя); `careerops_app` — login, DML на 11 таблиц схемы
  `careerops`, CONNECT на БД `careerops`; членства в ролях нет. Слотов репликации, подписок и публикаций нет.
- Активность: в момент инвентаря клиентских сессий нет. С момента старта сервера в `careerops`: 388 тыс. commit,
  329 тыс. сессий (таймеры), 30,9 тыс. insert, 15,4 тыс. update, 5,7 тыс. delete. Первая запись — 03.09, последняя —
  27.09 19:42 UTC. Других потребителей сервера, кроме CareerOps, нет.

## 7. Jina text reranker (EDGE) — KEEP, не трогать

| Параметр | Значение |
|---|---|
| Контейнер / compose | `careerops-reranker` / проект `careerops-reranker`, `~/workspace/careerops-reranker-deploy/compose.yml` |
| Образ | `careerops-reranker:2.14-cu132-fp16`, собран 10.09 из `pytorch/pytorch:2.14.0-cuda13.2-cudnn9-runtime`; Dockerfile — `~/workspace/careerops-reranker-src/infra/compose/reranker/` |
| Процесс | `python -m careerops_reranker` (uid 10001), host network, `0.0.0.0:18082` |
| Модель | `jinaai/jina-reranker-v3.5`, revision `e8a93f33f0b22108f8c2364f8484ce3422552fbc` (model, code и tokenizer) |
| Точность / backend | float16 / `transformers-cuda`; torch 2.14.0+cu132; transformers 4.57.3 |
| VRAM | ≈1,2 GiB из 4 GiB в простое (после тестового запроса 1,25 GiB) → ≈2,8 GiB свободно для m0 (важно для агента F) |
| API | `GET /healthz`; `GET /readyz` (статус и runtime identity); `POST /v1/rerank` с полями `query`, `documents[]`, `top_n`, `token_budget`, `expected_runtime` (строгая схема; `expected_runtime` должен совпасть с runtime из `/readyz`; тело ≤ 4 MiB) |
| Тест 28.09 | один `POST /v1/rerank` (2 документа): HTTP 200, 185 токенов, 1,3 с, релевантный документ первым |
| Веса | `/var/lib/careerops/reranker-cache/huggingface` (2,3 GB; blob safetensors 1,19 GB лежит дважды — в корне `HF_HOME` и в `hub/`, ≈1,2 GB дубля; сейчас не трогать) |
| Env-файл | `/etc/careerops/reranker/env` — только несекретные параметры (model id, revisions, dtype, device, host, port, backend) |
| Клиенты | сейчас нет (только healthcheck каждые 15 с) |
| Зависимость от CareerOps | пакет `careerops_reranker` живёт в монорепозитории CareerOps (GitHub и локальный клон); конфиг и кэш — в путях с именем `careerops` |

## 8. SeaweedFS (EDGE) — CAREEROPS_ONLY

- Версия 4.41 (сборка «30GB»), 4 контейнера healthy: master (9333), volume (8080; `-max=10`, `-minFreeSpace=50GiB`),
  filer (8888; leveldb2), s3 (8333, опубликован на LAN и localhost; `-allowDeleteBucketNotEmpty=false`). Наружу
  опубликован только s3.
- Данные `/srv/careerops/seaweedfs`: volume 1,6 GB (занято 10 из 10 слотов volume), filer 31 MB, master 28 KB.
- Бакеты (владелец — identity `careerops-admin`):

| Бакет | Логический размер | Объектов | Структура | Последняя запись |
|---|---|---|---|---|
| `careerops-raw` | 1 567 664 861 B (≈1,57 GB) | 61 018 (collection `careerops-raw`) | один верхний префикс `_lab/` с 4 подпрефиксами; 99 % объёма — пакеты сырых ответов внешнего сервиса | 28.09 |
| `careerops-lake` | 0 | 0 | пусто | — |
| `careerops-artifacts` | 0 | 0 | пусто | — |

  Плюс служебный `/topics` filer-а (71 MB, 927 файлов, collection по умолчанию).
- Identities в `s3.json` (ключи не читались): `careerops-admin` (Admin, Read, List, Tagging, Write) и
  `careerops-collector` (Read, List, Write на `careerops-raw`).
- Клиенты: таймеры CareerOps (материализатор и планировщик — через `/etc/careerops/hh/env`), `aws` CLI на хосте
  с `/etc/careerops/s3/collector.env`. Установленных соединений на 8333 в момент инвентаря нет.
- Проверка бакетов без ключей: `weed shell` внутри master-контейнера (`s3.bucket.list`, `fs.du /buckets/<b>`,
  `collection.list`) — команды в плане, §3.2.

## 9. YouTrack (CORE) — SHARED, по умолчанию KEEP

- Docker `jetbrains/youtrack:2026.1.12024`, compose `~/workspace/infra/youtrack/compose.yml`; установлен 02.04.2026 —
  раньше CareerOps (первые артефакты CareerOps — 25–29.08). JVM ≈2,9 GB RSS.
- Данные `/srv/youtrack`: `data` 60 MB, `conf` 164 KB, `logs` 19 MB, `backups` 86 MB (встроенные ежедневные бэкапы,
  хранятся 5), `host-archives` 1,3 GB (cron-архивы, 15 шт. по ≈85 MB).
- Доступ: LAN:8080, 127.0.0.1:18080; публично — через Caddy на EDGE (HTTPS, `<public-domain>`).
- Использование (по логам, без имён): 6 проектов, проект CareerOps — один из них; не меньше 3 человеческих учёток —
  владелец (последняя активность 23.09), второй пользователь (06.09), третий (апрель); GitHub-интеграции с 3
  репозиториями (CareerOps и 2 других проекта пользователя). После 23.09 — только анонимные и гостевые обращения
  (1–25 в день). Вывод: **не CareerOps-only**, доказательства ненужности нет.
- Связанные с CareerOps части YouTrack: проект CareerOps внутри YouTrack и GitHub-интеграция с репозиторием CareerOps
  (удаляются в UI YouTrack самим пользователем, если нужно), вспомогательные скрипты
  `~/workspace/careerops/services/youtrack-mcp` на CORE (73 MB вместе с `.venv`) и токен в
  `/etc/careerops/youtrack-mcp/env` — CAREEROPS_ONLY.

## 10. Caddy (EDGE) — SHARED (обслуживает только YouTrack)

- Caddy v2.11.4 (пакет из официального apt-репозитория Caddy), `caddy.service` (пользователь `caddy`), конфиг
  `/etc/caddy/Caddyfile` — один сайт: `<public-domain>` → `encode zstd gzip` → `reverse_proxy` на LAN-адрес CORE:8080
  (YouTrack). Слушает `*:80`, `*:443` (tcp и udp/HTTP3), admin `127.0.0.1:2019`. Сертификат ACME обновляется
  (последняя проверка 27.09). Access-логов нет.
- Маршрутов на CareerOps, SeaweedFS или реранкер нет, поэтому при выводе CareerOps в Caddy менять нечего. Судьба Caddy
  связана с YouTrack (§17, D2).

## 11. libvirt и VM

- CORE: libvirt 11.3.0, QEMU 10.0.11; работают libvirtd, virtlogd, virtlockd; `libvirt-guests` enabled. Сеть
  libvirt `default` неактивна (VM подключена мостом в `br0`). Пулы: `images` (`/var/lib/libvirt/images`) и `k8s-cp01`
  (`/var/lib/libvirt/cloud-init/k8s-cp01`). На EDGE libvirt нет.
- VM `k8s-cp01`: running, **autostart включён**, 2 vCPU, 4 GiB, UEFI (q35), qemu-guest-agent; диск `k8s-cp01.qcow2`
  (32 GiB виртуально, 5,85 GiB фактически), cloud-init `seed.iso`; базовый образ `debian-13-genericcloud-amd64.qcow2`
  (339 MB); снимков нет. Создана 09.09, bootstrap 13.09.
- Внутри VM: containerd 1.7.24 running; kubelet 1.36.4 enabled, но inactive; kubeadm, kubectl, crictl 1.36, helm;
  `/etc/kubernetes` пуст (только `manifests/.kubelet-keep`), `/var/lib/etcd` нет, подов и контейнеров нет; в containerd
  закешированы образы control plane v1.36.4 и Cilium. `~/k8s-bootstrap` (730 MB): публичные deb-пакеты, tar образов,
  chart и values Cilium, CLI helm и cilium, списки SHA-256. В истории shell — только `kubeadm config`, `version`,
  `helm pull/template`: **кластер не инициализировался**. Уникальной ценности нет, кроме мелких файлов values/списков.
- Принадлежность CareerOps: комментарий SSH-ключа администратора VM указывает на CareerOps; SSH-алиас и ключ
  `careerops_k8s_ed25519` на WORKSTATION; правило `careerops-br0-firewall` на CORE обслуживает именно её трафик.
  Других VM и worker-узлов нет (`virsh list --all`; ARP на `br0` — только роутер, EDGE, VM и один LAN-клиент).

## 12. Уже установленные инструменты

| Инструмент | CORE | EDGE | WORKSTATION |
|---|---|---|---|
| Python | 3.13.5 (системный) | 3.13.5 (системный) | см. агент A/C |
| uv | нет | нет | — |
| git / git-lfs | 2.47.3 / **нет** | 2.47.3 / **нет** | есть |
| rsync, zstd, jq, curl, wget, tmux, htop | есть | есть | ssh/scp (OpenSSH), `tar` (bsdtar 3.8.8 с zstd) |
| Java | нет | OpenJDK 21 и NVIDIA OpenJDK 8 — зависимости CUDA/Nsight | — |
| Docker / Compose | 26.1.5 / 2.26.1 | 26.1.5 / 2.26.1 | Docker Desktop (не запущен, по брифу) |
| NVIDIA Container Toolkit / CUDA toolkit | — | 1.20.0 / 13.2 (`/usr/local/cuda-13.2`) | CUDA 13.x (по брифу) |
| PostgreSQL-клиенты | только в контейнерах | только в контейнере | `pg_restore` 18.4 (локальная установка PostgreSQL 18, служба остановлена) — годится для `pg_restore --list` |
| Прочее | virsh, qemu-img | aws-cli 2.23.6 (используется S3-коллектором CareerOps), caddy | — |

`vm.max_map_count` = 1 048 576 на обоих хостах — требование OpenSearch (≥ 262 144) выполнено. Установка git-lfs или uv
на серверы — системное изменение и требует отдельного разрешения (§17, D9).

## 13. Реестр следов CareerOps (с классификацией и доказательствами)

| # | Хост | Элемент | Класс | Доказательство | Зависимости и риски |
|---|---|---|---|---|---|
| C1 | EDGE | таймеры и сервисы `careerops-hh-{dispatcher,materializer,planner}` (6 юнитов, 3 symlink в `timers.target.wants`, stamp-файл таймера планировщика) | CAREEROPS_ONLY | имена, `WorkingDirectory=/srv/careerops/app`, `EnvironmentFile=/etc/careerops/hh/*`, журнал | пишут в БД `careerops` и в S3; перед экспортом их нужно приостановить |
| C2 | EDGE | compose-проект `careerops-seaweedfs` (4 контейнера, сеть `careerops-seaweedfs_seaweed`, анонимный volume `402ca5e9…`, образ `chrislusf/seaweedfs:4.41`) | CAREEROPS_ONLY | имя проекта, identities `careerops-*`, бакеты `careerops-*`, клиенты — только C1 | содержимое бакетов нужно выгрузить до удаления |
| C3 | EDGE | данные SeaweedFS `/srv/careerops/seaweedfs` и бакеты `careerops-raw`, `careerops-lake`, `careerops-artifacts` | CAREEROPS_ONLY | §8 | 1,57 GB, 61 018 объектов; пустые бакеты уходят вместе с данными |
| C4 | EDGE | БД `careerops` (45 MB) и роль `careerops_app` | CAREEROPS_ONLY | владельцы и гранты, единственный потребитель — C1 | удаляются только после проверенного экспорта; сервер остаётся |
| C5 | EDGE | `/srv/careerops/app` (385 MB: git-клон CareerOps, 1 stash, `.venv`, конфигурация профилей внешнего сервиса с сессионными cookies и токенами) и `/srv/careerops/backups` (секреты и старый дамп 03.09) | CAREEROPS_ONLY | пути, git remote CareerOps | содержат секреты и персональные данные; stash нужно сохранить через `git bundle --all` |
| C6 | EDGE | `/etc/careerops/hh`, `/etc/careerops/s3`, `/etc/careerops/seaweedfs` | CAREEROPS_ONLY | env-файлы C1/C2, `s3.json` | секреты; **`/etc/careerops/reranker` — KEEP (S2)** |
| C7 | EDGE | `/var/lib/careerops/hh` (состояние планировщика, обновляется каждые 5 мин) | CAREEROPS_ONLY | права администратора, mtime | **`/var/lib/careerops/reranker-cache` — KEEP (S2)** |
| C8 | EDGE | образы `hh-worker-careerops-hh-worker:latest` + 7 безымянных сборок, `hh_applicant_tool:latest`; сеть `hh-applicant-tool_default` | CAREEROPS_ONLY | метки compose-проектов `hh-worker` и `hh-applicant-tool` | удалять точечно по ID и меткам |
| C9 | EDGE | в `~`: 6 объектов по маске `~/careerops-*` (≈210 MB: экспорты 01.09 и пользовательские JSON-профили); `~/workspace/infra/careerops`; `~/workspace/careerops` (git-клон, незакоммиченные изменения — compose s3 и его `.bak`) | CAREEROPS_ONLY, **кроме `~/workspace/careerops/postgres/` (S1)** | имена, git remote | персональные данные — только локальный архив; `postgres/` сначала скопировать в нейтральный путь |
| C10 | EDGE | `~/.ssh/careerops_edge{,.pub}` и блок `Host github-careerops` в `~/.ssh/config` (deploy-ключ GitHub для репозитория CareerOps) | CAREEROPS_ONLY | алиас и remote `/srv/careerops/app` | пользователь отзывает deploy-ключ в GitHub |
| C11 | CORE | VM `k8s-cp01`: домен, диск (5,9 GB), пул и каталог cloud-init, autostart | CAREEROPS_ONLY | §11 | не инициализирована; перед удалением сохранить мелкие конфиги |
| C12 | CORE | `careerops-br0-firewall.service` и `/usr/local/sbin/careerops-br0-firewall` | CAREEROPS_ONLY (привязан к C11) | правило «CareerOPS br0 L2 bridge» в `DOCKER-USER` | удалять **после** C11; трафик самого хоста правило не затрагивает |
| C13 | CORE | `~/workspace/careerops/services/youtrack-mcp` (скрипты автоматизации YouTrack для CareerOps) и `/etc/careerops/youtrack-mcp/env` (токен) | CAREEROPS_ONLY | имена скриптов, путь | пользователь отзывает токен в YouTrack |
| C14 | CORE | `~/k8s-admin.pub` (ключ для VM) | CAREEROPS_ONLY | комментарий ключа | после C11 |
| C15 | WORKSTATION | SSH-алиас `k8s-cp01` в `~/.ssh/config` и `config.txt`, ключ `careerops_k8s_ed25519{,.pub}` | CAREEROPS_ONLY | конфиг | удаляет пользователь после C11 |
| C16 | WORKSTATION | каталог `CareerOps` (клон и локальные бэкапы) в корне раздела с рабочими клонами | UNKNOWN (вне задачи) | — | не трогать |
| S1 | EDGE | PostgreSQL-сервер: контейнер `careerops-postgres`, проект `postgres`, volume `postgres_postgres_data`, compose и `.env` в `~/workspace/careerops/postgres/`, суперпользователь `careerops_admin` | **SHARED → KEEP** | постановка §8 и §16 | имя историческое; переименование — отдельное решение (D6/D7) |
| S2 | EDGE | text reranker: контейнер и образ `careerops-reranker*`, `/etc/careerops/reranker/env`, `/var/lib/careerops/reranker-cache`, `~/workspace/careerops-reranker-deploy`, `~/workspace/careerops-reranker-src` (клон CareerOps — источник сборки), база `pytorch/pytorch:…` | **SHARED → KEEP** | §7 | нельзя удалять вместе с «careerops-*»; ребрендинг — агент F |
| S3 | CORE, EDGE | профили NetworkManager `careerops-br0`, `careerops-br0-port-<nic>`, `careerops-lan-core` (CORE) и `careerops-lan-edge` (EDGE) | **SHARED → KEEP** | несут LAN-адреса хостов | удаление = потеря сети хоста; переименовывать только с консольным доступом |
| S4 | CORE, EDGE | SSH-ключи `id_ed25519_careerops_lan{,.pub}`, записи `authorized_keys` «core-to-edge…/edge-to-core…», алиасы `core-lan`/`edge-lan` | **SHARED → KEEP** | административное доверие CORE↔EDGE | удалять нельзя |
| S5 | CORE, EDGE | YouTrack (контейнер, `/srv/youtrack`, cron-бэкап), Caddy (единственный сайт — YouTrack) | **SHARED → KEEP по умолчанию** | §9, §10 | удаление — только решение пользователя (D2) |
| U1 | CORE, EDGE | `~/recovery/*-pre-headless-20260908-*` (снимки конфигурации обоих хостов, ≈0,64 GB на хост) | UNKNOWN (административный архив) | даты и состав | вероятно содержат копии `/etc` на 08.09, включая секреты CareerOps; решение пользователя (D10) |

## 14. Прочее, что работает и должно сохраниться (KEEP)

- CORE: sshd; NetworkManager и мост `br0`; tailscaled с rekick-таймером и drop-in; управление вентиляторами;
  smartmontools, lm-sensors, avahi, timesyncd; Docker и containerd; libvirt (демоны, без VM); journald (2,3 GB);
  административные снимки `/root/infra-backups`, `/root/nm-backup-pre-br0`, `~/infra-snapshots`.
- EDGE: sshd; NetworkManager и профиль `careerops-lan-edge`; NVIDIA-стек (драйвер 595.91.07, persistenced,
  CDI-refresh, Container Toolkit, CUDA 13.2, Nsight); Docker и containerd; PostgreSQL-сервер (S1); text reranker (S2);
  Caddy (пока KEEP YouTrack); tailscale (установлен, выключен); настройки питания ноутбука.
- Локальных DNS, DHCP, мониторинга (Prometheus, Grafana и т. п.) и систем резервного копирования, кроме бэкапов
  YouTrack, нет.

## 15. Рекомендация по `$VKM_DATA_ROOT`

| Вариант | Хост и ФС | Свободно | Носитель | Плюсы | Минусы | Вердикт |
|---|---|---|---|---|---|---|
| A | CORE, каталог на `/` (ext4 на LV `core-vg/root`) | 377 GB | SATA SSD | роль CORE по брифу; рядом Neo4j, OpenSearch, API и MCP; без сетевого хопа | общая ФС с ОС; DRAM-less SATA SSD (достаточно для этого масштаба) | **рекомендуется** |
| B | CORE, отдельный LV из свободного места VG | 24 GB | SATA SSD | изоляция от ОС | мало (нужно 30–120 GB); расширение требует offline-сжатия корневой ext4 | не подходит |
| C | EDGE, каталог на `/` | 366 GB | NVMe | быстрее | EDGE — ноутбук и GPU-узел; смешение ролей; сетевой хоп для Neo4j и OpenSearch | не рекомендуется (на EDGE — только веса моделей реранкеров) |
| D | WORKSTATION | ≈745–840 GB | NVMe | самый быстрый | постоянные DB/search-сервисы на WORKSTATION не держать (постановка §7); не always-on | только рабочая зона OCR (`$VKM_WORK`) с последующей синхронизацией на CORE |

Рекомендуемая раскладка на CORE:

```
/srv/vkm/                       владелец — администратор или отдельный системный пользователь vkm; права 0750
  data/                         = $VKM_DATA_ROOT
    canonical/  artifacts/  duckdb/  neo4j/{data,logs}  opensearch/data  logs/  tmp/
  infra/                        compose-файлы и host-local .env (секреты не в Git)
```

- Данные Neo4j и OpenSearch — bind mount в `$VKM_DATA_ROOT/neo4j` и `$VKM_DATA_ROOT/opensearch`, а не анонимные
  volumes Docker: весь runtime под одним корнем, проще перестройка и резервирование. Внутри контейнеров Neo4j работает
  под uid 7474, OpenSearch — под uid 1000: владельцы каталогов должны совпадать.
- Оценка 30–120 GB — это 8–32 % свободного места. Водяные знаки OpenSearch по умолчанию (85/90/95 % ФС) далеко.
- После удаления VM освобождается ≈4 GiB RAM: остаётся ≈23+ GiB под Neo4j (heap 2–4 GB и page cache 2–4 GB),
  OpenSearch (heap 4 GB), API и MCP.
- Канон восстанавливаем из PRIVATE и пайплайна, но OCR дорогой, поэтому стоит периодически копировать
  `canonical/` и `artifacts/ocr_raw/` на WORKSTATION (решение координатора).

## 16. Порты для новых сервисов (конфликтов нет)

| Сервис | Хост | Порт | Рекомендуемая привязка | Проверено 28.09 |
|---|---|---|---|---|
| Neo4j HTTP / Bolt | CORE | 7474 / 7687 | 127.0.0.1 и LAN-адрес CORE | свободны |
| OpenSearch REST | CORE | 9200 (9600 не публиковать) | 127.0.0.1 (LAN — только если WORKSTATION нужен прямой доступ) | свободны |
| VKM API | CORE | 8000 | 127.0.0.1 и LAN-адрес CORE | свободен (**не 8080** — YouTrack) |
| VKM MCP (HTTP/SSE; для stdio порт не нужен) | CORE | 8765 | 127.0.0.1 и LAN-адрес CORE | свободен |
| Jina text reranker | EDGE | 18082 (существующий) | сейчас 0.0.0.0; сузить до LAN-адреса и 127.0.0.1 — агент F | занят реранкером |
| Jina visual reranker m0 | EDGE | 18083 | 127.0.0.1 и LAN-адрес EDGE | свободен |
| PostgreSQL (control plane) | EDGE | 5432 (существующий) | LAN-адрес EDGE и 127.0.0.1 | занят сервером (KEEP) |

Не использовать: 22, 80, 443, 2019 (EDGE), 8080 и 18080 (CORE), 8333 (EDGE, до удаления SeaweedFS). Neo4j, OpenSearch,
API и MCP не добавлять в Caddy (он публичный). Публикация Docker-портов на конкретный LAN-адрес (как у YouTrack)
не делает их доступными из tailnet.

## 17. Решения, требуемые от координатора и пользователя

| ID | Вопрос | Рекомендация B |
|---|---|---|
| D1 | Выводить ли живую автоматизацию CareerOps на EDGE (таймеры и worker выполняют действия во внешнем сервисе по 3 профилям пользователя)? | Да, по постановке §8, но с явным подтверждением пользователя; сначала пауза таймеров, затем экспорт |
| D2 | YouTrack: оставить или вывести? | **KEEP** (SHARED: другие проекты и пользователи). Если выводить — отдельное подтверждение пользователя; тогда же выключить Caddy, убрать DNS-запись и проброс портов на роутере |
| D3 | VM `k8s-cp01`: удалить? Удалять ли базовый cloud-образ? | Удалить VM; базовый образ — по желанию (публичный, скачивается заново) |
| D4 | Архивный каталог на WORKSTATION и политика секретов | `<ARCHIVE_DRIVE>:\VKM_ARCHIVE\careerops_2026-09-28\` на разделе данных 2-TB NVMe (≈790 GB свободно; не Git, не облако). Секреты (cookies, токены, env с ключами, конфиг профилей) по умолчанию **не архивировать**, а только перечислить их имена; если нужны — отдельный зашифрованный контейнер |
| D5 | SeaweedFS: копия содержимого или только манифест? | Полная копия `careerops-raw` (1,57 GB, сжимается) + манифест + холодный снимок каталога SeaweedFS после остановки: дёшево и полностью восстановимо |
| D6 | Историческое имя CareerOps у SHARED-объектов (контейнеры, роль `careerops_admin`, пути, NM-профили, SSH-ключи) | В v0 не переименовывать; при желании — Phase 1 (PostgreSQL — координатор, реранкер — агент F) |
| D7 | Compose PostgreSQL внутри `~/workspace/careerops/postgres/` | Скопировать в `~/workspace/infra/postgres/` до удаления клона; в окно обслуживания пересоздать контейнер с healthcheck на БД `postgres` (≈5–10 с простоя) |
| D8 | Гигиена безопасности вне задачи: `PasswordAuthentication yes`, реранкер на 0.0.0.0, публичный YouTrack, общий tailnet | Зафиксировать как техдолг; новые сервисы VKM — только localhost/LAN |
| D9 | git-lfs и uv отсутствуют на CORE и EDGE | Ставить только если CORE должен сам читать LFS-сырьё PRIVATE (иначе сырьё обрабатывается на WORKSTATION) |
| D10 | Снимки `~/recovery` (08.09) на обоих хостах, вероятно, содержат копии секретов CareerOps | Решение пользователя; в cleanup не входят |
| D11 | Следы CareerOps на WORKSTATION (C15, C16) | C15 — пользователь после удаления VM; C16 — вне задачи |

## 18. Сырые данные (git-ignored, `work/corpus_platform/phase0/agent_b/raw/`)

| Файл | Содержание |
|---|---|
| `core_01_system.txt`, `edge_01_system.txt` | ОС, CPU, RAM, GPU, lsblk, df, LVM, SMART-модели |
| `core_02_network.txt`, `edge_02_network.txt` | интерфейсы, маршруты, nftables, `ss -ltnup`, tailscale, sshd |
| `core_03*_docker*.txt`, `edge_03*_docker*.txt` | Docker, контейнеры (inspect по полям), образы, volumes, сети |
| `core_04_*.txt`, `edge_04_*.txt` | systemd, таймеры, cron, инструменты, `du` |
| `core_05_custom_units.txt`, `edge_05_careerops_units.txt` | пользовательские юниты, журнал CareerOps-юнитов |
| `core_06_libvirt.txt`, `vm_k8s-cp01*.txt` | libvirt, VM, содержимое VM |
| `core_07*_youtrack*.txt` | YouTrack: данные, бэкапы, использование |
| `edge_07_postgres_server.txt`, `edge_08*_postgres_*.txt` | PostgreSQL: настройки, pg_hba, БД, роли, активность, таблицы с точными числами строк |
| `edge_09_reranker.txt` | реранкер: compose (замаскирован), health, модель, API, тест |
| `edge_10*_seaweedfs.txt` | SeaweedFS: compose, бакеты, префиксы, identities (без ключей) |
| `edge_11_caddy.txt` | Caddy |
| `core_12_*.txt`, `edge_12_*.txt`, `edge_13_*.txt` | поиск следов CareerOps (пути, без содержимого) |
| `core_08_*.txt`, `edge_14_readiness.txt`, `misc_*.txt` | готовность к платформе, порты, проверки |
| `workstation_01_archive_target.txt` | диски WORKSTATION и выбор архивного каталога |
