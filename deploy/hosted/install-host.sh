#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOSTNAME=beads160
ROOT=/opt/beads
ARTIFACTS=$ROOT/artifacts
PROJECTS=/var/lib/beads/projects
TRACKER_CLI=/opt/beads/venv/bin/amplifier-work-tracker
VERSIONS_FILE=/opt/beads/source/deploy/hosted/versions.env

fail() { printf 'install-host: %s\n' "$*" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || fail 'run as root inside the new container.'
[[ $(hostname -s) == "$EXPECTED_HOSTNAME" ]] || fail "hostname is $(hostname -s), expected $EXPECTED_HOSTNAME; no changes made."
[[ -f "$VERSIONS_FILE" ]] || fail "missing pinned versions file: $VERSIONS_FILE"
# shellcheck disable=SC1090
source "$VERSIONS_FILE"
[[ -f "$ROOT/source/pyproject.toml" && -d "$ROOT/source/modules/tool-work-tracker" && -e "$ROOT/source/.git" ]] || fail 'expected pinned fork checkout at /opt/beads/source.'
[[ -f "$ARTIFACTS/SHA256SUMS" ]] || fail 'controller-staged SHA256SUMS is required.'
[[ ! -e "$PROJECTS/projects/home" && ! -e "$PROJECTS/projects/system" ]] || fail 'home/system project already exists; refusing to adopt existing data.'
for artifact in "$BEADS_ARTIFACT" "$DOLT_ARTIFACT" "$NODE_ARTIFACT"; do
  [[ -f "$ARTIFACTS/$artifact" ]] || fail "missing staged artifact: $artifact"
done

python3 - "$ARTIFACTS/SHA256SUMS" "$BEADS_ARTIFACT" "$BEADS_SHA256" "$DOLT_ARTIFACT" "$DOLT_SHA256" "$NODE_ARTIFACT" "$NODE_SHA256" <<'PY'
import sys
path, *pairs = sys.argv[1:]
expected = dict(zip(pairs[::2], pairs[1::2]))
entries = {}
with open(path, encoding="utf-8") as source:
    for line in source:
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if len(fields) != 2 or fields[1] in entries:
            raise SystemExit("SHA256SUMS must contain exactly three valid entries")
        entries[fields[1]] = fields[0]
if entries != expected:
    raise SystemExit("SHA256SUMS must contain exactly the three reviewed artifact pins")
PY
(cd "$ARTIFACTS" && sha256sum -c SHA256SUMS) || fail 'artifact checksum verification failed.'

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates dbus-user-session git curl xz-utils jq nginx python3 python3-venv \
  python3-pip build-essential pkg-config libffi-dev libssl-dev tar gzip openssl \
  procps iproute2
source_revision=$(git -C "$ROOT/source" rev-parse HEAD) || fail 'could not read staged fork source revision.'
git -C "$ROOT/source" merge-base --is-ancestor "$SOURCE_REVISION" "$source_revision" ||
  fail "staged source $source_revision does not descend from pinned base $SOURCE_REVISION."
[[ -z $(git -C "$ROOT/source" status --porcelain --untracked-files=normal) ]] ||
  fail 'staged source must be a clean committed checkout; installer never pulls or updates it.'
python3 - "$ROOT/source" "$SOURCE_REVISION" "$source_revision" <<'PY'
import hashlib
import json
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
files = sorted((source / "deploy/hosted").glob("*"))
files += [source / "src/amplifier_work_tracker/hosted_mcp.py"]
manifest = {
    "upstream_base_revision": sys.argv[2],
    "staged_revision": sys.argv[3],
    "files": {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
        if path.is_file()
    },
}
if not manifest["files"] or not (source / "src/amplifier_work_tracker/hosted_mcp.py").is_file():
    raise SystemExit("staged deployment/MCP source files are incomplete")
path = pathlib.Path("/opt/beads/deployed-source-manifest.json")
path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o644)
PY

getent passwd beads >/dev/null || useradd --system --create-home --home-dir /var/lib/beads --shell /bin/bash beads
install -d -o beads -g beads -m 0750 /var/lib/beads /var/lib/beads/projects
install -d -o root -g root -m 0755 /opt/beads/bin /opt/beads/artifacts
install -d -o root -g root -m 0755 /opt/beads/node /opt/beads/ui
install -d -o root -g root -m 0755 /etc/beads

