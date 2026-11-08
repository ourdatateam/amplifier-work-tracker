#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOSTNAME=beads160
SOURCE=/opt/beads/source
PROJECTS=/var/lib/beads/projects
HOME_PROJECT=$PROJECTS/projects/home
SYSTEM_PROJECT=$PROJECTS/projects/system
UI_UNIT=/etc/systemd/system/beads-ui.service
WATCH_HELPER=/usr/local/sbin/beads-touch-watch-markers

fail() { printf 'repair-installed: %s\n' "$*" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || fail 'run as root inside the new container.'
[[ $(hostname -s) == "$EXPECTED_HOSTNAME" ]] || fail "hostname is $(hostname -s), expected $EXPECTED_HOSTNAME; no changes made."
[[ -d "$PROJECTS" && -d "$HOME_PROJECT/.beads" && -d "$SYSTEM_PROJECT/.beads" ]] ||
  fail 'expected existing projects at /var/lib/beads/projects/projects/{home,system}; refusing to alter project data.'
[[ -f "$SOURCE/deploy/hosted/backup.sh" && -f "$SOURCE/deploy/hosted/versions.env" && -f "$SOURCE/deploy/hosted/verify-host.sh" ]] ||
  fail 'reviewed staged deployment files are incomplete.'
[[ -f "$UI_UNIT" ]] || fail 'existing beads-ui system unit is missing; refusing to replace an unknown setup.'

uid=$(id -u beads 2>/dev/null) || fail 'beads service account is missing.'
RUNTIME_DIR=/run/user/$uid
[[ -S "$RUNTIME_DIR/bus" ]] || fail 'beads user systemd bus is unavailable.'
runuser -u beads -- env HOME=/var/lib/beads XDG_RUNTIME_DIR="$RUNTIME_DIR" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME_DIR/bus" \
  systemctl --user is-active --quiet amplifier-work-tracker.service ||
  fail 'upstream tracker user service is not active.'

working_dir_count=$(grep -c '^WorkingDirectory=' "$UI_UNIT" || true)
[[ $working_dir_count == 1 ]] || fail 'UI unit must contain exactly one WorkingDirectory line.'
if grep -Fqx 'WorkingDirectory=/var/lib/beads/projects/home' "$UI_UNIT"; then
  sed -i 's#^WorkingDirectory=/var/lib/beads/projects/home$#WorkingDirectory=/var/lib/beads/projects/projects/home#' "$UI_UNIT"
elif ! grep -Fqx "WorkingDirectory=$HOME_PROJECT" "$UI_UNIT"; then
  fail 'UI unit WorkingDirectory is neither the known old value nor the expected repaired path.'
fi

cat >"$WATCH_HELPER" <<'HELPER'
#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s nullglob
for project in /var/lib/beads/projects/projects/*; do
  [[ -d "$project/.beads" ]] || continue
  touch "$project/.beads/.hosted-watcher-marker"
done
HELPER
chmod 0755 "$WATCH_HELPER"
install -o root -g root -m 0755 "$SOURCE/deploy/hosted/backup.sh" /usr/local/sbin/backup-beads
install -o root -g root -m 0644 "$SOURCE/deploy/hosted/versions.env" /opt/beads/versions.env
install -o root -g root -m 0755 "$SOURCE/deploy/hosted/verify-host.sh" /usr/local/sbin/verify-beads-host

systemctl daemon-reload
systemctl enable beads-ui.service beads-mcp.service beads-watcher-touch.timer
systemctl restart beads-ui.service beads-mcp.service
systemctl restart beads-watcher-touch.timer
nginx -t
systemctl reload nginx
printf 'Repaired the beads160 UI path, watcher marker glob, backup and host verification files; project data was left in place.\n'
