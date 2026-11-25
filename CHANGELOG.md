# Changelog

*Em português: [CHANGELOG.pt-BR.md](CHANGELOG.pt-BR.md).*

## 0.14.0

An audit of ten days of a real fleet (one gateway, 12 profiles) found that the learning passes had stopped in 11 of
them, that six "effective" lessons had been judged on a handful of calls, and that a Hermes update had left an overlay
running stale code without anyone being told. This release fixes that and adds what was missing: every profile in one
view, Auto for all of them at once, what an update leaves behind, Hermes' own warnings explained, and the gateway's
memory against its budget.

### Fixed

- **A gateway started by a launcher of its own never ran a learning pass.** The plugin told a gateway by its command
  line (`hermes gateway run`); a launcher that runs `gateway.run` as the main module (runpy, `python -m gateway.run`)
  left argv as `[".../gateway/run.py"]`, so the gateway counted as a short command and claimed no pass, for any profile
  it served. On the audited machine no pass ran in 11 of 12 profiles for 7 days (since 0.12.0): 26 recurring failures
  in one profile never became a lesson, verdicts froze, the health checks went stale. Now the main module, argv[0] and
  Hermes' own record (`gateway.pid`) count too, and a process that did not claim at load asks again every 5 minutes.
  The same fix lets the gateway record which plugin version it runs and preload the plugin's code.
- **Passes no longer start all at once in a gateway serving many profiles.** One pass at a time per process, each
  profile's clock starts at its own delay (0–5 min), and on Linux the pass thread runs at a lower CPU priority than the
  turns.
- **A lesson's verdict needs evidence.** "Effective" meant "half the failure rate before", so 5 failures in 1,769 calls
  before and none in the 20 calls after was effective (the baseline predicted 0.06 failures there). Now the baseline
  must predict at least 3 failures after, and seeing that few must be unlikely by chance (Poisson, p ≤ 0.1); otherwise
  the lesson is not judged yet, and after 28 days it is inconclusive. A verdict the evidence no longer supports is taken
  back.
- **Old "effective" verdicts are dropped.** A lesson judged before 0.12.0's classifier change kept its verdict for ever;
  an "effective" from then is dropped, and a lesson whose failure keeps happening at the same rate while the agent reads
  it (3 or more in 14 days, counted the new way) is ineffective, and retired in Auto mode.
- **Hermes' doctor checks count the room under the gateway's memory limit.** Run from a gateway, the daily check's
  child counts against the gateway's service (MemoryHigh): it now waits when that cgroup has less than 600 MB left,
  not only when the machine does.
- **Status said "the gateway may be down" when it was running.** When no pass ran although the gateway runs, status now
  says that no long-running process claimed the passes, with the gateway's pid. The gateway's line shows the Hermes
  version it runs now (Hermes' own record), not the one the last health check stored.

### Added

- **Every profile in one view: `hermes evolution fleet`** (and the panel's new **Fleet** tab, and option 15 of the
  menu). For each profile: whether the plugin is on, which version runs (and which one the gateway loaded), the mode,
  when the last learning pass ran and whether it is late, lessons active and waiting, failures, turns and slow turns,
  automatic actions; plus the gateway, its memory, what the last update left behind and Hermes' warnings.
- **Auto for every profile at once: `hermes evolution fleet auto on|off|status`.** Each profile with the plugin on gets
  `mode: auto` and `lessons: auto` (and a learning pass every hour where it was off) through Hermes' own
  `hermes config set`, each change in that profile's history; `--owner suggested` also counts as you the channels that
  are yours by construction. The values each profile had are kept, and `fleet auto off` puts them back. `status` lists
  what Auto does by itself (each with its undo) and what it never does. A profile with `fleet_managed: false` is left
  alone. In the panel, the switch is a decision like any other (your dashboard login).
- **The daily report: `hermes evolution fleet report` and `fleet schedule`.** What the plugin learned and did in every
  profile in the last 24 hours, what waits for you, and the fleet view, as a Hermes scheduled task that runs no agent
  and spends no token (`--deliver` picks the channel; local by default).
- **Drift after a Hermes update.** From Hermes' update receipts and its checkout, read-only: the last update (from and
  to which version, its outcome), local changes it parked in a stash instead of putting back, plugins it turned off that
  are still off, services that did not come back; every file the gateway loaded in place of Hermes' own (an overlay) and
  whether Hermes' file changed after the overlay was written (the overlay then hides the new upstream fixes) or the
  overlay changed after the gateway loaded it (a restart is needed); files that differ from the official version; every
  stash an update left, with how much of what it holds is back in place now; each profile's configuration schema
  against the one this Hermes writes.
- **The pin of an overlay's builder (`overlay_pins`).** Name the script that builds a file loaded in place of one of
  Hermes' own and pins its sha256 (`EXPECTED = '…'`), optionally with the guide to rebuild it (`<script>|<guide>`):
  after every update and every 6 hours the plugin reads it (never runs it) and says when Hermes' file no longer
  matches the pin. It also watches the overlay's folder as the gateway found it at its start: a file added or removed
  there (a launcher that checks the folder may refuse the next start), or one that is a link, has a second name or can
  be written by someone else. Nothing is rebuilt or installed by itself.
- **Hermes' warnings in its log, in plain words.** Counted per day with numbers only (never a line of the log): memory
  over the agent cache's budget, the browser tools' browser not answering, the session database's restart race, a
  replaced `state.db`, an incomplete pre-update backup, event loop stalls, the root warning, sessions of profiles that
  no longer exist, contract errors, and `capability_check … decision=deny` (explained as the safe default it is).
- **The gateway's memory against its budget.** The gateway's memory now, its service's MemoryHigh and MemoryMax, the
  agent cache's budget as Hermes computes it, and how often the log saw it passed; when the budget is below the peak,
  what to change (a higher MemoryHigh when the machine has room, a smaller cache when it has not). Never applied by
  itself.
- **Lessons proven in one profile are offered to the others in Auto** that call the same tool. They arrive waiting
  and activate there only once that profile's own turns hit the failure. Only template lessons travel.
- **A lesson whose tool nobody calls any more is obsolete.** One Auto activated is taken back (and returns if its
  failure does); one you approved gets a proposal to retire it.
- **Template lessons of one cause on several tools share one sentence** in the prompt ("`terminal`, `tool_search` and
  `skill_manage`: this kind of command is blocked…"), so the prompt's 8 lines reach more causes.
- **What a pass and a hook cost, in detail.** Each pass records the seconds of each phase, and the hooks' cost now
  separates the plugin's own CPU time from the wait for the interpreter or the disk.

### Unchanged principles

No network access of its own, no telemetry, and it never updates itself or Hermes. Nothing here changes Hermes' code,
an overlay, a stash or a service: drift, warnings and memory are read and explained, and the commands are yours.

### Upgrading

`hermes -p <profile> plugins update hermes-atlas-evolution` in every profile (or `hermes evolution update --all`),
then restart the gateway once, so it loads the new code and starts claiming the passes. The test suite has 838 tests,
on the `tests` branch.

## 0.13.0

Ready for the Hermes plugin catalog: the plugin no longer updates itself or reads the network, Assist is the default mode, and three audits (catalog rules, the hooks inside Hermes, the background work) found the bugs fixed below.

### Changed: ready for the catalog

- **No self-update and no network access of its own (catalog rule 3).** `git ls-remote`, the Mode auto's sync of the plugin's copies across profiles and every "move to the latest commit" are gone, from the plugin and from `install.sh`. Only Hermes decides what code a profile gets: `hermes -p <profile> plugins update hermes-atlas-evolution` moves a catalog install to the catalog's reviewed pin and pulls a git install from the source it came from. `hermes -p <profile> evolution update` runs that command in each chosen profile (the one running it first); a copy Hermes pinned with `--ref` is moved only to the commit the profile running the command runs; a failed update is reported, never followed by a reinstall; `--force` is no longer passed to an update (it only skipped Hermes' scan prompt). For unattended updates, use Hermes' own `plugins.auto_apply`.
- **Assist is the default mode (catalog rule 12).** On its own the plugin now only removes an entry repeated word for word between `MEMORY.md` and `USER.md`. Auto is your explicit choice: `hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.mode auto`. A profile with no `mode` set switches to Assist when it updates.
- **The Mode auto installs the plugin by itself only from the catalog.** In a profile in use that never had it, by its catalog name (the reviewed pin), when the lead profile is a catalog install. From the repository, at the lead's own commit, only with your key.
- **A catalog install is never "pinned" for the plugin:** `plugins update` moves it, so status, the update and the suggestions treat it that way.
- **`install.sh` runs only Hermes' own commands:** `plugins update` where the plugin is, `plugins install` where it is missing (from GitHub, or from the catalog with the new `--catalog`), and a pinned copy is left as it is with the command that moves it. The example that piped a download straight into a shell is gone from the script.
- **Commands the plugin starts never inherit an approval bypass:** `HERMES_YOLO_MODE`, `HERMES_YOLO`, `HERMES_ACP_AUTO_APPROVE`, `HERMES_ACCEPT_HOOKS`, `HERMES_TUI_NO_CONFIRM` and `CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS` are dropped from every child's environment, the weekly digest's script included (a digest scheduled before keeps its old script until you schedule it again).
- **Credential files are never touched (catalog rule 11).** `.env` and `auth.json` are no longer profile markers, and no scan, size or settings watch looks at a `.env`, `*.env` or `auth.json` file, not even to check that it exists (`install.sh` too). A folder holding only such files is no longer a profile.
- **The panel decides only on Hermes' verdict.** Without Hermes' own login verdict, a cookie with a login's name no longer grants decisions: the panel answers read-only.
- **README in English first, then Portuguese, in plain Markdown,** with a Disclosure section (what it reads, writes and runs, and what the agent sees) and images pinned to a commit, so the catalog's plugin page shows it whole.

### Fixed

**Runtime guard**

- **A pattern could freeze the whole gateway.** `pkill -f '.*.*.*.*X'` made the guard's search of the gateway's own command line take 75 seconds and more, holding Python's lock: every session stopped, then Hermes blocked every tool. Costly patterns are now simplified or matched in one linear pass, only patterns of up to 64 characters reach the regex engine, and 3,000 random patterns take at most 0.15 s.
- **Padding no longer hides a command.** Here-document bodies and comments are taken out in one linear pass over the whole text (up to 500,000 characters), and a command longer than 20,000 characters is read at its beginning and its end.

**Stability**

- **Two learning passes never run at once.** The pass now renews its lease while it works and stops if another process took it; before, a long pass outlived its lease and a second one started beside it, and the first one's result was lost.
- **No more "database is locked" in the learning pass.** Under load (the gateway writing while the pass rebuilt the proposals) 34 of 60 passes failed and were postponed 10 minutes; now none.
- **The daily health checks keep to the pass's time.** They need 5 minutes left, receive the pass's deadline, and the Hermes check stops at 2 minutes; before, they could end minutes past it. The database quick check keeps to its 5 seconds (it took 9.9 s on a 441 MB `state.db`).
- **A command's output is bounded.** Its beginning and end are kept, with a "…" line between (400 MB of output took 1.2 GB of memory; now 16 MB).
- **A command that starts a helper is not "did not answer".** A Hermes command that ended well but left a background helper holding its output was reported as a timeout and the helper was killed; now it ends when the command does and the helper is left alone.
- **The doctor's checks end with the plugin.** The child running Hermes' doctor checks stops when the plugin's process dies, or at its timeout plus 15 seconds.
- **`hermes pm gc` waits when it cannot see what runs.** Without `/proc` (macOS, Windows) or when the scan of running programs ran out of time, the Mode auto, `care gc` and the menu hold gc back and say why ("which programs run could not be read").
- **Long sessions are read back in parts.** The first pass's backfill keeps to its time budget inside one long session (150,000 calls: 47 s and +223 MB before, 20 s and +20 MB now) and goes on from there at the next pass.
- **Under `plugins.isolation: host`** the learning pass and the cleanup run in the plugin host, and `status` sees the host's version.

**Efficiency**

- **Lighter writes on every tool call.** The de-duplication table `seen` is replaced by a small memory in the process: about 7 times less database growth, 2 instead of 5.6 journal frames per call. Journal checkpoints moved off the hooks to the 10-minute clock, and the journal shrinks back to 1 MiB.
- **One process serving many profiles** (9 or more) no longer reopens the database on every write: 1.3 ms → 0.09 ms.
- **A much faster scan of a large Hermes.** 20 profiles with 60,000 skills: 12 s → 2.6 s on a warm scan; 5,850 skills: 1.6 s → 0.4 s. Each plugin's code is parsed once per process.

**Learning**

- **New lessons reach the prompt.** The playbook now puts first the lessons whose failure happened in the last 30 days; before, old lessons kept the 8 lines, so new ones were never shown, measured or retired.
- **An automatic lesson nobody saw expires.** A lesson the Mode activated by itself, never shown to the agent, whose failure has not come back for 30 days, is withdrawn; it returns if the failure does. Lessons you wrote, approved, kept or imported never expire.
- **The decision history stays bounded.** The newest 1,000 entries always stay (the history shows 200); beyond that, entries older than 90 days are removed.

### Upgrading

