#!/usr/bin/env bash
# VKM retrieval lab, stage 3 — late interaction (mLateOn MaxSim) for the whole corpus, unattended on CORE (agent L;
# постановка лаборатории §44, §46, §54, §63–65).
#
#   CURRENT snapshot → embedding units (vkm-job: search export-units; EXISTS when stage 2 already exported them) →
#   late encoding of every unit on the RX580 (inside rx580-retrieval: embed encode --roles late, N concurrent clients
#   over disjoint shards of the units; §46: units already encoded under the same signature and text hash are skipped,
#   so a re-run on a new snapshot encodes only new/changed units) → §64 checks + token-vector pack (one-shot
#   rx580-embed-worker: embed pack --publish; float16 memmap + offsets index, CURRENT pointer) → the service picks the
#   new pack up (GET /health: late_store) → hybrid + late smoke through the VKM API → receipt + one status line.
#
# Idempotent and safe to re-run; one run at a time (flock). Steps whose code is not in the deployed images yet are
# detected and skipped with a note (status ENCODED instead of DONE). Per run:
# $VKM_DATA_ROOT_HOST/receipts/lab_stage3/<run>/{run.log,*.json,receipt.json}; the status line is
# receipts/lab_stage3/STATUS (progress while encoding) and the last receipt receipts/lab_stage3/latest.json.
#
# Start detached (lingering is enabled for the login user, so a user unit survives logout):
#   systemd-run --user --unit vkm-lab-stage3 --collect <compose dir>/lab_stage3.sh [--after-snapshot <CURRENT id>]
# Follow: cat <data root>/receipts/lab_stage3/STATUS ; journalctl --user -u vkm-lab-stage3 -f
#
# Options: --config FILE        lab config (default: lab_stage3.json, else lab_stage3.example.json next to the script)
#          --after-snapshot ID  first wait until canonical/CURRENT differs from ID (a new snapshot published later)
#          --skip-encode        only checks + pack + reload + smoke (e.g. after deploying the pack-capable image)
#          --dry-run            check the configuration, print the plan, run no container
# Environment: VKM_COMPOSE_DIR (default: the script's directory; compose.yml + .env), VKM_DATA_ROOT_HOST (default:
# from .env), VKM_DOCKER (default: docker), VKM_LAB3_POLL_S (default 60).
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${VKM_COMPOSE_DIR:-$HERE}"
DOCKER="${VKM_DOCKER:-docker}"
POLL_S="${VKM_LAB3_POLL_S:-60}"
CONFIG="" AFTER="" DRY=0 SKIP_ENCODE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --after-snapshot) AFTER="$2"; shift 2 ;;
    --skip-encode) SKIP_ENCODE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$CONFIG" ]; then
  CONFIG="$COMPOSE_DIR/lab_stage3.json"
  [ -f "$CONFIG" ] || CONFIG="$HERE/lab_stage3.example.json"
fi
command -v python3 >/dev/null || { echo "python3 is required on the host" >&2; exit 2; }
[ -f "$CONFIG" ] || { echo "lab config not found: $CONFIG" >&2; exit 2; }

env_get() {   # KEY from the environment, else from the compose .env
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f "$COMPOSE_DIR/.env" ]; then
    v="$(grep -E "^$1=" "$COMPOSE_DIR/.env" | tail -n1 | cut -d= -f2- || true)"
  fi
  printf '%s' "$v"
}

jget() {      # jget FILE KEY.PATH → value ("" if absent); dicts/lists as JSON
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        v = json.load(fh)
except Exception:
    sys.exit(0)
for k in sys.argv[2].split("."):
    if isinstance(v, list) and k.lstrip("-").isdigit() and -len(v) <= int(k) < len(v):
        v = v[int(k)]
    elif isinstance(v, dict):
        v = v.get(k)
    else:
        v = None
    if v is None:
        sys.exit(0)
print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
PY
}

