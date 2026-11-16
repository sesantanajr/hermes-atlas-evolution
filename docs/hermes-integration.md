# Atlas Evolution and the Hermes interfaces

**English** | [Português (Brasil)](pt-BR/hermes-integration.md)

The integration uses the Hermes Agent 0.23.1 tree as its reference (commit `3b421578588243f920886e6fe79a6faa87ea026e`). This document lists what the plugin registers in Hermes, what it reads from a profile, which setting unlocks each capability, what it does not do and where further validation is most useful. [Official release](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [Public plugin API](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/plugins/public-api.md)

The plugin relies on the published plugin API: it uses the `ctx` methods below and imports only the Hermes modules `hermes_constants`, `hermes_cli.config`, `agent.redact` (`redact_sensitive_text`) and `agent.secret_scope`, all listed in Hermes' public plugin API. Every optional `ctx` method is looked up first, so a host that lacks one still loads the plugin without that feature. Two readings look at modules Hermes has already loaded in the process, without importing them: the runtime guard takes the location of the `hermes_cli` package to know where the Hermes source is, and the health radar reads the plugin list of Hermes' plugin manager (`hermes_cli.plugins`), which is not part of the published plugin API. When that module is not loaded, the radar skips the reading.

## What the plugin registers

| Surface | Plugin API | Registered when |
|---|---|---|
| 17 hooks, listed below | `ctx.register_hook` | always, except `transform_tool_result` (lessons on and `tool_hints` on) and `pre_tool_call` (`runtime_guard` on) |
| Prompt section `atlas-evolution.playbook`, up to 1,400 characters | `ctx.register_system_prompt_section` | `lessons` is `suggest` or `auto` |
| Tools `atlas_evolution_plan` and `atlas_evolution_lessons`, toolset `atlas-evolution` | `ctx.register_tool` with a `check_fn` | always; both stay unavailable to the agent until `agent_tools` is on |
| Skill `hermes-evolution:evolution-review` | `ctx.register_skill` | when the host offers the method |
| Variable `ATLAS_EVOLUTION_AGENT=1` in every subprocess of an agent session | `ctx.register_session_env` | when the host offers the method |
| Auxiliary task `evolution_reflector`, inheriting from `compression` | `ctx.register_auxiliary_task`, then `ctx.llm.complete_structured` | `reflector` is on when the plugin loads and the host offers both |
| Settings form with 11 options | `config_schema` in `plugin.yaml` | always; Hermes stores the values under `plugins.entries.hermes-evolution.settings` |
| CLI command `hermes evolution` | `ctx.register_cli_command` | in-process plugins only; Hermes skips it under `plugins.isolation: host` |
| Slash command `/evolution` for the 0.1 notes | `ctx.register_command` | `legacy_memory_commands` is `true` |
| Unload callback that writes queued counters and closes the database | `ctx.on_unload` | when the host offers the method |
| Background thread `atlas-evolution-autopilot`, a daemon that runs the pass | none; started by the hooks `post_llm_call`, `on_session_end` and `on_kanban_dispatch_tick` | `autopilot_minutes` is not 0 |

The hooks, the prompt section, the runtime guard, the autopilot interval and the reflector's task are set when the plugin loads, so restart the gateway after changing those settings. Each autopilot pass and each command read the settings again.

## Hooks

The manifest declares the same 17 hooks, and a test compares the two lists.

| Hook | What the plugin does with it |
|---|---|
| `pre_tool_call` | The [runtime guard](#the-runtime-guard): vetoes agent terminal commands that would stop the gateway or delete what the running Hermes needs. Registered only when `runtime_guard` is on. |
| `post_api_request` | Counts the call, its tokens and its duration per provider and model. |
| `api_request_error` | Counts every failed attempt, classified as rate limit, authentication, timeout, upstream or error. |
| `post_auxiliary_call` | Counts auxiliary calls and failures per task. |
| `post_tool_call` | Counts the tool outcome, classifies a failure into a signature, times turns that use web research tools and, with the reflector on, keeps a scrubbed excerpt of an untemplated failure. |
| `transform_tool_result` | Adds the matching lesson to a failing result. Registered only when lessons are on and `tool_hints` is `true`. |
| `pre_llm_call` | Starts the turn timer, classifies owner feedback in memory and charges an owner correction to the skills of the previous turn. Returns context only for the opt-in 0.1 note recall. |
| `post_llm_call` | Closes the timing of a research turn and nudges the autopilot. |
| `on_session_end` | Records the outcome and duration of the turn and the outcome of each skill it loaded, and nudges the autopilot. |
| `on_skill_lifecycle` | Counts skill loads, patches and other lifecycle events, and attaches a loaded skill to its turn. |
| `subagent_start`, `subagent_stop` | Count delegations, their outcomes and their duration. |
| `post_compaction` | Records tokens before and after a compaction when Hermes reports them. |
| `post_curator_run` | Counts Curator runs, dry or real. |
| `on_kanban_worker_exited` | Counts worker exits by kind. |
| `on_session_finalize` | Returns at most one short notice per UTC day about decisions waiting, only for a session of the owner's local surfaces (CLI, TUI, Desktop) that is not a subagent. Hermes shows it outside the conversation. |
| `on_kanban_dispatch_tick` | Nudges the autopilot. Hermes fires it once per Kanban dispatcher tick, every 60 seconds by default, only where the dispatcher runs inside the gateway (`kanban.dispatch_in_gateway`). |

A nudge only compares two clocks; the pass itself runs in the autopilot thread (see [The autopilot](evolution-loop.md#the-autopilot)). Every callback fails open: a hook never raises into a turn, and a failure is logged at most once per event type. Hook writes go through one database connection per process and profile. While the database is busy an update waits in a queue of up to 512 entries and is written with the next event or when the plugin unloads. Overflow is counted and reported as `dropped_events`. The prompt section only reads the database and counts its display through the same queue.

On the development machine a hook event took about 0.1 ms with this design, against about 6 ms before it, when the previous design also lost 155 of 800 events with 8 concurrent threads. These are development measurements on one machine, not a guarantee.

## The runtime guard

With `runtime_guard` on (the default), the plugin registers a `pre_tool_call` callback (`guard`). Hermes calls it before every tool call of the agent, and a block directive replaces the call: its message becomes the tool result the agent reads. The guard looks only at the `command` argument of terminal-like tools (`terminal`, `shell`, `bash`, `run_command`, `execute_command`) and vetoes two kinds of command.

| Kind | Vetoed | What the agent is told |
|---|---|---|
| `gateway_stop` | `systemctl` with `restart`, `stop`, `kill`, `try-restart`, `reload-or-restart`, `try-reload-or-restart`, `condrestart`, `force-reload`, or `disable --now`, on a `hermes-gateway` unit (an instance or the `.service` suffix included); `service` with `restart` or `stop` on that unit; `pkill` or `killall` with an argument that contains hermes | the command would cut off the turns in progress, this one included; run `hermes gateway restart`, which waits for the running turns to finish; a forced restart is for the owner to run from their own terminal |
| `runtime_removal` | `rm`, `rmdir`, `unlink`, `shred`, `mv` (its sources) or `find` with `-delete` or with `-exec` running `rm`, aimed at a protected path, at a folder that contains one, or with a glob that matches one | the command would delete or move that path, and Hermes would stop working until someone repairs it by hand; removing it is for the owner, from their own terminal, with Hermes stopped |

The protected paths are those the running Hermes needs: the prefixes of the running Python interpreter and its environment, the Hermes source (the folder that holds the `hermes_cli` package), the Hermes root and this profile. A path with fewer than three components, such as `/usr`, is never added to the list, so a system interpreter whose prefix is `/usr` is not covered. Files inside a protected path are not covered either; only the path itself and the folders above it are.

How a command is read:

- It is split at `;`, `&&`, `||`, `|`, `&`, parentheses and line breaks, and in each part only the command word counts, after variable assignments and wrappers such as `sudo`, `env`, `nohup`, `timeout`, `nice`, `ionice`, `stdbuf`, `exec`, `command`, `builtin`, `time`, `chrt` and `doas`. A command named inside another command's arguments, such as in `echo`, is not a command.
- A command run through another program, such as `bash -c` or `ssh`, or from a script file, is not looked into.
- Here-document bodies are data written somewhere, not commands run now, so they are ignored.
- A home-relative path is expanded with the home folder. A path relative to the working folder is not checked.
- Any doubt lets the command through: a quote without its pair, a command over 20,000 characters, a tool that is not terminal-like.

The check is a pure function over the command: it reads and writes nothing, and its input is bounded. The callback never raises, because Hermes treats a `pre_tool_call` that fails or times out as a veto. Each veto is counted by kind in `guard_daily` (the command is not stored), and `hermes evolution status` and the digest show the counts. The message follows the plugin's language. The operator's own terminal is not affected.

This guard exists because, on a real server, the agent restarted its own gateway with `systemctl` 13 times in a week, 12 of them from one chat on one day, cutting the turns in progress, and once deleted the Python runtime the gateway unit used, which kept Hermes down for about an hour. It is a safety net for these two cases, not a sandbox: Hermes' own approval policy for terminal commands still applies.

## The reflector and Hermes' model access

The reflector is the only part of the plugin that involves a model, and it is on by default. When `reflector` is on at load time, the plugin registers the auxiliary task `evolution_reflector` with `inherit_from="compression"`: it starts from the effective configuration of the compression task, and you can give it its own provider and model in `auxiliary.evolution_reflector` of the profile configuration. A pass run inside Hermes (by the autopilot, `hermes evolution learn` or `ops tick`) then calls `ctx.llm.complete_structured` with that task, a JSON schema, at most 300 output tokens and a 60-second timeout. Hermes resolves the provider, sends the request and accounts for it; the plugin opens no connection, and no transaction of the plugin stays open during the call. When the host offers no plugin model access, or while `lessons` is `off`, the loop runs without drafts. Three real calls through Hermes in production (provider DeepSeek, about 600 tokens and about 2 seconds each) produced three drafts in Portuguese. What is sent and what is accepted back is described in [The evolution loop](evolution-loop.md#the-reflector).

## What the plugin reads

All of it is read-only and bounded in size and time.

| Source | Used for |
|---|---|
| `skills/.usage.json`, `skills/.curator_ledger.jsonl` | Skill health and tracked skill changes. Metadata and hashes only; skill bodies are never opened. |
| `logs/agent.log`, and `agent.log.1` to `agent.log.5` on the first run | The completion line of each background review. The tails of the Hermes logs are also scanned for error categories. Links are ignored. |
| `state.db` | Integrity check and Hermes' own usage accounting. The backfill: sessions and their messages from the 14 days before the plugin started observing, 25 sessions at a time, classified in memory and discarded. The radar: how the sessions of the last 7 days ended, and prompt tokens per agent call from `session_model_usage`. |
| `state.db` of the other profiles of the same Hermes root | For `status`: the number of sessions in the last 7 days of each profile that has no `plugins/hermes-evolution` folder. Nothing else is read. |
| Hermes' plugin manager, when it is loaded in the process | For the radar: the enabled plugins, the number of hooks each registered and the name of a load error. |
| `plugin.yaml` of the plugins bundled with Hermes and of `<HERMES_HOME>/plugins/` | For the radar: the hooks each plugin declares (`provides_hooks`). At most 500 folders and 64 KiB per manifest. |
| `cron/jobs.json`, Kanban boards | Workload diagnostics, as counts. |
| `gateway_state.json`, `gateway.pid` | Gateway state, including a named profile served by the root gateway. |
| `memories/MEMORY.md`, `memories/USER.md` | Presence, size, UTF-8 readability and structure counts such as entries, duplicates and oversized entries. The content is not stored or reported. |
| Hermes configuration | The plugin's own entry under `plugins.entries.hermes-evolution.settings`, `display.language`, an allowlist of settings for change tracking, and Cron, Kanban, delegation and memory limits. |
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
| Gateway lifecycle | `pre_tool_call`, `hermes gateway restart` | The runtime guard vetoes agent commands that would restart, stop or kill the gateway from inside a turn and points the agent to Hermes' own restart, which waits for running turns. |
| Session history | `state.db`, `on_session_finalize` | The backfill learns from the 14 days before installation. The radar reports sessions that end as orphans, heavy context per call and weak compactions. A short owner notice appears at the end of a local session. |
| Plugins | Plugin manager and manifests | The radar reports plugins that fail to load, register hooks they do not declare, or compete for a transform hook where only the first result counts. |
| Research | `web_search`, `web_extract` and the grounded citations skill | Follows turns with recognized web tools, proposes a budget and defines what verifies a claim. [Web search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) · [Grounded citations](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/skills/bundled/research/research-grounded-citations.md) |
| Delegation | `delegate_task` and Kanban | Measures subagent outcomes. Proposes synchronous delegation for work tied to the turn and a blocked card for durable verification. [Kanban](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/features/kanban.md) |
| Routines | Cron, local delivery and `no_agent` runs | Detects concentrated fires, delivery failures and jobs that cannot be scheduled. Generates recipes for the supervisor tick and the weekly digest. [Script-only cron](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/guides/cron-script-only.md) |
| Memory | Native provider and compaction | Observes compaction behaviour and inventories the structure of the native memory files. It never writes to memory and never stores its content. |
| Costs | Provider hooks and Hermes usage accounting | Separates input, cache and output tokens, measured costs and estimates. Compares behaviour with the profile's own baseline. |
| Resources | PSI, cgroup v2 and systemd | Joins pressure and service behaviour with profile health. A CPU, I/O or `MemoryHigh` trial requires explicit settings, verification and restoration. [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html) |

## Permissions

| Setting | Effect |
|---|---|
| Default installation | Observers with aggregated data. The autopilot runs a pass at most once an hour, and the first passes learn from 14 days of the profile's history. Recurring failures become candidate lessons that wait for approval. The reflector drafts lessons that also wait for approval. The runtime guard vetoes the commands described above. Operator commands can inspect the profile's signals. |
| `lessons: auto` | Derived lessons become active without approval and reach the agent in new sessions. Reflector drafts still wait. |
| `lessons: off` | No lesson is derived and no lesson surface is registered. |
| `agent_tools` (`agent_advice` in `settings.json`) | Exposes `atlas_evolution_plan` and `atlas_evolution_lessons`. The first returns steps, a budget and verification criteria. The second searches lessons, reports the queue and stores proposed lessons in quarantine. With an explicit toolset list, include `atlas-evolution`. |
| `reflector` (on by default) | Keeps scrubbed excerpts of untemplated failures and lets a pass ask a model, through Hermes, for quarantined drafts, within `reflector_daily_limit` calls per day. `false` switches it off. |
| `autopilot_minutes` (60 by default) | Interval of the background pass. `0` leaves passes to `learn`, `ops tick` and the weekly recipe. |
| `runtime_guard` (on by default) | Registers the `pre_tool_call` veto. `false` removes it. |
| `inspect_shared_kanban` | Includes the boards of the shared root that Hermes reports to the process. Reports show counts. |
| `kanban_handoff.enabled` | Authorizes `ops handoff` with a board and a profile chosen in advance. The card starts blocked and waits for operator review. |
| `linux_supervisor.mode: apply` | Authorizes temporary trials on the configured auxiliary unit, with the permissions the process already has in systemd. |

## What the plugin does not do in Hermes

- It does not lock, archive, roll back, edit or create a skill. Those are printed as `hermes curator` commands for you.
- It does not change Hermes configuration. A proposal that would is printed as a `hermes config` command.
- It does not write to memory or read memory content beyond the structure counts above.
- It does not rewrite a prompt that is already in use, inject context per turn (except the opt-in 0.1 note recall), or change tool arguments. The only changes to agent-visible content are the sentence appended to a failing tool result and, when the runtime guard vetoes a command, its fixed refusal message in place of the result.
- It does not stop, restart or kill anything. The runtime guard only refuses a command; it never runs one.
- It does not write to Hermes' history: `state.db` and the logs are opened read-only.
- It changes nothing in other profiles. `status` only counts their sessions and prints the install command.
- It does not approve, deny or route around Hermes approvals, and it never turns a refused approval into advice.
- It registers no middleware, provider, memory provider, redaction pattern or locale, and overrides nothing in Hermes core.
- It creates no Cron job. The recipes are printed for you to create. The only Kanban card it creates is the blocked card of `ops handoff`, which needs `kanban_handoff`.
- It opens no network connection and sends no telemetry. A model is called only by the reflector, through Hermes.
- It does not let the agent decide: playbook decisions refuse to run in an agent's shell.

## Installation notes

- **From GitHub.** `hermes plugins install sesantanajr/hermes-atlas-evolution --enable` in the chosen profile; `hermes -p NAME` selects a profile. Check with `hermes plugins doctor --ci hermes-evolution`.
- **Every profile you use.** Hermes loads plugins per profile, even when one gateway serves several profiles, so install the plugin in each one: `hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable`. `hermes evolution status` lists the busy profiles of the same Hermes root that do not have it.
- **Manually.** Copy the whole plugin folder, with `plugin.yaml` at its root, to `<HERMES_HOME>/plugins/hermes-evolution/` and run `hermes plugins enable hermes-evolution`. The default home on Linux is `~/.hermes`. Windows installations usually use `%LOCALAPPDATA%\hermes`. Follow the profile's maintenance procedure if the gateway is already running.
- **Host isolation.** Under `plugins.isolation: host` Hermes skips `register_cli_command`, so use `python -m hermes_evolution --home <profile> ...` from the plugin folder. The reflector does not run in that standalone runner; waiting excerpts are kept for the next pass run by Hermes. The autopilot and the reflector were not verified under host isolation. See [Operations](operations.md#running-outside-hermes).

## Advanced: let the native background review propose lessons

Hermes can admit a named tool to the background review through `auxiliary.background_review.extra_tools`. Adding `atlas_evolution_lessons` lets the review record what it learned as a lesson proposal in the playbook, next to its usual skill and memory writes.

```yaml
# config.yaml of the profile
auxiliary:
  background_review:
    extra_tools:
      - atlas_evolution_lessons
```

- This is operator configuration in Hermes. The plugin never sets it.
- The tool must be available to the parent agent, so it also requires `agent_tools` to be on.
- A lesson proposed this way has origin `agent`: it is stored in quarantine, its proposal is high risk and it reaches the agent only after you approve it. The tool accepts at most 5 proposals per hour.
- An approved proposal is shown in the prompt section like any other lesson. It is not tied to a failure signature, so it gets no individual before-and-after verdict. Its activation still counts as a concurrent change for the other verdicts.
- This combination was not exercised on a production gateway.

## Where the next validation has the most value

1. Run the loop on a production gateway for a few weeks and compare the lesson verdicts and the skills flagged by outcome with a manual review of the same turns.
2. Follow the autopilot in a long-running gateway for several days: the passes shown by `status`, their duration, and the hook latency while a pass runs.
3. Review the reflector's drafts against the failures they came from, beyond the three real calls made so far, and choose a low-cost model for `auxiliary.evolution_reflector`.
4. Read the runtime guard's vetoes and the radar's proposals against what really happened on the host, to find false positives.
5. Compare the duration and the quality of real research before and after the plans. Use the same set of questions, validate the sources and record where the budget left a material gap.
6. Cross-check worker failures, overlapping schedules and queue volume. Make one change at a time and follow latency, error rate and backlog.
7. Build a controlled set of Linux scenarios with CPU, I/O and memory pressure. Check the effective controller value, the impact on Hermes and the rollback with the agent and the auxiliary unit running.
