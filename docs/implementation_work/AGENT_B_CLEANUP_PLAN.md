# AGENT B — план вывода CareerOps (ПРЕДЛОЖЕНИЕ, НЕ ВЫПОЛНЯТЬ до принятия координатором)

Дата: 28.09.2026. Статус: **PROPOSED**. Ни одна команда из этого файла не выполнялась. Основание — инвентарь
`AGENT_B_INFRA_INVENTORY.md` (номера элементов C*, S*, U* и решения D* — оттуда) и постановка §8, §54.

Порядок для каждого элемента: **INVENTORY → EXPORT → VERIFY → STOP → DISABLE → REMOVE**. Переход к STOP разрешён
только после ворот G1 (всё из §6 — PASS) и подписи координатора. REMOVE — только после DISABLE и выдержки
(рекомендуется ≥ 24 ч) с повторными проверками выживания (§10).

Гигиена: хосты — роли; IP, логины, домен и секреты не приводятся. `~` — домашний каталог администратора на сервере.
`<ARCHIVE_DRIVE>` — раздел данных 2-TB NVMe WORKSTATION, `<PG_BIN>` — каталог `bin` локального PostgreSQL 18 на
WORKSTATION, `<EDGE_LAN_IP>` — LAN-адрес EDGE (фактические значения — в сводке агента B координатору и в git-ignored
raw-инвентаре). Значения секретов в ходе работ не печатать; `cat` для env-файлов, `s3.json`, cookies и токенов
не использовать.

## 0. Жёсткие запреты

- `docker system prune`, `docker image prune -a`, `docker volume prune`, `docker network prune` — запрещены.
- Удалять `/etc/careerops`, `/var/lib/careerops`, `~/workspace/careerops*` и `~/.ssh/*careerops*` целиком или по маске
  нельзя: внутри лежат KEEP-объекты (S1, S2, S4). Удаление — только по явным путям из §9.
- Не трогать: контейнеры `careerops-postgres` и `careerops-reranker` (кроме SQL `DROP` в R1 и опционального R1b),
  профили NetworkManager (S3), `authorized_keys` и ключи `id_ed25519_careerops_lan` (S4), YouTrack и Caddy (S5,
  если нет решения D2), пакеты (`apt purge` не делать), Tailscale, драйверы NVIDIA.
- Не перезагружать EDGE между паузой таймеров (§3.0) и их DISABLE (§8): включённые таймеры поднимутся при загрузке
  и изменят данные после экспорта. Если перезагрузка случилась — повторить §3.

## 1. Матрица элементов и действий

| # | Элемент | Класс | INVENTORY | EXPORT | VERIFY | STOP | DISABLE | REMOVE |
|---|---|---|---|---|---|---|---|---|
| C1 | таймеры и сервисы `careerops-hh-*` (EDGE) | CAREEROPS_ONLY | §2 | копии юнитов (§3.3) | V1 | пауза §3.0 | §8 | R3 |
| C2 | compose-проект `careerops-seaweedfs` (EDGE) | CAREEROPS_ONLY | §2 | compose-файлы (§3.3) | V1 | §7 S2 | §8 | R2 |
| C3 | данные SeaweedFS и 3 бакета (EDGE) | CAREEROPS_ONLY | §2 | манифест, объекты, холодный снимок (§3.2, §7) | V5, V6, G1b | вместе с C2 | — | R2 |
| C4 | БД `careerops`, роль `careerops_app` (EDGE) | CAREEROPS_ONLY | §2 | дампы (§3.1) | V2–V4 | пауза §3.0 | — | R1 |
| C5 | `/srv/careerops/app`, `/srv/careerops/backups` (EDGE) | CAREEROPS_ONLY | §2 | git bundle и runtime-архив без секретов (§3.3) | V1, V7 | пауза §3.0 | §8 | R5 |
| C6 | `/etc/careerops/{hh,s3,seaweedfs}` (EDGE) | CAREEROPS_ONLY | §2 | только список имён (секреты) | — | — | — | R5 |
| C7 | `/var/lib/careerops/hh` (EDGE) | CAREEROPS_ONLY | §2 | архив (§3.3) | V1 | пауза §3.0 | — | R5 |
| C8 | образы hh-worker и hh_applicant_tool, сеть `hh-applicant-tool_default` (EDGE) | CAREEROPS_ONLY | §2 | не нужен (пересобираются из кода в bundle) | — | — | — | R4 |
| C9 | `~/careerops-*` (6 объектов), `~/workspace/infra/careerops`, `~/workspace/careerops` (EDGE; **кроме `postgres/`**) | CAREEROPS_ONLY | §2 | архивы (§3.3) | V1 | — | — | R5 |
| C10 | deploy-ключ `~/.ssh/careerops_edge*` и `Host github-careerops` (EDGE) | CAREEROPS_ONLY | §2 | не архивировать (секрет) | — | — | отзыв ключа в GitHub (пользователь) | R6 |
| C11 | VM `k8s-cp01` (CORE) | CAREEROPS_ONLY | §2 | XML домена, cloud-init, мелкие файлы bootstrap (§4) | V1 | §7 S3 | §8 | R7 |
| C12 | `careerops-br0-firewall` (CORE) | CAREEROPS_ONLY | §2 | копии юнита и скрипта (§4) | V1 | — | — | R7 (после C11) |
| C13 | `youtrack-mcp` и `/etc/careerops/youtrack-mcp/env` (CORE) | CAREEROPS_ONLY | §2 | архив кода без `.venv` (§4) | V1 | — | отзыв токена в YouTrack (пользователь) | R7 |
| C14 | `~/k8s-admin.pub` (CORE) | CAREEROPS_ONLY | §2 | — | — | — | — | R7 |
| C15 | SSH-алиас и ключ `k8s-cp01` (WORKSTATION) | CAREEROPS_ONLY | — | — | — | — | — | R8 (пользователь) |
| S1 | PostgreSQL-сервер `careerops-postgres` | SHARED → KEEP | §2 | страховая копия `compose.yml` (без `.env`) | K-проверки | — | — | не удалять; копия compose в нейтральный путь (R5.0), опционально R1b |
| S2 | text reranker `careerops-reranker` и его пути | SHARED → KEEP | §2 | страховые копии compose и env (несекретный), bundle исходников | K1–K3 | — | — | не удалять |
| S3, S4 | профили NM, SSH-ключи CORE↔EDGE | SHARED → KEEP | §2 | — | K5, K-CORE, K-WS | — | — | не удалять |
| S5 | YouTrack и Caddy | SHARED → KEEP | §2 | только при D2 = «вывести» — §11 | — | — | — | только §11 |
| U* | сироты и общие образы (§9, R9), снимки `~/recovery` | UNKNOWN | — | — | — | — | — | только по решению координатора |

