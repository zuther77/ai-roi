# worker/setup.md — Connect a worker to the master and prove it with a test file

**Scope:** one-time machine setup — the master's address, its NFS server and
export, the worker's NFS client mount, and a probe that proves the two can
exchange a file. Building and running the worker processes themselves is
[`README.md`](README.md), which does not repeat anything on this page.

Goal: any worker — Windows, macOS, or Linux — connects to the master over
the regular LAN/Wi-Fi (design spec Section 3.5, v0.4.1) and can WRITE a
test file to the master's shared tracks directory over NFS. If the test
file appears on the master, the worker has everything the radio needs:
generation output lands in the master's `/srv/radio/tracks` exactly the
same way. Budget ~10 minutes, one time per worker.

Addressing (spec v0.4.2 — near-zero bookkeeping):

| Machine | Address | Notes |
|---|---|---|
| Master (Linux box) | `$MASTER_IP` | static, set at the OS level (Steps 1-2); your choice, never hardcoded in the repo |
| Any worker | plain DHCP | workers are never referenced by address — they connect to the master and self-register (worker registry); no router configuration for anyone |

One address in the whole system: the master's. It is the single value you
choose (`MASTER_IP`); the repo has no address of its own, so nothing needs
editing when you change routers or networks. Every command below uses shell
variables, defined once in Step 1.

---

## Part 1 — Master side (once, needs sudo)

**Step 1 — Read your network, then name the three values.**

```sh
ip route                 # "default via X" is your router (GATEWAY)
ip -4 -brief addr        # your LAN interface and its current address
```

The master's address must be **inside the subnet your router serves**. A
static address from a different subnet cannot reach the router, so the
machine loses the internet and the LAN. This happened here after a router
swap changed the network from `192.168.1.x` to `10.0.0.x`. Pick a free
address outside the router's DHCP pool, in the same subnet as the gateway.
Example values (use your own):

```sh
export MASTER_IP=10.0.0.210       # free address on your router's subnet
export GATEWAY=10.0.0.1           # the "default via" address above
export LAN_SUBNET=10.0.0.0/24     # your network; /24 is the common home size
```

Keep this shell open for Parts 1-3, or re-run these three lines in a new one.
Put the same `MASTER_IP` in the repo's `.env` too (Redis is published on it).

**Step 2 — Fix the master's address at the OS level (no router).** The
architecture is pull-based: workers connect TO the master (Redis, NFS)
and self-register, so a worker's own address never matters. Only the
master needs to be stable:

```sh
nmcli con show --active                  # note your Wi-Fi connection name
nmcli con mod "<your-wifi-connection>" ipv4.method manual \
  ipv4.addresses "$MASTER_IP/24" ipv4.gateway "$GATEWAY" ipv4.dns "$GATEWAY"
nmcli con up "<your-wifi-connection>"
ip -4 -brief addr                        # confirm it now holds $MASTER_IP
ping -c2 "$GATEWAY" && ping -c2 github.com   # router reachable, names resolve
```

`ipv4.dns` points at the router. A manual address takes nothing from DHCP,
so without a DNS entry the machine would have no resolver at all. The router
forwards lookups to whichever ISP you are on, so this survives a provider
change. (The GUI equivalent: Settings -> Wi-Fi -> your network -> IPv4 ->
Manual.)

**Step 3 — Install and configure the NFS server** (skip the install line
if already done — it is on this master):

```sh
sudo apt-get install -y nfs-kernel-server
sudo mkdir -p /srv/radio/tracks
```

The tracks directory is deliberately `/tmp`-shaped: sticky (`1777`), so
any worker's user id can write and squashed root too, while users can't
delete each other's files:

```sh
sudo chmod 1777 /srv/radio/tracks
```

**Step 4 — Export the directory to the LAN.** Check what is already
exported first:

```sh
cat /etc/exports
```

A line left over from an earlier network (a single old address, or an old
subnet after a router change) is harmless but dead. Remove it whenever
convenient; it does not grant access on the new network.

