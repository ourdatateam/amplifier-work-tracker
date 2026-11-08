# Hosted Beads deployment files

These files describe the Phase 1 host deployment. They do not prove that the
container, board, MCP adapter, NPM route, or backup restore has been exercised.
The architecture review calls out remaining runtime gates: full wrapper doctor,
the disposable work/custody contract, user-bus survival after reboot, MCP
authentication, board freshness, and NPM access policy.

## Deployment sequence

1. Prepare the reviewed deployment commit based on the upstream fork revision
   pinned in `versions.env`, plus the three controller-approved archives
   with their `SHA256SUMS` manifest. Do not download or replace artifacts on
   the host. Their measured SHA-256 pins and expected fork revision are in
   `versions.env`. `SOURCE_REVISION` is an immutable upstream base pin:
   installation requires it to be an ancestor of the staged commit, then
   records the actual staged HEAD and SHA-256 hashes of the staged deployment
   and hosted MCP files in `/opt/beads/deployed-source-manifest.json`. Stage a
   clean committed checkout. The installer never pulls or follows a moving
   branch or tag.
2. On Proxmox host `pve21`, recheck the planned ID and address, then run
   `bash deploy/hosted/provision-lxc.sh` as root. It checks the hostname, CTID
   and IP in both LXC and QEMU configs, `pct`/`qm` inventories, and confirms
   `pvesh get /cluster/nextid --vmid 11200160` returns the requested free ID.
   It fails closed on inventory/probe errors, then creates and starts only the
   new unprivileged CT. This run's CT has already been created; rerunning the
   script must refuse it. It never destroys or adopts a container.
3. Inside the new CT, stage the pinned git checkout at `/opt/beads/source`, archives and
   `SHA256SUMS` at `/opt/beads/artifacts`, then run
   `bash /opt/beads/source/deploy/hosted/install-host.sh` as root. Hostname is
   checked before mutation. Staged checksums are checked against reviewed pins
   before archives are extracted. The existing workspace root stays
   `/var/lib/beads/projects`; upstream Workspace adds its own `projects/`
   directory, so the project directories are
   `/var/lib/beads/projects/projects/home` and
   `/var/lib/beads/projects/projects/system`.
4. Review services with `bash /opt/beads/source/deploy/hosted/verify-host.sh`.
   Separately run the full gate as `beads`:

   ```bash
   cd /var/lib/beads
   runuser -u beads -- env HOME=/var/lib/beads \
     XDG_RUNTIME_DIR="/run/user/$(id -u beads)" \
     DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u beads)/bus" \
     PATH=/opt/beads/bin:/opt/beads/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
     AMPLIFIER_WORK_TRACKER_ROOT=/var/lib/beads/projects \
     /opt/beads/venv/bin/amplifier-work-tracker doctor --root /var/lib/beads/projects
   ```

   The repository full doctor gate is 38/38. Do not call this deployment
   accepted until full doctor and the disposable integration checks in the
   Phase 1 plan pass. Keep all checks on this container's own Dolt server.

## Services and network boundary

- The upstream tracker service is installed through the exact CLI order
  `amplifier-work-tracker service install --root <projects> --dolt-host 127.0.0.1 --dolt-port 3308`
  in `beads`' user systemd context. The install waits for its user bus, active
  unit, and loopback Dolt listener before creating projects or starting board
  and MCP services. The generated upstream user unit carries its installer's
  `PATH`; `/opt/beads/bin` is included. Linger makes that manager available at
  boot. Keep its generated unit under the beads home so doctor can inspect it.
- `beads-ui.service` runs the published `beads-ui@0.12.6` Node entry point on
  `127.0.0.1:3000`; it does not build a frontend. `beads-mcp.service` runs the
  separately authored `amplifier_work_tracker.hosted_mcp` adapter on 8104 and
  reads the hashed config at `/etc/beads/mcp-tokens.json`.
