# Atlas Evolution and the Hermes interfaces

**English** | [Português (Brasil)](pt-BR/hermes-integration.md)

The integration uses the Hermes Agent 0.23.1 tree as its reference (commit `3b421578588243f920886e6fe79a6faa87ea026e`). This document lists what the plugin registers in Hermes, what it reads from a profile, which setting unlocks each capability, what it does not do and where further validation is most useful. [Official release](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [Public plugin API](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/plugins/public-api.md)

The plugin relies on the published plugin API: it uses the `ctx` methods below and imports only the Hermes modules `hermes_constants`, `hermes_cli.config`, `agent.redact` (`redact_sensitive_text`) and `agent.secret_scope`, all listed in Hermes' public plugin API. Every optional `ctx` method is looked up first, so a host that lacks one still loads the plugin without that feature. Five readings look at modules Hermes has already loaded in the process, without importing them, and none of them is part of the published plugin API: the runtime guard takes the location of the `hermes_cli` package to know where the Hermes source is; the health radar reads the plugin list of Hermes' plugin manager (`hermes_cli.plugins`); the `hermes evolution` command reads the version of the loaded `hermes_cli` package for its update check; the turn hooks ask `agent.session_source` for the real origin of the running session (a Kanban worker runs with platform `cli`); and `status` asks `hermes_cli.plugin_isolation` whether plugins run in a plugin host. When a module is not loaded, that reading is skipped and the plugin falls back to what the hook payload says.

## What the plugin registers

| Surface | Plugin API | Registered when |
|---|---|---|
| 17 hooks, listed below | `ctx.register_hook` | always, except `transform_tool_result`, which is registered while lessons are on and stays silent unless `tool_hints` is on |
| Prompt section `hermes-atlas-evolution.playbook`, up to 1,400 characters | `ctx.register_system_prompt_section` | `lessons` is `suggest` or `auto` |
| Tools `hermes_atlas_evolution_plan` and `hermes_atlas_evolution_lessons`, toolset `hermes-atlas-evolution` | `ctx.register_tool` with a `check_fn` | always; both stay unavailable to the agent until `agent_tools` is on |
| Skill `hermes-atlas-evolution:evolution-review` | `ctx.register_skill` | when the host offers the method |
| Variable `ATLAS_EVOLUTION_AGENT=1` in every subprocess of an agent session | `ctx.register_session_env` | when the host offers the method |
| Auxiliary task `evolution_reflector`, inheriting from `compression` | `ctx.register_auxiliary_task`, then `ctx.llm.complete_structured` | `reflector` is on when the plugin loads and the host offers both |
| Settings form with 11 options | `config_schema` in `plugin.yaml` | always; Hermes stores the values under `plugins.entries.hermes-atlas-evolution.settings` |
| CLI command `hermes evolution` | `ctx.register_cli_command` | in-process plugins only; Hermes skips it under `plugins.isolation: host` |
| Slash command `/evolution` for the 0.1 notes | `ctx.register_command` | `legacy_memory_commands` is `true` |
| Unload callback that writes queued counters and closes the database | `ctx.on_unload` | when the host offers the method |
| Background thread `hermes-atlas-evolution-autopilot`, a daemon that runs the pass | none; started by the hooks `post_llm_call`, `on_session_end` and `on_kanban_dispatch_tick`, and by the clock | `autopilot_minutes` is not 0 |
| Background thread `hermes-atlas-evolution-clock`, a daemon that nudges the pass 2 minutes after the plugin loads and then every 10 minutes | none; stopped through `ctx.on_unload` | `autopilot_minutes` is not 0 and the host offers `on_unload` |

The hooks, the prompt section, the runtime guard, the autopilot interval and the reflector's task are set when the plugin loads, so restart the gateway after changing those settings. Each autopilot pass and each command read the settings again.

**Two copies in one profile.** When the profile still has the copy installed under the id used up to 0.5.0, `hermes-evolution`, and it is enabled, the plugin registers nothing at all and logs a warning with the command that removes the old copy (`hermes plugins remove hermes-evolution`): both copies would observe the same turns and write to the same database.

**The version a process loaded.** A gateway or dashboard process that loads the plugin records the plugin version, its kind and when it started in the plugin's database, keyed by its process ID, so that `status` can flag one that still runs older code ([Status and owner notice](evolution-loop.md#status-and-owner-notice)).

## Hooks

The manifest declares the same 17 hooks, and a test compares the two lists.

| Hook | What the plugin does with it |
|---|---|
| `pre_tool_call` | Keeps, in memory only, a hash of the command of each terminal-like call, to tell later whether another plugin rewrote it. With `runtime_guard` on, it runs the [runtime guard](#the-runtime-guard): vetoes agent terminal commands that would stop the gateway, delete what the running Hermes needs, or change this plugin's settings or switch it off. Not registered at all where Hermes runs plugins in a plugin host (see the guard). |
| `post_api_request` | Counts the call, its tokens and its duration per provider and model. |
| `api_request_error` | Counts every failed attempt, classified as rate limit, authentication, timeout, upstream or error. |
| `post_auxiliary_call` | Counts auxiliary calls and failures per task. |
| `post_tool_call` | Counts the tool outcome and its duration, classifies a failure into a signature (as `rewritten_command` when Hermes' `middleware_trace` names a change or the command that ran differs from the one `pre_tool_call` saw), counts a refused approval per session origin, counts the failure again when the turn served the owner (see [Who is served](evolution-loop.md#who-is-served)), times turns that use web research tools and, with the reflector on, keeps a scrubbed excerpt of an untemplated failure of the owner's own turns. |
| `transform_tool_result` | Adds the matching lesson to a failing result when `tool_hints` is `true`, which is off by default: Hermes keeps only the first plugin's change to a tool result. In a session that does not serve the owner, only template lessons are added. Registered while lessons are on, and silent otherwise. |
| `pre_llm_call` | Works out the turn's surface and whether it serves the owner, starts the turn timer, classifies owner feedback in memory and attributes an owner correction to the skills of the previous turn. Returns context only for the opt-in 0.1 note recall. |
| `post_llm_call` | Closes the timing of a research turn and nudges the autopilot. |
| `on_session_end` | Records the outcome and duration of the turn and the outcome of each skill it loaded, and nudges the autopilot. |
| `on_skill_lifecycle` | Counts skill loads, patches and other lifecycle events, and attaches a loaded skill to its turn. |
| `subagent_start`, `subagent_stop` | Count delegations, their outcomes and their duration. |
| `post_compaction` | Records tokens before and after a compaction when Hermes reports them. |
| `post_curator_run` | Counts Curator runs, dry or real. |
| `on_kanban_worker_exited` | Counts worker exits by kind. |
| `on_session_finalize` | Returns at most one short notice per UTC day about decisions waiting, only for a session of the owner's local surfaces (CLI, TUI, Desktop) that is not a subagent. Hermes 0.23.1 drops this text for the local sessions it hosts when their viewer closes or the session is reaped (`cli_close`, `tui_close`, `new_session`, the reaps), so for those reasons the plugin returns nothing and the day's notice is not spent; the waiting decisions show in `status` and in the digest. |
| `on_kanban_dispatch_tick` | Nudges the autopilot. Hermes fires it once per Kanban dispatcher tick, every 60 seconds by default, for the gateway's own profile, and only where the dispatcher runs inside the gateway (`kanban.dispatch_in_gateway`). The clock thread covers the other profiles a gateway serves. |

A nudge only compares two clocks; the pass itself runs in the autopilot thread (see [The autopilot](evolution-loop.md#the-autopilot)). Every callback fails open: a hook never raises into a turn, and a failure is logged at most once per event type. Hook writes go through one database connection per process and profile. While the database is busy an update waits in a queue of up to 512 entries and is written with the next event or when the plugin unloads. Overflow is counted and reported as `dropped_events`. The prompt section only reads the database and counts its display through the same queue.

On the development machine a hook event took about 0.1 ms with this design, against about 6 ms before it, when the previous design also lost 155 of 800 events with 8 concurrent threads. These are development measurements on one machine, not a guarantee.

## The runtime guard

With `runtime_guard` on (the default), the plugin's `pre_tool_call` callback runs the guard (`guard`). Hermes calls it before every tool call of the agent, and a block directive replaces the call: its message becomes the tool result the agent reads. The guard looks only at the `command` argument of terminal-like tools (`terminal`, `shell`, `bash`, `run_command`, `execute_command`) and vetoes two kinds of command.

| Kind | Vetoed | What the agent is told |
|---|---|---|
| `gateway_stop` | Linux: `systemctl` with `restart`, `stop`, `kill`, `try-restart`, `reload-or-restart`, `try-reload-or-restart`, `condrestart`, `force-reload`, or `disable --now`, on a `hermes-gateway` unit, including a profile's `hermes-gateway-<profile>`, a custom home's `hermes-gateway-<hash>`, an instance and the `.service` suffix; `service` with `restart` or `stop` on that unit. macOS: `launchctl` `kickstart -k`, `stop`, `bootout`, `unload`, `remove` or `kill` on the LaunchAgent `ai.hermes.gateway` or `ai.hermes.gateway-<suffix>`, named bare, as `gui/<uid>/<label>` or by its plist. Windows: `sc` or `net` `stop`, PowerShell `Stop-Service`, `Restart-Service`, `Suspend-Service` and `Stop-ScheduledTask`, and `schtasks /End`, on the service or task `Hermes_Gateway` or `Hermes_Gateway_<suffix>`. Anywhere: `pkill`, `killall`, `taskkill` or `Stop-Process` with an argument that contains hermes; `hermes gateway stop` and `hermes gateway restart` | the command would cut off the turns in progress, this one included; do not retry it in another form; tell the owner that a restart is needed and why; the owner runs `hermes gateway restart` from their own terminal, which lists the turns still running and asks before interrupting them |
| `runtime_removal` | `rm`, `rmdir`, `unlink`, `shred`, `mv` (its sources), and on Windows `del`, `erase`, `rd`, `Remove-Item` and `Move-Item`, or `find` with `-delete` or with `-exec` running `rm`, aimed at a protected path, at a folder that contains one, or with a glob that matches one | the command would delete or move that path, and Hermes would stop working until someone repairs it by hand; removing it is for the owner, from their own terminal, with Hermes stopped |
| `plugin_settings` | `hermes config set` or `unset` of a key under `plugins.entries.hermes-atlas-evolution` (or the id used up to 0.5.0), of `plugins.entries` or of the whole `plugins` section, and `hermes plugins disable`, `remove`, `rm` or `uninstall` of `hermes-atlas-evolution`, with any global flags such as `-p <profile>` | the command would change who counts as the owner or the plugin's protections, or switch the plugin off; only the owner decides that, from their own terminal; do not retry it in another form; tell the owner what you wanted to change and why |

The protected paths are those the running Hermes needs: the prefixes of the running Python interpreter and its environment, the base interpreter a virtual environment points at (the `home` line of its `pyvenv.cfg` and `sys._base_executable`), the Hermes source (the folder that holds the `hermes_cli` package), the Hermes root and this profile. Each is protected both as written and as resolved, so a link counts under either name: uv's `cpython-3.11-...` link to `cpython-3.11.16-...`, or a `~/.hermes` that points to another disk. A path with fewer than three components, such as `/usr`, is never added to the list, so a system interpreter whose prefix is `/usr` is not covered. Files inside a protected path are not covered either; only the path itself and the folders above it are.

How a command is read:

- It is split at `;`, `&&`, `||`, `|`, `&`, parentheses and line breaks, and in each part only the command word counts, after variable assignments and wrappers such as `sudo`, `env`, `nohup`, `timeout`, `nice`, `ionice`, `stdbuf`, `exec`, `command`, `builtin`, `time`, `chrt`, `doas`, `setsid`, `systemd-run` with its options, `runuser`, `watch`, `flock`, `xargs` and `rtk run` (or `rtk err` and the other `rtk` subcommands that run a command). A command named inside another command's arguments, such as in `echo`, is not a command.
- A command given as one string is read one level deep: to a shell (`bash`, `sh`, `zsh`, `dash`, `ksh`) with `-c` or a flag group that contains it, such as `-lc` or `-ec`, to `su`, `runuser`, `script` or `flock` with `-c` or `--command` (`script -qc` too), to PowerShell (`powershell`, `pwsh`) with `-Command` or `-c`, and to `cmd` with `/c`. A command run through another program, such as `ssh`, or from a script file, is not looked into.
- Here-document bodies are data written somewhere, not commands run now, so they are ignored.
- `~`, `$HOME` and `$HERMES_HOME` (this profile) are expanded at the start of a path, and so are `%USERPROFILE%`, `%LOCALAPPDATA%`, `%APPDATA%` and their `$NAME` and `$env:NAME` forms when they are set. On Windows, `C:\x`, `C:/x` and Git Bash's `/c/x` are the same path, compared without case. A path relative to the working folder, or one that starts with another variable, is not checked.
- Any doubt lets the command through: a quote without its pair, a command over 20,000 characters, a tool that is not terminal-like.

The check is a pure function over the command: it reads and writes nothing, and its input is bounded. The callback never raises, because Hermes treats a `pre_tool_call` that fails or times out as a veto. Each veto is counted by kind in `guard_daily` (the command is not stored), and `hermes evolution status` and the digest show the counts. The message follows the plugin's language. The operator's own terminal is not affected.

This guard exists because, on a real server, the agent restarted its own gateway with `systemctl` 13 times in a week, 12 of them from one chat on one day, cutting the turns in progress, and once deleted the Python runtime the gateway unit used, which kept Hermes down for about an hour. The same history also had a restart scheduled through `systemd-run`, which is why the guard looks at the command behind the wrappers above. Since 0.6.0 the agent's `hermes gateway restart` is vetoed too: without a terminal, Hermes 0.23.1 refuses to restart while sessions are running, and its `--yes` would interrupt them, so the restart is left to the owner. The guard is a safety net for these cases, not a sandbox: Hermes' own approval policy for terminal commands still applies.

**Plugin host.** Where Hermes runs plugins in a separate host process (`plugins.isolation: host`), the plugin registers no `pre_tool_call` at all, so neither the guard nor the rewritten-command check runs there. Every `pre_tool_call` is then a call into the host, and Hermes blocks a tool whose `pre_tool_call` fails: a host that crashed would block every tool of the profile. `status` says the guard is off for that reason.

## The reflector and Hermes' model access

The reflector is the only part of the plugin that involves a model, and it is on by default. When `reflector` is on at load time, the plugin registers the auxiliary task `evolution_reflector` with `inherit_from="compression"`: it starts from the effective configuration of the compression task, and you can give it its own provider and model in `auxiliary.evolution_reflector` of the profile configuration. A pass run inside Hermes (by the autopilot, `hermes evolution learn` or `ops tick`) then calls `ctx.llm.complete_structured` with that task, a JSON schema, at most 300 output tokens and a 60-second timeout. Hermes resolves the provider, sends the request and accounts for it; the plugin opens no connection, and no transaction of the plugin stays open during the call. When the host offers no plugin model access, or while `lessons` is `off`, the loop runs without drafts. Three real calls through Hermes in production (provider DeepSeek, about 600 tokens and about 2 seconds each) produced three drafts in Portuguese. What is sent and what is accepted back is described in [The evolution loop](evolution-loop.md#the-reflector).

## What the plugin reads

All of it is read-only and bounded in size and time.

| Source | Used for |
|---|---|
| `skills/.usage.json`, `skills/.curator_ledger.jsonl` | Skill health and tracked skill changes. Metadata and hashes only; the evolution loop never opens a skill body. |
| `SKILL.md` files under the skill folders, and the first 8 KiB of other plugins' `__init__.py` | Only for the operations supervisor (`ops inspect`, `ops tick`): skill files are read, within a total byte budget, to find duplicate skills, and only their hash and title are kept; a plugin's `__init__.py` start tells whether it is a provider plugin. Nothing of either is stored as text. |
| `logs/agent.log`, and `agent.log.1` to `agent.log.5` on the first run | The completion line of each background review. The tails of the Hermes logs are also scanned for error categories. Links are ignored. |
| `state.db` | Integrity check and Hermes' own usage accounting. The backfill: sessions and their messages from the 14 days before the plugin started observing, 25 sessions at a time, classified in memory and discarded. The radar: how the sessions of the last 7 days ended, and prompt tokens per agent call from `session_model_usage`. |
| `pyvenv.cfg` of the running Python environment | The runtime guard: the base interpreter the environment points at. |
| The other profiles of the same Hermes root | For `status`, up to 200 profiles: the number of sessions in the last 7 days from the `sessions` table of each `state.db`, whether its `plugins/` folder holds `hermes-atlas-evolution`, the old `hermes-evolution` or both (the `name` line of a `plugin.yaml` there tells a clone of this version kept under the old folder name), and, where the plugin has data, the end of its last pass and its lesson counts from `evolution/evolution.sqlite3`. Nothing else is read, and nothing is written there. |
| `/proc/<pid>/stat` and `/proc/<pid>/cmdline` of the command's ancestors, `/proc/self/cgroup` | On Linux, only when a decision command (`lessons approve`, `retire`, `add`, `review`, `proposals approve`, `reject`) runs: whether a Hermes process is an ancestor or the command runs in a Hermes gateway's cgroup. Nothing is stored. |
| Hermes' plugin manager, when it is loaded in the process | For the radar: the enabled plugins, the number of hooks each registered and the name of a load error. |
| `plugin.yaml` of the plugins bundled with Hermes and of `<HERMES_HOME>/plugins/` | For the radar: the hooks each plugin declares (`provides_hooks`). At most 500 folders and 64 KiB per manifest. |
| `cron/jobs.json`, Kanban boards | Workload diagnostics, as counts; `status` also checks whether the weekly digest job exists. |
| The `sessions` table of this profile's `state.db`: source and sender of each chat session | For `status`, the sessions and number of senders of each chat channel in 7 days (counts only). For `hermes evolution owner`, run by the operator in their own terminal, the sender ids of each channel in 30 days with their session counts. Nothing is stored. |
| `gateway_state.json`, `gateway.pid` | Gateway state, including a named profile served by the root gateway. |
| `memories/MEMORY.md`, `memories/USER.md` | Presence, size, UTF-8 readability and structure counts such as entries, duplicates and oversized entries. The content is not stored or reported. |
| Hermes configuration | The plugin's own entry under `plugins.entries.hermes-atlas-evolution.settings`, `display.language`, an allowlist of settings for change tracking, and Cron, Kanban, delegation and memory limits. |
| `HERMES_LANGUAGE` | The profile language, read through `agent.secret_scope` inside Hermes or from the environment outside it. |
| `/proc`, `/sys/fs/cgroup`, systemd unit properties | Linux pressure and service measurements. |

## How it fits the Hermes features

| Area | Hermes surface | Use by Atlas Evolution |
|---|---|---|
| Self-improvement | Background review, skills and the Curator ledger | Measures what the native loop leaves open: whether a rewrite was followed by better or worse turns for that skill, which skills are rewritten in circles and what the reviews cost. Regressions are proposed as the Curator's own rollback. [Curator](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator) |
| Tool failures | `post_tool_call`, `transform_tool_result`, system prompt sections | Turns recurring failures into lessons and delivers them without touching the cached prompt. [Observer hooks](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) |
| Approvals | Hermes' approval gate | A refused approval is recorded as `approval_refused` and never becomes a lesson, so the plugin never coaches the agent around a decision the user made. |
| Model access | Auxiliary tasks and `ctx.llm` | The reflector uses its own auxiliary task, so its model and cost are visible and configurable like any other auxiliary task. |
| Settings | `config_schema` and the Desktop plugin settings | The main options are edited where the other plugins are configured. |
| Gateway lifecycle | `pre_tool_call`, `hermes gateway restart` | The runtime guard vetoes agent commands that would restart, stop or kill the gateway from inside a turn, on Linux, macOS and Windows, and tells the agent to leave the restart to the owner, whose `hermes gateway restart` lists the turns still running and asks before interrupting them. `status` flags a gateway that still runs older plugin code after an update. |
| Session history | `state.db`, `on_session_finalize` | The backfill learns from the 14 days before installation, counting each message once although Hermes' compaction keeps copies of it. The radar reports sessions that end as orphans, heavy context per call and weak compactions. A short owner notice is returned at the end of a local session, where Hermes shows it. |
| Who is served | `pre_llm_call` (platform, sender), the session origin, `owner_sender_ids` | A turn serves the owner on their own surfaces, in their automations and subagents, or in a chat with a sender listed in `owner_sender_ids`. Only those turns let a derived lesson activate by itself, feed the reflector and reach the agent tools; a chat with someone else gets template lessons only. |
| Plugins | Plugin manager, manifests and `pre_tool_call` | The radar reports plugins that fail to load, register hooks they do not declare, or compete for a transform hook where only the first result counts, and tool failures another plugin caused: calls Hermes blocked after a plugin's `pre_tool_call` hung or raised, and commands that failed after a plugin rewrote them. Those never become lessons. |
| Profiles | One gateway, many profiles | Each profile loads its own plugins. `status` shows every profile of the Hermes root and the command for each one that lacks the plugin, and the clock gives every loaded profile its passes, not only the gateway's own. |
| Research | `web_search`, `web_extract` and the grounded citations skill | Follows turns with recognized web tools, proposes a budget and defines what verifies a claim. [Web search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) · [Grounded citations](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/skills/bundled/research/research-grounded-citations.md) |
| Delegation | `delegate_task` and Kanban | Measures subagent outcomes. Proposes synchronous delegation for work tied to the turn and a blocked card for durable verification. [Kanban](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/features/kanban.md) |
| Routines | Cron, local delivery and `no_agent` runs | Detects concentrated fires, delivery failures and jobs that cannot be scheduled. Generates recipes for the supervisor tick and the weekly digest. [Script-only cron](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/guides/cron-script-only.md) |
| Memory | Native provider and compaction | Observes compaction behaviour and inventories the structure of the native memory files. It never writes to memory and never stores its content. |
| Costs | Provider hooks and Hermes usage accounting | Separates input, cache and output tokens, measured costs and estimates. Compares behaviour with the profile's own baseline. |
| Resources | PSI, cgroup v2 and systemd | Joins pressure and service behaviour with profile health. A CPU, I/O or `MemoryHigh` trial requires explicit settings, verification and restoration. [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html) |

## Permissions

| Setting | Effect |
|---|---|
| Default installation | Observers with aggregated data. The autopilot runs a pass at most once an hour, and the first passes learn from 14 days of the profile's history. Recurring failures with a template become active lessons (`lessons: auto`) once the owner's own turns hit them twice; they reach the agent in new sessions, are measured, and are retired when they do not help. Failures only other people caused wait for approval. The reflector drafts lessons that wait for approval. The runtime guard vetoes the commands described above. Operator commands can inspect the profile's signals. |
| `owner_sender_ids` | The sender ids, text or numbers, whose chat turns count as the owner's, as in a local session. |
| `owner_platforms` | The platforms where every chat counts as the owner's, from the first message: only a platform nobody else writes to, such as a private bot or the owner's own interface through the API server. |
| `lessons: suggest` | Derived lessons are candidates that wait for approval, like every other lesson. |
| `lessons: off` | No lesson is derived and no lesson surface is registered. |
| `tool_hints` (off by default) | The matching lesson is also appended to a failing tool result, which discards the change another plugin's `transform_tool_result` would make to that result. |
| `agent_tools` (only from the Hermes settings form or `config.yaml`; `agent_advice` in `settings.json` is ignored) | Exposes `hermes_atlas_evolution_plan` and `hermes_atlas_evolution_lessons`, which answer only in a session that serves the owner. The first returns steps, a budget and verification criteria. The second searches lessons, reports the queue and stores proposed lessons in quarantine. With an explicit toolset list, include `hermes-atlas-evolution`. |
| `reflector` (on by default) | Keeps scrubbed excerpts of untemplated failures of the owner's own turns and lets a pass ask an AI model, through Hermes, for quarantined drafts, within `reflector_daily_limit` calls per day. `false` switches it off. |
| `autopilot_minutes` (60 by default) | Interval of the background pass. `0` leaves passes to `learn`, `ops tick`, the weekly recipe and the first `status`. |
| `runtime_guard` (on by default) | Registers the `pre_tool_call` veto, except in a plugin host. `false` removes it, but only from the Hermes settings form or `config.yaml`: `false` in `settings.json`, which the agent's file tools can write, is ignored, and so are `owner_sender_ids` and `owner_platforms` there. |
| `inspect_shared_kanban` | Includes the boards of the shared root that Hermes reports to the process. Reports show counts. |
| `kanban_handoff.enabled` | Authorizes `ops handoff` with a board and a profile chosen in advance. The card starts blocked and waits for operator review. |
| `linux_supervisor.mode: apply` | Authorizes temporary trials on the configured auxiliary unit, with the permissions the process already has in systemd. |

## What the plugin does not do in Hermes

- It does not lock, archive, roll back, edit or create a skill. Those are printed as `hermes curator` commands for you.
- It does not change Hermes configuration. A proposal that would is printed as a `hermes config` command.
- It does not write to memory or read memory content beyond the structure counts above.
- It does not rewrite a prompt that is already in use, inject context per turn (except the opt-in 0.1 note recall), or change tool arguments. The only changes to agent-visible content are the sentence appended to a failing tool result, with `tool_hints` on, and, when the runtime guard vetoes a command, its fixed refusal message in place of the result.
- It does not stop, restart or kill anything. The runtime guard only refuses a command; it never runs one.
- It does not write to Hermes' history: `state.db` and the logs are opened read-only.
- It changes nothing in other profiles. `status` only reads their sessions and the plugin's own state there, and prints the install or migration command for you to run.
- It does not approve, deny or route around Hermes approvals, and it never turns a refused approval into advice.
- It registers no middleware, provider, memory provider, redaction pattern or locale, and overrides nothing in Hermes core.
- It creates no Cron job. The recipes are printed for you to create. The only Kanban card it creates is the blocked card of `ops handoff`, which needs `kanban_handoff`.
- It opens no network connection and sends no telemetry. A model is called only by the reflector, through Hermes.
- It does not let the agent decide: playbook decisions refuse to run in an agent's shell.

## Installation notes

- **From GitHub.** `hermes plugins install sesantanajr/hermes-atlas-evolution --enable` in the default profile, `hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable` in a named one. Installing with `--enable` makes a running gateway load the plugin at once; its prompt section applies from the next session. Check with `hermes plugins doctor --ci hermes-atlas-evolution` and `hermes evolution status`.
- **Every profile you use.** Hermes loads plugins per profile, even when one gateway serves several profiles. For every named profile: `for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins install sesantanajr/hermes-atlas-evolution --enable; done`, plus the command for the default profile. `hermes evolution status` lists every profile of the same Hermes root, with the install command for each busy one that does not have the plugin.
- **Updating.** `hermes plugins update hermes-atlas-evolution`, with `-p <profile>` for each named profile, fetches the new code, but a running gateway keeps the code it loaded: Hermes reloads a running gateway on install, enable and disable, and on an update applied from Hermes Desktop, the dashboard or `/plugins update` in a chat, not on this command-line update. Finish with `hermes gateway restart`; when something is still running, it lists it and asks `Interrupt them and restart the gateway? [y/N]`. `status` flags a gateway or dashboard process that still runs an older version, on POSIX systems.
- **Migrating from 0.5.0.** Up to 0.5.0 the plugin id was `hermes-evolution`. In each profile that has it, run `hermes plugins remove hermes-evolution` and then `hermes plugins install sesantanajr/hermes-atlas-evolution --enable`, not `hermes plugins update`: Hermes names a plugin's folder after its id, so an update would not replace the old copy. Remove first: installing makes a running gateway rediscover its plugins, which unloads the removed copy and loads the new one; if you installed first, run `hermes gateway restart` after the removal. While the old copy is installed and enabled, the new one registers nothing, not even its command, so `hermes evolution` in that profile is still 0.5.0's (see [What the plugin registers](#what-the-plugin-registers)); `status` run from a profile already on 0.6.0 marks such a profile as having both copies and prints its fix. Hermes' `plugins remove` also deletes `plugins.entries.hermes-evolution` from the profile's `config.yaml`, so rename that block to `hermes-atlas-evolution` first if you changed options in the settings form. The data in `<HERMES_HOME>/evolution/` stays. Where an explicit toolset list or `auxiliary.background_review.extra_tools` names `atlas-evolution` or `atlas_evolution_lessons`, use `hermes-atlas-evolution` and `hermes_atlas_evolution_lessons`.
- **Manually.** Copy the whole plugin folder, with `plugin.yaml` at its root, to `<HERMES_HOME>/plugins/hermes-atlas-evolution/` and run `hermes plugins enable hermes-atlas-evolution`. The default Hermes folder on Linux is `~/.hermes`. Windows installations usually use `%LOCALAPPDATA%\hermes`. Enabling makes a running gateway load it; if Hermes says no gateway answered, run `hermes gateway restart`.
- **Host isolation.** Under `plugins.isolation: host` Hermes skips `register_cli_command`, so use `python -m hermes_atlas_evolution --home <profile> ...` from the plugin folder. The runtime guard is off there (see [The runtime guard](#the-runtime-guard)). The reflector does not run in that standalone runner; waiting excerpts are kept for the next pass run by Hermes. The autopilot and the reflector were not verified under host isolation. See [Operations](operations.md#running-outside-hermes).

### Cover image and the plugin catalog

Hermes shows a plugin's cover only for entries of its curated plugin catalog, the `plugin-catalog/` folder of the hermes-agent repository, which Nous Research reviews ([catalog submission](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)). An entry's `image` must be an https image on a GitHub host, in a 2:1 proportion, pinned to the commit of the entry. Installed from GitHub, Atlas Evolution shows a generic card until its catalog entry is accepted. Its cover is [banner.jpg](banner.jpg), 1280×640.

## Compatibility and limits

Requires Hermes Agent 0.23.1 or later and Python 3.11 or later.

**Verified on the development machine** (measurements, not guarantees):

- The unit and end-to-end tests against a simulated Hermes, also under Hermes' Python with its native redactor. CI runs them on Linux with Python 3.11 to 3.13 and on real macOS and Windows runners; tests that are POSIX by design (permission bits, the liveness probe, symlinked runtimes) are skipped on Windows.
- `hermes plugins validate` of Hermes 0.23.1: no warnings (2 tools, 17 hooks), security scan safe, ready for `plugins.isolation: host`.
- The backfill, on a copy of a real profile with 276 sessions: 0.6.0 replays 373 turns and 3,566 tool calls, exactly one per logical message of the window (0.5.0 counted 6,731, every copy Hermes' compaction keeps, and 86 interrupted turns instead of 23), in under a second. Of its 152 failures, 129 came from the owner's own turns.
- The autopilot (one claim per profile, expiring lease, retry, profile pinning, a manual `learn` waiting for a running pass): verified in tests and in the CLI. The clock, the release of a pass at process exit, the stale-process check and the guard against two copies in one profile: verified in tests.
- Measured for 0.4.0 and not repeated since: about 0.1 ms per hook event (about 6 ms before, with events lost under concurrency); with a `state.db` of about 170 MB, `learn` about 0.3 s and 25 MB, `report` about 0.35 s, `ops tick` about 0.5 s.

**Verified in production:** three real reflector calls through Hermes (provider DeepSeek, about 600 tokens and about 2 seconds each) produced three drafts in Portuguese.

**Not verified:** lesson verdicts and skill outcomes over weeks of real use (there are no field results yet), the clock and the stale-process check in a long-running gateway that serves several profiles, the migration from 0.5.0 on a running gateway, the Kanban nudge of the autopilot (which needs `kanban.dispatch_in_gateway`), the autopilot and the reflector under `plugins.isolation: host`, Linux trials on a real systemd, other Hermes versions, and a Hermes gateway running on macOS or Windows (the tests run there, the gateway did not).

**Limits**

- Verdicts compare two periods; they are correlations, not controlled experiments.
- Only lessons bound to a failure (derived and reflector) get a measured effect.
- A database at schema 8 (0.5.0 and later) cannot be opened by 0.4.0 or earlier. To go back that far, restore a backup or delete `evolution.sqlite3`. 0.6.0 keeps schema 8 and only adds a table, so 0.5.0 still opens its database; from 0.6.0 on, a database at a newer schema also opens, since later versions only add tables and columns.
- The autopilot needs a nudge: a turn, the clock or the Kanban tick. A host without `on_unload` gets no clock, so a quiet profile there waits for its next turn.
- The backfill reads Hermes' history only before the plugin started observing the profile, at most 14 days back. It does not replay model calls and tokens, it times tool calls only approximately (from the request to the result), and it cannot tell a command another plugin rewrote; a binary that failed live after a rewrite is kept out of the lessons from then on.
- Under `plugins.isolation: host` there is no `hermes evolution`: use `python -m hermes_atlas_evolution --home <profile> ...` (it reads `config.yaml` only with PyYAML). The cron recipes need the Hermes CLI; a standalone pass drafts nothing and keeps waiting excerpts for a pass run inside Hermes.
- Review counts depend on a line Hermes 0.23.1 writes to `agent.log`. Owner signals are heuristics over the first words of a message.
- The runtime guard looks only at the command word of each part of a terminal command, after the wrappers it knows, and one level into a command given as one string. A command nested deeper, one run over `ssh` or from a script, a command it cannot parse and a path relative to the working folder all pass. It is a safety net, not a sandbox.
- The stale-process check compares version numbers: a process that loaded different code under the same version number is not flagged, and on Windows no process is flagged.
- The operator guard reads process ancestry and cgroups from `/proc`, so outside Linux it relies on the environment markers alone.
- Who a chat turn serves comes from `owner_sender_ids` and `owner_platforms`. With both empty, every messaging chat counts as someone else's: its failures still count, but lessons from them wait for approval and the chat gets template lessons only. A platform listed in `owner_platforms` trusts everyone who writes there.

## Advanced: let the native background review propose lessons

Hermes can admit a named tool to the background review through `auxiliary.background_review.extra_tools`. Adding `hermes_atlas_evolution_lessons` lets the review record what it learned as a lesson proposal in the playbook, next to its usual skill and memory writes.

```yaml
# config.yaml of the profile
auxiliary:
  background_review:
    extra_tools:
      - hermes_atlas_evolution_lessons
```

- This is operator configuration in Hermes. The plugin never sets it.
- The tool must be available to the parent agent, so it also requires `agent_tools` to be on.
- A lesson proposed this way has origin `agent`: it is stored in quarantine, its proposal is high risk and it reaches the agent only after you approve it. The tool accepts at most 5 proposals per hour.
- An approved proposal is shown in the prompt section like any other lesson. It is not tied to a failure signature, so it gets no individual before-and-after verdict. Its activation still counts as a concurrent change for the other verdicts.
- This combination was not exercised on a production gateway.

## Where the next validation has the most value

1. Run the loop on a production gateway for a few weeks and compare the lesson verdicts and the skills flagged by outcome with a manual review of the same turns.
2. Follow the autopilot in a long-running gateway that serves several profiles, for several days: the passes `status` shows for each profile, their duration, and the hook latency while a pass runs.
3. Migrate a few real profiles from 0.5.0 and check with `status` that each one counts its turns once and keeps its lessons.
4. Review the reflector's drafts against the failures they came from, beyond the three real calls made so far, and choose a low-cost model for `auxiliary.evolution_reflector`.
5. Read the runtime guard's vetoes and the radar's proposals against what really happened on the host, to find false positives.
6. Compare the duration and the quality of real research before and after the plans. Use the same set of questions, validate the sources and record where the budget left a material gap.
7. Cross-check worker failures, overlapping schedules and queue volume. Make one change at a time and follow latency, error rate and backlog.
8. Build a controlled set of Linux scenarios with CPU, I/O and memory pressure. Check the effective controller value, the impact on Hermes and the rollback with the agent and the auxiliary unit running.
