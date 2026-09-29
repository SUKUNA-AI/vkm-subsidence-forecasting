#!/bin/sh
# Forced command of the EDGE backup key on CORE (agent OPS, 29.09.2026). One line in ~/.ssh/authorized_keys of the
# data-root owner on CORE (the coordinator adds it; the EDGE address and the key are host-local values):
#
#   command="/bin/sh <compose dir>/nightly/vkm_backup_gate.sh",restrict,from="<EDGE address>" ssh-ed25519 <key> vkm-backup@edge
#
# Allowed, through rrsync (rsync's restricted-rsync helper) and nothing else:
#   * pull  (rsync --server --sender …): read-only, rooted at the data root (VKM_DATA_ROOT_HOST of the compose .env);
#   * push  (rsync --server …):          write-only, no deletions, rooted at <data root>/receipts/backup/edge — the
#                                        backup status and receipts EDGE hands back to the 04:00 checks.
# A shell, a command, port forwarding or any other rsync root are refused ("restrict" also disables pty/forwarding).
set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_DIR=${VKM_COMPOSE_DIR:-$(dirname -- "$HERE")}
RRSYNC=${VKM_RRSYNC:-rrsync}
DATA=""
if [ -f "$COMPOSE_DIR/.env" ]; then
  DATA=$(grep -E '^VKM_DATA_ROOT_HOST=' "$COMPOSE_DIR/.env" | tail -n 1 | cut -d= -f2-)
fi
if [ -z "$DATA" ] || [ ! -d "$DATA/canonical" ]; then
  echo "vkm-backup-gate: the data root is not configured" >&2
  exit 1
fi

case "${SSH_ORIGINAL_COMMAND:-}" in
  "rsync --server --sender "*)
    exec "$RRSYNC" -ro "$DATA" ;;
  "rsync --server "*)
    mkdir -p "$DATA/receipts/backup/edge"
    exec "$RRSYNC" -wo -no-del "$DATA/receipts/backup/edge" ;;
  *)
    echo "vkm-backup-gate: only rsync is allowed (pull of the data root, push of the backup status)" >&2
    exit 1 ;;
esac
