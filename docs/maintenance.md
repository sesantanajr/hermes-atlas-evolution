# Maintenance: Hermes' health and the operating system

[Português (Brasil)](pt-BR/maintenance.md)

Two screens of the menu (`hermes evolution`, screens 12 and 13), the **Health** tab of the dashboard panel, and two commands, `hermes evolution care` and `hermes evolution system`. Neither uses the agent; neither spends a token except where it says so.

## Hermes' health

The learning pass reads it at most every 6 hours, and the menu on demand, from what Hermes already writes, with no network, no model and no `hermes` command:

| Reading | From | Needs attention when |
|---|---|---|
| The gateway | `gateway_state.json`, `state/gateway.heartbeat` | it says it runs but its heartbeat is older than 3 minutes (stuck); its memory is more than twice its median of the last week and above 512 MB |
| The session database | `state.db` (read-only), `state.db-wal` | it cannot be read; `PRAGMA quick_check` (once a week) fails; the write-ahead log passes 64 MB |
| The disk Hermes writes to | `statvfs` of the profile's folder | less than 256 MB free (Hermes' own floor) or 1 GB; at the pace of the last week it fills within 14 days |
| Each folder of the profile | `sessions`, `logs`, `checkpoints`, `cache`, `tools`, `backups`, `skills`, `memories`… (at most 8 seconds of scanning) | it grew by 1 GB and by half in about a week |
| The machine | `/proc/meminfo`, the load average | less than 10% of memory free; swap more than 80% used; load above twice the CPUs over 15 minutes |
| Hermes' tools | `tools/facts.json` | versions of Hermes-managed tools nothing references take 300 MB or more (only names Hermes manages count) |
| After a reboot | systemd | the gateway runs as a user service without linger: it would not come back |

What fixes things is the owner's, from the menu (screen 12) or `hermes evolution care <action> --yes`, each one Hermes' own command:

| Action | Command | Notes |
|---|---|---|
| Installation integrity | `hermes pm doctor` | offline: checks every Hermes-managed file against its recorded digest; runs before Hermes' own startup maintenance |
| Hermes' full check | `hermes doctor` | calls the configured providers' APIs (and pings an endpoint ending in `/anthropic` with a one-token request); no agent |
| Repair with Hermes | `hermes backup --quick --label atlas-evolution`, then `hermes doctor --fix` | the snapshot comes first; nothing runs if it fails; Hermes restores it with `/snapshot restore` |
| Free old tool versions | `hermes pm gc` | removes store entries nothing references |

## The operating system

Linux with apt (Ubuntu, Debian). Every other system reads as "not supported" and nothing is run.

**Check** (read-only; daily in the learning pass, and on each visit to screen 13): updates waiting (security ones counted apart), a reboot the system waits for and the packages that asked for it, `dpkg --audit` (an update left half-way), `apt-get check` without apt's lock (it never makes `unattended-upgrades` wait), another package manager running now (the `unattended-upgrade-shutdown` helper that waits for a shutdown does not count), held packages, room on `/`, `/boot` and `/var`, automatic security updates, the age of the package lists, Hermes' systemd units and the linger a user unit needs to start at boot. It is ready to update when nothing is half-way, broken or busy and there is room.

**Plan**: `apt-get update`, then a simulation of `apt-get --with-new-pkgs upgrade` (or `full-upgrade` with `--full`): what would be upgraded, installed, removed or kept back, and the packages that need a word: restarts the containers (docker, containerd), needs a reboot (libc6, systemd, the kernel, openssl), restarts the web server, remote access (ssh, sudo), the boot loader, the package manager itself.

**Update** (root, in the owner's own terminal; never the agent, never by itself):

1. The check must pass.
2. A copy of every installed package with its version and of `/etc`, owner-only, in `<profile>/evolution/system/<time>/`.
3. `apt-get -y --with-new-pkgs upgrade` in its own systemd unit (`hermes-evolution-os-upgrade-<time>`), so a closed terminal, a dropped SSH connection or a restarted gateway never leaves dpkg half-way. It keeps every configuration file you changed (`--force-confold`), restarts no service by itself (`NEEDRESTART_MODE=l`), and holds the packages you asked to leave out only for this run.
4. After: dpkg and apt checked again, every service that was running checked again (all of them, not only Hermes'), `hermes --version`, and a report with what changed and the command that goes back to each old version (`apt-get install <package>=<version>`, while that version is still available).

The plugin never reboots. When the system waits for a reboot, plan it: Hermes' services come back by themselves when they are enabled (and, for a user unit, with linger).

**Repair**: when an update stopped half-way (a power cut, a killed process), `dpkg --configure -a` and then `apt-get -f install`, keeping your configuration files; refused while another package manager runs.

### How it was tested

In containers (Docker) with Ubuntu 26.04, 24.04 and 22.04 and Debian 13, apart from any production system:

- a full update of Ubuntu 24.04 and 22.04 images of April 2024 (67 and 55 packages, libc6, apt, dpkg and openssl among them), with one package left out (kept at its old version, its hold released after) and an edited configuration file of libc6 (kept);
- a power cut in the middle of the update (the container killed while dpkg unpacked libc6, zlib or libsystemd, then started again): the check finds dpkg half-way and refuses an update, the repair refuses while another apt runs, then finishes the work, and the check is ready again;
- an update of Ubuntu 26.04 with Hermes 0.23.1 installed, through `hermes evolution system upgrade`: libc6 2.43 updated, Hermes answers after, no service stopped.

## The weekly digest

Decisions that wait need something to remind you of them (Hermes 0.23.1 drops end-of-session notices). The menu's suggestion, or `hermes evolution schedule-digest --deliver <local|channel>`, schedules the weekly digest (Mondays at 08:00) as a Hermes cron job that runs a script, not the agent (`--no-agent`): it spends no token. Once only; undone from the history (`hermes cron remove`).
