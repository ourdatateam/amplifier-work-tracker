#!/usr/bin/env bash
set -uo pipefail

ROOT=/opt/beads
EXPECTED_HOST=beads160
failures=0
check() {
  label=$1; shift
  if "$@"; then printf 'PASS %s\n' "$label"; else printf 'FAIL %s\n' "$label"; failures=$((failures + 1)); fi
}
contains() { grep -Fq -- "$2" "$1"; }

check 'hostname is beads160' test "$(hostname -s)" = "$EXPECTED_HOST"
check 'bd pin 1.1.2' contains "$ROOT/installed-bd-version.txt" '1.1.2'
check 'Dolt pin 2.2.3' contains "$ROOT/installed-dolt-version.txt" '2.2.3'
check 'Node pin 22.23.4' contains "$ROOT/installed-node-version.txt" 'v22.23.4'
check 'beads-ui pin 0.12.6' contains "$ROOT/installed-ui-version.txt" 'beads-ui@0.12.6'
check 'runtime Python version recorded' test -s "$ROOT/installed-python-version.txt"
check 'runtime npm version recorded' test -s "$ROOT/installed-npm-version.txt"
check 'raw MCP secrets are root-only' bash -c '[[ $(stat -c "%a:%U:%G" /etc/beads/client-secrets.json) == 600:root:root ]]'
check 'hashed MCP config is beads-readable only' bash -c '[[ $(stat -c "%a:%U:%G" /etc/beads/mcp-tokens.json) == 640:root:beads ]]'
check 'MCP token config has expected client roles' python3 -c 'import json; p=json.load(open("/etc/beads/mcp-tokens.json")); c=p["clients"]; assert {x["actor"] for x in c} == {"codex","claude","pi","grok","human"}; assert all(x["role"] == ("human" if x["actor"] == "human" else "worker") and len(x["token_sha256"]) == 64 for x in c)'
check 'Dolt endpoint bound only to loopback' bash -c "ss -ltn | awk '\$4 ~ /:3308$/ {found=1; if (\$4 != \"127.0.0.1:3308\" && \$4 != \"[::1]:3308\") bad=1} END {exit (!found || bad)}'"

uid=$(id -u beads 2>/dev/null || printf '0')
run_beads_systemctl() {
  runuser -u beads -- env HOME=/var/lib/beads XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user "$@"
}
check_bus() { run_beads_systemctl show-environment >/dev/null; }
check 'reachable beads user bus' check_bus
check 'tracker user service active' run_beads_systemctl is-active --quiet amplifier-work-tracker.service
check 'board service active' systemctl is-active --quiet beads-ui.service
check 'MCP service active' systemctl is-active --quiet beads-mcp.service
check 'watcher compatibility timer active' systemctl is-active --quiet beads-watcher-touch.timer
check 'nginx service active' systemctl is-active --quiet nginx
check 'board HTTP endpoint responds' curl --fail --silent --show-error --max-time 5 http://127.0.0.1:3000/
check 'MCP endpoint responds with auth challenge' bash -c '[[ $(curl --silent --show-error --max-time 5 -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8104/mcp) == 401 ]]'
check 'nginx health endpoint responds' curl --fail --silent --show-error --max-time 5 http://127.0.0.1/healthz
check 'nginx board route responds' curl --fail --silent --show-error --max-time 5 http://127.0.0.1/
check 'nginx MCP route responds with auth challenge' bash -c '[[ $(curl --silent --show-error --max-time 5 -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1/mcp) == 401 ]]'

if (( failures )); then
  printf 'verify-host: %d check(s) failed. Full wrapper doctor is a separate acceptance step.\n' "$failures" >&2
  exit 1
fi
printf 'All host checks passed. Run the full doctor acceptance command from README separately.\n'