DATA="$(env_get VKM_DATA_ROOT_HOST)"
[ -n "$DATA" ] && [ -d "$DATA/canonical" ] || { echo "VKM_DATA_ROOT_HOST is not a canonical data root" >&2; exit 2; }
OUT="$DATA/receipts/lab_stage3"
RUN_ID="lab3-$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$OUT/$RUN_ID"
mkdir -p "$RUN_DIR"
exec 9>"$OUT/.lock"
flock -n 9 || { echo "another lab_stage3 run holds $OUT/.lock" >&2; exit 75; }
exec > >(tee -a "$RUN_DIR/run.log") 2>&1

SERVICE_CONFIG="$(jget "$CONFIG" service_config)"; SERVICE_CONFIG="${SERVICE_CONFIG:-/config/rx580.json}"
EXPECT_KEY="$(jget "$CONFIG" expect_late_key)"
EXPECT_QUANT="$(jget "$CONFIG" expect_late_quant)"
VARIANT="$(jget "$CONFIG" context_variant)"; VARIANT="${VARIANT:-A}"
TEXT_RULE="$(jget "$CONFIG" text_rule)"; TEXT_RULE="${TEXT_RULE:-vkm-units-v1/$VARIANT}"
CLIENTS="$(jget "$CONFIG" clients)"; CLIENTS="${CLIENTS:-3}"
JOB_SIZE="$(jget "$CONFIG" job_size)"; JOB_SIZE="${JOB_SIZE:-512}"
BATCH="$(jget "$CONFIG" batch)"; BATCH="${BATCH:-8}"
KEEP_PACKS="$(jget "$CONFIG" keep_packs)"; KEEP_PACKS="${KEEP_PACKS:-2}"
WAIT_RELOAD_S="$(jget "$CONFIG" wait_reload_s)"; WAIT_RELOAD_S="${WAIT_RELOAD_S:-180}"
WAIT_SNAPSHOT_S="$(jget "$CONFIG" wait_snapshot_s)"; WAIT_SNAPSHOT_S="${WAIT_SNAPSHOT_S:-86400}"
LATE_CANDIDATES="$(jget "$CONFIG" smoke_late_candidates)"; LATE_CANDIDATES="${LATE_CANDIDATES:-100}"
mapfile -t QUERIES < <(python3 -c 'import json,sys; [print(q) for q in json.load(open(sys.argv[1], encoding="utf-8")).get("smoke_queries") or []]' "$CONFIG")
[[ "$CLIENTS" =~ ^[1-8]$ ]] || { echo "clients must be 1..8" >&2; exit 2; }
DC=("$DOCKER" compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/compose.yml")
WRITER="rx580L-${RUN_ID#lab3-}"
SHARD_DIR="/cache/lab3/$RUN_ID"          # container path (rx580-retrieval: /cache = the service's writable cache dir)
STEP="init" SNAP="" UNITS_DIR="" ART_DIR="" RESULT="FAILED" NOTE="" PROG_PID=""

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

finish() {    # receipt.json + latest.json + STATUS (also on failure)
  local rc=$?
  set +e
  [ -n "$PROG_PID" ] && kill "$PROG_PID" 2>/dev/null
  [ -n "$SHARD_DIR" ] && [ "$DRY" = 0 ] && "${DC[@]}" exec -T rx580-retrieval rm -rf "$SHARD_DIR" </dev/null >/dev/null 2>&1
  case "$RESULT" in DONE|DRY_RUN|ENCODED) ;; *) RESULT="FAILED" ;; esac
  python3 - "$RUN_DIR" "$OUT" "$RUN_ID" "$RESULT" "$STEP" "$SNAP" "$CONFIG" "$NOTE" "$rc" <<'PY'
