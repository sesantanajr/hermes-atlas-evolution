# Getting started

**English** · [Português (Brasil)](../pt-BR/getting-started.md) · [Documentation home](README.md)

From nothing to a Hermes that learns from its mistakes, in about ten minutes.

## 1. What you need

- **Hermes Agent 0.23.1 or later**, with `hermes --version` working in your terminal.
- `git` (Hermes uses it to install plugins).
- Nothing else: the plugin uses Python's standard library only.

## 2. Install

Hermes loads plugins **per profile**. Pick one of the two ways.

**One profile** (the default one, or a named one with `-p`):

```bash
hermes plugins install sesantanajr/hermes-atlas-evolution --enable
hermes -p work plugins install sesantanajr/hermes-atlas-evolution --enable
```

**Every profile at once**, with the installer:

```bash
git clone https://github.com/sesantanajr/hermes-atlas-evolution.git
sh hermes-atlas-evolution/install.sh                 # lists your profiles and asks which (Enter = all)
sh hermes-atlas-evolution/install.sh --all --yes     # no questions
```

The installer also takes `--profiles default,work`, `--install-missing` (install where the plugin is missing) and `--restart` (restart the gateway at the end, after checking that its service can start).

Installing loads the plugin into a running gateway: no restart is needed. The dashboard shows the **Evolution** tab once it reloads its plugins (restart the dashboard if the tab does not appear).

## 3. The first hour

1. **Two minutes after loading**, the plugin's clock starts the first **learning pass** in the background. Want it now? Run `hermes evolution learn`.
2. The first pass **replays up to 14 days** of the profile's history (sessions, tool calls, turns), so lessons and numbers do not start from zero.
3. It reads **every profile** of this Hermes (plugins, skills, memory) and runs **Hermes' own doctor checks** offline.
4. In **Auto mode** (the default) it already applies what is safe and reversible: duplicate memory entries out, a skill the curator keeps rewriting locked, old tool versions freed, the plugin installed in other profiles in use. Everything goes to the history with its undo.

Then look at it:

```bash
hermes evolution          # the menu (your own terminal)
hermes evolution status   # one page: what needs attention, the Evolution index, health
```

## 4. Make it yours

**Choose a mode.** Auto acts by itself on what is safe; Assist only removes identical memory repeats; Manual only measures and proposes.

```bash
hermes config set plugins.entries.hermes-atlas-evolution.settings.mode assist
```

**Tell it who you are in chats.** Your CLI, TUI and Desktop always count as you. For Telegram, Discord or the API server, mark your sender id or a channel that is only yours, so your failures teach and your chats get every lesson:

```bash
hermes evolution owner                     # each channel with its senders
hermes evolution owner add 123456789       # prints the `hermes config set` command that marks you
```

**Get the weekly digest in your chat**, as a Hermes cron job with no agent and no tokens:

```bash
hermes evolution schedule-digest --deliver telegram
```

## 5. Every week

- **The Evolution index** in `status` (and on the dashboard's Overview) says whether the agent is getting better: a score from 0 to 100 with its parts and its trend against a week ago; the parts show which one moved.
- **Proposals** wait for one key: `hermes evolution proposals list`, or the menu. Approving one the plugin can apply runs Hermes' own command; rejecting it keeps the Auto mode from ever applying it.
- **Lessons** a model drafted wait for you: `hermes evolution lessons review`.

## 6. Update and remove

```bash
hermes evolution update --all      # this plugin in every profile (a pinned profile moves to the latest commit)
hermes gateway restart             # once, to load the new code
```

To remove it from a profile: `hermes -p <profile> plugins remove hermes-atlas-evolution`. Its data stays in `<profile home>/evolution/` until you delete that folder.

**Next:** [The evolution loop](evolution-loop.md) explains how everything works · [Troubleshooting](troubleshooting.md) when something looks wrong.
