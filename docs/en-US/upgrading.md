# Updating

**English** | [Português (Brasil)](../pt-BR/upgrading.md)

## Updating the plugin

The plugin never updates itself and never reads the network: new code comes only from Hermes. Every command names the profile it runs in: `default` for the root profile, or the profile's name.

```sh
hermes -p <profile> evolution update --all          # Hermes' own `plugins update` in every profile, this one first
hermes -p <profile> evolution update                # pick the profiles from a list
hermes -p <profile> evolution update --list         # version, commit and pin of each profile
sh install.sh --all --yes --restart                 # Hermes' own commands, without the plugin loaded
hermes gateway restart
```

`evolution update` is a convenience: it runs Hermes' own command in each profile you pick, the profile you run it in first, and never picks a commit.

| The copy in a profile | What `evolution update` runs there |
|---|---|
| Installed from the Hermes plugin catalog | `plugins update hermes-atlas-evolution`: Hermes moves it to the catalog's reviewed pin. |
| Installed from GitHub | `plugins update hermes-atlas-evolution`: Hermes pulls the source it was installed from. |
| Pinned by Hermes with `--ref` | Moved only to the commit the profile you run the command in runs (`plugins install <url> --force --ref <that commit>`), and only when both copies come from this repository. Otherwise it is left as it is, with the command that moves it. |
| Missing, with `--install-missing` | The code the profile you run the command in runs: its catalog name, or this repository at that profile's commit. |
| A development link, or a fork's pinned or half-installed copy | Never touched. |

`update` installs only where the plugin already is; add `--install-missing` to install it in the profiles you chose that do not have it, and `--yes` to skip the question. `install.sh` does the same with Hermes' own commands only: `plugins update hermes-atlas-evolution` where the plugin is, `plugins install` where it is missing (with `--install-missing`; from GitHub, or by catalog name with `--catalog`), and a pinned copy left as it is, with the command that moves it.

Manually, one profile at a time:

```sh
hermes plugins update hermes-atlas-evolution                # the default profile
hermes -p <profile> plugins update hermes-atlas-evolution   # a named profile

# Every named profile
for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins update hermes-atlas-evolution; done

hermes gateway restart
systemctl restart hermes-dashboard   # as root, or however you run the dashboard
hermes -p <profile> evolution status
```

**Unattended updates are Hermes' own.** `hermes -p <profile> config set plugins.auto_apply true` lets the gateway's scheduled check apply catalog re-pins and pulls of git installs in that profile, and Hermes Desktop and the dashboard have an "Update all" button. What an unattended update does not accept (new capabilities, a widened catalog pin) waits for you to run the update yourself.

The command-line `plugins update` only replaces the files: it does not reload a gateway that is already running, so restart the gateway once at the end. The dashboard shows the **Evolution** tab once it rescans its plugins; if it does not appear, restart the dashboard. (Hermes does reload a running gateway on install, enable and disable, and when the update is applied from Hermes Desktop, the dashboard or `/plugins update` in a chat.) If something is still running (a turn, a cron job, a delegated task), the restart lists it and asks `Interrupt them and restart the gateway? [y/N]`: answer N and run it again in a few minutes, or y to interrupt them. `status` points out a gateway that still runs older code.

Hermes keeps a pin when a plugin is installed again without `--ref` (it reinstalls the pinned commit), and its `plugins update` refuses a pinned copy. `hermes evolution update` therefore moves a pin with `hermes -p <profile> plugins install <url> --force --ref <commit>`, Hermes' own way, to the commit the profile you run it in already runs; the profile stays pinned, now to that code.

Most settings apply within a minute. A few wait for `hermes gateway restart` (in the terminal: the next session): `runtime_guard`, `tool_hints`, and `agent_tools` or `reflector` when you turn it on (turning either off works without a restart).

If a profile still has the plugin under its old name `hermes-evolution`, run `hermes -p <profile> plugins remove hermes-evolution`, then `hermes -p <profile> plugins install hermes-atlas-evolution` and `hermes -p <profile> plugins enable hermes-atlas-evolution` (or, from GitHub, `hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable`).

Version notes: [CHANGELOG](../../CHANGELOG.md).

## Updating to 0.14.0

Update every profile as above and **restart the gateway once**: a gateway started by a launcher of its own (one that
runs `gateway.run` as its main module) is recognized from this version on and starts claiming the learning passes of
every profile it serves. Nothing to migrate by hand.

