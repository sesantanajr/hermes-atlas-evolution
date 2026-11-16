# Changelog

## 0.5.0

This release makes the loop run by itself and learn from what Hermes already recorded, turns the reflector on by default, and adds a runtime guard, a health radar, a status command and owner notices. The plugin is covered by 253 unit and end-to-end tests against a simulated Hermes context, and passes `hermes plugins validate` on Hermes Agent 0.23.1 without warnings, with the security scan reporting it safe. The reflector made three real model calls through Hermes in production. The autopilot is verified in tests and in the CLI. Windows was only simulated in tests.

### Added

- **Autopilot.** The learning pass runs by itself in a background daemon thread, at most once per `autopilot_minutes` (default 60; 0 leaves passes to the operator; otherwise 15 to 1440). The hooks `post_llm_call`, `on_session_end` and `on_kanban_dispatch_tick` nudge it. A hook only compares clocks, and a check starts at most once every 60 seconds per process. Every process of a profile claims the pass with a compare-and-set lease in the plugin's database; the lease lasts 15 minutes, so a dead owner's lease expires, and a failed pass is retried after 10 minutes. The thread pins the plugin's own profile, because Hermes keeps the active profile in a context variable that new threads do not inherit. Settings are read again for each pass, and an invalid value falls back instead of failing the pass.
- **Learning from Hermes' history.** On the first passes the plugin replays the last 14 days of the profile's `<HERMES_HOME>/state.db`, read-only and 25 sessions at a time, through the trackers the hooks use: tool results become tool counts and failure signatures, user messages become turns and owner feedback, and skills loaded with `skill_view` are charged with the outcome of their turn. Messages sent before the agent answered join the same turn. Only history older than the moment the plugin started observing the profile is read (for an upgrade, the start of the earliest day with live data), and each slice is written in one transaction together with its cursor, so nothing is counted twice. Budget per pass: 20 seconds in the autopilot, 120 seconds in `learn`. Statuses: `done`, `running`, `waiting`, `no_history`, `unreadable` and `unsupported_history`. On a copy of a real profile: 275 sessions, 436 turns and 6,731 tool calls in 1.2 s and 29 MB, producing 7 lessons on the first pass.
- **Runtime guard** (`runtime_guard`, on by default), a `pre_tool_call` callback for terminal-like tools. It vetoes commands that would stop, restart or kill the gateway the agent runs on (`systemctl` restart, stop, kill and similar actions on a `hermes-gateway` unit, `service` restart or stop of it, `pkill` or `killall` of hermes) and tells the agent to use `hermes gateway restart`, which waits for running turns. It also vetoes `rm`, `rmdir`, `unlink`, `shred`, `mv` and `find` with `-delete` aimed at the running interpreter and its environment, the Hermes source, the Hermes root, the profile, a folder above any of them, or a glob that matches one. Only the command word counts, after variable assignments and wrappers such as `sudo` or `timeout`. Here-document bodies are ignored, and commands over 20,000 characters are let through unparsed. Vetoes are counted by kind in the new table `guard_daily`.
- **Health radar**, read at each pass and shown in the new "Hermes health" section of the digest. All readings are read-only: sessions of the last 7 days that end as orphans (`ws_orphan_reap`, `startup_orphan_reap`), proposed at 10 or more ended sessions and 30% or more orphaned, subagents excluded; prompt tokens per agent call and the cached share, from `session_model_usage` rows with an empty or `main` task, proposed at 50 or more calls and an average of 60,000 tokens or more, with `hermes prompt-size` as the command; compactions that keep 75% or more of the context, with at least 5 samples; and plugins of this process, from the list of Hermes' plugin manager, that failed to load (only the exception name is kept), register more hooks than their manifest declares, or declare the same first-string-wins transform hook (`transform_tool_result`, `transform_terminal_output`, `transform_llm_output`). Findings become advisory proposals of the new kinds `session_lifecycle`, `context_weight`, `compaction`, `plugin_health` and `plugin_conflict`, each with the command to inspect it, and expire when the evidence goes away.
- **`hermes evolution status [--format md|json] [--lang en|pt]`.** It shows the events the hooks saw today, the last autopilot pass, the backfill status and totals, the reflector (calls today, drafts waiting), lesson counts, open proposals and guard vetoes in 7 days, and the other profiles of the same Hermes root that had sessions in 7 days and do not have the plugin, with the exact install command, since each profile loads its own plugins.
- **Owner notice** through `on_session_finalize`: at most one short notice per UTC day about decisions waiting (lessons, reflector drafts, high-risk proposals, a failing pass), only on the owner's local surfaces (CLI, TUI, Desktop), never in a gateway chat, where the other side may be someone else.
- **Settings** `autopilot_minutes` and `runtime_guard`, in the Hermes settings form and in `settings.json`.
- **Hooks** `pre_tool_call`, `on_session_finalize` and `on_kanban_dispatch_tick`. The manifest declares 17 hooks and 2 tools.
- **Tracked settings** `compression.threshold`, `compression.target_ratio`, `compression.protect_last_n` and `skills.auto_load`, so a change to one of them gets a before-and-after verdict.