tmp=$(mktemp -d /tmp/beads-install.XXXXXX)
trap 'rm -rf "$tmp"' EXIT
bd_member=$(tar -tzf "$ARTIFACTS/$BEADS_ARTIFACT" | awk '{gsub(/^\.\//, ""); if($0=="bd") {n++; found=$0}} END {if(n==1) print found; else exit 1}') || fail 'Beads archive layout must contain one top-level bd binary.'
dolt_member=$(tar -tzf "$ARTIFACTS/$DOLT_ARTIFACT" | awk '{gsub(/^\.\//, ""); if($0=="dolt-linux-amd64/bin/dolt") {n++; found=$0}} END {if(n==1) print found; else exit 1}') || fail 'Dolt archive layout must contain dolt-linux-amd64/bin/dolt.'
node_root="node-v${NODE_VERSION}-linux-x64"
tar -tJf "$ARTIFACTS/$NODE_ARTIFACT" | awk -v root="$node_root" '{gsub(/^\.\//, "")} index($0, root "/")==1 {found=1} END {exit !found}' || fail "Node archive layout does not contain $node_root/."
for archive in "$BEADS_ARTIFACT" "$DOLT_ARTIFACT"; do tar -tzf "$ARTIFACTS/$archive" | awk 'substr($0,1,1)=="/" || $0 ~ /(^|\/)\.\.($|\/)/ {bad=1} END {exit bad}' || fail "unsafe member path in $archive."; done
tar -xzf "$ARTIFACTS/$BEADS_ARTIFACT" --no-same-owner -C "$tmp"
install -o root -g root -m 0755 "$tmp/$bd_member" /opt/beads/bin/bd
tar -xzf "$ARTIFACTS/$DOLT_ARTIFACT" --no-same-owner -C "$tmp"
install -o root -g root -m 0755 "$tmp/$dolt_member" /opt/beads/bin/dolt
tar -xJf "$ARTIFACTS/$NODE_ARTIFACT" --no-same-owner -C /opt/beads/node
ln -sfn "/opt/beads/node/node-v${NODE_VERSION}-linux-x64/bin/node" /opt/beads/bin/node
ln -sfn "/opt/beads/node/node-v${NODE_VERSION}-linux-x64/bin/npm" /opt/beads/bin/npm
ln -sfn "/opt/beads/node/node-v${NODE_VERSION}-linux-x64/bin/npx" /opt/beads/bin/npx
export PATH=/opt/beads/bin:/opt/beads/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

python3 -m venv /opt/beads/venv
/opt/beads/venv/bin/python -m pip install --upgrade pip
/opt/beads/venv/bin/python -m pip install \
  '/opt/beads/source[web,hosted]' \
  -e /opt/beads/source/modules/tool-work-tracker
chown -R root:root /opt/beads/venv
chmod -R a+rX /opt/beads/venv
PATH=/opt/beads/bin:/opt/beads/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  npm --prefix /opt/beads/ui install --omit=dev --save-exact "beads-ui@${BEADS_UI_VERSION}"

cat >/etc/profile.d/beads.sh <<'EOF'
export PATH=/opt/beads/bin:/opt/beads/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export AMPLIFIER_WORK_TRACKER_ROOT=/var/lib/beads/projects
export DOLT_HOST=127.0.0.1
export DOLT_PORT=3308
EOF
chmod 0644 /etc/profile.d/beads.sh

cat >/etc/systemd/system/beads-ui.service <<'EOF'
[Unit]
Description=Beads Kanban UI
After=network.target

[Service]
Type=simple
User=beads
Group=beads
Environment=HOME=/var/lib/beads
Environment=PATH=/opt/beads/bin:/opt/beads/node/node-v22.23.4-linux-x64/bin:/usr/local/bin:/usr/bin:/bin
Environment=AMPLIFIER_WORK_TRACKER_ROOT=/var/lib/beads/projects
Environment=AMPLIFIER_WORK_TRACKER_DOLT_HOST=127.0.0.1
Environment=AMPLIFIER_WORK_TRACKER_DOLT_PORT=3308
Environment=DOLT_HOST=127.0.0.1
Environment=DOLT_PORT=3308
Environment=HOST=127.0.0.1
Environment=PORT=3000
WorkingDirectory=/var/lib/beads/projects/projects/home
ExecStart=/opt/beads/bin/node /opt/beads/ui/node_modules/beads-ui/server/index.js
ExecStartPost=/opt/beads/venv/bin/python /opt/beads/source/deploy/hosted/register-workspaces.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/beads

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/beads-mcp.service <<'EOF'
[Unit]
Description=Hosted Beads MCP adapter
After=network.target

