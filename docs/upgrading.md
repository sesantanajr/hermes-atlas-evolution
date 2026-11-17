# Updating

**English** | [Português (Brasil)](pt-BR/upgrading.md)

## Updating the plugin

```sh
hermes evolution update --all          # every profile; a pinned one moves to the latest commit
hermes evolution update                # pick the profiles from a list
hermes evolution update --list         # version, commit and pin of each profile
sh install.sh --all --yes --restart    # the same without the plugin loaded
hermes gateway restart
```

Manually, one profile at a time:

```sh
hermes plugins update hermes-atlas-evolution                # the default profile
hermes -p <profile> plugins update hermes-atlas-evolution   # a named profile

# Every named profile
for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins update hermes-atlas-evolution; done

hermes gateway restart
sudo systemctl restart hermes-dashboard   # or however you run the dashboard
hermes evolution status
```

The command-line `plugins update` only replaces the files: it does not reload a gateway that is already running, so restart the gateway once at the end. The dashboard shows the **Evolution** tab once it rescans its plugins; if it does not appear, restart the dashboard. (Hermes does reload a running gateway on install, enable and disable, and when the update is applied from Hermes Desktop, the dashboard or `/plugins update` in a chat.) If something is still running (a turn, a cron job, a delegated task), the restart lists it and asks `Interrupt them and restart the gateway? [y/N]`: answer N and run it again in a few minutes, or y to interrupt them. `status` points out a gateway that still runs older code.

**Every profile at once, by itself.** In Auto mode, the profile that looks after the whole Hermes keeps the plugin's copy in every other profile on its own code: a copy that is pinned to another commit, older, on other code with the same version, or left half-installed by an interrupted update is brought to the lead's commit with Hermes' own command (at most two profiles per pass, once per situation, retried a day later if Hermes refuses). A profile where the plugin is off, or set to Assisted or Manual, is left as it is, and so is a development link. Nothing new is downloaded by itself: a copy that is not pinned is updated only while the repository's latest commit is the one the lead runs, so update the lead (`hermes evolution update`) and the rest follow. Each sync is in the history (`hermes evolution decisions`); restart the gateway once to load the new code.

Hermes keeps a pin when a plugin is installed again without `--ref` (it reinstalls the pinned commit), and its `plugins update` refuses a pinned copy. `hermes evolution update`, `install.sh` and the Auto mode therefore move a pin with `hermes -p <profile> plugins install <url> --force --ref <commit>`, Hermes' own way; the profile stays pinned, now to current code.

Most settings apply within a minute. Three wait for `hermes gateway restart` (in the terminal: the next session): `runtime_guard`, `tool_hints`, and `agent_tools` when you turn it on (turning it off works at once).

If a profile still has the plugin under its old name `hermes-evolution`, run `hermes -p <profile> plugins remove hermes-evolution`, then `hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable`.

Version notes: [CHANGELOG](../CHANGELOG.md).
