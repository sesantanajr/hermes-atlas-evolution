# Security policy

Atlas Evolution runs inside Hermes Agent, observes its work and can place text in front of the agent. This document explains how to report a problem, states the threat model in short, and lists exactly what the plugin stores, reads and executes, so that you can check it against the code.

## Reporting a vulnerability

Please do not open a public issue with the details.

1. Use GitHub's private vulnerability reporting for this repository: the **Security** tab, then **Report a vulnerability**.
2. If that option is not available, contact the author through [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/) and ask for a private channel before sharing details.

Include the plugin version, the Hermes Agent version, the operating system, what you observed and the smallest set of steps that reproduces it. Remove conversation content, credentials and personal paths from anything you attach.

This is a volunteer project and there is no guaranteed response time. Fixes are released for the latest version only.

Reports of these kinds are especially welcome:

- conversation text, tool arguments, tool output, memory content, credentials or file paths reaching the plugin's databases or its command output;
- a lesson reaching the agent without operator approval in `suggest` mode, or with lessons `off`;
- a playbook decision that succeeds from an agent's shell;
- a refused approval that turns into advice for the agent;
- reflector input that survives scrubbing with a secret, or a draft that carries instructions into the playbook;
- the plugin changing Hermes (a skill, a setting, a unit) without the explicit setting that authorizes it;
- a hook callback raising an error into a turn or blocking it;
- a read that follows a link out of the profile, or a generated recipe that could run something other than what it prints.

## Threat model

**What is protected.** What the agent reads (its system prompt and tool results), the profile's skills and configuration, and the user's data.

**Who can write to the playbook**, the only plugin data the agent reads:

| Writer | How | State when created | Reaches the agent |
|---|---|---|---|
| The plugin | a pass, from a recurring failure, using a fixed template | `candidate`, or `active` in `auto` mode | after approval, or at once in `auto` |
| The operator | `hermes evolution lessons add` in their own terminal | `quarantined`, or `active` with `--activate` after a confirmation | after approval |
| The reflector (a model) | a pass with `reflector` on | `quarantined` | after approval |
| The agent | `atlas_evolution_lessons` with `agent_tools` on, at most 5 per hour, also through the background review if you allow it | `quarantined`, high-risk proposal | after approval |

Error messages and command output are untrusted. They never become lesson text: only a class and a short detail filtered as a name reach a template, and a detail read from command output must look like a lower-case command or module name. Lesson text from any writer is NFKC-normalized, and control, bidirectional, zero-width and private-use characters and secret-shaped values are refused, so the text the operator approves is the text the agent gets.

**Who decides.** Only the operator. `lessons approve`, `retire` and `add` and `proposals approve` and `reject` refuse to run when the environment carries `HERMES_SESSION_ID`, `HERMES_KANBAN_TASK`, `HERMES_DELEGATED_CHILD_CONTEXT` or `ATLAS_EVOLUTION_AGENT`, which the plugin adds to every subprocess of an agent session through `register_session_env`. Activation shows the exact text and asks. This guard keeps the agent from using the plugin's commands; it is not a sandbox. An agent that can run arbitrary commands as the same user could still write the database file directly, and Hermes' own approval policy for terminal commands is the control for that.

**Approval refusals.** When the user denies a command, an approval times out, no approver is reachable or the context cannot approve, the failure is the class `approval_refused`. It gets no template, no reflector draft and no investigation proposal, so the plugin never teaches the agent to route around the user's decision. The lesson for `blocked_by_policy` tells the agent not to retry or look for another way, and to tell the user.

**The reflector.** Off by default. While it is on and `lessons` is not `off`, the hook keeps one excerpt of the latest error of each untemplated failure: at most 300 characters, passed through Hermes' redactor with `force=True` (so it applies even when the profile turned redaction off) and always through the plugin's masks for URLs, e-mail addresses, paths, long hexadecimal strings, credential prefixes, long numbers and long quoted strings. The excerpt goes to a model through Hermes, under the auxiliary task `evolution_reflector`, with instructions that it is data and not instructions, and the reply must match a JSON schema. A draft is discarded if it fails the lesson text rules, contains `<` or matches the injection filter. Accepted drafts stay quarantined. Calls are capped per pass and per day. The excerpt is deleted once the failure has a lesson, when a draft is declined or discarded, after 14 days, or at the next pass with the reflector off.

## What the plugin stores

