# Troubleshooting

**English** · [Português (Brasil)](../pt-BR/troubleshooting.md) · [Documentation home](README.md)

Each entry: what you see, why, and what to do. `hermes evolution status` is always the first look: it lists what needs attention with the command that fixes it.

## The gateway does not come back after Hermes updated itself

**You see** `status=203/EXEC` in `systemctl --user status hermes-gateway`, a restart loop, or `hermes gateway restart` waiting for minutes.

**Why** Hermes replaced its bundled Python and the gateway's systemd service still starts the old one.

**Do**

```bash
hermes evolution care fix-gateway     # points the service at the Python Hermes uses now (a copy of the file is kept)
hermes evolution care undo-gateway    # puts the file back, if you ever need to
```

In Auto mode the plugin repairs this by itself (at most once a day, only the profile's own user service, never while a gateway runs) and records it in the history. While a service still starts from an old tools folder, `hermes pm gc` is held back so it never deletes that Python.

## `hermes plugins update` says "is pinned to"

**Why** that profile's copy was installed at a fixed commit, and Hermes keeps the pin on every reinstall.

**Do** `hermes evolution update --profiles <profile>` (or `--all`): it moves the pin to the latest commit with Hermes' own `plugins install <url> --force --ref <commit>`. In Auto mode the profile that looks after the whole Hermes keeps every copy on its own commit.

## `hermes evolution` is not found, or a profile shows "no events today"

**Why** the plugin is not installed or not on in that profile (Hermes loads plugins per profile), or the gateway still runs code loaded before the install.

**Do**

```bash
hermes -p <profile> plugins list                                              # is hermes-atlas-evolution there and on?
hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable
hermes gateway restart
```

## `status` says a running gateway runs older code

**Why** `hermes plugins update` replaces the files but does not reload a running gateway.

**Do** `hermes gateway restart` once. It lists the turns still running and asks before interrupting them.

## The Evolution index says "not yet"

**Why** it needs at least two parts with enough data over the last 7 days, for example 40 tool calls and 20 interactive turns.

**Do** nothing: it appears by itself after a few days of normal use. `hermes evolution status --format json` shows the sample behind each part.

## `status` says the plugin is adding time to a call

**Why** the slow tail (p95) of one of its hooks passed 50 ms over at least 200 calls; a slow disk or a database another process keeps busy is the usual cause.

**Do** look at `hermes evolution care check` (disk, the session database, memory). If the disk is a network or synced folder (OneDrive, NFS), move `HERMES_HOME` to a local disk.

## `status` says the plugin's database was damaged and started again

**Why** the file was torn (a power loss, a full disk, a sync tool copying it mid-write). The plugin confirmed the damage with SQLite's own check, set the file aside as `evolution/evolution.sqlite3.corrupt-<time>` and started a new one; Hermes itself was not affected.

**Do** nothing. The two newest damaged copies are kept; delete them once you no longer need them.

## The runtime guard blocked a command

**Why** the agent's command would restart or kill the gateway, delete what Hermes runs on, or change this plugin.

**Do** run the command yourself in your own terminal if you meant it. To switch the guard off for a profile (not recommended): `hermes config set plugins.entries.hermes-atlas-evolution.settings.runtime_guard false`, then `hermes gateway restart`.

## A command answers "Another learning pass is running in this profile"

**Why** passes never run twice at the same time in one profile (the gateway's background pass holds it).

**Do** try again in a minute.

## A command refuses to run in "an agent's terminal"

**Why** decisions (approving lessons or proposals, undo, settings) are the owner's: they refuse to run in a shell an agent session started, even with its environment cleared.

**Do** run it in your own terminal, or decide in the dashboard with a login.

## A setting I changed did not apply

**Why** most settings apply within a minute; `runtime_guard`, `tool_hints` and turning `agent_tools` on decide which hooks Hermes calls, so they apply at the next load.

**Do** `hermes gateway restart` (in the terminal, the next session picks it up). `status` lists the settings waiting for it.

## Remove the plugin completely

```bash
hermes -p <profile> plugins remove hermes-atlas-evolution    # each profile
```

Then delete `<profile home>/evolution/` in each profile to remove its data (lessons, history, counters).

**Still stuck?** Open an issue with the output of `hermes evolution status --format json` (it holds no conversation text): [issues](https://github.com/sesantanajr/hermes-atlas-evolution/issues).
