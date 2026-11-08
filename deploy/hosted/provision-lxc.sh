#!/usr/bin/env bash
set -Eeuo pipefail

CTID=11200160
HOSTNAME=beads160
IP_CIDR=192.168.200.160/24
IP=192.168.200.160
PVE_HOST=pve21
TEMPLATE=local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst
TEMPLATE_FILE=/var/lib/vz/template/cache/ubuntu-24.04-standard_24.04-2_amd64.tar.zst

fail() { printf 'provision-lxc: %s\n' "$*" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || fail 'run as root on the Proxmox host.'
[[ $(hostname -s) == "$PVE_HOST" ]] || fail "host is $(hostname -s), expected $PVE_HOST; no changes made."
for tool in pct qm pvesh ip awk grep find; do command -v "$tool" >/dev/null || fail "$tool is unavailable."; done
[[ -d /etc/pve/lxc && -d /etc/pve/qemu-server ]] || fail 'Proxmox LXC/QEMU config directories are unavailable.'
[[ -f "$TEMPLATE_FILE" ]] || fail "cached template missing: $TEMPLATE_FILE"

# Refuse any extant object with this VMID, in either manager or config tree.
for configs in /etc/pve/lxc /etc/pve/qemu-server; do
  config_list=$(find "$configs" -maxdepth 1 -type f -name '*.conf' -print) || fail "could not enumerate configs in $configs."
  while IFS= read -r config; do
    [[ -n "$config" ]] || continue
    [[ ${config##*/} != "$CTID.conf" ]] || fail "VMID $CTID already has a config; refusing to adopt it."
    if grep -Fq -- "$IP" "$config"; then fail "IP $IP appears in $config; refusing to continue."; else
      status=$?; [[ $status -eq 1 ]] || fail "could not inspect $config for address use."
    fi
  done <<<"$config_list"
done
pct_rows=$(pct list) || fail 'could not read the LXC list.'
qm_rows=$(qm list) || fail 'could not read the QEMU list.'
awk -v id="$CTID" 'NR>1 && $1==id {found=1} END {exit !found}' <<<"$pct_rows" && fail "CTID $CTID already exists in pct."
awk -v id="$CTID" 'NR>1 && $1==id {found=1} END {exit !found}' <<<"$qm_rows" && fail "VMID $CTID already exists in qm."
nextid=$(pvesh get /cluster/nextid --vmid "$CTID") || fail 'could not verify requested VMID with pvesh.'
[[ $nextid == "$CTID" ]] || fail "pvesh reports next free VMID $nextid, not requested $CTID."

host_addresses=$(ip -o -4 addr show) || fail 'could not inspect Proxmox host addresses.'
awk -v ip="$IP" '$4 ~ ("^" ip "/") {found=1} END {exit !found}' <<<"$host_addresses" && fail "IP $IP is assigned on the Proxmox host."
ip link show vmbr0 >/dev/null 2>&1 || fail 'vmbr0 is unavailable.'
command -v arping >/dev/null || fail 'arping is required to check the target address.'
if ! arping -D -q -I vmbr0 -c 3 -w 4 "$IP"; then fail "duplicate-address probe reports $IP occupied or could not verify it."; fi

printf 'Creating new unprivileged CT %s (%s) on vmbr0 at %s.\n' "$CTID" "$HOSTNAME" "$IP_CIDR"
pct create "$CTID" "$TEMPLATE" \
  --hostname "$HOSTNAME" --unprivileged 1 --cores 4 --memory 4096 --swap 1024 \
  --rootfs local-lvm:32 --onboot 1 \
  --net0 "name=eth0,bridge=vmbr0,ip=${IP_CIDR},gw=192.168.200.2" \
  --nameserver '192.168.200.13 192.168.200.12 192.168.200.11' --features nesting=0
pct start "$CTID"
printf 'CT %s created and started. No cleanup or rollback was attempted.\n' "$CTID"