import json, os, sys, datetime
run_dir, out, run_id, result, step, snap, config, note, rc = sys.argv[1:10]
def load(name):
    try:
        with open(os.path.join(run_dir, name), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
units, enc, pack, reload_, smoke = (load(n) for n in ("units.json", "encode-late.json", "pack.json", "reload.json",
                                                       "smoke.json"))
receipt = {
    "schema": "vkm.lab_stage3.receipt/1", "run_id": run_id, "status": result,
    "failed_step": None if result in ("DONE", "DRY_RUN", "ENCODED") else step,
    "exit_code": int(rc), "note": note or None, "snapshot_id": snap or None,
    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "config": json.load(open(config, encoding="utf-8")) if os.path.isfile(config) else None,
    "units": units and {k: units.get(k) for k in ("status", "snapshot_id", "count", "by_kind", "text_rule", "units_sha256")},
    "encode": enc,
    "pack": pack and {k: pack.get(k) for k in ("status", "pack_id", "snapshot_id", "config_signature", "model_id", "count",
                                               "total_tokens", "dimension", "dtype", "bytes", "checks_64",
                                               "published", "pruned", "seconds")},
    "reload": reload_,
    "smoke": smoke,
}
with open(os.path.join(run_dir, "receipt.json"), "w", encoding="utf-8") as fh:
    json.dump(receipt, fh, ensure_ascii=False, indent=1, sort_keys=True)
with open(os.path.join(out, "latest.json.tmp"), "w", encoding="utf-8") as fh:
    json.dump(receipt, fh, ensure_ascii=False, indent=1, sort_keys=True)
os.replace(os.path.join(out, "latest.json.tmp"), os.path.join(out, "latest.json"))
parts = [receipt["finished_at"], run_id, result]
if receipt["failed_step"]:
    parts.append(f"step={step}")
parts.append(f"snapshot={snap or '-'}")
if units:
    parts.append(f"units={units.get('count')}")
if enc:
    parts.append(f"late_embedded={enc.get('embedded')}")
if pack:
    parts.append(f"pack={pack.get('status')}:{pack.get('pack_id')}")
if smoke:
    parts.append(f"smoke={smoke.get('status')}")
if note:
    parts.append(f"note={note}")
with open(os.path.join(out, "STATUS.tmp"), "w", encoding="utf-8") as fh:
    fh.write(" ".join(str(p) for p in parts) + "\n")
os.replace(os.path.join(out, "STATUS.tmp"), os.path.join(out, "STATUS"))
PY
  log "finished: $RESULT (step $STEP, exit $rc); receipt $RUN_DIR/receipt.json"
}
trap finish EXIT

status_line() {   # progress for readers of STATUS while the run is active
  printf '%s %s RUNNING step=%s snapshot=%s%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RUN_ID" "$STEP" "${SNAP:--}" \
    "${1:+ $1}" > "$OUT/STATUS.tmp" && mv -f "$OUT/STATUS.tmp" "$OUT/STATUS"
}

late_rows() {     # rows of every writer in the most recently written multivector config directory (progress only)
  python3 - "$DATA/derived/embeddings/multivector" <<'PY'
import glob, json, os, sys
dirs = {}
for m in glob.glob(os.path.join(sys.argv[1], "*", "*", "*", "_manifest-*.json")):
    d = os.path.dirname(m)
    try:
        rows = int(json.load(open(m, encoding="utf-8")).get("rows_total") or 0)
    except Exception:
        continue
    t, n = dirs.get(d, (0.0, 0))
    dirs[d] = (max(t, os.path.getmtime(m)), n + rows)
print(max(dirs.values())[1] if dirs else 0)
PY
}

current_snapshot() { tr -d '[:space:]' < "$DATA/canonical/CURRENT"; }

