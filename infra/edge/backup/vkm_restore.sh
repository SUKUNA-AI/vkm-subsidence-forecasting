#!/usr/bin/env bash
# Restore from an EDGE backup snapshot (agent OPS, 29.09.2026): a subset or the whole data root into a NEW directory,
# then a sha256 check of every restored file against the snapshot's manifest (vkm_manifest.py verify).
#
#   vkm_restore.sh --to DIR [--snapshot NAME|latest] [--path REL]... [--jobs N] [--dry-run]
#   vkm_restore.sh --verify-only [--snapshot NAME|latest] [--path REL]...     # re-hash the snapshot itself
#
#   --to DIR        target directory; must not exist or be empty (never the live data root: restore next to it and
#                   swap, see OPERATIONS.md «Ночные задания и резервная копия»)
#   --snapshot      a snapshot under $VKM_BACKUP_ROOT/snapshots (default latest)
#   --path REL      relative prefix to restore, e.g. receipts, canonical/_snapshots, derived/navigation (repeatable;
#                   default: everything, including .vkm_backup/ with the manifests)
#   --dry-run       what would be restored (files, bytes; all present in the snapshot?) — copies nothing
#   --verify-only   check the snapshot on EDGE against its manifest (bit rot, a damaged store) — copies nothing
# The manifest is <snapshot>/.vkm_backup/target_manifest.jsonl.gz (the snapshot as verified against CORE when it was
# taken). A report is written to <DIR>/.vkm_restore/verify.json (or printed with --verify-only / --dry-run).
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${VKM_BACKUP_ENV:-$HOME/.config/vkm/backup.env}"
if [ -z "${VKM_BACKUP_ROOT:-}" ] && [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi
TOOL="${VKM_MANIFEST_TOOL:-$HERE/vkm_manifest.py}"
ROOT="${VKM_BACKUP_ROOT:-}"
SNAPSHOT="latest" TO="" DRY=0 VERIFY_ONLY=0 JOBS="${VKM_BACKUP_JOBS:-2}"
PATHS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --to) TO="${2:-}"; shift 2 ;;
    --snapshot) SNAPSHOT="${2:-}"; shift 2 ;;
    --path) PATHS+=("${2:-}"); shift 2 ;;
    --jobs) JOBS="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$ROOT" ] || { echo "VKM_BACKUP_ROOT is not set (see backup.env.example)" >&2; exit 2; }
for c in python3 rsync; do command -v "$c" >/dev/null || { echo "$c is required" >&2; exit 2; }; done
[[ "$JOBS" =~ ^[1-9][0-9]?$ ]] || { echo "--jobs must be 1..99" >&2; exit 2; }
[[ "$SNAPSHOT" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad snapshot name: $SNAPSHOT" >&2; exit 2; }
for p in "${PATHS[@]}"; do
  case "$p" in ""|/*|*..*) echo "--path must be a relative path inside the snapshot: $p" >&2; exit 2 ;; esac
done
SNAP="$ROOT/snapshots/$SNAPSHOT"
[ -d "$SNAP/" ] || { echo "snapshot not found: $SNAP" >&2; exit 2; }
SNAP="$(cd "$SNAP/" && pwd -P)"          # latest → the real snapshot directory
MAN="$SNAP/.vkm_backup/target_manifest.jsonl.gz"
[ -f "$MAN" ] || { echo "the snapshot has no manifest: $MAN" >&2; exit 2; }
path_args=()
for p in "${PATHS[@]}"; do path_args+=(--path "$p"); done
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
log "snapshot $(basename "$SNAP"); paths: ${PATHS[*]:-everything}"

if [ "$VERIFY_ONLY" = 1 ]; then
  python3 "$TOOL" verify --root "$SNAP" --manifest "$MAN" --jobs "$JOBS" "${path_args[@]}"
  exit $?
fi

# the selected files of the manifest (plus .vkm_backup/ when everything is restored)
LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT
python3 - "$MAN" "$LIST" "${PATHS[@]}" <<'PY'
import gzip, json, sys
man, out, prefixes = sys.argv[1], sys.argv[2], [p.strip("/") for p in sys.argv[3:]]
n = size = 0
with gzip.open(man, "rt", encoding="utf-8") as fh, open(out, "w", encoding="utf-8", newline="\n") as lst:
    fh.readline()
    for line in fh:
        rel, s, _m, _i, _h = json.loads(line)
        if not prefixes or any(rel == p or rel.startswith(p + "/") for p in prefixes):
            lst.write(rel + "\n")
            n += 1
            size += s
print(json.dumps({"files": n, "bytes": size}))
PY
[ -s "$LIST" ] || { echo "no file of the manifest matches the given --path" >&2; exit 1; }

if [ "$DRY" = 1 ]; then
  missing="$(while IFS= read -r rel; do [ -f "$SNAP/$rel" ] || echo "$rel"; done < "$LIST" | head -n 20)"
  if [ -n "$missing" ]; then log "DRY RUN: files of the manifest missing in the snapshot:"; echo "$missing"; exit 1; fi
  log "DRY RUN: all selected files are present; target ${TO:-<--to DIR>} would receive them (rsync -a --files-from)"
  exit 0
fi

[ -n "$TO" ] || { echo "--to DIR is required" >&2; exit 2; }
if [ -e "$TO" ] && [ -n "$(ls -A "$TO" 2>/dev/null)" ]; then
  echo "refusing to restore into a non-empty directory: $TO" >&2; exit 2
fi
mkdir -p "$TO"
rsync -a --files-from="$LIST" "$SNAP/" "$TO/"
if [ "${#PATHS[@]}" = 0 ]; then rsync -a "$SNAP/.vkm_backup" "$TO/"; fi
mkdir -p "$TO/.vkm_restore"
rc=0
python3 "$TOOL" verify --root "$TO" --manifest "$MAN" --jobs "$JOBS" "${path_args[@]}" \
  --out "$TO/.vkm_restore/verify.json" || rc=$?
python3 - "$TO/.vkm_restore" "$(basename "$SNAP")" "$rc" "${PATHS[@]}" <<'PY'
import datetime, json, os, sys
out, snap, rc, paths = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4:]
v = json.load(open(os.path.join(out, "verify.json"), encoding="utf-8"))
json.dump({"schema": "vkm.backup_restore/1", "snapshot": snap, "paths": paths or ["<all>"],
           "restored_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "verdict": v.get("verdict"), "files": v.get("files"), "bytes": v.get("bytes"), "ok": v.get("ok"),
           "missing": v["missing"]["count"], "corrupt": v["corrupt"]["count"]},
          open(os.path.join(out, "restore.json"), "w", encoding="utf-8"), indent=1, sort_keys=True)
print(f"restore {snap}: {v.get('verdict')} — {v.get('ok')}/{v.get('files')} files, {v.get('bytes')} bytes")
PY
exit "$rc"
