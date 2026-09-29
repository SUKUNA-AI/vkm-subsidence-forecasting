#!/usr/bin/env bash
# Install or remove the VKM backup job on EDGE (agent OPS, 29.09.2026). Run ON EDGE, as the owner of VKM_EDGE_ROOT,
# from the copied directory <VKM_EDGE_ROOT>/backup/bin (a copy of infra/edge/backup/). The backup store is the parent
# directory <VKM_EDGE_ROOT>/backup (snapshots/, status/, work/, logs/).
#
#   bash install.sh --dry-run                              checks and the plan; changes nothing
#   bash install.sh [--core-host ADDR --core-user USER]   backup.env (if absent), the SSH key (if absent), the ssh
#                                                          alias vkm-core-backup (with --core-host/--core-user),
#                                                          the user units, the timer enabled
#   bash install.sh --no-enable                            the same without enabling the timer
#   bash install.sh --uninstall                            timer disabled, units removed (snapshots, key, env stay)
#
# After installing: authorize the printed public key on CORE (a forced command, see infra/core/nightly/install.sh),
# then `bash vkm_backup.sh --dry-run` checks the whole chain through the gate without copying.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
UDIR="$HOME/.config/systemd/user"
ENVF="$HOME/.config/vkm/backup.env"
KEY="$HOME/.ssh/vkm_backup_ed25519"
ALIAS="vkm-core-backup"
UNITS=(vkm-backup.service vkm-backup.timer)
MODE="install" ENABLE=1 CORE_HOST="" CORE_USER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) MODE="dry"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    --no-enable) ENABLE=0; shift ;;
    --core-host) CORE_HOST="${2:-}"; shift 2 ;;
    --core-user) CORE_USER="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
say() { printf '%s\n' "$*"; }

if [ "$MODE" = "uninstall" ]; then
  systemctl --user disable --now vkm-backup.timer 2>/dev/null || true
  for u in "${UNITS[@]}"; do rm -f "$UDIR/$u"; done
  systemctl --user daemon-reload
  say "removed: ${UNITS[*]} (snapshots in $ROOT/snapshots, $KEY and $ENVF are kept; delete them by hand if retired)"
  exit 0
fi

bad=0
for c in bash python3 rsync flock ssh ssh-keygen sha256sum du df systemctl; do
  command -v "$c" >/dev/null && say "ok   $c" || { say "MISS $c"; bad=1; }
done
for f in vkm_backup.sh vkm_restore.sh vkm_manifest.py backup.env.example systemd/vkm-backup.service \
         systemd/vkm-backup.timer; do
  [ -f "$HERE/$f" ] || { say "MISS $HERE/$f"; bad=1; }
done
if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]; then
  say "WARN lingering is off: user timers stop at logout (sudo loginctl enable-linger $(id -un))"
fi
free_gb=$(( $(df -B1 --output=avail "$ROOT" | tail -n 1) / 1000000000 ))
say "free space for $ROOT: ${free_gb} GB (a full copy of the CORE backup set is ~45 GB; reserve 100 GB)"
[ "$free_gb" -ge 160 ] || say "WARN less than 160 GB free: lower VKM_BACKUP_KEEP_* or VKM_BACKUP_PACKS=none on CORE"
if [ -n "$CORE_HOST$CORE_USER" ] && { [ -z "$CORE_HOST" ] || [ -z "$CORE_USER" ]; }; then
  say "--core-host and --core-user go together"; bad=1
fi
[ "$bad" = 0 ] || { say "checks failed: nothing installed"; exit 2; }

if [ "$MODE" = "dry" ]; then
  say "would create $ROOT/{snapshots,status/history,work,logs}"
  say "would write $ENVF (VKM_BACKUP_ROOT=$ROOT, VKM_BACKUP_BIN=$HERE) unless it exists"
  [ -f "$KEY" ] && say "SSH key $KEY exists" || say "would create the SSH key $KEY (ed25519, no passphrase)"
  if grep -qsE "^Host[[:space:]]+$ALIAS\$" "$HOME/.ssh/config"; then say "ssh alias $ALIAS exists"
  elif [ -n "$CORE_HOST" ]; then say "would add the ssh alias $ALIAS (HostName from --core-host, User from --core-user)"
  else say "ssh alias $ALIAS is missing: pass --core-host/--core-user or add it by hand (OPERATIONS.md)"; fi
  say "would copy ${UNITS[*]} to $UDIR, daemon-reload$([ "$ENABLE" = 1 ] && echo ", enable --now vkm-backup.timer")"
  exit 0
fi

chmod 0755 "$HERE"/*.sh
mkdir -p "$ROOT/snapshots" "$ROOT/status/history" "$ROOT/work" "$ROOT/logs" "$UDIR" "$(dirname "$ENVF")" \
  "$HOME/.ssh"
chmod 0700 "$ROOT" "$HOME/.ssh"
if [ ! -f "$ENVF" ]; then
  sed -e "s|^VKM_BACKUP_ROOT=.*|VKM_BACKUP_ROOT=$ROOT|" -e "s|^VKM_BACKUP_BIN=.*|VKM_BACKUP_BIN=$HERE|" \
    "$HERE/backup.env.example" > "$ENVF"
  chmod 0600 "$ENVF"
  say "wrote $ENVF"
else
  say "kept $ENVF"
fi
if [ ! -f "$KEY" ]; then
  ssh-keygen -q -t ed25519 -N "" -C "vkm-backup@edge" -f "$KEY"
  say "created $KEY"
fi
if ! grep -qsE "^Host[[:space:]]+$ALIAS\$" "$HOME/.ssh/config"; then
  if [ -n "$CORE_HOST" ]; then
    [ -f "$HOME/.ssh/config" ] && cp -p "$HOME/.ssh/config" "$HOME/.ssh/config.bak-$(date +%Y%m%dT%H%M%S)"
    printf '\nHost %s\n  HostName %s\n  User %s\n  IdentityFile %s\n  IdentitiesOnly yes\n  BatchMode yes\n  ServerAliveInterval 30\n' \
      "$ALIAS" "$CORE_HOST" "$CORE_USER" "$KEY" >> "$HOME/.ssh/config"
    chmod 0600 "$HOME/.ssh/config"
    say "added the ssh alias $ALIAS"
    ssh-keygen -F "$CORE_HOST" >/dev/null 2>&1 || say "WARN the host key of $CORE_HOST is not in known_hosts: " \
      "connect once interactively (ssh $ALIAS) and compare the fingerprint with CORE"
  else
    say "WARN ssh alias $ALIAS is missing: add it (OPERATIONS.md) or re-run with --core-host/--core-user"
  fi
fi
for u in "${UNITS[@]}"; do install -m 0644 "$HERE/systemd/$u" "$UDIR/$u"; done
systemd-analyze --user verify "${UNITS[@]/#/$UDIR/}" || say "WARN systemd-analyze reported the lines above"
systemctl --user daemon-reload
[ "$ENABLE" = 1 ] && systemctl --user enable --now vkm-backup.timer
systemctl --user list-timers vkm-backup.timer --all || true
say ""
say "public key to authorize on CORE (forced command vkm_backup_gate.sh, see infra/core/nightly/install.sh):"
cat "$KEY.pub"
say "then: bash $HERE/vkm_backup.sh --dry-run    (reads through the gate, copies nothing)"