### Changed

- **The reflector is on by default.** An invalid value switches it off. It still keeps excerpts only while `lessons` is not `off`, passes them through Hermes' redactor with `force=True` and the plugin's masks, and calls the auxiliary task `evolution_reflector`, which inherits the compression model. The caps are unchanged: 3 calls per pass and `reflector_daily_limit` per day (default 6). Drafts stay in quarantine in every mode, `auto` included, and the operator approves them. In production, three real calls through Hermes (provider DeepSeek, about 600 tokens and about 2 seconds each) produced three drafts in Portuguese.
- **Passes never overlap.** A manual `hermes evolution learn` waits up to 30 seconds for a running pass instead of running alongside it, and then counts as the latest pass. `ops tick` uses the same claim.
- **Short transactions.** No transaction spans a model call or a log read, so hook writers of a running gateway never wait for the reflector.
- **Background reviews.** The first run also reads `agent.log.1` to `agent.log.5` inside the 14-day window.
- **Digest.** Lessons awaiting approval are grouped in one line, and up to 20 other open proposals are listed instead of 12 proposals in all.
- A lesson is not attached to a tool result that already carries a `hint` from Hermes' own tools, such as the terminal's.
- `gui` counts as a local surface.
- **Storage.** The database moves to schema 8 automatically, from schemas 0 to 7.

### Fixed

- After a log rotation, the rest of the old `agent.log` is read before the new one, so background reviews logged just before Hermes renamed the file are no longer lost. The first run no longer starts near the end of the log.
- A pass no longer holds the database write lock while it reads `agent.log` or waits for the reflector's model.
- Two processes that derive the same lesson at the same time no longer fail the pass: derived lessons are inserted idempotently.
- A manual `learn` could run at the same time as the pass of `ops tick`. Both now claim the pass.
- A reflector call is counted before it is made. A crash or a killed process in the middle of a pass could let the next pass exceed the daily cap.

### Security

- **Runtime guard** (see Added). On a real server the agent restarted its own gateway with `systemctl` 13 times in a week, 12 of them from one chat on one day, and once deleted the Python runtime the gateway unit used, which caused about an hour of downtime. The guard is a pure function that never raises, because Hermes blocks a tool whose `pre_tool_call` raises, and any doubt lets the command through.
- **Reflector drafts stay quarantined in every mode**, `auto` included, because a crafted error message could otherwise reach the agent's prompt.
- **The backfill keeps only what the hooks keep.** It opens `state.db` read-only, and text is classified in memory and discarded.
- **Owner notices never go to a gateway chat.**
- **Settings fail safe.** An invalid `reflector` switches the reflector off, an invalid `runtime_guard` keeps the guard on, and an invalid `autopilot_minutes` leaves passes to the operator.

### Upgrade notes