See [Updating to 0.13.0](docs/en-US/upgrading.md#updating-to-0130). The test suite has 783 tests, on the `tests` branch.

## 0.12.0

A full audit of 0.11.0 and its fixes: safer automatic actions, a stronger runtime guard, lessons judged on what the agent really read, and the test suite back in CI.

### Fixed

**Hermes 0.23.2**

- **Your own commands work again under Hermes 0.23.2.** Its launcher runs `python3 -I -c '<launcher script>' evolution …`, which the plugin did not recognise as this command's own launcher: every command only the owner may run (`owner`, `care doctor`, `care pm-doctor`, `update`, approving or retiring lessons) was refused in the owner's own terminal, and `lessons list` hid the owner's lesson text. The launcher is now recognised; a command from an agent's shell under a gateway is still refused.

**Automatic actions (Mode auto)**

- **A second gateway is never started.** A gateway Hermes reports as `degraded` (no chat platform connected yet; scheduled tasks still run) or `starting` now counts as running. Before, the Mode auto could repair a leftover service and start a second gateway beside a live one. Whether a gateway runs is read from what it leaves behind: its process (pid with its start time or command line), a fresh heartbeat, `gateway.pid`, the lock it holds on `gateway.lock` (read from `/proc/locks`) and `host-gateway.json`. While any gateway of this Hermes runs, nothing is repaired by itself.
- **One check before any start.** The Mode auto and `care fix-gateway` look at every `hermes-gateway*` service of both systemd scopes (user and system) and refuse when one is active, activating, reloading or deactivating, when a scope cannot be read, or when the service runs another `HERMES_HOME`. The Mode auto also never starts a legacy per-profile service.
- **The right service, never production's.** Service names follow Hermes' own rule: the bare `hermes-gateway.service` only for the account's own `~/.hermes` (or the home the bare service pins), `hermes-gateway-<name>` for `profiles/<name>`, `hermes-gateway-<hash>` for any other home. Before, a staging or test home could rewrite and start the production gateway's service. The service's `HERMES_HOME` is checked before its file is written, and for a profile the root's gateway serves, the root's service comes first.
- **The repair follows the whole service.** Paths with spaces in `ExecStart` are read as Hermes reads them, every `Exec*=` line that names the old program is rewritten (`ExecStop`, `ExecStopPost`…), and the service starts with `--no-block`: the plugin waits up to 180 seconds and says "still starting" instead of "failed".
- **Every gateway repair can be undone, one at a time.** A journal of the latest 40 repairs and undos (`<root>/evolution/gateway-repair.json`) lets `care undo-gateway` work from any profile and go back one repair at a time to the original file; it refuses when the file changed since. A repair you undid is never repeated by the Mode auto.
- **`hermes pm gc` read the way Hermes runs it.** Hermes deletes every folder of the tool store that `tools/facts.json` does not name, whatever its name. Any folder a service (any `Exec*=` line) or a running program (`/proc/*/exe`) uses now blocks it, a missing or unreadable `facts.json` blocks it (it would delete every tool), a systemd scope that cannot be read blocks it, and the check runs again right before gc. It follows Hermes' environment generations and the store a profile that borrows another's really uses. When the folder in use is a build Hermes does not manage (a Python of your own that a service runs on purpose), the advice says so and no longer offers switching the service to Hermes' Python as the fix.
- **Only the code the lead runs is spread.** The plugin installs itself into a new profile only while the repository's latest commit is the one the lead runs (it waits otherwise, as the sync does), and the fallback reinstall never replaces a fork's copy with the published one.
- **Memory repeats.** Assist and Auto take out only an entry repeated across `MEMORY.md` and `USER.md` (Hermes already drops a repeat inside one file), and only after checking, under Hermes' lock on both files, that the copy kept is still there: the last copy is never removed. Nothing is removed when `config.yaml` was not read, and the file keeps its mode and owner. The memory review is linear: 2,000 entries took 21 s and now take 0.2 s; a 512 KB file takes 0.4 s (it took hours).
- **Safe reads everywhere.** A named pipe, or a link to `/dev/zero`, named `SKILL.md` no longer hangs or floods the learning pass: files are opened without following links and without blocking, only when they are regular files, up to a size limit.
- **Undo.** An older decision can no longer be undone over a newer one on the same proposal (undoing an old rejection no longer reopens what you rejected later). Undo writes down that the change must not come back before it runs Hermes, so a crash in between cannot lose it. A decision whose trash entry was emptied or whose memory copy was pruned is no longer offered as undoable.
- **Profiles as Hermes finds them.** A `~/.hermes` that is a link stays `default`; `profiles/default`, deleted profiles (`profiles/.deleted`) and folders without Hermes' identity files are not profiles, and a deleted one is refused.
- **Windows.** `hermes.exe` is found beside the launcher, in `.hermes/bin` or in the virtual environment's `Scripts` (a `.cmd` only as a last resort, quoted); a timeout ends the whole process tree; read-only git pack files are deleted from the trash; the shared fleet state has a real lock. On POSIX systems each command runs in its own process group, and a timeout ends the group.
- **An unread `config.yaml` means Manual.** When `config.yaml` cannot be read (no PyYAML, over 1 MiB, a YAML error), the mode is Manual and `status` says so first. Before, the standalone runner without PyYAML ran the Mode auto although `config.yaml` said manual.

**Runtime guard**

- **Whatever the verb.** Writing to, truncating or changing the attributes of anything inside the running Python, Hermes' code or this plugin is vetoed (`truncate`, `dd of=`, `tee`, a redirect, `chmod`, `chown`, `chgrp`, `ln -sf`, `rsync --delete`, and `cp`, `mv` or `install` into them). Above them, and at or above the profile's folder or Hermes' root, only `chmod`, `chown`, `chgrp`, `ln` and `rsync --delete` are vetoed. A `cd` earlier in the command and literal `VAR=` assignments are followed for these too. Plain copies into your home folder, into the Hermes root or into a shared folder such as `/usr/local/lib` stay allowed. Replacing a Hermes `config.yaml` (the profile's, the root's or another profile's) with `cp`, `mv` or `install` is vetoed; appending to it with `>>` counts only when the command names the plugin's settings.
- **Fast on long commands.** A long command could take up to 41 s to check (a pipeline with many `| xargs`, for one), and Hermes blocks a tool after 30 s; any command up to 20,000 characters now takes under 0.7 s, and one budget bounds every nested check.
- **Reads deeper.** Nesting up to 8 levels (it was 3); a literal `VAR=value` or `export VAR=value` before `$VAR`; `watch '…'`; `cat <<EOF` piped into a shell; `<<<'…'` glued to the shell; busybox and toybox applets; a command word with a glob (`sys*ctl`); relative paths after `cd /`.
- **Padding no longer hides a command.** A trailing comment or a huge here-document body is cut before the 20,000-character limit, so it cannot push the command past the check.
- An internal failure of the guard is now logged, once per process. The command still goes through: blocking every tool would be worse.

**Learning**

- **Failure classes read the way a person would.** In a traceback only the exception lines count; in command output only its last lines; an HTTP code only where it reads as one (`HTTP/1.1 429`, `status code: 401`); "timeout" only as a phrase. Before, `1 failed, 429 passed` became "rate limited" and a traceback with `line 403` became "auth", and both made wrong lessons.
- **A lesson is judged from the first time the agent read it**, in its prompt or in a tool hint. A lesson never shown is not judged (the prompt shows at most 8 lessons and 1,200 characters). Before, lessons that were never shown were retired or promoted.
- A per-day baseline divides by the days the failure was really observed, not always 14, and the retire text says whether it compared failures per call or per day.
- The lessons part of the Evolution index also counts the lessons retired in the last 7 days, so retiring a lesson that did not help no longer lifts the score.
- Usage and cost above 20,000 records give partial totals ("at least US$ X", the newest 20,000 records) instead of nothing.
- The "restart needed" notice compares with what this load registered, and turning the reflector on while Hermes runs says "on after `hermes gateway restart`" instead of looking unavailable.
- Only long-running processes claim the learning pass (the gateway, the dashboard, `hermes serve`, the TUI or Desktop, an ACP server, an interactive CLI): one-shot runs (`hermes -z`, scripts, Kanban workers) no longer cut passes short.
- A new session no longer gets the lessons as they were before a pass changed them, and the digest recipe no longer swallows the day's notice of a failure that jumped.

**Privacy and trust**

- `settings.json`, which the agent's file tools can write, can no longer turn `tool_hints` on or raise `reflector_daily_limit` above 6. Those count only from Hermes' configuration, like the owner ids, a mode other than manual, the agent tools and switching the guard off: `settings.json` can only narrow.
- A dashboard without a login hides the text of lessons written by you, the reflector or the agent (lessons, waiting lessons, proposals, the digest), as the terminal does in an agent's shell: "(text hidden: read it in your terminal)".
- An unexpected error in the panel answers `unexpected` with a plain sentence; the details go only to the dashboard's log.

**Commands, menu and panel**

