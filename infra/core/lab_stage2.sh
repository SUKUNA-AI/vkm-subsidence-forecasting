#!/usr/bin/env bash
# VKM retrieval lab, stage 2 — unattended runner on CORE (постановка лаборатории §63–65; CP-26, CP-37).
#
#   CURRENT snapshot → embedding units (vkm-job: search export-units) → dense encoding of every unit on the RX580
#   (inside rx580-retrieval: embed encode; §46: units already embedded under the same signature are skipped) →
#   §64 checks + versioned k-NN index + alias swap (vkm-job: search build-vectors) → hybrid smoke through the VKM API
#   (3 Russian queries, ≥ 1 hit each, stage trace present) → receipt + one status line.
#
# Idempotent and safe to re-run: every step skips work already done for this snapshot and signature; one run at a
# time (flock). Per run: $VKM_DATA_ROOT_HOST/receipts/lab_stage2/<run>/{run.log,*.json,receipt.json}; the status line
# is receipts/lab_stage2/STATUS and the last receipt receipts/lab_stage2/latest.json.
#
# Start detached (lingering is enabled for the login user, so a user unit survives logout):
#   systemd-run --user --unit vkm-lab-stage2 --collect <compose dir>/lab_stage2.sh [--after-snapshot <CURRENT id>]
#   or: nohup setsid <compose dir>/lab_stage2.sh [...] >/dev/null 2>&1 </dev/null &
# Follow: cat <data root>/receipts/lab_stage2/STATUS ; journalctl --user -u vkm-lab-stage2 -f
#
# Options: --config FILE        lab config (default: lab_stage2.json, else lab_stage2.example.json next to the script)
#          --after-snapshot ID  first wait until canonical/CURRENT differs from ID (final snapshot published later)
#          --dry-run            check the configuration, print the plan, run no container
# Environment: VKM_COMPOSE_DIR (default: the script's directory; compose.yml + .env), VKM_DATA_ROOT_HOST (default:
# from .env), VKM_DOCKER (default: docker), VKM_LAB2_POLL_S (default 60).
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${VKM_COMPOSE_DIR:-$HERE}"
DOCKER="${VKM_DOCKER:-docker}"
POLL_S="${VKM_LAB2_POLL_S:-60}"
CONFIG="" AFTER="" DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --after-snapshot) AFTER="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$CONFIG" ]; then
  CONFIG="$COMPOSE_DIR/lab_stage2.json"
  [ -f "$CONFIG" ] || CONFIG="$HERE/lab_stage2.example.json"
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
OUT="$DATA/receipts/lab_stage2"
RUN_ID="lab2-$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$OUT/$RUN_ID"
mkdir -p "$RUN_DIR"
exec 9>"$OUT/.lock"
flock -n 9 || { echo "another lab_stage2 run holds $OUT/.lock" >&2; exit 75; }
exec > >(tee -a "$RUN_DIR/run.log") 2>&1