- The first use after upgrading migrates `evolution.sqlite3` to schema 8. Earlier versions cannot open a migrated database. To go back to 0.4.0, restore a backup of the file or delete it.
- Restart the gateway after upgrading, so that the new hooks, the autopilot and the runtime guard are registered.
- The reflector is now on in profiles that never set `reflector`. It keeps scrubbed excerpts and can make up to `reflector_daily_limit` model calls a day (6 by default) through `evolution_reflector`, which uses the compression model unless `auxiliary.evolution_reflector` names another. Set `reflector: false` to keep the behaviour of 0.4.0.
- The runtime guard is on: agent terminal commands that restart or stop the gateway with `systemctl`, `service`, `pkill` or `killall` are vetoed, and the agent is told to use `hermes gateway restart`. Set `runtime_guard: false` to remove it.
- On an upgrade, the backfill reads only the history older than the earliest day with live data in the plugin's database, so nothing is counted twice. The background review counter keeps its cursor and does not read the rotated logs.
- A weekly digest recipe or an `ops tick` schedule keeps working; their passes share the autopilot's claim. To run passes only on your own schedule, set `autopilot_minutes` to 0.
- Hermes loads plugins per profile. Install the plugin in every profile you use with `hermes -p <profile> plugins install sesantanajr/hermes-atlas-evolution --enable`; `hermes evolution status` lists the busy profiles that lack it.

## 0.4.0

This release adds the evolution loop, which is now the main purpose of the plugin, and speaks the profile's language in English or Brazilian Portuguese. The operations supervisor of 0.2.0 and 0.3.0 remains. The plugin is covered by 207 unit and end-to-end tests against a simulated Hermes context, and passes `hermes plugins validate` and `hermes plugins doctor` on Hermes Agent 0.23.1 without warnings. It has not run on a production gateway yet, and the optional reflector has not made a real model call.

### Added

- **Failure signatures.** Each failed tool call is classified into one of 20 classes and stored as a signature of tool, class and a short identifier-like detail. Every tool call and every turn (duration, completed, failed, interrupted, slow) is counted per surface: local, gateway, cron and subagent. Owner corrections, repeated questions and thanks are classified in memory and stored as counters.
- **Playbook of lessons.** A recurring failure whose class has a template becomes a one-sentence lesson rendered from fixed English and Portuguese templates. Lessons reach the agent through the session prompt section `atlas-evolution.playbook` and as a hint attached to the failing tool result, once per failure per session and at most 6 per session. Modes `off`, `suggest` (default) and `auto`.
- **Lesson measurement.** Each lesson bound to a failure is measured against that failure: a 14-day baseline before activation, counted once and frozen, against the latest 14 days. An ineffective lesson is retired in `auto` mode or becomes a proposal.
- **Skills by outcome.** Each skill loaded in an interactive turn (local or gateway), matched by session or task, inherits how the turn ended: failed, interrupted, slow, with a tool error, or corrected by the owner in the next message of the session within 30 minutes. A skill with at least 8 turns that goes materially worse than the profile becomes a `skill_outcome` proposal. `hermes evolution skills` returns an `outcomes` block and the digest a "Skills by outcome" section.
- **Change verdicts.** A skill rewrite is judged by the turns that loaded that skill in the week before and the week after, and a regression proposes the native `hermes curator rollback <entry-id>`. A configuration change gets a week-before versus week-after verdict of the profile's metrics, labelled correlational and reported with the number of concurrent changes. Values that are not plain labels, such as `provider/model`, are tracked as short fingerprints.
- **Skill health** from the Curator ledger and the usage sidecar: excessive rewriting, flip-flops, rewrites never exercised and agent-created skills never used. Metadata only.
- **Learning yield.** Background reviews are counted with their result, model calls and tokens, and the digest shows the share of tokens that went to learning.
- **Optional reflector**, off by default. For recurring failures without a template it keeps one scrubbed error excerpt of up to 300 characters per failure and asks a model, through Hermes' `ctx.llm.complete_structured` and the plugin's own auxiliary task `evolution_reflector` (inheriting from `compression`), for one conditional lesson that matches a JSON schema. Calls are capped at 3 per pass and `reflector_daily_limit` per day (default 6). Drafts pass an injection filter, stay in quarantine bound to their failure and are measured once approved. Excerpts go through Hermes' redactor with `force=True` and the plugin's masks, and are deleted once the failure has a lesson, when a draft is declined, after 14 days, or when the reflector is off. Nothing is drafted while `lessons` is `off`.
- **Proposal queue** with evidence, a risk level and the exact command. A lesson proposal carries the exact sentence, and its risk follows the author: template or operator low, reflector medium, agent high. The plugin applies only changes to its own playbook; changes to Hermes are shown as native commands for the operator.
- **English and Brazilian Portuguese** for everything a person or the agent reads: lessons, the digest, diagnoses, plans, recommendations, Kanban cards, agent tool replies, errors and the command help. The language follows the plugin's `language` setting, then the profile's `HERMES_LANGUAGE`, then `display.language`, then the locale, then English. `--lang en|pt` works before or after any subcommand. A test extracts the diagnostic sentences, command help, Kanban card texts and plans from the code and fails when one lacks its pair in the catalog.
- **Hermes settings form.** `config_schema` in `plugin.yaml` exposes `lessons`, `language`, `tool_hints`, `feedback_signals`, `agent_tools`, `slow_turn_seconds`, `owner_sender_ids`, `reflector` and `reflector_daily_limit` in Hermes Desktop under Settings → Plugins. Values stored in `plugins.entries.hermes-evolution.settings` win over the same keys in `settings.json`, which keeps the advanced options.
- **Digest** in Markdown or JSON and a no-agent cron recipe for weekly delivery (`report --schedule-spec`).
- **Commands** `learn`, `lessons`, `skills`, `proposals`, `experiments` and `report` under `hermes evolution`.
- **Bundled skill** `hermes-evolution:evolution-review`, and the agent tool `atlas_evolution_lessons`, which is behind `agent_tools` like `atlas_evolution_plan`.
- **Hooks** `transform_tool_result`, `on_session_end` and `on_skill_lifecycle`. The manifest declares 14 hooks and 2 tools. The plugin also uses `register_skill`, `register_session_env`, `register_auxiliary_task` and `on_unload` when the host offers them.
- `ops tick` runs one evolution pass at most once an hour and returns it in the field `evolution`.
- Supervisor output fields `dropped_events`, `tool_result_error`, `schedule_errors`, `recent_fire_errors`, `scheduled` (Kanban) and `served_by_host_gateway`.
- The standalone runner (`python -m hermes_evolution`) reads the profile's `config.yaml` when PyYAML is installed and uses defaults without it.
- Documentation in English and Brazilian Portuguese, `CONTRIBUTING.md`, `SECURITY.md`, a test workflow for Python 3.11 to 3.13, and issue and pull request templates.

