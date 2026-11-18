# Updating

**English** | [Português (Brasil)](../pt-BR/upgrading.md)

## Updating the plugin

Every command names the profile it runs in: `default` for the root profile, or the profile's name.

```sh
hermes -p <profile> evolution update --all          # every profile; a pinned one moves to the latest commit
hermes -p <profile> evolution update                # pick the profiles from a list
hermes -p <profile> evolution update --list         # version, commit and pin of each profile
sh install.sh --all --yes --restart                 # the same without the plugin loaded
hermes gateway restart
```

`update` installs only where the plugin already is; add `--install-missing` to install it in the profiles you chose that do not have it, and `--yes` to skip the question.

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

The command-line `plugins update` only replaces the files: it does not reload a gateway that is already running, so restart the gateway once at the end. The dashboard shows the **Evolution** tab once it rescans its plugins; if it does not appear, restart the dashboard. (Hermes does reload a running gateway on install, enable and disable, and when the update is applied from Hermes Desktop, the dashboard or `/plugins update` in a chat.) If something is still running (a turn, a cron job, a delegated task), the restart lists it and asks `Interrupt them and restart the gateway? [y/N]`: answer N and run it again in a few minutes, or y to interrupt them. `status` points out a gateway that still runs older code.

**Every profile at once, by itself.** In Auto mode, the profile that looks after the whole Hermes keeps the plugin's copy in every other profile on its own code: a copy that is pinned to another commit, older, on other code with the same version, or left half-installed by an interrupted update is brought to the lead's commit with Hermes' own command (at most two profiles per pass, once per situation, retried a day later if Hermes refuses). A profile where the plugin is off, or set to Assisted or Manual, is left as it is, and so are a development link and a profile with `fleet_managed` off. Nothing new is downloaded by itself: a copy that is not pinned is updated only while the repository's latest commit is the one the lead runs (read with `git ls-remote`), so update the lead (`hermes -p <profile> evolution update`) and the rest follow. Each sync is in the history (`hermes -p <profile> evolution decisions`); restart the gateway once to load the new code.

Hermes keeps a pin when a plugin is installed again without `--ref` (it reinstalls the pinned commit), and its `plugins update` refuses a pinned copy. `hermes evolution update`, `install.sh` and the Auto mode therefore move a pin with `hermes -p <profile> plugins install <url> --force --ref <commit>`, Hermes' own way; the profile stays pinned, now to current code.

Most settings apply within a minute. A few wait for `hermes gateway restart` (in the terminal: the next session): `runtime_guard`, `tool_hints`, and `agent_tools` or `reflector` when you turn it on (turning either off works without a restart).

If a profile still has the plugin under its old name `hermes-evolution`, run `hermes -p <profile> plugins remove hermes-evolution`, then `hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable`.

Version notes: [CHANGELOG](../../CHANGELOG.md).

## Updating to 0.12.0

Update as above and restart the gateway once. There is nothing to migrate by hand.

- **On Hermes 0.23.2, update now.** Its new launcher makes 0.11.0 and earlier refuse your own owner commands in your own terminal ("Only the operator can decide on lessons and proposals…" for `owner`, `care doctor`, approving lessons). 0.12.0 recognises it. Until you update, the dashboard's Evolution tab with a login still decides.
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
