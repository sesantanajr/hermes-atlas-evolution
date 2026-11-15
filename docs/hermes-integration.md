# Atlas Evolution and the Hermes interfaces

**English** | [Português (Brasil)](pt-BR/hermes-integration.md)

The integration uses the Hermes Agent 0.23.1 tree as its reference (commit `3b421578588243f920886e6fe79a6faa87ea026e`). This document lists what the plugin registers in Hermes, what it reads from a profile, which setting unlocks each capability and where further validation is most useful. [Official release](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [Public plugin API](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/plugins/public-api.md)

The plugin uses only the published plugin API: the `ctx` methods below and the modules `hermes_constants`, `hermes_cli.config` and `agent.redact`. The private version import of earlier releases was removed in 0.4.0.

## What the plugin registers

| Surface | Plugin API | Registered when |
|---|---|---|
| 14 hooks, listed below | `ctx.register_hook` | always, except `transform_tool_result` |
| Prompt section `atlas-evolution.playbook`, up to 1,400 characters | `ctx.register_system_prompt_section` | `evolution.lessons` is `suggest` or `auto` |
| Tools `atlas_evolution_plan` and `atlas_evolution_lessons`, toolset `atlas-evolution` | `ctx.register_tool` | always; both stay unavailable to the agent until `agent_advice` is `true` |
| Skill `hermes-evolution:evolution-review` | `ctx.register_skill` | when the host offers the method |
| CLI command `hermes evolution` | `ctx.register_cli_command` | in-process plugins only; Hermes skips it under `plugins.isolation: host` |
| Slash command `/evolution` for the 0.1 notes | `ctx.register_command` | `legacy_memory_commands` is `true` |
| Unload callback that writes queued counters and closes the database | `ctx.on_unload` | when the host offers the method |

## Hooks

The manifest declares the same 14 hooks, and a test compares the two lists.

| Hook | What the plugin does with it |
|---|---|
| `post_api_request` | Counts the call, its tokens and its duration per provider and model. |
| `api_request_error` | Counts every failed attempt, classified as rate limit, authentication, timeout, upstream or error. |
| `post_auxiliary_call` | Counts auxiliary calls and failures per task. |
| `post_tool_call` | Counts the tool outcome, classifies a failure into a signature and times turns that use web research tools. |
| `transform_tool_result` | Adds the matching lesson to a failing result. Registered only when lessons are on and `tool_hints` is `true`. |
| `pre_llm_call` | Starts the turn timer and classifies owner feedback in memory. Returns context only for the opt-in 0.1 note recall. |
| `post_llm_call` | Closes the timing of a research turn. |
| `on_session_end` | Records the outcome and the duration of the turn. |
| `on_skill_lifecycle` | Counts skill loads, patches and other lifecycle events. |
| `subagent_start`, `subagent_stop` | Count delegations, their outcomes and their duration. |
| `post_compaction` | Records tokens before and after a compaction when Hermes reports them. |
| `post_curator_run` | Counts Curator runs, dry or real. |
| `on_kanban_worker_exited` | Counts worker exits by kind. |

Every callback fails open: a hook never raises into a turn, and a failure is logged at most once per event type. Hook writes go through one database connection per process and profile. While the database is busy an update waits in a queue of up to 512 entries and is written with the next event. Overflow is counted and reported as `dropped_events`.

On the development machine a hook event took about 0.1 ms after this change, against about 6 ms before it, when the previous design also lost 155 of 800 events with 8 concurrent threads. These are development measurements on one machine, not a guarantee.

## What the plugin reads

All of it is read-only and bounded in size and time.

| Source | Used for |
|---|---|
| `skills/.usage.json`, `skills/.curator_ledger.jsonl` | Skill health and tracked skill changes. Metadata and hashes only; skill bodies are never opened. |
| `logs/agent.log` | The completion line of each background review. The tails of the Hermes logs are also scanned for error categories. |
| `state.db` | Integrity check and Hermes' own usage accounting. |
| `cron/jobs.json`, Kanban boards | Workload diagnostics, as counts. |
| `gateway_state.json`, `gateway.pid` | Gateway state, including a named profile served by the root gateway. |
| `memories/MEMORY.md`, `memories/USER.md` | Presence, size, UTF-8 readability and structure counts such as entries, duplicates and oversized entries. The content is not stored or reported. |
| Hermes configuration | An allowlist of settings for change tracking, and Cron, Kanban and delegation limits. |
| `/proc`, `/sys/fs/cgroup`, systemd unit properties | Linux pressure and service measurements. |

## How it fits the Hermes features

| Area | Hermes surface | Use by Atlas Evolution |
|---|---|---|
| Self-improvement | Background review, skills and the Curator ledger | Measures what the native loop leaves open: whether a rewrite was followed by better or worse metrics, which skills are rewritten in circles and what the reviews cost. The Curator keeps the life cycle of managed skills. [Curator](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator) |
| Tool failures | `post_tool_call`, `transform_tool_result`, system prompt sections | Turns recurring failures into lessons and delivers them without touching the cached prompt. [Observer hooks](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) |
| Research | `web_search`, `web_extract` and the grounded citations skill | Follows turns with recognized web tools, proposes a budget and defines what verifies a claim. [Web search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) · [Grounded citations](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/skills/bundled/research/research-grounded-citations.md) |
| Delegation | `delegate_task` and Kanban | Measures subagent outcomes. Proposes synchronous delegation for work tied to the turn and a blocked card for durable verification. [Kanban](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/user-guide/features/kanban.md) |
| Routines | Cron, local delivery and `no_agent` runs | Detects concentrated fires, delivery failures and jobs that cannot be scheduled. Generates recipes for the supervisor tick and the weekly digest. [Script-only cron](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/guides/cron-script-only.md) |
| Memory | Native provider and compaction | Observes compaction behaviour and inventories the structure of the native memory files. It never writes to memory and never stores its content. |
| Costs | Provider hooks and Hermes usage accounting | Separates input, cache and output tokens, measured costs and estimates. Compares behaviour with the profile's own baseline. |
| Resources | PSI, cgroup v2 and systemd | Joins pressure and service behaviour with profile health. A CPU, I/O or `MemoryHigh` trial requires explicit settings, verification and restoration. [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html) |

## Permissions

| Setting | Effect |
|---|---|
| Default installation | Observers with aggregated data. Recurring failures become candidate lessons that wait for approval. Operator commands can inspect the profile's signals. |
| `evolution.lessons: "auto"` | Derived lessons become active without approval and reach the agent in new sessions. |
| `evolution.lessons: "off"` | No lesson is derived and no lesson surface is registered. |
| `agent_advice` | Exposes `atlas_evolution_plan` and `atlas_evolution_lessons`. The first returns steps, a budget and verification criteria. The second searches lessons, reports the queue and stores proposed lessons in quarantine. With an explicit toolset list, include `atlas-evolution`. |
| `inspect_shared_kanban` | Includes the boards of the shared root that Hermes reports to the process. Reports show counts. |
| `kanban_handoff.enabled` | Authorizes `ops handoff` with a board and a profile chosen in advance. The card starts blocked and waits for operator review. |
| `linux_supervisor.mode: apply` | Authorizes temporary trials on the configured auxiliary unit, with the permissions the process already has in systemd. |

No setting lets the plugin lock, archive or roll back a skill, or change Hermes configuration. Those remain commands for the operator.

## Installation notes

- **From the catalog.** `hermes plugins install sesantanajr/hermes-atlas-evolution --enable` in the chosen profile; `hermes -p NAME` selects a profile. Check with `hermes plugins doctor --ci hermes-evolution`.
- **Manually.** Copy the whole plugin folder, with `plugin.yaml` at its root, to `<HERMES_HOME>/plugins/hermes-evolution/` and run `hermes plugins enable hermes-evolution`. The default home on Linux is `~/.hermes`. Windows installations usually use `%LOCALAPPDATA%\hermes`. Follow the profile's maintenance procedure if the gateway is already running.
- **Host isolation.** Under `plugins.isolation: host` Hermes skips `register_cli_command`, so use `python -m hermes_evolution --home <profile> ...` from the plugin folder. See [Operations](operations.md#running-outside-hermes).

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
- The tool must be available to the parent agent, so it also requires `"agent_advice": true` in `<HERMES_HOME>/evolution/settings.json`.
- A lesson proposed this way is stored in quarantine and reaches the agent only after you approve it. The tool accepts at most 5 proposals per hour.
- An approved proposal is shown in the prompt section like any other lesson. It is not tied to a failure signature, so it gets no individual before-and-after verdict. Its activation still counts as a concurrent change for the other verdicts.
- This combination was not exercised on a production gateway.

## Where the next validation has the most value

1. Run the loop on a production gateway for a few weeks and compare the lesson verdicts with a manual review of the same failures.
2. Compare the duration and the quality of real research before and after the plans. Use the same set of questions, validate the sources and record where the budget left a material gap.
3. Cross-check worker failures, overlapping schedules and queue volume. Make one change at a time and follow latency, error rate and backlog.
4. Build a controlled set of Linux scenarios with CPU, I/O and memory pressure. Check the effective controller value, the impact on Hermes and the rollback with the agent and the auxiliary unit running.
5. Reuse the outcome of verifications and of the Curator as reviewed operational lessons. Compare the effect of recommendations across different profiles before sharing policies.