Export to the **whole LAN subnet** — accepted by the owner (2026-10-04,
spec v0.4.2): this system targets single-household Wi-Fi, root_squash +
the 1777 directory confine any LAN device to at most writing junk into
tracks, and no new-worker bookkeeping ever happens. Add the line if your
`/etc/exports` lacks one for the LAN:

```sh
echo "/srv/radio/tracks ${LAN_SUBNET}(rw,sync,no_subtree_check,insecure)" | sudo tee -a /etc/exports
sudo exportfs -ra
```

Why `insecure`: the Windows worker's WSL2 layer NATs the NFS client's
source port to an unprivileged one on every network (not just Wi-Fi), and
Linux's nfsd refuses non-privileged ports without this option. Confirmed
live: the Windows box's mount said "access denied by server" until `insecure`
was added (Sprint 2).

**Step 5 — Verify the server is listening and exporting:**

```sh
showmount -e "$MASTER_IP"
# must list: /srv/radio/tracks  <your LAN_SUBNET>
ss -tln | grep -E ':(2049|111)\b'      # nfsd + portmapper listening
```

`showmount` is a convenience check, not the real test. If it prints
`RPC: Unable to receive`, rpcbind or mountd is stopped or firewalled
(`systemctl enable --now rpcbind nfs-kernel-server`, then Step 6); an
NFSv4-only server also fails it while mounting works. The real proof is the
mount and the probe file in Parts 2-3.

**Step 6 — Firewall check** (only if you run one; this master did not
have one blocking LAN by default):

```sh
sudo ufw status
# if "active", allow the LAN subnet (workers are DHCP; their IPs are not fixed):
sudo ufw allow from "$LAN_SUBNET" to any port 2049
sudo ufw allow from "$LAN_SUBNET" to any port 111      # portmapper: NFSv3 + showmount
```

The master side is done. Leave Redis to `docker compose up -d` as usual —
a worker that can also reach the queue will additionally pass
`nc -vz "$MASTER_IP" 6379` (bonus check, not needed for the NFS test).

---

## Part 2 — Worker side, per OS

On the worker, set the master's address once per shell (the same
`MASTER_IP` as Step 1; the worker needs no other value):

```sh
export MASTER_IP=10.0.0.210
```

### Windows worker (Windows Home: WSL2 is the NFS client)

Windows Home has no native NFS client; the WSL2 distro does (its kernel
ships the NFS client — verified). In a WSL terminal:

```sh
sudo apt-get update && sudo apt-get install -y nfs-common   # mount helper
sudo mkdir -p /mnt/radio-tracks
sudo mount -t nfs "$MASTER_IP:/srv/radio/tracks" /mnt/radio-tracks
findmnt /mnt/radio-tracks        # proves the mount is live
```

Notes, both learned the hard way on this project:
- The mount must be re-done after each WSL restart (add the `mount`
  line to `/etc/fstab` to automate, with the master's literal address since
  fstab does not expand shell variables: `<master-ip>:/srv/radio/tracks
  /mnt/radio-tracks nfs defaults 0 0`).
- **Docker containers on this machine cannot bind-mount this path**
  (`timed out waiting ... to be automounted` — a Docker Desktop WSL2
  limitation): the *worker container* mounts the NFS export inside
  itself instead. That is the Windows runbook in `README.md`; this
  page tests the machine-level mount.

This machine also runs the containerized worker next to a containerized model
server, so confirm Docker can see the GPU before leaving this page:

