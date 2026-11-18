# Troubleshooting

**English** · [Português (Brasil)](../pt-BR/troubleshooting.md) · [Documentation home](README.md)

Each entry: what you see, why, and what to do. `hermes -p <profile> evolution status` is always the first look: it lists what needs attention with the command that fixes it. `<profile>` is the profile's name, `default` for the root profile.

## The gateway does not come back after Hermes updated itself

**You see** `status=203/EXEC` in `systemctl --user status hermes-gateway`, a restart loop, or `hermes gateway restart` waiting for minutes. Or `status` says the gateway runs but the program its service starts was deleted: it runs now, and the next restart fails.

**Why** Hermes replaced its bundled Python and the gateway's systemd service still starts the old one.

**Do**

```bash
hermes -p <profile> evolution care fix-gateway     # points the service at the Python Hermes uses now (a copy of the file is kept)
hermes -p <profile> evolution care undo-gateway    # puts the file back, if you ever need to
```

The gateway is repaired (by the Mode auto or `care fix-gateway`) only when no gateway of this Hermes runs, degraded and starting ones included, and no gateway service of either scope runs, starts or stops, and only the service Hermes names for this home and that runs this `HERMES_HOME`. The one exception is the service being repaired: when it is the one running (its program was deleted under it), `care fix-gateway` fixes its file and starting it changes nothing; the Mode auto never touches a service that runs. `hermes pm gc` deletes every tools folder `facts.json` does not name; any folder a service or running program uses blocks it. `care undo-gateway` works from any profile, undoes one repair at a time back to the original file, and an undone repair is never repeated automatically.

In Auto mode the plugin repairs this by itself, at most once a day, only a user service, and records it in the history. When `care fix-gateway` says "Not now", it names what is in the way (a gateway that runs, a service that starts or stops, a scope it could not read, a service of another Hermes); nothing was changed. "Still starting" means systemd is starting the gateway: check again in a minute.

## `hermes plugins update` says "is pinned to"

**Why** that profile's copy was installed at a fixed commit, and Hermes keeps the pin on every reinstall.

**Do** `hermes -p <lead> evolution update --profiles <profile>`, run from the profile whose code you want there (`<lead>`): it moves the pin to the commit that profile runs, with Hermes' own `plugins install <url> --force --ref <commit>`, and only when both copies come from this repository. To choose another commit yourself, run that `plugins install` command with the commit you want. The plugin never picks a commit and never moves a pin by itself, in any mode.

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

**Do** nothing: it appears by itself after a few days of normal use. `hermes -p <profile> evolution status --format json` shows the sample behind each part.

## `status` says the plugin is adding time to a call

**Why** the slow tail (p95) of one of its hooks passed 50 ms over at least 200 calls; a slow disk or a database another process keeps busy is the usual cause.

**Do** look at `hermes -p <profile> evolution care check` (disk, the session database, memory). If the disk is a network or synced folder (OneDrive, NFS), move `HERMES_HOME` to a local disk.

## `status` says the plugin's database was damaged and started again

**Why** the file was torn (a power loss, a full disk, a sync tool copying it mid-write). The plugin confirmed the damage with SQLite's own check, set the file aside as `evolution/evolution.sqlite3.corrupt-<time>` and started a new one; Hermes itself was not affected.

**Do** nothing. The two newest damaged copies are kept; delete them once you no longer need them.

## The runtime guard blocked a command

**Why** the agent's command would restart or kill the gateway, delete what Hermes runs on, or change this plugin.

**Do** run the command yourself in your own terminal if you meant it. To switch the guard off for a profile (not recommended): `hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.runtime_guard false`, then `hermes gateway restart`.

## A command answers "Another learning pass is running in this profile"

**Why** passes never run twice at the same time in one profile (the gateway's background pass holds it).

**Do** try again in a minute.

## A command refuses to run in "an agent's terminal"

**Why** decisions (approving lessons or proposals, undo, settings) are the owner's: they refuse to run in a shell an agent session started, even with its environment cleared.

**Do** run it in your own terminal, or decide in the dashboard with a login.

## A setting I changed did not apply

**Why** most settings apply within a minute; `runtime_guard`, `tool_hints`, and turning `agent_tools` or `reflector` on, decide what the plugin registers in Hermes, so they apply at the next load. Or the value is in `settings.json` and would widen trust: `owner_sender_ids`, `owner_platforms`, `agent_tools` on, `tool_hints` on, `reflector_daily_limit` above 6, `runtime_guard` off and a `mode` other than `manual` count only from Hermes' configuration, because the agent's file tools can write `settings.json`.

**Do** `hermes gateway restart` (in the terminal, the next session picks it up). `status` lists the settings waiting for it, and the keys of `settings.json` it ignored, under Needs attention. Set those with `hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.<key> <value>` from your own terminal.

## `status` says `config.yaml` was not read

**Why** the plugin could not read the profile's `config.yaml`: the standalone runner without PyYAML, a file over 1 MiB, or a YAML error. Without it the plugin does not know what you chose, so it acts as in Manual mode: nothing changes by itself, and the values shown are the defaults.

**Do** run the command through Hermes (`hermes -p <profile> evolution …`), or install PyYAML for the standalone runner, or fix the file (`hermes -p <profile> config check` shows what Hermes thinks of it).

## The gateway shows "running (degraded…)"

**Why** Hermes started the gateway and no chat platform is connected yet; scheduled tasks still run. The plugin counts it as running: it never repairs or starts a gateway while one like this is up.

**Do** check the platform's credentials with `hermes -p <profile> gateway status`. Nothing to do on the plugin's side.

## The panel shows "(text hidden: read it in your terminal)"

**Why** the dashboard has no login, so anyone who can load the page, the agent's terminal included, could read it. The panel then leaves out the text of lessons written by you, the reflector or the agent, as the terminal does in an agent's shell. Template lessons stay.

**Do** read them with `hermes -p <profile> evolution lessons list` in your own terminal, or turn on the dashboard's login.

## Remove the plugin completely

```bash
hermes -p <profile> plugins remove hermes-atlas-evolution    # each profile
```

Then delete `<profile home>/evolution/` in each profile to remove its data (lessons, history, counters).

To stop it without removing it, `hermes -p <profile> plugins disable hermes-atlas-evolution` makes a running gateway unload it; after a removal, run `hermes gateway restart`. Neither command deletes the data folder. In Auto mode, the profile that looks after the whole Hermes remembers where the plugin has been and never installs it there again by itself.

**Still stuck?** Open an issue with the output of `hermes -p <profile> evolution status --format json` (it holds no conversation text): [issues](https://github.com/sesantanajr/hermes-atlas-evolution/issues).