## 2. INVENTORY: предусловия и повторная сверка (ворота G0)

Предусловия: приняты решения D1 (вывод живой автоматизации), D3 (VM), D4 (архив и политика секретов), D5 (объём
выгрузки SeaweedFS); координатор назначил исполнителя и дату; есть консольный или Tailscale-доступ к CORE на случай
проблем с сетью.

Сверка с инвентарём непосредственно перед работами (EDGE, `ssh edge`):

```bash
set -u
STAMP=2026-09-28                                   # фактическая дата экспорта
STAGE="$HOME/archive_careerops_$STAMP"             # имя не попадает под маску ~/careerops-*
mkdir -p "$STAGE"/{pg,s3,git,files,receipts,bundles} && chmod 700 "$STAGE"
{
  date -Is
  sudo docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
  systemctl list-timers --all --no-pager | grep careerops
  sudo ss -ltnp | grep -E ':(22|80|443|5432|8333|18082)\b'
  df -h /
  nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
  sudo docker inspect -f '{{.Name}} started={{.State.StartedAt}} restarts={{.RestartCount}}' careerops-reranker careerops-postgres
} | tee "$STAGE/receipts/pre_state_edge.txt"
```

Ожидается: 6 контейнеров (`careerops-reranker`, `careerops-postgres`, 4 × `careerops-seaweedfs-*`), 3 таймера
`careerops-hh-*`, реранкер ≈1,2 GiB VRAM. Любое расхождение с инвентарём — стоп и повторная инвентаризация.
Базовая линия проверок выживания: выполнить §10 и сохранить вывод в `$STAGE/receipts/survival_baseline.txt`.

CORE (`ssh core`):

```bash
set -u
STAMP=2026-09-28
STAGE="$HOME/archive_careerops_$STAMP"
mkdir -p "$STAGE"/{vm,files,receipts,bundles} && chmod 700 "$STAGE"
{ date -Is; sudo virsh list --all; sudo docker ps -a --format '{{.Names}}\t{{.Status}}'; ip -br a; sudo iptables -S DOCKER-USER; } \
  | tee "$STAGE/receipts/pre_state_core.txt"
```

## 3. EXPORT на EDGE

### 3.0 Пауза писателей CareerOps (обратимо)

Пауза нужна, чтобы экспорт был согласованным: таймеры пишут в БД и S3 каждые 5–10 минут. Выполнять вне
запланированных окон worker-а.

```bash
sudo systemctl stop careerops-hh-dispatcher.timer careerops-hh-materializer.timer careerops-hh-planner.timer
for s in dispatcher materializer planner; do
  while systemctl is-active --quiet "careerops-hh-$s.service"; do sleep 5; done
done
while [ -n "$(sudo docker ps -q --filter name=careerops-hh-worker)" ]; do sleep 10; done
systemctl list-timers --all --no-pager | grep careerops-hh | tee "$STAGE/receipts/freeze.txt"   # NEXT = n/a
```

Откат паузы: `sudo systemctl start careerops-hh-dispatcher.timer careerops-hh-materializer.timer careerops-hh-planner.timer`.

### 3.1 PostgreSQL: логический экспорт БД `careerops`

Формат: основной артефакт — custom (`-Fc`, сжатие 6; восстанавливается выборочно через `pg_restore`); дополнительно —
plain SQL schema-only и data-only (читаются без инструментов) и глобальные объекты без хешей паролей. Команды
`docker exec` без `-t`, чтобы вывод не искажался.

```bash
PG="sudo docker exec careerops-postgres"
PGI="sudo docker exec -i careerops-postgres"

{ $PG psql -X -U careerops_admin -d postgres -Atc 'select version()'; $PG pg_dump --version; date -Is; } \
  > "$STAGE/pg/versions.txt"

cat > "$STAGE/pg/count_rows.sql" <<'SQL'
select table_schema || '.' || table_name,
       (xpath('/row/n/text()',
              query_to_xml(format('select count(*) as n from %I.%I', table_schema, table_name), false, true, '')))[1]::text::bigint
from information_schema.tables
where table_schema not in ('pg_catalog', 'information_schema') and table_type = 'BASE TABLE'
order by 1;
SQL
$PGI psql -X -v ON_ERROR_STOP=1 -U careerops_admin -d careerops -At -F $'\t' \
  < "$STAGE/pg/count_rows.sql" > "$STAGE/pg/counts_live.tsv"
wc -l "$STAGE/pg/counts_live.tsv"                                         # ожидается 11

# custom-архив пишется в файл внутри контейнера (seekable → в архиве есть смещения данных), затем копируется наружу
$PG pg_dump -U careerops_admin -d careerops -Fc -Z 6 -f /tmp/careerops.dump
sudo docker cp careerops-postgres:/tmp/careerops.dump "$STAGE/pg/careerops.dump"
$PG rm -f /tmp/careerops.dump
sudo chown "$(id -u):$(id -g)" "$STAGE/pg/careerops.dump"
$PG psql -X -U careerops_admin -d careerops -Atc "select count(*) from pg_indexes where schemaname = 'careerops'" \
  > "$STAGE/pg/index_count_live.txt"                                      # 31 на момент инвентаря
$PG pg_dump -U careerops_admin -d careerops --schema-only         > "$STAGE/pg/careerops_schema.sql"
$PG pg_dump -U careerops_admin -d careerops --data-only           > "$STAGE/pg/careerops_data.sql"
$PG pg_dumpall -U careerops_admin --globals-only --no-role-passwords > "$STAGE/pg/globals_no_passwords.sql"
$PGI pg_restore --list < "$STAGE/pg/careerops.dump"               > "$STAGE/pg/careerops.dump.list"
ls -l "$STAGE/pg"
```

### 3.2 SeaweedFS: манифест бакетов и выгрузка объектов

Манифест без ключей — через `weed shell` внутри master-контейнера (только читающие команды):

```bash
WS() { sudo docker exec careerops-seaweedfs-master-1 sh -c "echo '$1' | weed shell -master=127.0.0.1:9333 -filer=filer:8888" </dev/null; }
{
  WS "s3.bucket.list"
  for b in careerops-raw careerops-lake careerops-artifacts; do WS "fs.du /buckets/$b"; WS "fs.ls -l /buckets/$b"; done
  WS "collection.list"
} > "$STAGE/s3/seaweedfs_manifest.txt" 2>&1
```

