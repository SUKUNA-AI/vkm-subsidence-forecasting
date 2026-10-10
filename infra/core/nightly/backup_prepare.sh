#!/usr/bin/env bash
# VKM backup to EDGE, CORE side (agent OPS, 29.09.2026): the source manifest of the backup set, before EDGE copies it.
#
#   backup set of the CANONICAL data root (vkm_manifest.py CORE_SET: .vkm_root.json, canonical/, artifacts/, duckdb/,
#   accounting/ without tmp/, derived/ with only the CURRENT late pack, receipts/ without receipts/backup/, logs/) → sha256 manifest (hashes of
#   unchanged files are reused from the previous manifest; every file is re-read on VKM_BACKUP_REHASH_DAY) →
#   receipts/backup/source/<YYYY-MM-DDTHHMM>.manifest.jsonl.gz + LATEST.json (pointer, sha256 of the manifest file,
#   counts) → EDGE (infra/edge/backup/vkm_backup.sh, 02:30 MSK) copies exactly the files of this manifest and compares
#   its copy with it. The status pushed back by EDGE lands in receipts/backup/edge/ (created here).
#
# Timer: vkm-backup-prepare.timer (02:00 MSK); one run at a time (flock). Writes only under receipts/backup/.
# Options: --dry-run     walk and count (files, bytes, what would be hashed); writes nothing
#          --rehash-all  re-read every file (default: on day VKM_BACKUP_REHASH_DAY of the month)
# Environment: VKM_COMPOSE_DIR (default: the parent of this directory; its .env gives VKM_DATA_ROOT_HOST),
#   VKM_DATA_ROOT_HOST, VKM_BACKUP_PACKS (current | none | all; default current), VKM_BACKUP_JOBS (hash threads,
#   default 2), VKM_BACKUP_KEEP_MANIFESTS (default 7), VKM_BACKUP_REHASH_DAY (day of the month, default 1; 0 = never),
#   VKM_MANIFEST_TOOL (default: vkm_manifest.py next to this script).
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${VKM_COMPOSE_DIR:-$(dirname "$HERE")}"
TOOL="${VKM_MANIFEST_TOOL:-$HERE/vkm_manifest.py}"
PACKS="${VKM_BACKUP_PACKS:-current}"
JOBS="${VKM_BACKUP_JOBS:-2}"
KEEP="${VKM_BACKUP_KEEP_MANIFESTS:-7}"
REHASH_DAY="${VKM_BACKUP_REHASH_DAY:-1}"
DRY=0 REHASH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --rehash-all) REHASH=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
command -v python3 >/dev/null || { echo "python3 is required on the host" >&2; exit 2; }
[ -f "$TOOL" ] || { echo "manifest tool not found: $TOOL" >&2; exit 2; }
case "$PACKS" in current|none|all) ;; *) echo "VKM_BACKUP_PACKS must be current, none or all" >&2; exit 2 ;; esac
[[ "$JOBS" =~ ^[1-9][0-9]?$ ]] || { echo "VKM_BACKUP_JOBS must be 1..99" >&2; exit 2; }
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || { echo "VKM_BACKUP_KEEP_MANIFESTS must be >= 1" >&2; exit 2; }

env_get() {   # KEY from the environment, else from the compose .env
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f "$COMPOSE_DIR/.env" ]; then
    v="$(grep -E "^$1=" "$COMPOSE_DIR/.env" | tail -n1 | cut -d= -f2- || true)"
  fi
  printf '%s' "$v"
}
DATA="$(env_get VKM_DATA_ROOT_HOST)"
[ -n "$DATA" ] && [ -d "$DATA/canonical" ] && [ -f "$DATA/.vkm_root.json" ] \
  || { echo "VKM_DATA_ROOT_HOST is not a canonical data root" >&2; exit 2; }
OUT="$DATA/receipts/backup/source"
NAME="$(TZ=Europe/Moscow date +%Y-%m-%dT%H%M)"
[ "$REHASH_DAY" != 0 ] && [ "$(TZ=Europe/Moscow date +%-d)" = "$REHASH_DAY" ] && REHASH=1
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

PREV=""
if [ -f "$OUT/LATEST.json" ]; then
  PREV="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("manifest") or "")' \
          "$OUT/LATEST.json" 2>/dev/null || true)"
  [ -n "$PREV" ] && PREV="$DATA/$PREV"
