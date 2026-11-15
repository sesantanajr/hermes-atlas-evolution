# The evolution loop

**English** | [Português (Brasil)](pt-BR/evolution-loop.md)

This document describes the loop that Atlas Evolution 0.4.0 adds to Hermes Agent: what is recorded, when a failure becomes a lesson, how lessons, skills and changes are measured, what the optional reflector does and what ends up in the proposal queue. Every threshold below is a constant in the code, and the module that holds it is named next to it. For installation and a summary, see the [README](../README.md).

## Contents

1. [One pass](#one-pass)
2. [Signals](#signals)
3. [Lessons](#lessons)
4. [The reflector](#the-reflector)
5. [How a lesson is measured](#how-a-lesson-is-measured)
6. [Skills by outcome](#skills-by-outcome)
7. [Changes and experiments](#changes-and-experiments)
8. [Skill health](#skill-health)
9. [Learning yield](#learning-yield)
10. [Proposals](#proposals)
11. [Digest](#digest)
12. [Settings and language](#settings-and-language)
13. [Agent tools and the bundled skill](#agent-tools-and-the-bundled-skill)
14. [Storage](#storage)

## One pass

Hooks record signals while the agent works. Everything else happens in a pass (`evolve.cycle`), which reads Hermes' own records, updates the plugin's tables and never changes Hermes. A pass runs when:

- you run `hermes evolution learn`;
- `hermes evolution ops tick` runs, at most once an hour (`CYCLE_INTERVAL_SECONDS`; the result is the `evolution` field of its output);
- the weekly digest recipe runs, which calls `learn --notify-only` before `report`.

Nothing schedules a pass by itself. Without one of the three, signals accumulate but no lesson is derived and nothing is measured.

A pass does the following, in order:

1. Drops aggregates older than the retention window.
2. Counts the background reviews logged since the previous pass.
3. Derives lessons from recurring failures.
4. Asks the reflector for drafts when it is on and Hermes' model access is available; otherwise deletes any waiting error excerpt.
5. Measures each active lesson that is bound to a failure.
6. Computes skill health for the last 7 days.
7. Detects configuration changes and skill rewrites.
8. Compares the week before and the week after each tracked change.
9. Closes approved `skill_lock` proposals whose skill is now locked or pinned.
10. Compares the skills loaded in the last 14 days with the profile as a whole.
11. Rebuilds the open proposals from the current evidence.

With `--notify-only`, `learn` prints only new lessons, new reflector drafts, regressions whose verdict became final in this pass, lessons retired in this pass and high-risk proposals opened in this pass. It prints nothing when there are none.

## Signals

### Tool outcomes

The `post_tool_call` hook counts every call that ends with status `ok`, `error` or `blocked`, per tool and UTC day: calls, errors, blocked calls and total duration. Other statuses, such as `timeout` or `cancelled`, are counted by the operations observer and do not enter the loop. Hermes wraps deferred tools in `tool_call`; a wrapper that carries a single call is attributed to the tool that ran, and an unresolved wrapper takes the tool name from Hermes' own error text.

A call with status `error` or `blocked` is classified (`signals.classify`). The classifier first reads the text that Hermes or the tool wrote about the error: the error message (first 2,000 characters) and, when the result is a JSON object, its `error`, `hint`, `approval` and `message` fields. Only when that text matches no rule does it read the command output: the last 1,500 characters of the `output` field, or the first 1,500 characters of a result that is not JSON. It also reads `exit_code`. Results over 262,144 characters are not parsed as JSON. The text is classified in memory and discarded.

The outcome is a signature: the first 20 hexadecimal characters of the SHA-256 of tool, class and detail. Two failures with the same tool, class and detail are the same failure.

### Failure classes

Rules are tried in the order of the table and the first match names the class (`signals._RULES`).

| Class | Matches, for example | Detail kept | Lesson template |
|---|---|---|---|
| `missing_argument` | `missing required argument(s): X`, `the following arguments are required` | the argument name | yes, when the name is known |
| `approval_refused` | `User denied`, `has NOT consented`, `Silence is not consent`, `timed out without user response`, `Failed to send approval request`, `nobody can approve` | none | never |
| `locked_resource` | `is locked by the user`, `locked by operator` | none | yes |
| `review_precondition` | `Refusing background curator patch`, `has not been loaded in this review` | none | yes |
| `edit_precondition` | `Refusing to overwrite`, `has not seen its`, "must be read before" | none | yes |
| `edit_mismatch` | `Could not find a match for old_string`, `No edit was applied` | none | yes |
| `blocked_by_policy` | `BLOCKED:`, `flagged as dangerous`, `blocked by policy`, and any `blocked` call that matches no other rule | `inline_script` for inline code execution | yes |
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

Fourteen classes have a template (`lessons.TEMPLATES`).

- **`approval_refused`** covers Hermes' approval gate: the user denied, nobody answered, no approver could be reached, or the context cannot approve. Hermes already tells the agent not to reach the same outcome another way, so this class never becomes a lesson of any kind: no template, no reflector draft and no `investigate_failure` proposal. The digest marks it as "never a lesson: the user's approval decides".
- **`auth`, `code_exception`, `network`, `command_failed` and `other`** have no template because no fixed sentence is safe advice for them. When one of them recurs, the reflector can draft a lesson (if it is on), and the queue gets an `investigate_failure` proposal.
- **`blocked_by_policy`** has a template that tells the agent not to retry, not to look for another way to the same result, and to tell the user what it needed.

### The detail filter

The detail is the only free value taken from an error message, so it passes a filter before it is stored (`signals._clean_hint`). It is kept only when it reads as a short name:

- it matches `[A-Za-z_][A-Za-z0-9_.+-]*` and has at most 32 characters;
- it has at most 4 digits and at least one letter;
- when longer than 8 characters, no more than 60% of its letters are upper case;
- it is not a URL and does not start with a known credential prefix;
- inside Hermes, it is left unchanged by Hermes' own secret redactor, which runs with `force=True` so that it applies even when the profile turned redaction off.

A detail read from command output, which anyone can write, must also be a lower-case command name (`[a-z_][a-z0-9_+-]*`) or, for a missing module, a lower-case dotted module name. Anything else is dropped and the failure is stored without a detail. Tool and skill names pass a similar allowlist (`experience.label`); a name that fails it is stored as `unknown`.

### Turns

A turn is measured from `pre_llm_call` to `on_session_end`, which Hermes fires at the end of every run, including interrupted ones. Each turn is recorded per UTC day and surface.

| Surface | When |
|---|---|
| `subagent` | the payload has a parent session |
| `local` | platform `cli`, `tui`, `desktop`, `acp` or `local` |
| `cron` | platform `cron` |
| `gateway` | every other platform |

The record holds counters only: turns, completed, failed, interrupted, slow, turns with tools, tool errors, total and maximum duration, and six latency buckets (up to 15 s, 30 s, 60 s, 120 s, 300 s and above). A turn is slow when it lasts at least `slow_turn_seconds` (default 60). The p50 and p95 in the digest are the upper bound of the bucket where the percentile falls, not exact values. The tracker holds at most 512 open turns in memory, forgets a turn open for more than 6 hours and caps durations at 4 hours.

### Owner feedback

When `feedback_signals` is on, the plugin looks at the user message at the start of a turn and stores one counter per day and kind. The message itself is not stored.

- **Whose messages count.** Local sessions always. Gateway messages only when the sender ID is listed in `owner_sender_ids`. Subagents never.
- **Correction.** The message starts with a correction marker in Portuguese or English, such as "não," or "não." (with punctuation), "não era isso", "errado", "wrong" or "try again".
- **Thanks.** The message starts with a thanks marker, such as "obrigado", "thanks" or "perfect".
- **Repeated question.** The message arrives within 180 seconds of the previous one in the same session, has at least 4 distinct words and shares at least 60% of them with it. The previous message is kept in memory as a set of hashed words, for up to 256 sessions.

A correction or a repeated question is also charged to the skills of the turn being corrected (see [Skills by outcome](#skills-by-outcome)). These are heuristics over the first words of a message. Read the counts as indicators.

### Skill events

The `on_skill_lifecycle` hook counts, per skill and day, the actions `loaded`, `patched`, `edited`, `created`, `installed`, `archived`, `stale` and `restored`, plus `reused_after_patch` when Hermes reports that a patched skill was loaded again. A `loaded` event is also attached to the open turn of the same session or task, for skills by outcome.

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

The sentence comes from a fixed template in English and Portuguese (`lessons.TEMPLATES`), filled with the tool name and the detail, and rendered in the configured language each time it is read. No model is called. Example for `missing_argument`: "When calling `example_search`, always pass the required argument `query`."

### Modes

| `lessons` | New derived lessons | What the agent sees | Ineffective lessons |
|---|---|---|---|
| `off` | none are derived | nothing; the prompt section and the hint hook are not registered | a `lesson_retire` proposal |
| `suggest` (default) | created as `candidate`, each with a low-risk proposal | only lessons you approved | a `lesson_retire` proposal |
| `auto` | created as `active` | active lessons, from the next session | retired by the pass 14 days after activation; a `lesson_retire` proposal before that |

Signals and measurement continue in every mode. In every mode, lessons written by you without `--activate`, proposed by the agent or drafted by the reflector wait for approval.

### Status and origin

| Status | Meaning |
|---|---|
| `candidate` | derived, waiting for approval |
| `quarantined` | written by the operator without `--activate`, proposed by the agent or drafted by the reflector; waiting for approval |
| `active` | shown to the agent |
| `retired` | no longer shown; deleted 90 days after its last update |

| Origin | Created by | Bound to a failure | Proposal risk |
|---|---|---|---|
| `derived` | a pass, from a failure signature | yes | low |
| `operator` | `hermes evolution lessons add` | no | low |
| `reflector` | a pass with the reflector on | yes | medium |
| `agent` | the `atlas_evolution_lessons` tool, action `propose` | no | high |

### Text rules

Lesson text that is not a template, from you, the agent or the reflector, goes through `lessons.clean_text`:

- it is normalized to Unicode NFKC and its whitespace is collapsed;
- control, format (bidirectional and zero-width), private-use and surrogate characters and line or paragraph separators are refused, not stripped, so what you read is exactly what the agent gets;
- it has 12 to 240 characters;
- it must not contain a value shaped like a credential (`sk-`, `ghp_`, `gho_`, `github_pat_`, `xox?-`, `AKIA`, `AIza` followed by at least 8 characters) or a URL with a user and password.

A lesson names the tool it applies to, or `general`. The playbook holds at most 200 lessons that are not derived and not retired. The agent can propose at most 5 lessons per hour (`agent_tools.PROPOSALS_PER_HOUR`).

### Delivery

- **Prompt section** (`atlas-evolution.playbook`). Hermes renders it at the start of each session and freezes it into that session's system prompt. It lists active lessons of any origin, those measured as effective first and then those with the most evidence, up to 8 lessons and 1,200 characters, under a one-line header. The section is registered with a limit of 1,400 characters. It only reads the database and counts each display (`shown`) through the hook write queue, so a pass running in another process does not leave a new session without its lessons.
- **Hint on the failing result** (`transform_tool_result`, when `tool_hints` is on). When a call with status `error` or `blocked` fails with a signature that has an active lesson, the sentence is added to that result. A JSON object gains the key `atlas_evolution_lesson`; any other text gains a final line that starts with `[Atlas Evolution lesson]` (`[Lição do Atlas Evolution]` in Portuguese). Each signature is hinted once per session, with at most 6 hints per session (`hints.MAX_HINTS_PER_SESSION`), and only when the call has a session ID. Results over 262,144 characters and successful calls are left untouched. Only lessons bound to a failure (derived lessons and reflector drafts) can be hinted. The index of active lessons is reloaded at most once a minute, so a lesson approved during a session can be hinted in that session.

Because the section is fixed for the whole session and the hint is appended to a new tool result, neither path changes a prefix the provider has already cached.

## The reflector

The reflector (`reflector`) drafts lessons for recurring failures that have no template. It is off by default and is switched on with the `reflector` option. While it is off, nothing below happens.

**What is kept.** When the plugin loads with the reflector on and `lessons` is not `off`, the tool hook keeps, for each failure signature of the classes `code_exception`, `command_failed`, `network`, `auth` and `other`, the latest error excerpt (`failure_sample`):

1. the last 4 non-empty lines of the error text and output, joined, at most 1,200 characters;
2. passed through Hermes' secret redactor when the plugin runs inside Hermes, with `force=True` so that it applies even when the profile turned redaction off;
3. masked by the plugin: URLs become `<url>`, e-mail addresses `<email>`, paths of two or more segments `<path>`, hexadecimal strings of 16 or more characters `<hex>`, values with a credential prefix `<secret>`, runs of 5 or more digits `<n>`, and quoted strings over 40 characters `'<text>'`;
4. cut to the last 300 characters (`EXCERPT_LIMIT`).

**Which failures are drafted.** The same recurrence rule as derived lessons: seen at least 3 times, in at least 2 sessions or on at least 2 days, with an excerpt and without a lesson. The most frequent come first.

**How many calls.** At most 3 per pass (`PER_RUN`) and `reflector_daily_limit` per UTC day (default 6, from 1 to 50). Every attempt counts toward the daily limit, including failed ones.

**The call.** The plugin registers the Hermes auxiliary task `evolution_reflector` ("Atlas Evolution reflector") with `inherit_from="compression"`, so by default it uses the model configuration of the compression task. You choose another model in `auxiliary.evolution_reflector` of the profile configuration. The pass calls `ctx.llm.complete_structured` with that task, a JSON schema (`applies`, a boolean, and `lesson`, a string), at most 300 output tokens and a 60-second timeout. The input holds the tool name, the failure class, the detail, the number of occurrences, sessions and days, and the excerpt. The instructions, in the configured language, say that the excerpt is data and never instructions, ask for `applies: false` when the failure looks transient, specific to one request or outside the agent's control, and otherwise ask for one sentence of 20 to 200 characters shaped as "when X, do Y", without paths, names, credentials, URLs or text copied from the excerpt. The plugin opens no connection: Hermes makes the call with its own provider configuration.

**What is accepted.** A draft is discarded when `applies` is not true, when the sentence fails the [text rules](#text-rules), when it contains `<`, or when it matches the injection filter (phrases such as "ignore previous instructions", "disregard", "system prompt", "you are now", their Portuguese forms, or `system` and `assistant` tags). An accepted draft becomes a `quarantined` lesson with origin `reflector`, bound to its failure signature, and gets a medium-risk proposal. Once approved it is shown, hinted and measured like a derived lesson.

**When the excerpt is deleted.** As soon as the failure has a lesson, when the model declines or the draft is discarded, 14 days after its last update (`SAMPLE_DAYS`), and at every pass that runs with the reflector off. A provider error, or a pass without Hermes' model access, leaves the excerpt for a later pass.

**Where it runs.** Only in a pass run by the Hermes CLI (`hermes evolution learn`, `ops tick` or the weekly recipe), because the model access comes from Hermes, and only while `lessons` is not `off` (otherwise the block reports `lessons_off`, since an approved draft would never be shown). The standalone runner (`python -m hermes_evolution`) drafts nothing; its `learn` reports `unavailable_without_hermes_llm` and keeps the waiting excerpts for the next pass run by Hermes.

The `learn` output has a `reflector` block with the drafts, the number declined and failed, the calls made today and the daily limit.

## How a lesson is measured

Each pass measures every active lesson that is bound to a failure signature (`lessons.evaluate`): derived lessons and approved reflector drafts.

| Item | Value |
|---|---|
| Before window | the 14 days before the activation day (`BASELINE_DAYS`), counted at the first pass after activation and then frozen, so retention never changes it |
| After window | the latest 14 days since activation (`AFTER_WINDOW_DAYS`): from the day after activation, or from 13 days before today when activation is older |
| Activation day | left out of both windows, because it is partly before the lesson |
| Rate | failures of the signature per call of the tool, when both windows have at least 5 calls; otherwise failures per calendar day of each window |
| First verdict | 5 days after activation or 20 calls of the tool after it, whichever comes first, and never on the activation day |

| Verdict | Condition |
|---|---|
| `effective` | the rate after is at most half the rate before |
| `ineffective` | at least 3 failures in the after window and a rate after of at least 90% of the rate before |
| `inconclusive` | anything else, including no failure in either window |

The verdict is recomputed at each pass while the lesson is active. Activating a lesson again starts a new measurement with a new baseline. In `auto` mode a lesson that is `ineffective` 14 days or more after activation is retired (`RETIRE_AFTER_DAYS`). In the other cases an `ineffective` verdict opens a `lesson_retire` proposal.

This is a comparison of two periods for one failure. It does not control for changes in how the tool was used.

## Skills by outcome

Hermes reports each skill it loads through `on_skill_lifecycle`. The plugin attaches a `loaded` skill to the open turn of the same session or task, up to 16 skills per turn (`signals.MAX_SKILLS_PER_TURN`), and charges each of them with how the turn ended (`skill_outcome_daily`):

- the turn failed, was interrupted or was slow;
- the turn had at least one tool error;
- the owner corrected it: the owner's next message in the same session, within 30 minutes of the end of the turn (`CORRECTION_WINDOW_SECONDS`), was a correction or a repeated question. This needs `feedback_signals`.

Only interactive turns are attributed, those on the local and gateway surfaces, which are also the turns of the profile the skill is compared with. Cron and subagent turns are not attributed.

**Compared with the profile** (`skill_health.skill_outcomes`). The profile baseline is every interactive turn (local and gateway) in the window: the share that failed, was interrupted or was slow, and corrections plus repeated questions per turn. A skill with at least 8 turns (`MIN_SKILL_TURNS`) is flagged `skill_underperforming` when, for the failed, interrupted or corrected share, its rate exceeds the profile's by at least the margin below and is at least 1.5 times the larger of the profile rate and 1%.

| Rate | Margin |
|---|---|
| failed | 5 percentage points |
| interrupted | 10 percentage points |
| corrected | 5 percentage points |

A pass compares the last 14 days and opens a low-risk `skill_outcome` proposal for each flagged skill, with the command `hermes curator ledger --skill <skill>` as the next step. `hermes evolution skills --days N` returns the same comparison for its window in the `outcomes` block, and the digest shows it as "Skills by outcome".

**A skill rewrite judged by its own turns** (`skill_health.compare_skill_windows`). When the Curator ledger shows a rewrite, the change is judged by the turns that loaded that skill in the week before and the week after, not by the whole profile. Each window needs at least 5 such turns. A rate is worse when it rose by at least its margin and to at least 1.25 times its old value, and better when it fell by at least its margin and to at most 0.8 times its old value.

| Rate | Margin |
|---|---|
| failed | 5 percentage points |
| interrupted | 10 percentage points |
| with a tool error | 10 percentage points |
| corrected | 5 percentage points |
| slow | 15 percentage points |

A `regressed` verdict opens a high-risk `skill_rollback` proposal with the native command `hermes curator rollback <entry-id>`, where the entry is the last ledger entry of that skill on the day of the change.

## Changes and experiments

### What counts as a change

| Kind | Source | Granularity |
|---|---|---|
| `config` | Hermes configuration, compared with the snapshot of the previous pass | one change per key and UTC day |
| `skill` | the Curator ledger: `create`, `patch`, `edit`, `write_file`, `remove_file` | one change per skill and UTC day, with the number of rewrites |
| `lesson` | activation of a lesson | recorded so that it counts as a concurrent change; its effect is measured as described above |

Only these configuration keys are watched (`experiments.CONFIG_KEYS`): `compression.enabled`, `compression.threshold_tokens`, `compression.proactive_prune_tokens`, `model.default`, `model.model`, `model.provider`, `memory.provider`, `memory.nudge_interval`, `skills.creation_nudge_interval`, `auxiliary.background_review.enabled`, `auxiliary.background_review.model`, `auxiliary.compression.model`, `tool_budget.mcp_result_size_chars`, `curator.enabled`, `curator.consolidate`, and the list of enabled plugins as a count plus a short hash. A value that is a plain label is stored as it is. Any other value (a `provider/model` name, a URL, a mapping) is stored as `set:` and a 10-character fingerprint, never the value, so a change between two such values is still detected.

The first pass only records the snapshot. Configuration is available to `hermes evolution` commands. The standalone runner sees it only when PyYAML is installed, and tracks no configuration change without it.

### Windows and metrics

A change is compared across the 7 days before and the 7 days after it, leaving out the day of the change (`WINDOW_DAYS`). The first, provisional verdict comes 3 days after the change. It is recomputed at each pass and becomes final 8 days after the change.

A skill rewrite uses the turns that loaded the skill, as described in [Skills by outcome](#skills-by-outcome). A configuration change uses the metrics of the whole profile:

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

Each verdict stores the samples behind it, the number of other changes within 7 days of this one (`concurrent_changes`) and a method label: `same_metrics_week_before_vs_week_after_correlational` for configuration and `turns_that_loaded_the_skill_week_before_vs_week_after` for skills. A verdict says the metrics moved after the change. It does not say the change caused it.

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

Hermes 0.23.1 has no hook for a finished background review, so the yield comes from the completion line that the `agent.background_review` logger writes to `logs/agent.log` (`logwatch`). From each line the plugin keeps the day, the number of model calls, input, output and cached tokens, and the result label: `none`, `skill`, `memory`, `skill+memory` or `error`. The rest of the line is not stored. Lines are counted in the local days of the log, because Hermes stamps them in local time.

A cursor (inode and offset) keeps a line from being counted twice. On the first run, or after rotation, reading starts within the last 4 MiB of the file, so old history is not attributed to today. A pass reads at most 32 MiB.

The digest shows how many reviews ran, the share that produced nothing and the tokens they used. When the plugin also observed the main model calls, it adds the share of uncached input and output tokens that went to reviews. That share is approximate.

## Proposals

Everything the loop wants changed goes through one queue (`proposals`). Each proposal has its evidence, a risk level and one command.

| Kind | Risk | Opened when | Command | On approval |
|---|---|---|---|---|
| `lesson` | by origin: derived or operator low, reflector medium, agent high | a lesson is `candidate` or `quarantined`; the evidence carries the exact sentence | `hermes evolution lessons approve <id>` | the plugin activates the lesson |
| `lesson_retire` | low | an active lesson is measured `ineffective` | `hermes evolution lessons retire <id>` | the plugin retires the lesson |
| `skill_lock` | medium | `skill_churn` | `hermes curator lock <skill>` | you run it |
| `skill_review` | low | `skill_unexercised_rewrites` | `hermes curator ledger --skill <skill>` | you review the ledger |
| `skill_archive` | low | `skill_never_used` | `hermes curator archive <skill>` | you run it |
| `skill_outcome` | low | a skill's turns go worse than the profile | `hermes curator ledger --skill <skill>` | you review the skill |
| `review_interval` | medium | at least 10 background reviews in the last 7 days and at least half produced nothing | `hermes config set skills.creation_nudge_interval 20` | you run it |
| `investigate_failure` | low | a failure of `auth`, `code_exception`, `network`, `command_failed` or `other` seen at least 5 times on at least 2 of the last 7 days | `hermes evolution lessons add --tool <tool> "<what to do instead>"` | you write the lesson |
| `investigate_latency` | medium | at least 20 interactive turns in 7 days and at least 30% of them slow | `hermes evolution report --days 7` | you investigate |
| `skill_rollback` | high | a skill change with verdict `regressed` | `hermes curator rollback <entry-id>` | you run it |
| `config_review` | high | a configuration change with verdict `regressed` | `hermes config get <key>` | you review the setting |

The plugin applies only the two lesson kinds. For every other kind, `proposals approve` marks the proposal as approved and returns the command in the field `run_this`. Nothing is executed for you.

### Who can decide

`proposals approve`, `proposals reject` and the `lessons` subcommands `approve`, `retire` and `add` refuse to run when the environment carries an agent marker (`cli.AGENT_MARKERS`): `HERMES_SESSION_ID`, `HERMES_KANBAN_TASK` or `HERMES_DELEGATED_CHILD_CONTEXT`, set by Hermes for agent work, or `ATLAS_EVOLUTION_AGENT`, which the plugin adds to every subprocess of an agent session through `register_session_env`. Run them in your own terminal.

Activating something shows the text first and asks `[y/N]`: `lessons approve` and `lessons add --activate` show the lesson, and `proposals approve` shows the lesson of a lesson proposal or the command of any other. Without an interactive terminal the command requires `--yes`. `lessons retire` and `proposals reject` do not ask.

### Lifecycle

- `open`: waiting for a decision. Evidence is refreshed at each pass.
- `applied`: an approved lesson or `lesson_retire` proposal, the same proposals settled directly by `lessons approve` or `lessons retire`, or a `skill_lock` proposal whose skill a later pass found locked or pinned.
- `approved`: a native proposal you approved. Only `skill_lock` is closed automatically afterwards; the others stay approved as a record of the decision.
- `rejected`: declined. Rejecting a `lesson` proposal also retires the lesson, and `lessons retire` marks the lesson's open proposal as rejected. A rejected proposal reopens after 30 days if the evidence is still there.
- `expired`: the evidence was gone at a later pass. It reopens if the evidence returns.

`proposals list` sorts by risk, highest first. `proposals approve` and `proposals reject` accept `--note` with up to 200 characters.

### Regression proposals

`skill_rollback` and `config_review` are built from the stored verdicts of the changes of the last 30 days (`REGRESSION_DAYS`), provisional or final. The proposal stays open while the verdict stays `regressed` and the change is less than 30 days old; its evidence says whether the verdict is final. `learn --notify-only` prints a high-risk proposal once, in the pass that opens it, and a regression once, in the pass where its verdict becomes final.

## Digest

`hermes evolution report` builds the digest for the last 1 to 35 days (default 7) and compares it with the same span before it.

| Section | Content |
|---|---|
| Summary | interactive turns, p50 and p95, tool calls and errors, estimated cost from Hermes' accounting when available, owner signals |
| Versus the previous period | the six profile metrics above, with a verdict |
| Failures that repeat | up to 8 signatures, with count, days and lesson status; `approval_refused` is marked as never a lesson |
| Self-improvement loop | background reviews, share that produced nothing, tokens, share of tokens that went to learning, skill rewrites and the most rewritten skills |
| Skills by outcome | the whole profile and up to 8 skills with at least 8 turns, marking those worse than the profile |
| Playbook | counts by status and up to 8 active lessons with their measured effect |
| Changes and their measured effect | up to 8 changes that have a verdict |
| Proposals awaiting a decision | up to 12 open proposals with their command |

`--format json` returns the full structure, which also has cron and subagent turns, per-tool totals, skill events, the skill outcome block and the usage observed by the hooks. Where a number could not be measured the digest says "not measured".

`report --schedule-spec` prints a recipe: a script and the arguments of a native `hermes cron create` in no-agent mode, by default on weekday 1 at hour 8 with local delivery (`--weekday`, `--hour`, `--deliver`, `--days` and `--lang` change it; weekday 0 is Sunday). The script runs `learn --notify-only` and then `report`. The command writes no file and creates no job.

## Settings and language

The options of the loop can be set in two places (`cli.effective_settings`):

1. the Hermes settings form, declared by `config_schema` in `plugin.yaml` and stored by Hermes under `plugins.entries.hermes-evolution.settings`;
2. the `evolution` block of `<HERMES_HOME>/evolution/settings.json` (`agent_tools` corresponds to the top-level key `agent_advice` there).

Hermes wins key by key. The plugin reads the settings when it loads; the commands read them each time they run.

Validation differs by caller (`evolve.options`). The commands reject an invalid `lessons`, `slow_turn_seconds`, `owner_sender_ids` or `reflector_daily_limit` with an error. Inside the gateway each invalid value falls back to its default and a warning is logged once; an invalid `lessons` value means `off`, and an unreadable `settings.json` turns lessons off and leaves every other option at its default until it is fixed, so a typo can never switch on what you switched off. `tool_hints` and `feedback_signals` are on unless set to `false`; `reflector` is on only when set to `true`.

**Language** (`i18n`). Everything a person or the agent reads is in English or Brazilian Portuguese: lessons, the prompt section header, hints, the digest, diagnoses, plans, recommendations, Kanban cards, the replies of the agent tools, error messages and the command help. The language is chosen in this order: `--lang en|pt` on the command line, the plugin's `language` option, the profile's `HERMES_LANGUAGE`, `display.language` of the profile configuration, the locale (`LC_ALL`, `LC_MESSAGES`, `LANG`), and finally English. The command help is built before the options are read, so it follows `HERMES_LANGUAGE`, `display.language` and the locale only. Codes such as `pt`, `pt-BR` and `pt_br` all select Portuguese. Field names, codes and identifiers in JSON output stay in English. A gettext-style catalog of sentence pairs (`catalog`) translates the rest, and `tests/test_i18n.py` fails when a diagnostic sentence, a help text, a Kanban card text or a plan sentence lacks its pair.

## Agent tools and the bundled skill

Both tools belong to the toolset `atlas-evolution` and are available only when `agent_tools` (`agent_advice` in `settings.json`) is on.

| Tool | Actions |
|---|---|
| `atlas_evolution_lessons` | `search`: up to 5 active lessons for a tool, `general` ones included. `propose`: stores a quarantined lesson for operator review, at most 5 per hour. `status`: lesson counts by status and up to 20 open proposals. |
| `atlas_evolution_plan` | returns a bounded plan for research, delegation or operational diagnosis. It is described in [Operations](operations.md). |

The skill `hermes-evolution:evolution-review` is registered with the plugin. It tells the agent how to run the review with the owner: read the digest, explain failures, skills by outcome and lessons, walk through the open proposals from the highest risk down with the exact lesson text, and hand the decisions to the owner as commands to run in their own terminal. The agent informs and recommends; the commands it runs cannot approve, reject or activate anything.

## Storage

The loop's tables live in `<HERMES_HOME>/evolution/evolution.sqlite3`, next to the tables of the operations observer. The database is at schema 7. Version 0.4.0 migrates schemas 0 to 6 automatically on first use: 4 added the loop, 5 skill outcomes, 6 the reflector excerpts and 7 the frozen lesson baselines. Any other schema, including a database already migrated when an earlier plugin version opens it, is refused.

| Table | One row per | Holds |
|---|---|---|
| `tool_daily` | day and tool | calls, errors, blocked calls, total duration |
| `failure` | signature | tool, class, detail, first and last time seen, total, sessions, hash of the last failing session |
| `failure_daily` | day and signature | count |
| `failure_sample` | signature | the scrubbed error excerpt kept for the reflector, and when it was updated; empty while the reflector is off |
| `turn_daily` | day and surface | turn counters and latency buckets |
| `feedback_daily` | day and kind | count of corrections, repeated questions or thanks |
| `skill_daily` | day, skill and action | count |
| `skill_outcome_daily` | day and skill | turns that loaded the skill, and how many failed, were interrupted, were slow, had a tool error or were corrected, plus total duration |
| `review_daily` | day and result | reviews, model calls, input, output and cached tokens |
| `lesson` | lesson | signature, tool, class, detail, origin, status, custom text, timestamps, times shown, hints given, verdict, rate before and after, frozen baseline counts |
| `change` | tracked change | kind, subject, detail, time, verdict, measured metrics |
| `proposal` | proposal | kind, risk, subject, evidence, command, status, note |
| `cursor` | key | position in `agent.log`, last configuration snapshot, last ledger time, last scheduled pass, reflector calls of the day |

Daily aggregates, changes, decided proposals and retired lessons are kept for 90 days (`experience.RETENTION_DAYS`). Lessons that are not retired and proposals that are open or approved are kept. Retention is applied by each pass and by the first hook write of each UTC day in each process.

Hook writes share one connection per process and profile. While the database is busy an update waits in a queue of up to 512 entries; overflow is counted and reported as `dropped_events` by `hermes evolution usage`.