Объекты `careerops-raw` — через `aws` CLI с учётными данными read-доступа `careerops-collector` из host-local
env-файла. Значения не выводятся: env-файл читается внутри root-скрипта.

```bash
cat > "$STAGE/receipts/s3_export.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
STAGE="$1"
set -a; . /etc/careerops/s3/collector.env; set +a
export AWS_EC2_METADATA_DISABLED=true
EP="${S3_ENDPOINT:-http://127.0.0.1:8333}"
case "$EP" in http*) ;; *) EP="http://$EP" ;; esac
aws --endpoint-url "$EP" s3api list-objects-v2 --bucket careerops-raw --output json \
  > "$STAGE/s3/careerops-raw.list-objects-v2.json"
aws --endpoint-url "$EP" s3 sync s3://careerops-raw "$STAGE/s3/careerops-raw/" --only-show-errors
EOF
sudo bash "$STAGE/receipts/s3_export.sh" "$STAGE"
sudo chown -R "$(id -u):$(id -g)" "$STAGE/s3"

python3 - "$STAGE" <<'PY' | tee "$STAGE/receipts/s3_counts.txt"
import json, sys, pathlib
stage = pathlib.Path(sys.argv[1])
objs = [o for o in json.load(open(stage / "s3/careerops-raw.list-objects-v2.json")).get("Contents", [])
        if not o["Key"].endswith("/")]
with open(stage / "s3/careerops-raw.manifest.tsv", "w") as f:
    f.write("key\tsize\tetag\tlast_modified\n")
    for o in objs:
        f.write(f"{o['Key']}\t{o['Size']}\t{o['ETag'].strip(chr(34))}\t{o['LastModified']}\n")
local = [p for p in (stage / "s3/careerops-raw").rglob("*") if p.is_file()]
print("objects_listed", len(objs), "bytes_listed", sum(o["Size"] for o in objs))
print("files_local", len(local), "bytes_local", sum(p.stat().st_size for p in local))
PY

( cd "$STAGE/s3" && find careerops-raw -type f -print0 | sort -z | xargs -0 sha256sum > careerops-raw.sha256 )
tar --zstd -cf "$STAGE/bundles/careerops-raw.objects.tar.zst" -C "$STAGE/s3" \
  careerops-raw careerops-raw.list-objects-v2.json careerops-raw.manifest.tsv careerops-raw.sha256 seaweedfs_manifest.txt
```

Бакеты `careerops-lake` и `careerops-artifacts` пусты — для них достаточно манифеста.

### 3.3 Файлы, git и несекретные конфиги

```bash
# git: вся история и все refs, включая stash в /srv/careerops/app
git -C /srv/careerops/app bundle create "$STAGE/git/careerops-app.bundle" --all
git -C "$HOME/workspace/careerops-reranker-src" bundle create "$STAGE/git/careerops-reranker-src.bundle" --all   # S2: источник сборки реранкера

# runtime приложения без .git, .venv и секретов (cookies, токены профилей, .env);
# sudo — часть файлов могла быть создана worker-контейнером от root
sudo tar --zstd -cf "$STAGE/bundles/careerops-app-runtime.tar.zst" -C /srv/careerops \
  --exclude='app/.git' --exclude='app/.venv' --exclude='__pycache__' \
  --exclude='*/cookies.txt' --exclude='*/config.json' --exclude='app/.env' \
  app backups/20260903-154402/postgres.dump backups/20260903-154402/pre-update-working-tree.patch
sudo chown "$(id -u):$(id -g)" "$STAGE/bundles/careerops-app-runtime.tar.zst"

# рабочие клоны CareerOps (с .git и незакоммиченными изменениями), без .env PostgreSQL
tar --zstd -cf "$STAGE/bundles/careerops-workspace.tar.zst" -C "$HOME/workspace" \
  --exclude='careerops/postgres/.env' careerops infra/careerops

# домашние экспорты: ровно 6 объектов по маске
( cd "$HOME" && ls -d careerops-* ) | tee "$STAGE/receipts/home_items.txt"          # ожидается 6 строк
tar --zstd -cf "$STAGE/bundles/careerops-home-exports.tar.zst" -C "$HOME" $(cd "$HOME" && ls -d careerops-*)

# состояние планировщика (сначала список файлов; файлы с секретами исключаются)
sudo find /var/lib/careerops/hh -type f | tee "$STAGE/receipts/state_hh_files.txt"
sudo tar --zstd -cf "$STAGE/bundles/careerops-state-hh.tar.zst" -C /var/lib/careerops \
  --exclude='*cookie*' --exclude='*token*' --exclude='*secret*' hh
sudo chown "$(id -u):$(id -g)" "$STAGE/bundles/careerops-state-hh.tar.zst"

# несекретные конфиги C1/C2 и страховые копии KEEP-конфигов S1/S2
mkdir -p "$STAGE/files"/{systemd,keep_copies}
cp /etc/systemd/system/careerops-hh-*.service /etc/systemd/system/careerops-hh-*.timer "$STAGE/files/systemd/"
cp "$HOME/workspace/careerops/postgres/compose.yml"        "$STAGE/files/keep_copies/postgres.compose.yml"
cp "$HOME/workspace/careerops-reranker-deploy/compose.yml" "$STAGE/files/keep_copies/reranker.compose.yml"
sudo cp /etc/careerops/reranker/env                        "$STAGE/files/keep_copies/reranker.env"   # несекретный: model id, revisions, dtype, port
sudo chown -R "$(id -u):$(id -g)" "$STAGE/files"
tar --zstd -cf "$STAGE/bundles/careerops-configs-nonsecret.tar.zst" -C "$STAGE/files" .

# перечень секретов, которые сознательно НЕ архивируются (только имена)
{
  echo "# not archived on purpose (secrets); names only"
  sudo find /etc/careerops/hh /etc/careerops/s3 /etc/careerops/seaweedfs -type f
  find /srv/careerops/app/hh-applicant-tool/config \( -name cookies.txt -o -name config.json \)
  ls /srv/careerops/backups/*/etc-careerops-hh.tar.gz /srv/careerops/backups/*/hh-profiles.tar.gz
  echo "$HOME/workspace/careerops/postgres/.env (S1, KEEP: copied to ~/workspace/infra/postgres in R5.0)"
  echo "$HOME/.ssh/careerops_edge"
} > "$STAGE/bundles/SECRETS_NOT_ARCHIVED.txt"
```

