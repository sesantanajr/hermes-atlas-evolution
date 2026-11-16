![Atlas carrying a celestial sphere over a black technical grid. Atlas Evolution.](docs/banner.jpg)

# Atlas Evolution

A measured self-improvement loop for Hermes Agent. It learns from the profile's own history on the first pass and then runs by itself: tool failures that keep coming back become one-sentence lessons for the agent, every lesson, skill rewrite and setting change is judged by what happened before and after it, and a runtime guard stops the agent from taking down the Hermes it runs on. Lessons written from fixed templates apply at once and are retired when they do not help; text written by a model or by the agent waits for your approval, and changes to Hermes itself are left to you. Atlas Evolution is an independent community plugin: it uses Hermes' public plugin API and the Python standard library, and opens no network connection of its own.

[![Tests](https://github.com/sesantanajr/hermes-atlas-evolution/actions/workflows/tests.yml/badge.svg)](https://github.com/sesantanajr/hermes-atlas-evolution/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Hermes Agent 0.23.1 or later](https://img.shields.io/badge/Hermes%20Agent-%E2%89%A5%200.23.1-6f42c1.svg)](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1)
[![Python 3.11 or later](https://img.shields.io/badge/python-%E2%89%A5%203.11-3776ab.svg)](https://www.python.org/downloads/)

**English** | [Português (Brasil)](README.pt-BR.md)

## Install

Requires Hermes Agent 0.23.1 or later. Hermes loads plugins per profile, even when one gateway serves them all, so install the plugin in every profile you use.

```sh
# The default profile
hermes plugins install sesantanajr/hermes-atlas-evolution --enable

# A named profile
hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable

# Every named profile (the default profile is not in this folder: run the first command too)
for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins install sesantanajr/hermes-atlas-evolution --enable; done

# Check
hermes evolution status
```

Installing loads the plugin into a running gateway; no restart is needed. The first `status` in a profile runs the first pass at once, which reads up to 14 days of that profile's history, so it does not start from zero (`--no-learn` skips it). `status` also lists every profile of the same Hermes and gives the install command for each profile used in the last 7 days that does not have the plugin.

In Hermes' plugin list the plugin shows a generic card for now: Hermes shows a cover only for entries of its curated plugin catalog, reviewed by Nous Research ([details](docs/hermes-integration.md#cover-image-and-the-plugin-catalog)).

## Update

```sh
hermes plugins update hermes-atlas-evolution                # the default profile
hermes -p <profile> plugins update hermes-atlas-evolution   # each other profile
hermes gateway restart
hermes evolution status
```

`plugins update` fetches the new code but does not reload a gateway that is already running (Hermes reloads plugins on install, enable and disable), so restart the gateway once at the end. The restart lets the running turns finish first; when sessions are running, it names them and asks before it goes on. `status` points out a gateway that still runs older code.

## Migrating from 0.5.0

Up to 0.5.0 the plugin was installed as `hermes-evolution`. The id changed, so `hermes plugins update` cannot replace that copy. In each profile that has it, remove the old copy first and then install the new one (add `-p <profile>` to both commands for a named profile):

```sh
hermes plugins remove hermes-evolution
hermes plugins install sesantanajr/hermes-atlas-evolution --enable
```

- What was learned stays: the `hermes evolution` command, the data in `<HERMES_HOME>/evolution/` and `settings.json` keep their names.
- While the old copy is enabled, the new one registers nothing, so no turn is counted twice. If you installed the new copy first, run `hermes gateway restart` after removing the old one.
- `plugins remove` also deletes the block `plugins.entries.hermes-evolution` from the profile's `config.yaml`. If you changed options in the Hermes settings form, rename that block to `hermes-atlas-evolution` first.
- Two defaults changed: `lessons` is now `auto` (`lessons: suggest` keeps the behaviour of 0.5.0; lessons already waiting keep waiting), and `tool_hints` is now `false`.
- The agent tools and their toolset are now `hermes_atlas_evolution_plan`, `hermes_atlas_evolution_lessons` and `hermes-atlas-evolution`. Update an explicit toolset list or `auxiliary.background_review.extra_tools` that names the old ones.

`status` in a profile that already runs 0.6.0 lists the profiles still on the old id, with these two commands.

## What it does

- **Learns from the history Hermes already keeps.** The first passes replay the last 14 days of the profile's sessions, so a failure that recurred last week can become a lesson on the first day.
- **Turns repeated tool failures into lessons.** A failure seen at least 3 times, in 2 sessions or on 2 days, becomes one sentence such as "When calling `example_search`, always pass the required argument `query`." The agent reads the active lessons at the start of each session.
- **Measures before and after.** Each lesson against its own failure, each skill rewrite by the turns that loaded the skill, each setting change by the week before and the week after. In `auto` mode a lesson that does not help is retired.
- **Asks a model when no template fits.** The reflector drafts a lesson through Hermes, at most 6 calls a day by default, and the draft waits for you.
- **Watches Hermes' health.** Sessions that end as orphans, heavy context per call, compactions that free little room, plugins that fail to load or compete for one hook, and tool failures caused by another plugin, which never become lessons.
- **Guards the runtime.** It vetoes agent commands that would stop, restart or kill the gateway, or delete the Python, the code or the profile that Hermes runs on. The agent is told to leave a restart to you.
- **Runs by itself and asks before changing Hermes.** A background pass runs at most once an hour per profile. Changes to Hermes, such as locking a skill, are proposed with the exact command for you to run.

## Commands

Subcommands of `hermes evolution`. `--lang en|pt` works with any of them.

| Command | What it does |
|---|---|
| `status` | Whether the plugin observes, learns and protects this profile, and the state of every profile of this Hermes. |
| `report [--days N]` | The digest of the last N days (7 by default), compared with the period before. |
| `lessons list` | The lessons, with origin, evidence and measured effect. |
| `lessons review` | Goes through every lesson that waits for approval, with its exact text: `a` approves, `r` rejects, Enter skips. |
| `lessons approve <id>` | Activates one waiting lesson after showing its exact text. |
| `proposals list`, `proposals approve <id>`, `proposals reject <id>` | The queue of proposals, and your decision on one. |
| `learn` | Runs a pass now instead of waiting for the background one. |

Every command and option: [Commands](docs/evolution-loop.md#commands).

## Settings

Set them in Hermes Desktop under Settings → Plugins, or in the profile's `config.yaml` under `plugins.entries.hermes-atlas-evolution.settings`. Restart the gateway after a change.

| Option | Default | What it does |
|---|---|---|
| `lessons` | `auto` | `auto`: template lessons apply at once, are measured and are retired when they do not help; reflector drafts and agent proposals wait for approval. `suggest`: every lesson waits for approval. `off`: measure only. |
| `language` | `auto` | `en` or `pt`. `auto` follows the profile language. |
| `reflector` | `true` | Drafts lessons with a model, through Hermes, for failures that have no template. |
| `autopilot_minutes` | `60` | Runs the background pass at most once per interval, from 15 to 1440 minutes. `0` runs it only when you call `learn`. |
| `runtime_guard` | `true` | Vetoes agent commands that would stop the gateway or delete what Hermes runs on. |
| `agent_tools` | `false` | Offers the tools `hermes_atlas_evolution_plan` and `hermes_atlas_evolution_lessons` to the agent. |
| `tool_hints` | `false` | Also adds the matching lesson to a failing tool result. Off by default because Hermes keeps only the first plugin's change to a tool result. |
| `feedback_signals` | `true` | Counts your corrections, repeated questions and thanks, as numbers only. |

```yaml
plugins:
  entries:
    hermes-atlas-evolution:
      settings:
        lessons: suggest
```

Every option, `settings.json` and what happens with an invalid value: [Settings and language](docs/evolution-loop.md#settings-and-language).

## Privacy and safety

- **What it keeps.** Counters, failure classes, hashed session IDs, lesson sentences and proposals, under `<HERMES_HOME>/evolution/`. It keeps no conversation text, prompt, tool argument, tool output or memory content. While the reflector is on, it keeps one scrubbed error excerpt of up to 300 characters per failure that has no template, until the failure has a lesson.
- **What it reads outside its own data**, always read-only: Hermes' `state.db` (the history for the first passes and the health radar), `logs/agent.log` and its rotations `agent.log.1` to `agent.log.5`, the Curator ledger and the skill usage file, and, for `status`, the session count of the other profiles of the same Hermes and the plugin's own state in them. The operations supervisor also reads counts from Cron, Kanban, the memory files and Linux ([full list](docs/hermes-integration.md#what-the-plugin-reads)).
- **Background threads.** In each Hermes process, per profile: a clock that wakes every 10 minutes and the thread that runs the pass. No transaction stays open during a model call.
- **Model calls.** Only the reflector calls a model, through Hermes and its provider settings (auxiliary task `evolution_reflector`), at most 3 times per pass and 6 times a day by default. `reflector: false` stops it.
- **Network.** No connection of its own and no telemetry.
- **Gateway chats.** The plugin sends nothing to a chat. Its notices appear only on your local surfaces (CLI, TUI, Desktop). What the other side of a gateway chat sees is what the agent writes.
- **Decisions.** Approving, rejecting and activating refuse to run in the agent's shell. The plugin changes only its own data; changes to Hermes are printed as commands for you.

Details: [SECURITY.md](SECURITY.md).

## Documentation

- [The evolution loop](docs/evolution-loop.md): how lessons are made, delivered and measured, the reflector, the health radar, the digest with an example, `status`, and every command and setting.
- [Hermes integration](docs/hermes-integration.md): hooks, the runtime guard, what the plugin reads, permissions, installation notes, and compatibility and limits.
- [Operations](docs/operations.md): the operations supervisor (Linux pressure, usage and cost, Cron and Kanban).
- [CONTRIBUTING.md](CONTRIBUTING.md): development, tests and the rules for changes.
- [SECURITY.md](SECURITY.md): threat model, what the plugin stores and reads, and how to report a vulnerability.
- [CHANGELOG.md](CHANGELOG.md): what changed in each version.

## License

Atlas Evolution is free and open-source software, released under the [MIT License](LICENSE) (SPDX identifier `MIT`). You may use, copy, modify, merge, publish, distribute, sublicense and sell copies, including for commercial use, provided the copyright notice and the license text are kept in every copy. The software is provided as is, without warranty of any kind.

Contributions are welcome under the same license; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Author

Created by **Sérgio Sant'Ana Júnior**. [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/)
