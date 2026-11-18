![Atlas carrying a celestial sphere over a black technical grid. Atlas Evolution.](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/banner.jpg)

# Atlas Evolution

**The self-improvement and care layer for Hermes Agent.** It learns from the agent's mistakes, measures whether it is getting better, says why and what to do first, and keeps every profile healthy, without spending tokens.

**English** · Português: [leia nesta página](#português-brasil) · [README.pt-BR.md](README.pt-BR.md)

[![Checks](https://github.com/sesantanajr/hermes-atlas-evolution/actions/workflows/checks.yml/badge.svg)](https://github.com/sesantanajr/hermes-atlas-evolution/actions/workflows/checks.yml)
[![Version](https://img.shields.io/github/v/tag/sesantanajr/hermes-atlas-evolution?label=version&sort=semver&color=2ea44f)](CHANGELOG.md)
[![Hermes Agent 0.23.1 or later](https://img.shields.io/badge/Hermes%20Agent-%E2%89%A5%200.23.1-6f42c1)](https://github.com/NousResearch/hermes-agent)
![Hermes install scan: safe](https://img.shields.io/badge/Hermes%20install%20scan-safe-2ea44f)
![Python 3.11 to 3.14](https://img.shields.io/badge/python-3.11%20%E2%80%93%203.14-3776ab)
![No dependencies](https://img.shields.io/badge/dependencies-none%20(stdlib%20only)-555)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

---

## English

[Documentation](docs/en-US/README.md) · [Changelog](CHANGELOG.md)

### Why

A Hermes that runs for weeks piles up problems nobody sees: the agent repeats the same mistake, two plugins fight over one job, plugins are installed and never load, skills sit unused or duplicated, memory fills with repeats that go into every prompt, the gateway stops after an update, the disk fills, the server misses its updates.

**Atlas Evolution** (`hermes-atlas-evolution`) watches all of it from inside Hermes and turns it into short proposals in plain words, with the numbers, a recommendation and one key to apply. By default it changes almost nothing by itself; in Auto mode, which you turn on, it also fixes what is safe and can be undone. It is written for Hermes, with Hermes' own plugin API and commands, in English and Brazilian Portuguese.

### Highlights

| | |
|---|---|
| 🧠 **Learns from mistakes** | A tool error that keeps coming back becomes a one-sentence lesson the agent reads. The failure is read the way a person would (in a traceback, only the exception; in long output, only its end), each lesson is measured from the first time the agent saw it, and the ones that do not help are retired. |
| 📈 **Shows whether the agent evolves, and why** | The **Evolution index**: one 0–100 score per profile and day, from tool failures, failed or slow turns, your corrections, lessons that worked and Hermes' health, with its 7-day trend. When it moves, it says which part and what moved it (`tools 92 → 62 · terminal +147 errors`). |
| 🎯 **Says what to do first** | Up to 3 steps ranked by the points each could add to the index, each with its command. A failure that jumps today (a dependency, a key, a service or a quota that changed) is flagged the same day. |
| 🩺 **Keeps Hermes healthy** | Runs Hermes' own `hermes doctor` checks on every profile once a day (offline), watches the gateway, the session database, disk, memory and old tool versions, and explains each problem with the command that fixes it. |
| 🧹 **Tidies every profile** | Plugins that conflict, duplicate or never load; skills unused, duplicated or near twins; leftovers of failed installs; entries repeated between Hermes' memory and user profile. |
| 🔁 **Repairs, carefully** | A damaged database of its own is set aside and started again. In Auto mode, a gateway that cannot start because Hermes replaced its Python is pointed at the new one, only when no gateway of this Hermes is alive, with step-by-step undo. |
| 🛡️ **Protects Hermes from its own agent** | The runtime guard vetoes agent commands that would restart or kill the gateway, delete, truncate or change the permissions of what Hermes runs on, or switch this plugin off, even when hidden in `bash -c`, variables, `watch`, here-documents or `busybox`. |
| 🐧 **Keeps the server up to date** | Ubuntu and Debian updates you start, with checks before and after, a safety copy and a repair if the power goes in the middle. |
| ⚡ **Costs nothing you can feel** | Every hook is timed where it runs and shown in `status`: typically well under a millisecond per tool call, with a warning if that ever grows. |

### What it does not do

- **It has no network access of its own and sends no telemetry.** What reaches the network goes through Hermes or apt, and only as listed under Disclosure below.
- **It never updates itself.** New code comes only from Hermes: `hermes -p <profile> plugins update hermes-atlas-evolution`, "Update all" in Hermes Desktop and the dashboard, or Hermes' own `plugins.auto_apply`.
- **It calls no model except the reflector**, which is on by default and goes through Hermes, at most 6 calls a day by default: it sends a redacted excerpt of a tool error to the model you configured for it, and its drafts always wait for you.
- **It keeps nothing anyone said**: no conversation text, prompt or tool argument goes into its database. Of tool output, it keeps only a redacted excerpt of an error (at most 300 characters) for the reflector, until a draft exists or 14 days pass.
- **It never reads `.env`, `auth.json` or any other credential store**, and it stores no credentials.
- **It does not update Hermes itself** or change a model, key or provider: it points out the problem and the command.
- **It never turns off or removes plugins, nor archives skills, by itself**: that always waits for you.
- **It is not a sandbox.** The runtime guard is a safety net against the agent's accidents, not against someone set on getting around it, and it is off with `plugins.isolation: host`.
- **It does not replace your backups**: the copies it keeps are there to undo its own actions.

### Disclosure

What a cautious reader would want to know before installing, checked against the code of this version. [SECURITY.md](SECURITY.md) has the full list, with every time limit.

**Network.** None of its own, and no telemetry. Three things can reach the network, each through someone else's tool:

- Hermes' own `plugins install` and `plugins update`, which download the plugin from GitHub (for a catalog install, at the catalog's reviewed pin): when you run `evolution update` or `install.sh`, and when Auto mode installs the plugin in another profile by its catalog name.
- The reflector (on by default): a redacted error excerpt goes to the model you configured for Hermes' auxiliary task `evolution_reflector`, through Hermes, at most 6 calls a day by default.
- Commands you run yourself: `evolution system plan`, `upgrade` and `repair` (as root) run `apt-get`, and `evolution care doctor` runs Hermes' full `hermes doctor`, which calls your providers' APIs.

**Reads outside its own data.** Read-only, size-capped, in every profile of this Hermes home:

- each profile's `config.yaml` (it may hold keys: they are never logged or stored), `state.db` (opened read-only), `logs/agent.log`, `memories/MEMORY.md` and `USER.md`, skills (`SKILL.md`, `skills/.usage.json`, the curator ledger) and `cron/jobs.json`;
- `plugins/`, with Hermes' `.install-metadata.json` and each plugin's `plugin.yaml` and `.git` files, and up to 4 MB of each other plugin's Python source, parsed and never imported;
- Hermes' plugin catalog cache, `tools/facts.json`, and the gateway's pid, state and heartbeat files;
- on Linux: `/proc` (memory, CPU, locks, uptime, and the `exe`, `cmdline` and `stat` of processes), Hermes' systemd units (`systemctl show`, `list-units`), `loginctl`, apt and dpkg state, and `/etc/os-release`.

**Writes outside its own data.**

- In Assist mode (the default): `memories/MEMORY.md`, to take out an entry repeated word for word in `USER.md`, after keeping a copy of the file.
- In Auto mode: also the Hermes commands below, a move of a failed `plugins/.install-*` folder into its trash, and the gateway's systemd unit file when it repairs it (a `.bak` copy is kept beside it).
- On your request only: the memory entry you choose to take out (a copy kept first), this plugin's settings through `hermes config set`, and, for the weekly digest, a script under `<profile>/scripts/` and a Hermes cron job.
- `evolution system upgrade` and `repair`, which you run as root, keep a root-only folder `/var/lib/hermes-atlas-evolution/system/` (the latest 5 runs) with the package lists, the run's script, log and report, and **a tarball of `/etc` taken before the update. `/etc` holds the machine's credentials** (password hashes, service keys and certificates): the folder is `0700` and the files `0600`, but treat it like `/etc` itself.

**Commands it runs.** Hermes' own CLI for one named profile (`hermes -p <profile> …`), with an argument list and no shell. Child processes never inherit an approval bypass (`HERMES_YOLO_MODE`, `HERMES_YOLO`, `HERMES_ACP_AUTO_APPROVE` and the others listed in SECURITY.md), so Hermes' approvals stay on.

| Command | When | By itself? |
|---|---|---|
| `curator lock`, `curator unlock` | Lock a skill the curator keeps rewriting; unlock is the undo | Auto: the lock (at most 3 changes per pass). Also your key. Unlock: your undo only |
| `plugins install hermes-atlas-evolution` | This plugin in a profile in use that never had it | Auto: only by its catalog name, when the lead profile is a catalog install. From the repository: your key, `evolution update --install-missing` or `install.sh` |
| `plugins update hermes-atlas-evolution` | Update this plugin | Never: `evolution update` or `install.sh`, which you run |
| `plugins remove hermes-atlas-evolution` | Undo of an install | Your undo only |
| `plugins disable`, `plugins enable` | Turn off one of two plugins in conflict; enable is the undo | Never: your key, one proposal at a time |
| `pm gc` | Free old tool versions nothing references | Auto: at most weekly, from the lead profile, never while a folder it deletes is in use. Cannot be undone. Also your click |
| `pm doctor`, `doctor` | Hermes' own checks | Never: your click (`care pm-doctor`, `care doctor`) |
| `backup --quick`, then `doctor --fix` | Hermes' own repair, after a quick backup | Never: your click (`care fix`) |
| `config set plugins.entries.hermes-atlas-evolution.settings.<key>` | Change this plugin's settings | Never: your change in the menu or the panel |
| `cron create … --no-agent`, `cron remove` | The weekly digest, and its undo | Never: your `schedule-digest` |
| `systemctl [--user] daemon-reload`, `reset-failed`, `start --no-block` of the gateway's unit | Right after repairing that unit | Auto: a user service only, at most once a day, only when no gateway is alive. Also your `care fix-gateway` |
| `apt-get update`, a simulation (`apt-get -s`), then the upgrade or repair through `systemd-run` | `evolution system plan`, `upgrade`, `repair` | Never: only you, in your own terminal (the upgrade and the repair as root) |

The daily system check also runs read-only commands: `apt list --upgradable`, `dpkg --audit`, `apt-get check` (without taking apt's lock), `apt-mark showhold`, `uname -r`, `systemctl show` and `list-units`, and `loginctl show-user`.

**What the agent sees.**

- Learned lessons, as a section of the system prompt of new sessions: at most 8 lines and 1,200 characters.
- In a direct chat whose sender is you, the lessons that chat's prompt left out, once, added to your first message (at most 5 lines, 900 characters).
- With `tool_hints` on (off by default), the matching lesson added to a failing tool's result.
- The runtime guard (`pre_tool_call`): it vetoes agent commands that stop or restart the gateway, delete or overwrite what Hermes runs on, or change this plugin's settings; the agent reads a fixed refusal instead of the result.
- One tool, `hermes_atlas_evolution_lessons`, only when `agent_tools` is on (off by default).

**Background work.**

- In long-running Hermes processes (the gateway, the dashboard, the TUI or Desktop backend, `hermes serve`, an ACP server, an interactive CLI, a plugin host): a clock thread that wakes every 10 minutes, and at most one learning pass per interval (`autopilot_minutes`, 60 by default) for each profile.
- Once a day for each profile in use, the lead profile starts a short-lived child of Hermes' own Python that runs Hermes' doctor checks offline (the provider probes, `npm audit`, the Skills Hub and the terminal backend are skipped). Like `hermes doctor`, it loads that profile's plugins, so their code runs in that child. It is stopped at 120 seconds.

**Stored data.**

- `<profile>/evolution/`: an SQLite database (counters, failure classes, lessons, proposals, decisions, and the redacted error excerpts the reflector waits for), `settings.json`, its trash (emptied after 30 days) and the copies of memory files it took an entry out of. The lead profile's database also keeps the last result of Hermes' doctor checks per profile, as Hermes wrote it (it can name files and folders).
- `<root>/evolution/`: the fleet state (the profiles where the plugin was seen, the automatic changes you undid) and the gateway-repair journal.
- The optional weekly digest: a script under `<profile>/scripts/` and a Hermes cron job.

### How it works

- **Observe**: hooks count what happens (outcome, duration, failure class), never message text or tool arguments.
- **Learn**: a background pass turns recurring failures into lessons, measures their effect, reads every profile and runs Hermes' own health checks.
- **Explain**: the index says what moved it, a failure that jumped today and what to do first, from a few queries over the numbers the plugin already keeps, with no model call.
- **Act**: in Assist mode it only takes out a memory entry repeated word for word. In Auto mode it applies what is safe and reversible, checking the disk and the services again right before it acts. The rest becomes a proposal with its undo.
- **Show**: `status`, the weekly digest, the terminal menu and the **Evolution** tab of the dashboard, with the Evolution index on top.

### Quick start

Requires **Hermes Agent 0.23.1 or later**. Hermes loads plugins per profile: for a named profile, add `-p <profile>` to each command (`hermes -p work plugins install hermes-atlas-evolution`).

```bash
# From the Hermes plugin catalog (the reviewed version), then open the menu
hermes plugins install hermes-atlas-evolution
hermes plugins enable hermes-atlas-evolution
hermes evolution
```

```bash
# Or from GitHub (also if Hermes says the name is not in the catalog)
hermes plugins install sesantanajr/hermes-atlas-evolution --enable
```

```bash
# Every profile at once, installing where it is missing
git clone https://github.com/sesantanajr/hermes-atlas-evolution.git
sh hermes-atlas-evolution/install.sh --all --install-missing             # from GitHub
sh hermes-atlas-evolution/install.sh --all --install-missing --catalog   # from the Hermes plugin catalog
```

`install.sh` only runs Hermes' own commands: `plugins update hermes-atlas-evolution` where the plugin is, and `plugins install` where it is missing. A copy pinned to a commit is left as it is, with the command that moves it. Without `--all` it lists the profiles and asks which; add `--yes` to skip the question.

Installing loads the plugin into a running gateway, with no restart. The first pass replays up to 14 days of the profile's history, so it does not start from zero. Next steps: [Getting started](docs/en-US/getting-started.md). On an earlier version? See [Updating to 0.13.0](docs/en-US/upgrading.md#updating-to-0130).

### Modes

| Mode | By itself | Waits for you |
|---|---|---|
| **Assist** (default) | Only takes out an entry repeated word for word between memory (`MEMORY.md`) and the user profile (`USER.md`), after keeping a copy of the file. | Everything else. |
| **Auto** (you turn it on) | Also, in every profile of this Hermes: locks skills the curator keeps rewriting; moves failed `.install-*` folders to its trash (kept 30 days); repairs the gateway's service when no gateway is alive; frees old tool versions (`hermes pm gc`, at most weekly); installs the plugin where a profile in use never had it, by itself only from the Hermes plugin catalog, by name (when the lead profile is a catalog install; from the repository, only on your key). All with undo, except freeing old tool versions, which cannot be taken back. | Anything that could break something: turning a plugin off, removing or updating plugins, archiving skills. |
| **Manual** | Nothing. It measures and proposes. | Everything. |

Every action goes to the history with its undo, and an action you undo is never repeated. Turn Auto mode on in the menu (screen 5), the dashboard or with `hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.mode auto`. When `config.yaml` cannot be read (a Python without PyYAML, for example), the plugin acts as Manual and says so first in `status`. To keep the lead profile's hands off a profile, turn `fleet_managed` off in that profile.

### See it

**The Evolution tab of the Hermes dashboard** (and of Hermes Desktop): decide there, for every profile, with a dashboard login. Without a login it only shows, and hides the text of lessons written by you, the reflector or the agent.

![Overview: active lessons, proposals waiting for you, the last learning pass and the commands that share lessons with another profile](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/panel-overview-en.png)

**The terminal menu**, `hermes evolution`, in your own terminal: what needs attention, lettered suggestions that act at once, and 14 screens.

![The terminal menu: summary, what needs attention, suggestions a to f and the numbered screens](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/terminal-menu-en.png)

### More screens

- [Proposals](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/panel-proposals-en.png): in plain words, each with what was seen, what to do, how to undo it and the Apply now button.
- [Health](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/panel-health-en.png): the gateway, disk, memory, swap, the session database, old tool versions and system updates.
- [Environment](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/panel-environment-en.png): Hermes' doctor for each profile, its plugins and skills, and what to do.
- [The Environment screen of the terminal menu](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/terminal-environment-en.png), by risk.
- [One finding opened](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/terminal-finding-en.png): which plugin wins, what to do, the command and its undo.
- [The Operating system screen](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/terminal-os-en.png): updates waiting, reboot, automatic security updates, dpkg and apt.

### Commands

`hermes -p <profile> evolution <command>`; `--lang en|pt` works with all of them, and `--format md|json` with all but `menu`.

| Command | What it does |
|---|---|
| *(none)*, `menu` | The interactive menu (in an agent's shell or a pipe, it prints `status`). |
| `status` | What needs attention, the Evolution index and what moved it, what to do first, what the plugin costs, Hermes' and the system's health, every profile. |
| `report [--days N]` | The digest of the last N days (7 by default), against the period before, with what to do first. |
| `learn` | A learning pass now. |
| `lessons list \| review \| approve <id> \| retire <id> \| add \| export \| import` | The lessons, your decisions, a lesson you write yourself, and sharing them with another profile. |
| `proposals list \| approve <id> \| reject <id>` | The proposals; approving one the plugin can apply runs Hermes' own command. |
| `decisions list \| undo <id>` | Every decision and automatic action, and the undo of one. |
| `care [check \| doctor \| pm-doctor \| fix \| gc \| fix-gateway \| undo-gateway]` | Hermes' health and repairs, including a gateway service that still starts a removed Python. |
| `environment [show \| scan \| catalog]` | Every profile: plugins, skills, conflicts and what to do. |
| `update [--all \| --profiles a,b] [--install-missing] [--list] [--yes]` | Runs Hermes' own `plugins update hermes-atlas-evolution` in each profile you pick, this one first. With `--install-missing`, a profile without the plugin gets the code this profile runs (its catalog name, or this repository at this profile's commit). A copy Hermes pinned with `--ref` moves only to this profile's commit, and only when both come from this repository. It never picks a commit. |
| `system [check \| plan \| upgrade \| repair \| report]` | The operating system's update (Linux with apt). |
| `schedule-digest [--deliver <channel>]` | The weekly digest as a Hermes cron job with no agent and no tokens. |
| `usage` | The model and tool calls measured, and their estimated cost. |
| `owner list \| add \| remove \| add-platform \| remove-platform` | Which chat senders and channels count as you. |
| `skills` | Skills rewritten over and over, flip-flopping, or rewritten and never used. |
| `experiments` | The tracked changes, with their before-and-after verdicts. |

Full reference: [Commands](docs/en-US/evolution-loop.md#commands).

### Settings

In the dashboard's **Evolution** tab, the menu (screen 6), Hermes Desktop (Settings → Plugins) or `config.yaml` under `plugins.entries.hermes-atlas-evolution.settings`. Changes apply within a minute; `runtime_guard`, `tool_hints` and turning `agent_tools` or the `reflector` on apply after `hermes gateway restart`.

| Option | Default | What it does |
|---|---|---|
| `mode` | `assist` | What the plugin does by itself: `assist`, `auto` or `manual` (see Modes above). |
| `lessons` | `auto` | `auto`: template lessons apply once your own turns hit the failure; text written by a model waits for you. `suggest`: every lesson waits. `off`: measure only. |
| `owner_sender_ids`, `owner_platforms` | `[]` | Who is you in chats: your sender ids, and channels where every chat is yours. In your direct chats you get every lesson from your first message. |
| `fleet_managed` | `true` | Off in this profile: the lead profile never installs anything or moves anything to its trash here by itself. |
| `language` | `auto` | `en` or `pt`; `auto` follows the profile. |
| `reflector`, `reflector_daily_limit` | `true`, `6` | Drafts lessons with a model, through Hermes, for failures with no template; drafts wait for you. |
| `autopilot_minutes` | `60` | The background learning pass (15 to 1440; `0` = by hand only). |
| `runtime_guard` | `true` | Vetoes agent commands that would take Hermes down. |
| `agent_tools`, `tool_hints`, `feedback_signals`, `slow_turn_seconds` | | See [Settings and language](docs/en-US/evolution-loop.md#settings-and-language). |

Anything that widens what the plugin does or whom it trusts counts only from Hermes' configuration, never from the plugin's own `settings.json`, which the agent's file tools can write: who you are in chats, the agent's tools, switching the runtime guard off, a mode other than Manual, adding lessons to tool results (`tool_hints`) and a reflector limit above 6 a day. `settings.json` can only narrow.

### Compatibility

| | |
|---|---|
| **Hermes Agent** | 0.23.1 or later, tested on 0.23.1 and 0.23.2; one plugin copy per profile; works with `plugins.isolation: host` (the runtime guard then stays off). Hermes' own install scan says `safe` on both (`plugin-guard-v12` of 0.23.1 and `plugin-guard-v15` of 0.23.2). |
| **Python** | 3.11 to 3.14, standard library only. The test suite runs in CI: Linux 3.11–3.14, macOS and Windows 3.13. |
| **Systems** | Linux: everything, tested inside a gateway. macOS and Windows: everything except the operating system's update (apt) and the systemd repairs; not tested inside a gateway there. |
| **Dashboard** | The **Evolution** tab of the Hermes dashboard and Hermes Desktop. |
| **Languages** | English and Brazilian Portuguese, end to end. |

### Privacy and safety

- **Keeps** counters, failure classes, hashed session ids, lessons, proposals and, for the reflector, redacted error excerpts in `<HERMES_HOME>/evolution/`; no conversation text, prompt or tool argument.
- **Reads**, read-only and size-capped, Hermes' history, logs, skills, plugins (their source parsed, never imported) and health files of every profile ([full list](docs/en-US/hermes-integration.md#what-the-plugin-reads)). The daily doctor child loads each profile's plugins, as `hermes doctor` does.
- **Network**: none of its own, no telemetry, and no self-update. Only the reflector calls a model, through Hermes.
- **Only you decide**, in your own terminal or a dashboard with a login; the agent can never approve or widen anything. Each change has its undo or its copy.

Details: [SECURITY.md](SECURITY.md).

### Documentation

[Documentation home](docs/en-US/README.md): [Getting started](docs/en-US/getting-started.md) · [The evolution loop](docs/en-US/evolution-loop.md) · [Maintenance](docs/en-US/maintenance.md) · [Hermes integration](docs/en-US/hermes-integration.md) · [Updating](docs/en-US/upgrading.md) · [Troubleshooting](docs/en-US/troubleshooting.md) · [Changelog](CHANGELOG.md)

### What's new in 0.13.0

Ready for the Hermes plugin catalog.

- **No self-update, no network of its own.** The plugin never reads the network and never moves its own code. Updates come only from Hermes (`hermes -p <profile> plugins update hermes-atlas-evolution`, "Update all", or `plugins.auto_apply`). `evolution update` now runs that Hermes command in the profiles you pick, and never picks a commit.
- **Assist by default.** A new install, and a profile with no `mode` set, starts in Assist: by itself it only takes out a memory entry repeated word for word. Auto mode is your choice.
- **Install by catalog name.** Auto mode installs the plugin in another profile by itself only from the Hermes plugin catalog, by name, and no longer syncs copies across profiles. `install.sh --catalog` installs by catalog name too.
- **Hermes' approvals in every child.** The commands the plugin starts no longer inherit Hermes' YOLO or auto-approve variables.
- **Disclosure.** Everything the plugin reads, writes, runs and stores is listed above.
- **Stability fixes** in the runtime guard, the database, the commands it runs and the learning pass.

Every change: [Changelog](CHANGELOG.md).

### Contributing

Issues and pull requests are welcome; read [CONTRIBUTING.md](CONTRIBUTING.md) first. The test suite lives on the `tests` branch. Security problems go through [SECURITY.md](SECURITY.md).

### License

[MIT](LICENSE): free and open source, including commercial use, with the copyright notice kept.

### Author

Created by **Sérgio Sant'Ana Júnior** · [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/)

---

## Português (Brasil)

[Documentação](docs/pt-BR/README.md) · [Mudanças](CHANGELOG.pt-BR.md)

### Por quê

Um Hermes que roda há semanas acumula problemas que ninguém vê: o agente repete o mesmo erro, dois plugins brigam pelo mesmo trabalho, plugins instalados nunca carregam, skills ficam paradas ou duplicadas, a memória enche de repetições que vão em todo prompt, o gateway para depois de uma atualização, o disco enche, o servidor fica sem atualização.

O **Atlas Evolution** (`hermes-atlas-evolution`) acompanha tudo isso de dentro do Hermes e transforma em propostas curtas, em português, com os números, uma recomendação e uma tecla para aplicar. No padrão, ele quase não muda nada sozinho; no Modo Auto, que você liga, também resolve o que é seguro e pode ser desfeito. Ele é feito para o Hermes, com a API de plugins e os comandos do próprio Hermes, em português e inglês.

### Destaques

| | |
|---|---|
| 🧠 **Aprende com os erros** | Um erro de ferramenta que volta vira uma lição de uma frase que o agente passa a ler. A falha é lida como uma pessoa leria (num traceback, só a exceção; numa saída longa, só o fim), cada lição é medida a partir da primeira vez que o agente a viu, e as que não ajudam são aposentadas. |
| 📈 **Mostra se o agente evolui, e por quê** | O **Índice de evolução**: uma nota de 0 a 100 por perfil e por dia, a partir das falhas de ferramentas, dos turnos que falharam ou foram lentos, das suas correções, das lições que funcionaram e da saúde do Hermes, com a tendência de 7 dias. Quando ele muda, diz qual parte e o que a moveu (`ferramentas 92 → 62 · terminal +147 erros`). |
| 🎯 **Diz o que fazer primeiro** | Até 3 passos em ordem dos pontos que cada um pode somar ao índice, cada um com o seu comando. Uma falha que dispara hoje (uma dependência, uma chave, um serviço ou uma cota que mudou) é avisada no mesmo dia. |
| 🩺 **Mantém o Hermes saudável** | Roda as checagens do próprio `hermes doctor` em cada perfil uma vez por dia (sem rede), acompanha o gateway, o banco de sessões, o disco, a memória e versões antigas de ferramentas, e explica cada problema com o comando que resolve. |
| 🧹 **Arruma todos os perfis** | Plugins em conflito, duplicados ou que nunca carregam; skills sem uso, duplicadas ou quase iguais; restos de instalações que falharam; entradas repetidas entre a memória e o perfil do usuário do Hermes. |
| 🔁 **Repara, com cuidado** | Um banco do próprio plugin que se danificou é posto de lado e recomeçado. No Modo Auto, um gateway que não sobe porque o Hermes trocou o Python é apontado para o novo, só quando nenhum gateway deste Hermes está vivo e com desfazer passo a passo. |
| 🛡️ **Protege o Hermes do próprio agente** | A Proteção do Hermes barra comandos do agente que reiniciariam ou matariam o gateway, apagariam, truncariam ou mudariam a permissão daquilo em que o Hermes roda, ou desligariam este plugin, mesmo escondidos em `bash -c`, variáveis, `watch`, here-docs ou `busybox`. |
| 🐧 **Mantém o servidor atualizado** | Atualizações do Ubuntu e do Debian que você inicia, com checagens antes e depois, cópia de segurança e reparo se a energia cair no meio. |
| ⚡ **Não pesa** | Cada gancho é cronometrado onde roda e aparece no `status`: em geral bem menos de um milissegundo por chamada de ferramenta, com aviso se isso crescer. |

### O que ele não faz

- **Não tem acesso à rede próprio e não envia telemetria.** O que chega à rede passa pelo Hermes ou pelo apt, e só como está em Transparência, abaixo.
- **Nunca se atualiza sozinho.** Código novo só vem do Hermes: `hermes -p <perfil> plugins update hermes-atlas-evolution`, o "Atualizar todos" do Hermes Desktop e do dashboard, ou o `plugins.auto_apply` do próprio Hermes.
- **Não chama modelo, a não ser o refletor**, que vem ligado por padrão e passa pelo Hermes, no máximo 6 chamadas por dia no padrão: ele envia um trecho de um erro de ferramenta, sem dados sensíveis, ao modelo que você configurou para ele, e os rascunhos sempre esperam você.
- **Não guarda o que se fala**: nenhum texto de conversa, prompt ou argumento de ferramenta vai para o banco. Da saída das ferramentas, guarda só um trecho de um erro sem dados sensíveis (no máximo 300 caracteres) para o refletor, até haver um rascunho ou passarem 14 dias.
- **Nunca lê o `.env`, o `auth.json` nem outro lugar de credenciais**, e não guarda credenciais.
- **Não atualiza o próprio Hermes** nem troca modelo, chave ou provedor: aponta o problema e o comando.
- **Não desliga nem remove plugins e não arquiva skills sozinho**: isso sempre espera você.
- **Não é uma caixa de areia.** A Proteção do Hermes é uma rede de segurança contra acidentes do agente, não contra alguém decidido a contorná-la, e fica desligada com `plugins.isolation: host`.
- **Não substitui o seu backup**: as cópias que ele guarda servem para desfazer as próprias ações.

### Transparência

O que uma pessoa cuidadosa quer saber antes de instalar, conferido com o código desta versão. O [SECURITY.md](SECURITY.md) tem a lista completa, com cada limite de tempo.

**Rede.** Nenhum acesso próprio, e nenhuma telemetria. Três coisas podem chegar à rede, cada uma pela ferramenta de outro:

- O `plugins install` e o `plugins update` do próprio Hermes, que baixam o plugin do GitHub (numa instalação pelo catálogo, na versão revisada do catálogo): quando você roda o `evolution update` ou o `install.sh`, e quando o Modo Auto instala o plugin em outro perfil pelo nome no catálogo.
- O refletor (ligado por padrão): um trecho de erro sem dados sensíveis vai ao modelo que você configurou para a tarefa auxiliar `evolution_reflector` do Hermes, pelo Hermes, no máximo 6 chamadas por dia no padrão.
- Comandos que você mesmo roda: o `evolution system plan`, `upgrade` e `repair` (como root) rodam o `apt-get`, e o `evolution care doctor` roda o `hermes doctor` completo do Hermes, que chama as APIs dos seus provedores.

**O que lê fora dos próprios dados.** Só leitura, com limite de tamanho, em todos os perfis deste Hermes:

- o `config.yaml` de cada perfil (ele pode ter chaves: nunca vão para log nem são guardadas), o `state.db` (aberto só para leitura), o `logs/agent.log`, o `memories/MEMORY.md` e o `USER.md`, as skills (`SKILL.md`, `skills/.usage.json`, o registro do curador) e o `cron/jobs.json`;
- a pasta `plugins/`, com o `.install-metadata.json` do Hermes e o `plugin.yaml` e os arquivos `.git` de cada plugin, e até 4 MB do código Python de cada outro plugin, analisado e nunca importado;
- o cache do catálogo de plugins do Hermes, o `tools/facts.json` e os arquivos de pid, estado e batimento do gateway;
- no Linux: o `/proc` (memória, CPU, travas, tempo ligado, e o `exe`, o `cmdline` e o `stat` dos processos), as unidades systemd do Hermes (`systemctl show`, `list-units`), o `loginctl`, o estado do apt e do dpkg, e o `/etc/os-release`.

**O que grava fora dos próprios dados.**

- No modo Assistido (o padrão): o `memories/MEMORY.md`, para tirar uma entrada repetida palavra por palavra no `USER.md`, depois de guardar uma cópia do arquivo.
- No Modo Auto: também os comandos do Hermes abaixo, a mudança de uma pasta `plugins/.install-*` que falhou para a lixeira dele, e o arquivo da unidade systemd do gateway quando ele a repara (com uma cópia `.bak` ao lado).
- Só quando você pede: a entrada da memória que você escolhe tirar (com uma cópia antes), as configurações deste plugin pelo `hermes config set` e, para o resumo semanal, um script em `<perfil>/scripts/` e uma tarefa agendada (cron) do Hermes.
- O `evolution system upgrade` e o `repair`, que você roda como root, mantêm uma pasta só do root, `/var/lib/hermes-atlas-evolution/system/` (as 5 últimas execuções), com as listas de pacotes, o script, o log e o relatório da execução, e **um arquivo tar do `/etc` feito antes da atualização. O `/etc` guarda credenciais da máquina** (hashes de senha, chaves e certificados de serviços): a pasta é `0700` e os arquivos `0600`, mas trate-a como o próprio `/etc`.

**Comandos que ele roda.** A CLI do próprio Hermes para um perfil nomeado (`hermes -p <perfil> …`), com uma lista de argumentos e sem shell. Os processos filhos nunca herdam um desvio das aprovações (`HERMES_YOLO_MODE`, `HERMES_YOLO`, `HERMES_ACP_AUTO_APPROVE` e os outros listados no SECURITY.md), então as aprovações do Hermes continuam valendo.

| Comando | Quando | Sozinho? |
|---|---|---|
| `curator lock`, `curator unlock` | Travar uma skill que o curador reescreve sem parar; o unlock é o desfazer | Auto: a trava (no máximo 3 mudanças por rodada). Também a sua tecla. Unlock: só o seu desfazer |
| `plugins install hermes-atlas-evolution` | Este plugin num perfil em uso que nunca o teve | Auto: só pelo nome no catálogo, quando o perfil que cuida de todos veio do catálogo. Pelo repositório: a sua tecla, o `evolution update --install-missing` ou o `install.sh` |
| `plugins update hermes-atlas-evolution` | Atualizar este plugin | Nunca: o `evolution update` ou o `install.sh`, que você roda |
| `plugins remove hermes-atlas-evolution` | Desfazer uma instalação | Só o seu desfazer |
| `plugins disable`, `plugins enable` | Desligar um de dois plugins em conflito; o enable é o desfazer | Nunca: a sua tecla, uma proposta por vez |
| `pm gc` | Liberar versões antigas de ferramentas que nada usa | Auto: no máximo uma vez por semana, a partir do perfil que cuida de todos, nunca com uma pasta que ele apagaria em uso. Não tem volta. Também o seu clique |
| `pm doctor`, `doctor` | As checagens do próprio Hermes | Nunca: o seu clique (`care pm-doctor`, `care doctor`) |
| `backup --quick`, depois `doctor --fix` | O reparo do próprio Hermes, depois de um backup rápido | Nunca: o seu clique (`care fix`) |
| `config set plugins.entries.hermes-atlas-evolution.settings.<opção>` | Mudar as configurações deste plugin | Nunca: a sua mudança no menu ou no painel |
| `cron create … --no-agent`, `cron remove` | O resumo semanal, e o desfazer dele | Nunca: o seu `schedule-digest` |
| `systemctl [--user] daemon-reload`, `reset-failed`, `start --no-block` da unidade do gateway | Logo depois de reparar essa unidade | Auto: só um serviço de usuário, no máximo uma vez por dia, só quando nenhum gateway está vivo. Também o seu `care fix-gateway` |
| `apt-get update`, uma simulação (`apt-get -s`), depois a atualização ou o reparo pelo `systemd-run` | `evolution system plan`, `upgrade`, `repair` | Nunca: só você, no seu próprio terminal (a atualização e o reparo como root) |

A checagem diária do sistema também roda comandos só de leitura: `apt list --upgradable`, `dpkg --audit`, `apt-get check` (sem pegar a trava do apt), `apt-mark showhold`, `uname -r`, `systemctl show` e `list-units`, e `loginctl show-user`.

**O que o agente vê.**

- As lições aprendidas, numa seção do prompt de sistema das sessões novas: no máximo 8 linhas e 1.200 caracteres.
- Numa conversa direta em que o remetente é você, as lições que o prompt dessa conversa deixou de fora, uma vez, junto com a sua primeira mensagem (no máximo 5 linhas, 900 caracteres).
- Com `tool_hints` ligado (desligado por padrão), a lição que corresponde, junto ao resultado de uma ferramenta que falhou.
- A Proteção do Hermes (`pre_tool_call`): barra comandos do agente que param ou reiniciam o gateway, apagam ou sobrescrevem aquilo em que o Hermes roda, ou mudam as configurações deste plugin; o agente lê uma recusa fixa no lugar do resultado.
- Uma ferramenta, `hermes_atlas_evolution_lessons`, só com `agent_tools` ligado (desligado por padrão).

**Trabalho em segundo plano.**

- Nos processos do Hermes que ficam no ar (o gateway, o dashboard, o backend da TUI ou do Desktop, o `hermes serve`, um servidor ACP, uma CLI interativa, um host de plugins): uma thread de relógio que acorda a cada 10 minutos, e no máximo uma rodada de aprendizado por intervalo (`autopilot_minutes`, 60 no padrão) para cada perfil.
- Uma vez por dia para cada perfil em uso, o perfil que cuida de todos inicia um processo filho curto do próprio Python do Hermes, que roda as checagens do `hermes doctor` sem rede (as sondas dos provedores, o `npm audit`, o Skills Hub e o backend do terminal ficam de fora). Como o `hermes doctor`, ele carrega os plugins desse perfil, então o código deles roda nesse processo. Ele é parado aos 120 segundos.

**Dados guardados.**

- `<perfil>/evolution/`: um banco SQLite (contadores, classes de falha, lições, propostas, decisões e os trechos de erro sem dados sensíveis que esperam o refletor), o `settings.json`, a lixeira (esvaziada depois de 30 dias) e as cópias dos arquivos de memória de que ele tirou uma entrada. O banco do perfil que cuida de todos também guarda o último resultado das checagens do doctor do Hermes por perfil, como o Hermes escreveu (pode citar arquivos e pastas).
- `<raiz>/evolution/`: o estado da frota (os perfis em que o plugin já foi visto, as mudanças automáticas que você desfez) e o registro dos reparos do gateway.
- O resumo semanal, se você o agendar: um script em `<perfil>/scripts/` e uma tarefa agendada (cron) do Hermes.

### Como funciona

- **Observa**: os ganchos contam o que acontece (resultado, duração, classe da falha), nunca texto de mensagem nem argumentos de ferramentas.
- **Aprende**: uma rodada em segundo plano transforma falhas repetidas em lições, mede o efeito delas, lê todos os perfis e roda as checagens de saúde do próprio Hermes.
- **Explica**: o índice diz o que o moveu, uma falha que disparou hoje e o que fazer primeiro, com poucas consultas aos números que o plugin já guarda, sem chamar modelo.
- **Age**: no modo Assistido, só tira uma entrada da memória repetida palavra por palavra. No Modo Auto, aplica o que é seguro e reversível, conferindo de novo o disco e os serviços logo antes de agir. O resto vira proposta com o seu desfazer.
- **Mostra**: o `status`, o resumo semanal, o menu do terminal e a aba **Evolution** do dashboard, com o Índice de evolução no topo.

### Começo rápido

Requer **Hermes Agent 0.23.1 ou superior**. O Hermes carrega plugins por perfil: para um perfil com nome, acrescente `-p <perfil>` a cada comando (`hermes -p trabalho plugins install hermes-atlas-evolution`).

```bash
# Pelo catálogo de plugins do Hermes (a versão revisada), depois abra o menu
hermes plugins install hermes-atlas-evolution
hermes plugins enable hermes-atlas-evolution
hermes evolution
```

```bash
# Ou pelo GitHub (também se o Hermes disser que o nome não está no catálogo)
hermes plugins install sesantanajr/hermes-atlas-evolution --enable
```

```bash
# Todos os perfis de uma vez, instalando onde ainda não há
git clone https://github.com/sesantanajr/hermes-atlas-evolution.git
sh hermes-atlas-evolution/install.sh --all --install-missing             # pelo GitHub
sh hermes-atlas-evolution/install.sh --all --install-missing --catalog   # pelo catálogo de plugins do Hermes
```

O `install.sh` só roda comandos do próprio Hermes: `plugins update hermes-atlas-evolution` onde o plugin está, e `plugins install` onde ele falta. Uma cópia fixada num commit fica como está, com o comando que a move. Sem `--all`, ele lista os perfis e pergunta quais; acrescente `--yes` para pular a pergunta.

Instalar carrega o plugin num gateway em execução, sem reiniciar. A primeira rodada repassa até 14 dias do histórico do perfil, então não começa do zero. Próximos passos: [Primeiros passos](docs/pt-BR/getting-started.md). Já usa uma versão anterior? Veja [Atualizar para a 0.13.0](docs/pt-BR/upgrading.md#atualizar-para-a-0130).

### Modos

| Modo | Sozinho | Espera você |
|---|---|---|
| **Assistido** (padrão) | Só tira uma entrada repetida palavra por palavra entre a memória (`MEMORY.md`) e o perfil do usuário (`USER.md`), depois de guardar uma cópia do arquivo. | Todo o resto. |
| **Auto** (você liga) | Também, em todos os perfis deste Hermes: trava skills que o curador reescreve sem parar; move para a lixeira pastas `.install-*` de instalações que falharam (guardadas por 30 dias); repara o serviço do gateway quando nenhum gateway está vivo; libera versões antigas de ferramentas (`hermes pm gc`, no máximo uma vez por semana); instala o plugin onde um perfil em uso nunca o teve, sozinho só pelo catálogo de plugins do Hermes, pelo nome (quando o perfil que cuida de todos veio do catálogo; pelo repositório, só com a sua tecla). Tudo com desfazer, menos liberar versões antigas de ferramentas, que não tem volta. | O que poderia quebrar algo: desligar um plugin, remover ou atualizar plugins, arquivar skills. |
| **Manual** | Nada. Ele mede e propõe. | Tudo. |

Cada ação vai para o histórico com o seu desfazer, e uma ação que você desfaz nunca se repete. Ligue o Modo Auto no menu (tela 5), no dashboard ou com `hermes -p <perfil> config set plugins.entries.hermes-atlas-evolution.settings.mode auto`. Se o `config.yaml` não puder ser lido (um Python sem PyYAML, por exemplo), o plugin age como Manual e diz isso primeiro no `status`. Para que o perfil que cuida de todos não mexa num perfil, desligue `fleet_managed` nesse perfil.

### Veja como é

**A aba Evolution do dashboard do Hermes** (e do Hermes Desktop): decida ali, por qualquer perfil, com login no dashboard. Sem login, ela só mostra, e esconde o texto das lições escritas por você, pelo refletor ou pelo agente.

![Visão geral: lições ativas, quantas propostas esperam você, a última rodada e os comandos para levar lições a outro perfil](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/panel-overview-pt.png)

**O menu do terminal**, `hermes evolution`, no seu próprio terminal: o que pede atenção, sugestões com letras que agem na hora, e 14 telas.

![O menu do terminal: resumo, o que pede atenção, sugestões de a até f e as telas numeradas](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/terminal-menu-pt.png)

### Mais telas

- [Propostas](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/panel-proposals-pt.png): em palavras simples, cada uma com o que foi visto, o que fazer, como desfazer e o botão Aplicar agora.
- [Saúde](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/panel-health-pt.png): o gateway, disco, memória, swap, o banco de sessões, versões antigas de ferramentas e atualizações do sistema.
- [Ambiente](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/panel-environment-pt.png): o doctor do Hermes de cada perfil, os plugins e skills dele, e o que fazer.
- [A tela Ambiente do menu do terminal](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/terminal-environment-pt.png), por risco.
- [Uma constatação aberta](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/terminal-finding-pt.png): qual plugin vence, o que fazer, o comando e o desfazer.
- [A tela Sistema operacional](https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/e2dc5a6162b3f87d15b4f042421b4898fc0dd29f/screenshots/terminal-os-pt.png): atualizações esperando, reinício, atualizações automáticas de segurança, dpkg e apt.

### Comandos

`hermes -p <perfil> evolution <comando>`; `--lang en|pt` funciona com todos, e `--format md|json` com todos, menos `menu`.

| Comando | O que faz |
|---|---|
| *(nenhum)*, `menu` | O menu interativo (no shell de um agente ou num pipe, imprime o `status`). |
| `status` | O que pede atenção, o Índice de evolução e o que o moveu, o que fazer primeiro, quanto o plugin custa, a saúde do Hermes e do sistema, todos os perfis. |
| `report [--days N]` | O resumo dos últimos N dias (7 por padrão), contra o período anterior, com o que fazer primeiro. |
| `learn` | Uma rodada de aprendizado agora. |
| `lessons list \| review \| approve <id> \| retire <id> \| add \| export \| import` | As lições, as suas decisões, uma lição escrita por você, e levá-las para outro perfil. |
| `proposals list \| approve <id> \| reject <id>` | As propostas; aprovar uma que o plugin sabe aplicar roda o comando do próprio Hermes. |
| `decisions list \| undo <id>` | Cada decisão e ação automática, e o desfazer de uma. |
| `care [check \| doctor \| pm-doctor \| fix \| gc \| fix-gateway \| undo-gateway]` | Saúde e reparos do Hermes, inclusive um serviço do gateway que ainda chama um Python removido. |
| `environment [show \| scan \| catalog]` | Todos os perfis: plugins, skills, conflitos e o que fazer. |
| `update [--all \| --profiles a,b] [--install-missing] [--list] [--yes]` | Roda o `plugins update hermes-atlas-evolution` do próprio Hermes em cada perfil que você escolher, este primeiro. Com `--install-missing`, um perfil sem o plugin recebe o código que este perfil roda (o nome no catálogo, ou este repositório no commit deste perfil). Uma cópia que o Hermes fixou com `--ref` só passa para o commit deste perfil, e só quando as duas vêm deste repositório. Ele nunca escolhe um commit. |
| `system [check \| plan \| upgrade \| repair \| report]` | A atualização do sistema operacional (Linux com apt). |
| `schedule-digest [--deliver <canal>]` | O resumo semanal como tarefa agendada do Hermes, sem agente e sem tokens. |
| `usage` | As chamadas de modelos e ferramentas medidas e o seu custo estimado. |
| `owner list \| add \| remove \| add-platform \| remove-platform` | Quais remetentes e canais de chat contam como você. |
| `skills` | Skills reescritas sem parar, que vão e voltam, ou reescritas e nunca usadas. |
| `experiments` | As mudanças acompanhadas, com o veredito de antes e depois. |

Referência completa: [Comandos](docs/pt-BR/evolution-loop.md#comandos).

### Configurações

Na aba **Evolution** do dashboard, no menu (tela 6), no Hermes Desktop (Configurações → Plugins) ou no `config.yaml` em `plugins.entries.hermes-atlas-evolution.settings`. As mudanças valem em até um minuto; `runtime_guard`, `tool_hints` e ligar `agent_tools` ou o `reflector` valem depois de `hermes gateway restart`.

| Opção | Padrão | O que faz |
|---|---|---|
| `mode` | `assist` | O que o plugin faz sozinho: `assist`, `auto` ou `manual` (veja Modos, acima). |
| `lessons` | `auto` | `auto`: lições de padrão fixo valem quando os seus próprios turnos encontram a falha; texto escrito por um modelo espera você. `suggest`: toda lição espera. `off`: só mede. |
| `owner_sender_ids`, `owner_platforms` | `[]` | Quem é você nos chats: os seus ids de remetente e os canais em que todo chat é seu. Nas suas conversas diretas, você recebe todas as lições desde a primeira mensagem. |
| `fleet_managed` | `true` | Desligado neste perfil: o perfil que cuida de todos não instala nada nem move nada para a lixeira aqui sozinho. |
| `language` | `auto` | `en` ou `pt`; `auto` segue o perfil. |
| `reflector`, `reflector_daily_limit` | `true`, `6` | Rascunha lições com um modelo, pelo Hermes, para falhas sem padrão fixo; os rascunhos esperam você. |
| `autopilot_minutes` | `60` | A rodada de aprendizado em segundo plano (15 a 1440; `0` = só à mão). |
| `runtime_guard` | `true` | Barra comandos do agente que derrubariam o Hermes. |
| `agent_tools`, `tool_hints`, `feedback_signals`, `slow_turn_seconds` | | Veja [Configurações e idioma](docs/pt-BR/evolution-loop.md#configuração-e-idioma). |

O que amplia o que o plugin faz ou em quem ele confia só vale pela configuração do Hermes, nunca pelo `settings.json` do próprio plugin, que as ferramentas de arquivo do agente podem gravar: quem é você nos chats, as ferramentas do agente, desligar a Proteção do Hermes, um modo diferente de Manual, juntar lições aos resultados das ferramentas (`tool_hints`) e um limite do refletor acima de 6 por dia. O `settings.json` só pode restringir.

### Compatibilidade

| | |
|---|---|
| **Hermes Agent** | 0.23.1 ou superior, testado na 0.23.1 e na 0.23.2; uma cópia do plugin por perfil; funciona com `plugins.isolation: host` (a Proteção do Hermes fica desligada nesse caso). A varredura de instalação do próprio Hermes diz `safe` nas duas (`plugin-guard-v12` da 0.23.1 e `plugin-guard-v15` da 0.23.2). |
| **Python** | 3.11 a 3.14, só a biblioteca padrão. A suíte de testes roda no CI: Linux 3.11–3.14, macOS e Windows 3.13. |
| **Sistemas** | Linux: tudo, testado dentro de um gateway. macOS e Windows: tudo, menos a atualização do sistema operacional (apt) e os reparos do systemd; não testado dentro de um gateway nesses sistemas. |
| **Dashboard** | A aba **Evolution** do dashboard do Hermes e do Hermes Desktop. |
| **Idiomas** | Português do Brasil e inglês, de ponta a ponta. |

### Privacidade e segurança

- **Guarda** contadores, classes de falha, ids de sessão com hash, lições, propostas e, para o refletor, trechos de erro sem dados sensíveis em `<HERMES_HOME>/evolution/`; nenhum texto de conversa, prompt ou argumento de ferramenta.
- **Lê**, só para ler e com limite de tamanho, o histórico, os logs, as skills, os plugins (o código analisado, nunca importado) e os arquivos de saúde do Hermes de cada perfil ([lista completa](docs/pt-BR/hermes-integration.md#o-que-o-plugin-lê)). O processo diário do doctor carrega os plugins de cada perfil, como o `hermes doctor` faz.
- **Rede**: nenhuma própria, nenhuma telemetria e nenhuma atualização sozinho. Só o refletor chama um modelo, pelo Hermes.
- **Só você decide**, no seu próprio terminal ou num dashboard com login; o agente nunca aprova nem amplia nada. Cada mudança tem o seu desfazer ou a sua cópia.

Detalhes: [SECURITY.md](SECURITY.md).

### Documentação

[Início da documentação](docs/pt-BR/README.md): [Primeiros passos](docs/pt-BR/getting-started.md) · [O ciclo de evolução](docs/pt-BR/evolution-loop.md) · [Manutenção](docs/pt-BR/maintenance.md) · [Integração com o Hermes](docs/pt-BR/hermes-integration.md) · [Atualização](docs/pt-BR/upgrading.md) · [Solução de problemas](docs/pt-BR/troubleshooting.md) · [Mudanças](CHANGELOG.pt-BR.md)

### Novidades da 0.13.0

Pronto para o catálogo de plugins do Hermes.

- **Sem atualização sozinho, sem rede própria.** O plugin nunca acessa a rede e nunca troca o próprio código. As atualizações vêm só do Hermes (`hermes -p <perfil> plugins update hermes-atlas-evolution`, "Atualizar todos" ou `plugins.auto_apply`). O `evolution update` agora roda esse comando do Hermes nos perfis que você escolher, e nunca escolhe um commit.
- **Assistido por padrão.** Uma instalação nova, e um perfil sem `mode` definido, começam no Assistido: sozinho, ele só tira uma entrada da memória repetida palavra por palavra. O Modo Auto é escolha sua.
- **Instalação pelo nome no catálogo.** O Modo Auto só se instala sozinho em outro perfil pelo catálogo de plugins do Hermes, pelo nome, e não sincroniza mais as cópias entre os perfis. O `install.sh --catalog` também instala pelo nome no catálogo.
- **As aprovações do Hermes em todo processo filho.** Os comandos que o plugin inicia não herdam mais as variáveis de YOLO ou de aprovação automática do Hermes.
- **Transparência.** Tudo o que o plugin lê, grava, roda e guarda está listado acima.
- **Correções de estabilidade** na Proteção do Hermes, no banco de dados, nos comandos que ele roda e na rodada de aprendizado.

Cada mudança: [Mudanças](CHANGELOG.pt-BR.md).

### Contribuir

Issues e pull requests são bem-vindos; leia antes o [CONTRIBUTING.md](CONTRIBUTING.md). A suíte de testes fica no ramo `tests`. Problemas de segurança vão pelo [SECURITY.md](SECURITY.md).

### Licença

[MIT](LICENSE): livre e de código aberto, inclusive para uso comercial, mantendo o aviso de copyright.

### Autor

Criado por **Sérgio Sant'Ana Júnior** · [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/)
