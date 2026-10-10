#!/usr/bin/env bash
# VKM nightly backup of the CORE data root to EDGE (agent OPS, 29.09.2026; user decision A2 of 29.09).
#
# CORE is the only canonical copy. EDGE pulls it (read-only for EDGE on CORE; CORE never writes on EDGE):
#   1. source manifest — receipts/backup/source/LATEST.json and the manifest it names (built on CORE at 02:00 by
#      infra/core/nightly/backup_prepare.sh; must be fresh; its sha256 is checked);
#   2. plan — rsync file list = the manifest's files; bytes to transfer against the previous snapshot; retention and
#      the free-space guard make room first;
#   3. copy — rsync --files-from into snapshots/<YYYY-MM-DD>.partial with --link-dest=<previous snapshot>: unchanged
#      files are hard links (the data root is immutable, so a night costs only its new files); on the re-hash weekday
#      also --checksum, so a damaged file of the store is copied again instead of linked;
#   4. snapshot manifest — sha256 of every copied file (hashes of hard-linked files reused from the previous snapshot;
#      all re-read on the re-hash weekday: bit rot of the store shows up as cache conflicts);
#   5. verification — source manifest vs snapshot manifest (vkm_manifest.py compare): PASS / WARN (declared mutable
#      pointers and receipts only) / FAIL (immutable byte/set mismatch, regardless of mtime, or cache conflicts);
#   6. promote — PASS/WARN: rename to snapshots/<name>, move snapshots/latest; FAIL: keep as <name>.failed;
#   7. retention — newest per day (7), ISO week (4), month (6), at least 3, never latest; the free-space guard deletes
#      the oldest beyond that while free space < VKM_BACKUP_MIN_FREE_GB;
#   8. receipt — status/latest.json, status/STATUS, status/history/<run>.json, <snapshot>/.vkm_backup/receipt.json;
#      pushed to CORE receipts/backup/edge/ (through the same restricted key) for the 04:00 checks.
#
# Timer: vkm-backup.timer (02:30 MSK). One run at a time (flock). Log: <root>/logs/<run>.log.
# Options: --dry-run  steps 1–2 plus rsync --dry-run (what would be copied), the retention plan; no snapshot, no
#                     deletion, no push (work files only under <root>/work/)
#          --rehash   force step 3/4 re-hash mode (default: on VKM_BACKUP_REHASH_WEEKDAY)
# Environment (~/.config/vkm/backup.env, see backup.env.example): VKM_BACKUP_ROOT (required), VKM_BACKUP_SOURCE
#   (default vkm-core-backup:/), VKM_BACKUP_STATUS_DEST (default = source), VKM_BACKUP_SSH_CMD, VKM_BACKUP_KEEP_DAILY
#   / _WEEKLY / _MONTHLY / _MIN, VKM_BACKUP_MIN_FREE_GB, VKM_BACKUP_BUDGET_GB, VKM_BACKUP_MANIFEST_MAX_AGE_H,
#   VKM_BACKUP_WAIT_MANIFEST_S, VKM_BACKUP_POLL_S, VKM_BACKUP_REHASH_WEEKDAY (1–7, 0 = never), VKM_BACKUP_JOBS,
#   VKM_BACKUP_BWLIMIT (KiB/s), VKM_BACKUP_DU_TIMEOUT_S, VKM_MANIFEST_TOOL.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${VKM_BACKUP_ENV:-$HOME/.config/vkm/backup.env}"
if [ -z "${VKM_BACKUP_ROOT:-}" ] && [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi
DRY=0 FORCE_REHASH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --rehash) FORCE_REHASH=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

ROOT="${VKM_BACKUP_ROOT:-}"
SRC="${VKM_BACKUP_SOURCE:-vkm-core-backup:/}"
case "$SRC" in */) ;; *) SRC="$SRC/" ;; esac
STATUS_DEST="${VKM_BACKUP_STATUS_DEST:-$SRC}"
SSH_CMD="${VKM_BACKUP_SSH_CMD:-ssh -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=30}"
KEEP_DAILY="${VKM_BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${VKM_BACKUP_KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${VKM_BACKUP_KEEP_MONTHLY:-6}"
KEEP_MIN="${VKM_BACKUP_KEEP_MIN:-3}"
MIN_FREE_GB="${VKM_BACKUP_MIN_FREE_GB:-100}"
BUDGET_GB="${VKM_BACKUP_BUDGET_GB:-180}"
MAX_AGE_H="${VKM_BACKUP_MANIFEST_MAX_AGE_H:-20}"
WAIT_S="${VKM_BACKUP_WAIT_MANIFEST_S:-2700}"
POLL_S="${VKM_BACKUP_POLL_S:-60}"
REHASH_WEEKDAY="${VKM_BACKUP_REHASH_WEEKDAY:-7}"
JOBS="${VKM_BACKUP_JOBS:-2}"
BWLIMIT="${VKM_BACKUP_BWLIMIT:-}"
DU_TIMEOUT_S="${VKM_BACKUP_DU_TIMEOUT_S:-900}"
TOOL="${VKM_MANIFEST_TOOL:-$HERE/vkm_manifest.py}"