Если по решению D4 секреты всё же нужны, их упаковывают отдельным зашифрованным контейнером (инструмент шифрования
выбирает координатор) и хранят вне Git и облака.

### 3.4 Сборка комплекта и контрольные суммы

Перед этим шагом выполнить на EDGE проверки V4 (тестовое восстановление) и V5 (полнота S3) из §6 — их результаты
входят в комплект.

```bash
cp "$STAGE"/pg/* "$STAGE"/git/* "$STAGE/bundles/"
cp "$STAGE/receipts/s3_counts.txt" "$STAGE/receipts/home_items.txt" "$STAGE/receipts/v4.txt" "$STAGE/bundles/"
( cd "$STAGE/bundles" && rm -f SHA256SUMS && sha256sum -- * > SHA256SUMS && cat SHA256SUMS && du -sh . )
```

## 4. EXPORT на CORE

```bash
# C13: код youtrack-mcp без .venv (токен в /etc/careerops/youtrack-mcp/env не архивируется)
tar --zstd -cf "$STAGE/bundles/careerops-youtrack-mcp.tar.zst" -C "$HOME/workspace/careerops/services" \
  --exclude='.venv' --exclude='__pycache__' youtrack-mcp

# C11: описание VM
sudo virsh dumpxml k8s-cp01                                     > "$STAGE/vm/k8s-cp01.domain.xml"
sudo qemu-img info -U /var/lib/libvirt/images/k8s-cp01.qcow2   > "$STAGE/vm/k8s-cp01.qemu-img-info.txt"
sudo cp /var/lib/libvirt/cloud-init/k8s-cp01/meta-data /var/lib/libvirt/cloud-init/k8s-cp01/network-config "$STAGE/vm/"
sudo grep -ciE 'passw|chpasswd|token|secret' /var/lib/libvirt/cloud-init/k8s-cp01/user-data \
  && echo "user-data NOT archived (contains secrets)" \
  || sudo cp /var/lib/libvirt/cloud-init/k8s-cp01/user-data "$STAGE/vm/"

# C12: юнит и скрипт правила br0 (для возможного отката)
sudo cp /etc/systemd/system/careerops-br0-firewall.service /usr/local/sbin/careerops-br0-firewall "$STAGE/files/"
sudo chown -R "$(id -u):$(id -g)" "$STAGE/vm" "$STAGE/files"
tar --zstd -cf "$STAGE/bundles/careerops-core-vm-and-units.tar.zst" -C "$STAGE" vm files
( cd "$STAGE/bundles" && rm -f SHA256SUMS && sha256sum -- * > SHA256SUMS && cat SHA256SUMS )
```

Внутри VM (с WORKSTATION, где есть алиас `k8s-cp01`): мелкие файлы bootstrap (values Cilium, списки образов,
SHA256SUMS; без tar-образов, deb-пакетов, бинарников и `cilium-rendered.yaml`, где могут оказаться
сгенерированные TLS-ключи) и состояние кластера:

```bash
ssh k8s-cp01 'cd ~/k8s-bootstrap && find . -type f -size -1M ! -name cilium-rendered.yaml -print0 | tar --null -czf ~/k8s-bootstrap-small.tar.gz -T - && { sudo crictl images; systemctl is-enabled kubelet; systemctl is-active kubelet; ls -la /etc/kubernetes; } > ~/k8s-cp01.state.txt'
```

Диск VM (5,9 GB) не архивируется: кластер не инициализировался, уникальных данных нет (инвентарь §11).

## 5. Перенос на WORKSTATION

PowerShell на WORKSTATION. Каталог — вне Git-репозиториев и облачной синхронизации (решение D4).

```powershell
$STAMP = '2026-09-28'
$A = '<ARCHIVE_DRIVE>:\VKM_ARCHIVE\careerops_' + $STAMP
New-Item -ItemType Directory -Force -Path "$A\edge", "$A\core", "$A\vm" | Out-Null
scp "edge:archive_careerops_$STAMP/bundles/*" "$A\edge\"
scp "core:archive_careerops_$STAMP/bundles/*" "$A\core\"
scp "k8s-cp01:k8s-bootstrap-small.tar.gz" "k8s-cp01:k8s-cp01.state.txt" "$A\vm\"
Get-ChildItem "$A\vm" -File | ForEach-Object { '{0}  {1}' -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name } |
  Set-Content -Encoding ascii "$A\vm\SHA256SUMS"
```

`scp` передаёт двоичные файлы без искажений. Перенаправление `>` в PowerShell 5.1 для двоичных данных
не использовать.

## 6. VERIFY — ворота G1 (всё должно быть PASS)

```powershell
function Test-Sums([string]$dir) {
  $bad = @(); $n = 0
  foreach ($line in Get-Content "$dir\SHA256SUMS") {
    if ($line -notmatch '^([0-9a-f]{64})\s+\*?(.+)$') { continue }
    $n++; $f = Join-Path $dir $Matches[2].Trim(); $h = $Matches[1]
    if (-not (Test-Path $f)) { $bad += "MISSING $f"; continue }
    if ((Get-Item $f).Length -eq 0) { $bad += "EMPTY $f" }
    if ((Get-FileHash $f -Algorithm SHA256).Hash.ToLower() -ne $h) { $bad += "MISMATCH $f" }
  }
  if ($bad.Count) { $bad; "V1 FAIL $dir" } else { "V1 PASS $dir ($n files)" }
}
Test-Sums "$A\edge"; Test-Sums "$A\core"
```