### Changed

- **Storage.** The tables of the evolution loop live in `<HERMES_HOME>/evolution/evolution.sqlite3`, next to the observer metrics. The database moves to schema 7 automatically, from schemas 0 to 6. See the upgrade notes.
- **Hook writes** share one connection per process and profile. An update that finds the database busy waits in a bounded queue of 512 entries, and overflow is counted and reported as `dropped_events`. On the development machine a hook event took about 0.1 ms after the change, against about 6 ms before it. This is one measurement on one machine, not a guarantee.
- Hook callbacks report any failure once and never raise into a turn. Queued counters are written and the connection is released when the plugin unloads.
- Supervisor diagnoses, which were mostly in Portuguese, follow the profile language.
- The 35-day retention of observer metrics is applied by the first hook write of each UTC day, so it no longer needs a scheduled `tick`.
- The native redactor lookup used when cleaning failure details is cached.
- Outside Linux the host, cgroup and remediation code stays inert, and the modules import without the POSIX-only APIs. This was simulated in tests and not run on a real Windows host.

### Fixed

- Hook events were lost under concurrency: in a development test with 8 threads, 155 of 800 events were dropped, and three hook processes next to a running tick lost 586 of 2,400. The same tests lose none now.
- Each failed attempt of a retried request is counted. Retries that shared a request ID used to count as one, so rate limit findings never reached their threshold.
- Request and tool call IDs that Hermes or a provider reuses are no longer discarded as repeated deliveries.
- Provider failures are classified from the verdict Hermes gives them. Connection timeouts, quota replies, 529 responses and failures of auxiliary calls were stored as a generic error.
- A tool that ends with status `timeout` counts as a failed attempt instead of `unknown`.
- Gateway diagnostics and the Linux scan work for a named profile served by the root gateway.
- Log failures are attributed by logger and not by words in tool output. This removes false storage, memory and plugin faults such as `native_memory_pipeline_fault`, and detects memory provider timeouts and stuck background calls.
- The `seen` table is pruned without depending on `tick`.
- `page_cache_headroom`, a diagnosis that proposes no action, is no longer sent as an alert.
- Cron jobs that cannot be scheduled or dispatched are reported.
- Kanban cards parked in `scheduled` are counted.
- File paths reported as model names, including Windows drive-letter paths, are kept out of model labels.
- `run_policy` no longer fails when two findings share a code or when a stored feedback value is unknown.
- A `--home` that does not exist is no longer created. Opening a database used to create the whole directory tree with empty databases.
- Native billing costs are rounded to eight decimals.
- The private Hermes version import (`hermes_cli.version_info`) was removed. The CLI reads the version from the loaded `hermes_cli` package and skips the update check when it is unavailable.
- JSON printed by the CLI keeps accented characters.
- Settings nested too deeply are rejected with a clear error.
- The inventory checks native memory files against the limits configured in the profile, not only the upstream defaults.
- Found in review before this release and fixed in it: a message that merely starts with "Não " counted as a correction; a busy database could leave a new session without its lessons; retention could turn an effective lesson ineffective; regression proposals expired once their verdict became final; per-day lesson rates ignored the calendar length of each window; review lines from other loggers or in UTC days could be miscounted.