```sh
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

### macOS worker (native, no WSL)

macOS has no `/mnt`, and `/` is a read-only system volume, so
`mkdir /mnt/...` fails with `Read-only file system`. Mount under
`/Volumes`, which is the writable place macOS already uses for mounts.

`vers=3` matches the export this project already proved (the `insecure`
option is an NFSv3 source-port rule). NFSv4 on macOS often fails later
with a permission error that looks like a bad export.

```sh
sudo mkdir -p /Volumes/radio-tracks
sudo mount_nfs -o vers=3 "$MASTER_IP:/srv/radio/tracks" /Volumes/radio-tracks
# if the mount is refused, retry with a privileged source port:
sudo mount_nfs -o vers=3,resvport "$MASTER_IP:/srv/radio/tracks" /Volumes/radio-tracks
mount | grep radio-tracks        # proves the mount is live
```

macOS ships the NFS client; nothing to install. The mount does not
survive a reboot — re-run the `mkdir` and `mount_nfs` lines. `resvport`
is only needed if the server's `insecure` option is removed.

### Linux worker (native)

```sh
sudo apt-get install -y nfs-common       # or nfs-utils on some distros
sudo mkdir -p /mnt/radio-tracks
sudo mount -t nfs "$MASTER_IP:/srv/radio/tracks" /mnt/radio-tracks
findmnt /mnt/radio-tracks
```

---

## Part 3 — The test file

Set `TRACKS` to the mount path from Part 2, then the rest is the same
on every OS:

| OS | `TRACKS` |
|---|---|
| Windows (WSL) and Linux | `/mnt/radio-tracks` |
| macOS | `/Volumes/radio-tracks` |

```sh
TRACKS=/mnt/radio-tracks          # macOS: TRACKS=/Volumes/radio-tracks

echo "hello from $(hostname)" > "$TRACKS/.probe-$(hostname)"
cat "$TRACKS/.probe-$(hostname)"     # worker reads back its own file
```

On the **master**, verify it arrived. Use the hostname the worker
printed — do not leave a `$()` placeholder; the shell would try to run
it as a command:

```sh
ls -la /srv/radio/tracks/ | grep probe
cat /srv/radio/tracks/.probe-HOSTNAME
```

Then prove the reverse direction (worker sees what the master writes):

```sh
# on the master:
echo "hello from master" > /srv/radio/tracks/.probe-master
# on the worker (TRACKS still set as above):
cat "$TRACKS/.probe-master"
```

Clean up from either side (`rm "$TRACKS"/.probe-*` on the worker, or
`rm /srv/radio/tracks/.probe-*` on the master).

**Pass criteria:** the worker-written file is visible and readable on the
master's `/srv/radio/tracks`, and the master-written file is readable on
the worker — over the LAN/Wi-Fi, no cables. This same path is how a
worker's generated tracks reach the radio (with the atomic
temp-write-then-`rename()` pattern — see `README.md`).

---

## Part 4 — Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Master has no internet after setting the static address, or workers lost the master after a router change | The address is not in the subnet the router serves, so the machine cannot reach its gateway | Compare `ip route` with `MASTER_IP`; choose a free address on the gateway's subnet (Step 1), redo Step 2, update `/etc/exports` (Step 4) and every worker's `MASTER_IP` |
| `access denied by server while mounting` | LAN-subnet export line missing, `insecure` missing, or `exportfs -ra` not run | Add the line for your `LAN_SUBNET` with `insecure`, re-export; re-check with `showmount -e` |
| `bad option; ... need a /sbin/mount.<type> helper` | `nfs-common` not installed (classic WSL2 first attempt) | `sudo apt-get install -y nfs-common` |
| `mount.nfs: Connection timed out` | Wrong IP, worker on a different subnet/VLAN (guest Wi-Fi), or firewall | `ping $MASTER_IP`, check router isolation settings ("AP/client isolation" off), Step 6 firewall rules |
| `Permission denied` on write | `/srv/radio/tracks` not `1777` on the master | `sudo chmod 1777 /srv/radio/tracks` (root_squash is fine *because* 1777 absorbs uid differences) |
| macOS `Read-only file system` creating the mount directory | `/mnt` does not exist and `/` cannot be written | use `/Volumes/radio-tracks` (Part 2, macOS) |
| macOS `mount_nfs` refused | privileged-port requirement, or NFSv4 negotiation | retry with `-o vers=3,resvport` |
| Mounted earlier, `Stale file handle` now | Server re-exported (`exportfs -ra`) or rebooted underneath the mount | Re-run the mount command |
| Docker: `timed out waiting ... to be automounted` | Docker Desktop WSL2 cannot pass a WSL-side NFS path into containers — a Windows-specific quirk, not a broken setup | The machine mount above stays for manual use; containers mount the export themselves (runbook in `README.md`) |