| ID | Проверка | Команда | Критерий PASS |
|---|---|---|---|
| V1 | целостность и непустота | `Test-Sums` (выше) | все хеши совпали, размеры > 0 |
| V2 | дамп читается на WORKSTATION (`pg_restore` 18.4) | блок `$toc` ниже | команда без ошибок |
| V3 | состав дампа | блок `$toc` ниже | `TABLE=11 TABLE_DATA=11 SEQUENCE=5 INDEX+CONSTRAINT=` значение из `index_count_live.txt` (31 на момент инвентаря; FK-ограничения не считаются) |
| V4 | тестовое восстановление (EDGE, одноразовый контейнер без сети) | блок ниже | `diff` пуст: `counts_restored.tsv` = `counts_live.tsv` |
| V5 | полнота S3 | `$STAGE/receipts/s3_counts.txt` | `objects_listed` = `files_local` и `bytes_listed` = `bytes_local`; порядок величин совпадает с `fs.du` из манифеста (≈61 тыс. объектов, ≈1,57 GB на момент инвентаря) |
| V6 | архивы читаются на WORKSTATION | `tar -tf "$A\edge\careerops-raw.objects.tar.zst" \| Measure-Object -Line` и так для каждого `.tar.zst` | без ошибок; число файлов `careerops-raw/` = `files_local` |
| V7 | git bundles | на EDGE: `git -C /srv/careerops/app bundle verify "$STAGE/git/careerops-app.bundle"`; на WORKSTATION: `git bundle list-heads "$A\edge\careerops-app.bundle"` | «is okay»; в списке есть `refs/stash` |
| V8 | квитанция | запись результатов V1–V7, хешей, размеров и счётчиков в receipt (§12) | файл создан |

V2–V3 (WORKSTATION, PowerShell):

```powershell
$toc = & '<PG_BIN>\pg_restore.exe' --list "$A\edge\careerops.dump"
function N([string]$p) { ($toc | Select-String -SimpleMatch $p | Measure-Object).Count }
'TABLE={0} TABLE_DATA={1} SEQUENCE={2} INDEX+CONSTRAINT={3}' -f (N ' TABLE careerops '), (N ' TABLE DATA careerops '),
  (N ' SEQUENCE careerops '), ((N ' INDEX careerops ') + (N ' CONSTRAINT careerops ') - (N ' FK CONSTRAINT careerops '))
Get-Content "$A\edge\index_count_live.txt"
```

V4 — на EDGE (образ `postgres:18.6` уже есть; контейнер без сети, удаляется сам вместе с анонимным volume):

```bash
sudo docker run -d --rm --name careerops-pgverify --network none -e POSTGRES_HOST_AUTH_METHOD=trust postgres:18.6
until sudo docker logs careerops-pgverify 2>&1 | grep -q 'PostgreSQL init process complete'; do sleep 1; done
until sudo docker exec careerops-pgverify pg_isready -U postgres -q; do sleep 1; done
sudo docker exec careerops-pgverify createdb -U postgres careerops_verify
sudo docker cp "$STAGE/pg/careerops.dump" careerops-pgverify:/tmp/careerops.dump
sudo docker exec careerops-pgverify pg_restore -U postgres -d careerops_verify --no-owner --no-privileges --exit-on-error \
  /tmp/careerops.dump
sudo docker exec -i careerops-pgverify psql -X -v ON_ERROR_STOP=1 -U postgres -d careerops_verify -At -F $'\t' \
  < "$STAGE/pg/count_rows.sql" > "$STAGE/pg/counts_restored.tsv"
sudo docker exec careerops-pgverify psql -X -U postgres -d careerops_verify -Atc \
  "select count(*) from pg_indexes where schemaname = 'careerops'" > "$STAGE/pg/index_count_restored.txt"
diff "$STAGE/pg/counts_live.tsv" "$STAGE/pg/counts_restored.tsv" \
  && diff "$STAGE/pg/index_count_live.txt" "$STAGE/pg/index_count_restored.txt" \
  && echo "V4 PASS" | tee "$STAGE/receipts/v4.txt"
sudo docker stop careerops-pgverify
```

Любой FAIL — повторить соответствующий шаг EXPORT; к STOP не переходить.

## 7. STOP (после G1 и подписи координатора)

S1. Таймеры CareerOps уже на паузе (§3.0): проверить `systemctl list-timers --all | grep careerops-hh` (NEXT = n/a).

S2. SeaweedFS (EDGE) — остановка в обратном порядке зависимостей и холодный снимок полного состояния:

```bash
sudo docker stop careerops-seaweedfs-s3-1 careerops-seaweedfs-filer-1 careerops-seaweedfs-volume-1 careerops-seaweedfs-master-1
sudo tar --zstd -cpf "$STAGE/bundles/seaweedfs_cold.tar.zst" -C /srv/careerops seaweedfs
sudo chown "$(id -u):$(id -g)" "$STAGE/bundles/seaweedfs_cold.tar.zst"
( cd "$STAGE/bundles" && sha256sum seaweedfs_cold.tar.zst | tee -a SHA256SUMS )
```