fi
args=(build --root "$DATA" --packs "$PACKS" --label core-source --jobs "$JOBS")
[ -n "$PREV" ] && [ -f "$PREV" ] && args+=(--cache "$PREV")
[ "$REHASH" = 1 ] && args+=(--rehash-all)

if [ "$DRY" = 1 ]; then
  log "DRY RUN — data root $DATA; packs $PACKS; cache ${PREV:-none}; rehash_all=$REHASH; manifest would be $OUT/$NAME.manifest.jsonl.gz"
  python3 "$TOOL" "${args[@]}" --dry-run
  exit 0
fi

mkdir -p "$OUT" "$DATA/receipts/backup/edge"
exec 9>"$OUT/.lock"
flock -n 9 || { echo "another backup_prepare run holds $OUT/.lock" >&2; exit 75; }
STARTED="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
MAN="$OUT/$NAME.manifest.jsonl.gz"
log "manifest of the backup set → $MAN (packs $PACKS, cache ${PREV:-none}, rehash_all=$REHASH)"
rc=0
python3 "$TOOL" "${args[@]}" --out "$MAN" > "$OUT/$NAME.build.json" || rc=$?
# rc 1 = written, but re-hashed files contradict the cached hashes (a file changed without a new mtime/inode)
[ "$rc" -le 1 ] && [ -f "$MAN" ] || { log "manifest build failed (exit $rc)"; exit 1; }
python3 - "$DATA" "$OUT" "$NAME" "$MAN" "$STARTED" "$rc" <<'PY'
import datetime, gzip, hashlib, json, os, sys
data, out, name, man, started, rc = sys.argv[1:7]
h = hashlib.sha256()
with open(man, "rb") as fh:
    for block in iter(lambda: fh.read(1 << 20), b""):
        h.update(block)
with gzip.open(man, "rt", encoding="utf-8") as fh:
    head = json.loads(fh.readline())
try:
    current = open(os.path.join(data, "canonical", "CURRENT"), encoding="utf-8").read().strip()
except OSError:
    current = None
latest = {"schema": "vkm.backup_source/1", "name": name, "manifest": os.path.relpath(man, data).replace(os.sep, "/"),
          "manifest_sha256": h.hexdigest(), "manifest_bytes": os.path.getsize(man), "started_at": started,
          "created_at": head.get("created_at"), "canonical_current": current,
          "set": (head.get("set") or {}).get("name"), "resolved": head.get("resolved"),
          **{k: head.get(k) for k in ("files", "bytes", "digest", "hashed", "hashed_bytes", "reused", "rehash_all",
                                      "cache_conflicts", "seconds", "walk")},
          "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
tmp = os.path.join(out, "LATEST.json.tmp")
with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(latest, fh, ensure_ascii=False, indent=1, sort_keys=True)
os.replace(tmp, os.path.join(out, "LATEST.json"))
conf = (head.get("cache_conflicts") or {}).get("count") or 0
line = (f"{latest['finished_at']} {name} {'DONE' if not conf else 'DONE_WITH_CONFLICTS'} files={latest['files']} "
        f"bytes={latest['bytes']} hashed={latest['hashed']} reused={latest['reused']} conflicts={conf} "
        f"current={current}\n")
with open(os.path.join(out, "STATUS.tmp"), "w", encoding="utf-8") as fh:
    fh.write(line)
os.replace(os.path.join(out, "STATUS.tmp"), os.path.join(out, "STATUS"))
print(line, end="")
PY
# keep the newest $KEEP manifests (and their build reports); LATEST.json always names the newest
mapfile -t old < <(ls -1 "$OUT" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}\.manifest\.jsonl\.gz$' | sort -r \
                   | tail -n +"$((KEEP + 1))")
for f in "${old[@]}"; do
  rm -f -- "$OUT/$f" "$OUT/${f%.manifest.jsonl.gz}.build.json"
done
[ "${#old[@]}" -gt 0 ] && log "pruned ${#old[@]} old manifest(s)"
[ "$rc" = 0 ] || { log "WARNING: re-hashed files contradict cached hashes (see $OUT/$NAME.build.json)"; exit 1; }
exit 0
