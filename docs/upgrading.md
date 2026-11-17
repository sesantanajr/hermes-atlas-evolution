# Updating

**English** | [Português (Brasil)](pt-BR/upgrading.md)

## Updating the plugin

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

Most settings apply within a minute. Three wait for `hermes gateway restart` (in the terminal: the next session): `runtime_guard`, `tool_hints`, and `agent_tools` when you turn it on (turning it off works at once).

If a profile still has the plugin under its old name `hermes-evolution`, run `hermes -p <profile> plugins remove hermes-evolution`, then `hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable`.

Version notes: [CHANGELOG](../CHANGELOG.md).
