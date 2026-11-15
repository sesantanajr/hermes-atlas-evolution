# Changelog

## 0.4.0

This release adds the evolution loop, which is now the main purpose of the plugin. The operations supervisor of 0.2.0 and 0.3.0 remains. The loop is covered by unit tests and by end-to-end tests against a simulated Hermes context, and the plugin passes `hermes plugins validate` and `hermes plugins doctor` on Hermes Agent 0.23.1. It has not run on a production gateway yet.

### Added

- **Failure signatures.** Each failed tool call is classified into one of 20 classes and stored as a signature of tool, class and a short identifier-like detail. Every tool call and every turn (duration, completed, failed or interrupted) is counted per surface: local, gateway, cron and subagent. Owner corrections, repeated questions and thanks are classified in memory and stored as counters.
- **Playbook of lessons.** A recurring failure becomes a one-sentence lesson rendered from fixed English and Portuguese templates, with no model call. Lessons reach the agent through the session prompt section `atlas-evolution.playbook` and as a hint attached to the failing tool result, at most once per session for each lesson. Modes `off`, `suggest` (default) and `auto`.
- **Measurement.** Each derived lesson is measured against its own failure before and after activation. An ineffective lesson is retired in `auto` mode or becomes a proposal. Skill rewrites and configuration changes get a week-before versus week-after verdict that is labelled correlational and reports concurrent changes.
- **Skill health** from the Curator ledger and the usage sidecar: excessive rewriting, flip-flops and rewrites never exercised. Metadata only.
- **Learning yield.** Background reviews are counted with their result, model calls and tokens, and the digest shows the share of tokens that went to learning.
- **Proposal queue** with evidence, a risk level and the exact command. The plugin applies only changes to its own playbook. Changes to Hermes are shown as native commands for the operator.
- **Digest** in Markdown or JSON, in English or Portuguese, and a no-agent cron recipe for weekly delivery.
- **Commands** `learn`, `lessons`, `skills`, `proposals`, `experiments` and `report` under `hermes evolution`.
- **Bundled skill** `hermes-evolution:evolution-review` and the agent tool `atlas_evolution_lessons`, which is behind `agent_advice` like `atlas_evolution_plan`.
- **Hooks** `transform_tool_result`, `on_session_end` and `on_skill_lifecycle`. The manifest now declares 14 hooks and 2 tools.
- **Settings** block `evolution`: `lessons`, `tool_hints`, `feedback_signals`, `language`, `slow_turn_seconds` and `owner_sender_ids`.
- `ops tick` runs one evolution pass at most once an hour and returns it in the field `evolution`.
- Supervisor output fields `dropped_events`, `tool_result_error`, `schedule_errors`, `recent_fire_errors`, `scheduled` (Kanban) and `served_by_host_gateway`.
- The standalone runner (`python -m hermes_evolution`) reads the profile's `config.yaml` when PyYAML is installed and uses defaults without it.
- Documentation in English and Brazilian Portuguese, `CONTRIBUTING.md`, `SECURITY.md`, a test workflow for Python 3.11 to 3.13, and issue and pull request templates.

### Changed

- **Storage.** The tables of the evolution loop live in `<HERMES_HOME>/evolution/evolution.sqlite3`, next to the observer metrics. The database moves to schema 4 automatically, from schemas 0 to 3. See the upgrade notes.
- **Hook writes** share one connection per process and profile. An update that finds the database busy waits in a bounded queue of 512 entries, and overflow is counted and reported as `dropped_events`. On the development machine a hook event took about 0.1 ms after the change, against about 6 ms before it. This is one measurement on one machine, not a guarantee.
- Hook callbacks report any failure once and never raise into a turn. Queued counters are written and the connection is released when the plugin unloads.
- The 35-day retention of observer metrics is applied by the first hook write of each UTC day, so it no longer needs a scheduled `tick`.
- Outside Linux the host, cgroup and remediation code stays inert, and the modules import without the POSIX-only APIs. This was simulated in tests and not run on a real Windows host.

### Fixed

- Hook events were lost under concurrency: in a development test with 8 threads, 155 of 800 events were dropped. The same test loses none now.
- Each failed attempt of a retried request is counted. Retries that shared a request ID used to count as one.
- Request and tool call IDs that Hermes or a provider reuses are no longer discarded as repeated deliveries.
- Provider failures are classified from the verdict Hermes gives them. Timeouts, 529 responses and failures of auxiliary calls were stored as a generic error.
- A tool that ends with status `timeout` counts as a failed attempt.
- Gateway diagnostics work for a named profile served by the root gateway.
- Log failures are attributed by logger and not by words in tool output, which removes a false `native_memory_pipeline_fault`.
- The `seen` table is pruned without depending on `tick`.
- `page_cache_headroom`, a diagnosis that proposes no action, is no longer sent as an alert.
- Cron jobs that cannot be scheduled or dispatched are reported.
- Kanban cards parked in `scheduled` are counted.
- File paths reported as model names, including Windows drive-letter paths, are kept out of model labels.
- `run_policy` no longer fails when two findings share a code or when a stored feedback value is unknown.
- A `--home` that does not exist is an error. It used to create the directory tree with empty databases.
- Native billing costs are rounded to eight decimals.
- The private Hermes version import was removed. The CLI reads the version from the loaded `hermes_cli` package and skips the update check when it is unavailable.
- JSON printed by the CLI keeps accented characters.
- Settings nested too deeply are rejected with a clear error.
- The inventory checks native memory files against the limits configured in the profile, not only the upstream defaults.

### Upgrade notes

- The first use after upgrading migrates `evolution.sqlite3` to schema 4. Version 0.3.0 cannot open a migrated database. To go back to 0.3.0, restore a backup of the file or delete it.
- The hook connection stays open while Hermes runs. On Windows this prevents deleting `evolution.sqlite3` until Hermes stops.
- Under `plugins.isolation: host` Hermes skips the plugin's CLI command. Use `python -m hermes_evolution --home <profile> ...`.

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