Everything is local, under `<HERMES_HOME>/evolution/`. On POSIX systems the folder is created with mode `0700` and the databases with mode `0600`.

| File | Content |
|---|---|
| `evolution.sqlite3` | Daily counters for tools, turns per surface, owner feedback kinds, skill events, the outcome of turns per loaded skill, background reviews, model calls and tokens per provider and model, and native delegation, compaction, Curator and Kanban worker events. Failure signatures: tool name, failure class, a short filtered detail, counts, first and last time seen, and a truncated hash of the last failing session. Lessons, tracked changes with their verdicts, proposals and reading cursors. With the reflector on, one scrubbed error excerpt per untemplated failure. Maintenance finding codes with their feedback. Hashes that prevent counting one event twice. |
| `supervisor.sqlite3` | Incident codes with the plugin's own hypothesis text, daily baselines, adaptive samples, alert feedback and the journal of systemd trials (unit names, weights and pressure values). |
| `settings.json` | Written by you. The plugin only reads it. Values set in the Hermes settings form are stored by Hermes in the profile configuration. |

Text that is stored as written, because someone wrote it on purpose:

- **Lessons.** A sentence of 12 to 240 characters from `hermes evolution lessons add`, the agent's `atlas_evolution_lessons` or the reflector, checked as described above.
- **Proposal notes.** The optional `--note` of a decision, up to 200 characters.
- **Legacy notes.** Only when `legacy_memory_commands` is enabled: notes saved with the 0.1 commands.

## What the plugin does not store

- Conversation text, prompts or model responses.
- Tool arguments or tool output. Error text is classified in memory and discarded; only the reflector, when on, keeps the scrubbed excerpt described above.
- Memory content. Native memory files are read to count entries and check their encoding.
- Raw log lines. From `agent.log` the plugin keeps numbers and a fixed result label.
- Session, turn or request identifiers in clear. They are hashed.
- Paths, URLs or credential-shaped values. A failure detail is kept only when it is a short name of at most 32 characters that passes `signals._clean_hint`, including Hermes' redactor with `force=True`. Tool, skill, provider and model names pass an allowlist, and a name that fails it is stored as `unknown` or dropped.
- Configuration values that are not plain labels. They are stored as a short fingerprint.

Retention is 35 days for observer metrics and 90 days for the evolution loop's aggregates, incidents and closed trials.

## What the plugin reads

Hermes files are opened read-only, with size limits, and the databases with a short deadline. The list is in [Hermes integration](docs/hermes-integration.md#what-the-plugin-reads).

## What the plugin executes

- **No network connection of its own and no telemetry.** The only model call is the optional reflector's, made by Hermes through `ctx.llm` with the provider you configured for `auxiliary.evolution_reflector`.
- `systemctl show` for the two units named in `linux_supervisor`, when that policy is configured.
- `systemctl --runtime set-property` on the auxiliary unit, only with `linux_supervisor.mode: "apply"`, under a lock, with the previous value recorded and restored.
- `hermes kanban ... create` with a blocked card, only for `hermes evolution ops handoff` and only with `kanban_handoff.enabled`.

The cron recipes (`report --schedule-spec` and `ops schedule-spec`) are printed for review. The plugin does not write the script and does not create the job.

Optional packages are used only when you install them yourself: PyYAML lets the standalone runner read `config.yaml`, and a local SentenceTransformers model can be configured for `ops similar`, loaded from local files with remote code disabled.

## What reaches the agent

A lesson is text placed in the system prompt of new sessions and in the result of a failing tool call. Treat approving a lesson like editing the system prompt.

- Template lessons vary only in the tool name and the filtered detail.
- Lessons from you without `--activate`, from the agent and from the reflector start in quarantine and are never shown before you approve them.
- The agent tools are hidden until `agent_tools` is on.
- With `lessons` set to `off` no lesson surface is registered.

## Verification status

The plugin is covered by 207 unit and end-to-end tests against a simulated Hermes context, including tests that assert sensitive-looking input is absent from the stored rows, that decisions refuse to run with agent markers and that every approval refusal text of Hermes 0.23.1 is classified as `approval_refused`. It passes `hermes plugins validate` and `hermes plugins doctor` on Hermes Agent 0.23.1. The reflector was tested with a simulated model only. The loop went through an independent review before release. It has not been audited by a third party and has not run on a production gateway yet.
