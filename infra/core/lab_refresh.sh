#!/usr/bin/env bash
# VKM retrieval projections refresh to one canonical snapshot — dense + late in one unattended job (agent L).
#
#   0. the snapshot is a parameter: canonical/CURRENT must be it (optionally wait, bounded, until it is); and
#      rx580-retrieval must run the image the one-shot pack worker will run (VKM_RX580_IMAGE) — a pack is always
#      built by the image that serves it (the kind-grouped pack layout is unreadable for images before it);
#   1. lab_stage2.sh — dense (jina-v5-nano on the RX580): units of the snapshot (export-units; EXISTS when present) →
#      encode only new/changed units (§46) → §64 checks + a new versioned k-NN index with every unit of the snapshot
#      (units that left the canon keep their artifact rows as history and are never projected) → alias swap → smoke;
#   2. lab_stage3.sh — late (mLateOn): encode only new/changed units → §64 checks + token pack (float16 memmap,
#      BIB_ENTRY region last) → publish packs/CURRENT → rx580-retrieval picks it up without a restart → late smoke;
#   3. receipt: both stages on the requested snapshot, and the unit count of the snapshot, the dense index count and
#      the late pack count are equal.
#
# Each stage keeps its own lock, receipts and STATUS (receipts/lab_stage2, receipts/lab_stage3); this job adds
# receipts/lab_refresh/<run>/receipt.json and receipts/lab_refresh/STATUS. A failed stage stops the job. The job is
# DONE only when both stages are DONE on the requested snapshot (a stage run against an image without the needed step
# reports ENCODED) and the three counts are equal; otherwise INCOMPLETE or FAILED, with a non-zero exit status.
#
# Start detached:  systemd-run --user --unit vkm-lab-refresh --collect /bin/bash <compose dir>/lab_refresh.sh --snapshot <ID>
# Follow:          cat <data root>/receipts/lab_refresh/STATUS ; journalctl --user -u vkm-lab-refresh -f
# Options:         --snapshot ID   the snapshot to refresh to (required unless --dry-run)
#                  --wait-s N      wait up to N s until canonical/CURRENT is ID (default 0: it must be already)
#                  --dry-run       the plan of both stages and the checks of step 0; nothing runs
# Environment:     as lab_stage2.sh / lab_stage3.sh (VKM_COMPOSE_DIR, VKM_DATA_ROOT_HOST, VKM_DOCKER);
#                  VKM_REFRESH_POLL_S (default 60; snapshot wait).
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${VKM_COMPOSE_DIR:-$HERE}"
DOCKER="${VKM_DOCKER:-docker}"
POLL_S="${VKM_REFRESH_POLL_S:-60}"
DRY=0 WANT="" WAIT_S=0
while [ $# -gt 0 ]; do
  case "$1" in
    --snapshot) WANT="${2:-}"; shift 2 ;;
    --wait-s) WAIT_S="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