SERVICE_CONFIG="$(jget "$CONFIG" service_config)"; SERVICE_CONFIG="${SERVICE_CONFIG:-/config/rx580.json}"
EXPECT_KEY="$(jget "$CONFIG" expect_dense_key)"
EXPECT_QUANT="$(jget "$CONFIG" expect_dense_quant)"
VARIANT="$(jget "$CONFIG" context_variant)"; VARIANT="${VARIANT:-A}"
TEXT_RULE="$(jget "$CONFIG" text_rule)"; TEXT_RULE="${TEXT_RULE:-vkm-units-v1/$VARIANT}"
JOB_SIZE="$(jget "$CONFIG" job_size)"; JOB_SIZE="${JOB_SIZE:-512}"
BATCH="$(jget "$CONFIG" batch)"; BATCH="${BATCH:-8}"
WAIT_S="$(jget "$CONFIG" wait_projections_s)"; WAIT_S="${WAIT_S:-21600}"
WAIT_SNAPSHOT_S="$(jget "$CONFIG" wait_snapshot_s)"; WAIT_SNAPSHOT_S="${WAIT_SNAPSHOT_S:-86400}"
mapfile -t QUERIES < <(python3 -c 'import json,sys; [print(q) for q in json.load(open(sys.argv[1], encoding="utf-8")).get("smoke_queries") or []]' "$CONFIG")
DC=("$DOCKER" compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/compose.yml")
WRITER="rx580-${RUN_ID#lab2-}"
WRITER="${WRITER:0:40}"
STEP="init" SNAP="" UNITS_DIR="" RESULT="FAILED" NOTE=""

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

finish() {    # receipt.json + latest.json + STATUS (also on failure)
  local rc=$?
  set +e
  [ "$RESULT" = "DONE" ] || [ "$RESULT" = "DRY_RUN" ] || RESULT="FAILED"
  python3 - "$RUN_DIR" "$OUT" "$RUN_ID" "$RESULT" "$STEP" "$SNAP" "$CONFIG" "$NOTE" "$rc" <<'PY'
import json, os, sys, datetime
run_dir, out, run_id, result, step, snap, config, note, rc = sys.argv[1:10]
def load(name):
    try:
        with open(os.path.join(run_dir, name), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
units, enc, vec, smoke = load("units.json"), load("encode-dense.json"), load("build-vectors.json"), load("smoke.json")
enc0 = enc[0] if isinstance(enc, list) and enc else {}
receipt = {
    "schema": "vkm.lab_stage2.receipt/1", "run_id": run_id, "status": result, "failed_step": None if result in ("DONE", "DRY_RUN") else step,
    "exit_code": int(rc), "note": note or None, "snapshot_id": snap or None,
    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "config": json.load(open(config, encoding="utf-8")) if os.path.isfile(config) else None,
    "units": units and {k: units.get(k) for k in ("status", "snapshot_id", "count", "by_kind", "text_rule", "units_sha256")},
    "encode": enc0 and {k: enc0.get(k) for k in ("key", "kind", "config_signature", "objects", "plan", "embedded", "seconds",
                                                 "objects_per_s", "queue", "validation")},
    "vectors": vec and {k: vec.get(k) for k in ("status", "build_id", "index", "embeddings", "checks_64", "checks",
                                                "receipt_ref", "timings_s")},
    "smoke": smoke,
    "late_interaction": "NOT_RUN (next stage)",
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
if enc0:
    parts.append(f"embedded={enc0.get('embedded')}")
if vec:
    parts.append(f"vectors={vec.get('status')}:{vec.get('build_id')}")
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
  printf '%s %s RUNNING step=%s snapshot=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RUN_ID" "$STEP" "${SNAP:--}" \
    > "$OUT/STATUS.tmp" && mv -f "$OUT/STATUS.tmp" "$OUT/STATUS"
}

current_snapshot() { tr -d '[:space:]' < "$DATA/canonical/CURRENT"; }

log "run $RUN_ID; config $CONFIG; compose $COMPOSE_DIR; dense $EXPECT_KEY/$EXPECT_QUANT; rule $TEXT_RULE"
if [ "$DRY" = 1 ]; then
  SNAP="$(current_snapshot 2>/dev/null || true)"
  log "DRY RUN — planned commands:"
  log "  ${DC[*]} --profile jobs run --rm -T vkm-job search export-units --variant $VARIANT"
  log "  ${DC[*]} exec -T rx580-retrieval python -m vkm_corpus.embeddings.cli encode --config $SERVICE_CONFIG" \
      "--docs <units dir>/docs.jsonl --data-root /data --roles dense --text-rule $TEXT_RULE --writer $WRITER" \
      "--device RX580 --job-size $JOB_SIZE --batch $BATCH --skip-validate"
  log "  ${DC[*]} --profile jobs run --rm -T vkm-job search build-vectors --embeddings" \
      "/data/receipts/lab_stage2/$RUN_ID/encode-dense.json --snapshot <CURRENT> --skip-if-current"
  log "  ${DC[*]} exec -T api vkm-corpus search hybrid-smoke --api-url http://127.0.0.1:8000 (${#QUERIES[@]} queries)"
  RESULT="DRY_RUN"
  exit 0
fi

# ---------------------------------------------------------------- 0. the snapshot to embed
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

# wait (bounded) until reconcile has projected this snapshot (BM25 aliases): the smoke hydrates through the API
STEP="wait_projections"; status_line
deadline=$(( $(date +%s) + WAIT_S ))
while :; do
  "${DC[@]}" --profile jobs run --rm -T vkm-job search status > "$RUN_DIR/search-status.json" 2>>"$RUN_DIR/run.log" || true
  built="$(jget "$RUN_DIR/search-status.json" aliases.pages.built_from_snapshot_id)"
  [ "$built" = "$SNAP" ] && { log "BM25 projection is on $SNAP"; break; }
  now_snap="$(current_snapshot)"
  if [ "$now_snap" != "$SNAP" ]; then log "CURRENT moved to $now_snap while waiting: adopting it"; SNAP="$now_snap"; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then NOTE="BM25 projection not on $SNAP after ${WAIT_S}s"; log "$NOTE (continuing)"; break; fi
  sleep "$POLL_S"
done

# ---------------------------------------------------------------- 1. units of the snapshot (vkm-job, CANONICAL root)
STEP="units"; status_line
"${DC[@]}" --profile jobs run --rm -T vkm-job search export-units --variant "$VARIANT" > "$RUN_DIR/units.json"
[ "$(jget "$RUN_DIR/units.json" snapshot_id)" = "$SNAP" ] || { NOTE="units export is not of $SNAP"; exit 1; }
UNITS_DIR="$(jget "$RUN_DIR/units.json" dir)"
log "units: $(jget "$RUN_DIR/units.json" count) ($(jget "$RUN_DIR/units.json" status)) in $UNITS_DIR"

# ---------------------------------------------------------------- 2. dense encoding on the RX580 (inside the service)
STEP="encode"; status_line
"${DC[@]}" exec -T rx580-retrieval curl -fsS http://127.0.0.1:8790/health > "$RUN_DIR/rx580-health.json"
dense_key="$(python3 -c 'import json,sys; m=[x for x in json.load(open(sys.argv[1])).get("models",[]) if x.get("role")=="dense"]; print((m[0].get("key") or "")+" "+(m[0].get("quant") or "")) if m else print("")' "$RUN_DIR/rx580-health.json")"
[ -n "$dense_key" ] || { NOTE="the RX580 service has no dense model"; exit 1; }
if [ -n "$EXPECT_KEY" ] && [ "${dense_key%% *}" != "$EXPECT_KEY" ]; then NOTE="dense model is ${dense_key%% *}, expected $EXPECT_KEY"; exit 1; fi
if [ -n "$EXPECT_QUANT" ] && [ "${dense_key##* }" != "$EXPECT_QUANT" ]; then NOTE="dense quant is ${dense_key##* }, expected $EXPECT_QUANT"; exit 1; fi
running="$("${DC[@]}" exec -T rx580-retrieval python -c 'import os
n = 0
for p in os.listdir("/proc"):
    if p.isdigit() and p != str(os.getpid()):
        try:
            c = open(f"/proc/{p}/cmdline", "rb").read().replace(b"\0", b" ")
        except OSError:
            continue
        n += b"vkm_corpus.embeddings.cli encode" in c
print(n)')"
[ "${running//[^0-9]/}" = "0" ] || { NOTE="an encode is already running in rx580-retrieval"; exit 1; }
log "encoding with ${dense_key} (writer $WRITER); already embedded units are skipped"
for attempt in 1 2 3; do     # a transient backend error stops `embed encode`; written parts are kept (§46 resume)
  if "${DC[@]}" exec -T rx580-retrieval python -m vkm_corpus.embeddings.cli encode --config "$SERVICE_CONFIG" \
      --docs "$UNITS_DIR/docs.jsonl" --data-root /data --roles dense --text-rule "$TEXT_RULE" \
      --writer "$WRITER-$attempt" --device RX580 --job-size "$JOB_SIZE" --batch "$BATCH" --skip-validate \
      > "$RUN_DIR/encode-dense.json"; then
    break
  fi
  [ "$attempt" -lt 3 ] || { NOTE="embed encode failed 3 times"; exit 1; }
  log "encode attempt $attempt failed; retrying in 120 s"
  sleep 120
done
log "encoded: $(jget "$RUN_DIR/encode-dense.json" 0.embedded) new, plan $(jget "$RUN_DIR/encode-dense.json" 0.plan)"

# ---------------------------------------------------------------- 3. §64 checks + vectors index + alias swap (vkm-job)
STEP="vectors"; status_line
[ "$(current_snapshot)" = "$SNAP" ] || { NOTE="CURRENT moved away from $SNAP during encoding: re-run"; exit 1; }
"${DC[@]}" --profile jobs run --rm -T vkm-job search build-vectors \
  --embeddings "/data/receipts/lab_stage2/$RUN_ID/encode-dense.json" --snapshot "$SNAP" --skip-if-current \
  > "$RUN_DIR/build-vectors.json"
log "vectors: $(jget "$RUN_DIR/build-vectors.json" status) $(jget "$RUN_DIR/build-vectors.json" build_id)"

# ---------------------------------------------------------------- 4. hybrid smoke through the VKM API
STEP="smoke"; status_line
qargs=()
for q in "${QUERIES[@]}"; do qargs+=(--query "$q"); done
"${DC[@]}" exec -T api vkm-corpus search hybrid-smoke --api-url http://127.0.0.1:8000 "${qargs[@]}" \
  > "$RUN_DIR/smoke.json"
log "smoke: $(jget "$RUN_DIR/smoke.json" status)"

STEP="done"
RESULT="DONE"