log "run $RUN_ID; config $CONFIG; compose $COMPOSE_DIR; late $EXPECT_KEY/$EXPECT_QUANT; rule $TEXT_RULE; clients $CLIENTS"
if [ "$DRY" = 1 ]; then
  SNAP="$(current_snapshot 2>/dev/null || true)"
  log "DRY RUN — planned commands:"
  log "  ${DC[*]} --profile jobs run --rm -T vkm-job search export-units --variant $VARIANT"
  log "  ${DC[*]} exec -T rx580-retrieval python -m vkm_corpus.embeddings.cli encode --config $SERVICE_CONFIG" \
      "--docs $SHARD_DIR/shard-<i>.jsonl --data-root /data --roles late --text-rule $TEXT_RULE --writer $WRITER-a<n>s<i>" \
      "--device RX580 --job-size $JOB_SIZE --batch $BATCH --skip-validate   (× $CLIENTS concurrent clients)"
  log "  ${DC[*]} --profile jobs run --rm -T rx580-embed-worker pack --config $SERVICE_CONFIG --data-root /data" \
      "--units <units dir> --text-rule $TEXT_RULE --publish --keep $KEEP_PACKS"
  log "  ${DC[*]} exec -T rx580-retrieval curl -fsS http://127.0.0.1:8790/health   (until late_store = the new pack)"
  log "  ${DC[*]} exec -T api vkm-corpus search hybrid-smoke --api-url http://127.0.0.1:8000 --late (${#QUERIES[@]} queries)"
  RESULT="DRY_RUN"
  SHARD_DIR=""
  exit 0
fi

# ---------------------------------------------------------------- 0. the snapshot to encode
STEP="snapshot"; status_line
if [ -n "$AFTER" ]; then
  log "waiting (≤ ${WAIT_SNAPSHOT_S}s) until canonical/CURRENT differs from $AFTER"
  until_ts=$(( $(date +%s) + WAIT_SNAPSHOT_S ))
  while [ "$(current_snapshot)" = "$AFTER" ]; do
    [ "$(date +%s)" -lt "$until_ts" ] || { NOTE="CURRENT still $AFTER after ${WAIT_SNAPSHOT_S}s"; exit 1; }
    sleep "$POLL_S"
  done
fi
SNAP="$(current_snapshot)"
[[ "$SNAP" =~ ^[A-Za-z0-9._-]+$ ]] || { NOTE="bad CURRENT"; exit 1; }
log "CURRENT snapshot: $SNAP"
status_line

# ---------------------------------------------------------------- 1. units of the snapshot (vkm-job, CANONICAL root)
STEP="units"; status_line
"${DC[@]}" --profile jobs run --rm -T vkm-job search export-units --variant "$VARIANT" > "$RUN_DIR/units.json" </dev/null
[ "$(jget "$RUN_DIR/units.json" snapshot_id)" = "$SNAP" ] || { NOTE="units export is not of $SNAP"; exit 1; }
UNITS_DIR="$(jget "$RUN_DIR/units.json" dir)"
UNITS_COUNT="$(jget "$RUN_DIR/units.json" count)"
log "units: $UNITS_COUNT ($(jget "$RUN_DIR/units.json" status)) in $UNITS_DIR"

# ---------------------------------------------------------------- 2. late encoding on the RX580 (inside the service)
STEP="encode"; status_line
"${DC[@]}" exec -T rx580-retrieval curl -fsS http://127.0.0.1:8790/health > "$RUN_DIR/rx580-health.json" </dev/null
late_key="$(python3 -c 'import json,sys; m=[x for x in json.load(open(sys.argv[1])).get("models",[]) if x.get("role")=="late"]; print((m[0].get("key") or "")+" "+(m[0].get("quant") or "")) if m else print("")' "$RUN_DIR/rx580-health.json")"
[ -n "$late_key" ] || { NOTE="the RX580 service has no late model"; exit 1; }
if [ -n "$EXPECT_KEY" ] && [ "${late_key%% *}" != "$EXPECT_KEY" ]; then NOTE="late model is ${late_key%% *}, expected $EXPECT_KEY"; exit 1; fi
if [ -n "$EXPECT_QUANT" ] && [ "${late_key##* }" != "$EXPECT_QUANT" ]; then NOTE="late quant is ${late_key##* }, expected $EXPECT_QUANT"; exit 1; fi
if [ "$SKIP_ENCODE" = 1 ]; then
  log "encode skipped (--skip-encode)"
