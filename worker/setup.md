# worker/setup.md — Connect a worker to the master and prove it with a test file

Goal: any worker — Windows, macOS, or Linux — connects to the master over
the regular LAN/Wi-Fi (design spec Section 3.5, v0.4.1) and can WRITE a
test file to the master's shared tracks directory over NFS. If the test
file appears on the master, the worker has everything the radio needs:
generation output lands in the master's `/srv/radio/tracks` exactly the
same way. Budget ~10 minutes, one time per worker.

Addressing (spec v0.4.2 — near-zero bookkeeping):

| Machine | Address | Notes |
|---|---|---|
| Master (Linux box) | `192.168.1.210` | static, set at the OS level (Step 2) |
| Any worker | plain DHCP | workers are never referenced by address — they connect to the master and self-register (worker registry); no router configuration for anyone |

One address in the whole system: the master's. Everything below assumes
it; substitute if you choose a different one.

---

## Part 1 — Master side (once, needs sudo)

**Step 1 — Confirm the master's LAN address on the worker network.**

```sh
ip -4 -brief addr        # the Wi-Fi/LAN interface should show 192.168.1.210/24
ip route                 # default via 192.168.1.1 — that's your home router
```

**Step 2 — Fix the master's address at the OS level (no router).** The
architecture is pull-based: workers connect TO the master (Redis, NFS)
and self-register, so a worker's own address never matters. Only the
master needs to be stable:

```sh
nmcli con show --active                  # note your Wi-Fi connection name
nmcli con mod "<your-wifi-connection>" ipv4.method manual \
  ipv4.addresses 192.168.1.210/24 ipv4.gateway 192.168.1.1 ipv4.dns 192.168.1.1
nmcli con up "<your-wifi-connection>"
ip -4 -brief addr                        # confirm it now holds .210
```

(The GUI equivalent: Settings -> Wi-Fi -> your network -> IPv4 -> Manual.)

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

Export to the **whole LAN subnet** — accepted by the owner (2026-10-04,
spec v0.4.2): this system targets single-household Wi-Fi, root_squash +
the 1777 directory confine any LAN device to at most writing junk into
tracks, and no new-worker bookkeeping ever happens. Add the line if your
`/etc/exports` lacks one for the LAN:

```sh
echo '/srv/radio/tracks 192.168.1.0/24(rw,sync,no_subtree_check,insecure)' | sudo tee -a /etc/exports
sudo exportfs -ra
```

Why `insecure`: the Windows worker's WSL2 layer NATs the NFS client's
source port to an unprivileged one on every network (not just Wi-Fi), and
Linux's nfsd refuses non-privileged ports without this option. Confirmed
live: the DELL's mount said "access denied by server" until `insecure`
was added (Sprint 2).

**Step 5 — Verify the server is listening and exporting:**

```sh
showmount -e 192.168.1.210
# must list: /srv/radio/tracks  192.168.1.50,192.168.1.51 (plus the legacy direct-link client if present)
ss -tln | grep -E ':(2049|111)\b'      # nfsd + portmapper listening
```

**Step 6 — Firewall check** (only if you run one; this master did not
have one blocking LAN by default):

```sh
sudo ufw status
# if "active", allow the workers' IPs:
sudo ufw allow from 192.168.1.0/24 to any port 2049
```

The master side is done. Leave Redis to `docker compose up -d` as usual —
a worker that can also reach the queue will additionally pass
`nc -vz 192.168.1.210 6379` (bonus check, not needed for the NFS test).

---

## Part 2 — Worker side, per OS

### Windows worker (Windows Home: WSL2 is the NFS client)

Windows Home has no native NFS client; the WSL2 distro does (its kernel
ships the NFS client — verified). In a WSL terminal:

```sh
sudo apt-get update && sudo apt-get install -y nfs-common   # mount helper
sudo mkdir -p /mnt/radio-tracks
sudo mount -t nfs 192.168.1.210:/srv/radio/tracks /mnt/radio-tracks
findmnt /mnt/radio-tracks        # proves the mount is live
```

