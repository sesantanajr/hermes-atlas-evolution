# Operations supervisor

**English** | [Português (Brasil)](pt-BR/operations.md)

Besides the [evolution loop](evolution-loop.md), Atlas Evolution supervises the host and the workload of a Hermes profile. It examines what Hermes records, measures the load of the agent and of Linux, and cross-checks the evidence to suggest tests and to conduct reversible remediation. It observes by default, uses only the public Hermes plugin API and keeps your memory provider, plugins and router as they are.

All commands below are subcommands of `hermes evolution`. Field names, codes and identifiers in their JSON output are always in English. Human-readable text (hypotheses, alternatives, recommendations, plans, Kanban cards and error messages) is printed in English or Brazilian Portuguese: `--lang en|pt` before or after any subcommand, otherwise the plugin's `language` setting, then `HERMES_LANGUAGE`, `display.language` and the process locale, and finally English. The command help follows `HERMES_LANGUAGE`, `display.language` and the locale.

## Contents

1. [What the supervisor measures](#what-the-supervisor-measures)
2. [Commands](#commands)
3. [Diagnoses and adaptive alerts](#diagnoses-and-adaptive-alerts)
4. [Usage and cost](#usage-and-cost)
5. [Research, delegation and scheduling](#research-delegation-and-scheduling)
6. [Linux remediation](#linux-remediation)
7. [Configuration keys](#configuration-keys)
8. [Data and compatibility](#data-and-compatibility)
9. [Commands kept from earlier versions](#commands-kept-from-earlier-versions)
10. [Removing the plugin](#removing-the-plugin)

## What the supervisor measures

| Area | Evidence collected | Interpretation |
|---|---|---|
| Linux | Available RAM, reclaimable cache, swap, PSI, cgroup limits, CPU, I/O, volume, inodes and known areas of the profile | Separates useful cache from memory pressure. Cumulative counters describe history. PSI measures time with tasks waiting for a resource. |
| Hermes | Gateway state, readability and structure of native memory, SQLite integrity and categories of log errors | Cross-checks write failures against space, inodes and locks. Indicates when a failure calls for reproduction in Hermes, in the provider or in the plugin. The integrity check (`PRAGMA quick_check`) covers a `state.db` and WAL of up to 64 MiB, with a cooperative deadline of 300 ms; above that the status is `integrity_skipped_large`. |
| Plugins and skills | Size, configured state, recent evidence of use when attributable, duplicates and skill variants | Recent use is reported as data and produces no diagnosis. Duplicated skills produce review findings, and profile plugins explicitly disabled with 32 MiB or more produce `large_disabled_plugins`. |
| Research and tokens | Duration of completed turns with known web tools, tool errors, retries, tokens by class and cache | Tells long research apart from tool failure, provider limits and low context reuse. The research p95 is approximated by buckets. |
| Delegation and maintenance | Subagent events, compaction, Curator runs and Kanban worker exits | Shows recurring failures and token coverage before and after compaction when Hermes reports it. |
| Queue and scheduling | Cron fire bursts, run and delivery failures, jobs that cannot be scheduled or dispatched, ready and blocked items, cards parked in `scheduled`, and signs of expired leases in Kanban | Helps identify congestion and choose between short tasks, durable work and scripts without a model. |

The collector stores aggregates. It does not record conversation text, native memory content, search results or raw log lines. An incident keeps the plugin's own hypothesis text.

Version 0.4.0 added these output fields:

| Field | Where | Meaning |
|---|---|---|
| `dropped_events` | `observer_usage` in `usage` and in the legacy `tick`; `usage` in `ops inspect` and `ops tick` | Hook events discarded because the write queue was full. Totals are then a lower bound. |
| `tool_result_error` | `logs.events` in `health`; `health.logs.events` in `ops inspect` and `ops tick` | Log lines that only echo the output of a failed tool. They no longer count as storage, memory or plugin faults. Present when greater than zero. |
| `schedule_errors`, `recent_fire_errors` | `workloads.cron.counts` | Enabled jobs Hermes could not schedule, and due fires in the last 24 hours that never reached the runner. |
| `scheduled` | `workloads.kanban.counts` | Cards waiting in the `scheduled` column. |
| `served_by_host_gateway` | `gateway` in `health`; `health.gateway` in `ops inspect` and `ops tick` | The profile is a named profile served by the root gateway, whose record is used for the checks. |

## Commands

```sh
hermes evolution ops inspect --upstream-tag v0.23.1
hermes evolution usage --days 7
hermes evolution ops tick --upstream-tag v0.23.1
hermes evolution ops history
hermes evolution ops review --days 14
hermes evolution ops plan research --effort deep
hermes evolution ops plan delegation --durable
hermes evolution ops similar "slow disk and long research"
```

| Command | What it does |
|---|---|
| `ops inspect` | Collects and explains the signals at this moment. It records no observation and changes nothing in Hermes. |
| `ops tick [--notify-only]` | Collects, compares with history, records hypotheses and baselines, and carries out only the actions the settings allow. It also runs one evolution pass at most once an hour and returns it in the field `evolution`. If the tick fails, it tries to end an active Linux trial. |
| `ops history` | Shows up to 100 incidents and 100 trials with their state. |
| `ops review [--days N]` | Cross-checks adaptive measurements, feedback and trial outcomes. Default 14 days, from 1 to 90. |
| `ops feedback CODE helpful\|dismissed\|reset` | Rates an adaptive alert that was already reported. |
| `ops similar "<text>" [--limit 1..10]` | Looks up earlier incidents similar to the text. Default 5. |
| `ops plan research\|delegation\|operations [--effort quick\|standard\|deep] [--durable]` | Returns a read-only plan. Default effort `standard`. |
| `ops handoff KIND SUBJECT` | Creates a blocked Kanban verification card. `KIND` is `resources`, `research`, `delegation`, `curator` or `gateway`. |
| `ops schedule-spec [--every-minutes N] [--upstream-tag TAG]` | Prints a no-agent cron recipe for `ops tick --notify-only`. |
| `ops rollback` | Ends an active Linux trial and restores the previous value. Without a trial it returns `idle`. |
| `usage [--days 1..35]` | Shows hook measurements and Hermes' native accounting in separate fields. |

`ops inspect` and `ops tick` accept `--days 1..35`, `--upstream-tag`, `--hermes-source` and `--sensitivity low|balanced|high`. `--upstream-tag` is a reference the operator provides for an offline comparison; nothing is downloaded. Inside Hermes, `--hermes-source` defaults to the running Hermes tree.

With `--notify-only`, `ops tick` prints nothing when nothing is new. Otherwise it prints `alerts`, `host_action` (only when a trial becomes `active`, `rolled_back`, `released` or `not_applied`), `maintenance_changes` and `evolution`: the new lessons, reflector drafts, regressions whose verdict became final, retired lessons and high-risk proposals of the pass, or `null`.

### Running outside Hermes

To run without enabling the plugin, or when the installation uses `plugins.isolation: host` (where Hermes skips plugin CLI commands), run from the extracted project root:

```sh
python -m hermes_evolution --home /path/to/profile --hermes-version 0.23.1 \
  ops inspect --upstream-tag v0.23.1
```

`--home` defaults to `HERMES_HOME`. The standalone runner reads the profile's `config.yaml` (up to 1 MiB) when PyYAML is installed, for the memory provider, limits, the Cron, Kanban and delegation settings, `display.language` and the plugin's own settings. Without PyYAML it uses defaults. `--hermes-source` adds the tree of plugins and skills distributed with Hermes to the inventory. The runner never creates a `--home` that does not exist: commands that open the Evolution databases fail, `health` reports `profile_present: false`, and `ops history`, `ops similar`, `ops rollback`, `ops plan`, `ops schedule-spec` and `ops handoff` return without error. The reflector of the evolution loop does not run in the standalone runner.

## Diagnoses and adaptive alerts

Each diagnosis presents a hypothesis, the evidence, alternatives, a confidence level and a proposed action.

Adaptive learning groups observations in six-hour UTC slots and compares the current sample with the same slot of earlier days, over 28 days, with robust statistics (a weighted median with a 7-day half-life and the median absolute deviation) once there are at least 5 samples. It considers available RAM, CPU, memory and I/O pressure, prompt tokens, research time, tool and API error rates and Kanban queue depth, according to coverage. Only `ops tick` records samples.

- `--sensitivity low|balanced|high`, or the `sensitivity` key, adjusts the threshold. The default is `balanced`, and the command-line option wins over the key.
- `ops feedback CODE helpful|dismissed|reset` adjusts how often an alert recurs. A dismissed alert stays quiet for 14 days. Other alerts recur at most every 12 hours, or every 6 hours when rated helpful, and a rise in severity is delivered at once. Critical alerts are always delivered, at most once an hour.
- A diagnosis that proposes no action, such as a healthy page cache, stays in the report and is not sent as a notice.

`ops similar` compares incidents with bilingual terms and trigrams. The `semantic_incident_model` key may point to the absolute path of a local multilingual SentenceTransformers model directory, already installed in `safetensors` format. The command then uses CPU and local files, with remote code and downloads disabled. Similarity helps you look incidents up. The remediation decision uses checkable measurements and independent rules.

## Usage and cost

`usage` presents hook measurements (`observer_usage`) and native accounting (`hermes_native_billing`) in separate fields, with data coverage and a distinction between actual and estimated costs.

- Provider failures are classified from the verdict Hermes gives each failed attempt (rate limit, authentication, timeout, upstream), and every failed attempt of a retried request is counted. A tool that exceeds its deadline counts as a failed attempt.
- `rates_usd_per_million` is a table by provider and model with the values `input`, `output`, `cache_read` and `cache_write`, each from 0 to 1,000,000. Keys are matched against the provider name and the model the provider returned, in lower case, so write them in lower case. All four values are required for a row to be priced. Reasoning tokens are not priced separately.
- `daily_estimated_budget_usd` is an optional limit, greater than 0 and at most 1,000,000, that uses the UTC day and the observed, priced share. When the estimate reaches it, `tick` reports the finding `evolution_daily_budget`. It does not stop Hermes or any model call. A partial estimate can differ from the invoice.

```json
{
  "rates_usd_per_million": {
    "example-provider": {
      "example-model": {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.25}
    }
  },
  "daily_estimated_budget_usd": 5
}
```

The provider, the model and the prices above are placeholders.

## Research, delegation and scheduling

`ops plan` returns a read-only plan with a suggested budget: search queries, page extracts and parallel lanes for research, parallel lanes and review passes for delegation, and comparable samples for operations. Research plans include source discovery, evidence verification and a record of uncertainties. Delegation plans point to `delegate_task` for answers the current agent must wait for and to Kanban for work that must survive restarts (`--durable`).

Turn on `agent_tools` in the Hermes plugin settings, or set `"agent_advice": true` in `<HERMES_HOME>/evolution/settings.json`, and reload the plugin to expose `atlas_evolution_plan` and `atlas_evolution_lessons` to the profile. Include `atlas-evolution` when the profile uses an explicit list of toolsets. The plan tool returns only the plan.

Reading shared Kanban boards requires `"inspect_shared_kanban": true`. Creating a card requires your own handoff authorization, independently of that key. The example assigns verifications to the profile `reviewer` on the existing board `ops`.

```json
{
  "agent_advice": true,
  "inspect_shared_kanban": true,
  "kanban_handoff": {"enabled": true, "board": "ops", "assignee": "reviewer"}
}
```

```sh
hermes evolution ops handoff resources "cpu-spikes"
hermes evolution ops schedule-spec --every-minutes 5 --upstream-tag v0.23.1
```

`handoff` creates a blocked card meant for verification through the native Kanban, with an idempotency key per profile, board, assignee, kind, subject and UTC day. The operator releases the card after reviewing its scope. The subject is an identifier of up to 80 characters. The command does nothing inside a Kanban worker or a delegated task, and needs `hermes` on the `PATH`.

`schedule-spec` delivers the script and the native Cron arguments to schedule `ops tick --notify-only` in `no_agent` mode as the job `atlas-evolution-watchdog`, with local output and without opening a model session. The interval is 5 to 1,440 minutes. Nothing is written. Review the script and apply the recipe in the chosen profile. The tick uses that profile's Linux policy. Use the five-minute interval in `apply` mode, so that trials are verified and closed.

## Linux remediation

By default the supervisor observes. The settings below authorize a trial on **one auxiliary unit** that is not critical, in the same `slice` as Hermes, under systemd with cgroup v2. Choose a background job whose performance may drop temporarily under contention. `observe` mode measures the two services first. `apply` mode authorizes the bounded adjustment. Outside Linux this code stays inert.

Save the settings in `<HERMES_HOME>/evolution/settings.json`.

```json
{
  "linux_supervisor": {
    "mode": "observe",
    "scope": "user",
    "hermes_unit": "hermes-gateway.service",
    "background_unit": "my-background-indexer.service",
    "resources": ["cpu", "io", "memory"],
    "memory_high_bytes": 536870912
  }
}
```

| Key | Values | Default |
|---|---|---|
| `mode` | `observe` or `apply` | `observe` |
| `scope` | `user` or `system` | `user` |
| `hermes_unit`, `background_unit` | two different `.service` unit names | required |
| `resources` | any of `cpu`, `io`, `memory` | `["cpu"]` |
| `memory_high_bytes` | a whole number of MiB, at least 128 MiB | required when `memory` is listed |

**Evidence required.** A trial starts only when the profile is present, `state.db` is healthy, no critical or gateway failure is reported and a diagnosis proposes rebalancing that resource. The supervisor then confirms two samples taken 60 seconds to 30 minutes apart, the identity of both services, PSI pressure in the Hermes cgroup and a share of at least 65% of the auxiliary unit in the activity of the measured pair. For I/O it requires activity on the same device. It uses the available controllers, an initial effective weight of 100 and unambiguous identification of the units.

**The adjustment.** For CPU and I/O it temporarily sets the auxiliary unit's `CPUWeight` or `IOWeight` to 50. For memory the `MemoryHigh` limit must be explicit, and the supervisor requires reclaimable clean cache, a margin above the working set and no sign of swap or OOM. Choose `memory_high_bytes` from the real unit. The example only shows the format.

**Verification and rollback.** Each adjustment uses `systemctl --runtime`. The supervisor records the intent before applying, checks the effective value, measures the response and restores the previous setting if the effect is uncertain or the pressure does not fall by at least 20%. The next tick after 15 minutes ends the trial. After any trial no new trial starts for one hour. An ineffective or unverified trial blocks the same unit and resource for 24 hours, and repeated ineffective trials within 14 days defer that resource for 24, 48 and then 96 hours. The command uses only the permissions the process already has. Adjustments observed from outside take precedence.

Schedule `hermes evolution ops tick` **every five minutes** in your existing scheduler, as the user that manages those units. Applying requires the verification ticks to be running as well. To end a trial early, run `hermes evolution ops rollback`. Use `ops history` to confirm that the weight was restored. The adjustment is temporary in systemd and disappears with a host reboot. Cleaning data, disabling plugins, memory changes and code fixes are review recommendations in this version.

Weight changes and rollbacks are tested with a simulated systemd adapter. Validate the controller and the chosen unit in staging before authorizing `apply`.

## Configuration keys

`<HERMES_HOME>/evolution/settings.json` is an optional JSON object of up to 64 KiB. The plugin reads it when it loads, so restart the gateway after a change; the commands read it each time. The options of the evolution loop are described in [The evolution loop](evolution-loop.md#settings-and-language). Those exposed in the Hermes settings form win over the same keys in this file.

| Key | Format | Default | Effect |
|---|---|---|---|
| `evolution` | object | see the loop | `lessons`, `language`, `tool_hints`, `feedback_signals`, `slow_turn_seconds`, `owner_sender_ids`, `reflector`, `reflector_daily_limit` |
| `agent_advice` | boolean | `false` | Exposes the agent tools (`agent_tools` in the Hermes form). |
| `sensitivity` | `low`, `balanced`, `high` | `balanced` | Threshold of adaptive alerts. |
| `rates_usd_per_million` | object by provider and model | none | Prices for cost estimates (see [Usage and cost](#usage-and-cost)). |
| `daily_estimated_budget_usd` | number above 0, up to 1,000,000 | none | Daily (UTC) limit on the priced estimate. |
| `inspect_shared_kanban` | boolean | `false` | Includes shared Kanban boards in diagnostics. |
| `kanban_handoff` | object: `enabled`, `board`, `assignee` | disabled | Authorizes `ops handoff`. `board` and `assignee` are lower-case slugs. |
| `linux_supervisor` | object | absent | Policy for reversible systemd trials (see [Linux remediation](#linux-remediation)). |
| `semantic_incident_model` | absolute path | none | Local embedding model for `ops similar`. |
| `legacy_memory_commands` | boolean | `false` | Allows new 0.1 notes and the `/evolution` chat command. |
| `automatic_shared_recall` | boolean | `false` | With `legacy_memory_commands`, injects matching shared 0.1 notes into a turn. |

## Data and compatibility

- The databases live in `<HERMES_HOME>/evolution/`: `evolution.sqlite3` for observer metrics and the evolution loop, and `supervisor.sqlite3` for incidents, baselines, adaptive samples and trials. Observer metrics are kept for 35 days; the loop's aggregates, incidents and closed trials for 90 days. The 35-day retention is applied by the first hook write of each UTC day in each process, so it holds even when no tick is scheduled. Hermes' own backups and logs follow their existing retention.
- Version 0.4.0 migrates `evolution.sqlite3` from schemas 0 to 6 to schema 7 automatically and refuses any other schema. An earlier plugin version cannot open a migrated database: going back requires restoring a backup of the file or deleting it.
- Hook writes share one connection per process and profile, which stays open while Hermes runs. On Windows this means `evolution.sqlite3` cannot be deleted while Hermes is running.
- Hermes' own databases (`state.db`, Kanban boards) are opened read-only.
- Note files from version 0.1 are preserved. The commands that record new notes and the legacy chat command require `legacy_memory_commands: true`. The old note injection also requires `automatic_shared_recall: true`. By default, inspection uses the signals of Hermes' native memory.
- The integration contract was checked against the official v0.23.1 tree, commit `3b421578588243f920886e6fe79a6faa87ea026e`.

## Commands kept from earlier versions

| Command | What it does |
|---|---|
| `health [--upstream-tag TAG]` | Prints the snapshot of Hermes files and host signals that `ops inspect` builds on. |
| `tick [--upstream-tag TAG] [--days 1..35] [--changes-only]` | Runs the maintenance policy of version 0.2 and records its decisions. `ops tick` already includes it. |
| `feedback CODE helpful\|dismissed\|reset` | Records how a maintenance finding performed. |
| `history` | Shows maintenance priorities and feedback. |
| `memory [--shared \| --dm PLATFORM USER CHAT] ...` | Access to the 0.1 note data: `remember`, `recall`, `evaluate`, `audit`, `forget`. |

## Removing the plugin

Run `hermes evolution ops rollback` before disabling the plugin with `hermes plugins disable hermes-evolution`, then reload the profile's gateway.

## References

[Hermes Agent v0.23.1](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1) · [Hermes hooks](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/website/docs/developer-guide/observer-hooks.md) · [Linux PSI](https://docs.kernel.org/accounting/psi.html) · [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) · [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html) · [SentenceTransformers](https://sbert.net/docs/package_reference/sentence_transformer/model.html)