else
  running="$("${DC[@]}" exec -T rx580-retrieval python -c 'import os
n = 0
for p in os.listdir("/proc"):
    if p.isdigit() and p != str(os.getpid()):
        try:
            c = open(f"/proc/{p}/cmdline", "rb").read().replace(b"\0", b" ")
        except OSError:
            continue
        n += b"vkm_corpus.embeddings.cli encode" in c and b"--roles late" in c
print(n)' </dev/null)"
  [ "${running//[^0-9]/}" = "0" ] || { NOTE="a late encode is already running in rx580-retrieval"; exit 1; }
  # disjoint shards of the units (line i → shard i mod N) in the service's cache dir; removed at the end of the run
  "${DC[@]}" exec -T rx580-retrieval python -c 'import os, sys
src, out, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.makedirs(out, mode=0o700, exist_ok=True)
fhs = [open(os.path.join(out, f"shard-{i}.jsonl"), "w", encoding="utf-8") for i in range(n)]
k = 0
with open(src, encoding="utf-8") as fh:
    for line in fh:
        if line.strip():
            fhs[k % n].write(line)
            k += 1
for f in fhs:
    f.close()
print(k)' "$UNITS_DIR/docs.jsonl" "$SHARD_DIR" "$CLIENTS" > "$RUN_DIR/shards.txt" </dev/null
  log "encoding with ${late_key} in $CLIENTS clients over $(tr -d '[:space:]' < "$RUN_DIR/shards.txt") units (writer $WRITER); already encoded units are skipped"
  ( while sleep "$POLL_S"; do status_line "late_rows=$(late_rows)/$UNITS_COUNT"; done ) &
  PROG_PID=$!
  t_enc=$(date +%s)
  ok=0
  for attempt in 1 2 3; do   # a transient backend error stops a client; written parts are kept (§46 resume)
    pids=()
    for i in $(seq 0 $((CLIENTS - 1))); do
      "${DC[@]}" exec -T rx580-retrieval python -m vkm_corpus.embeddings.cli encode --config "$SERVICE_CONFIG" \
        --docs "$SHARD_DIR/shard-$i.jsonl" --data-root /data --roles late --text-rule "$TEXT_RULE" \
        --writer "$WRITER-a${attempt}s$i" --device RX580 --job-size "$JOB_SIZE" --batch "$BATCH" --skip-validate \
        > "$RUN_DIR/encode-late-a$attempt-s$i.json" </dev/null &
      pids+=($!)
    done
    failed=0
    for p in "${pids[@]}"; do wait "$p" || failed=$((failed + 1)); done
    if [ "$failed" = 0 ]; then ok=1; break; fi
    [ "$attempt" -lt 3 ] || break
    log "encode attempt $attempt: $failed client(s) failed; retrying in 120 s"
    sleep 120
  done
  kill "$PROG_PID" 2>/dev/null || true; PROG_PID=""
  python3 - "$RUN_DIR" "$(( $(date +%s) - t_enc ))" "$CLIENTS" > "$RUN_DIR/encode-late.json" <<'PY'
import glob, json, os, sys
run_dir, wall, clients = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
reports = []
for f in sorted(glob.glob(os.path.join(run_dir, "encode-late-a*-s*.json"))):
    try:
        reports += [dict(r, file=os.path.basename(f)) for r in json.load(open(f, encoding="utf-8"))]
    except Exception:
        pass
late = [r for r in reports if r.get("role") == "late"]
dirs = sorted({r.get("dir") for r in late if r.get("dir")})
emb = sum(int(r.get("embedded") or 0) for r in late)
first = {}
for r in late:                      # the first attempt of each shard carries the full plan of that shard
    first.setdefault(r["file"].split("-s")[-1], r)
plan = {}
for r in first.values():
    for k, v in (r.get("plan") or {}).items():
        if isinstance(v, int):
            plan[k] = plan.get(k, 0) + v