Ворота G1b (WORKSTATION): `scp` файла `seaweedfs_cold.tar.zst` и обновлённого `SHA256SUMS` в `$A\edge\`, затем
`Test-Sums "$A\edge"` и `tar -tf` — PASS до REMOVE (R2).

S3. VM `k8s-cp01` (CORE):

```bash
sudo virsh shutdown k8s-cp01
for i in $(seq 1 60); do [ "$(sudo LC_ALL=C virsh domstate k8s-cp01)" = "shut off" ] && break; sleep 2; done
sudo LC_ALL=C virsh domstate k8s-cp01                  # shut off
```

После STOP — проверки выживания §10 (K1–K5, K-CORE, K-WS).

## 8. DISABLE

```bash
# EDGE
sudo systemctl disable careerops-hh-dispatcher.timer careerops-hh-materializer.timer careerops-hh-planner.timer
sudo docker update --restart=no careerops-seaweedfs-s3-1 careerops-seaweedfs-filer-1 careerops-seaweedfs-volume-1 careerops-seaweedfs-master-1
# CORE
sudo virsh autostart --disable k8s-cp01
```

Выдержка ≥ 24 ч (или по решению координатора), затем снова §10. Откат DISABLE и STOP:
`sudo systemctl enable --now <timers>`, `sudo docker update --restart=unless-stopped <containers>` и
`sudo docker start <containers>` (порядок master → volume → filer → s3), `sudo virsh autostart k8s-cp01` и
`sudo virsh start k8s-cp01`.

## 9. REMOVE (точечно; у каждого шага есть предпроверка и постпроверка)

### R1. БД `careerops` и роль `careerops_app` (EDGE)

```bash
PSQL="sudo docker exec careerops-postgres psql -X -v ON_ERROR_STOP=1 -U careerops_admin -d postgres"
$PSQL -Atc "select count(*) from pg_stat_activity where datname = 'careerops'"      # 0
$PSQL -c "DROP DATABASE careerops;"
$PSQL -Atc "select count(*) from pg_shdepend d join pg_roles r on r.oid = d.refobjid where r.rolname = 'careerops_app'"   # 0
$PSQL -c "DROP ROLE careerops_app;"
$PSQL -Atc "select datname from pg_database order by 1"                              # postgres, template0, template1
$PSQL -Atc "select rolname from pg_roles where rolname !~ '^pg_' order by 1"         # careerops_admin
```

`careerops_admin` — bootstrap-суперпользователь сервера: удалить его нельзя, он остаётся (переименование — D6).

R1b (опционально, решение D7; в окно обслуживания): healthcheck контейнера ссылается на удалённую БД — `pg_isready`
остаётся «healthy», но сервер пишет FATAL в журнал каждые 5 с. Исправление (после R5.0):

```bash
cd "$HOME/workspace/infra/postgres"
sed -i 's/^POSTGRES_DB=.*/POSTGRES_DB=postgres/' .env    # влияет только на healthcheck: каталог данных уже инициализирован
sudo docker compose -p postgres up -d                    # пересоздаёт контейнер, volume сохраняется; простой ≈5–10 с
sudo docker inspect -f '{{.State.Health.Status}}' careerops-postgres   # healthy
```

### R2. SeaweedFS (EDGE; только после G1b)

```bash
sudo docker rm careerops-seaweedfs-s3-1 careerops-seaweedfs-filer-1 careerops-seaweedfs-volume-1 careerops-seaweedfs-master-1
sudo docker network rm careerops-seaweedfs_seaweed
sudo docker volume rm 402ca5e9d3bee369e9f945fb2c5dcaf0b6d235b59a6604ef4648ce4c6244ea97
sudo docker image rm chrislusf/seaweedfs:4.41
sudo rm -rf /srv/careerops/seaweedfs
sudo ss -ltn | grep -c ':8333' || true                   # 0
```

Бакеты `careerops-raw`, `careerops-lake`, `careerops-artifacts` удаляются вместе с данными SeaweedFS; в receipt
записать их имена, счётчики и хеши архивов.

### R3. systemd-юниты CareerOps (EDGE)

```bash
sudo rm /etc/systemd/system/careerops-hh-dispatcher.service  /etc/systemd/system/careerops-hh-dispatcher.timer \
        /etc/systemd/system/careerops-hh-materializer.service /etc/systemd/system/careerops-hh-materializer.timer \
        /etc/systemd/system/careerops-hh-planner.service     /etc/systemd/system/careerops-hh-planner.timer
sudo systemctl daemon-reload && sudo systemctl reset-failed
sudo rm -f /var/lib/systemd/timers/stamp-careerops-hh-planner.timer
systemctl list-unit-files 'careerops-*' --no-legend | wc -l     # 0
```

### R4. Образы и сеть hh (EDGE)

```bash
sudo docker image rm hh-worker-careerops-hh-worker:latest hh_applicant_tool:latest
for i in 88994c412925 83ff0461122d af96bff6e3e2 52c229c1fc62 ebd3d1376ade 03f1b0ab117d 972ed24a7845; do
  p=$(sudo docker image inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$i" 2>/dev/null)
  [ "$p" = "hh-worker" ] && sudo docker image rm "$i" || echo "skip $i (project=$p)"
done
sudo docker network rm hh-applicant-tool_default
sudo docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}'   # остались pytorch, careerops-reranker, postgres, ubuntu, hello-world
```

### R5. Файлы CareerOps (EDGE)

R5.0 — сначала вынести KEEP-конфиг PostgreSQL (S1) из клона CareerOps. Работающий контейнер это не затрагивает;
compose-проект остаётся `postgres`, так как новый каталог тоже называется `postgres`.

```bash
mkdir -p "$HOME/workspace/infra/postgres"
cp -a "$HOME/workspace/careerops/postgres/." "$HOME/workspace/infra/postgres/"
chmod 600 "$HOME/workspace/infra/postgres/.env"
cmp "$HOME/workspace/careerops/postgres/compose.yml" "$HOME/workspace/infra/postgres/compose.yml" && echo "compose copied"
( cd "$HOME/workspace/infra/postgres" && sudo docker compose -p postgres ps )     # careerops-postgres Up (healthy)
```

R5.1 — удаление по allowlist с проверкой, что KEEP-пути на месте:

```bash
sudo rm -rf /srv/careerops/app /srv/careerops/backups
sudo rmdir /srv/careerops                                   # пусто после R2
sudo rm -rf /etc/careerops/hh /etc/careerops/s3 /etc/careerops/seaweedfs
sudo rm -rf /var/lib/careerops/hh
( cd "$HOME" && ls -d careerops-* ) && [ "$(cd "$HOME" && ls -d careerops-* | wc -l)" -eq 6 ] && rm -rf "$HOME"/careerops-*
rm -rf "$HOME/workspace/infra/careerops"
rm -rf "$HOME/workspace/careerops"                          # только после R5.0
# постпроверка KEEP
sudo test -f /etc/careerops/reranker/env           && echo "KEEP reranker env OK"
sudo test -d /var/lib/careerops/reranker-cache     && echo "KEEP reranker cache OK"
test -f "$HOME/workspace/careerops-reranker-deploy/compose.yml" && echo "KEEP reranker compose OK"
test -d "$HOME/workspace/careerops-reranker-src/.git"           && echo "KEEP reranker source OK"
test -f "$HOME/workspace/infra/postgres/.env"                   && echo "KEEP postgres compose OK"
```

### R6. Deploy-ключ GitHub (EDGE)

```bash
cp "$HOME/.ssh/config" "$HOME/.ssh/config.bak.$STAMP"
python3 - <<'PY'
import pathlib, re
p = pathlib.Path.home() / ".ssh" / "config"
blocks = re.split(r"(?m)^(?=Host\s)", p.read_text())
keep = [b for b in blocks if not re.match(r"Host\s+github-careerops\s*$", b.splitlines()[0] if b.strip() else "")]
p.write_text("".join(keep))
PY
grep -n 'github-careerops' "$HOME/.ssh/config" || echo "block removed"
grep -nE '^Host (core-lan|core-ts)' "$HOME/.ssh/config"          # KEEP-алиасы на месте
rm -f "$HOME/.ssh/careerops_edge" "$HOME/.ssh/careerops_edge.pub"
```

Пользователь отзывает этот deploy-ключ в настройках репозитория CareerOps на GitHub.

### R7. CORE: VM, правило br0, youtrack-mcp

```bash
# VM (C11)
sudo virsh undefine k8s-cp01 --nvram
sudo virsh vol-delete --pool images k8s-cp01.qcow2
sudo virsh pool-destroy k8s-cp01 && sudo virsh pool-undefine k8s-cp01
sudo rm -rf /var/lib/libvirt/cloud-init/k8s-cp01
sudo virsh vol-delete --pool images debian-13-genericcloud-amd64.qcow2   # только если D3 = удалить базовый образ
sudo virsh list --all                                                    # пусто