- Nginx listens on the CT's private interface at port 80. `/` proxies the board,
  `/mcp` proxies MCP, and `/healthz` returns basic health. Host and WebSocket
  headers are preserved; response buffering is off for streaming. NPM/LAN
  policy must keep this internal. The config creates no DNS, certificate,
  router forward, or public exposure.
- The stock board watches `.beads` filesystem paths while Amp writes to Dolt
  SQL. A five-second systemd timer touches only a marker inside each project's
  `.beads` directory so the watcher rereads state. This compatibility shim
  requires a post-install Amp-to-board freshness and board-to-wrapper readback
  check.

## Credentials and human policy

Random per-client secrets are generated once at
`/etc/beads/client-secrets.json` (root-only, mode 0600). The MCP service reads
the SHA-256 token config at `/etc/beads/mcp-tokens.json` (root:beads, mode
0640). Installation refuses to overwrite either existing file. Retrieve client
values only through an approved root-only channel; never copy values into logs,
source control, shell history, or support messages.

The board is the human administrative interface. Ordinary board edits are
permitted. For held work, release or resolve through the wrapper first; do not
reassign held items in the board and do not force-delete active work. The UI
can mutate Beads directly and does not enforce wrapper custody. Review NPM's
internal access policy before allowing users to reach it.

## Backups

The daily systemd timer invokes `backup.sh`. It records which Amp, UI, MCP and
watcher services were running, stops all running writers before Amp, and the
trap attempts to restart every service that was running even if an earlier
restart fails. A restart failure makes the backup command exit nonzero without
masking an earlier backup failure. It archives the complete
`/var/lib/beads` tree (including `.beads/shared-server/dolt` and projects),
`/etc/beads` secrets, and `/opt/beads` source/commit, version manifest and
installed binaries. It checks tar readability and verifies a SHA-256 sidecar.
After a successful archive and service restart, it keeps the seven newest
checksum-verified archives and their sidecars. Pruning matches only timestamped
`beads-*.tar.gz` archives and their exact sidecars in `/var/backups/beads`.
Archive and sidecar remain root:root mode 0600 there.
Restore into an isolated replacement CT or private Dolt data directory, then
verify expected issue content before cutover. Keep runtime acceptance and
recovery helpers with the backups under `/opt/beads/acceptance`; a checksum
alone does not establish logical recovery. Never restore over a live installation.

If the initial install left the UI pointed at the old directory, copy the
reviewed `deploy/hosted/repair-installed.sh`
into the new container and run it as root. It is guarded to hostname `beads160`,
checks both existing project directories and the active upstream user service,
repairs the UI working directory and watcher path, refreshes backup/verification
files, and reloads Nginx after checking its config. It does not rerun
installation or alter project data or credentials.

## NPM18 proxy host

`npm-proxy.mjs` is run as root inside the live NPM18 guest (CT1120018 on
`pve9`), with working directory `/app` and native Node. It reads certificate
id 6 and proxy-host rows through NPM's `db()` query interface, then uses its
trusted internal `Access` context and proxy-host `create`/`update` API. It
requires the reviewed wildcard certificate and internal-only access-list id 0,
rejects ambiguous shared-domain records, backs up the SQLite DB and any exact
record with mode 0600 before mutation, and verifies the host, `nginx -t`, and
`meta.nginx_online`. The route forwards only `beads.ssiops.com` to
`http://192.168.200.160:80`, with forced TLS, HTTP/2, WebSockets, Host and
Authorization forwarding. Identical config is a no-op. It does not read
credentials, change DNS, or create public ingress. Never start old standby
CT1120018 on pve8.

To run after separately reviewing the artifact, copy this script into the live
guest and run `node /path/to/npm-proxy.mjs` from `/app` as root. This repository
review did not run it against NPM or claim a live deployment.

## Scope boundary

Phase 2 archive, search, catalog, conversation capture, and retrieval remain a
Supabase backlog item. They are not deployed here. No existing work or
repository Beads database is migrated by this host setup.
