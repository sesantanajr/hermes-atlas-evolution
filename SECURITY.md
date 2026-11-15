# Security policy

Atlas Evolution runs inside Hermes Agent and observes its work. This document explains how to report a problem and states exactly what the plugin stores, reads and executes, so that you can check it against the code.

## Reporting a vulnerability

Please do not open a public issue with the details.

1. Use GitHub's private vulnerability reporting for this repository: the **Security** tab, then **Report a vulnerability**.
2. If that option is not available, contact the author through [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/) and ask for a private channel before sharing details.

Include the plugin version, the Hermes Agent version, the operating system, what you observed and the smallest set of steps that reproduces it. Remove conversation content, credentials and personal paths from anything you attach.

This is a volunteer project and there is no guaranteed response time. Fixes are released for the latest version only.

Reports of these kinds are especially welcome:

- conversation text, tool arguments, tool output, memory content, credentials or file paths reaching the plugin's databases or its command output;
- a lesson reaching the agent without operator approval in `suggest` mode, or with lessons turned `off`;
- the plugin changing Hermes (a skill, a setting, a unit) without the explicit setting that authorizes it;
- a hook callback raising an error into a turn or blocking it;
- a read that follows a link out of the profile, or a generated recipe that could run something other than what it prints.

## What the plugin stores

Everything is local, under `<HERMES_HOME>/evolution/`. On POSIX systems the folder is created with mode `0700` and the databases with mode `0600`.

| File | Content |
|---|---|
| `evolution.sqlite3` | Daily counters for tools, turns per surface, owner feedback kinds, skill events, background reviews, model calls and tokens per provider and model, and native delegation, compaction, Curator and Kanban worker events. Failure signatures: tool name, failure class, a short filtered detail, counts, first and last time seen, and a truncated hash of the last failing session. Lessons, tracked changes with their verdicts, proposals and reading cursors. Maintenance finding codes with their feedback. Hashes that prevent counting one event twice. |
| `supervisor.sqlite3` | Incident codes with the plugin's own hypothesis text, daily baselines, adaptive samples, alert feedback and the journal of systemd trials (unit names, weights and pressure values). |
| `settings.json` | Written by you. The plugin only reads it. |

Text that is stored as written, because someone wrote it on purpose:

- **Custom lessons.** A sentence of 12 to 240 characters added with `hermes evolution lessons add` or proposed by the agent through `atlas_evolution_lessons`. Control characters and values that look like secrets are rejected.
- **Proposal notes.** The optional `--note` of a decision, up to 200 characters.
- **Legacy notes.** Only when `legacy_memory_commands` is enabled: notes saved with the 0.1 commands.

## What the plugin does not store

- Conversation text, prompts or model responses.
- Tool arguments or tool output. Error text is classified in memory and discarded.
- Memory content. Native memory files are read to count entries and check their encoding.
- Raw log lines. From `agent.log` the plugin keeps numbers and a fixed result label.
- Session, turn or request identifiers in clear. They are hashed.
- Paths, URLs or credential-shaped values. The detail of a failure is kept only when it is a short name of at most 32 characters that passes `signals._clean_hint`. Tool, skill, provider and model names pass an allowlist, and a name that fails it is stored as `unknown` or dropped.
- Configuration values that are not plain labels. They are stored as `set`.

Retention is 35 days for observer metrics and 90 days for the evolution loop's aggregates, incidents and closed trials.

## What the plugin reads

Hermes files are opened read-only, with size limits, and the databases with a short deadline. The list is in [Hermes integration](docs/hermes-integration.md#what-the-plugin-reads).

## What the plugin executes

- **No network access and no telemetry.** The code opens no socket and calls no model.
- `systemctl show` for the two units named in `linux_supervisor`, when that policy is configured.
- `systemctl --runtime set-property` on the auxiliary unit, only with `linux_supervisor.mode: "apply"`, under a lock, with the previous value recorded and restored.
- `hermes kanban ... create` with a blocked card, only for `hermes evolution ops handoff` and only with `kanban_handoff.enabled`.

The cron recipes (`report --schedule-spec` and `ops schedule-spec`) are printed for review. The plugin does not write the script and does not create the job.

Optional packages are used only when you install them yourself: PyYAML lets the standalone runner read `config.yaml`, and a local SentenceTransformers model can be configured for `ops similar`, loaded from local files with remote code disabled.

## What reaches the agent

A lesson is text placed in the system prompt of new sessions and in the result of a failing tool call. Treat approving a lesson like editing the system prompt.

- Derived lessons come from fixed templates. Only the tool name and the filtered detail vary.
- Lessons proposed by the agent start in quarantine and are never shown before an operator approves them.
- The agent tools are hidden until `agent_advice` is enabled, and the agent can propose at most 5 lessons per hour.
- With `evolution.lessons: "off"` no lesson surface is registered.

## Verification status

The plugin is covered by unit tests and by end-to-end tests against a simulated Hermes context, including tests that assert sensitive-looking input is absent from the stored rows. It passes `hermes plugins validate` and `hermes plugins doctor` on Hermes Agent 0.23.1. It has not been audited by a third party and has not run on a production gateway yet.
