#!/usr/bin/env bash
# VKM nightly checks on CORE (agent OPS, 29.09.2026; user decisions A3 and B1.5 of 29.09): 04:00 MSK, a short Russian
# summary for 07:00–08:00 MSK.
#
# Steps (stdout → raw/<step>.out, stderr → raw/<step>.err, exit code and seconds → steps.jsonl):
#   containers     docker compose ps: state and health of the vkm-core services (addresses are dropped)
#   disk           free space of the data root
#   canon          snapshot validator on CURRENT (canon validate; --deep on VKM_NIGHTLY_DEEP_WEEKDAY)
#   duckdb         the DuckDB file against CURRENT (duckdb status)
#   graph          DOCUMENT graph checks C1–C16 (graph verify)
#   nav            NAV graph checks N1–N11 (nav graph-verify on derived/navigation/CURRENT)
#   search_status  aliases, builds and the dense vector count (search status)
#   search         Russian BM25 smoke (search smoke)
#   rx580          health of the RX580 retrieval service (served late pack)
#   hybrid         hybrid + late smoke through the VKM API (search hybrid-smoke --late)
#   vectors        units of CURRENT and the packs/CURRENT pointers — units = dense index = late pack
#   mcp            MCP smoke of all read tools (mcp_smoke.py inside the mcp container)
#   dossiers       117 topic dossiers (dossiers.py inside the api container) → derived/dossiers/<snapshot>/
#   topic_v1       TOPIC_BENCHMARK_V1 hybrid_late: harness inside api, scoring in vkm-nightly (topic_score.py)
#   backup         the EDGE backup receipt and the CORE source manifest
# then nightly_summary.py → summary.json + summary.md; receipts/nightly/LATEST (date) and STATUS (one line).
#
# Output: $VKM_DATA_ROOT_HOST/receipts/nightly/<YYYY-MM-DD MSK>/ (a re-run of the same day moves the earlier run to
# <date>-rerun-<HHMMSS>); runs older than VKM_NIGHTLY_KEEP_DAYS are removed. Exit: 0 GREEN/YELLOW, 1 RED, 2 usage,
# 75 another run holds the lock.
# Resources: the systemd unit caps the host processes (CPUQuota, MemoryMax, Nice); container steps run in the
# vkm-nightly compose service (profile nightly: cpus 12, mem_limit, low cpu_shares, data root read-only) or as a single
# client inside api/mcp (one request at a time). The run waits (bounded) while a reconcile or a lab refresh runs.
#
# Options: --dry-run            configuration, services, frozen topic files and the plan; runs nothing, writes nothing
#          --only a,b / --skip a,b  run a subset of the steps
# Environment: VKM_COMPOSE_DIR (default: the parent of this directory: compose.yml + .env), VKM_DATA_ROOT_HOST
#   (default: from .env), VKM_DOCKER, VKM_COMPOSE_PROJECT (default vkm-core), VKM_NIGHTLY_TOPIC_DIR (default
#   topic_v1/ next to this script), VKM_NIGHTLY_DEEP_WEEKDAY (1–7, 0 = never; default 7), VKM_NIGHTLY_DOSSIER_BUDGET
#   (default 12000), VKM_NIGHTLY_KEEP_DAYS (default 60), VKM_NIGHTLY_WAIT_BUSY_S (default 3600), VKM_NIGHTLY_POLL_S
#   (default 60), VKM_NIGHTLY_DOSSIER_KEEP (snapshot dirs of dossiers, default 5), VKM_NIGHTLY_TIMEOUT_<STEP>
#   (seconds, e.g. VKM_NIGHTLY_TIMEOUT_DOSSIERS=5400).
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${VKM_COMPOSE_DIR:-$(dirname "$HERE")}"
DOCKER="${VKM_DOCKER:-docker}"
PROJECT="${VKM_COMPOSE_PROJECT:-vkm-core}"
TOPIC_DIR="${VKM_NIGHTLY_TOPIC_DIR:-$HERE/topic_v1}"
DEEP_WEEKDAY="${VKM_NIGHTLY_DEEP_WEEKDAY:-7}"
BUDGET="${VKM_NIGHTLY_DOSSIER_BUDGET:-12000}"
KEEP_DAYS="${VKM_NIGHTLY_KEEP_DAYS:-60}"
WAIT_BUSY_S="${VKM_NIGHTLY_WAIT_BUSY_S:-3600}"
POLL_S="${VKM_NIGHTLY_POLL_S:-60}"
DOSSIER_KEEP="${VKM_NIGHTLY_DOSSIER_KEEP:-5}"
STEPS=(containers disk canon duckdb graph nav search_status search rx580 hybrid vectors mcp dossiers topic_v1 backup)
declare -A TIMEOUT=([containers]=60 [disk]=30 [canon]=1800 [duckdb]=300 [graph]=1800 [nav]=1800 [search_status]=300
                    [search]=600 [rx580]=60 [hybrid]=300 [vectors]=60 [mcp]=900 [dossiers]=3600 [topic_v1]=2400
                    [backup]=30)