Notes, both learned the hard way on this project:
- The mount must be re-done after each WSL restart (add the `mount`
  line to `/etc/fstab` to automate: `192.168.1.210:/srv/radio/tracks
  /mnt/radio-tracks nfs defaults 0 0`).
- **Docker containers on this machine cannot bind-mount this path**
  (`timed out waiting ... to be automounted` — a Docker Desktop WSL2
  limitation): the *worker container* mounts the NFS export inside
  itself instead. That is the DELL runbook in `worker/README.md`; this
  page tests the machine-level mount.

### macOS worker (native, no WSL)

```sh
sudo mkdir -p /mnt/radio-tracks
sudo mount_nfs 192.168.1.210:/srv/radio/tracks /mnt/radio-tracks
# if the mount is refused, retry with:
sudo mount_nfs -o resvport 192.168.1.210:/srv/radio/tracks /mnt/radio-tracks
mount | grep radio-tracks        # proves the mount is live
```

macOS ships the NFS client; nothing to install. `resvport` asks macOS to
use a privileged source port, needed only if the server's `insecure`
option were ever removed.

### Linux worker (native)

```sh
sudo apt-get install -y nfs-common       # or nfs-utils on some distros
sudo mkdir -p /mnt/radio-tracks
sudo mount -t nfs 192.168.1.210:/srv/radio/tracks /mnt/radio-tracks
findmnt /mnt/radio-tracks
```

---

## Part 3 — The test file (identical steps for every OS)

From the worker's mounted directory:

```sh
echo "hello from $(hostname)" > /mnt/radio-tracks/.probe-$(hostname)
cat /mnt/radio-tracks/.probe-$(hostname)     # worker reads back its own file
```

On the **master**, verify it arrived:

```sh
ls -la /srv/radio/tracks/ | grep probe
cat "/srv/radio/tracks/.probe-$(your-worker-hostname)"   # substitute the name
```

Then prove the reverse direction (worker sees what the master writes):

```sh
# on the master:
echo "hello from master" > /srv/radio/tracks/.probe-master
# on the worker:
cat /mnt/radio-tracks/.probe-master
```

Clean up from either side (`rm /mnt/radio-tracks/.probe-*` or the
master's `rm /srv/radio/tracks/.probe-*`).

**Pass criteria:** the worker-written file is visible and readable on the
master's `/srv/radio/tracks`, and the master-written file is readable on
the worker — over the LAN/Wi-Fi, no cables. This same path is how a
worker's generated tracks reach the radio (with the atomic
temp-write-then-`rename()` pattern — see `worker/README.md`).

---

## Part 4 — Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `access denied by server while mounting` | LAN-subnet export line missing, `insecure` missing, or `exportfs -ra` not run | Add the `192.168.1.0/24` line with `insecure`, re-export; re-check with `showmount -e` |
| `bad option; ... need a /sbin/mount.<type> helper` | `nfs-common` not installed (classic WSL2 first attempt) | `sudo apt-get install -y nfs-common` |
| `mount.nfs: Connection timed out` | Wrong IP, worker on a different subnet/VLAN (guest Wi-Fi), or firewall | `ping 192.168.1.210`, check router isolation settings ("AP/client isolation" off), Step 6 firewall rules |
| `Permission denied` on write | `/srv/radio/tracks` not `1777` on the master | `sudo chmod 1777 /srv/radio/tracks` (root_squash is fine *because* 1777 absorbs uid differences) |
| macOS `mount_nfs` refused | privileged-port requirement | retry with `-o resvport` |
| Mounted earlier, `Stale file handle` now | Server re-exported (`exportfs -ra`) or rebooted underneath the mount | Re-run the mount command |
| Docker: `timed out waiting ... to be automounted` | Docker Desktop WSL2 cannot pass a WSL-side NFS path into containers — a Windows-specific quirk, not a broken setup | The machine mount above stays for manual use; containers mount the export themselves (runbook in `worker/README.md`) |
