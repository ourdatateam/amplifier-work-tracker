#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_DIR=/var/backups/beads
BEADS_UID=$(id -u beads)
RUNTIME_DIR=/run/user/$BEADS_UID
BUS_ADDRESS=unix:path=$RUNTIME_DIR/bus
AMP_SERVICE=amplifier-work-tracker.service
WRITERS=(beads-ui.service beads-mcp.service beads-watcher-touch.timer beads-watcher-touch.service)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TEMP=
BACKUP_COMPLETE=0
RESTART_FAILED=0
declare -A WAS_ACTIVE=()

fail() { printf 'backup-beads: %s\n' "$*" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || fail 'run as root inside beads160.'
getent passwd beads >/dev/null || fail 'beads service account is missing.'
[[ -S "$RUNTIME_DIR/bus" ]] || fail 'beads user systemd bus is unavailable.'
install -d -o root -g root -m 0700 "$BACKUP_DIR"
runuser -u beads -- env HOME=/var/lib/beads XDG_RUNTIME_DIR="$RUNTIME_DIR" \
  DBUS_SESSION_BUS_ADDRESS="$BUS_ADDRESS" systemctl --user is-active --quiet "$AMP_SERVICE" ||
  fail 'tracker service must be active before backup; refusing an ambiguous stop/start.'
WAS_ACTIVE[$AMP_SERVICE]=1
for service in "${WRITERS[@]}"; do
  if systemctl is-active --quiet "$service"; then WAS_ACTIVE[$service]=1; else WAS_ACTIVE[$service]=0; fi
done

restart_previously_active() {
  local service
  RESTART_FAILED=0
  # Try every previously active service even after an earlier restart fails.
  for service in "$AMP_SERVICE" beads-ui.service beads-mcp.service beads-watcher-touch.service beads-watcher-touch.timer; do
    [[ ${WAS_ACTIVE[$service]:-0} == 1 ]] || continue
    if [[ $service == "$AMP_SERVICE" ]]; then
      if ! runuser -u beads -- env HOME=/var/lib/beads XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        DBUS_SESSION_BUS_ADDRESS="$BUS_ADDRESS" systemctl --user start "$service"; then
        printf 'backup-beads: ERROR: failed to restart %s; inspect its user journal.\n' "$service" >&2
        RESTART_FAILED=1
      fi
    elif ! systemctl start "$service"; then
      printf 'backup-beads: ERROR: failed to restart %s.\n' "$service" >&2
      RESTART_FAILED=1
    fi
  done
}
retain_verified_backups() {
  python3 - "$BACKUP_DIR" <<'PY'
import hashlib
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
pattern = re.compile(r"^beads-(\d{8}T\d{6}Z)\.tar\.gz$")
archives = []
for archive in root.iterdir():
    match = pattern.fullmatch(archive.name)
    if not match or not archive.is_file():
        continue
    sidecar = root / f"{archive.name}.sha256"
    verified = False
    if sidecar.is_file():
        result = subprocess.run(
            ["sha256sum", "-c", sidecar.name], cwd=root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        verified = result.returncode == 0
    archives.append((match.group(1), archive, sidecar, verified))
archives.sort(key=lambda row: row[0], reverse=True)
keepers = {row[1] for row in [item for item in archives if item[3]][:7]}
for _, archive, sidecar, _ in archives:
    if archive in keepers:
        continue
    archive.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)
PY
}
cleanup() {
  local rc=$?
  trap - EXIT INT TERM HUP
  if [[ -n "$TEMP" ]] && ! rm -f -- "$TEMP"; then
    printf 'backup-beads: ERROR: failed to remove temporary archive %s.\n' "$TEMP" >&2
    if (( rc == 0 )); then rc=1; fi
  fi
  restart_previously_active
  if (( RESTART_FAILED )); then
    if (( rc == 0 )); then rc=1; fi
  elif (( rc == 0 && BACKUP_COMPLETE == 1 )); then
    if ! retain_verified_backups; then
      printf 'backup-beads: ERROR: retention pruning failed.\n' >&2
      rc=1
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

TEMP=$(mktemp "$BACKUP_DIR/.beads-${STAMP}.XXXXXX.tar.gz")
chmod 0600 "$TEMP"
# Quiesce every deployment writer before stopping the authoritative Dolt/Amp service.
for service in beads-watcher-touch.timer beads-watcher-touch.service beads-ui.service beads-mcp.service; do
  [[ ${WAS_ACTIVE[$service]:-0} != 1 ]] || systemctl stop "$service"
done
runuser -u beads -- env HOME=/var/lib/beads XDG_RUNTIME_DIR="$RUNTIME_DIR" \
  DBUS_SESSION_BUS_ADDRESS="$BUS_ADDRESS" systemctl --user stop "$AMP_SERVICE"
tar --numeric-owner --xattrs --acls -czf "$TEMP" \
  /var/lib/beads /etc/beads /opt/beads
tar -tzf "$TEMP" >/dev/null || fail 'tar integrity check failed.'
mv -- "$TEMP" "$BACKUP_DIR/beads-${STAMP}.tar.gz"
TEMP=
chmod 0600 "$BACKUP_DIR/beads-${STAMP}.tar.gz"
chown root:root "$BACKUP_DIR/beads-${STAMP}.tar.gz"
(
  cd "$BACKUP_DIR"
  sha256sum "beads-${STAMP}.tar.gz" >"beads-${STAMP}.tar.gz.sha256"
  chmod 0600 "beads-${STAMP}.tar.gz.sha256"
  chown root:root "beads-${STAMP}.tar.gz.sha256"
  sha256sum -c "beads-${STAMP}.tar.gz.sha256"
)
BACKUP_COMPLETE=1
printf 'Backup verified: %s/beads-%s.tar.gz (owner root:root, mode 0600; SHA-256 sidecar verified).\n' "$BACKUP_DIR" "$STAMP"