- **The first passes after the restart** replay up to 14 days of history in profiles that never had one, one profile at
  a time, at a lower CPU priority, starting 2 to 7 minutes after the gateway starts.
- **Lesson verdicts are judged again.** An "effective" verdict needs evidence now; one judged on too few calls is taken
  back, and a lesson whose failure keeps happening while the agent reads it becomes ineffective (retired in Auto mode).
- **Every profile at once**: `hermes evolution fleet` shows them all; `hermes evolution fleet auto on` turns Auto mode
  on in every profile through `hermes config set` and keeps each profile's previous values for `fleet auto off`;
  `hermes evolution fleet schedule` adds the daily report as a scheduled task with no agent.
- **Watch the pin of an overlay's builder** (optional): if a script builds a file loaded in place of one of Hermes' own
  and pins its sha256, name it once in the profile that looks after the others:

  ```sh
  hermes -p default config set plugins.entries.hermes-atlas-evolution.settings.overlay_pins \
    '["/path/to/build_overlay.py|docs/how-to-rebuild.md"]'
  ```

  After every Hermes update the plugin then says when Hermes' file no longer matches the pin. It never rebuilds it.

## Updating to 0.13.0

Update as above and restart the gateway once. There is nothing to migrate by hand; check the first point if you relied on Auto mode.

- **Assist is now the default mode.** A profile with no `mode` set switches from Auto to Assist: by itself it then only takes out a memory entry repeated word for word between `MEMORY.md` and `USER.md`, and everything else becomes a proposal that waits for you. A profile where you set `mode` keeps it. To keep Auto mode in a profile, from your own terminal:

  ```sh
  hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.mode auto
  ```

- **Auto mode no longer syncs copies across profiles.** It never moves the plugin's copy in another profile to a commit. It still installs the plugin where a profile in use never had it, but by itself only by its catalog name, when the lead profile is a catalog install (Hermes installs the reviewed pin); an install from the repository waits for your key (the proposal, or `evolution update --install-missing`).
- **`evolution update` runs Hermes' command, and nothing else.** In each profile you pick it runs `plugins update hermes-atlas-evolution` (no `--force`), moves a copy pinned with `--ref` only to the commit of the profile you run it in, and installs a missing one (with `--install-missing`) with the code that profile runs. It no longer looks up the repository's latest commit.
- **`install.sh --catalog`.** With `--install-missing`, it installs by catalog name instead of from GitHub. The installer runs only Hermes' own commands and leaves a pinned copy as it is, printing the command that moves it.
- **No network of its own.** The plugin no longer contacts GitHub or anything else; downloads happen only inside Hermes' own `plugins install` and `plugins update`.
- **Hermes' approvals in every child.** The commands the plugin starts no longer inherit `HERMES_YOLO_MODE`, `HERMES_YOLO` or `HERMES_ACP_AUTO_APPROVE`.
- **Stability fixes.** The runtime guard, the database, the output of the commands it runs and the learning pass's deadline: see the [CHANGELOG](../../CHANGELOG.md).

## Updating to 0.12.0

Update as above and restart the gateway once. There is nothing to migrate by hand.

- **On Hermes 0.23.2, update now.** Its new launcher makes 0.11.0 and earlier refuse your own owner commands in your own terminal ("Only the operator can decide on lessons and proposals…" for `owner`, `care doctor`, approving lessons, and `evolution update` itself). 0.12.0 recognises it. To get there, update with Hermes' own command in each profile that has the plugin (`hermes -p <profile> plugins update hermes-atlas-evolution`) or with `sh install.sh --all --yes --restart` from a fresh clone, then restart the gateway. Until you update, the dashboard's Evolution tab with a login still decides.
- **The database gains a table.** `lesson_measure` keeps when the agent first read each lesson, since a lesson is now measured from that moment. Tables are only added, so an older copy of the plugin still opens the database. A lesson that was active but never shown to the agent loses its old verdict and is measured from the first time it is shown; one already shown keeps its measurement.
- **Two values move to Hermes' configuration.** `tool_hints: true` and a `reflector_daily_limit` above 6 now count only from Hermes' configuration, like the owner ids and the agent tools: in `<profile home>/evolution/settings.json`, which the agent's file tools can write, they are ignored, and `status` lists them under Needs attention. If you set one there, set it again from your own terminal, then restart the gateway:

  ```sh
  hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.tool_hints true
  hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.reflector_daily_limit 12
  hermes gateway restart
  ```