[Service]
Type=simple
User=beads
Group=beads
Environment=HOME=/var/lib/beads
Environment=PATH=/opt/beads/bin:/opt/beads/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=AMPLIFIER_WORK_TRACKER_ROOT=/var/lib/beads/projects
Environment=AMPLIFIER_WORK_TRACKER_DOLT_HOST=127.0.0.1
Environment=AMPLIFIER_WORK_TRACKER_DOLT_PORT=3308
Environment=DOLT_HOST=127.0.0.1
Environment=DOLT_PORT=3308
Environment=BEADS_MCP_HOST=127.0.0.1
Environment=BEADS_MCP_TOKEN_FILE=/etc/beads/mcp-tokens.json
Environment=BEADS_MCP_PORT=8104
ExecStart=/opt/beads/venv/bin/python -m amplifier_work_tracker.hosted_mcp
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadOnlyPaths=/etc/beads/mcp-tokens.json
ReadWritePaths=/var/lib/beads

[Install]
WantedBy=multi-user.target
EOF

cat >/usr/local/sbin/beads-touch-watch-markers <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s nullglob
for project in /var/lib/beads/projects/projects/*; do
  [[ -d "$project/.beads" ]] || continue
  touch "$project/.beads/.hosted-watcher-marker"
done
EOF
chmod 0755 /usr/local/sbin/beads-touch-watch-markers
cat >/etc/systemd/system/beads-watcher-touch.service <<'EOF'
[Unit]
Description=Refresh beads-ui directory watcher for Dolt-backed projects
After=beads-ui.service

[Service]
Type=oneshot
User=beads
Group=beads
ExecStart=/usr/local/sbin/beads-touch-watch-markers
EOF
cat >/etc/systemd/system/beads-watcher-touch.timer <<'EOF'
[Unit]
Description=Refresh beads-ui watcher markers every five seconds

[Timer]
OnBootSec=5s
OnUnitActiveSec=5s
AccuracySec=1s
Unit=beads-watcher-touch.service

[Install]
WantedBy=timers.target
EOF

install -o root -g root -m 0644 /opt/beads/source/deploy/hosted/nginx-beads.conf /etc/nginx/sites-available/beads
ln -sfn /etc/nginx/sites-available/beads /etc/nginx/sites-enabled/beads
rm -f /etc/nginx/sites-enabled/default
cat >/etc/nginx/conf.d/beads-websocket-map.conf <<'EOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
EOF
nginx -t
install -o root -g root -m 0755 /opt/beads/source/deploy/hosted/backup.sh /usr/local/sbin/backup-beads
cat >/etc/systemd/system/beads-backup.service <<'EOF'
[Unit]
Description=Cold, verified backup of hosted Beads data

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/backup-beads
EOF
cat >/etc/systemd/system/beads-backup.timer <<'EOF'
[Unit]
Description=Daily hosted Beads backup

[Timer]
OnCalendar=daily
RandomizedDelaySec=20m
Persistent=true
Unit=beads-backup.service

[Install]
WantedBy=timers.target
EOF

# Create per-client credentials once. Never replace partial or existing config.
if [[ -e /etc/beads/client-secrets.json || -e /etc/beads/mcp-tokens.json ]]; then
  fail 'MCP credential file already exists; refusing to replace either credential file.'
fi
umask 077
python3 - <<'PY'
import hashlib, json, secrets
actors = ("codex", "claude", "pi", "grok", "human")
raw = {actor: secrets.token_urlsafe(36) for actor in actors}
clients = [
    {"actor": actor, "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "role": "human" if actor == "human" else "worker"}
    for actor, token in raw.items()
]
with open("/etc/beads/client-secrets.json", "x", encoding="utf-8") as f:
    json.dump(raw, f, indent=2)
    f.write("\n")
with open("/etc/beads/mcp-tokens.json", "x", encoding="utf-8") as f:
    json.dump({"clients": clients}, f, indent=2)
    f.write("\n")
PY
chown root:root /etc/beads/client-secrets.json
chmod 0600 /etc/beads/client-secrets.json
chown root:beads /etc/beads/mcp-tokens.json
chmod 0640 /etc/beads/mcp-tokens.json

install -o root -g root -m 0644 /opt/beads/source/deploy/hosted/versions.env /opt/beads/versions.env
python3 --version > /opt/beads/installed-python-version.txt
/opt/beads/bin/bd version > /opt/beads/installed-bd-version.txt
/opt/beads/bin/dolt version > /opt/beads/installed-dolt-version.txt
/opt/beads/bin/node --version > /opt/beads/installed-node-version.txt
PATH="$PATH" /opt/beads/bin/npm --version > /opt/beads/installed-npm-version.txt
PATH="$PATH" /opt/beads/bin/npm --prefix /opt/beads/ui list --depth=0 > /opt/beads/installed-ui-version.txt
chmod 0644 /opt/beads/installed-*-version.txt

getent group systemd-journal >/dev/null && usermod -a -G systemd-journal beads || :
loginctl enable-linger beads
uid=$(id -u beads)
systemctl start "user@${uid}.service"
for attempt in $(seq 1 30); do
  [[ -S "/run/user/$uid/bus" ]] && break
  sleep 1
done
[[ -S "/run/user/$uid/bus" ]] || fail 'beads user systemd bus did not become reachable.'
runuser -u beads -- env HOME=/var/lib/beads XDG_RUNTIME_DIR="/run/user/$uid" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
  PATH=/opt/beads/bin:/opt/beads/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  AMPLIFIER_WORK_TRACKER_ROOT="$PROJECTS" DOLT_HOST=127.0.0.1 DOLT_PORT=3308 \
  /opt/beads/venv/bin/amplifier-work-tracker service install --root "$PROJECTS" --dolt-host 127.0.0.1 --dolt-port 3308

runuser -u beads -- env HOME=/var/lib/beads XDG_RUNTIME_DIR="/run/user/$uid" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
  systemctl --user show-environment >/dev/null
TRACKER_UNIT=/var/lib/beads/.config/systemd/user/amplifier-work-tracker.service
[[ -f "$TRACKER_UNIT" ]] || fail 'upstream tracker unit was not written in beads user context.'
grep -Fq 'Environment=PATH=/opt/beads/bin:' "$TRACKER_UNIT" || fail 'generated tracker unit PATH cannot find /opt/beads/bin tools.'
grep -Fq "serve --root $PROJECTS" "$TRACKER_UNIT" || fail 'generated tracker unit does not serve the selected projects root.'
runuser -u beads -- env HOME=/var/lib/beads XDG_RUNTIME_DIR="/run/user/$uid" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user is-active --quiet amplifier-work-tracker.service ||
  fail 'tracker user service did not become active after install.'

for attempt in $(seq 1 30); do
  ss -ltn | awk '$4 ~ /^127\.0\.0\.1:3308$/ {found=1} END {exit !found}' && break
  sleep 2
done
ss -ltn | awk '$4 ~ /^127\.0\.0\.1:3308$/ {found=1} END {exit !found}' || fail 'tracker did not bring Dolt up on 127.0.0.1:3308.'

create_workspace_project() {
  local name=$1
  [[ ! -e "$PROJECTS/projects/$name" ]] || fail "project path already exists: $PROJECTS/projects/$name"
  runuser -u beads -- env HOME=/var/lib/beads XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    PATH=/opt/beads/bin:/opt/beads/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    AMPLIFIER_WORK_TRACKER_ROOT="$PROJECTS" DOLT_HOST=127.0.0.1 DOLT_PORT=3308 \
    "$TRACKER_CLI" new "$name" --root "$PROJECTS"
}
for name in home system; do
  create_workspace_project "$name"
done

systemctl daemon-reload
systemctl enable --now nginx beads-ui.service beads-mcp.service beads-watcher-touch.timer beads-backup.timer
systemctl reload nginx
printf 'Installed pins: bd %s; Dolt %s; Node %s; beads-ui %s. Runtime versions recorded under /opt/beads/.\n' \
  "$BEADS_VERSION" "$DOLT_VERSION" "$NODE_VERSION" "$BEADS_UI_VERSION"
printf 'MCP raw credentials: root-only /etc/beads/client-secrets.json (values were not displayed).\n'