# правило br0 (C12) — только после удаления VM; ExecStop удаляет правило из DOCKER-USER
sudo systemctl disable --now careerops-br0-firewall.service
sudo iptables -S DOCKER-USER | grep -c 'CareerOPS br0' || true          # 0
sudo rm /etc/systemd/system/careerops-br0-firewall.service /usr/local/sbin/careerops-br0-firewall
sudo systemctl daemon-reload

# youtrack-mcp (C13) — после того как пользователь отозвал токен в YouTrack
sudo find /etc/careerops -type f                                         # ожидается только youtrack-mcp/env
rm -rf "$HOME/workspace/careerops"
sudo rm -rf /etc/careerops
rm -f "$HOME/k8s-admin.pub"                                              # C14
```

Затем сразу K-CORE (§10): LAN-адрес на `br0`, маршрут по умолчанию, SSH с WORKSTATION и с EDGE.

### R8. WORKSTATION (пользователь)

Удалить блок `Host k8s-cp01` из `~/.ssh/config` и `~/.ssh/config.txt`, ключ `careerops_k8s_ed25519{,.pub}` и запись
VM в `known_hosts` (`ssh-keygen -R <адрес VM>`).

### R9. UNKNOWN — только по отдельному решению координатора

EDGE: сиротский volume `6611dacd…` (12 KB, 0 ссылок) — перед удалением посмотреть содержимое
(`sudo ls -la /var/lib/docker/volumes/6611dacd*/_data`); образы `hello-world`, `ubuntu:latest`. CORE: `hello-world`,
неиспользуемый `postgres:18.6`, пустой каталог `~/workspace/infra/clickhouse-test`. Снимки `~/recovery` (U1) — решение
пользователя. В общий cleanup CareerOps не входят.

## 10. KEEP и проверки выживания

KEEP: PostgreSQL-сервер (S1), text reranker и его файлы (S2), SSH и ключи CORE↔EDGE (S4), сеть и профили NM (S3),
Tailscale, NVIDIA-стек, Docker, libvirt (демоны), YouTrack и Caddy (S5, пока нет решения D2), управление вентиляторами
CORE, административные снимки.

EDGE (`ssh edge`):

```bash
# K1 — реранкер готов, модель и ревизия прежние
curl -fsS http://127.0.0.1:18082/readyz | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d["runtime"]; assert d["status"]=="ready" and r["model_id"]=="jinaai/jina-reranker-v3.5" and r["model_revision"].startswith("e8a93f33"), d; print("K1 PASS", r["model_revision"][:8], r["dtype_or_quantization"])'
# K2 — реранкер считает (проверено 28.09: HTTP 200, 185 токенов, 1,3 с)
python3 - <<'PY'
import json, urllib.request
rt = json.load(urllib.request.urlopen("http://127.0.0.1:18082/readyz", timeout=5))["runtime"]
body = json.dumps({"query": "surface subsidence above a salt mine",
                   "documents": ["ground surface subsidence over mine workings", "a recipe for pancakes"],
                   "top_n": 2, "token_budget": 4096, "expected_runtime": rt}).encode()
