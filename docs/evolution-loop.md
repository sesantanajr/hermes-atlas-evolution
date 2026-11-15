# The evolution loop

**English** | [Português (Brasil)](pt-BR/evolution-loop.md)

This document describes the loop that Atlas Evolution 0.4.0 adds to Hermes Agent: what is recorded, when a failure becomes a lesson, how effects are measured and what ends up in the proposal queue. Every threshold below is a constant in the code, and the module that holds it is named next to it. For installation and a summary, see the [README](../README.md).

## Contents

1. [One pass](#one-pass)
2. [Signals](#signals)
3. [Lessons](#lessons)
4. [How a lesson is measured](#how-a-lesson-is-measured)
5. [Changes and experiments](#changes-and-experiments)
6. [Skill health](#skill-health)
7. [Learning yield](#learning-yield)
8. [Proposals](#proposals)
9. [Digest](#digest)
10. [Agent tools and the bundled skill](#agent-tools-and-the-bundled-skill)
11. [Storage](#storage)

## One pass

Hooks record signals while the agent works. Everything else happens in a pass (`evolve.cycle`), which reads Hermes' own records, updates the plugin's tables and never changes Hermes. A pass runs when:

- you run `hermes evolution learn`;
- `hermes evolution ops tick` runs, at most once an hour (the result is the `evolution` field of its output);
- the weekly digest recipe runs, which calls `learn --notify-only` before `report`.

Nothing schedules a pass by itself. Without one of the three, signals accumulate but no lesson is derived and nothing is measured.

A pass does the following, in order:

1. Drops aggregates older than the retention window.
2. Counts the background reviews logged since the previous pass.
3. Derives lessons from recurring failures.
4. Measures each active derived lesson.
5. Computes skill health for the last 7 days.
6. Detects configuration changes and skill rewrites.
7. Compares the week before and the week after each tracked change.
8. Closes approved `skill_lock` proposals whose skill is now locked or pinned.
9. Rebuilds the open proposals from the current evidence.

With `--notify-only`, `learn` prints only new lessons, regressions, lessons retired in this pass and open high-risk proposals. It prints nothing when there are none.

## Signals

### Tool outcomes

The `post_tool_call` hook counts every call that ends with status `ok`, `error` or `blocked`, per tool and UTC day: calls, errors, blocked calls and total duration. Other statuses, such as `timeout` or `cancelled`, are counted by the operations observer and do not enter the loop. Hermes wraps deferred tools in `tool_call`; a wrapper that carries a single call is attributed to the tool that ran.

A call with status `error` or `blocked` is classified. The classifier looks at the error message (first 2,000 characters) and, when the result is JSON, at its `error`, `hint`, `approval` and `message` fields, the last 1,500 characters of `output` and the `exit_code`. For a result that is not JSON it looks at the first 1,500 characters. Results larger than 262,144 characters are not parsed. The text is classified in memory and discarded.

The outcome is a signature: the first 20 hexadecimal characters of the SHA-256 of tool, class and detail. Two failures with the same tool, class and detail are the same failure.

### Failure classes

Rules are tried in the order of the table and the first match names the class (`signals._RULES`).

| Class | Matches, for example | Detail kept | Lesson template |
|---|---|---|---|
| `missing_argument` | `missing required argument(s): X`, `the following arguments are required` | the argument name | yes, when the name is known |
| `locked_resource` | `is locked by the user` | none | yes |
| `review_precondition` | `Refusing background curator patch`, `has not been loaded in this review` | none | yes |
| `edit_precondition` | `Refusing to overwrite`, "must be read before" | none | yes |
| `edit_mismatch` | `Could not find a match for old_string`, `No edit was applied` | none | yes |
| `approval_timeout` | `timed out without user response` | none | yes |
| `blocked_by_policy` | `BLOCKED:`, `flagged as dangerous`, and any `blocked` call that matches no other rule | `inline_script` for inline code execution | yes |
| `rate_limited` | `rate limit`, `too many requests`, `429`, `quota exceeded` | none | yes |
| `timeout` | `timed out`, `timeout`, `deadline exceeded` | none | yes |
| `missing_dependency` | `X: command not found`, `No module named X`, `Unable to locate package X` | the command, module or package | yes, when the name is known |
| `permission_denied` | `permission denied`, `EACCES`, `operation not permitted` | none | yes |
| `auth` | `401`, `403`, `unauthorized`, `invalid api key` | none | no |
| `backend_unavailable` | `not initialized`, `connection refused`, `is not running` | none | yes |
| `not_found` | `not found`, `no such file`, `does not exist`, `unknown tool` | `skill` or `file` | yes |
| `limit_exceeded` | `exceeds`, `too long`, `too large`, `limit of N` | none | yes |
| `invalid_argument` | `usage:`, `unrecognized arguments`, `invalid choice`, `must be one of` | none | yes |
| `code_exception` | a Python traceback, `SyntaxError` | the exception class | no |
| `network` | `connection reset`, `network is unreachable`, an SSL error | none | no |
| `command_failed` | no rule matched and the exit code is not zero | `exit_<code>` or `exit_signal` | no |
| `other` | nothing matched | none | no |

Fifteen classes have a template. `auth`, `code_exception`, `network`, `command_failed` and `other` never become a derived lesson, because no fixed sentence is safe advice for them. When one of these recurs, the queue gets an `investigate_failure` proposal.

### The detail filter

The detail is the only free value taken from an error message, so it passes a filter before it is stored (`signals._clean_hint`). It is kept only when it reads as a short name:

- it matches `[A-Za-z_][A-Za-z0-9_.+-]*` and has at most 32 characters;
- it has at most 4 digits and at least one letter;
- when longer than 8 characters, no more than 60% of its letters are upper case;
- it is not a URL and does not start with a known credential prefix;
- inside Hermes, it is left unchanged by Hermes' own secret redactor.

Anything else is dropped and the failure is stored without a detail. Tool and skill names pass a similar allowlist (`experience.label`); a name that fails it is stored as `unknown`.

### Turns

A turn is measured from `pre_llm_call` to `on_session_end`, which Hermes fires at the end of every run, including interrupted ones. Each turn is recorded per UTC day and surface.

| Surface | When |
|---|---|
| `subagent` | the payload has a parent session |
| `local` | platform `cli`, `tui`, `desktop`, `acp` or `local` |
| `cron` | platform `cron` |
| `gateway` | every other platform |

The record holds counters only: turns, completed, failed, interrupted, slow, turns with tools, tool errors, total and maximum duration, and six latency buckets (up to 15 s, 30 s, 60 s, 120 s, 300 s and above). A turn is slow when it lasts at least `slow_turn_seconds` (default 60). The p50 and p95 in the digest are the upper bound of the bucket where the percentile falls, not exact values. The tracker holds at most 512 open turns in memory and durations are capped at 4 hours.

### Owner feedback

When `feedback_signals` is on, the plugin looks at the user message at the start of a turn and stores one counter per day and kind. The message itself is not stored.

- **Whose messages count.** Local sessions always. Gateway messages only when the sender ID is listed in `owner_sender_ids`. Subagents never.
- **Correction.** The message starts with a correction marker in Portuguese or English, such as "não", "errado", "wrong" or "try again".
- **Thanks.** The message starts with a thanks marker, such as "obrigado", "thanks" or "perfect".
- **Repeated question.** The message arrives within 180 seconds of the previous one in the same session, has at least 4 distinct words and shares at least 60% of them with it. The previous message is kept in memory as a set of hashed words, for up to 256 sessions.

These are heuristics over the first words of a message. Read the counts as indicators.

### Skill events

The `on_skill_lifecycle` hook counts, per skill and day, the actions `loaded`, `patched`, `edited`, `created`, `installed`, `archived`, `stale` and `restored`, plus `reused_after_patch` when Hermes reports that a patched skill was loaded again.

## Lessons

A lesson is one sentence of the form "when X, do Y". The set of lessons is the playbook.

### When a failure becomes a lesson

A pass creates a lesson for a failure signature when all of the following hold (`lessons.derive`):

- the mode is `suggest` or `auto`;
- the failure was seen at least 3 times (`MIN_TOTAL`);
- it was seen in at least 2 sessions or on at least 2 UTC days, because one bad session is not a pattern;
- its class has a template, and the detail is present when the template needs one;
- the tool name is known;
- no lesson exists for that signature yet.

Sessions are counted when the failing session differs from the previous failing session of the same signature. The session ID is stored only as a truncated hash.

The sentence comes from a fixed template in English and Portuguese (`lessons.TEMPLATES`), filled with the tool name and the detail. No model is called. Example for `missing_argument`: "When calling `example_search`, always pass the required argument `query`."

### Modes

| `evolution.lessons` | New lessons | What the agent sees | Ineffective lessons |
|---|---|---|---|
| `off` | none are derived | nothing; the prompt section and the hint hook are not registered | a `lesson_retire` proposal |
| `suggest` (default) | created as `candidate`, each with a low-risk proposal | only lessons you approved | a `lesson_retire` proposal |
| `auto` | created as `active` | active lessons, from the next session | retired by the pass after 14 days of activity; a `lesson_retire` proposal before that |

Signals and measurement continue in every mode.

### Status and origin

| Status | Meaning |
|---|---|
| `candidate` | derived, waiting for approval |
| `quarantined` | written by the agent, waiting for approval |
| `active` | shown to the agent |
| `retired` | no longer shown; kept for 90 days |

| Origin | Created by |
|---|---|
| `derived` | a pass, from a failure signature |
| `operator` | `hermes evolution lessons add`; active at once |
| `agent` | the `atlas_evolution_lessons` tool, action `propose`; starts quarantined |
| `reflector` | reserved for a roadmap item; nothing creates it in 0.4.0 |

### Custom lessons

An operator or agent lesson is one sentence of 12 to 240 characters, without control characters and without anything that looks like a secret (`lessons.valid_text`). It names the tool it applies to, or `general`. The playbook holds at most 200 custom lessons that are not retired. The agent can propose at most 5 lessons per hour.

Custom lessons are not tied to a failure signature. They appear in the prompt section and in the tool's `search` action, but they are never attached as a hint and they get no measured effect.

### Delivery

- **Prompt section** (`atlas-evolution.playbook`). Hermes renders it at the start of each session and freezes it into that session's system prompt. It lists active lessons, the ones measured as effective first and then the ones with the most evidence, up to 8 lessons and 1,200 characters. The section is registered with a limit of 1,400 characters. Each render increments the lesson's `shown` counter.
- **Hint on the failing result** (`transform_tool_result`, when `tool_hints` is on). When a call fails with a signature that has an active lesson, the sentence is added to that result. A JSON object gains the key `atlas_evolution_lesson`; any other text gains a final line that starts with `[Atlas Evolution lesson]` (`[Lição do Atlas Evolution]` in Portuguese). Each signature is hinted once per session, with at most 6 hints per session. Results over 262,144 characters and successful calls are left untouched. The index of active lessons is reloaded at most once a minute.

Because the section is fixed for the whole session and the hint is appended to a new tool result, neither path changes a prefix the provider has already cached.

## How a lesson is measured

Each pass measures every active lesson that has a signature (`lessons.evaluate`).

| Item | Value |
|---|---|
| Before window | the 14 days before the activation day (`BASELINE_DAYS`) |
| After window | from the day after activation until today |
| Activation day | left out of both windows, because it is partly before the lesson |
| Rate | failures of the signature per call of the tool, when both windows have at least 5 calls; failures per day otherwise |
| First verdict | 5 days after activation or 20 calls of the tool after it, whichever comes first, and never on the activation day |

| Verdict | Condition |
|---|---|
| `effective` | the rate after is at most half the rate before |
| `ineffective` | at least 3 failures after activation and a rate after of at least 90% of the rate before |
| `inconclusive` | anything else, including no failure in either window |

The verdict is recomputed at each pass while the lesson is active. In `auto` mode a lesson that is `ineffective` 14 days after activation is retired (`RETIRE_AFTER_DAYS`). In the other cases an `ineffective` verdict opens a `lesson_retire` proposal.

This is a comparison of two periods for one failure. It does not control for changes in how the tool was used.

## Changes and experiments

### What counts as a change

| Kind | Source | Granularity |
|---|---|---|
| `config` | Hermes configuration, compared with the snapshot of the previous pass | one change per key and UTC day |
| `skill` | the Curator ledger: `create`, `patch`, `edit`, `write_file`, `remove_file` | one change per skill and UTC day, with the number of rewrites |
| `lesson` | activation of a lesson | recorded so that it counts as a concurrent change; its effect is measured as described above |

Only these configuration keys are watched (`experiments.CONFIG_KEYS`): `compression.enabled`, `compression.threshold_tokens`, `compression.proactive_prune_tokens`, `model.default`, `model.provider`, `memory.provider`, `memory.nudge_interval`, `skills.creation_nudge_interval`, `auxiliary.background_review.enabled`, `auxiliary.background_review.model`, `auxiliary.compression.model`, `tool_budget.mcp_result_size_chars`, `curator.enabled`, `curator.consolidate`, and the list of enabled plugins as a count plus a short hash. Values are stored as short labels; a value that is not a plain label is stored as `set`.

The first pass only records the snapshot. Configuration is available to `hermes evolution` commands. The standalone runner sees it only when PyYAML is installed, and tracks no configuration change without it.

### Windows and metrics

A change is compared across the 7 days before and the 7 days after it, leaving out the day of the change (`WINDOW_DAYS`). The first, provisional verdict comes 3 days after the change. It is recomputed at each pass and becomes final 8 days after the change.

| Metric | Change that matters | Minimum sample in each window |
|---|---|---|
| `turn_failed_pct` | 2 percentage points | 20 turns |
| `turn_slow_pct` | 5 percentage points | 20 turns |
| `turn_mean_ms` | 3,000 ms | 20 turns |
| `owner_corrections_per_100_turns` | 2 | 20 turns |
| `tool_error_pct` | 2 percentage points | 40 tool calls |
| `uncached_input_tokens_per_call` | 300 tokens | 30 API calls |

Turn metrics use interactive surfaces only (`local` and `gateway`). Owner corrections include repeated questions. A metric is worse when it rose by at least the amount above and by at least 25% in relative terms. It is better when it fell by at least that amount and by at least 20%. The relative change is computed against the larger of the old value and the amount above, so small bases do not produce large ratios.

| Verdict | Condition |
|---|---|
| `insufficient_data` | no metric had the minimum sample in both windows |
| `regressed` | at least one metric worse, none better |
| `improved` | at least one metric better, none worse |
| `mixed` | some better and some worse |
| `neutral` | measured, with no material change |

Each verdict stores the samples behind it, the number of other changes within 7 days of this one (`concurrent_changes`) and the method label `same_metrics_week_before_vs_week_after_correlational`. A verdict says the metrics moved after the change. It does not say the change caused it.

`hermes evolution experiments` lists the changes with their verdicts.

## Skill health

Skill health reads two Hermes files and nothing else (`skill_health`): `skills/.usage.json` for counters and `skills/.curator_ledger.jsonl` for the history of mutations. Skill bodies are never opened. From the ledger it keeps the entry ID, timestamp, skill name, action, actor and the content hashes before and after.

| Finding | Condition | Proposal |
|---|---|---|
| `skill_churn` | 8 or more rewrites in the window, or 2 or more flip-flops | `skill_lock`, medium risk |
| `skill_unexercised_rewrites` | 5 or more rewrites since the skill was last reused after a patch | `skill_review`, low risk |
| `skill_never_used` | created by the agent, active, never used and at least 21 days old | `skill_archive`, low risk |

A flip-flop is a rewrite that returns a file to a hash it already had earlier in the window (A, then B, then A). Pinned and locked skills produce no finding. `hermes evolution skills` prints the same report for 1 to 35 days.

## Learning yield

Hermes 0.23.1 has no hook for a finished background review, so the yield comes from the completion line it writes to `logs/agent.log` (`logwatch`). From each line the plugin keeps the day, the number of model calls, input, output and cached tokens, and the result label: `none`, `skill`, `memory`, `skill+memory` or `error`. The rest of the line is not stored.

A cursor (inode and offset) keeps a line from being counted twice. On the first run, or after rotation, reading starts within the last 4 MiB of the file, so old history is not attributed to today. A pass reads at most 32 MiB.

The digest shows how many reviews ran, the share that produced nothing and the tokens they used. When the plugin also observed the main model calls, it adds the share of uncached input and output tokens that went to reviews. That share is approximate.

## Proposals

Everything the loop wants changed goes through one queue (`proposals`). Each proposal has its evidence, a risk level and one command.

| Kind | Risk | Opened when | Command | On approval |
|---|---|---|---|---|
| `lesson` | low | a lesson is `candidate` or `quarantined` | `hermes evolution lessons approve <id>` | the plugin activates the lesson |
| `lesson_retire` | low | an active lesson is measured `ineffective` | `hermes evolution lessons retire <id>` | the plugin retires the lesson |
| `skill_lock` | medium | `skill_churn` | `hermes curator lock <skill>` | you run it |
| `skill_review` | low | `skill_unexercised_rewrites` | `hermes curator ledger --skill <skill>` | you run it |
| `skill_archive` | low | `skill_never_used` | `hermes curator archive <skill>` | you run it |
| `review_interval` | medium | at least 10 background reviews in the last 7 days and at least half produced nothing | `hermes config set skills.creation_nudge_interval 20` | you run it |
| `investigate_failure` | low | a failure without a template seen at least 5 times on at least 2 of the last 7 days | `hermes evolution lessons add --tool <tool> "<what to do instead>"` | you write the lesson |
| `investigate_latency` | medium | at least 20 interactive turns in 7 days and at least 30% of them slow | `hermes evolution report --days 7` | you investigate |
| `skill_rollback` | high | a skill change with verdict `regressed` | `hermes curator rollback <entry-id>` | you run it |
| `config_review` | high | a configuration change with verdict `regressed` | `hermes config get <key>` | you review the setting |

The plugin applies only the two lesson kinds. For every other kind, `proposals approve` marks the proposal as approved and returns the command in the field `run_this`. Nothing is executed for you.

### Lifecycle

- `open`: waiting for a decision. Evidence is refreshed at each pass.
- `applied`: a lesson proposal that was approved, or a `skill_lock` proposal whose skill a later pass found locked or pinned.
- `approved`: a native proposal you approved. Only `skill_lock` is closed automatically afterwards; the others stay approved as a record of the decision.
- `rejected`: declined. Rejecting a `lesson` proposal also retires the lesson. A rejected proposal can reopen after 30 days if the evidence is still there.
- `expired`: the evidence was gone at a later pass. It reopens if the evidence returns.

`proposals list` sorts by risk, highest first. `proposals approve` and `proposals reject` accept `--note` with up to 200 characters.

### Known behaviour of regression proposals in 0.4.0

A `skill_rollback` or `config_review` proposal is rebuilt from the verdicts computed in the current pass. A change is evaluated from day 3 until its verdict becomes final on day 8, and it is not evaluated again after that. An open regression proposal therefore expires at the first pass after the verdict becomes final. Decide it while the change is under evaluation. The verdict itself remains visible in `hermes evolution experiments` and in the digest.

## Digest

`hermes evolution report` builds the digest for the last 1 to 35 days (default 7) and compares it with the same span before it.

| Section | Content |
|---|---|
| Summary | interactive turns, p50 and p95, tool calls and errors, estimated cost from Hermes' accounting when available, owner signals |
| Versus the previous period | the six metrics above, with a verdict |
| Failures that repeat | up to 8 signatures, with count, days and lesson status |
| Self-improvement loop | background reviews, share that produced nothing, tokens, skill rewrites and the most rewritten skills |
| Playbook | counts by status and up to 8 active lessons with their measured effect |
| Changes and their measured effect | up to 8 changes that have a verdict |
| Proposals awaiting a decision | up to 12 open proposals with their command |

`--format json` returns the full structure, which also has cron and subagent turns, per-tool totals and skill events. `--lang en` or `--lang pt` overrides the configured language. Where a number could not be measured the digest says "not measured".

`report --schedule-spec` prints a recipe: a script and the arguments of a native `hermes cron create` in no-agent mode, by default on weekday 1 at hour 8 with local delivery (`--weekday`, `--hour`, `--deliver`, `--days` and `--lang` change it; weekday 0 is Sunday). The script runs `learn --notify-only` and then `report`. The command writes no file and creates no job.

## Agent tools and the bundled skill

Both tools belong to the toolset `atlas-evolution` and are available only when `agent_advice` is `true` in the settings.

| Tool | Actions |
|---|---|
| `atlas_evolution_lessons` | `search`: up to 5 active lessons for a tool, `general` ones included. `propose`: stores a quarantined lesson for operator review. `status`: lesson counts by status and up to 20 open proposals. |
| `atlas_evolution_plan` | returns a bounded plan for research, delegation or operational diagnosis. It is described in [Operations](operations.md). |

The skill `hermes-evolution:evolution-review` is registered with the plugin. It tells the agent how to run the review with the owner: read the digest, explain failures and lessons, walk through the open proposals from the highest risk down and apply only what the owner approved. Its rules forbid approving on the owner's behalf.

## Storage

The loop's tables live in `<HERMES_HOME>/evolution/evolution.sqlite3`, next to the tables of the operations observer. Version 0.4.0 migrates that database to schema 4 automatically. Version 0.3.0 cannot open a migrated database.

| Table | One row per | Holds |
|---|---|---|
| `tool_daily` | day and tool | calls, errors, blocked calls, total duration |
| `failure` | signature | tool, class, detail, first and last time seen, total, sessions, hash of the last failing session |
| `failure_daily` | day and signature | count |
| `turn_daily` | day and surface | turn counters and latency buckets |
| `feedback_daily` | day and kind | count of corrections, repeated questions or thanks |
| `skill_daily` | day, skill and action | count |
| `review_daily` | day and result | reviews, model calls, input, output and cached tokens |
| `lesson` | lesson | signature, tool, class, detail, origin, status, custom text, timestamps, times shown, hints given, verdict, rate before and after |
| `change` | tracked change | kind, subject, detail, time, verdict, measured metrics |
| `proposal` | proposal | kind, risk, subject, evidence, command, status, note |
| `cursor` | key | position in `agent.log`, last configuration snapshot, last ledger time, last scheduled pass |

Daily aggregates, changes, decided proposals and retired lessons are kept for 90 days (`experience.RETENTION_DAYS`). Lessons that are not retired and proposals that are open or approved are kept. Retention is applied by each pass and by the first hook write of each UTC day.

Hook writes share one connection per process and profile. While the database is busy an update waits in a queue of up to 512 entries; overflow is counted and reported as `dropped_events` by `hermes evolution usage`.
