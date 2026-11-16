# The evolution loop

**English** | [Português (Brasil)](pt-BR/evolution-loop.md)

This document describes the loop that Atlas Evolution 0.5.0 adds to Hermes Agent: how it runs by itself and learns from the profile's history, what is recorded, when a failure becomes a lesson, how lessons, skills and changes are measured, what the reflector does, what the health radar reads and what ends up in the proposal queue. Every threshold below is a constant in the code, and the module that holds it is named next to it. For installation and a summary, see the [README](../README.md).

## Contents

1. [One pass](#one-pass)
2. [The autopilot](#the-autopilot)
3. [Learning from Hermes' history](#learning-from-hermes-history)
4. [Signals](#signals)
5. [Lessons](#lessons)
6. [The reflector](#the-reflector)
7. [How a lesson is measured](#how-a-lesson-is-measured)
8. [Skills by outcome](#skills-by-outcome)
9. [Changes and experiments](#changes-and-experiments)
10. [Skill health](#skill-health)
11. [Learning yield](#learning-yield)
12. [Health radar](#health-radar)
13. [Proposals](#proposals)
14. [Digest](#digest)
15. [Status and owner notice](#status-and-owner-notice)
16. [Settings and language](#settings-and-language)
17. [Agent tools and the bundled skill](#agent-tools-and-the-bundled-skill)
18. [Storage](#storage)

## One pass

Hooks record signals while the agent works. Everything else happens in a pass (`evolve.cycle`), which reads Hermes' own records, updates the plugin's tables and never changes Hermes. A pass runs when:

- the autopilot finds one due, at most once per `autopilot_minutes` (default 60; see [The autopilot](#the-autopilot));
- you run `hermes evolution learn`;
- `hermes evolution ops tick` runs, at most once an hour (`CYCLE_INTERVAL_SECONDS`; the result is the `evolution` field of its output);
- the weekly digest recipe runs, which calls `learn --notify-only` before `report`.

All four claim the same pass, so two passes of one profile never run at the same time, and each pass counts as the latest one for the others. With `autopilot_minutes` set to 0 and none of the other three, signals accumulate but no lesson is derived and nothing is measured.

A pass does the following, in order:

1. Marks when the plugin started observing the profile, unless the plugin did so when it loaded.
2. While the backfill is not done, replays the next part of Hermes' history (see [Learning from Hermes' history](#learning-from-hermes-history)), for up to 20 seconds, or 120 seconds in `learn`.
3. Drops aggregates older than the retention window.
4. Counts the background reviews logged since the previous pass (on the first run, those of the last 14 days, rotated files included).
5. Derives lessons from recurring failures.
6. Asks the reflector for drafts when it is on, `lessons` is not `off` and Hermes' model access is available. With the reflector off it deletes every waiting error excerpt; otherwise excerpts wait for a later pass and expire after 14 days.
7. Measures each active lesson that is bound to a failure.
8. Computes skill health for the last 7 days.
9. Detects configuration changes and skill rewrites.
10. Compares the week before and the week after each tracked change.
11. Closes approved `skill_lock` proposals whose skill is now locked or pinned.
12. Compares the skills loaded in the last 14 days with the profile as a whole.
13. Reads the [health radar](#health-radar).
14. Rebuilds the open proposals from the current evidence.

Each phase commits on its own, so the hook writers of a running gateway never wait while a pass reads a log or waits for a model.

With `--notify-only`, `learn` prints only new lessons, new reflector drafts, regressions whose verdict became final in this pass, lessons retired in this pass and high-risk proposals opened in this pass. It prints nothing when there are none. The full output of `learn` also has a `history` block, with the backfill status and running totals, and a `radar` list with the codes of the current radar findings.

## The autopilot

The autopilot (`autopilot`) runs the pass by itself, in a background daemon thread of each Hermes process that loaded the plugin, at most once per `autopilot_minutes` for the profile. The value is 0 (off: passes run only when you ask) or 15 to 1440 minutes; the default is 60.

- **What starts it.** The hooks `post_llm_call` and `on_session_end`, after each turn, and `on_kanban_dispatch_tick`, which Hermes fires once per Kanban dispatcher tick (every 60 seconds by default) only where the dispatcher runs inside the gateway (`kanban.dispatch_in_gateway`). A hook only compares two clocks: a check starts at most once every 60 seconds per process (`CHECK_SECONDS`), in the thread, never on the turn path. A pass that is due in a quiet process waits for the next nudge.
- **One pass per profile.** Every process of the profile, whether a gateway, a CLI session or `ops tick`, claims the pass with a compare-and-set in the plugin's database: in one write transaction it reads when the last pass ended and who holds the lease, and takes the lease. A lease lasts 15 minutes (`LEASE_SECONDS`), so a process that dies in the middle of a pass leaves a lease that expires, and the pass is tried again. A failed pass is tried again after 10 minutes (`RETRY_SECONDS`) instead of a full interval, and the first failure in a process is logged once.
- **The right profile.** Hermes keeps the active profile in a context variable that new threads do not inherit, so the thread pins the plugin's own profile before anything reads Hermes' configuration.
- **Settings.** Each pass reads the settings again, and an invalid value falls back on its own instead of failing the pass (see [Settings and language](#settings-and-language)). The interval itself is set when the plugin loads.
- **Manual passes.** `hermes evolution learn` waits up to 30 seconds for a running pass instead of running alongside it, and then counts as the latest pass, so the autopilot does not repeat it right away. A failed manual pass leaves the schedule as it was. `ops tick` uses the same claim, with its own hourly interval.

`hermes evolution status` shows the interval and the last pass: when it ended, whether it succeeded and how long it took (see [Status and owner notice](#status-and-owner-notice)). The autopilot is verified in tests and in the CLI.

## Learning from Hermes' history

Hermes keeps every session of a profile in `<HERMES_HOME>/state.db`. Until the backfill is done, each pass reads the next part of it (`backfill`), so a failure that recurred before the plugin was installed can become a lesson on the first day.

- **What is read.** The sessions that overlap the last 14 days (`BACKFILL_DAYS`) before the plugin started observing the profile, 25 at a time (`SLICE_SESSIONS`), oldest first, and their messages inside that window. The database is opened read-only and never modified.
- **What it becomes.** Messages are replayed through the code the hooks use:
  - tool results become tool counts and failure signatures, and, with the reflector on and `lessons` not `off`, the scrubbed excerpt of an untemplated failure. A stored result counts as failed the way Hermes' tools report it: an `error` field, a non-zero `exit_code`, `success: false`, or text that starts like an error;
  - user messages open turns, timed per surface, and, for the owner, count as corrections, repeated questions or thanks;
  - a skill loaded with `skill_view` is charged with the outcome of its turn.
- **Turns.** A message sent before the agent answered joins the same turn, as Hermes handles it, and is not a repeated question. A turn is completed when its last assistant message finished without asking for tools. Otherwise it is interrupted when a new user message followed or the session was reaped or reset, and failed in the remaining cases. A turn cut by the start of observation is left to the hooks.
- **Nothing counted twice.** Only history older than the moment the plugin started observing the profile (`observed_since`) is read. That moment is when the plugin first loaded in the profile; for an upgrade, whose database already holds live data, it is the start of the earliest day with live data. Each slice is written in one transaction together with its cursor, so a pass killed half way leaves nothing half counted, and the next pass resumes after the last session written.
- **Budget.** 20 seconds per pass in the autopilot and in `ops tick`, 120 seconds in `learn`. The next pass continues where the last one stopped.
- **Status.** `waiting` (no slice read yet), `running`, `done`, `no_history` (there is no `state.db`), `unreadable` (tried again at the next pass) and `unsupported_history` (a `state.db` without the expected tables or columns). `done`, `no_history` and `unsupported_history` are final.
- **Privacy.** Text is classified in memory and discarded, as in the hooks. Model calls and tokens are not replayed, so usage counts start when the plugin loads.

On a copy of a real profile, the first pass replayed 275 sessions, 436 turns and 6,731 tool calls in 1.2 s and 29 MB, and produced 7 lessons. This is one measurement on one machine.

The first run of the review counter also reads the rotated logs of the same 14 days (see [Learning yield](#learning-yield)).

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
| `local` | platform `cli`, `tui`, `desktop`, `gui`, `acp` or `local` |
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
| `agent` | the `hermes_evolution_lessons` tool, action `propose` | no | high |

### Text rules

Lesson text that is not a template, from you, the agent or the reflector, goes through `lessons.clean_text`:

- it is normalized to Unicode NFKC and its whitespace is collapsed;
- control, format (bidirectional and zero-width), private-use and surrogate characters and line or paragraph separators are refused, not stripped, so what you read is exactly what the agent gets;
- it has 12 to 240 characters;
- it must not contain a value shaped like a credential (`sk-`, `ghp_`, `gho_`, `github_pat_`, `xox?-`, `AKIA`, `AIza` followed by at least 8 characters) or a URL with a user and password.

A lesson names the tool it applies to, or `general`. The playbook holds at most 200 lessons that are not derived and not retired. The agent can propose at most 5 lessons per hour (`agent_tools.PROPOSALS_PER_HOUR`).

### Delivery

- **Prompt section** (`hermes-evolution.playbook`). Hermes renders it at the start of each session and freezes it into that session's system prompt. It lists active lessons of any origin, those measured as effective first and then those with the most evidence, up to 8 lessons and 1,200 characters, under a one-line header. The section is registered with a limit of 1,400 characters. It only reads the database and counts each display (`shown`) through the hook write queue, so a pass running in another process does not leave a new session without its lessons.
- **Hint on the failing result** (`transform_tool_result`, when `tool_hints` is on). When a call with status `error` or `blocked` fails with a signature that has an active lesson, the sentence is added to that result. A JSON object gains the key `hermes_evolution_lesson`; any other text gains a final line that starts with `[Atlas Evolution lesson]` (`[Lição do Atlas Evolution]` in Portuguese). Each signature is hinted once per session, with at most 6 hints per session (`hints.MAX_HINTS_PER_SESSION`), and only when the call has a session ID. Results over 262,144 characters and successful calls are left untouched, and so is a JSON result that already has a non-empty `hint` field, such as the hints Hermes' terminal and file tools write: Hermes already advised. Only lessons bound to a failure (derived lessons and reflector drafts) can be hinted. The index of active lessons is reloaded at most once a minute, so a lesson approved during a session can be hinted in that session.

Because the section is fixed for the whole session and the hint is appended to a new tool result, neither path changes a prefix the provider has already cached.

## The reflector

The reflector (`reflector`) drafts lessons for recurring failures that have no template. It is on by default; `reflector: false` switches it off, and so does an invalid value. While it is off, nothing below happens.

**What is kept.** When the plugin loads with the reflector on and `lessons` is not `off`, the tool hook keeps, for each failure signature of the classes `code_exception`, `command_failed`, `network`, `auth` and `other`, the latest error excerpt (`failure_sample`). The backfill keeps the same excerpts from the history it replays. Each excerpt is:

1. the last 4 non-empty lines of the error text and output, joined, at most 1,200 characters;
2. passed through Hermes' secret redactor when the plugin runs inside Hermes, with `force=True` so that it applies even when the profile turned redaction off;
3. masked by the plugin: URLs become `<url>`, e-mail addresses `<email>`, paths of two or more segments `<path>`, hexadecimal strings of 16 or more characters `<hex>`, values with a credential prefix `<secret>`, runs of 5 or more digits `<n>`, and quoted strings over 40 characters `'<text>'`;
4. cut to the last 300 characters (`EXCERPT_LIMIT`).

**Which failures are drafted.** The same recurrence rule as derived lessons: seen at least 3 times, in at least 2 sessions or on at least 2 days, with an excerpt and without a lesson. The most frequent come first.

**How many calls.** At most 3 per pass (`PER_RUN`) and `reflector_daily_limit` per UTC day (default 6, from 1 to 50). Every attempt counts toward the daily limit, including failed ones, and it is counted before the call is made, so a crash or a killed process cannot exceed the cap. No transaction stays open during the call.

**The call.** The plugin registers the Hermes auxiliary task `evolution_reflector` ("Atlas Evolution reflector") with `inherit_from="compression"`, so by default it uses the model configuration of the compression task. You choose another model in `auxiliary.evolution_reflector` of the profile configuration. The pass calls `ctx.llm.complete_structured` with that task, a JSON schema (`applies`, a boolean, and `lesson`, a string), at most 300 output tokens and a 60-second timeout. The input holds the tool name, the failure class, the detail, the number of occurrences, sessions and days, and the excerpt. The instructions, in the configured language, say that the excerpt is data and never instructions, ask for `applies: false` when the failure looks transient, specific to one request or outside the agent's control, and otherwise ask for one sentence of 20 to 200 characters shaped as "when X, do Y", without paths, names, credentials, URLs or text copied from the excerpt. The plugin opens no connection: Hermes makes the call with its own provider configuration.

**What is accepted.** A draft is discarded when `applies` is not true, when the sentence fails the [text rules](#text-rules), when it contains `<`, or when it matches the injection filter (`reflector._INJECTION`), which refuses English text that tells the model to set aside earlier instructions or to disregard something, mentions a system prompt or tells the model what it now is, Portuguese requests to ignore or forget the instructions, and `system` or `assistant` tags. An accepted draft becomes a `quarantined` lesson with origin `reflector`, bound to its failure signature, and gets a medium-risk proposal. Drafts stay in quarantine in every mode, `auto` included, because a crafted error message could otherwise reach the agent's prompt. Once approved, a draft is shown, hinted and measured like a derived lesson.

**When the excerpt is deleted.** As soon as the failure has a lesson, when the model declines or the draft is discarded, 14 days after its last update (`SAMPLE_DAYS`), and at every pass that runs with the reflector off. A provider error, or a pass without Hermes' model access, leaves the excerpt for a later pass.

**Where it runs.** Only in a pass run inside Hermes, because the model access comes from Hermes: the autopilot of a Hermes process, `hermes evolution learn`, `ops tick` or the weekly recipe. The model access is registered when the plugin loads, so turning the reflector on takes effect in Hermes processes started after the change. It runs only while `lessons` is not `off` (otherwise the block reports `lessons_off`, since an approved draft would never be shown). The standalone runner (`python -m hermes_evolution`) drafts nothing; its `learn` reports `unavailable_without_hermes_llm` and keeps the waiting excerpts for the next pass run by Hermes.

The `learn` output has a `reflector` block with the drafts, the number declined and failed, the calls made today and the daily limit.

**Verified in production.** Three real calls through Hermes (provider DeepSeek, about 600 tokens and about 2 seconds each) produced three drafts in Portuguese.

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

Only these configuration keys are watched (`experiments.CONFIG_KEYS`): `compression.enabled`, `compression.threshold_tokens`, `compression.proactive_prune_tokens`, `model.default`, `model.model`, `model.provider`, `memory.provider`, `memory.nudge_interval`, `skills.creation_nudge_interval`, `auxiliary.background_review.enabled`, `auxiliary.background_review.model`, `auxiliary.compression.model`, `tool_budget.mcp_result_size_chars`, `curator.enabled`, `curator.consolidate`, `compression.threshold`, `compression.target_ratio`, `compression.protect_last_n`, `skills.auto_load`, and the list of enabled plugins as a count plus a short hash. A value that is a plain label is stored as it is. Any other value (a `provider/model` name, a URL, a mapping) is stored as `set:` and a 10-character fingerprint, never the value, so a change between two such values is still detected.

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

A cursor (inode and offset) keeps a line from being counted twice. The first run reads `agent.log.5` down to `agent.log.1` and then `agent.log` from the start, counting only lines dated inside the last 14 local days. After a rotation, the rest of the old file is read before the new one, so lines written just before Hermes renamed the log are not lost. A file truncated in place is read again from the start. Links are ignored. A pass reads at most 32 MiB, in pieces of 4 MiB, and the next pass continues. A profile upgraded from 0.4.0 keeps its cursor and does not read the rotated files again.

The digest shows how many reviews ran, the share that produced nothing and the tokens they used. When the plugin also observed the main model calls, it adds the share of uncached input and output tokens that went to reviews. That share is approximate.

## Health radar

At each pass the radar (`radar`) reads what slows Hermes down or wastes its context, over the last 7 days (`RADAR_DAYS`). Every reading is read-only and bounded. A reading that crosses its threshold becomes an advisory proposal with the native command to inspect it, and the proposal expires when the evidence goes away.

| Reading | Source | Proposed when | Proposal |
|---|---|---|---|
| Sessions that end as orphans (`ws_orphan_reap`, `startup_orphan_reap`), reaped after a disconnect or a restart, so the end-of-session work of Hermes and its memory never runs for them | `sessions` in `state.db`, subagent sessions left out | at least 10 ended sessions (`MIN_ENDED_SESSIONS`) and at least 30% of them orphaned (`ORPHAN_SHARE`) | `session_lifecycle`, low risk, `hermes sessions list` |
| Prompt tokens per agent call (fresh, cache-read and cache-write input), and the share served from cache | `session_model_usage` in `state.db`, rows with an empty or `main` task, so auxiliary tasks are left out | at least 50 calls (`MIN_CALLS`) and an average of at least 60,000 tokens (`HEAVY_PROMPT_TOKENS`) | `context_weight`, low risk, `hermes prompt-size`; the evidence names `skills.auto_load`, `compression.proactive_prune_tokens` and `tool_budget.mcp_result_size_chars` as settings to look at |
| Compactions that keep most of the context: each costs a model call and buys little room | the `post_compaction` events the plugin counted | at least 5 compactions (`MIN_COMPACTIONS`) that together kept at least 75% of the tokens (`WEAK_COMPACTION_KEPT`) | `compaction`, low risk, `hermes config get compression` |
| Plugins of this process that failed to load; only the exception name is kept | Hermes' plugin manager | any occurrence | `plugin_health`, low risk, `hermes plugins doctor <plugin>` |
| Plugins that register more hooks than their manifest declares | Hermes' plugin manager and the `provides_hooks` of each `plugin.yaml` | any occurrence | `plugin_health`, low risk, `hermes plugins doctor <plugin>` |
| Two or more enabled plugins that declare a transform hook where the first string returned wins: `transform_tool_result`, `transform_terminal_output` or `transform_llm_output` | the same manifests | any occurrence | `plugin_conflict`, medium risk, `hermes plugins list` |

The plugin readings come from the plugin list of Hermes' plugin manager, read only when that module is already loaded in the process; the radar does not import it. A process without it uses the snapshot of the last process that could take one, kept in the plugin's database, and with no snapshot the plugins are skipped. Manifests are read from the folder of the plugins bundled with Hermes and from `<HERMES_HOME>/plugins/`, at most 500 folders and 64 KiB per manifest. Competing plugins are listed in the alphabetical order of their keys, which is the order Hermes loads them in unless one plugin requires another.

The settings these findings point to are tracked as [changes](#changes-and-experiments), so a change to one of them gets its own before-and-after verdict. The digest shows the same readings for its own window, up to 14 days, in the section "Hermes health".

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
| `session_lifecycle` | low | many sessions end as orphans ([Health radar](#health-radar)) | `hermes sessions list` | you investigate |
| `context_weight` | low | a heavy context per agent call | `hermes prompt-size` | you investigate |
| `compaction` | low | compactions keep most of the context | `hermes config get compression` | you review the setting |
| `plugin_health` | low | a plugin failed to load or registers hooks it does not declare | `hermes plugins doctor <plugin>` | you investigate |
| `plugin_conflict` | medium | plugins compete for a transform hook where only the first result counts | `hermes plugins list` | you review the plugins |

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
| Hermes health | the radar readings for the digest window, up to 14 days: how sessions ended, the context per agent call and its cached share, compactions, the plugins of this process, and the commands the runtime guard vetoed, by kind |
| Proposals awaiting a decision | one line for the lessons awaiting approval (up to 10 named, whose text is in `lessons list`), then up to 20 other open proposals with their command |

`--format json` returns the full structure, which also has cron and subagent turns, per-tool totals, skill events, the skill outcome block, the usage observed by the hooks, the radar readings (`health`) and the guard vetoes by kind (`guard`). Where a number could not be measured the digest says "not measured".

`report --schedule-spec` prints a recipe: a script and the arguments of a native `hermes cron create` in no-agent mode, by default on weekday 1 at hour 8 with local delivery (`--weekday`, `--hour`, `--deliver`, `--days` and `--lang` change it; weekday 0 is Sunday). The script runs `learn --notify-only` and then `report`. The command writes no file and creates no job.

## Status and owner notice

`hermes evolution status` (`status`) says whether the plugin is observing, learning and protecting this profile right now. It prints Markdown by default, JSON with `--format json`, and accepts `--lang en|pt`. It reads the plugin's own store and shows:

- when the plugin started observing the profile;
- the events the hooks saw today (UTC): turns, tool calls and model calls;
- the autopilot interval and the last pass: when it ended, whether it succeeded and how long it took;
- the backfill status and its totals of sessions, turns, tool calls and failures;
- the reflector: on or off, calls today against the daily limit, and drafts waiting;
- lessons by state (active, awaiting approval, retired) and the number of open proposals;
- the runtime guard and the commands it vetoed in the last 7 days;
- the other profiles of the same Hermes root that had sessions in the last 7 days and do not have the plugin, with the exact install command. Each profile loads its own plugins, so a busy profile without the plugin is invisible to the loop. Their `state.db` is opened read-only, and only sessions are counted.

A final list says what needs attention: no events today, the autopilot off, no pass yet, a failed last pass, lessons waiting, profiles without the plugin.

**Owner notice.** When a session ends, Hermes calls `on_session_finalize`, and the plugin may return one short notice about decisions waiting: lessons awaiting approval, reflector drafts to read, high-risk proposals, or a failed last pass. There is at most one notice per UTC day, and only on the owner's local surfaces (CLI, TUI, Desktop), never for a subagent and never in a gateway chat, where the other side may be someone else. Hermes shows the notice outside the conversation; it does not start an agent turn.

## Settings and language

The options of the loop can be set in two places (`cli.effective_settings`):

1. the Hermes settings form, declared by `config_schema` in `plugin.yaml` and stored by Hermes under `plugins.entries.hermes-evolution.settings`;
2. the `evolution` block of `<HERMES_HOME>/evolution/settings.json` (`agent_tools` corresponds to the top-level key `agent_advice` there).

Hermes wins key by key. The plugin reads the settings when it loads, which sets the hooks, the prompt section, the runtime guard, the autopilot interval and the reflector's model access. Each autopilot pass and each command read the settings again.

Validation differs by caller (`evolve.options`). The commands reject an invalid `lessons`, `slow_turn_seconds`, `owner_sender_ids`, `reflector`, `reflector_daily_limit`, `autopilot_minutes` or `runtime_guard` with an error. Inside the gateway and in the autopilot each invalid value falls back on its own, and a warning is logged once when the plugin loads: an invalid `lessons` means `off`, an invalid `reflector` switches the reflector off, an invalid `autopilot_minutes` means 0 (passes only when you ask), an invalid `runtime_guard` keeps the guard on, and the others take their defaults. An unreadable `settings.json` turns lessons off until it is fixed; the values set in the Hermes form still apply to every other option. `tool_hints` and `feedback_signals` are on unless set to `false`; `reflector` and `runtime_guard` take `true` or `false`; `autopilot_minutes` takes 0 or an integer from 15 to 1440.

**Language** (`i18n`). Everything a person or the agent reads is in English or Brazilian Portuguese: lessons, the prompt section header, hints, the digest, diagnoses, plans, recommendations, Kanban cards, the replies of the agent tools, error messages and the command help. The language is chosen in this order: `--lang en|pt` on the command line, the plugin's `language` option, the profile's `HERMES_LANGUAGE`, `display.language` of the profile configuration, the locale (`LC_ALL`, `LC_MESSAGES`, `LANG`), and finally English. The command help is built before the options are read, so it follows `HERMES_LANGUAGE`, `display.language` and the locale only. Codes such as `pt`, `pt-BR` and `pt_br` all select Portuguese. Field names, codes and identifiers in JSON output stay in English. A gettext-style catalog of sentence pairs (`catalog`) translates the rest, and `tests/test_i18n.py` fails when a diagnostic sentence, a help text, a Kanban card text or a plan sentence lacks its pair.

## Agent tools and the bundled skill

Both tools belong to the toolset `hermes-evolution` and are available only when `agent_tools` (`agent_advice` in `settings.json`) is on.

| Tool | Actions |
|---|---|
| `hermes_evolution_lessons` | `search`: up to 5 active lessons for a tool, `general` ones included. `propose`: stores a quarantined lesson for operator review, at most 5 per hour. `status`: lesson counts by status and up to 20 open proposals. |
| `hermes_evolution_plan` | returns a bounded plan for research, delegation or operational diagnosis. It is described in [Operations](operations.md). |

The skill `hermes-evolution:evolution-review` is registered with the plugin. It tells the agent how to run the review with the owner: read the digest, explain failures, skills by outcome and lessons, walk through the open proposals from the highest risk down with the exact lesson text, and hand the decisions to the owner as commands to run in their own terminal. The agent informs and recommends; the commands it runs cannot approve, reject or activate anything.

## Storage

The loop's tables live in `<HERMES_HOME>/evolution/evolution.sqlite3`, next to the tables of the operations observer. The database is at schema 8. Version 0.5.0 migrates schemas 0 to 7 automatically on first use: 4 added the loop, 5 skill outcomes, 6 the reflector excerpts, 7 the frozen lesson baselines and 8 the runtime guard counts. Any other schema, including a database already migrated when an earlier plugin version opens it, is refused.

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
| `guard_daily` | day and kind | commands the runtime guard vetoed: `gateway_stop` or `runtime_removal`, never the command itself |
| `cursor` | key | position in `agent.log` and the rotated files, last configuration snapshot, last ledger time, end of the last pass and its status, the autopilot lease, when observation started, the backfill position, status and totals, reflector calls of the day, the day of the last owner notice, the radar's last plugin snapshot |

Daily aggregates, changes, decided proposals and retired lessons are kept for 90 days (`experience.RETENTION_DAYS`). Lessons that are not retired and proposals that are open or approved are kept. Retention is applied by each pass and by the first hook write of each UTC day in each process. It covers `guard_daily` too.

Hook writes share one connection per process and profile. While the database is busy an update waits in a queue of up to 512 entries; overflow is counted and reported as `dropped_events` by `hermes evolution usage`.
