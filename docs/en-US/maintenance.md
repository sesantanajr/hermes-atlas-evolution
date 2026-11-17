# Maintenance: Hermes' health and the operating system

**English** | [Português (Brasil)](../pt-BR/maintenance.md)

Where to look: the menu (`hermes evolution`) screens 12 (Hermes health), 13 (Operating system) and 14 (Environment), the **Health** and **Environment** tabs of the dashboard panel, and the commands `hermes evolution care` and `hermes evolution system`. None of them uses the agent, and none spends a token.

## Hermes' health

### What the plugin reads

The learning pass reads this at most every 6 hours, and screen 12 reads it again each time you open it. It comes from what Hermes already writes: no network, no model, no `hermes` command.

| Reading | From | Needs attention when |
|---|---|---|
| The gateway | `gateway_state.json`, `state/gateway.heartbeat` | it says it runs but its heartbeat is older than 3 minutes (stuck); with no fresh heartbeat, its process is gone or its pid now belongs to another program (crashed; checked on POSIX systems only; a fresh heartbeat counts as running, even from a gateway in another PID namespace); its memory is more than twice its median of the last week and above 512 MB |
| The session database | `state.db` (read-only), `state.db-wal` | it cannot be read; `PRAGMA quick_check` (once a week) fails; the write-ahead log passes 64 MB |
| The disk Hermes writes to | `statvfs` of the profile's folder | less than 256 MB free (Hermes' own floor) or less than 1 GB; at the pace of the last week it fills within 14 days |
| Each folder of the profile | `sessions`, `logs`, `checkpoints`, `cache`, `tools`, `backups`, `skills`, `memories`… (at most 8 seconds of scanning) | it grew by 1 GB and by half in about a week |
| The machine | `/proc/meminfo`, the load average | less than 10% of memory free; swap more than 80% used; load above twice the CPUs over 15 minutes |
| Hermes' tools | `tools/facts.json` at the Hermes root (one store for every profile) | versions of Hermes-managed tools that nothing references take 300 MB or more (only names Hermes manages count) |
| After a reboot | systemd | the gateway runs as a user service without linger: it would not come back. Fix: `loginctl enable-linger <user>` |

What needs attention shows on screen 12, in the panel's Health tab and in `hermes evolution status`, each item with the command that fixes it.

### Hermes' own doctor, once a day

`hermes doctor` is Hermes' full check of an installation. The plugin runs its checks for you, without the parts that use the network or reach outside the machine, and turns what it finds into items you can act on.