command -v python3 >/dev/null || { echo "python3 is required on the host" >&2; exit 2; }
[[ "$WAIT_S" =~ ^[0-9]+$ ]] || { echo "--wait-s needs a number of seconds" >&2; exit 2; }
if [ -n "$WANT" ]; then
  [[ "$WANT" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad snapshot id: $WANT" >&2; exit 2; }
elif [ "$DRY" = 0 ]; then
  echo "--snapshot <ID> is required (the snapshot to refresh to)" >&2; exit 2
fi
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
OUT="$DATA/receipts/lab_refresh"
RUN_ID="refresh-$(date -u +%Y%m%dT%H%M%SZ)"
STARTED="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
mkdir -p "$OUT/$RUN_ID"
exec 9>"$OUT/.lock"
flock -n 9 || { echo "another lab_refresh run holds $OUT/.lock" >&2; exit 75; }
STAGE="check" RESULT="FAILED" NOTE=""

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

finish() {    # receipt.json + STATUS (also on failure); stage receipts older than this job never count
  local rc=$? verdict
  set +e
  python3 - "$DATA" "$OUT" "$RUN_ID" "$RESULT" "$STAGE" "$rc" "$STARTED" "$WANT" "$NOTE" <<'PY'
import datetime, json, os, sys
data, out, run_id, result, stage, rc, started, want, note = sys.argv[1:10]
def load(*parts):
    try:
        with open(os.path.join(*parts), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
def fresh(name):
    r = load(data, "receipts", name, "latest.json") or {}
    return r if (r.get("finished_at") or "") >= started else {}
s2, s3 = fresh("lab_stage2"), fresh("lab_stage3")
bv = load(data, "receipts", "lab_stage2", s2.get("run_id") or "-", "build-vectors.json") or {}
dense = {"COMPLETE": (bv.get("checks") or {}).get("count"),
         "SKIPPED_CURRENT": (bv.get("vectors") or {}).get("count")}.get(bv.get("status"))
counts = {"units_stage2": (s2.get("units") or {}).get("count"), "units_stage3": (s3.get("units") or {}).get("count"),
          "dense_index": dense, "late_pack": (s3.get("pack") or {}).get("count")}
equal = None if None in counts.values() else len(set(counts.values())) == 1
on_snapshot = bool(want) and s2.get("snapshot_id") == want and s3.get("snapshot_id") == want
if result == "DONE" and not (s2.get("status") == "DONE" and s3.get("status") == "DONE" and equal and on_snapshot):
    result = "INCOMPLETE"
    note = note or ("stage receipts are not on the requested snapshot" if not on_snapshot else
                    "counts differ" if equal is False else "a stage did not finish DONE")
receipt = {"schema": "vkm.lab_refresh.receipt/1", "run_id": run_id, "status": result, "started_at": started,
           "snapshot_requested": want or None, "failed_stage": stage if result == "FAILED" else None,
           "exit_code": int(rc), "note": note or None,
           "snapshot_id": s3.get("snapshot_id") or s2.get("snapshot_id"), "counts": counts, "counts_equal": equal,
           "dense_build": {k: bv.get(k) for k in ("status", "build_id", "index")},
           "late_pack": {k: (s3.get("pack") or {}).get(k) for k in ("status", "pack_id", "total_tokens", "bytes")},
           "stage2": {k: s2.get(k) for k in ("run_id", "status", "snapshot_id", "failed_step", "note")},
           "stage3": {k: s3.get(k) for k in ("run_id", "status", "snapshot_id", "failed_step", "note")},
           "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
with open(os.path.join(out, run_id, "receipt.json"), "w", encoding="utf-8") as fh:
    json.dump(receipt, fh, ensure_ascii=False, indent=1, sort_keys=True)
parts = [receipt["finished_at"], run_id, result, f"snapshot={want or '-'}",
         *(f"{k}={v}" for k, v in counts.items()), f"equal={equal}"]
if receipt["failed_stage"]:
    parts.insert(3, f"stage={stage}")
if note:
    parts.append(f"note={note}")
with open(os.path.join(out, "STATUS.tmp"), "w", encoding="utf-8") as fh:
    fh.write(" ".join(parts) + "\n")
os.replace(os.path.join(out, "STATUS.tmp"), os.path.join(out, "STATUS"))
print(" ".join(parts))
sys.exit(0 if result in ("DONE", "DRY_RUN") else 1)
PY
  verdict=$?
  [ "$rc" = 0 ] && rc=$verdict
  exit "$rc"
}
trap finish EXIT

current_snapshot() { tr -d '[:space:]' < "$DATA/canonical/CURRENT" 2>/dev/null || true; }

# ---------------------------------------------------------------- 0. checks: the snapshot and the serving image
want_img="$(env_get VKM_RX580_IMAGE)"
cid="$("${DC[@]}" ps -q rx580-retrieval 2>/dev/null | head -n1 || true)"
svc_img=""
[ -n "$cid" ] && svc_img="$("$DOCKER" inspect --format '{{.Config.Image}}' "$cid" 2>/dev/null || true)"
log "run $RUN_ID; snapshot ${WANT:--} (CURRENT $(current_snapshot)); rx580-retrieval runs ${svc_img:-?}, pack worker" \
    "image ${want_img:-compose default}"
if [ "$DRY" = 1 ]; then
  bash "$HERE/lab_stage2.sh" --dry-run
  bash "$HERE/lab_stage3.sh" --dry-run
  RESULT="DRY_RUN"
  exit 0
fi
[ -n "$cid" ] || { NOTE="rx580-retrieval is not running"; exit 1; }
if [ -z "$want_img" ]; then
  NOTE="VKM_RX580_IMAGE is not set in .env: the pack worker's image is unknown"; exit 1
fi
if [ "$svc_img" != "$want_img" ]; then
  NOTE="rx580-retrieval runs $svc_img, the pack worker would run $want_img: 'up -d --no-deps rx580-retrieval' first"
  exit 1
fi
deadline=$(( $(date +%s) + WAIT_S ))
until [ "$(current_snapshot)" = "$WANT" ]; do
  if [ "$(date +%s)" -ge "$deadline" ]; then NOTE="CURRENT is $(current_snapshot), not $WANT"; exit 1; fi
  sleep "$POLL_S"
done
log "CURRENT is $WANT"

STAGE="stage2"
bash "$HERE/lab_stage2.sh"
[ "$(current_snapshot)" = "$WANT" ] || { NOTE="CURRENT moved away from $WANT during stage 2"; exit 1; }
STAGE="stage3"
bash "$HERE/lab_stage3.sh"
STAGE="done"
RESULT="DONE"