### Security

These come from an independent security and correctness review of the loop before release.

- **Approval refusals never become lessons.** A denied, timed-out, unreachable or non-interactive approval is the class `approval_refused`, with no template, no reflector draft and no investigation proposal. The `blocked_by_policy` lesson tells the agent not to retry or look for another way, and to tell the user.
- **Only the operator decides.** `lessons approve`, `retire` and `add` and `proposals approve` and `reject` refuse to run when they find `HERMES_SESSION_ID`, `HERMES_KANBAN_TASK`, `HERMES_DELEGATED_CHILD_CONTEXT` or `ATLAS_EVOLUTION_AGENT`, which the plugin adds to every subprocess of an agent session through `register_session_env`. Activation shows the exact text and asks, or requires `--yes` without an interactive terminal. `lessons add` creates a quarantined lesson unless `--activate` is given. The bundled skill hands every decision to the owner as a command to run in their own terminal.
- **What the operator reads is what the agent gets.** Lesson text is NFKC-normalized, and control, bidirectional, zero-width and private-use characters are refused.
- **Untrusted output.** A detail read from command output is kept only when it looks like a lower-case command or module name. The tool name is read from Hermes' error text only for the unresolved `tool_call` wrapper. Hermes' redactor runs with `force=True` on failure details.
- **Settings fail closed.** Inside the gateway an invalid value falls back field by field, and an invalid `lessons` value or an unreadable `settings.json` means lessons `off`.

### Upgrade notes

- The first use after upgrading migrates `evolution.sqlite3` to schema 7. Earlier versions cannot open a migrated database. To go back to 0.3.0, restore a backup of the file or delete it.
- The hook connection stays open while Hermes runs. On Windows this prevents deleting `evolution.sqlite3` until Hermes stops.
- Under `plugins.isolation: host` Hermes skips the plugin's CLI command. Use `python -m hermes_evolution --home <profile> ...`. The reflector does not run there and keeps its waiting excerpts for the next pass run by Hermes.

## 0.3.0

- Added creator attribution, adaptive operational feedback, sensitivity levels and outcome review.
- Added Cron and Kanban workload diagnostics plus Hermes-native delegation, compaction, Curator and worker telemetry.
- Added bounded research and delegation plans, an opt-in advisory tool, blocked Kanban verification handoffs and a script-only Cron recipe.
- Added opt-in, reversible Linux memory-pressure trials alongside CPU and I/O trials.

## 0.2.0

- Adds Linux PSI, cgroup, memory, filesystem and gateway diagnostics with bounded local reads.
- Audits native Hermes memory metadata, plugin footprints and skill duplication without collecting raw conversation contents.
- Correlates provider events, completed research timing and daily baselines with operational findings.
- Adds journaled, verified and reversible systemd CPU and I/O weighting for an explicitly configured background service. Observation is the default.
- Adds bilingual incident similarity and optional offline neural embeddings for advisory lookup.
- Keeps legacy 0.1 notes available while placing new note creation and chat integration behind an explicit opt-in.