[ -n "$ROOT" ] || { echo "VKM_BACKUP_ROOT is not set (see backup.env.example)" >&2; exit 2; }
case "$ROOT" in /*) ;; *) echo "VKM_BACKUP_ROOT must be an absolute path" >&2; exit 2 ;; esac
for c in python3 rsync flock; do command -v "$c" >/dev/null || { echo "$c is required on the host" >&2; exit 2; }; done
[ -f "$TOOL" ] || { echo "manifest tool not found: $TOOL" >&2; exit 2; }
for n in KEEP_DAILY KEEP_WEEKLY KEEP_MONTHLY KEEP_MIN MIN_FREE_GB BUDGET_GB MAX_AGE_H WAIT_S POLL_S JOBS DU_TIMEOUT_S; do
  [[ "${!n}" =~ ^[0-9]+$ ]] || { echo "$n must be a non-negative integer" >&2; exit 2; }
done
[[ "$REHASH_WEEKDAY" =~ ^[0-7]$ ]] || { echo "VKM_BACKUP_REHASH_WEEKDAY must be 0..7" >&2; exit 2; }
[ -z "$BWLIMIT" ] || [[ "$BWLIMIT" =~ ^[0-9]+$ ]] || { echo "VKM_BACKUP_BWLIMIT must be KiB/s" >&2; exit 2; }
[ "$JOBS" -ge 1 ] || JOBS=1

RUN_ID="bk-$(date -u +%Y%m%dT%H%M%SZ)"
STARTED="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
SNAPS="$ROOT/snapshots"
WORK="$ROOT/work/$RUN_ID"
mkdir -p "$WORK"
exec 9>"$ROOT/.lock"
flock -n 9 || { echo "another vkm_backup run holds $ROOT/.lock" >&2; exit 75; }
if [ "$DRY" = 0 ]; then
  mkdir -p "$SNAPS" "$ROOT/status/history" "$ROOT/logs"
  exec > >(tee -a "$ROOT/logs/$RUN_ID.log") 2>&1
fi
REHASH=0
[ "$FORCE_REHASH" = 1 ] && REHASH=1
[ "$REHASH_WEEKDAY" != 0 ] && [ "$(TZ=Europe/Moscow date +%u)" = "$REHASH_WEEKDAY" ] && REHASH=1
NAME="$(TZ=Europe/Moscow date +%F)"
[ -e "$SNAPS/$NAME" ] && NAME="$(TZ=Europe/Moscow date +%Y-%m-%dT%H%M)"
if [ -e "$SNAPS/$NAME" ]; then
  echo "a snapshot named $NAME exists already (two runs in one minute?)" >&2
  exit 75
fi
SNAP="$SNAPS/$NAME.partial"
PREV=""
if [ -L "$SNAPS/latest" ] && [ -d "$SNAPS/latest/" ]; then
  PREV="$(basename "$(readlink "$SNAPS/latest")")"
fi
PREV_MAN=""
[ -n "$PREV" ] && [ -f "$SNAPS/$PREV/.vkm_backup/target_manifest.jsonl.gz" ] && \
  PREV_MAN="$SNAPS/$PREV/.vkm_backup/target_manifest.jsonl.gz"
STEP="init" RESULT="FAILED" VERDICT="" NOTE="" FINAL="" RSYNC_RC="" T_COPY=""

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
jget() {      # jget FILE KEY.PATH → value ("" if absent)
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        v = json.load(fh)
except Exception:
    sys.exit(0)
for k in sys.argv[2].split("."):
    v = v.get(k) if isinstance(v, dict) else None
    if v is None:
        sys.exit(0)
print(v if isinstance(v, str) else json.dumps(v))
PY
}

finish() {    # receipt + STATUS (also on failure) and the push to CORE
  local rc=$?
  set +e
  [ "$RESULT" = "DONE" ] || [ "$RESULT" = "DRY_RUN" ] || RESULT="FAILED"
  local fs_size fs_free store=""
  read -r fs_size fs_free < <(df -B1 --output=size,avail "$ROOT" 2>/dev/null | tail -n 1)
  if [ "$DRY" = 0 ] && [ -d "$SNAPS" ]; then
    store="$(timeout "$DU_TIMEOUT_S" nice -n 19 du -sb "$SNAPS" 2>/dev/null | cut -f1)"
  fi
  python3 - "$ROOT" "$WORK" "$RUN_ID" "$RESULT" "$STEP" "$VERDICT" "$NOTE" "$rc" "$STARTED" "$NAME" "$FINAL" \
      "$PREV" "${RSYNC_RC:-}" "${T_COPY:-}" "$REHASH" "${fs_size:-}" "${fs_free:-}" "$store" "$BUDGET_GB" "$DRY" <<'PY'
import datetime, json, os, re, sys
(root, work, run_id, result, step, verdict, note, rc, started, name, final, prev, rsync_rc, t_copy, rehash, fs_size,
 fs_free, store, budget_gb, dry) = sys.argv[1:21]
def load(*p):
    try:
        with open(os.path.join(*p), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
def num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None
latest, est, cmp_, tgt, prune = (load(work, n) for n in ("LATEST.json", "estimate.json", "compare.json",
                                                            "target_build.json", "prune.json"))
stats = {}
try:
    text = open(os.path.join(work, "rsync.out"), encoding="utf-8", errors="replace").read()
    for key, pat in (("files_transferred", r"Number of regular files transferred: ([\d,]+)"),
                     ("bytes_transferred", r"Total transferred file size: ([\d,]+)"),
                     ("files_listed", r"Number of files: ([\d,]+)")):
        m = re.search(pat, text)
        if m:
            stats[key] = int(m.group(1).replace(",", ""))
except OSError:
    pass
now = datetime.datetime.now(datetime.timezone.utc)
age_h = None
if latest and latest.get("created_at"):
    try:
        age_h = round((now - datetime.datetime.fromisoformat(latest["created_at"])).total_seconds() / 3600, 2)
    except ValueError:
        pass
snaps_dir = os.path.join(root, "snapshots")
names = sorted(n for n in os.listdir(snaps_dir) if os.path.isdir(os.path.join(snaps_dir, n))
               and not os.path.islink(os.path.join(snaps_dir, n)) and not n.endswith((".partial", ".failed"))) \
    if os.path.isdir(snaps_dir) else []
fs_size_i, fs_free_i, store_i = num(fs_size), num(fs_free), num(store)
receipt = {
    "schema": "vkm.backup_receipt/1", "host": "EDGE", "run_id": run_id, "status": result,
    "verdict": verdict or ("FAIL" if result == "FAILED" else None),
    "failed_step": None if result in ("DONE", "DRY_RUN") else step, "exit_code": int(rc), "note": note or None,
    "started_at": started, "finished_at": now.isoformat(timespec="seconds"),
    "snapshot": final or None, "planned_snapshot": name, "previous": prev or None, "rehash": rehash == "1",
    "source": latest and {k: latest.get(k) for k in ("name", "created_at", "files", "bytes", "digest",
                                                      "canonical_current", "manifest_sha256", "resolved")},
    "source_age_h": age_h,
    "estimate": est,
    "transfer": {"rsync_exit": num(rsync_rc), "seconds": num(t_copy), **stats},
    "target_manifest": tgt and {k: tgt.get(k) for k in ("files", "bytes", "hashed", "hashed_bytes", "reused",
                                                         "cache_conflicts", "seconds")},
    "compare": cmp_ and {"verdict": cmp_.get("verdict"), "equal": cmp_.get("equal"),
                         "digest_equal": cmp_.get("digest_equal"),
                         "promotion_allowed": cmp_.get("promotion_allowed"),
                         "cache_conflicts": cmp_.get("cache_conflicts"),
                         "changed_immutable": (cmp_.get("changed_after_manifest") or {}).get("stable"),
                         "extra_immutable": (cmp_.get("extra") or {}).get("immutable"),
                         **{k: (cmp_.get(k) or {}).get("count") for k in ("missing", "missing_volatile", "corrupt",
                                                                          "changed_after_manifest", "extra")},
                         "examples": {k: (cmp_.get(k) or {}).get("examples", [])[:5]
                                      for k in ("missing", "corrupt") if (cmp_.get(k) or {}).get("count")}},
    "retention": prune and {"kept": (prune.get("plan") or {}).get("keep"), "deleted": prune.get("deleted"),
                            "deleted_for_space": prune.get("deleted_for_space"), "space_ok": prune.get("space_ok")},
    "store": {"snapshots": names, "count": len(names), "store_bytes": store_i,
              "budget_bytes": int(budget_gb) * 10**9, "over_budget": (store_i or 0) > int(budget_gb) * 10**9,
              "fs_size_bytes": fs_size_i, "fs_free_bytes": fs_free_i,
              "fs_free_pct": round(100 * fs_free_i / fs_size_i, 1) if fs_size_i and fs_free_i is not None else None},
}
text = json.dumps(receipt, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
with open(os.path.join(work, "receipt.json"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write(text)
parts = [receipt["finished_at"], run_id, result, f"verdict={receipt['verdict']}", f"snapshot={final or '-'}"]
if receipt["failed_step"]:
    parts.insert(3, f"step={step}")
if latest:
    parts += [f"files={latest.get('files')}", f"bytes={latest.get('bytes')}"]
if stats.get("bytes_transferred") is not None:
    parts.append(f"new_bytes={stats['bytes_transferred']}")
parts += [f"snapshots={len(names)}", f"store_bytes={store_i}", f"fs_free={fs_free_i}"]
if note:
    parts.append(f"note={note}")
line = " ".join(str(p) for p in parts) + "\n"
if dry != "1":
    status = os.path.join(root, "status")
    os.makedirs(os.path.join(status, "history"), exist_ok=True)
    for dst in (os.path.join(status, "history", run_id + ".json"), os.path.join(status, "latest.json")):
        tmp = dst + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, dst)
    if final and os.path.isdir(os.path.join(snaps_dir, final, ".vkm_backup")):
        with open(os.path.join(snaps_dir, final, ".vkm_backup", "receipt.json"), "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(os.path.join(status, "STATUS.tmp"), "w", encoding="utf-8") as fh:
        fh.write(line)
    os.replace(os.path.join(status, "STATUS.tmp"), os.path.join(status, "STATUS"))
    hist = sorted(os.listdir(os.path.join(status, "history")))
    for old in hist[:-120]:                      # ~4 months of nightly receipts
        os.remove(os.path.join(status, "history", old))
else:
    sys.stdout.write(text)
print(line, end="")
PY
  if [ "$DRY" = 0 ]; then
    if rsync -a --timeout=300 -e "$SSH_CMD" "$ROOT/status/latest.json" "$ROOT/status/STATUS" "$STATUS_DEST" \
         > "$WORK/push.out" 2>&1; then
      log "status pushed to CORE"
    else
      log "WARNING: pushing the status to CORE failed (see $WORK/push.out); the 04:00 checks will see an old receipt"
    fi
    # work files of runs older than 14 days
    find "$ROOT/work" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} + 2>/dev/null
    find "$ROOT/logs" -type f -mtime +120 -delete 2>/dev/null
  fi
  log "finished: $RESULT verdict=${VERDICT:--} (step $STEP, exit $rc)"
  exit "$rc"
}
trap finish EXIT
trap 'NOTE="stopped by signal"; exit 143' TERM INT

log "run $RUN_ID: source $SRC → $SNAPS/$NAME (previous ${PREV:-none}; rehash=$REHASH; dry_run=$DRY)"

# ---------------------------------------------------------------- 1. the source manifest (fresh, sha256 checked)
STEP="manifest"
deadline=$(( $(date +%s) + WAIT_S ))
while :; do
  rm -f "$WORK/LATEST.json"
  if rsync -a --timeout=120 -e "$SSH_CMD" "${SRC}receipts/backup/source/LATEST.json" "$WORK/LATEST.json" \
       2> "$WORK/latest.err"; then
    age_ok="$(python3 - "$WORK/LATEST.json" "$MAX_AGE_H" <<'PY'
import datetime, json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(d["created_at"])
print("yes" if age.total_seconds() <= float(sys.argv[2]) * 3600 else "no")
PY
)"
    [ "$age_ok" = "yes" ] && break
    msg="the source manifest on CORE is older than ${MAX_AGE_H} h (vkm-backup-prepare did not run?)"
  else
    msg="cannot read receipts/backup/source/LATEST.json from CORE ($(tail -c 300 "$WORK/latest.err" | tr '\n' ' '))"
  fi
  if [ "$DRY" = 1 ] || [ "$(date +%s)" -ge "$deadline" ]; then NOTE="$msg"; exit 1; fi
  log "$msg — waiting ${POLL_S} s"
  sleep "$POLL_S"
done
MAN_REL="$(jget "$WORK/LATEST.json" manifest)"
MAN_SHA="$(jget "$WORK/LATEST.json" manifest_sha256)"
[[ "$MAN_REL" =~ ^receipts/backup/source/[0-9T-]+\.manifest\.jsonl\.gz$ ]] || { NOTE="bad manifest path in LATEST.json"; exit 1; }
rsync -a --timeout=300 -e "$SSH_CMD" "${SRC}${MAN_REL}" "$WORK/source.manifest.jsonl.gz"
got="$(sha256sum "$WORK/source.manifest.jsonl.gz" | cut -d' ' -f1)"
[ "$got" = "$MAN_SHA" ] || { NOTE="sha256 of the source manifest differs from LATEST.json"; exit 1; }
log "source manifest $(jget "$WORK/LATEST.json" name): $(jget "$WORK/LATEST.json" files) files," \
    "$(jget "$WORK/LATEST.json" bytes) bytes, CURRENT $(jget "$WORK/LATEST.json" canonical_current)"

# ---------------------------------------------------------------- 2. plan: file list, bytes to copy, room on disk
STEP="plan"
python3 "$TOOL" files --manifest "$WORK/source.manifest.jsonl.gz" --out "$WORK/files.list" > /dev/null
est_args=(estimate --manifest "$WORK/source.manifest.jsonl.gz")
[ -n "$PREV_MAN" ] && est_args+=(--previous "$PREV_MAN")
python3 "$TOOL" "${est_args[@]}" > "$WORK/estimate.json"
NEED="$(jget "$WORK/estimate.json" bytes)"
log "to copy (not in ${PREV:-any previous snapshot}): $(jget "$WORK/estimate.json" files) files, $NEED bytes"
ret_args=(--snapshots-dir "$SNAPS" --daily "$KEEP_DAILY" --weekly "$KEEP_WEEKLY" --monthly "$KEEP_MONTHLY"
          --minimum "$KEEP_MIN" --min-free-gb "$MIN_FREE_GB" --keep-failed 1)
[ -n "$PREV" ] && ret_args+=(--protect "$PREV")
rsync_args=(-a --no-o --no-g --files-from="$WORK/files.list" --ignore-missing-args --partial-dir=.rsync-partial
            --timeout=900 --stats --no-human-readable -e "$SSH_CMD")
[ -n "$PREV" ] && rsync_args+=(--link-dest="$SNAPS/$PREV")
[ "$REHASH" = 1 ] && rsync_args+=(--checksum)
[ -n "$BWLIMIT" ] && rsync_args+=(--bwlimit="$BWLIMIT")
if [ "$DRY" = 1 ]; then
  python3 "$TOOL" prune "${ret_args[@]}" --need-bytes "$NEED" --dry-run --out "$WORK/prune.json" > /dev/null
  log "retention plan: delete $(jget "$WORK/prune.json" deleted); free after: $(jget "$WORK/prune.json" free_bytes)" \
      "bytes (wanted ≥ $(jget "$WORK/prune.json" wanted_free_bytes))"
  log "DRY RUN — rsync ${rsync_args[*]} --dry-run $SRC $SNAPS/$NAME.partial/"
  RSYNC_RC=0
  rsync "${rsync_args[@]}" --dry-run "$SRC" "$WORK/dry-target/" > "$WORK/rsync.out" 2> "$WORK/rsync.err" || RSYNC_RC=$?
  log "rsync --dry-run exit $RSYNC_RC: $(grep -E 'Number of regular files transferred|Total transferred file size' \
      "$WORK/rsync.out" | tr '\n' ' ')"
  rmdir "$WORK/dry-target" 2>/dev/null || true
  [ "$RSYNC_RC" = 0 ] || [ "$RSYNC_RC" = 24 ] || { NOTE="rsync --dry-run failed (exit $RSYNC_RC)"; exit 1; }
  RESULT="DRY_RUN"
  exit 0
fi
python3 "$TOOL" prune "${ret_args[@]}" --need-bytes "$NEED" --out "$WORK/prune_before.json" > /dev/null \
  || { NOTE="not enough free space on EDGE for $NEED new bytes + ${MIN_FREE_GB} GB reserve"; exit 1; }

# ---------------------------------------------------------------- 3. copy (hard links to the previous snapshot)
STEP="copy"
mkdir -p "$SNAP"
t0=$(date +%s)
RSYNC_RC=0
rsync "${rsync_args[@]}" "$SRC" "$SNAP/" > "$WORK/rsync.out" 2> "$WORK/rsync.err" || RSYNC_RC=$?
T_COPY=$(( $(date +%s) - t0 ))
log "rsync exit $RSYNC_RC in ${T_COPY} s: $(grep -E 'Number of regular files transferred|Total transferred file size' \
    "$WORK/rsync.out" | tr '\n' ' ')"
case "$RSYNC_RC" in
  0|23|24) ;;     # 23/24: some files failed or vanished — the verification decides
  *) NOTE="rsync failed (exit $RSYNC_RC): $(tail -c 300 "$WORK/rsync.err" | tr '\n' ' ')"; exit 1 ;;
esac

# ---------------------------------------------------------------- 4. manifest of the copy
STEP="verify"
mkdir -p "$SNAP/.vkm_backup"
cp "$WORK/source.manifest.jsonl.gz" "$SNAP/.vkm_backup/source_manifest.jsonl.gz"
cp "$WORK/LATEST.json" "$SNAP/.vkm_backup/source_LATEST.json"
tb_args=(build --tree --root "$SNAP" --skip .vkm_backup --label edge-snapshot --jobs "$JOBS"
         --out "$SNAP/.vkm_backup/target_manifest.jsonl.gz")
[ -n "$PREV_MAN" ] && tb_args+=(--cache "$PREV_MAN")
[ "$REHASH" = 1 ] && tb_args+=(--rehash-all)
TB_RC=0
python3 "$TOOL" "${tb_args[@]}" > "$WORK/target_build.json" || TB_RC=$?
[ "$TB_RC" -le 1 ] || { NOTE="manifest of the copy failed (exit $TB_RC)"; exit 1; }
cp "$WORK/target_build.json" "$SNAP/.vkm_backup/target_build.json"

# ---------------------------------------------------------------- 5. verification: source vs copy
COMPARE_RC=0
python3 "$TOOL" compare "$WORK/source.manifest.jsonl.gz" "$SNAP/.vkm_backup/target_manifest.jsonl.gz" \
  --out "$WORK/compare.json" || COMPARE_RC=$?
cp "$WORK/compare.json" "$SNAP/.vkm_backup/compare.json"
VERDICT="$(jget "$WORK/compare.json" verdict)"
case "$VERDICT" in PASS|WARN) ;; *) VERDICT="FAIL" ;; esac
if [ "$COMPARE_RC" != 0 ] || [ "$(jget "$WORK/compare.json" promotion_allowed)" != true ]; then
  VERDICT="FAIL"
fi
if [ "$TB_RC" != 0 ]; then
  VERDICT="FAIL"
  NOTE="re-hash contradicted the hash cache; promotion blocked pending reconciliation (see target_build.json)"
fi
log "verification: $VERDICT (equal $(jget "$WORK/compare.json" equal), missing $(jget "$WORK/compare.json" missing.count)," \
    "corrupt $(jget "$WORK/compare.json" corrupt.count), changed $(jget "$WORK/compare.json" changed_after_manifest.count))"

# ---------------------------------------------------------------- 6. promote (or keep as .failed)
STEP="promote"
if [ "$VERDICT" = "FAIL" ]; then
  rm -rf "$SNAPS/$NAME.failed"
  mv -T "$SNAP" "$SNAPS/$NAME.failed"
  NOTE="${NOTE:-verification FAIL: missing or corrupt files (see $NAME.failed/.vkm_backup/compare.json)}"
  exit 1
fi
mv -T "$SNAP" "$SNAPS/$NAME"
FINAL="$NAME"
ln -sfn "$NAME" "$SNAPS/latest.new"
mv -Tf "$SNAPS/latest.new" "$SNAPS/latest"
log "snapshot $NAME is latest"

# ---------------------------------------------------------------- 7. retention and the free-space guard
STEP="retention"
python3 "$TOOL" prune "${ret_args[@]}" --protect "$NAME" --out "$WORK/prune.json" > /dev/null \
  || NOTE="${NOTE:-free space on EDGE is below the ${MIN_FREE_GB} GB reserve after retention}"
log "retention: deleted $(jget "$WORK/prune.json" deleted)"

STEP="done"
RESULT="DONE"
