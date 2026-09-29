#!/usr/bin/env bash
# Install or remove the VKM nightly jobs on CORE (agent OPS, 29.09.2026). Run ON CORE, as the data-root owner, from
# the copied directory <compose dir>/nightly — the coordinator first copies there infra/core/nightly/,
# infra/edge/backup/vkm_manifest.py and the frozen topic_v1 files (OPERATIONS.md «Ночные задания и резервная копия»).
#
#   bash install.sh --dry-run     checks, dry runs of both jobs and the plan; changes nothing
#   bash install.sh               ~/.config/vkm/nightly.env (if absent), the user units, both timers enabled
#   bash install.sh --no-enable   units installed, timers not enabled
#   bash install.sh --uninstall   timers disabled, units removed (receipts, dossiers and nightly.env stay)
#
# Timers: vkm-backup-prepare.timer 02:00 MSK (source manifest for the EDGE copy), vkm-nightly.timer 04:00 MSK.
# The EDGE backup key is authorized separately (a forced command, printed at the end).
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${VKM_COMPOSE_DIR:-$(dirname "$HERE")}"
UDIR="$HOME/.config/systemd/user"
ENVF="$HOME/.config/vkm/nightly.env"
UNITS=(vkm-nightly.service vkm-nightly.timer vkm-backup-prepare.service vkm-backup-prepare.timer)
TIMERS=(vkm-backup-prepare.timer vkm-nightly.timer)
FILES=(nightly_checks.sh backup_prepare.sh vkm_backup_gate.sh nightly_summary.py dossier_store.py dossiers.py
       mcp_smoke.py topic_score.py vkm_manifest.py topic_v1/SHA256SUMS topic_v1/topic_set_v1.jsonl
       topic_v1/topic_queries_v1.tsv topic_v1/metrics_spec_v1.json topic_v1/page_mapping_v1.json
       topic_v1/results_v1.json topic_v1/harness_core.py)
MODE="install" ENABLE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) MODE="dry"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    --no-enable) ENABLE=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
say() { printf '%s\n' "$*"; }

if [ "$MODE" = "uninstall" ]; then
  systemctl --user disable --now "${TIMERS[@]}" 2>/dev/null || true
  for u in "${UNITS[@]}"; do rm -f "$UDIR/$u"; done
  systemctl --user daemon-reload
  say "removed: ${UNITS[*]} (receipts/nightly, receipts/backup, derived/dossiers and $ENVF are kept)"
  say "also remove the EDGE backup key line (vkm_backup_gate.sh) from ~/.ssh/authorized_keys if the backup is retired"
  exit 0
fi

# ---------------------------------------------------------------- checks
bad=0
for c in bash python3 docker flock timeout sha256sum systemctl; do
  command -v "$c" >/dev/null && say "ok   $c" || { say "MISS $c"; bad=1; }
done
command -v rrsync >/dev/null && say "ok   rrsync (backup gate)" || { say "MISS rrsync (Debian: package rsync)"; bad=1; }
for f in "${FILES[@]}"; do [ -f "$HERE/$f" ] || { say "MISS $HERE/$f"; bad=1; }; done
for u in "${UNITS[@]}"; do [ -f "$HERE/systemd/$u" ] || { say "MISS systemd/$u"; bad=1; }; done
[ -f "$COMPOSE_DIR/compose.yml" ] && [ -f "$COMPOSE_DIR/.env" ] || { say "MISS compose.yml/.env in $COMPOSE_DIR"; bad=1; }
if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]; then
  say "WARN lingering is off: user timers stop at logout (sudo loginctl enable-linger $(id -un))"
fi
if ! docker compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/compose.yml" --profile nightly config \
     --services 2>/dev/null | grep -qx vkm-nightly; then
  say "MISS compose service vkm-nightly (deploy the current infra/core/compose.yml first)"; bad=1
fi
[ "$bad" = 0 ] || { say "checks failed: nothing installed"; exit 2; }
chmod 0755 "$HERE"/*.sh

if [ "$MODE" = "dry" ]; then
  say "--- nightly_checks.sh --dry-run"
  bash "$HERE/nightly_checks.sh" --dry-run || bad=1
  say "--- backup_prepare.sh --dry-run"
  bash "$HERE/backup_prepare.sh" --dry-run || bad=1
  say "--- plan"
  say "would write $ENVF (VKM_NIGHTLY_DIR=$HERE, VKM_COMPOSE_DIR=$COMPOSE_DIR) unless it exists"
  say "would copy ${UNITS[*]} to $UDIR, daemon-reload$([ "$ENABLE" = 1 ] && echo ", enable --now ${TIMERS[*]}")"
  exit "$bad"
fi

# ---------------------------------------------------------------- install
mkdir -p "$UDIR" "$(dirname "$ENVF")"
if [ ! -f "$ENVF" ]; then
  sed -e "s|^VKM_NIGHTLY_DIR=.*|VKM_NIGHTLY_DIR=$HERE|" -e "s|^VKM_COMPOSE_DIR=.*|VKM_COMPOSE_DIR=$COMPOSE_DIR|" \
    "$HERE/nightly.env.example" > "$ENVF"
  chmod 0600 "$ENVF"
  say "wrote $ENVF"
else
  say "kept $ENVF"
fi
for u in "${UNITS[@]}"; do install -m 0644 "$HERE/systemd/$u" "$UDIR/$u"; done
systemd-analyze --user verify "${UNITS[@]/#/$UDIR/}" || say "WARN systemd-analyze reported the lines above"
systemctl --user daemon-reload
if [ "$ENABLE" = 1 ]; then
  systemctl --user enable --now "${TIMERS[@]}"
fi
systemctl --user list-timers "${TIMERS[@]}" --all || true
say ""
say "EDGE backup key → append ONE line to ~/.ssh/authorized_keys of $(id -un) on this host (key and address from EDGE):"
say "command=\"/bin/sh $HERE/vkm_backup_gate.sh\",restrict,from=\"<EDGE address>\" <contents of the EDGE key .pub>"
say "first runs by hand: systemctl --user start vkm-backup-prepare.service; systemctl --user start vkm-nightly.service"