- **A new setting, `fleet_managed`** (on by default). Turn it off in a profile the lead profile should leave alone: it then never installs, updates or moves anything to the trash there by itself, and still shows what it finds. `hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.fleet_managed false`, in that profile. `settings.json` can only turn it off.
- **An unread `config.yaml` means Manual.** When the plugin cannot read `config.yaml` (no PyYAML in the standalone runner, a file over 1 MiB, a YAML error), it acts as in Manual mode and `status` says so first. Install PyYAML, or run the standalone runner with Hermes' own Python.
- **Gateway repairs.** Every repair of the gateway's service, and every undo, now goes to a journal of the whole Hermes (`<root>/evolution/gateway-repair.json`, the latest 40). The single record that 0.9 to 0.11 kept is still read, so a repair made before the update can still be undone with `hermes -p <profile> evolution care undo-gateway`, from any profile.
- **Commands name their profile.** Everything the plugin prints, in the terminal, the menu, the panel and the digest, reads `hermes -p <profile> evolution …`. The `--format json` output is unchanged.
- **Contributors.** The test suite lives on the `tests` branch; [CONTRIBUTING.md](../../CONTRIBUTING.md#the-test-suite) says how to get it.

## Updating to 0.11.0

0.11.0 removes what 0.10.0 marked as deprecated. Update as above; then check the two points below that may concern you: an old cron job and the notes of version 0.1.

| Removed | Use instead |
|---|---|
| `hermes -p <profile> evolution ops ...`, the Linux operations supervisor of 0.3 (`inspect`, `tick`, `history`, `rollback`, `similar`, `review`, `feedback`, `plan`, `handoff`, `schedule-spec`, the Linux trials of cgroup weights, the Kanban hand-off) | `status`, `care` and the dashboard's **Health** tab for Hermes' health ([Maintenance](maintenance.md)); `usage` for model calls and their cost ([Usage and cost](evolution-loop.md#usage-and-cost)) |
| `health` | `care check` |
| `tick` | Nothing to run: the learning pass runs by itself, and `learn` runs one now |
| `feedback` | Nothing: it rated the findings of the old `tick` |
| `history` | `decisions` |
| `memory remember`, `recall`, `audit` and `forget`, the `/evolution` chat command, and the automatic recall of shared notes into turns (the notes of 0.1) | Hermes' own memory; the notes still in the database are below |

- **Old settings are ignored.** `sensitivity`, `inspect_shared_kanban`, `kanban_handoff`, `linux_supervisor`, `semantic_incident_model`, `daily_estimated_budget_usd`, `legacy_memory_commands` and `automatic_shared_recall` are no longer read, in `settings.json` or in the Hermes configuration. A value left there is ignored, without an error or a warning; delete it when you like. `rates_usd_per_million` stays: `usage` prices the calls with it.
- **The notes of 0.1 stay in the database.** Nothing reads or writes them any more, and the agent no longer gets them, but they are not deleted. While some remain, `status` lists under what needs attention "N notes of version 0.1 are still in the plugin's database", with a read-only command that prints them, so you can copy what you need (into Hermes' memory, for example). To save them to `evolution/notes-0.1.json`, next to the database, and remove them from it, `status` gives a second command; once you run it, the item goes away.
- **A cron job that ran the old tick must be removed.** A job that runs `hermes -p <profile> evolution ops tick` or `hermes -p <profile> evolution tick` now fails at every run. The recipe of `ops schedule-spec` named it `hermes-atlas-evolution-watchdog`. `status` finds such a job in Hermes' scheduler and lists it under what needs attention, with the exact `cron remove` command. Find it with `hermes cron list` and remove it with `hermes cron remove <job>`, in each profile where you created it (`hermes -p <profile> cron list`). If you scheduled it in another scheduler (a crontab, a systemd timer), remove it there. The weekly digest job (`hermes-atlas-evolution-digest`) stays.
- **The supervisor's database.** `evolution/supervisor.sqlite3` is no longer written or read. Delete it if you want the room back.
- **The standalone runner** still accepts `--hermes-version` and ignores it, so a script that passes it still runs; you can drop it.
