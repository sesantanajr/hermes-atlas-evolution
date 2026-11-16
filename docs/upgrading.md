# Updating

[Português (Brasil)](pt-BR/upgrading.md)

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

Version notes: [CHANGELOG](../CHANGELOG.md).

## Migrating from 0.5.0

Up to 0.5.0 the plugin was installed as `hermes-evolution`. The id changed, so `hermes plugins update` cannot replace that copy. In each profile that has it (add `-p <profile>` to the commands for a named profile):

1. Only if you changed the plugin's options in the Hermes settings form: open the profile's `config.yaml` and rename `plugins.entries.hermes-evolution` to `plugins.entries.hermes-atlas-evolution` (the next command deletes the old block).
2. Remove the old copy and install the new one:

   ```sh
   hermes plugins remove hermes-evolution
   hermes plugins install sesantanajr/hermes-atlas-evolution --enable
   ```

3. Restart the gateway once, from your own terminal: `hermes gateway restart`.
4. Check with `hermes evolution status`. It lists the other profiles that still have the old copy, with the exact command for each, and marks a profile that has both copies, where the old one is still the one running.

Notes:

- What was learned stays: the `hermes evolution` command, the data in `<HERMES_HOME>/evolution/` and `settings.json` keep their names. The first pass of 0.6.0 recounts the history that 0.5.0 replayed, because 0.5.0 counted messages that Hermes keeps more than one copy of.
- While the old copy is enabled, the new one stays inactive (the Hermes log says so), so no turn is counted twice. If the title of `hermes evolution status` in a profile still shows 0.5.0, the old copy is still there: remove it in that profile and restart the gateway.
- Two defaults changed: `lessons` is now `auto` (`lessons: suggest` keeps the behaviour of 0.5.0; lessons already waiting keep waiting), and `tool_hints` is now `false`.
- Only if you listed them by hand in `config.yaml`: replace `atlas-evolution`, `atlas_evolution_plan` and `atlas_evolution_lessons` with `hermes-atlas-evolution`, `hermes_atlas_evolution_plan` and `hermes_atlas_evolution_lessons` in a toolset list or in `auxiliary.background_review.extra_tools`.

In Hermes' plugin list the plugin shows a generic card: Hermes shows a cover only for entries of its curated plugin catalog ([details](hermes-integration.md#cover-image-and-the-plugin-catalog)).