- **Who runs it.** The profile that looks after the whole Hermes (the lead, see [The environment](evolution-loop.md#the-environment)), in its learning pass, once a day for each profile in use: a profile with sessions in the last 7 days, a profile with the plugin on, and the default profile. A profile nobody uses waits until you ask. A pass runs as many profiles as fit in its time, and only while the machine has at least 600 MB of memory free (a run takes about 210 MB for a few seconds); the others wait for the next pass.
- **How.** In a short-lived process of Hermes' own Python, in a process group of its own, with that profile's home and a clean environment (only `PATH`, `HOME`, the locale and a few system variables pass). Each check is called as `hermes doctor` calls it, without `--fix`; an error in one check, Hermes' own exits included, is caught there, so it never hides the others. Like `hermes doctor`, the run loads the profile's plugins to list their tools. Five checks are skipped: the provider API probes, `npm audit`, the dashboard import, the Skills Hub (they use the network or take minutes) and the terminal backend (it may open an SSH connection or start a container). One run takes about 7 seconds per profile on Hermes 0.23.1. At 120 seconds the whole process group is stopped, so nothing it started outlives it.
- **What it changes.** No setting and no file of yours. Its footprint is the same as `hermes doctor`: Hermes' own logs and lock files.
- **How long a result counts.** A result older than 3 days is no longer shown: the profile went idle, or the runs were skipped. When a change is found stale on disk, that profile's stored result is dropped and the checks run again at the next look.
- **Look again now.** `v` on screen 14, or `hermes evolution environment scan`, runs it again for every profile in use.

What the doctor reports becomes items of the Environment (screen 14 and the panel's Environment tab). Each item says what was seen, what to do, with `hermes -p <profile> ...` commands, and quotes Hermes' own words:

| Item | Seen when | What you do |
|---|---|---|
| Security problem (high risk, first in the list) | the doctor names a security advisory (the item is the advisory's id, such as `security.GHSA-...`) or a suspicious MCP server (`mcp.<name>`) | Act now, following Hermes' text: uninstall the package or remove the MCP server it names, and rotate every credential it may have seen. For an MCP server the command is `hermes -p <profile> mcp remove <name>`; for an advisory, `hermes -p <profile> doctor --ack <id>` closes the alert once you have handled it. |
| A package cache Hermes never cleans | a folder under `cache/` that rebuilds itself when needed (`uv`, `pnpm`, `npm`, `pip`, `yarn`, `huggingface`, `piper-voices`) is large and outside every pruner (Hermes cleans only `cache/scratch` and `cache/terminal`) | Apply it: one key moves it to the plugin's trash (`evolution/trash/<profile>/` of the lead). One folder at a time, never in **Apply all**. **Undo** puts it back; if the folder exists again by then, undo says so and the copy stays in the trash until it is deleted. |
| Any other large cache folder | another folder under `cache/`: `browser` (the browser tool's profile, with its logins and cookies), `documents` (files attached to conversations, which old sessions point to), `images`, `generated`, `audio`, `delegation`, `spillover`, `web`… | Yours: the plugin never moves it, because Hermes or the sessions still use it. The item says what the folder holds and gives the command to look inside, `du -sh <path>/* \| sort -h \| tail -20`; delete only what is no longer needed. |
| Rollback snapshots above Hermes' cap | the `/rollback` snapshots take more than Hermes' size cap | See what takes the space with `hermes -p <profile> checkpoints status`. Hermes always keeps the latest snapshot of each project, so its cap is a floor. Then choose: keep fewer days (`hermes -p <profile> config set checkpoints.retention_days 3`, then `hermes -p <profile> checkpoints prune`) or, if you never use `/rollback`, turn it off (`hermes -p <profile> config set checkpoints.enabled false`, then `hermes -p <profile> checkpoints clear`). Deleted snapshots cannot be brought back. |
| A problem the plugin knows | tools on without a key, a missing `.env`, no provider key, a session database to prune, repair or recover, linger, `config.yaml` to migrate or with settings in the wrong place, an old `HERMES_MAX_ITERATIONS`, the memory provider off, a plugin turned off by an update or in both lists, cron tasks being skipped or their disk almost full, one gateway per profile, two profiles with the same chat credential, `hermes` not on the `PATH` | The item says the fact and the action. Examples: a plugin an update turned off → `hermes -p <profile> plugins enable <name>` (or `plugins remove <name>` if you do not want it); tools without a key → `hermes -p <profile> setup`, or `hermes -p <profile> tools` to turn them off; linger → `sudo loginctl enable-linger $USER`. The next daily check closes the item once it is solved. |
| Any other problem the doctor lists | a sentence the plugin does not know | Shown in Hermes' own words, with the command Hermes names. |

A problem of the whole machine (a security advisory, linger, the `PATH`, one gateway per profile, two profiles with the same chat credential, the disk of the cron tasks) is listed once, on the first profile that saw it (the lead first), with the other profiles named.

Optional pieces that are only not set up (an optional package, a provider without a login) are not problems: they are counted, never proposed. Each profile's line on screen 14 shows the count of problems, warnings and optional pieces, and when it was checked; when the doctor could not run (a timeout, a Hermes without these checks), the line says so and why. In that case, run `hermes doctor` yourself.

Before the plugin moves a package cache, it runs the doctor check that found it again for that profile. If the situation is gone, nothing moves and the item closes.

**The trash** is emptied at every learning pass, whatever the mode: what entered it more than 30 days ago is deleted.

### What you can run

Each action runs Hermes' own command, from screen 12 or with `hermes evolution care <action> --yes`, in your own terminal (never in an agent's shell). Each has a limit of 900 seconds.

| Action | `care` | Command | What it does |
|---|---|---|---|
| Check again | `check` | none | Reads the health table again now. Open to anyone. |
| Installation integrity | `pm-doctor` | `hermes pm doctor` | Offline. Checks every Hermes-managed file against its recorded digest. Runs before Hermes' own startup maintenance. |
| Hermes' full check | `doctor` | `hermes doctor` | Calls every configured provider's API (an endpoint ending in `/anthropic` gets a one-token request), runs `npm audit` against the npm registry, asks `gh auth status` and imports the dashboard in a subprocess. It uses the network and can take minutes. It changes no setting. |
| Repair with Hermes | `fix` | `hermes backup --quick --label atlas-evolution`, then `hermes doctor --fix` | The backup runs first. If it fails, nothing is repaired. |
| Free old tool versions | `gc` | `hermes pm gc` | Removes tool versions nothing references. Nothing to undo: Hermes fetches a version again if something asks for it. |

**Reading the doctor's answer.** `hermes doctor` ends with exit code 1 when it found problems. That is an answer, not a failure: the plugin shows how many problems, the lines marked ✗ and the doctor's own numbered list of what to do. Exit 0 means nothing to fix.

**What `hermes doctor --fix` may change:** create missing folders and the `.env` file, migrate `config.yaml` (Hermes keeps its own copy first), repair the session database, recreate the `hermes` launcher, and download Chromium when the browser tool needs it.

**What the quick backup restores:** `config.yaml`, `.env`, the session database and the cron jobs. To go back, run `/snapshot restore <id>` in a Hermes chat.

**`hermes pm gc` by itself.** In Auto mode, the lead profile runs `hermes pm gc` once for the whole Hermes (there is one tool store, at the root) when 300 MB or more of old tool versions are unused, at most once a week. It is recorded in the decision history.

## The operating system

Linux with apt (Ubuntu, Debian). Every other system reads as "not supported" and nothing is run.

**Check** (read-only; daily in the learning pass, and on each visit to screen 13):

- updates waiting, with security ones counted apart;
- a reboot the system waits for, and the packages that asked for it;
- `dpkg --audit` (an update left half-way) and `apt-get check` without apt's lock (it never makes `unattended-upgrades` wait);
- whether another package manager works now: a dpkg or apt lock held (read from `/proc/locks`), or an `apt`, `apt-get`, `aptitude` or `dpkg` process. The `unattended-upgrade-shutdown` helper, which only waits for a shutdown, does not count. While another package manager works, an unfinished dpkg is its own work in progress and is not reported; one working for more than 6 hours (an `apt` left waiting at its question) needs attention;
- held packages, and the ones a stopped run of this plugin still holds;
- room on `/`, `/boot` and `/var`;
- automatic security updates, read with `apt-config dump` (apt's own reading of every file under `apt.conf.d`);
- the age of the package lists: the newest `Release` file apt fetched, by hand or by the timer;
- Hermes' systemd units: a unit that runs now and is not enabled would not come back after a reboot (a unit you left stopped is not reported); a user unit of the profile's owner also needs linger.

It is ready to update when nothing is half-way, broken or busy and there is room.

**Plan**: `apt-get update`, then a simulation of `apt-get --with-new-pkgs upgrade` (or `full-upgrade` with `--full`): what would be upgraded, installed, removed or kept back, and the packages that need a word: restarts the containers (docker, containerd), needs a reboot (libc6, systemd, the kernel, openssl), restarts the web server, remote access (ssh, sudo), the boot loader, the package manager itself.

**Update** (root, in the owner's own terminal; never the agent, never by itself):

1. The check must pass, and Hermes' profile and the plugin's own code must belong to root. If another user can change them, root would run what that user wrote: the plugin refuses and the plan shows what to do with apt yourself.
2. A copy of every installed package with its version and of `/etc`, in a folder only root can read, outside every Hermes profile (so never inside a Hermes backup): `/var/lib/hermes-atlas-evolution/system/<time>-<random>/`. Its files are created new, never through a link; the latest 5 runs are kept.
3. `apt-get -y --with-new-pkgs upgrade` in its own systemd unit (`hermes-evolution-os-upgrade-<run>`), so a closed terminal, a dropped SSH connection or a restarted gateway never leaves dpkg half-way. It keeps every configuration file you changed (`--force-confold`), restarts no service by itself (`NEEDRESTART_MODE=l`), and holds the packages you asked to leave out only for this run: they are written down before they are held, and released when the run ends, fails or is stopped (`systemctl stop`, Ctrl+C, a hang-up); a package leaves that list only once `apt-mark` really released it, waiting up to 3 minutes for a dpkg that is still finishing. A name apt would not accept is refused, never dropped in silence.
4. After: dpkg and apt checked again, every service that was running checked again (all of them, not only Hermes'; one that failed counts against the update, one that ended without an error is listed apart, since most stop when idle), units that newly failed, `hermes --version`, and a report with what changed and the command that goes back to each old version (`apt-get install <package>=<version>`, while that version is still available). `hermes evolution system report` and the menu show the latest one, written when it is first asked for if the menu was closed.

The plugin never reboots. When the system waits for a reboot, plan it: Hermes' services come back by themselves when they are enabled (and, for a user unit, with linger).

**A run that vanished** (the machine lost power, the unit was killed): the menu and `system report` say so instead of waiting for ever, and the check lists the packages that run still holds.

**Repair**, in its own systemd unit like an update: releases what a stopped run of the plugin still holds, then `dpkg --configure -a` and `apt-get -f install`, keeping your configuration files; refused while another package manager works.

### How it was tested

In containers (Docker) with Ubuntu 26.04, 24.04 and 22.04 and Debian 13, apart from any production system:

- a full update of Ubuntu 24.04 and 22.04 images of April 2024 (67 and 55 packages, libc6, apt, dpkg and openssl among them), with one package left out (kept at its old version, its hold released after) and an edited configuration file of libc6 (kept); the run folder only root's, outside the profile;
- a power cut in the middle of the update (the container killed while dpkg unpacked libc6, zlib or libsystemd, then started again): the check finds dpkg half-way and the package the run still held, and refuses an update; the repair refuses while another apt runs, then releases the hold and finishes the work, and the check is ready again;
- the update stopped as `systemctl stop` does (SIGTERM to the whole run) while a package's script still ran, with dpkg keeping its lock: the run waits for it and releases the package left out, and the repair finishes dpkg's work;
- a package left out written with its architecture (`zlib1g:amd64`) while the owner already held it: the owner's hold is left alone;
- a real dpkg lock held by another program: the check says busy and names the lock, and is ready once it is released;
- a Hermes owned by another user: root refuses to update through it;
- an update of Ubuntu 26.04 with Hermes 0.23.1 installed, through `hermes evolution system upgrade`: libc6 2.43 updated, Hermes answers after, no service stopped.

## The weekly digest

Decisions that wait need something to remind you of them (Hermes 0.23.1 drops end-of-session notices). The menu's suggestion, or `hermes evolution schedule-digest --deliver <local|channel>`, schedules the weekly digest (Mondays at 08:00) as a Hermes cron job that runs a script, not the agent (`--no-agent`): it spends no token. It is created once and undone from the decision history (`hermes cron remove`).
