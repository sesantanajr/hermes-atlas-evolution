![Atlas carrying a celestial sphere over a black technical grid. Atlas Evolution.](docs/banner.jpg)

# Atlas Evolution

**English** | [Português (Brasil)](README.pt-BR.md)

**A plugin that makes Hermes Agent better on its own: it learns from Hermes' mistakes, keeps Hermes and the server it runs on healthy and up to date, and spends no token on that maintenance.** You decide in plain words, from a menu in your terminal or from a tab in the Hermes dashboard, and everything it changes can be undone.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Hermes Agent 0.23.1 or later](https://img.shields.io/badge/Hermes%20Agent-%E2%89%A5%200.23.1-6f42c1.svg)](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1)
[![Linux](https://img.shields.io/badge/system%20updates-Ubuntu%20%7C%20Debian-e95420.svg)](docs/maintenance.md)

## What it is for

Hermes Agent runs for days, in many chats, with many tools. Over time the same errors come back, skills get rewritten again and again, memory fills up with repeats, disks fill, the gateway grows in memory and the server falls behind on updates. Atlas Evolution (`hermes-atlas-evolution` in Hermes) watches all of that and acts:

| | What it does for you |
|---|---|
| **Learns** | Turns tool errors that keep coming back into one-sentence lessons the agent reads, measures whether each lesson helped, and retires the ones that did not. |
| **Saves tokens** | Locks skills the curator keeps rewriting, takes repeats out of Hermes' memory (which goes into every prompt), and points at what makes each call heavy. |
| **Keeps Hermes healthy** | Reads Hermes' own files, with no agent, no network and no token: the gateway alive and its memory, the session database, disk growth per folder and the day it would be full, memory, swap, CPU, old tool versions. Runs Hermes' own `doctor` and repairs on your click, with a backup first. |
| **Keeps the server up to date** | Checks, plans and runs the Ubuntu or Debian update safely: checks before, a copy of every package version and of `/etc`, the update in its own service so a dropped connection never leaves it half-way, your configuration files kept, checks after, and a repair for an update a power cut interrupted. |
| **Acts by itself where it is safe** | With `auto_actions`, the end of each learning pass applies the safe fixes by itself, each recorded with its undo. |
| **Protects the running Hermes** | Stops the agent from restarting, killing or deleting the Hermes it runs on, and from changing this plugin. |
| **Explains** | Every proposal says what was seen, with its numbers, what applying it does and how to undo it, in English or Brazilian Portuguese. |

Its data stays on your machine, it opens no network connection of its own, and only you decide: never the agent.

## See it

### In the Hermes dashboard

The **Evolution** tab of the Hermes dashboard (and of Hermes Desktop). With a dashboard login you decide there, for every profile.

![Overview: active lessons, what waits for your decision with Approve all, and the commands that share lessons with another profile](docs/screenshots/panel-overview-en.png)

![Proposals in plain words: lock a skill that keeps being rewritten, with what was seen, what it does, how to undo it, and the Apply now button](docs/screenshots/panel-proposals-en.png)

![Health: the gateway, free disk and memory, swap, the session database, old tool versions, system updates and a reboot waiting, with what needs attention](docs/screenshots/panel-health-en.png)

### In the terminal

`hermes evolution` opens the same, as a numbered menu, in your own terminal (never in an agent's shell): what needs attention, lettered suggestions that act at once, and 13 screens.

![The terminal menu: summary, what needs attention, suggestions a to f and the numbered screens](docs/screenshots/terminal-menu-en.png)

![The Operating system screen: version, updates waiting, reboot, automatic security updates, dpkg and apt, ready to update, and the actions](docs/screenshots/terminal-os-en.png)

## Quick start

Requires Hermes Agent 0.23.1 or later. Hermes loads plugins per profile, so install it in each profile you use.

```bash
hermes plugins install sesantanajr/hermes-atlas-evolution --enable              # the default profile
hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable # each named profile
hermes evolution                                                                # the menu
```

Installing loads the plugin into a running gateway; no restart is needed. The first look reads up to 14 days of the profile's history, so it does not start from zero. Updating, and moving from the old `hermes-evolution` (0.5.0): [Updating](docs/upgrading.md).

## What's new in 0.8.4

- **Hermes health, without the agent and without tokens.** A new menu screen and dashboard tab read Hermes' own files: the gateway alive (its heartbeat) and its memory, the session database, room on disk and how fast each folder grows (with the day it would be full), memory, swap, CPU and old tool versions nothing uses (about 700 MB on a real server). Hermes' own `hermes pm doctor`, `hermes doctor`, `hermes doctor --fix` (after `hermes backup --quick`) and `hermes pm gc` run on your click.
- **The operating system's update, done safely.** Check, plan and update Ubuntu or Debian from the menu or `hermes evolution system`, with checks before and after and a repair for an interrupted update. Tested in containers with 67 packages at once, a package left out, an edited configuration file kept, and a power cut in the middle of `libc6`.
- **The weekly digest in your chat, with one key.** Scheduled as a Hermes cron job that runs no agent and spends no token, and undone from the history.
- **Proposals and memory, smarter.** Memory entries that differ in a number (a port, a version) are two facts, not a repeat. Every change: [CHANGELOG](CHANGELOG.md).

## Commands

Subcommands of `hermes evolution`. `--lang en|pt` works with any of them.

| Command | What it does |
|---|---|
| *(none)*, `menu` | The interactive menu, in your own terminal. Without one (a script, a pipe, an agent's shell), the bare command prints `status`. |
| `status` | Whether the plugin observes, learns and protects this profile, what needs attention, Hermes' health and the system's, and every profile of this Hermes. |
| `report [--days N]` | The digest of the last N days (7 by default), compared with the period before. |
| `lessons list`, `lessons review`, `lessons approve <id>`, `lessons approve --all` | The lessons, and your decision on the ones that wait. |
| `proposals list`, `proposals approve <id>`, `proposals reject <id>` | The proposals; approving a change the plugin can make applies it with Hermes' own command. |
| `decisions`, `decisions undo <id>` | Your decisions and the automatic actions, from the terminal, the dashboard and the autopilot, and the undo of one. |
| `care [check \| pm-doctor \| doctor \| fix \| gc]` | Hermes' health, read from its own files; and Hermes' own checks and repairs. |
| `system [check \| plan \| upgrade \| repair \| report] [--full] [--skip PKG...]` | The operating system's update (Linux with apt). |
| `schedule-digest [--deliver local\|<channel>]` | The weekly digest as a Hermes cron job with no agent. |
| `lessons export`, `lessons import` | Lessons shared with another profile or machine, after showing each one. |
| `learn` | A learning pass now. |
| `owner` | Each chat channel with its senders, and the command that marks one as you. |

Every command and option: [Commands](docs/evolution-loop.md#commands).

## Settings

Set them in the **Evolution** tab of the dashboard, in the menu (screen 6), in Hermes Desktop under Settings → Plugins, or in the profile's `config.yaml` under `plugins.entries.hermes-atlas-evolution.settings`. A change applies within a minute, without restarting the gateway.

| Option | Default | What it does |
|---|---|---|
| `lessons` | `auto` | `auto`: template lessons apply once your own turns hit the failure, are measured and are retired when they do not help; text written by a model or by the agent waits for you. `suggest`: every lesson waits for you. `off`: measure only. |
| `auto_actions` | `safe` | What the plugin does by itself at the end of each pass, each action in the history with its undo. `safe`: takes out copies of a memory entry that are the very same text. `full`: also locks skills the curator keeps rewriting (at most 3 per pass). `off`: everything waits for you. |
| `owner_sender_ids`, `owner_platforms` | `[]` | Who is you in chats: your sender ids, and platforms where every chat is yours (a private bot, your own interface). |
| `language` | `auto` | `en` or `pt`. `auto` follows the profile language. |
| `reflector` | `true` | Drafts lessons with an AI model, through Hermes, for failures with no template (at most 6 calls a day); the drafts wait for you. |
| `autopilot_minutes` | `60` | The background learning pass, at most once per interval (15 to 1440 minutes; `0` = only by hand). |
| `runtime_guard` | `true` | Vetoes agent commands that would stop the gateway or delete what Hermes runs on. |
| `agent_tools`, `tool_hints`, `feedback_signals`, `slow_turn_seconds` | | See [Settings and language](docs/evolution-loop.md#settings-and-language). |

`owner_sender_ids`, `owner_platforms`, `agent_tools: true`, `runtime_guard: false` and `auto_actions: full` count only from Hermes' configuration, never from the plugin's own `settings.json`, which the agent's file tools can write.

## Privacy and safety

- **What it keeps.** Counters, failure classes, hashed session ids, lessons and proposals, under `<HERMES_HOME>/evolution/`; no conversation text, prompt, tool argument or output. A copy of a memory file when an entry is taken out, and of the package list and `/etc` before a system update, owner-only.
- **What it reads**, read-only: Hermes' history, logs, skill records, its gateway and database files; for the system, the package manager's state ([full list](docs/hermes-integration.md#what-the-plugin-reads)).
- **Network and model.** No connection of its own and no telemetry. Only the reflector calls a model, through Hermes. `hermes doctor`, run only on your click, calls the providers' APIs (and pings an `/anthropic` endpoint with one token); everything else here spends nothing.
- **Decisions.** Only you decide, in your own terminal or in a dashboard with a login. The plugin changes its own data; its settings with `hermes config set`; Hermes' memory only to take out a repeat; skills only with `hermes curator lock`; the system only when you run its update. Each change has its undo or its copy.

Details: [SECURITY.md](SECURITY.md).

## Documentation

- [The evolution loop](docs/evolution-loop.md): lessons, the reflector, automatic actions, Hermes' memory, the menu, every command and setting.
- [Maintenance](docs/maintenance.md): Hermes' health and the operating system's update, step by step.
- [Hermes integration](docs/hermes-integration.md): hooks, the dashboard panel, the runtime guard, what the plugin reads.
- [Updating](docs/upgrading.md): updating the plugin, and moving from 0.5.0.
- [Operations](docs/operations.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md).

## License

Atlas Evolution is free and open-source software, released under the [MIT License](LICENSE). You may use, copy, modify, merge, publish, distribute, sublicense and sell copies, including for commercial use, provided the copyright notice and the license text are kept in every copy. The software is provided as is, without warranty of any kind. Contributions are welcome under the same license; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Author

Created by **Sérgio Sant'Ana Júnior**. [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/)