print(json.dumps({"key": late[0].get("key") if late else None, "kind": "multivector", "clients": clients,
                  "config_signature": late[0].get("config_signature") if late else None,
                  "dir": dirs[0] if len(dirs) == 1 else None, "dirs": len(dirs), "embedded": emb, "plan": plan,
                  "wall_seconds": wall, "objects_per_s": round(emb / wall, 1) if emb and wall else None,
                  "reports": len(late)}, ensure_ascii=False, indent=1))
PY
  [ "$ok" = 1 ] || { NOTE="late encode failed 3 times"; exit 1; }
  log "encoded: $(jget "$RUN_DIR/encode-late.json" embedded) new in $(jget "$RUN_DIR/encode-late.json" wall_seconds) s"
fi

# ---------------------------------------------------------------- 3. §64 checks + token-vector pack (one-shot worker)
STEP="pack"; status_line
if ! "${DC[@]}" --profile jobs run --rm -T rx580-embed-worker pack --help >/dev/null 2>&1 </dev/null; then
  NOTE="the deployed rx580 image has no 'embed pack' yet: late artifacts encoded, pack/reload/smoke not run"
  log "$NOTE"
  RESULT="ENCODED"
  exit 0
fi
"${DC[@]}" --profile jobs run --rm -T rx580-embed-worker pack --config "$SERVICE_CONFIG" --data-root /data \
  --units "$UNITS_DIR" --text-rule "$TEXT_RULE" --publish --keep "$KEEP_PACKS" > "$RUN_DIR/pack.json" </dev/null
PACK_ID="$(jget "$RUN_DIR/pack.json" pack_id)"
log "pack: $(jget "$RUN_DIR/pack.json" status) $PACK_ID ($(jget "$RUN_DIR/pack.json" count) units, $(jget "$RUN_DIR/pack.json" total_tokens) tokens)"
[ -n "$PACK_ID" ] || { NOTE="pack step returned no pack id"; exit 1; }

# ---------------------------------------------------------------- 4. the service picks the pack up (hot reload)
STEP="reload"; status_line
deadline=$(( $(date +%s) + WAIT_RELOAD_S ))
while :; do
  "${DC[@]}" exec -T rx580-retrieval curl -fsS http://127.0.0.1:8790/health > "$RUN_DIR/rx580-health-after.json" </dev/null || true
  got="$(jget "$RUN_DIR/rx580-health-after.json" late_store.pack_id)"
  if [ "$got" = "$PACK_ID" ]; then
    python3 -c 'import json,sys; h=json.load(open(sys.argv[1])); print(json.dumps({"status": "ok", "late_store": h.get("late_store")}))' \
      "$RUN_DIR/rx580-health-after.json" > "$RUN_DIR/reload.json"
    log "rx580-retrieval serves pack $PACK_ID"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    NOTE="rx580-retrieval does not serve pack $PACK_ID after ${WAIT_RELOAD_S}s (search.multivector_dir of rx580.json?)"
    exit 1
  fi
  sleep 10
done

# ---------------------------------------------------------------- 5. hybrid + late smoke through the VKM API
STEP="smoke"; status_line
if ! "${DC[@]}" exec -T api vkm-corpus search hybrid-smoke --help 2>/dev/null </dev/null | grep -q -- '--late'; then
  NOTE="the deployed API image has no late stage yet: smoke not run"
  log "$NOTE"
  RESULT="ENCODED"
  exit 0
fi
qargs=()
for q in "${QUERIES[@]}"; do qargs+=(--query "$q"); done
"${DC[@]}" exec -T api vkm-corpus search hybrid-smoke --api-url http://127.0.0.1:8000 --late \
  --late-candidates "$LATE_CANDIDATES" "${qargs[@]}" > "$RUN_DIR/smoke.json" </dev/null
log "smoke: $(jget "$RUN_DIR/smoke.json" status)"

STEP="done"
RESULT="DONE"