req = urllib.request.Request("http://127.0.0.1:18082/v1/rerank", data=body, headers={"Content-Type": "application/json"})
out = json.loads(urllib.request.urlopen(req, timeout=60).read())
assert out["results"][0]["index"] == 0, out
print("K2 PASS", out.get("usage"))
PY
# K3 — модель не перезагружалась: тот же процесс на GPU, контейнер не перезапускался
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader     # один python ≈1,2 GiB
sudo docker inspect -f '{{.Name}} {{.State.Status}} {{.State.Health.Status}} started={{.State.StartedAt}} restarts={{.RestartCount}}' careerops-reranker careerops-postgres
# K4 — PostgreSQL принимает подключения
sudo docker exec careerops-postgres pg_isready -h 127.0.0.1 -p 5432
sudo docker exec careerops-postgres psql -X -U careerops_admin -d postgres -Atc 'select 1'
# K5 — базовые сервисы и сеть
systemctl is-active ssh docker containerd nvidia-persistenced NetworkManager
ip -br a show eno1
```

PASS: K1–K5 без ошибок; `started=` у `careerops-reranker` и `careerops-postgres` совпадает с базовой линией
(`pre_state_edge.txt`), кроме запланированного пересоздания PostgreSQL в R1b.

K-CORE (`ssh core`):

```bash
systemctl is-active ssh docker libvirtd tailscaled NetworkManager core-fanctl
ip -br a show br0                        # LAN-адрес на br0
ip route show default                    # default via <шлюз> dev br0
tailscale status | head -1
curl -s -o /dev/null -w 'youtrack %{http_code}\n' http://127.0.0.1:18080/     # 200 или 302 (пока YouTrack KEEP)
ssh -o BatchMode=yes edge-lan true && echo "CORE->EDGE ssh PASS"
```

K-WS (WORKSTATION, PowerShell):

```powershell
ssh -o BatchMode=yes core true; if ($?) { 'WS->CORE ssh PASS' }
ssh -o BatchMode=yes edge true; if ($?) { 'WS->EDGE ssh PASS' }
ssh edge "ssh -o BatchMode=yes core-lan true && echo EDGE-to-CORE ssh PASS"
ssh core "timeout 3 bash -c '</dev/tcp/<EDGE_LAN_IP>/5432' && echo PG-LAN PASS"
```

Когда выполнять: базовая линия (§2), после STOP (§7), после DISABLE и выдержки (§8), после каждого шага R1–R7 и
в финале. Любой FAIL — остановить cleanup и откатить последний шаг (§13).

## 11. YouTrack и Caddy — только при решении D2 = «вывести»

Инвентарь не доказал ненужность YouTrack (другие проекты и пользователи), поэтому по умолчанию этот раздел
**не выполняется**. Если пользователь подтвердит вывод:

```bash
# CORE — EXPORT
YT="$HOME/archive_youtrack_$STAMP"; mkdir -p "$YT" && chmod 700 "$YT"
# свежий бэкап можно сделать в UI: Administration → Database Backup → Back up now; иначе — последний ежедневный
sudo cp "$(ls -t /srv/youtrack/backups/*.tar.gz | head -1)" "$YT/"
sudo tar --zstd -cf "$YT/youtrack-conf.tar.zst" -C /srv/youtrack conf      # содержит секреты сервиса — хранить как секрет
cp "$HOME/workspace/infra/youtrack/compose.yml" "$YT/"
sudo chown -R "$(id -u):$(id -g)" "$YT" && ( cd "$YT" && sha256sum -- * > SHA256SUMS )
```

VERIFY: `scp` в `$A\youtrack\`, `Test-Sums`, `tar -tzf` бэкапа без ошибок. STOP: `sudo docker stop youtrack` (CORE),
`sudo systemctl stop caddy` (EDGE). DISABLE: `sudo docker update --restart=no youtrack`, `sudo systemctl disable caddy`,
удалить строку `backup-youtrack-host.sh` из root-crontab CORE (`sudo crontab -l | grep -v backup-youtrack-host.sh |
sudo crontab -`). REMOVE: `sudo docker rm youtrack`, `sudo docker network rm youtrack_default`,
`sudo docker image rm jetbrains/youtrack:2026.1.12024`, `sudo rm -rf /srv/youtrack`,
`sudo rm /usr/local/bin/backup-youtrack-host.sh`, `rm -rf ~/workspace/infra/youtrack`. Пакет Caddy не удалять —
только выключить. Пользователь убирает DNS-запись `<public-domain>`, проброс портов 80/443 на роутере и GitHub-интеграции
YouTrack. Проверки: 80/443 на EDGE закрыты, K1–K5 и K-CORE — PASS.

## 12. Receipt

Координатор фиксирует в `docs/corpus_platform/CAREEROPS_MIGRATION_RECEIPT.md` (без IP, логинов и секретов):
дату и исполнителя; логический путь архива (`<ARCHIVE_DRIVE>:\VKM_ARCHIVE\careerops_<STAMP>\`); список файлов
с размерами и SHA-256 (из `SHA256SUMS`); версии сервера и `pg_dump`; число таблиц и строк по таблицам (`counts_live.tsv`
и `counts_restored.tsv`); счётчики S3 (`s3_counts.txt`); результаты V1–V8 и G1b; перечень удалённых элементов
(C1–C14) с датой каждого шага R*; перечень неархивированных секретов (`SECRETS_NOT_ARCHIVED.txt`, только имена);
результаты финальных K-проверок; открытые действия пользователя (отзыв deploy-ключа GitHub, токена YouTrack, сессий
профилей внешнего сервиса; R8). Критерии постановки §54 сопоставить построчно: inventory сохранён; DB exported;
checksum записан; копия на Windows существует; export проверен; старая БД удалена после verify; SeaweedFS удалён;
старые сервисы CareerOps удалены; text reranker, PostgreSQL-сервер, SSH и сеть живы.

## 13. Риски и откат

| # | Риск | Меры | Откат |
|---|---|---|---|
| R-1 | Удалить KEEP-объект с именем CareerOps (env и кэш реранкера, compose и `.env` PostgreSQL, NM-профили, SSH-ключи) | удаление только по allowlist §9; запрет масок; постпроверки KEEP в R5.1 и R6; страховые копии compose и env реранкера в архиве | восстановить из `careerops-configs-nonsecret.tar.zst` (compose, env реранкера); кэш весов скачивается заново по pinned revision; `.env` PostgreSQL — из копии R5.0 |
| R-2 | Данные CareerOps изменились после экспорта | пауза таймеров §3.0; без перезагрузки до DISABLE; сверка `counts_live.tsv` прямо перед R1 | повторить §3 |
| R-3 | Неполный или битый экспорт | ворота G1 (V1–V8) и G1b; тестовое восстановление V4 | повторить EXPORT; STOP не начинать |
| R-4 | Утечка секретов или персональных данных | секреты не архивируются (перечень имён); архив только на локальном диске WORKSTATION, не в Git и не в облаке; raw-инвентарь git-ignored | удалить лишние файлы из архива; отозвать затронутые учётные данные |
| R-5 | CORE теряет сеть после удаления VM или правила `br0` | правило касается только мостового трафика VM; мост `br0` и NM-профили не трогаются; K-CORE сразу после R7; нужен консольный или Tailscale-доступ | `sudo cp` юнита и скрипта из архива обратно, `sudo systemctl enable --now careerops-br0-firewall.service` |
| R-6 | Остановка CareerOps затрагивает пользователя (действия во внешнем сервисе по 3 профилям) | явное решение D1 до §3.0 | `systemctl start` таймеров (до REMOVE) |
| R-7 | `DROP DATABASE` необратим | только после G1, выдержки и повторной сверки счётчиков | `CREATE DATABASE careerops OWNER careerops_admin;` → `pg_restore -d careerops careerops.dump` (из `$STAGE` или из архива через `docker exec -i`); роль `careerops_app` — из `globals_no_passwords.sql`, пароль задать заново |
| R-8 | Потеря данных SeaweedFS | объектная выгрузка (V5, V6) и холодный снимок (G1b) до R2 | распаковать `seaweedfs_cold.tar.zst` в `/srv/careerops/`, поднять compose-файлы из `careerops-workspace.tar.zst`; либо залить объекты обратно `aws s3 sync` |
| R-9 | Сборка реранкера зависит от исходников CareerOps (GitHub и локальный клон) | клон `careerops-reranker-src` — KEEP; bundle в архиве; репозиторий CareerOps на GitHub не удалять, пока агент F не перенесёт сборку | `git clone` из bundle |
| R-10 | `careerops_admin` нельзя удалить | оставить; переименование — отдельное решение D6 | — |
| R-11 | Остаются действующие сессии и ключи во внешних системах | пользователь отзывает deploy-ключ GitHub, токен YouTrack и сессии профилей внешнего сервиса | — |
| R-12 | FATAL в журнале PostgreSQL каждые 5 с после R1 | R1b в окно обслуживания; в compose нет ротации json-логов — учесть при R1b | — |
| R-13 | Нехватка места | EDGE: 366 GB свободно, комплект ≈3–5 GB; WORKSTATION: ≈790 GB | — |

Итоговый порядок: G0 → §3.0 пауза → EXPORT (§3, §4) → перенос (§5) → G1 → STOP (§7) + G1b → DISABLE (§8) → выдержка →
REMOVE R1–R7 (с K-проверками после каждого шага) → R8 (пользователь) → финальные K-проверки → receipt (§12).
