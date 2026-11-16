# Security policy

Atlas Evolution runs inside Hermes Agent, observes its work, can place text in front of the agent and can veto some of the agent's terminal commands. This document explains how to report a problem, states the threat model in short, and lists exactly what the plugin stores, reads and executes, so that you can check it against the code.

## Reporting a vulnerability

Please do not open a public issue with the details.

1. Use GitHub's private vulnerability reporting for this repository: the **Security** tab, then **Report a vulnerability**.
2. If that option is not available, contact the author through [LinkedIn](https://www.linkedin.com/in/sergiosantanacloud/) and ask for a private channel before sharing details.

Include the plugin version, the Hermes Agent version, the operating system, what you observed and the smallest set of steps that reproduces it. Remove conversation content, credentials and personal paths from anything you attach.

This is a volunteer project and there is no guaranteed response time. Fixes are released for the latest version only.

Reports of these kinds are especially welcome:

- conversation text, tool arguments, tool output, memory content, credentials or file paths reaching the plugin's databases or its command output;
- a lesson reaching the agent without operator approval in `suggest` mode or with lessons `off`, or, in `auto` mode, lesson text that does not come from a fixed template (a reflector draft, an agent proposal, an operator lesson added without `--activate`) reaching it without approval;
- a playbook decision that succeeds from an agent's shell;
- a refused approval that turns into advice for the agent;
- reflector input that survives scrubbing with a secret, or a draft that carries instructions into the playbook;
- the plugin changing Hermes (a skill, a setting, a unit) without the explicit setting that authorizes it;
- a hook callback raising an error into a turn or blocking it;
- a read that follows a link out of the profile, or a generated recipe that could run something other than what it prints;
- an agent command that restarts the gateway or deletes what Hermes runs on and passes the runtime guard, or a veto that blocks ordinary work;
- the backfill, the radar or `status` writing to Hermes' files or to another profile, or text from Hermes' history reaching the plugin's databases;
- an owner notice shown in a gateway chat;
- someone other than the owner, in a chat, making a lesson activate by itself, getting operator, reflector or agent lesson text, getting an answer from the agent tools, or getting an error excerpt of theirs to the reflector.

## Threat model

**What is protected.** What the agent reads (its system prompt and tool results), the profile's skills and configuration, the user's data, and the running Hermes itself (its gateway, interpreter, source and profile) against the agent's own terminal commands.

**Who can write to the playbook**, the only plugin data the agent reads:

| Writer | How | State when created | Reaches the agent |
|---|---|---|---|
| The plugin | a pass, from a recurring failure, live or replayed from Hermes' history, using a fixed template | `active` in `auto` mode, the default, once the owner's own turns hit the failure twice; otherwise, and in `suggest`, `candidate` | at once when active; otherwise after approval |
| The operator | `hermes evolution lessons add` in their own terminal | `quarantined`, or `active` with `--activate` after a confirmation | after approval |
| The reflector (a model) | a pass with `reflector` on, the default | `quarantined`, in every mode | after approval |
| The agent | `hermes_atlas_evolution_lessons` with `agent_tools` on, at most 5 per hour, also through the background review if you allow it | `quarantined`, high-risk proposal | after approval |

Error messages and command output are untrusted, whether they arrive through a hook or are replayed from Hermes' history. They never become lesson text: only a class and a short detail filtered as a name reach a template, and a detail read from command output must look like a lower-case command or module name. Lesson text from any writer is NFKC-normalized, and control, bidirectional, zero-width and private-use characters and secret-shaped values are refused, so the text the operator approves is the text the agent gets.

**Who decides.** Only the operator. `lessons approve`, `retire`, `add` and `review` and `proposals approve` and `reject` refuse to run when the environment carries `HERMES_SESSION_ID`, `HERMES_KANBAN_TASK`, `HERMES_DELEGATED_CHILD_CONTEXT` or `ATLAS_EVOLUTION_AGENT`, which the plugin adds to every subprocess of an agent session through `register_session_env`. On Linux they also refuse when a Hermes process is an ancestor of the command or the command runs in the systemd cgroup of a Hermes gateway, both read from `/proc`, so clearing those variables or detaching with `setsid` is not enough; the command's own `hermes evolution` launcher does not count. Activation shows the exact text and asks. This guard keeps the agent from using the plugin's commands; it is not a sandbox. An agent that can run arbitrary commands as the same user could still write the database file directly, and Hermes' own approval policy for terminal commands is the control for that.

**Chats with other people.** A turn serves the owner on their own surfaces (CLI, TUI, Desktop, ACP), in their automations and subagents, or in a chat with a sender listed in `owner_sender_ids`; every other chat, integration or A2A peer serves someone else, and so does a session the plugin has not seen a turn of since it loaded. What someone else does is measured, but it cannot change the owner's prompt: their failures alone never activate a lesson (it waits for approval), they never leave an excerpt for the reflector, a chat that may serve someone else gets template lessons only (fixed text and an identifier-like detail, never operator, reflector or agent text), and the agent tools refuse to answer there. With `owner_sender_ids` empty, every messaging chat counts as someone else's.

**Approval refusals.** When the user denies a command, an approval times out, no approver is reachable or the context cannot approve, the failure is the class `approval_refused`. It gets no template, no reflector draft and no investigation proposal, so the plugin never teaches the agent to route around the user's decision. The lesson for `blocked_by_policy` tells the agent not to retry or look for another way, and to tell the user.

**The reflector.** On by default since 0.5.0; `reflector: false`, or an invalid value, switches it off. While it is on and `lessons` is not `off`, the hook (and the backfill, for replayed history) keeps one excerpt of the latest error of each untemplated failure of a turn that served the owner: at most 300 characters, passed through Hermes' redactor with `force=True` (so it applies even when the profile turned redaction off) and always through the plugin's masks for URLs, e-mail addresses, paths, long hexadecimal strings, credential prefixes, long numbers and long quoted strings. The excerpt goes to a model through Hermes, under the auxiliary task `evolution_reflector`, with instructions that it is data and not instructions, and the reply must match a JSON schema. A draft is discarded if it fails the lesson text rules, contains `<` or matches the injection filter. Accepted drafts stay quarantined in every mode, `auto` included, because a crafted error message could otherwise reach the agent's prompt. Calls are capped per pass and per day, and each call is counted before it is made, so a crash cannot exceed the cap. The excerpt is deleted once the failure has a lesson, when a draft is declined or discarded, after 14 days (also by the daily prune of the hooks, when no pass runs), or at the next pass with the reflector off.

**The runtime guard.** An agent with a terminal runs as the same user as Hermes, so it can stop the gateway it runs on or delete what that gateway needs. With `runtime_guard` on (the default), a `pre_tool_call` callback vetoes, for terminal-like tools, commands that would stop, restart or kill the gateway (its systemd unit on Linux, under any profile or home suffix, its LaunchAgent on macOS, its Windows service or scheduled task) or the Hermes processes, the agent's `hermes gateway restart` included, which is left to the owner, and commands that would delete or move the running interpreter and its environment (also under the name of a link, such as the base interpreter a virtual environment points at), the Hermes source, the Hermes root, the profile, a folder above them or a glob that matches them. Where Hermes runs plugins in a plugin host (`plugins.isolation: host`) no `pre_tool_call` is registered at all, because Hermes blocks a tool whose `pre_tool_call` fails and a crashed host would then block every tool; the guard is off there, and `status` says so. The details are in [Hermes integration](docs/hermes-integration.md#the-runtime-guard). The guard is a safety net for these two failure modes, not a sandbox: it looks only at the command word of each part of a command, after the wrappers it knows, ignores here-document bodies, looks one level into a command given as one string (`bash -c`, `su -c` and similar) but not into a command run through another program, such as `ssh`, or from a script, and lets any doubt through, such as a command it cannot parse or a path relative to the working folder. An invalid `runtime_guard` value keeps it on. Hermes' own approval policy for terminal commands remains the control for everything else.

**The autopilot and the history.** The pass runs in a background thread of each Hermes process, claimed so that only one process of a profile runs it at a time. A clock thread per loaded profile only nudges it, 2 minutes after the plugin loads and then every 10 minutes. The thread binds its own profile's home and secret scope, so in a gateway that serves several profiles a reflector call uses that profile's provider keys and never another profile's. The pass reads Hermes' records, including `state.db` read-only, and writes only the plugin's own database. It runs no command and changes nothing in Hermes.

**Other profiles.** `status` reads every profile of the same Hermes root, read-only: the number of sessions in the last 7 days from its `state.db`, whether its `plugins/` folder holds this plugin or the copy under the old id, and, where the plugin has data, the end of its last pass and its lesson counts from the plugin's own database there. It never writes to another profile; it prints the install or migration command for the operator to run.

**Long-running processes.** A gateway or dashboard that loads the plugin records, in the plugin's database of its profile, the plugin version it loaded, its kind and when it started, keyed by its process ID. `status` checks whether each recorded process is still alive with a signal-0 probe, which does not affect the process and runs only on POSIX systems, flags one that runs another version than the code on disk, and deletes the record of one that is gone.

**Owner notices.** At the end of a local session the plugin may return a short notice about decisions waiting. It is shown only on the owner's local surfaces (CLI, TUI, Desktop), never for a subagent and never in a gateway chat, where the other side may be someone else, and it carries counts only. Hermes 0.23.1 drops this text for local sessions it hosts when their viewer closes or the session is reaped, so the plugin returns nothing then and does not spend the day's notice.

## What the plugin stores

Everything is local, under `<HERMES_HOME>/evolution/`. On POSIX systems the folder is created with mode `0700` and the databases with mode `0600`.

| File | Content |
|---|---|
| `evolution.sqlite3` | Daily counters for tools, turns per surface, owner feedback kinds, skill events, the outcome of turns per loaded skill, background reviews, model calls and tokens per provider and model, and native delegation, compaction, Curator and Kanban worker events. Failure signatures: tool name, failure class, a short filtered detail, counts, first and last time seen, and a truncated hash of the last failing session. Lessons, tracked changes with their verdicts, proposals and reading cursors. With the reflector on, one scrubbed error excerpt per untemplated failure. Counts of the commands the runtime guard vetoed, by kind only. The backfill position and totals, the autopilot lease and the status of the last pass, and the day of the last owner notice. For each gateway or dashboard process that loaded the plugin, keyed by its process ID: the plugin version, the process kind and when it started, deleted once `status` finds the process gone. The radar's last snapshot of the plugins of a process: plugin keys, hook counts and the exception name of a load error; radar proposals also keep the session end reasons as Hermes records them. Maintenance finding codes with their feedback. Hashes that prevent counting one event twice. |
| `supervisor.sqlite3` | Incident codes with the plugin's own hypothesis text, daily baselines, adaptive samples, alert feedback and the journal of systemd trials (unit names, weights and pressure values). |
| `settings.json` | Written by you. The plugin only reads it. Values set in the Hermes settings form are stored by Hermes in the profile configuration. |

Text that is stored as written, because someone wrote it on purpose:

- **Lessons.** A sentence of 12 to 240 characters from `hermes evolution lessons add`, the agent's `hermes_atlas_evolution_lessons` or the reflector, checked as described above.
- **Proposal notes.** The optional `--note` of a decision, up to 200 characters.
- **Legacy notes.** Only when `legacy_memory_commands` is enabled: notes saved with the 0.1 commands.

## What the plugin does not store

- Conversation text, prompts or model responses, including those replayed from `state.db`.
- Tool arguments or tool output. Error text is classified in memory and discarded; only the reflector, when on, keeps the scrubbed excerpt described above.
- The commands the runtime guard vetoes. The hash of each terminal command that `pre_tool_call` keeps to tell whether another plugin rewrote it stays in memory, at most 512 at a time, and is never stored.
- Memory content. Native memory files are read to count entries and check their encoding.
- Raw log lines. From `agent.log` the plugin keeps numbers and a fixed result label.
- Session, turn or request identifiers in clear. They are hashed.
- Paths, URLs or credential-shaped values. A failure detail is kept only when it is a short name of at most 32 characters that passes `signals._clean_hint`, including Hermes' redactor with `force=True`. Tool, skill, provider and model names pass an allowlist, and a name that fails it is stored as `unknown` or dropped.
- Configuration values that are not plain labels. They are stored as a short fingerprint.

Retention is 35 days for observer metrics and 90 days for the evolution loop's aggregates, incidents and closed trials. The guard's daily counts (`guard_daily`) follow the 90-day retention. The delivery IDs that stop one event from being counted twice (`seen`) are kept 2 days, and reflector excerpts (`failure_sample`) at most 14 days.

## What the plugin reads

Hermes files are opened read-only, with size limits, and the databases with a short deadline. This includes, for `status`, the `state.db` of the other profiles of the same Hermes root, where it only counts sessions, and the plugin's own database in those profiles, where it reads the end of the last pass and the lesson counts. On Linux, a decision command also reads `/proc` to find its ancestor processes and its cgroup. The list is in [Hermes integration](docs/hermes-integration.md#what-the-plugin-reads).

## What the plugin executes

- **No network connection of its own and no telemetry.** The only model call is the reflector's, on by default, made by Hermes through `ctx.llm` with the provider configured for `auxiliary.evolution_reflector`, which inherits the compression model unless you set another.
- Two background daemon threads per profile loaded in a Hermes process: the clock, which only nudges the pass, and the autopilot, which runs it. Neither executes a command.
- `status` probes whether a recorded Hermes process is still alive with signal 0, which sends nothing to the process, on POSIX systems only.
- The runtime guard executes nothing. It returns a block directive to Hermes, which then does not run the command.
- `systemctl show` for the two units named in `linux_supervisor`, when that policy is configured.
- `systemctl --runtime set-property` on the auxiliary unit, only with `linux_supervisor.mode: "apply"`, under a lock, with the previous value recorded and restored.
- `hermes kanban ... create` with a blocked card, only for `hermes evolution ops handoff` and only with `kanban_handoff.enabled`.

The cron recipes (`report --schedule-spec` and `ops schedule-spec`) are printed for review. The plugin does not write the script and does not create the job.

Optional packages are used only when you install them yourself: PyYAML lets the standalone runner read `config.yaml`, and a local SentenceTransformers model can be configured for `ops similar`, loaded from local files with remote code disabled.

## What reaches the agent

A lesson is text placed in the system prompt of new sessions and, with `tool_hints` on (off by default), in the result of a failing tool call. Treat approving a lesson like editing the system prompt.

- Template lessons vary only in the tool name and the filtered detail. In `auto` mode, the default, a new template lesson reaches the agent at once and is retired when it is measured as ineffective 14 days or more after activation; in `suggest` mode it waits for your approval.
- Lessons from you without `--activate`, from the agent and from the reflector start in quarantine and are never shown before you approve them, in every mode.
- The agent tools are hidden until `agent_tools` is on.
- With `lessons` set to `off` no lesson surface is registered.
- When the runtime guard vetoes a command, the agent reads the guard's fixed refusal, in the plugin's language, in place of the tool result. It names the protected path, or tells the agent to ask the owner, who runs `hermes gateway restart` from their own terminal.
- Owner notices are shown to the owner outside the conversation. They do not reach the agent and never start a turn.

## Verification status

The plugin is covered by unit and end-to-end tests against a simulated Hermes context, including tests that assert sensitive-looking input is absent from the stored rows, also for history replayed from `state.db`, that decisions refuse to run with agent markers, that every approval refusal text of Hermes 0.23.1 is classified as `approval_refused`, that the runtime guard vetoes the known commands, lets ordinary administration through and never raises, and that a profile with the copy under the old id registers nothing. It passes `hermes plugins validate` on Hermes Agent 0.23.1, with the security scan reporting it safe. In production, three real reflector calls through Hermes produced three drafts. The autopilot is verified in tests and in the CLI, and its clock and the stale-process check in tests. The runtime guard's macOS and Windows rules, Windows paths and output are covered by tests that simulate those systems on Linux; the plugin has not run on a real macOS or Windows machine. The loop went through an independent review before the 0.4.0 release. It has not been audited by a third party, and lesson verdicts have not been observed over weeks of real use yet.