DRY=0 ONLY="" SKIP_STEPS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --only) ONLY="${2:-}"; shift 2 ;;
    --skip) SKIP_STEPS="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,37p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
for c in python3 flock timeout "$DOCKER"; do command -v "$c" >/dev/null || { echo "$c is required on the host" >&2; exit 2; }; done
[[ "$DEEP_WEEKDAY" =~ ^[0-7]$ ]] || { echo "VKM_NIGHTLY_DEEP_WEEKDAY must be 0..7" >&2; exit 2; }
for n in BUDGET KEEP_DAYS WAIT_BUSY_S POLL_S DOSSIER_KEEP; do
  [[ "${!n}" =~ ^[0-9]+$ ]] || { echo "$n must be a non-negative integer" >&2; exit 2; }
done
for s in ${ONLY//,/ } ${SKIP_STEPS//,/ }; do
  [[ " ${STEPS[*]} " == *" $s "* ]] || { echo "unknown step: $s (steps: ${STEPS[*]})" >&2; exit 2; }
done

env_get() {   # KEY from the environment, else from the compose .env
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f "$COMPOSE_DIR/.env" ]; then
    v="$(grep -E "^$1=" "$COMPOSE_DIR/.env" | tail -n1 | cut -d= -f2- || true)"
  fi
  printf '%s' "$v"
}
DATA="$(env_get VKM_DATA_ROOT_HOST)"
[ -n "$DATA" ] && [ -d "$DATA/canonical" ] || { echo "VKM_DATA_ROOT_HOST is not a canonical data root" >&2; exit 2; }
DC=("$DOCKER" compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/compose.yml")
DATE="$(TZ=Europe/Moscow date +%F)"
TAG="$(date -u +%Y%m%dT%H%M%SZ)"
MARK="nightly-$TAG"
BASE="$DATA/receipts/nightly"
RUN="$BASE/$DATE"
RAW="$RUN/raw"
DEEP=0
[ "$DEEP_WEEKDAY" != 0 ] && [ "$(TZ=Europe/Moscow date +%u)" = "$DEEP_WEEKDAY" ] && DEEP=1
[ "$DEEP" = 1 ] && TIMEOUT[canon]=3600
for s in "${STEPS[@]}"; do      # VKM_NIGHTLY_TIMEOUT_<STEP>=seconds overrides a step's timeout
  v="VKM_NIGHTLY_TIMEOUT_${s^^}"
  if [ -n "${!v:-}" ]; then
    [[ "${!v}" =~ ^[1-9][0-9]*$ ]] || { echo "$v must be a number of seconds" >&2; exit 2; }
    TIMEOUT[$s]="${!v}"
  fi
done
readp() { { tr -d '[:space:]' < "$1"; } 2>/dev/null || true; }
CUR="$(readp "$DATA/canonical/CURRENT")"
NAV="$(readp "$DATA/derived/navigation/CURRENT")"
CAT="$(readp "$DATA/derived/catalogues/CURRENT")"
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
want_step() {
  local s="$1"
  if [ -n "$ONLY" ] && [[ ",$ONLY," != *",$s,"* ]]; then return 1; fi
  if [ -n "$SKIP_STEPS" ] && [[ ",$SKIP_STEPS," == *",$s,"* ]]; then return 1; fi
  return 0
}

# ---------------------------------------------------------------- dry run: configuration and plan only
if [ "$DRY" = 1 ]; then
  rc=0
  log "DRY RUN — compose $COMPOSE_DIR (project $PROJECT); data root $DATA; run dir would be $RUN"
  log "CURRENT ${CUR:-?}; NAV ${NAV:-?}; catalogues ${CAT:-?}; deep validation today: $DEEP"
  services="$("${DC[@]}" --profile nightly config --services 2>/dev/null || true)"
  for s in api mcp rx580-retrieval neo4j opensearch vkm-nightly; do
    if grep -qx "$s" <<<"$services"; then log "  compose service $s: defined"
    else log "  compose service $s: MISSING (deploy infra/core/compose.yml with the vkm-nightly service)"; rc=2; fi
  done
  for f in nightly_summary.py dossier_store.py dossiers.py mcp_smoke.py topic_score.py; do
    python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$HERE/$f" \
      && log "  $f: parses" || { log "  $f: SYNTAX ERROR"; rc=2; }
  done
  if [ -f "$TOPIC_DIR/SHA256SUMS" ] && ( cd "$TOPIC_DIR" && sha256sum -c --quiet SHA256SUMS ) && \
     [ -f "$TOPIC_DIR/harness_core.py" ] && [ -f "$TOPIC_DIR/results_v1.json" ]; then
    log "  topic_v1 files: frozen sha256 OK"
  else
    log "  topic_v1 files: missing or changed in $TOPIC_DIR"; rc=2
  fi
  tmp="$(mktemp -d)"
  { printf 'SET_JSONL = r"""\n'; cat "$TOPIC_DIR/topic_set_v1.jsonl" 2>/dev/null; printf '"""\n'; cat "$HERE/dossiers.py"; } \
    > "$tmp/dossiers.py"
  n="$(python3 "$tmp/dossiers.py" --dry-run 2>/dev/null | grep -c '"kind": "plan"' || true)"
  log "  dossiers planned: $n topics (expected 117)"
  [ "$n" = 117 ] || rc=2
  m="$(python3 "$HERE/mcp_smoke.py" --dry-run 2>/dev/null \
       | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["planned_tools"]))' 2>/dev/null || echo 0)"
  log "  MCP smoke plans $m of 49 read tools"
  [ "$m" = 49 ] || rc=2
  rm -rf "$tmp"
  for s in "${STEPS[@]}"; do
    want_step "$s" && log "  step $s (timeout ${TIMEOUT[$s]} s)" || log "  step $s: skipped by options"
  done
  log "  busy now: $(if "$DOCKER" ps -q --filter "label=com.docker.compose.project=$PROJECT" \
        --filter "label=com.docker.compose.service=vkm-job" 2>/dev/null | grep -q .; then echo vkm-job; else echo no; fi)"
  exit "$rc"
fi

mkdir -p "$BASE"
exec 9>"$BASE/.lock"
flock -n 9 || { echo "another nightly run holds $BASE/.lock" >&2; exit 75; }
if [ -d "$RUN" ]; then mv "$RUN" "$BASE/$DATE-rerun-$(TZ=Europe/Moscow date +%H%M%S)"; fi
mkdir -p "$RAW"
exec > >(tee -a "$RUN/run.log") 2>&1
TMPD="$(mktemp -d)"
STARTED="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
CUR_STEP="init" STEP_TIMEOUT=60 SUMMARY_DONE=0

status_line() {
  printf '%s %s RUNNING step=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$DATE" "$CUR_STEP" > "$BASE/STATUS.tmp" \
    && mv -f "$BASE/STATUS.tmp" "$BASE/STATUS"
}
record_step() {   # id rc seconds skipped note
  python3 -c 'import json, sys
a = sys.argv[1:]
print(json.dumps({"step": a[0], "rc": int(a[1]) if a[1] else None, "seconds": int(a[2]), "skipped": a[3] == "1",
                  "note": a[4] or None}, ensure_ascii=False))' "$@" >> "$RUN/steps.jsonl"
}
T() { timeout --kill-after=30 "$STEP_TIMEOUT" "$@"; }
VJ() {        # vkm-corpus command in the vkm-nightly container (data root read-only; capped by compose)
  T "${DC[@]}" --profile nightly run --rm -T --name "vkm-nightly-${CUR_STEP//_/-}-${TAG,,}" vkm-nightly "$@" </dev/null
}
VJ_PY() {     # python program on stdin in the vkm-nightly container
  T "${DC[@]}" --profile nightly run --rm -T --name "vkm-nightly-${CUR_STEP//_/-}-${TAG,,}" --entrypoint python \
    vkm-nightly - "$@"
}
EXEC() {      # command inside a running service; marked so that a timeout can stop it (kill_marked)
  local svc="$1"; shift
  T "${DC[@]}" exec -T -e VKM_NIGHTLY_MARK="$MARK" "$svc" "$@"
}
KILL_MARKED='import os, signal, sys
mark = ("VKM_NIGHTLY_MARK=" + sys.argv[1]).encode()
for p in os.listdir("/proc"):
    if p.isdigit() and p != str(os.getpid()):
        try:
            env = open("/proc/%s/environ" % p, "rb").read().split(b"\0")
        except OSError:
            continue
        if mark in env:
            try:
                os.kill(int(p), signal.SIGTERM)
            except OSError:
                pass'
kill_marked() { "${DC[@]}" exec -T "$1" python -c "$KILL_MARKED" "$MARK" </dev/null >/dev/null 2>&1 || true; }
make_bundle() {   # make_bundle OUT PROGRAM: the frozen topic set as SET_JSONL + the program
  { printf 'SET_JSONL = r"""\n'; cat "$TOPIC_DIR/topic_set_v1.jsonl"; printf '"""\n'; cat "$2"; } > "$1"
}

run_step() {      # run_step ID FUNCTION: stdout → raw/ID.out, stderr → raw/ID.err, a line in steps.jsonl
  local id="$1" fn="$2" rc=0 t0
  if ! want_step "$id"; then record_step "$id" "" 0 1 "not selected"; return 0; fi
  CUR_STEP="$id" STEP_TIMEOUT="${TIMEOUT[$id]}"
  status_line
  t0=$(date +%s)
  "$fn" > "$RAW/$id.out" 2> "$RAW/$id.err" || rc=$?
  if [ "$rc" = 124 ] || [ "$rc" = 137 ]; then
    "$DOCKER" rm -f "vkm-nightly-${id//_/-}-${TAG,,}" >/dev/null 2>&1 || true
    kill_marked api; kill_marked mcp
  fi
  record_step "$id" "$rc" "$(( $(date +%s) - t0 ))" 0 ""
  log "step $id: exit $rc ($(( $(date +%s) - t0 )) s)"
}

finish() {        # summary (also after a crash: missing steps show as not run), LATEST, STATUS, cleanup
  local rc=$?
  set +e
  kill_marked api; kill_marked mcp
  "$DOCKER" ps -aq --filter "name=vkm-nightly-.*-${TAG,,}" 2>/dev/null | xargs -r "$DOCKER" rm -f >/dev/null 2>&1
  rm -rf "$TMPD"
  if [ "$SUMMARY_DONE" = 0 ]; then
    python3 "$HERE/nightly_summary.py" build --run-dir "$RUN" --base "$BASE" > /dev/null 2> "$RAW/summary.err"
  fi
  local overall
  overall="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["overall"])' \
             "$RUN/summary.json" 2>/dev/null || echo NONE)"
  python3 - "$RUN/summary.json" "$DATE" "$BASE" "$rc" <<'PY'
import json, os, sys, datetime
path, date, base, rc = sys.argv[1:5]
try:
    s = json.load(open(path, encoding="utf-8"))
    c = s.get("counts") or {}
    line = (f"{s.get('finished_at')} {date} {s.get('overall')} pass={c.get('PASS')} warn={c.get('WARN')} "
            f"fail={c.get('FAIL')} skip={c.get('SKIP')}\n")
except Exception:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    line = f"{now} {date} NO_SUMMARY exit={rc}\n"
with open(os.path.join(base, "STATUS.tmp"), "w", encoding="utf-8") as fh:
    fh.write(line)
os.replace(os.path.join(base, "STATUS.tmp"), os.path.join(base, "STATUS"))
PY
  if [ -f "$RUN/summary.md" ]; then
    printf '%s\n' "$DATE" > "$BASE/LATEST.tmp" && mv -f "$BASE/LATEST.tmp" "$BASE/LATEST"
  fi
  find "$BASE" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' -mtime +"$KEEP_DAYS" \
    -exec rm -rf {} + 2>/dev/null
  log "finished: overall $overall (exit $rc)"
  if [ "$rc" = 0 ] && [ "$overall" = "RED" ]; then rc=1; fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 143' TERM INT

# ---------------------------------------------------------------- context, busy wait
PREV_DIR="$(python3 "$HERE/nightly_summary.py" prev --base "$BASE" --date "$DATE" --exclude "$RUN")"
busy_reason() {
  local r="" f l
  if "$DOCKER" ps -q --filter "label=com.docker.compose.project=$PROJECT" \
       --filter "label=com.docker.compose.service=vkm-job" 2>/dev/null | grep -q .; then r="vkm-job"; fi
  for l in lab_refresh lab_stage2 lab_stage3; do
    f="$DATA/receipts/$l/.lock"
    if [ -f "$f" ] && ! flock -n "$f" true 2>/dev/null; then r="${r:+$r, }$l"; fi
  done
  [ -e "$DATA/canonical/.lock" ] && r="${r:+$r, }canonical/.lock"
  printf '%s' "$r"
}
BUSY="$(busy_reason)"
deadline=$(( $(date +%s) + WAIT_BUSY_S ))
while [ -n "$BUSY" ] && [ "$(date +%s)" -lt "$deadline" ]; do
  CUR_STEP="wait:$BUSY"; status_line
  log "busy ($BUSY): waiting ${POLL_S} s"
  sleep "$POLL_S"
  BUSY="$(busy_reason)"
done
CUR="$(readp "$DATA/canonical/CURRENT")"
NAV="$(readp "$DATA/derived/navigation/CURRENT")"
CAT="$(readp "$DATA/derived/catalogues/CURRENT")"
python3 - "$RUN/context.json" "$DATE" "$TAG" "$STARTED" "$CUR" "$NAV" "$CAT" "$DEEP" "$BUSY" "${PREV_DIR##*/}" <<'PY'
import json, sys
path, date, tag, started, cur, nav, cat, deep, busy, prev = sys.argv[1:11]
json.dump({"schema": "vkm.nightly_context/1", "date": date, "run_id": "nightly-" + tag, "started_at": started,
           "canonical_current": cur or None, "nav_current": nav or None, "catalogues_current": cat or None,
           "deep": deep == "1", "busy": busy or None, "prev_dir": prev or None},
          open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
PY
log "run $DATE ($TAG): CURRENT ${CUR:-?}, NAV ${NAV:-?}, previous run ${PREV_DIR##*/}; deep=$DEEP; busy=${BUSY:-no}"

# ---------------------------------------------------------------- steps
step_containers() {
  T "${DC[@]}" ps --all --format json | python3 -c 'import json, sys
keep = ("Service", "Name", "State", "Health", "Status", "ExitCode", "Image")
text = sys.stdin.read().strip()
rows = json.loads(text) if text.startswith("[") else [json.loads(x) for x in text.splitlines() if x.strip()]
for r in rows:
    print(json.dumps({k: r.get(k) for k in keep}, ensure_ascii=False))'
}
step_disk() { LC_ALL=C df -B1 --output=size,used,avail,target "$DATA"; }
step_canon() { if [ "$DEEP" = 1 ]; then VJ canon validate --deep; else VJ canon validate; fi; }
step_duckdb() { VJ duckdb status; }
step_graph() { VJ graph verify; }
step_nav() {
  if [ -z "$NAV" ] || [ ! -d "$DATA/derived/navigation/$NAV" ]; then
    printf '{"status": "SKIP", "reason": "no derived/navigation/CURRENT build"}\n'
    return 0
  fi
  VJ nav graph-verify --nav-dir "/data/derived/navigation/$NAV"
}
step_search_status() { VJ search status; }
step_search() { VJ search smoke; }
step_rx580() { EXEC rx580-retrieval curl -fsS --max-time 30 http://127.0.0.1:8790/health </dev/null; }
step_hybrid() { EXEC api vkm-corpus search hybrid-smoke --api-url http://127.0.0.1:8000 --late </dev/null; }
step_vectors() {
  python3 - "$DATA" "$CUR" <<'PY'
import glob, json, os, sys
data, cur = sys.argv[1:3]
def load(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
units = None
for p in sorted(glob.glob(os.path.join(data, "derived", "embeddings", "units", cur, "*", "units.json"))):
    u = load(p)
    if u:
        units = {k: u.get(k) for k in ("count", "snapshot_id", "text_rule", "units_sha256")}
        units["dir"] = os.path.relpath(os.path.dirname(p), data)
        break
packs = []
for p in sorted(glob.glob(os.path.join(data, "derived", "embeddings", "*", "*", "*", "*", "packs", "CURRENT"))):
    c = load(p) or {}
    packs.append({"dir": os.path.relpath(os.path.dirname(p), data),
                  **{k: c.get(k) for k in ("pack_id", "count", "snapshot_id", "published_at")}})
print(json.dumps({"units": units, "packs": packs}, ensure_ascii=False))
PY
}
step_mcp() { EXEC mcp python - --deadline-s "$(( STEP_TIMEOUT - 60 ))" < "$HERE/mcp_smoke.py"; }
step_dossiers() {
  local rc=0 prev=()
  make_bundle "$TMPD/dossiers.py" "$HERE/dossiers.py"
  EXEC api python - --budget "$BUDGET" --deadline-s "$(( STEP_TIMEOUT - 180 ))" < "$TMPD/dossiers.py" \
    > "$RAW/dossiers.jsonl" || rc=$?
  [ -s "$RAW/dossiers.jsonl" ] || return "$(( rc ? rc : 1 ))"
  if [ -n "$PREV_DIR" ] && [ -f "$PREV_DIR/dossiers_index.json" ]; then prev=(--prev-index "$PREV_DIR/dossiers_index.json"); fi
  python3 "$HERE/dossier_store.py" --in "$RAW/dossiers.jsonl" --dossiers-dir "$DATA/derived/dossiers" \
    --snapshot "$CUR" --out-index "$RUN/dossiers_index.json" --run-tag "$TAG" --keep "$DOSSIER_KEEP" "${prev[@]}" \
    || return $?
  gzip -f "$RAW/dossiers.jsonl"          # the answers live in derived/dossiers/<snapshot>/; keep a compressed copy
  return "$rc"
}
step_topic_v1() {
  local rc=0 d
  ( cd "$TOPIC_DIR" && sha256sum -c --quiet SHA256SUMS ) || { echo "frozen topic_v1 files changed (SHA256SUMS)" >&2; return 2; }
  make_bundle "$TMPD/harness.py" "$TOPIC_DIR/harness_core.py"
  EXEC api python - --systems hybrid_late --outlines seen < "$TMPD/harness.py" > "$RAW/topic_v1_runs.jsonl" || rc=$?
  [ "$rc" = 0 ] || return "$rc"
  mkdir -p "$RUN/topic_v1"
  cp "$TOPIC_DIR/topic_set_v1.jsonl" "$TOPIC_DIR/metrics_spec_v1.json" "$TOPIC_DIR/results_v1.json" "$RUN/topic_v1/"
  d="/data/receipts/nightly/$(basename "$RUN")"
  VJ_PY --runs "$d/raw/topic_v1_runs.jsonl" --set "$d/topic_v1/topic_set_v1.jsonl" \
    --spec "$d/topic_v1/metrics_spec_v1.json" --registered "$d/topic_v1/results_v1.json" < "$HERE/topic_score.py"
}
step_backup() {
  python3 - "$DATA" <<'PY'
import json, os, sys
data = sys.argv[1]
def load(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
print(json.dumps({"edge": load(os.path.join(data, "receipts", "backup", "edge", "latest.json")),
                  "source": load(os.path.join(data, "receipts", "backup", "source", "LATEST.json"))},
                 ensure_ascii=False))
PY
}

for s in "${STEPS[@]}"; do
  run_step "$s" "step_$s"
done

# ---------------------------------------------------------------- summary
CUR_STEP="summary"; status_line
python3 "$HERE/nightly_summary.py" build --run-dir "$RUN" --base "$BASE" > "$RAW/summary.out" 2> "$RAW/summary.err"
SUMMARY_DONE=1
cat "$RUN/summary.md"
