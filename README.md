![Atlas carrying a celestial sphere over a black technical grid. Atlas Evolution.](docs/banner.jpg)

# Atlas Evolution

**Atlas Evolution looks after your Hermes Agent for you.** It looks at every profile of your Hermes (the agent's errors, skills, memory, plugins, the gateway and the server), fixes by itself what is safe to fix, without the agent and without spending tokens, and turns the rest into short suggestions in plain words, with the numbers, a clear recommendation and one key to apply. Everything it changes can be undone.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Hermes Agent 0.23.1 or later](https://img.shields.io/badge/Hermes%20Agent-%E2%89%A5%200.23.1-6f42c1.svg)](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1)
[![Linux](https://img.shields.io/badge/system%20updates-Ubuntu%20%7C%20Debian-e95420.svg)](docs/maintenance.md)

## What it does, in practice

A Hermes that has run for weeks piles up problems nobody sees: the agent repeats the same mistake, two plugins fight over the same job, installed plugins never load, skills sit unused or duplicated, memory fills with repeats (and it goes into every prompt), the disk fills, the server misses updates. Atlas Evolution (`hermes-atlas-evolution` in Hermes) finds these and fixes them:

| | In practice |
|---|---|
| **Stops repeated mistakes** | A tool error that keeps coming back becomes a one-sentence lesson the agent reads. The plugin measures whether it helped and drops the ones that did not. |
| **Tidies the whole environment** | Looks at every profile at once and points out plugins that change the same tool's result (and which one gets lost), two plugins doing one job, plugins removed from the Hermes catalog, archived or unmaintained, plugins installed that never load, skills unused, duplicated, alike or copied into many profiles, and leftovers of failed installs. It knows Hermes' official plugin catalog (over 2,300 plugins) and which plugins are archived or unmaintained. |
| **Saves tokens** | Takes repeats out of Hermes' memory, locks skills the curator keeps rewriting, and shows what makes each call heavy. |
| **Keeps Hermes healthy** | The gateway alive or stuck, the session database, the disk (and the day it fills), memory, swap, CPU, old tool versions. Runs Hermes' own `doctor`, repair and cleanup, with a backup first. |
| **Keeps the server up to date** | Updates Ubuntu or Debian with checks before and after, a safety copy, your configuration files kept, and a repair if the power goes in the middle. |
| **Protects Hermes from its own agent** | Blocks agent commands that would take down or delete what Hermes runs on. |

### The Mode auto (default)

In **Mode auto** Atlas Evolution thinks and acts for you. One profile looks after the whole Hermes and sees every profile, without the plugin having to be installed in each one. By itself it only does what is safe and can be undone.

In each profile where it runs (with that profile's own mode):

- takes out identical copies of a memory entry (a copy of the file is kept);
- locks skills the curator keeps rewriting;
- frees old tool versions, at most once a week.

In the other profiles, from the profile that looks after the whole Hermes:

- installs itself, at the same version, in profiles in use that never had it (never where you turned it off or removed it); Hermes downloads it from GitHub;
- moves folders left by failed installs to its trash (kept 30 days).

What could break something (turning off one of two plugins, removing plugins, archiving skills) waits for you, with its reason and the key or command to apply. Before it changes anything in a profile, the plugin reads that profile again from disk: a stored suggestion is never trusted. Every action goes to the history with its undo, and an action you undo is never repeated. Rather decide everything yourself? Switch to **Assisted** or **Manual** in the menu (screen 5) or the dashboard.

**Updating from 0.8.4 with the default settings switches you to Mode auto.** Its first pass looks at every profile and may, by itself: take out identical memory copies, lock skills the curator keeps rewriting, run `hermes pm gc` if old tool versions take 300 MB or more, install the plugin in profiles used in the last 7 days that never had it, and move failed install folders to its trash. To look first and decide by hand, run `hermes config set plugins.entries.hermes-atlas-evolution.settings.mode assist` before updating, then see screen 14.

Its data stays on your machine, it opens no network connection of its own, and the agent never decides anything.

## See it

### In the Hermes dashboard

The **Evolution** tab of the Hermes dashboard (and of Hermes Desktop). With a dashboard login you decide there, for every profile.

![Overview: active lessons, what waits for your decision with Approve all, and the commands that share lessons with another profile](docs/screenshots/panel-overview-en.png)

![Proposals in plain words: lock a skill that keeps being rewritten, with what was seen, what it does, how to undo it, and the Apply now button](docs/screenshots/panel-proposals-en.png)

![Health: the gateway, free disk and memory, swap, the session database, old tool versions, system updates and a reboot waiting, with what needs attention](docs/screenshots/panel-health-en.png)

![Environment: every profile with its sessions, plugins and skills, the Hermes plugin catalog, and what to do, each with what was seen and the recommendation](docs/screenshots/panel-environment-en.png)

### In the terminal

`hermes evolution` opens the same, as a numbered menu, in your own terminal (never in an agent's shell): what needs attention, lettered suggestions that act at once, and 14 screens.

![The terminal menu: summary, what needs attention, suggestions a to f and the numbered screens](docs/screenshots/terminal-menu-en.png)

![The Environment screen: every profile of this Hermes, the plugin catalog, and what to fix across them](docs/screenshots/terminal-environment-en.png)

![One finding opened: two plugins change the same tool's result, which one wins, what to do, the command it runs and its undo](docs/screenshots/terminal-finding-en.png)

![The Operating system screen: version, updates waiting, reboot, automatic security updates, dpkg and apt, ready to update, and the actions](docs/screenshots/terminal-os-en.png)

## Quick start

Requires Hermes Agent 0.23.1 or later. Hermes loads plugins per profile, so install it in each profile you use.

```bash
hermes plugins install sesantanajr/hermes-atlas-evolution --enable              # the default profile
hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable # each named profile
hermes evolution                                                                # the menu
```

Installing loads the plugin into a running gateway; no restart is needed. The first look reads up to 14 days of the profile's history, so it does not start from zero. In Mode auto the plugin then installs itself in the other profiles you use. To update it: [Updating](docs/upgrading.md).

## What's new in 0.8.5

- **Mode auto, on by default.** Atlas Evolution now looks after every profile of your Hermes at once and does by itself what is safe and can be undone; the rest becomes a suggestion with its reason and one key. Assisted and Manual are still there.
- **Environment: every profile, plugin and skill** (screen 14, the Environment tab, `hermes evolution environment`). It reads the plugins' code without running it to know which tools each one changes, and reports a conflict only when two plugins can change the same tool (as far as their code shows), saying which one wins, which one is lost and what to turn off (never a security plugin). It also finds duplicate plugins, plugins removed from the Hermes catalog, archived or unmaintained, installed and never loaded, newer versions in the catalog, skills unused, duplicated, alike or copied into many profiles, idle profiles and leftovers of failed installs.
- **A knowledge base in the plugin's database.** Hermes' official plugin catalog (over 2,300, with the ones Hermes removed from it), what the plugin knows about well-known plugins, and the playbook of every situation, with its action and undo, are tables of `evolution.sqlite3`.
- **Clear recommendations.** Every finding says what was seen, what to do, the command it runs and how to undo it. The vague "plugins compete for a hook" observation is gone: two plugins on different tools are no conflict.
- **Automatic maintenance.** Frees old tool versions (`hermes pm gc`) once a week. Every change: [CHANGELOG](CHANGELOG.md).

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
| `environment [show \| scan \| catalog]` | Every profile of this Hermes: plugins, skills, conflicts and what to do; `scan` looks again now; `catalog` the Hermes plugin catalog kept in the plugin's database. |
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
| `mode` | `auto` | What the plugin does by itself. `auto`: thinks and acts for you in every profile, only what is safe and can be undone (see [The Mode auto](#the-mode-auto-default)). `assist`: only identical memory copies by itself; the rest waits for one key. `manual`: measures and proposes; nothing changes by itself. |
| `owner_sender_ids`, `owner_platforms` | `[]` | Who is you in chats: your sender ids, and platforms where every chat is yours (a private bot, your own interface). |
| `language` | `auto` | `en` or `pt`. `auto` follows the profile language. |
| `reflector` | `true` | Drafts lessons with an AI model, through Hermes, for failures with no template (at most 6 calls a day); the drafts wait for you. |
| `autopilot_minutes` | `60` | The background learning pass, at most once per interval (15 to 1440 minutes; `0` = only by hand). |
| `runtime_guard` | `true` | Vetoes agent commands that would stop the gateway or delete what Hermes runs on. |
| `agent_tools`, `tool_hints`, `feedback_signals`, `slow_turn_seconds` | | See [Settings and language](docs/evolution-loop.md#settings-and-language). |

`owner_sender_ids`, `owner_platforms`, `agent_tools: true`, `runtime_guard: false` and any `mode` other than `manual` count only from Hermes' configuration, never from the plugin's own `settings.json`, which the agent's file tools can write.

## Privacy and safety

- **What it keeps.** Counters, failure classes, hashed session ids, lessons and proposals, under `<HERMES_HOME>/evolution/`; no conversation text, prompt, tool argument or output. A copy of a memory file when an entry is taken out, owner-only; before a system update, the package list and a copy of `/etc` in a root-only folder outside every profile (`/var/lib/hermes-atlas-evolution/system/`).
- **What it reads**, read-only: Hermes' history, logs, skill records, its gateway and database files; every profile's `config.yaml`, plugins (their manifest, git checkout and Python source, parsed and never run) and skills; Hermes' cached plugin catalog; for the system, the package manager's state ([full list](docs/hermes-integration.md#what-the-plugin-reads)).
- **Network and model.** No connection of its own and no telemetry. When Mode auto installs the plugin in another profile, Hermes downloads it from GitHub. Only the reflector calls a model, through Hermes. `hermes doctor`, run only on your click, calls the providers' APIs (and pings an `/anthropic` endpoint with one token); everything else here spends nothing.
- **Decisions.** Only you decide, in your own terminal or in a dashboard with a login. The plugin changes its own data; its settings with `hermes config set`; Hermes' memory only to take out a repeat; skills only with `hermes curator lock`; plugins of any profile only with Hermes' own `hermes -p <profile> plugins disable|enable|install`; failed install folders by moving them to its trash; the system only when you run its update. Each change has its undo or its copy.

Details: [SECURITY.md](SECURITY.md).

## Documentation

- [The evolution loop](docs/evolution-loop.md): lessons, the reflector, automatic actions, Hermes' memory, the menu, every command and setting.
- [Maintenance](docs/maintenance.md): Hermes' health and the operating system's update, step by step.
- [Hermes integration](docs/hermes-integration.md): hooks, the dashboard panel, the runtime guard, what the plugin reads.
- [Updating](docs/upgrading.md): updating the plugin.
- [Operations](docs/operations.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md).

## License

Atlas Evolution is free and open-source software, released under the [MIT License](LICENSE). You may use, copy, modify, merge, publish, distribute, sublicense and sell copies, including for commercial use, provided the copyright notice and the license text are kept in every copy. The software is provided as is, without warranty of any kind. Contributions are welcome under the same license; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Author

Created by **Sérgio Sant'Ana Júnior**. [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/)