- Every command the plugin prints names its profile: `hermes -p <profile> evolution …`. The menu recognises every form.
- A proposal's lesson is shown in the reader's language. `proposals reject` on a lesson says what happens: the lesson is turned down for good; for a proposal to retire a lesson, the lesson stays active.
- A number out of range gets one short sentence (it listed 500 choices); argparse errors are in Portuguese with `--lang pt`; `--help` follows `--lang`; a missing `--home` is said in words.
- `environment` honours the mode set in `config.yaml`; `owner` takes `--format md|json`; an empty `proposals list --all` says "No proposal yet."; `lessons review` names the origin (yours, the agent's, a reflector draft).
- The menu shows no colour with `TERM=dumb`, with `NO_COLOR` or outside a terminal; the learning pass time comes from its real deadline (12 minutes, not 2); attention items name the letter of their suggestion. Portuguese says "rodada" and "Pede atenção".
- Gateway states in words: "running (degraded: no chat platform connected yet; scheduled tasks still run)" and "starting".
- `status` always renders, and lists invalid settings and an unread `config.yaml` under Needs attention.
- The help line of `hermes evolution` no longer names the removed operations stack.

**Documentation**

- The quick start for every profile needs `--install-missing` on a first install.
- "No network of its own" was wrong: the only network access is `git ls-remote` of this repository, for the Mode auto's sync and `update`. There is no telemetry.
- The lists of what Auto mode does include the gateway repair and the plugin's sync; SECURITY.md says that the gateway repair edits a systemd unit file and runs `systemctl`.
- The panel's Proposals tab has **Apply now**, **Apply all** and **Dismiss**. macOS and Windows: everything except the apt and systemd parts, tested in CI, not inside a gateway.
- CONTRIBUTING rules 5 and 6 match what the plugin does, and the issue templates ask for the current version and name every area.

### Added

- **Your lessons in your direct chats.** In a direct chat (a DM) whose sender is you (`owner_sender_ids`), the lessons the chat's prompt leaves out (written by you, the reflector or the agent) come with your first message, once per chat, at most 5 lines and 900 characters. Never in a group, thread or topic, where the model's history could keep them for someone else later, and never for anyone else.
- **Events for other plugins and dashboards** on Hermes' event bus (`ctx.emit`): `hermes-atlas-evolution:lesson_activated`, `hermes-atlas-evolution:lesson_retired` and `hermes-atlas-evolution:failure_spike`, with ids and numbers only.
- **`on_session_reset` and `on_session_delete`.** What the plugin keeps in memory about a session goes as soon as the session is reset or deleted (16 hooks).
- **The health finding `gateway_program_deleted`.** The gateway runs, but the program its service starts was deleted under it (Hermes replaced its Python), so the next restart fails. `care fix-gateway` fixes it now. It counts in the Evolution index.
- **`fleet_managed`** (on by default). Off, the lead profile never installs, updates or moves anything to the trash in that profile by itself; it still shows what it finds. `settings.json` can only turn it off.

### Changed

- **The test suite runs in CI again**, on Linux with Python 3.11 to 3.14, on macOS and on Windows: over 700 tests. It lives on the `tests` branch, because Hermes clones the default branch into every profile, so installs stay small; a branch named `tests-<branch>` runs instead, with a pull request from that branch. CI also checks the panel's script.
- **`install.sh`** runs from one function, so a download cut short runs nothing. It reads the latest commit once, reads Hermes' install record as JSON, takes the end of input at its question as "nothing" (it was "every profile") and follows Hermes' rules for what a profile is.

### Upgrading

Nothing to do by hand. [Updating to 0.12.0](docs/en-US/upgrading.md#updating-to-0120) lists the details: the database gains a table, `tool_hints: true` and a reflector limit above 6 move from `settings.json` to Hermes' configuration, and the new `fleet_managed` setting.

## 0.11.0

An index that explains itself, an install Hermes' own scan calls safe, and the deprecated stack removed.

### Added

- **Why the Evolution index moved.** When a part moved 5 points or more against a week ago, `status` and the digest say which part and what drove it: the tools whose errors grew or fell the most, failed and slow turns, your corrections, the lessons that did not help, the health problems found now (`What moved it: tools 92 → 62 · terminal +147 errors`).
- **A failure that jumped today.** A failure that already happened at least 5 times in the UTC day and 4 times its daily average of the 14 days before is listed under what needs attention, with its tool and both counts: a dependency, a key, a service or a quota that just changed, said the same day. A quiet scheduled run (`learn --notify-only`) tells each one once per day.
- **Do first.** Up to 3 steps ranked by the index points each could add, estimated with the index's own formula: a lesson waiting for you, a recurring failure without a lesson, a health problem. Each has its command. In `status`, in the digest and in the panel's Overview ("What the numbers say"). No model call: a few indexed queries over 7 to 14 days.
- **Self-repair of `lessons: false`.** `hermes config set ... settings.lessons off` stores the boolean false, which Hermes reported at every load and its settings form showed as no choice. The plugin now writes the text `off` back once, with Hermes' own writer for its settings (`ctx.set_config`).
- **An old tick job is found.** A job of Hermes' scheduler that still runs `evolution ops tick` or `evolution tick` fails at every run in 0.11.0: `status` lists it with the exact `cron remove` command.
- **The notes of 0.1 can be closed.** Next to the read-only command that prints them, `status` gives one that saves them to `evolution/notes-0.1.json` and removes them from the database; the item then goes away.

### Fixed

- **Hermes' install scan says `safe`.** 0.10.x got `caution` from Hermes' plugin scan (`plugin-guard-v15`), because of advice strings that showed `sudo loginctl`, `sudo reboot`, `sudo dpkg-reconfigure` and `sudo journalctl`, and two CHANGELOG examples of a pipe into `sh`. A caution install waits for a yes from a terminal, so the plugin's own install into another profile (Mode auto, `update --all`) was refused there. The advice now reads `loginctl enable-linger $USER` (it asks for a password when it needs one), `sudo systemctl reboot`, `dpkg-reconfigure -plow unattended-upgrades` as root and `sudo systemctl status <unit> -n 30 --no-pager`. The plugin never runs any of them itself; a new test keeps such strings out.
- **Plugin host** (`plugins.isolation: host`): the `evolution` CLI command is no longer registered there (Hermes skips it with a warning at every load), and `status` says the runtime guard is off there even when it is read inside the host.
- **A hosted scope that refuses a profile switch** (`ScopeSwitchRefused`) no longer fails the learning pass: the pass runs in the scope it is in.
- **The panel's tab** comes after Skills: Analytics is hidden by default, so `after:analytics` put it last.

### Removed

What 0.10.0 marked as deprecated, about 5,300 lines and 14 modules (`supervisor`, `linux_actions`, `learning`, `reasoning`, `inventory`, `linux_host`, `workloads`, `ops_metrics`, `semantic_ops`, `advisory`, `native_actions`, `diagnostics`, `memory`, `native_signals`):

- the commands `ops ...` (the Linux operations supervisor of 0.3), `health` (use `care check`), `tick` (the pass runs by itself; `learn` runs one now), `feedback`, `history` (use `decisions`), `memory remember|recall|audit|forget`, the `/evolution` chat command and the automatic recall of the 0.1 notes into turns;
- the hooks `subagent_stop`, `post_curator_run` and `on_kanban_worker_exited` (14 hooks remain);
- the settings `sensitivity`, `inspect_shared_kanban`, `kanban_handoff`, `linux_supervisor`, `semantic_incident_model`, `daily_estimated_budget_usd`, `legacy_memory_commands` and `automatic_shared_recall`, now ignored without an error;
- `supervisor.sqlite3`, no longer written; the standalone runner ignores `--hermes-version`;
- the Operations pages of the docs: `usage` and `rates_usd_per_million` moved to [Usage and cost](docs/en-US/evolution-loop.md#usage-and-cost).

### Documentation

- [Updating to 0.11.0](docs/en-US/upgrading.md#updating-to-0110): what replaces each removed command, the old cron job, the 0.1 notes.
- The reflector's route, as Hermes resolves it: the compression task's provider and model reach it only with `plugins.entries.hermes-atlas-evolution.llm.allow_task_override: true`; otherwise set `auxiliary.evolution_reflector`.
- The hook tables list the 14 hooks of `plugin.yaml`; every link and anchor was checked in both languages.

## 0.10.1

A leaner plugin and documentation rebuilt in two languages.

### Changed

- **A lighter install.** Hermes installs a plugin with `git clone --depth 1` of its default branch into every profile, and the README's images (about 3 MB) went with it, unused. They now live on the repository's `assets` branch and the README and the docs load them from there: each profile's copy dropped from about 5.2 MB to about 2.2 MB.
- **Documentation in en-US and pt-BR.** The English pages moved to `docs/en-US/`, side by side with `docs/pt-BR/`; each language has a documentation home, a new **Getting started** guide (install, the first hour, making it yours, every week, update and remove) and a new **Troubleshooting** guide (the gateway after a Hermes update, pinned profiles, a plugin that does not load, the Evolution index, the plugin's own cost, a damaged database, the runtime guard, settings that wait for a restart, a full removal). Every link was checked.
- **A modern README** in both languages: what it is and why, highlights, a diagram of how it works, the modes, compatibility, and the screens.
- **CI** on `actions/checkout@v5` and `actions/setup-python@v6`, the current Node 24 runtime of GitHub Actions.
- The Operations page says it is deprecated (removed in 0.11.0).

## 0.10.0

The agent's evolution as a number, a plugin that measures what it costs, and the same work done with less: a
profiling pass over the hooks, the learning pass and the look at every profile, and an audit of every module.

### Added

- **The Evolution index** (`evolution_index`): one number from 0 to 100 per profile and per UTC day, from what the plugin already measures over the last 7 days: tools (calls that did not fail), turns (neither failed nor slow), you (how rarely you corrected the agent or asked again), lessons (of the judged active lessons, the share that halved their failure) and health (Hermes' and the server's checks: what stops the agent costs 25 points, what to fix soon 8, housekeeping nothing). It is the weighted mean of the parts that have data (at least two), stored by each learning pass (90 days kept), and shown with its parts and its trend against a week ago in `status`, the digest and the first tile of the panel's Overview.
- **What the plugin costs the agent** (`selfcost`): every hook is timed where it runs (about a microsecond of measuring), each process writes its numbers every 10 minutes, and `status` shows the median and the p95 per tool call, per turn and per prompt. Above 50 ms of p95 over 200 calls, `status` says so.

### Faster

- The lessons section of the prompt is rendered at most every 30 seconds per kind of session, and again at once after a learning pass or a settings change in the same process: it was read from the database on every prompt (3.8 ms to 0.02 ms per prompt).
- A tool call's counters and outcome are one write transaction instead of two, each update still in a savepoint of its own; the shared writer checks the database file's identity every 2 seconds instead of on every write (0.5 ms to 0.3 ms per tool call).
- Near-twin skills are compared as bitmasks, sorted by size so a set only meets those it can match (8 times faster on 400 skills, the same pairs); a `SKILL.md` is read and hashed again only when its size or change time changed.
- The modules only the deprecated commands use (`diagnostics`, `memory`, `advisory`, `native_actions`) are no longer imported by every Hermes process at load.

### Fixed

- One name for a profile, one root, one list of profiles everywhere (`environment.root_of`, `environment.profiles`, `admin.profile_name`): a linked profile got its target's name in `care` and in an exported pack, and `status` listed folders (links, names Hermes refuses) that the Mode auto never looks after.
- `care --help` names `fix-gateway` and `undo-gateway`; the `ops` and `memory` subcommands have a help line; `ops review --days` is documented as 1 to 35, as the command accepts; the package's docstring carries its name.

### Deprecated (removed in 0.11.0)

- `health` (use `care check`), `history` (use `decisions`), `tick`, `feedback`, `memory` and the `/evolution` chat command (the 0.1 notes; `memory audit` lists them), and `ops` (the 0.3 Linux supervisor and its trials). None of them is used by default, by the menu, the panel or the learning pass; each now says so when it runs, on stderr.

### Removed

- The agent tool `hermes_atlas_evolution_plan`: it returned fixed text and read no data. `hermes evolution ops plan` prints the same plan until 0.11.0; `agent_tools` now gives the agent the lessons tool alone.
- Code nothing used (`environment._similar`, `hermes_care._in_use`, `admin.LISTS`, `explain._FAILURE`, `logwatch.RESULTS`, `proposals.RISKS`).

## 0.9.2

Self-repair and every profile kept current by itself. A second full review (six independent passes over the whole plugin) fixed what 0.9.1 still got wrong, starting with pinned profiles, which 0.9.1 reported as updated while Hermes had kept them on the old commit.

### Added

- **Every profile on the same code, by itself (Mode auto).** The profile that looks after the whole Hermes brings the plugin's copy in every other profile to its own commit: a copy pinned to another commit, an older one, one on other code with the same version, or one an interrupted update left half-installed. At most two profiles per pass, once per situation, retried a day later if Hermes refuses; never where the plugin is off, where that profile's own mode is Assisted or Manual, or in a development link. Nothing new is downloaded by itself: a copy that is not pinned is updated only while the repository's latest commit is the lead's. Each sync is in the history.
- **Self-repair of the plugin's database.** A damaged `evolution.sqlite3` (a torn write, a full disk, a sync tool copying it mid-write) used to stop every pass, lesson and counter for as long as the process ran. The damage is now confirmed with SQLite's own check, the file is set aside as `evolution.sqlite3.corrupt-<time>` (the two newest are kept) and a new one starts; the log says so.
- **One version per running gateway.** A gateway or dashboard imports all of the plugin's modules at start, so an update written to disk while it runs can never mix new modules with old ones in that process.
- **The gateway repairs itself (Mode auto).** When the gateway's own user service names a Python Hermes removed (systemd `status=203/EXEC`, a restart loop) and no gateway runs, the profile that looks after the Hermes points it at the Python Hermes uses now, as `care fix-gateway` does: a copy of the file is kept and the history shows it with its undo (`hermes evolution care undo-gateway`). At most once a day; never while a gateway runs, for a unit that is off and not failing, or in system scope.
- **Retention without the autopilot.** With `autopilot_minutes: 0` the database still drops old counters and the reflector's excerpts once a day.

### Fixed

- **Pinned profiles were never updated.** Hermes keeps its install records in `plugins/.install-metadata.json` (one record per plugin), keeps a pin when a plugin is installed again without `--ref`, and refuses `plugins update` on a pinned copy. `hermes evolution update`, `install.sh` and the advice read the pin from the wrong file and reinstalled without `--ref`, so Hermes reinstalled the pinned commit and the profile was reported as updated. A pin now moves to the latest commit with Hermes' own `plugins install <url> --force --ref <commit>`; a copy without a manifest is installed again; `--enable` is added only where the plugin is already on, so an update never switches it on where its owner switched it off; a development link is never touched.
- **`hermes pm gc` could delete the Python the gateway's service still starts from** (once Hermes' `facts.json` had moved on, the folder was only hidden from the report). The health check now finds such a folder and says so, and `pm gc` is blocked (Mode auto, `care gc`, the menu) until the service points at the current Python, which `care fix-gateway` does.
- **Gateway repair and status:** `care fix-gateway` looks at the unit Hermes names for this profile (`hermes-gateway-<profile>`, or the root's when it serves the profile) instead of one fixed name, refuses where systemd cannot be asked (under `sudo`), leaves a leftover unit alone while another gateway runs, only reloads when the file was right already, and keeps its undo record; afterwards the status stops saying the gateway cannot start (and says it again after an undo). Unit files are written with fsync.
- **Linux checks:** a `dpkg --audit` or `apt-get check` cut by the time budget is "not checked", never "broken"; systemd 254's bus error and unexpected exit codes leave the scope unverified instead of empty; a program this user cannot look at is never called missing; the gateway unit of this profile is picked, never another profile's; a named profile served by the root gateway reads the root's state; a system upgrade run is bounded (`RuntimeMaxSec`).
- **A proposal you rejected was applied by itself 30 days later** in Mode auto (it reopens for you to see; the automatic pass did not know you had said no). A rejection now keeps it from ever being applied by itself, in every profile; undoing the rejection lifts that.
- **Operator-only commands:** an agent could pass the check after clearing the environment markers when a Hermes ancestor's arguments contained the word "evolution" (a profile named evolution, a one-shot prompt mentioning it). Only this command's own launcher is skipped now, read from the shape of its arguments.
- **Runtime guard:** commands that restart or kill the gateway, delete Hermes or change this plugin still passed when written as: a line continuation (`\` at the end of a line); a `#` inside a word (a URL, `$#`) read as a comment; a `<<EOF` inside quotes or a comment; a shell that reads its commands from a heredoc, `<<<` or a pipe (`bash <<'EOF'`, the output of `echo` piped into `sh`); `kill -- -$PPID` or `pkill -u $USER`; `cmd /c` and `powershell -Command` with Windows paths, `taskkill /IM python.exe`; `find ... ! -name X -delete` or `-o`; `grep -F hermes | xargs kill`; a write into the plugin's own files (`sed -i`, `: >`, `tee`). All are vetoed now. It no longer blocks `find ~/.hermes -name '*.log' | xargs rm`, `cd ~/.hermes && ls logs/*.log | xargs rm` or reading `config.yaml`, and a command with many `{a,b}` groups no longer takes minutes to check (a stalled check blocks the tool).
- **Text hidden from agent shells:** `report` and `learn` printed lesson texts, drafts and proposals' lessons that `lessons list` hides from an agent's terminal; they hide them too now.
- **Memory:** the automatic duplicate removal could take out the only copy the agent sees (the same entry in `MEMORY.md` and in a `USER.md` Hermes does not load); a removal whose record could not be written failed the whole pass.
- **Learning:** a failure the reflector declined was asked about again on every pass, so the same few failures took the whole daily budget; one damaged session in Hermes' history stopped the replay for ever (and with it the lessons' baselines); a manual pass had no deadline and could outlive its lease; the Curator ledger was read under a write lock.
- **Dashboard:** another profile was judged against the dashboard's own plugin version and could show its plugin list; the owner's sender ids reached read-only sessions; the panel and its refusals could speak different languages; an id too large for SQLite gave a bare 500; an operating-system permission error was called "managed by your administrator"; the digest's cost now says when it includes sessions that began before the period.
- **Command line:** every printed command names its profile (`hermes -p default ...`), because Hermes may follow a sticky profile; a command that failed (`update`, `care`) exits 1, so `hermes evolution update --all --yes && hermes gateway restart` no longer restarts after a failed update; Ctrl-C prints a sentence, not a traceback; the no-terminal hint repeats your own request; `owner add/remove` refuses where the current owners cannot be read instead of printing a list that would drop them; cron and maintenance decisions are described correctly; an empty `HERMES_HOME` is no profile.
- **Windows:** `NUL` is no longer taken for a terminal, and the language follows the Windows locale.
- **Profiles:** a profile folder that is a link to another place is no longer taken for the default profile (which made it a second lead).
- **`install.sh`:** every profile runs with its own `HERMES_HOME` and `--profile` (a sticky or exported profile no longer updates the wrong one); a profile name Hermes would refuse, a name given twice, a `profiles` folder higher up the path, an unset `HOME` and a missing terminal (a script piped from `curl` into `sh`, without `-t`) are handled; the gateway restart never reads the rest of a piped script and its failure shows in the exit code.

### Removed

- Switches that only the test suite used: `ATLAS_EVOLUTION_NO_HERMES` and `ATLAS_EVOLUTION_HOST_CHECKS`, and the references to test files in the code and the documentation. The CI workflow is `checks.yml`.

## 0.9.1

Stability release: a full review of the plugin fixed what could hang, leak, lose data or give wrong advice, and the plugin now updates itself across profiles and repairs the gateway incident it used to miss.

### Added

- **`hermes evolution update`**: updates the plugin in every profile (`--all`), in the ones you name (`--profiles default,work`) or in the ones you pick from a list. A profile pinned to a commit is reinstalled without the pin; a refused update falls back to a forced reinstall; one failing profile never stops the others. `--list` shows each profile's version, commit and pin; `--install-missing` also installs where it is missing.
- **`install.sh`**: the same, without the plugin loaded (first install, or a broken copy). `sh install.sh` asks which profiles; `sh install.sh --all --yes --restart` does everything and checks the gateway's service before restarting it.
- **The gateway that cannot start.** When Hermes replaces its bundled Python and the gateway's service still starts the old one (systemd `status=203/EXEC`, a restart loop, `hermes gateway restart` waiting minutes), `status`, `care` and the panel now say so, with the file that sets the old path and the commands that fix it. `hermes evolution care fix-gateway` repairs it (a copy of the file is kept; `care undo-gateway` puts it back). A restart loop, a gateway stuck in starting/restarting and stale health data are reported too. `hermes pm gc` advice never offers a runtime a service still uses.

### Fixed

- Auto mode installed the plugin in other profiles with `--ref <commit>`, which pinned them, so `hermes plugins update` failed there with "is pinned to". It installs without a ref now; pinned or out-of-date copies are reported with the command that clears the pin.
- Hook path: one bad record (a broken character in a tool error) no longer stops all recording until a restart; the daily cleanup no longer runs inside a tool call; turn tracking no longer stops when feedback recording fails; closed storage handles are not reopened; long messages and per-turn caches are bounded; a plugin reload no longer keeps the old one in memory.
- Runtime guard: no longer blocks killing your own processes because a path contains "hermes" or because of `-u root`; `find ... -name X -delete` under the Hermes folder is allowed; here-strings, deeper nesting, `$(...)` inside quotes and pathological patterns are read correctly; `reboot`, `shutdown`, `systemctl reboot` and `loginctl terminate-user` count as stopping the gateway.
- Linux checks: user-scope units are read with the right session bus as root, timed-out commands no longer leave children behind, the update check respects its time budget, reports are written atomically, `unattended-upgrades` counts as busy, a live gateway is not called crashed when Hermes did not record its start time, `hermes pm gc` never outlives the maintenance lease.
- Smaller fixes: skill locks found in deep folders, the watchdog finds `hermes` outside PATH, malformed panel requests answer with a message instead of a bare 500, duplicate digest jobs, `decisions list --limit` and `ops review --days` ranges, the terminal Environment screen shows the same mode as the panel.

### Removed

- The test suite is no longer shipped with the plugin.

## 0.9.0

Atlas Evolution reads Hermes' own doctor for you every day, on every profile, and speaks your language everywhere: the panel, the menu, every command and the settings form. An audit of every action it takes, and two independent reviews, fixed what they found.

### Added

- **Hermes' doctor, every day** (`doctor.py`). Once a day, for each profile in use, the profile that looks after the whole Hermes runs Hermes' own `hermes doctor` checks in a short-lived process of Hermes' Python, with the profile's home and a clean environment, without `--fix` and without the five checks that use the network, take minutes or reach outside (provider probes, npm audit, the dashboard import, the Skills Hub, the terminal backend). About 7 s and 210 MB per profile, one at a time, skipped while the machine has less than 600 MB available. Nothing is changed: like `hermes doctor`, it loads the profile's plugins to list their tools and leaves its logs and lock files. A run stops speaking after 3 days without a new one, and a problem of the whole machine (a security advisory, linger, the PATH, the gateway's supervision) is listed once. What the checks find joins the Environment screen and tab, most important first:
  - a cache folder Hermes never cleans, over 1 GB: a package cache that rebuilds itself (uv, pnpm, npm, pip...) goes to the plugin's trash with one key, one at a time (put back with undo, deleted after 30 days); any other folder (the browser's logins, conversation attachments, generated images) is yours, with what it holds and the command to look inside;
  - rollback snapshots above Hermes' cap: Hermes' three ways out (fewer days, prune, or turning /rollback off), which you run;
  - a security advisory or a suspicious MCP server: first in the list, with `hermes doctor --ack <id>` or `hermes mcp remove <name>`;
  - every other problem the doctor lists, with a plain title and explanation where the plugin knows it (tools on without a key, a missing `.env`, no provider key, a session database to repair, a gateway that would not come back after a reboot, a config to migrate, a memory provider or a plugin turned off, duplicate chat credentials...) and Hermes' own words and command otherwise.
  Optional pieces never set up (an optional package, a provider without a login) are counted, not proposed. Before a change runs, the check that found it runs again for that profile.
- **The full doctor, explained** (menu → Hermes health). Exit 1 is shown as "the doctor found N problems", with its own numbered list of what to do; before `hermes doctor --fix` the menu says what it may change and what `hermes backup --quick` restores.
- The number of doctor problems and warnings per profile in the menu (screen 14), the panel's Environment tab and `status`.

### Changed

- **Bilingual everywhere.** The panel follows the profile's language (with `auto`: the language chosen for the profile in Hermes, then the dashboard's); `care`, `environment`, `decisions`, `lessons list`, `proposals list`, `learn`, `skills`, `experiments`, `system`, `health`, `usage`, `history`, `tick` and `schedule-digest` print readable text in the profile's language (scripts that read their JSON add `--format json`; the JSON is unchanged); the help is in Portuguese too; the settings form shows each label as "English · Português"; codes such as `busy`, `one_by_one` or `applied` are sentences; dates are local with their zone; sizes and numbers follow the language.
- The agent's tools are registered only when `agent_tools` is on, so `hermes doctor` no longer warns "hermes-atlas-evolution (system dependency not met)" on every Hermes. Turning them on needs `hermes gateway restart`; turning them off is immediate.
- The panel: "Waiting for you" counts each item once; the command of an approved manual change stays on screen with a Copy button and in the History; numeric fields can be emptied; Copy works without the Clipboard API; the dialog takes focus and closes with Esc; tabs work with the arrow keys.
- The menu prints Markdown as plain terminal text, keeps you in Settings after an error, says when a key is not an option, and renders a lesson in your language when you open it.
- The review skill no longer asks the agent for lesson text it cannot see, and knows that approving applies a skill lock by itself.
- A dismissed observation comes back after 30 days only if the situation is still there; a change across profiles that is true again a day after it was applied is said again.
- The docs describe what the plugin does now, in English and in Portuguese, without version history.

### Fixed

- The trash deleted at once a folder whose own date was older than 30 days (renaming keeps it), and its undo then failed. Entries now count 30 days from the day they enter, and the trash is emptied at every learning pass (before, only when something else entered it).
- Auto mode could reinstall the plugin in a profile where you removed it (`hermes plugins remove` also clears its settings). The plugin's own database left there now counts as your decision.
- A plugin installed as a link to a folder (a development checkout) counted as missing, so the environment never ran and the install was retried every day.
- A transform hook that returns the original result (`return result`) counted as changing nothing, though Hermes keeps the first string any callback returns: a real overlap was missed.
- The install pinned the commit of the profile that looks after the whole Hermes even from a fork or a local commit, which Hermes could not fetch. It pins only a checkout of the published repository.
- Profile names follow Hermes' own rule, so a copy such as `profiles/work.bak` is not read as a profile.
- A change found stale on disk was reopened from the cached look at the next pass.
- `hermes pm gc` looked at a tool store per profile (there is one, at the root) and ran only from the default profile; it now runs once for the whole Hermes, from the profile that looks after it, marked before it starts.
- The background pass had no deadline inside its 15-minute lease.
- The runtime guard let `kill $(pgrep -f 'gateway run')`, `pgrep -f gateway | xargs kill` and awk lookups through: a pid lookup whose pattern selects the gateway now counts (`pgrep` against the command line with `-f` and the name without it, `grep`/`awk` only on a process list from a pipe), and ordinary commands such as `kill $(cat app.pid)` or `kill $(pgrep -u root worker)` still pass.
- `hermes doctor` ending with 1 because it found problems was shown as a failure, and its list of what to do was dropped.
- The session database was opened through an unescaped URI (a path with `#` opened another file, read-write).
- On Windows, checking the gateway's process sent it CTRL+C instead of probing it.
- apt checks: automatic updates set in another file of `apt.conf.d`, package lists refreshed by hand, and a systemd unit left off on purpose are no longer reported forever.

### Upgrading

`hermes plugins update hermes-atlas-evolution` in each profile that has it, then `hermes gateway restart` and `sudo systemctl restart hermes-dashboard` (or restart Hermes Desktop). The first daily doctor run happens at the next learning pass; `hermes evolution environment scan` runs it now.

## 0.8.5

Atlas Evolution now looks after every profile of a Hermes at once, in a Mode auto that is on by default, and knows Hermes' plugin ecosystem.

### Added

- **The mode** (`mode`: `auto` by default, `assist`, `manual`; menu screen 5, the panel's settings). It replaces `auto_actions`, which is still read when `mode` is not set (`off` → manual, `safe` → assist, `full` → auto), so an owner who chose it keeps that choice; the plugin's own `settings.json`, which the agent can write, may only narrow it to `manual`. In auto, the plugin applies by itself only reversible actions, in every profile: identical memory copies, locking skills the curator keeps rewriting, installing itself (at the lead's own commit) in profiles in use that never had it, moving folders of failed installs to its trash (kept 30 days), and `hermes pm gc` at most weekly; at most 3 changes to Hermes per pass (plus up to 10 identical memory copies and the weekly pm gc), each started only with time left to finish; an action the owner undoes is never repeated, whichever profile leads later.
- **The environment** (`environment.py`; menu screen 14, the panel's **Environment** tab, `hermes evolution environment [show | scan | catalog]`). One profile looks after the whole Hermes (of the profiles with the plugin on, the default one when it ran a pass in the last two days, else the first that did), at most every 6 hours, reading every profile's config.yaml, plugins (manifest, install record, git checkout and Python source, parsed with `ast` and never run), skills and usage; every file as a regular file only and up to a size limit. Findings: `plugin_transform_overlap` (two plugins can change the same tool's result through a transform hook: the code is read for the tools each one acts on, only some, all but some, or any; Hermes loads in waves of `requires_plugins`, alphabetically by manifest name, and keeps the first result of each call; plugins on different tools are not reported; a security plugin is never the one suggested to turn off), `plugin_transform_protected` and `plugin_hints_overlap` (two protective plugins, or this plugin's own `tool_hints`: advice, never a plugin turned off), `plugin_duplicate` (two plugins rewriting the terminal's command, or a role only one should fill), `plugin_removed` (on Hermes' kill list for that profile, as Hermes reads it; Hermes already refuses to load it, so the plugin only shows the removal command), `plugin_obsolete` (archived, delisted, missing or unmaintained, verified 2026-11-16), `plugin_unused` (installed, never loaded), `plugin_leftover` (`.install-*` folders), `plugin_update` (a newer version in Hermes' catalog, grouped per plugin), `self_missing` (never had the plugin) and `self_off` (the owner turned it off: kept), `profile_idle`, `skills_unused` (30 days, Hermes' own curator default), `skills_duplicate`, `skills_similar`, `skills_shared` (copies in 3 or more profiles: `skills.external_dirs`; the skills Hermes bundles are left out). **Before any change in a profile the plugin reads that profile again from disk** and runs only if the same situation is still there: a stored proposal, the cached scan or another profile's files are never trusted. Turning off one of two plugins is one key on that proposal, never in Apply all.
- **The knowledge base** (`ecosystem.py`, `knowledge/*.json`), copied into the tables `catalog_plugin`, `catalog_removed`, `knowledge_plugin` and `playbook` of the lead profile's `evolution.sqlite3`: Hermes' own plugin catalog read from the copy Hermes caches (else its source), what the plugin knows about well-known plugins, the obsolete plugins of the ecosystem, and the playbook of every situation (level, risk, action, undo, title and advice in English and Portuguese).
- Every proposal has an **advice** line ("What to do") in the menu, the panel's Proposals and Environment tabs and `hermes evolution environment`; changes in other profiles show and run as `hermes -p <profile> ...`.

### Changed

- The radar no longer reports plugins sharing a transform hook: it was confusing and, for plugins acting on different tools, wrong. The environment reports only a real overlap, with who wins.
- Hermes commands run only for a real profile (one with `config.yaml`), so a scratch folder never makes Hermes bootstrap a new home; the test suite never reaches the machine's own Hermes (`ATLAS_EVOLUTION_NO_HERMES=1`).
- The README says what the plugin does in practice and leaves out the move from old versions.

### Known limits

- When the profile that looks after the whole Hermes changes, the new one may show again a suggestion you rejected in the old one (an automatic change you undid stays undone).
- Plugins kept in category folders (`plugins/<category>/<name>/`) are not read by the environment.

### Updating from 0.8.4

With the default settings, updating switches you to Mode auto. Its first pass looks at every profile and may, by itself: take out identical memory copies, lock skills the curator keeps rewriting, run `hermes pm gc` if old tool versions take 300 MB or more, install the plugin in profiles used in the last 7 days that never had it (Hermes downloads it from GitHub), and move failed install folders to its trash. To look first and decide by hand, set the mode to assist before updating: `hermes config set plugins.entries.hermes-atlas-evolution.settings.mode assist`.

```bash
hermes -p <profile> plugins update hermes-atlas-evolution   # each profile that has it; without -p for the default one
hermes gateway restart
sudo systemctl restart hermes-dashboard
hermes evolution                                            # screen 14
```

## 0.8.4

Atlas Evolution now takes care of Hermes itself and of the server it runs on, without the agent and without tokens, and shows what it is for at the top of the README, with screens of the dashboard panel and of the terminal.

### Added

- **Hermes' health** (`hermes_care.py`; menu screen 12, the panel's new **Health** tab, `hermes evolution care`). Read from Hermes' own files, with no network, no model and no `hermes` command: the gateway alive (its heartbeat) and its memory against its usual, the session database (journal mode, a write-ahead log that grew, a weekly `quick_check`), room on the disk Hermes writes to with the day it would be full at the pace of the last week, the growth of each folder of the profile, memory, swap and CPU, versions of Hermes-managed tools nothing references, and a gateway that would not come back after a reboot (a user unit without linger). Every 6 hours in the learning pass, with a 21-day history. Hermes' own `hermes pm doctor` (offline), `hermes doctor`, `hermes doctor --fix` after `hermes backup --quick`, and `hermes pm gc` run on the owner's click.
- **The operating system's update** (`system_update.py`; menu screen 13, `hermes evolution system check|plan|upgrade|repair|report`), Linux with apt. A daily read-only check; a plan (`apt-get update` and a simulation that flags packages that restart the containers, need a reboot, touch remote access or the package manager); and an update by the owner only, as root in their own terminal: the check must pass, Hermes' profile and the plugin's code must belong to root, a copy of every package version and of `/etc` is kept in a root-only folder outside every profile (`/var/lib/hermes-atlas-evolution/system/`, the latest 5 runs), `apt-get --with-new-pkgs upgrade` runs in its own systemd unit with the owner's configuration files kept and no service restarted by itself, then dpkg, apt, every running service and Hermes are checked again and a report lists what changed and how to go back. Busy means a dpkg or apt lock really held (`/proc/locks`) or a package manager running. Packages left out are written down before they are held and released when the run ends, fails or is stopped (a package leaves the list only once really released, waiting up to 3 minutes for a dpkg still finishing; `name:arch` compared as apt shows it); a run that vanished is reported, never waited for, and a repair (in its own unit too) releases what it still held and finishes an update a power cut interrupted. It never reboots.
- **The weekly digest with one key**: the menu's suggestion or `hermes evolution schedule-digest --deliver <local|channel>` creates a Hermes cron job that runs a script, not the agent; once only; undone from the history (`hermes cron remove`).
- `status` and the menu show what needs attention from Hermes' health and the system's, with their values; new suggestions (finish an interrupted update, install security updates, free old tool versions, schedule the digest); screens 12 and 13 come after the existing ones, so the numbers you know do not move.
- The README starts with what the plugin is for and screens of the panel and of the terminal (taken from a real Hermes 0.23.1 with demo data); updating is in `docs/upgrading.md`; Hermes' health and the system's update in `docs/maintenance.md`.

### Changed

- Memory entries that differ in a number (a port, a version, an id) are two facts, not a repeat; the list of repeats shows at most 100.
- The panel's Health tab gets its attention lines written by the server, with the same texts as the status page.

### Tested

Beyond the unit tests: the suite in containers with Ubuntu 26.04 (Python 3.14), Ubuntu 24.04 (3.12) and Debian 13 (3.13); full system updates of Ubuntu 24.04 and 22.04 images of April 2024 (67 and 55 packages) with a package left out and an edited configuration file kept; a power cut in the middle of dpkg, detected and repaired, the package it held released; the update stopped as `systemctl stop` does; a dpkg lock held by another program; a Hermes owned by another user, refused; an update of Ubuntu 26.04 with Hermes 0.23.1 installed through the plugin's command; a real Hermes 0.23.1 in a container (two profiles, the dashboard with a password login, `hermes curator lock`, `hermes cron create`, `hermes pm doctor`); and stress tests (8 processes applying one proposal, 200 learning passes, 100,000 files, 5,000 proposals). A package manager working for more than 6 hours needs attention; services that ended without an error are listed apart from the ones that failed; a gateway counts as crashed only without a fresh heartbeat (one in another PID namespace is not). Two rounds of independent review of the system update and the health checks led to the root-only run folder, the root-owned check, the lock-based busy test, the released holds, the lost runs and the stricter service comparison above.

### Updating from 0.8.3

```bash
hermes -p <profile> plugins update hermes-atlas-evolution   # each profile; without -p for the default one
hermes gateway restart
sudo systemctl restart hermes-dashboard
hermes evolution                                            # screens 12 and 13
```

## 0.8.3

Atlas Evolution now acts by itself where it is safe, and says in plain words what each proposal means. In a real profile 16 proposals waited with codes such as `skill_lock` and `[medium]`, and nothing happened until the owner ran each command by hand; one skill had been rewritten 75 times in a week.

### Added

- **Automatic actions** (`auto_actions`, `safe` by default). At the end of each learning pass, `safe` takes out copies of a memory entry that are the very same text, character for character (a copy of the file is kept; an entry the owner puts back stays); `full` also locks the skills Hermes' curator keeps rewriting, with `hermes curator lock`, at most 3 per pass. Every action is recorded in the history with `automatic` as the actor and its undo (`hermes curator unlock`, the memory copy); an automatic change the owner undoes is never applied by itself again; a failure is reported and never stops the pass. The setting counts only from Hermes' configuration, like `runtime_guard: false`. The recommended setup now includes `full`.
- **Apply now.** A reversible change a proposal asks for (today, locking a skill) is applied by the plugin with Hermes' own command from the menu, the panel or `proposals approve`, one at a time or all at once, and undone from the history.
- **Proposals in plain words** (`explain.py`): a title, the risk, what was seen with its numbers, what applying it does and how to undo it, in English or Brazilian Portuguese, in the menu and the panel, grouped as changes the plugin applies, lessons, changes you apply and observations. An observation is dismissed with one key or click; a failure no template covers gets its lesson written from the menu.
- **The menu's summary** shows what needs attention in full sentences and up to five lettered suggestions that act at once. A new screen, **Automatic actions** (5), shows the mode, the last 7 days of automatic actions with their undo and changes the mode; the following screens moved one number down.
- `status` and the panel's header show the mode and the automatic actions of the last 7 days; `learn --notify-only` includes them.
- `CHANGELOG.pt-BR.md`, and a "What's new" and a quick start at the top of both READMEs.

### Updating from 0.8.2

```bash
hermes -p <profile> plugins update hermes-atlas-evolution   # each profile; without -p for the default one
hermes gateway restart
sudo systemctl restart hermes-dashboard
```

Automatic actions start as `safe`. To let the plugin lock unstable skills by itself: the menu's suggestion, screen 5, the recommended setup, or `hermes -p <profile> config set plugins.entries.hermes-atlas-evolution.settings.auto_actions full`.

## 0.8.2

0.8.1's panel could not decide in a real production dashboard. Behind HTTPS, Hermes names the login cookie `__Host-hermes_session_at`, and the panel only knew the plain name, so the dashboard's own profile stayed read-only with the owner logged in; and every other profile selected in the dashboard runs the plugin in its own plugin host, which Hermes reaches without any cookie, so those profiles could never decide. The panel now decides only in the dashboard's own process, with the session Hermes' login gate verified there, and for any other profile it asks that process, naming the profile on screen. An independent review of the first fix found that trusting the plugin host's forwarded verdict could be fooled (Hermes hides the shared session token from that process, and the agent can change `dashboard.public_url`), so the plugin host never decides. Checked end to end in a Hermes 0.23.1 dashboard with a password login over HTTPS and in a headless browser, for the dashboard's profile and a second one, and against the same dashboard without a login, where the token any program can read still gets read-only and 403 on every route, the review's attack included. This version also adds an interactive menu in the terminal and the care of Hermes' memory.

### Added

- **An interactive menu in the terminal.** `hermes evolution` with no command (or `hermes evolution menu`) opens a numbered menu in the owner's own terminal: lessons waiting (one by one or approve all), proposals, active lessons, the history with undo, every setting and the recommended setup, who is you in chats, Hermes' memory, the digest, the full status and a learning pass now. It decides through the same code as the panel and records in the same history. Plain text with numbered choices, so it works on Linux, macOS and Windows, over SSH and with a screen reader, in English or Brazilian Portuguese. It never opens in an agent's shell or without a terminal on both ends; there the bare command prints `status` (before, it was an error).
- **Hermes' memory, kept lean.** The menu's memory screen shows how full `MEMORY.md` and `USER.md` are against Hermes' limits and what repeats in them: the same entry twice (in one file or across both), one entry that says what another says, very similar entries. The owner chooses which one stays; the other is taken out under Hermes' own memory lock, after reading the file again, atomically, and only if it is still there as shown. A copy of the file is kept first (owner-only, `evolution/memory-backups/`, the latest 100), the history records the entry's fingerprint and never its text, and undo puts it back while it fits. The plugin never rewrites an entry's words.
- `admin.py`: the settings, every decision and its undo, the recommended setup and the dashboard's login rule, shared by the panel and the menu.

### Fixed

- **The panel recognizes a real login** (see above): the session Hermes verified in the dashboard process (not one verified by the deployment session token), and the `__Host-` and `__Secure-` cookie names where a host publishes no verified session. A profile's plugin host never decides (`other_profile`); the panel then calls the dashboard's own process with `?profile=current&target=<profile>`, which acts on that profile only when it belongs to the same Hermes and has the plugin installed and enabled (a Hermes Desktop grant, bound to one profile, only on its own). Where the panel stays read-only, it says why and shows the command of the terminal menu.
- `lessons: off` set through `hermes config set` (or a bare `off` in YAML) is stored as `false`; the plugin now reads it as `off` instead of refusing the settings and failing `status`, `learn` and the menu.
- Settings are written and logged one by one, so those written before a refusal stay in the history and can be undone. A `hermes config set` that hangs is reported as a refusal, not a crash; the menu takes a non-ASCII digit as an invalid choice; it re-reads the owner lists and the recommended setup before writing, and a proposal before deciding it, so a change made meanwhile in the panel is never overwritten; memory files with a byte order mark are read as Hermes reads them, and an identical repeat Hermes already sends once is not counted as savings.
- The documentation and SECURITY.md described the 0.8.0 panel as read-only and the settings writer of an early 0.8.1 draft; both now describe what runs.

### Updating from 0.8.1

```bash
hermes -p <profile> plugins update hermes-atlas-evolution   # each profile; without -p for the default one
hermes gateway restart
sudo systemctl restart hermes-dashboard   # or however you run the dashboard, so it loads the new panel code
```

## 0.8.1

Atlas Evolution can now be run from the Hermes dashboard. The Evolution tab, read-only in 0.8.0, lets the owner decide there when the dashboard asks for a login: approve or reject each lesson, approve every waiting lesson at once after reading each sentence, decide proposals, change every setting, pick who is the owner in chats from suggestions, and undo any decision. Settings now apply to the running gateway within a minute, without a restart. The terminal and the panel decide through the same code and share one history. A dashboard without a login stays read-only, because Hermes hands its session token to any program on the machine there. An independent review of the new decision route found six defects before release, the worst a way for the open dashboard's token to decide when sent as `Authorization: Bearer`; all are fixed with tests. The panel was exercised end to end in a real Hermes 0.23.1 dashboard with a password login, for the dashboard's own profile and for a second profile.

### Added

- **Decisions in the dashboard panel.** Approve, reject, retire or reactivate a lesson; **Approve all**, which first lists every sentence it approves and leaves out lessons the agent proposed (those are approved one by one); approve or reject a proposal; and **Undo** in a new History tab. Only a logged-in owner decides there: a request is accepted only with the session cookie of a dashboard login (password or OAuth) or Hermes Desktop's grant, without the dashboard's session token in any form (header, `?token=` or `Authorization: Bearer`), and with the header `X-Atlas-Evolution: 1`, which another site's page cannot send unasked. Values the plugin would not use as given are refused before they are written, and a broken `settings.json` never takes the panel down. Without a login the panel stays read-only, says why, shows the terminal commands, and leaves the sender ids of chat channels out of what it reads.
- **Settings in the panel.** Every option of the settings form, **Apply the recommended setup** (lessons on `auto`, reflector on, and the channels that are yours by construction), and **Who is you in chats**, with each channel of the last 30 days, its senders and suggestions: the API server (only clients holding its key write there, such as your own interface) and senders a Hermes host marks as the owner (`hosted-owner:`). Phone numbers and messaging ids are listed for you to pick, never guessed. Settings are written through Hermes' own plugin-settings writer, the one the settings form uses.
- **Settings apply without a restart.** Every hook and the plugin's clock compare the modification times of `config.yaml` and `settings.json` at most every 30 seconds and apply a change at once: who counts as the owner, the lessons mode, the reflector and its limit, the language, owner signals, the slow-turn threshold and the autopilot interval. `runtime_guard` and `tool_hints` decide which hooks Hermes calls, and a profile that loaded with lessons off has no prompt section, so those wait for the next load of the plugin; `status` and the panel list them (`settings_reload`).
- **`hermes evolution lessons approve --all`** approves every waiting lesson except the agent's proposals, after showing each sentence, and **`hermes evolution decisions`** lists the decisions from the terminal and the panel, with **`decisions undo <id>`**.
- **One history of decisions** (`decision_log`): ids, states and setting values, never lesson text. An undo applies only while what the decision changed is still as it left it; undoing an approval also drops the measurement it started.

### Changed

- Lesson decisions in the terminal (`approve`, `retire`, `review`) go through the same code as the panel and are recorded in the history.
- The runtime guard vetoes the agent's commands that call the panel's decision route, however the address is spelled.
- A setting changed while the gateway runs is read with Hermes' own loader, so an administrator's managed values still win, and a change of the managed configuration is noticed too.
- `hermes evolution decisions` in an agent's terminal, and the panel without a login, show the owner's chat accounts as counts and leave written lesson text out.
- Undoing a setting applies only while it still has the value the decision set, and is recorded only once Hermes took the old value back.
- `lessons approve <id>` of a lesson the queue cleaning withdrew activates it again, as in 0.8.0.

### Upgrade notes

- `hermes -p <profile> plugins update hermes-atlas-evolution` in each profile, then let the gateway reload the plugin (`hermes gateway restart` from your terminal, or disable and enable the plugin). Restart the dashboard once so it mounts the new decision route.
- To decide from the panel, the dashboard needs a login (`dashboard.basic_auth` or OAuth, which Hermes turns on when `dashboard.public_url` is a public address).
- The database gains one table, `decision_log`; 0.5.0 to 0.8.0 still open it.

## 0.8.0

This release gives Atlas Evolution a face inside Hermes and lets what one profile learned help the others, and it closes what three independent reviews of 0.7.0 found. An **Evolution** tab in the Hermes dashboard and in Hermes Desktop shows what the profile learned, what waits for the owner's decision with the exact command to run, the digest and the status; it only reads. **Lesson packs** carry lessons from one profile to another, or to another machine, after the owner has seen every sentence. The reviews looked at security (the runtime guard, who may decide, what reaches the agent), at compatibility with Hermes 0.23.1 and at the learning loop; every defect they found is fixed here with a test that fails without its fix. The runtime guard was rebuilt around how a shell actually runs a command and was checked against about 350 commands written to get around it; none that it vetoed before passes now. The suite has 430 tests, run under the system Python and Hermes' own with no `ResourceWarning`, and CI now also runs Python 3.14 and a lint job. The panel was checked in a real Hermes 0.23.1 dashboard, in English and Portuguese; it has not been used on macOS or Windows yet, and lesson verdicts have still not been watched over weeks of real use.

### Added

- **The Evolution panel in the Hermes dashboard.** A tab under Plugins, in the dashboard and in Hermes Desktop, with five views: an overview (active lessons, what waits for you, tool calls and turns with their error rates, guard vetoes, the last pass), the lessons with their measured effect, the open proposals, the digest for 7, 14 or 30 days, and the status page. Everything waiting for a decision comes with its exact text and the command that decides it, with the profile's `-p` filled in and a copy button. It decides nothing: its API has one `GET` route, `/api/plugins/hermes-atlas-evolution/snapshot`, that Hermes authenticates, scopes to the profile on screen and refuses where the plugin is disabled, and it runs in one of the dashboard's worker threads. Plain JavaScript on the dashboard's plugin SDK, with no build step; it follows the dashboard's theme and language and renders every lesson as text.
- **Lesson packs.** `hermes evolution lessons export [--out FILE]` prints the profile's active lessons (those measured as not helping are left out) as a small JSON pack: a template lesson travels as its tool, failure class and detail and is rendered by the receiving profile in its own language, a written lesson as its sentence, and nothing about conversations travels. `hermes evolution lessons import FILE` (or `-`), or `--from <profile>` to read another profile of the same Hermes directly and read-only, shows every sentence first (`--dry-run` changes nothing). Each entry is checked as untrusted input. A sentence already in the playbook, waiting or turned down never comes in again. An imported template lesson waits and, in `auto` mode, activates once the owner's own turns hit its failure twice there, without first waiting for three failures in two sessions; a written one is quarantined; `--activate` activates both. Imported lessons are measured, and retired, by the receiving profile. Both commands are the operator's.
- **Hermes' own tool timeouts are failures.** Hermes 0.23.1 ends a tool call at its own deadline with status `timeout` (on by default). Those calls were invisible to the loop: now they are failures of class `timeout`, count as errors and in the slow-tool readings, become a lesson when they recur, and the history replay reads the text Hermes stores for them.

### Changed

- **The runtime guard reads a command the way the shell runs it.** It finds the command after `if`, `then`, `do`, `while`, `{`, `!` and the other words that open a compound command; inside `$(...)` and backticks, also within double quotes; behind `eval`, `su --command=`, `find -exec`, `ssh localhost`, the string literals of `python -c`, `perl -e` and similar, and more wrappers (`taskset`, `unshare`, `nsenter`, `chroot`, `strace`, `uv run`, `uvx`, `pipx run`, `npx`, `sudo`'s and `systemd-run`'s long options and timers). The `hermes` command counts under every name it runs by, `python -m hermes_cli.main` (the gateway unit's own form) included. New gateway vetoes: `kill` of a process id looked up from Hermes (`kill $(pgrep -f hermes)`, `pgrep ... \| xargs kill`, the pid file), `kill $PPID` and `kill -1`, `pkill` and `killall` whose pattern selects the gateway process itself (`pkill -f python`), systemctl options with a value before the action, unit globs, `condstop`, `freeze`, `mask --now`, a unit the shell fills in when the command names Hermes, and systemd's restart and stop methods called over D-Bus. New removal vetoes: the inside of the interpreter, its environment and Hermes' packages (`rm -rf venv/lib`), brace expansion (`mv ~/.hermes{,.old}`), `${VAR:?}` and `${VAR:-...}`, `~user`, `mv -t`, `xargs rm` fed with a protected path, and relative paths under the terminal's `workdir` or an earlier `cd`. New settings vetoes: removing the plugin's own folder, `hermes plugins trust` of it or an install over it, and a terminal write to a Hermes `config.yaml` that names the plugins section, this plugin or its trust settings. Text sent to a background process with `process_manage` `write` or `submit` is checked as a command.
- **`pre_tool_call` is registered only while the runtime guard is on**, and **`transform_tool_result` only while `tool_hints` is on.** Hermes blocks a tool whose `pre_tool_call` fails or stalls, and reads every tool result in a thread of its own while any plugin listens to `transform_tool_result`, so neither costs anything when its feature is off. `hermes plugins validate` now notes `transform_tool_result` as declared but not registered under the default settings.
- **A delegated child is a subagent under the TUI and Desktop too.** A child keeps its parent's session source in its copied context, so under the TUI or Desktop its turns counted as the owner's local turns, in the turn and skill-outcome numbers and in owner feedback. The platform `subagent` now decides first, as Hermes' own `is_attended` does.
- **The owner notice waits for an end Hermes shows.** Hermes shows `on_session_finalize` text only when the classic CLI exits or starts a new session and when the TUI or Desktop closes a session; for any other reason (`/resume`, `/branch`, a viewer closing a gateway-hosted session, a reaped one) the plugin now returns nothing, so the day's notice is not spent where nobody sees it.
- **What an agent's terminal may print.** In an agent's terminal, `lessons list` and `proposals list` leave out the sentence of every written lesson (`text_hidden`), because the terminal of a stranger's chat is an agent's terminal too.
- **CI** runs Python 3.14 too, and a lint job (ruff for undefined names, unused imports and syntax Python 3.11 refuses, then a byte-compile with 3.11).

### Fixed

- **Security: the guard's bypasses.** Every one of the commands above passed the 0.7.0 guard, including the common `if ! systemctl --user is-active ...; then systemctl --user restart hermes-gateway; fi`, which also bypassed the veto on the plugin's own settings.
- **Security: the 0.1 shared notes.** With `legacy_memory_commands` and `automatic_shared_recall` on, the agent's terminal could write a shared note, and the recall put it into every turn, a stranger's chat included. Writing and removing notes is now the operator's, and shared notes are recalled only into turns that serve the owner.
- **Security: credentials in excerpts and lessons.** The reflector's excerpt kept a MySQL `-p` password, `password=` and `token=` values, Google OAuth, GitLab, Stripe and npm tokens, AWS key ids and opaque API keys, and sent them to the model; the lesson text rules accepted several of them. The excerpt now masks all of these and opaque tokens (20 or more characters mixing letters and digits), and a lesson is refused when it holds a known key prefix, an opaque token (a run with a part where letters and digits keep alternating, so `System32` or `--max-old-space-size=4096` still pass) or anything Hermes' own redactor would hide.
- **A lesson activated right after a one-day burst had no baseline.** The activation day was left out of the before window, so a lesson activated within hours of its third failure got a baseline of 0 failures, could never be judged effective, and 3 later failures made it ineffective and retired it for good, its sentence blocked. The activation day up to the moment of activation now counts in the baseline (`lesson_activation`), and a baseline without failures is always inconclusive.
- **Rejecting the proposal to retire a lesson did not keep it.** In `auto` mode the lesson was retired anyway; rejecting `lesson_retire` now keeps it for that activation (`lesson_keep`), and its verdict is still reported.
- **A pass could undo a retire the owner made while it ran.** The verdict is written only while the lesson is still active.
- **An unreadable configuration was recorded as every watched setting changing, and then changing back**, with verdicts and possibly high-risk proposals. A pass that cannot read the Hermes configuration now records no change.
- **A failure with a template could be starved.** Lesson creation read the 200 most frequent failures before filtering, so 200 recurring failures without a template kept one with a template from ever becoming a lesson. The filter now runs first (`DERIVE_BATCH`).
- **Baselines frozen from part of the history.** A pass whose history replay failed for a moment (`unreadable`) froze baselines anyway; they now wait until the replay is settled.
- **Investigation proposals flapped** between open and expired as busier failures moved through the week's top 12; they now come from their own query (`INVESTIGATE_LIMIT`).
- **One failing reflector call could take every call.** A failure whose draft call keeps raising now waits after 3 failed calls (1 day, doubling up to 14), so the others get drafted (`reflector_backoff`).
- **A clock far ahead deleted the history.** One hook write with the system clock a year ahead pruned every daily aggregate. Retention now never counts from more than 2 days past the newest day stored, and believes a clock that stays ahead once 3 days of new data exist.
- **The daily notice could show several times** while the database was busy; it is now marked in the same transaction that decides to show it, and waits when it cannot be marked.
- **A tool hint on a deeply nested result could raise** (found by the new Python 3.14 CI job): with a large enough stack, Python 3.14 parses a tool result nested deeper than it can write back, and the hint failed instead of being added as text after the result. The hook stayed fail-open; the lesson was lost for that call.
- **A connection leak** in the health radar when Hermes' history could not be probed, and the test suite's own unclosed connections.
- **Security: who counts as the agent.** The check for a Hermes ancestor skipped any ancestor whose words included "evolution", so a one-shot `hermes -z "...evolution..."` or a profile named evolution could decide; only the command's own launcher chain is skipped now. A virtual environment's `python .../bin/hermes` counts as Hermes.

### Upgrade notes

- Run `hermes plugins update hermes-atlas-evolution` in each profile (with `-p <profile>` for a named one), then `hermes gateway restart` once. From 0.6.0 or earlier, follow the notes of those versions first.
- The **Evolution** tab appears in the dashboard once it rescans its plugins; restart the dashboard if it does not show.
- To give a profile what another one learned: `hermes -p <profile> evolution lessons import --from <other profile> --dry-run`, then again without `--dry-run`.
- The database stays at schema 8 and gains four tables (`lesson_import`, `lesson_activation`, `lesson_keep`, `reflector_backoff`) and the cursor key `retention_day`, so 0.5.0 to 0.7.0 can still open it. A 0.7.0 copy still running somewhere would withdraw imported lessons that wait, because it does not know them; restart every gateway after the update.
- With `runtime_guard: false` the plugin no longer registers `pre_tool_call`, and with `tool_hints: false` (the default) no `transform_tool_result`: `hermes plugins validate` lists the latter as declared but not registered, which is expected.
- The guard vetoes more commands. If it stops something you meant the agent to do, run it in your own terminal; the guard never affects it.

## 0.7.0

This release makes the plugin see what slows Hermes down and whom each chat serves, keeps the lesson queue clean, and closes the ways the agent itself, or a stranger in a chat, could widen whom the plugin trusts. It points out refused approvals nobody could have answered, with the Hermes setting that decided them, and tools that are slow call after call. It lets the owner name their platforms and pick their sender ids with a new command, `hermes evolution owner`, activates a waiting lesson once the owner's own turns hit its failure, and withdraws the lessons nobody decided on that the evidence no longer supports. Settings that widen trust count only from the Hermes configuration, which the agent's file tools cannot write, the runtime guard keeps the agent from changing them, and a subagent serves whoever its parent served. Three reviews went over this release before it shipped: an independent one found ten defects, an adversarial one over the fixes found 22 more, from the guard to the documentation, and a last independent pass found six more; all are fixed here, each with a test that fails without its fix. The suite has 376 tests against a simulated Hermes context, run under the system Python and Hermes' own, and CI runs it on Linux with Python 3.11 to 3.13 and on real macOS and Windows runners. The plugin passes `hermes plugins validate` on Hermes Agent 0.23.1 with the security scan reporting it safe. On a copy of a real profile the new findings named 15 approvals refused in a week where nobody could answer, read from Hermes' own text, and one MCP tool averaging 37 seconds over 45 calls; they have not been watched over weeks of real use yet.

### Added

- **`hermes evolution owner`.** Lists the chat channels of the profile in the last 30 days, each with its sender ids and their session counts, and marks the ones that already count as the owner's. `add <id>`, `remove <id>`, `add-platform <name>` and `remove-platform <name>` print the exact `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key> '[...]'` command, with the whole new list, for the owner to run, and nothing is written by the plugin. Operator only, in the owner's own terminal. Restart the gateway after a change.
- **`owner_platforms`.** Platforms where every chat counts as the owner's from the first message, such as a private bot or the owner's own interface through the API server. Their chats get every lesson, their failures count as the owner's, and their feedback is counted. List only a platform nobody else writes to.
- **Refused approvals nobody could answer.** A refused approval is counted per day by the real origin of its session, the tool and the Hermes setting that decided it. Hermes names the setting in its own refusal whenever single-query mode, cron or an unattended platform decides (`set approvals.<key>: approve`), and the plugin reads it from there. A refusal a person made ("denied by user", "User denied ...", an approval transport, also in a subagent) counts as answered, except the automatic denial a CLI subagent gets (`delegation.subagent_auto_approve`); otherwise one-shot runs, cron, Kanban, batch runs, the Curator, A2A peers and `--source tool` integrations count as unattended. The digest shows them under "Hermes health" with the settings that decided them, and 5 or more in 7 days open a low-risk proposal that points to `hermes config get approvals` (or `delegation`) and the `command_allowlist`, and says never to approve everything. A refused approval still never becomes a lesson.
- **Slow tools.** The digest lists the tools that took the most time, with their mean and number of calls, and any tool that averages 20 seconds or more over at least 5 calls in 7 days opens a low-risk proposal, whatever its share of the total time. Tools that wait for a person (`clarify`, the Desktop onboarding `setup_beat` and tour `gui_tour`, `browser_vault`) or run a whole subagent (`delegate_task`) are left out. The history replay times tool calls too, approximately (from the request to the result), so the first pass already shows them.
- **A waiting lesson activates when the owner hits its failure.** In `auto` mode a derived lesson that waited only because the owner's own turns had not hit its failure twice yet activates by itself at the first pass after they have. A candidate from `suggest` mode or from an earlier version keeps waiting for approval, and a lesson the operator rejected stays retired.
- **Lesson hygiene.** Once the history replay is settled, every pass withdraws the lessons nobody decided on that the evidence no longer supports: a derived candidate or a reflector draft whose failure no longer meets the rule (3 times, in 2 sessions or on 2 days), a candidate that would say the same sentence as an active lesson, a lesson the owner turned down or a candidate with more evidence, one about a missing binary that only another plugin's rewrite called, and a draft that is only advice every failure would get. A withdrawn lesson is kept as retired with the verdict `withdrawn`, so its id never points to another lesson, and it comes back with the same id and text if its failure does, unless the owner retires it. Active, retired and operator lessons are never touched, and a withdrawal or promotion changes a lesson only if it is still where the pass read it. The pass reports how many it withdrew (`lessons_withdrawn`), and `status` counts them apart from the retired ones.
- **`status` points out three gaps**: chat channels used in the last 7 days where no sender counts as the owner, with their session and sender counts (never the ids); decisions waiting while no weekly digest is scheduled to remind the owner; and values of `settings.json` that are ignored because they would widen trust, by their name there.
- **The runtime guard protects the plugin's own settings.** It vetoes the agent's `hermes config set` or `unset` of the `plugins` section (which enables, disables and isolates plugins) and of this plugin's settings, its `hermes config edit`, and its `hermes plugins disable` or `remove` of this plugin, which would let an agent make itself or a chat the owner, or switch the plugin or the guard off. Other plugins' settings stay open to the agent. Global options such as `-p <profile>` are read wherever they sit in the command, as Hermes reads them, also for the gateway vetoes, and a word the shell fills in at the group, key, action or name (`$VAR`, `$'...'`, a command substitution, the words `xargs` adds) counts as such a change; `env -S '<command>'` is read like `bash -c`. These vetoes are counted as `plugin_settings` in the digest. The gateway veto also covers `hermes gateway uninstall`, `start --all`, `run --replace` and `install --force`, and `hermes profile delete` is vetoed as the removal of what Hermes runs on.

### Changed

- **Settings that widen trust count only from the Hermes configuration.** The agent's file tools need no approval to write `<HERMES_HOME>/evolution/settings.json`; Hermes guards only its `config.yaml`. `owner_sender_ids`, `owner_platforms`, `agent_advice: true` (the agent tools), `runtime_guard: false`, and `legacy_memory_commands: true` and `automatic_shared_recall: true` (the 0.1 notes, whose recall puts stored text in front of the agent on every turn) are therefore ignored in `settings.json` and count only from the Hermes settings form or `config.yaml`; the two 0.1 keys are not in the form, and `hermes config set` writes them.
- **A subagent serves whoever its parent served.** A delegated child of the owner's own session, automation or chat serves the owner; a child of someone else's chat serves that person, gets template lessons only, and its failures never count as the owner's; a child whose parent the plugin has not seen serves someone else. Hermes' `subagent_start` names the parent before the child's prompt is built, a compaction that gives the parent a new id carries this over (`post_compaction`), and the history replay follows `parent_session_id`.
- **Changing who is the owner replays the history again.** The replay decides turn by turn whether the owner was served, so it keeps a fingerprint of the owner settings and is redone when they change, as long as the history can still be read: naming yourself after the first pass makes the replayed history of your chats yours too. A run that cannot read the Hermes configuration (`python -m hermes_atlas_evolution` without a YAML parser) never redoes it for that.
- **One sentence, one decision.** Failures that read as the same sentence derive one lesson and share the owner's evidence for activation, promotion and `lessons list`. A sentence the owner turned down, or one retired for not helping, never comes back through another failure that reads the same.
- **Lessons are kept, retired ones included.** Retention no longer deletes retired lessons after 90 days, so a lesson id never points to another lesson and a sentence the owner turned down stays down.
- **The owner's failures are counted per day** (`failure_owner_daily`), so they age out with the 90-day retention and a replay takes back exactly what it counted. The `failure_owner` table of 0.6.0 is no longer written; it is kept so 0.6.0 still opens the database.
- **Sharper reflector drafts.** The excerpt the reflector reads starts with the name of the program the command ran (`[git]`, `[npm]`) when it is a bare command called from the PATH or a system folder such as `/usr/bin`, never a script, a path or an argument; the prefix takes its room from the start, so the end of the error, where the cause is, stays, and the excerpt holds at most 300 characters. The instructions ask for a sentence specific to that program or condition, and a draft that is only generic advice (reading the output or the error, checking before retrying, diagnosing the cause, in English or Portuguese) is declined; a draft that also says what to do is kept.
- **Commands rewritten by Hermes' tool middleware** count as `rewritten_command`, never a lesson, when the `middleware_trace` that Hermes passes to `post_tool_call` names a change.
- **No lesson about another plugin's binary.** No `missing_dependency` lesson is derived for a binary that failed live after another plugin rewrote the command into it, such as a command proxy missing from the PATH of a session.
- **`lessons review` saves each decision at once** and holds no write lock while the owner reads, so the gateway's hooks keep writing. A lesson that a pass changed while it was on screen is skipped instead of decided on.
- **The radar** counts the plugin among the competitors for `transform_tool_result` only while its hints can change a result (`tool_hints` on and lessons not off).
- Waiting lessons point to `hermes evolution lessons review` in `status` and in the digest. The proposal to investigate a failure, the digest scheduler's messages, the titles of the new findings and the help of every command speak Portuguese too.

### Fixed

- **A subagent delegated from a stranger's chat counted as the owner** (since 0.6.0): its prompt got the operator's lessons, the agent tools answered it, and its failures counted as the owner's and reached the reflector.
- **A redo of the history counted old days twice.** On a profile observed for more than 76 days, the replay read days older than the 90-day retention, whose daily counts were already gone, so a redo added them to the failure totals again. The replay now starts no earlier than the retention window, fixed for the whole replay, and a redo leaves the totals of older days alone.
- **Beside a 0.6.0 copy, the replay was redone at every pass, or finished the old way.** The revision now has its own key, `backfill_revision`, `backfill_version` keeps saying `2`, and `backfill_done` says `running` while this version replays, so a 0.5.0 or 0.6.0 gateway not yet restarted after the update leaves the replay alone.
- **Per-signature lookups scanned whole tables.** Indexes by signature on `failure_daily` and `failure_owner_daily` are created when the database opens, and the promotion of waiting lessons reads everything before it takes the write lock.
- **Dates on Windows.** Python on Windows refuses to turn a time before 1970 into a date, which a missing time in Hermes' history can produce; every UTC date is now computed the same way on every system.
- The tests that are POSIX by design (permission bits, the liveness probe, symlinked runtimes) skip themselves on Windows, and the macOS database settings are checked on a real macOS.

### Upgrade notes

- From 0.6.0: run `hermes plugins update hermes-atlas-evolution` in each profile (with `-p <profile>` for a named one), then `hermes gateway restart` once. From 0.5.0, follow the 0.6.0 notes below: remove `hermes-evolution` and install `hermes-atlas-evolution`.
- If you set `owner_sender_ids`, `owner_platforms`, `agent_advice: true`, `runtime_guard: false`, `legacy_memory_commands: true` or `automatic_shared_recall: true` in `settings.json`, they are now ignored, and `status` lists them. Set them again in the Hermes settings form, or with `hermes config set plugins.entries.hermes-atlas-evolution.settings.<key> <value>` (for sender ids and platforms, `hermes evolution owner add` prints the command).
- The database stays at schema 8 and gains three tables (`failure_owner_daily`, `approval_daily`, `lesson_owner_wait`) and two indexes, so lessons and proposals stay and 0.5.0 and 0.6.0 can still open it. The history replay is redone once, the same way 0.6.0 redid 0.5.0's, to count the owner's failures per day, refused approvals per origin and setting, and tool durations. 0.6.0's owner totals are cleared; the owner's failures in days observed live under 0.6.0 are not recounted, so a lesson from those days waits until the owner's turns hit its failure again.
- If you talk to your agent through a messaging app or your own interface, run `hermes evolution owner` in your own terminal, run the command it prints for your sender id or your private platform, and restart the gateway. The next pass replays the history again as yours.
- Lessons waiting for a decision that the evidence no longer supports are withdrawn by the first passes after the replay; `hermes evolution lessons review` shows what is left.

## 0.6.0

This release renames the plugin to `hermes-atlas-evolution`, the name of its repository, and makes it count right, tell the owner from everyone else, and protect Hermes on every system it runs on. The history replay counts each message once (0.5.0 counted every copy Hermes' compaction keeps), only the owner's own turns can activate a lesson by themselves, the runtime guard knows the gateway on Linux, macOS and Windows, and every message speaks English or Brazilian Portuguese. Installing, updating and checking the plugin in every profile is easier, and every loaded profile gets its passes even when it is idle. The suite has 321 tests against a simulated Hermes context, run under the system Python and Hermes' own; the plugin passes `hermes plugins validate` on Hermes Agent 0.23.1 with the security scan reporting it safe. The clock, the stale-process check and the migration from 0.5.0 are verified in tests, not yet on a long-running production gateway.

### Added

- **`status` for every profile.** A table of every profile of the Hermes root, up to 200: sessions in the last 7 days, the plugin installed, installed under the old id, installed under both ids (the old copy is the one running) or not installed, the last pass, and lessons active and waiting. It points to a busier profile, prints the fix for each profile still on the old id or with both copies, the install command for each profile used in 7 days without the plugin, and flags a process that still runs older code. Other profiles are read only.
- **First pass from `status`.** In a profile that never finished a pass, `status` runs one at once, with 90 seconds in all and at most 60 for the history, so the first look is not all zeros. `--no-learn` never runs it.
- **Autopilot clock.** A daemon thread per loaded profile nudges the pass 2 minutes after the plugin loads and then every 10 minutes, because a gateway that serves several profiles delivers its Kanban tick only to its own profile. It starts only when `autopilot_minutes` is not 0 and the host offers `on_unload`, and it stops when the plugin unloads.
- **Stale code check.** A gateway or dashboard records the plugin version it loaded. `status` flags one that is still alive and runs another version than the code on disk, and says to run `hermes gateway restart`. The liveness probe runs on POSIX systems only.
- **`hermes evolution lessons review`.** Shows each lesson that waits for approval with its exact text: `a` approves, `r` rejects, Enter skips, `x` quits. Operator only, in the owner's terminal.
- **Who is served.** A turn serves the owner on their own surfaces, in their automations and subagents, or in a chat with a sender listed in `owner_sender_ids` (ids may be numbers). Failures are counted again in a new table, `failure_owner`, when the turn served the owner. In `auto` mode a derived lesson activates by itself only after the owner's own turns hit its failure twice; failures only other people caused wait for approval. Only the owner's failures leave an excerpt for the reflector. A chat that may serve someone else gets template lessons only, never operator, reflector or agent text, and the agent tools answer only in the owner's sessions.
- **Failures caused by other plugins.** Two failure classes that never become lessons: `plugin_fault`, Hermes' refusal after another plugin's `pre_tool_call` hung or raised, and `rewritten_command`, a terminal command that failed after another plugin rewrote it. The radar counts both, the digest shows them under "Hermes health" and labels them as never a lesson, and 3 or more of a kind open a medium-risk proposal.
- **Web research in the history replay.** The first passes also time the research turns of the replayed history, shown under "Hermes health" in the digest.
- **One copy per profile.** While the copy installed under the old id `hermes-evolution` is enabled in a profile, the new one registers nothing and logs the command that removes the old one, so the two never count the same turns. A clone of this version kept under the old folder name is not mistaken for the old copy.
- **The runtime guard on macOS and Windows.** It vetoes `launchctl` `kickstart -k`, `stop`, `bootout`, `unload`, `remove` and `kill` on the LaunchAgent `ai.hermes.gateway[-<suffix>]`, and `sc`/`net stop`, `Stop-`/`Restart-`/`Suspend-Service`, `Stop-ScheduledTask`, `schtasks /End`, `taskkill` and `Stop-Process` on the service or task `Hermes_Gateway[_<suffix>]`, also inside `powershell -Command` and `cmd /c`. On Windows, `C:\x`, `C:/x` and Git Bash's `/c/x` are one path; `%LOCALAPPDATA%`, `$env:LOCALAPPDATA` and `$HERMES_HOME` expand.

### Changed

- **Name.** The plugin id is `hermes-atlas-evolution`, as the repository (it was `hermes-evolution`). The Python package is `hermes_atlas_evolution` (standalone runner `python -m hermes_atlas_evolution`); the toolset is `hermes-atlas-evolution`; the tools are `hermes_atlas_evolution_plan` and `hermes_atlas_evolution_lessons`; the prompt section is `hermes-atlas-evolution.playbook`; the hint key is `hermes_atlas_evolution_lesson`; the bundled skill is `hermes-atlas-evolution:evolution-review`; and the recipes and Kanban cards use `hermes-atlas-evolution-digest`, `hermes-atlas-evolution-watchdog` and `--created-by hermes-atlas-evolution`. The product name stays Atlas Evolution, and the `hermes evolution` command, the data folder `<HERMES_HOME>/evolution/` and `settings.json` keep their names. Settings saved under the old id still apply; the new id wins key by key.
- **Lessons default to `auto`.** Lessons written from the fixed templates are active at once (once the owner's own turns hit the failure), are measured, and are retired when measured as ineffective 14 days or more after activation. Reflector drafts and agent proposals still wait for approval. `suggest` makes every lesson wait; `off` only measures.
- **`tool_hints` is off by default.** Hermes keeps only the first plugin's change to a tool result, so a hint would discard the change another plugin makes to that result. The hook stays registered while lessons are on, and is silent; the radar no longer lists the plugin among the competitors for `transform_tool_result` while it is.
- **Surfaces follow the real origin of a session.** Kanban workers and the Curator count as automations (`cron`), `--source tool` integrations, A2A peers and bot rooms as chats, one-shot runs as local. Only a delegated child (platform `subagent`) is a subagent; a session continued after `/new`, `/branch` or a compression keeps its surface. The history replay follows the same rules.
- **Owner feedback counts only what a person wrote after an earlier turn**: not a session's first message, not a `/goal` continuation, not the same text sent again (a `/loop` tick), not an automation's prompt.
- **Restarts are left to the owner.** The runtime guard also vetoes the agent's `hermes gateway restart` and tells the agent to ask the owner, whose `hermes gateway restart` lists the turns still running and asks before interrupting them.
- **The runtime guard knows more names and wrappers**: systemd units `hermes-gateway-<profile>` and `hermes-gateway-<hash>`; `setsid`, `systemd-run` with its options, `runuser`, `watch`, `flock`, `xargs` and `rtk run`; one level into a command given as one string to a shell, `su`, `runuser`, `script`, `flock`, PowerShell or `cmd`. Paths are protected as written and as resolved, including the base interpreter a virtual environment names in `pyvenv.cfg` and a `HERMES_HOME` that is a link.
- **No `pre_tool_call` in a plugin host.** Under `plugins.isolation: host` the plugin registers no `pre_tool_call`, because Hermes blocks a tool whose `pre_tool_call` fails and a crashed host would block every tool. The guard is off there, and `status` says so and says what the guard covers elsewhere.
- **The operator guard** also refuses decisions when a Hermes process is an ancestor of the command or the command runs in a Hermes gateway's systemd cgroup, both read from `/proc` on Linux.
- **The autopilot binds the profile's secret scope** as well as its home, so in a gateway that serves several profiles a reflector call uses that profile's provider keys.
- **Passes take a deadline where the caller has one**: 200 seconds in `ops tick`, 90 in the first pass of `status`. A reflector call starts only when its worst case still fits.
- **The weekly digest recipe** delivers the digest even when its `learn` fails or runs out of time, and gives `learn` 300 seconds.
- **Every message speaks Portuguese.** Each error a command can show has a pt-BR form, and a test walks the package's `raise` statements to keep it so. The legacy `/evolution` command and the scheduler scripts answer in the profile's language, and `python -m hermes_atlas_evolution` prints UTF-8 on a Windows console with another code page.
- **Retention.** Reflector excerpts also expire in the daily prune; the delivery IDs that stop an event from being counted twice (`seen`) are kept 2 days instead of 35.
- **A database written by a newer version opens**, since schemas only add tables and columns. On macOS the database uses full syncs, as Hermes does for `state.db`.
- **The owner notice is not spent on a session end Hermes does not show.** Hermes 0.23.1 drops the end-of-session text of the local sessions it hosts when their viewer closes or they are reaped.
- **Documentation.** The README covers installing, migrating and updating in every profile, in plain words; development moved to `CONTRIBUTING.md` and the details to `docs/`. The README states that Atlas Evolution is free and open-source software under the MIT license, and `LICENSE` names the author.

### Fixed

- **The history replay counted messages more than once.** Hermes' in-place compaction archives a session's rows and inserts copies of them, under the same `message_uid` and sometimes with a later timestamp or a shortened body, plus a summary row. The replay now skips summaries and replays each message once per session. On a real history: 3,566 tool calls instead of 6,731, and 23 interrupted turns instead of 86.
- A session the replay cannot read is skipped and counted (`skipped`), never the whole backfill, and a finished turn of a session that continued after observation began is counted.
- JSON nested deeper than the parser allows no longer raises, in the replay, in the live failure classifier, in lesson hints or in the skill readers; before, one such payload stopped every later pass.
- A lesson baseline that reaches into history the backfill has not replayed yet waits instead of freezing a partial count.
- The log reader reads every file rotated since its last run and skips a line longer than one read instead of stopping on it.
- Replay slices are written synchronously, in one transaction that first checks the cursor, never through the hooks' queue.
- A profile served by another profile's gateway never ran a pass while it had no turns. The clock covers it.
- A pass cut by the exit of its process kept its lease for 15 minutes. The process now frees the lease at exit.
- `status` in a profile that had not run a pass yet showed zeros until the next turn. It now runs the first pass.
- Only the lease holder records the end of a pass, and the history replay drops a slice when another pass moved its cursor meanwhile.
- No write transaction stays open while a pass reads the skill files, the Curator ledger or Hermes' `state.db`.
- The radar listed the plugin itself as competing for `transform_tool_result` while its hook returned nothing.

### Upgrade notes

- In each profile that has 0.5.0: only if you changed the plugin's options in the Hermes settings form, rename `plugins.entries.hermes-evolution` to `plugins.entries.hermes-atlas-evolution` in the profile's `config.yaml` first, because the next command deletes the old block. Then run `hermes plugins remove hermes-evolution` and `hermes plugins install sesantanajr/hermes-atlas-evolution --enable`, with `-p <profile>` for a named profile, not `hermes plugins update`: Hermes names a plugin's folder after its id, so an update would not replace the old copy. Finish with `hermes gateway restart`. `status` marks a profile that has both copies and prints its fix.
- The database stays at schema 8 and gains one table (`failure_owner`), so lessons and proposals stay and 0.5.0 can still open it. The history replay that 0.5.0 finished counted every compaction copy, so it is redone once: whole UTC days before the first observed day are cleared and replayed, failure totals give back what that replay added, and lesson baselines frozen from those days are counted again. The first observed day and later days are not touched.
- Profiles that never set `lessons` now run in `auto`. Set `lessons: suggest` to keep the behaviour of 0.5.0. Candidate lessons derived before the upgrade stay candidates until you decide on them with `lessons review` or `lessons approve`.
- If you talk to your agent through a messaging app, list your sender ids in `owner_sender_ids`. Without them every messaging chat counts as someone else's: lessons from its failures wait for approval, and the chat gets template lessons only.
- Profiles that never set `tool_hints` no longer get a lesson appended to failing results. Set `tool_hints: true` to bring it back.
- Only if you listed them by hand: replace `atlas-evolution`, `atlas_evolution_plan` and `atlas_evolution_lessons` with `hermes-atlas-evolution`, `hermes_atlas_evolution_plan` and `hermes_atlas_evolution_lessons` in a toolset list or in `auxiliary.background_review.extra_tools`.
- Cron jobs created from earlier recipes keep working, because they call `hermes evolution`. Create them again from a recipe printed now to get the new timeouts and the messages in your language; the new recipes use the new job names, so delete the old job first.
- After `hermes plugins update hermes-atlas-evolution` in each profile, run `hermes gateway restart`. From 0.6.0 on, `status` flags a gateway or dashboard that still runs older code.
- An agent that runs `hermes gateway restart` is now refused and asks you instead. Restart from your own terminal.

## 0.5.0

This release makes the loop run by itself and learn from what Hermes already recorded, turns the reflector on by default, and adds a runtime guard, a health radar, a status command and owner notices. The plugin is covered by 257 unit and end-to-end tests against a simulated Hermes context, and passes `hermes plugins validate` on Hermes Agent 0.23.1 without warnings, with the security scan reporting it safe. The reflector made three real model calls through Hermes in production. The autopilot is verified in tests and in the CLI. Windows was only simulated in tests.

### Added

- **Autopilot.** The learning pass runs by itself in a background daemon thread, at most once per `autopilot_minutes` (default 60; 0 leaves passes to the operator; otherwise 15 to 1440). The hooks `post_llm_call`, `on_session_end` and `on_kanban_dispatch_tick` nudge it. A hook only compares clocks, and a check starts at most once every 60 seconds per process. Every process of a profile claims the pass with a compare-and-set lease in the plugin's database; the lease lasts 15 minutes, so a dead owner's lease expires, and a failed pass is retried after 10 minutes. The thread pins the plugin's own profile, because Hermes keeps the active profile in a context variable that new threads do not inherit. Settings are read again for each pass, and an invalid value falls back instead of failing the pass.
- **Learning from Hermes' history.** On the first passes the plugin replays the last 14 days of the profile's `<HERMES_HOME>/state.db`, read-only and 25 sessions at a time, through the trackers the hooks use: tool results become tool counts and failure signatures, user messages become turns and owner feedback, and skills loaded with `skill_view` are charged with the outcome of their turn. Messages sent before the agent answered join the same turn. Only history older than the moment the plugin started observing the profile is read (for an upgrade, the start of the earliest day with live data), and each slice is written in one transaction together with its cursor, so nothing is counted twice. Budget per pass: 20 seconds in the autopilot, 120 seconds in `learn`. Statuses: `done`, `running`, `waiting`, `no_history`, `unreadable` and `unsupported_history`. On a copy of a real profile: 275 sessions, 436 turns and 6,731 tool calls in 1.2 s and 29 MB, producing 7 lessons on the first pass.
- **Runtime guard** (`runtime_guard`, on by default), a `pre_tool_call` callback for terminal-like tools. It vetoes commands that would stop, restart or kill the gateway the agent runs on (`systemctl` restart, stop, kill and similar actions on a `hermes-gateway` unit, `service` restart or stop of it, `pkill` or `killall` of hermes, `hermes gateway stop`, also one level inside `bash -c` and similar shells) and tells the agent to use `hermes gateway restart`, which waits for running turns. It also vetoes `rm`, `rmdir`, `unlink`, `shred`, `mv` and `find` with `-delete` aimed at the running interpreter and its environment, the Hermes source, the Hermes root, the profile, a folder above any of them, or a glob that matches one. Only the command word counts, after variable assignments and wrappers such as `sudo` or `timeout`. Here-document bodies are ignored, and commands over 20,000 characters are let through unparsed. Vetoes are counted by kind in the new table `guard_daily`.
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
- An unreadable `settings.json` no longer discards the values set in Hermes' settings form. Lessons stay `off` until the file is fixed, and every other option keeps the form's value.
- Two active lessons that render the same sentence put it in the prompt once.
- The digest says "1 lesson awaits approval" in the singular.

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
