---
name: evolution-review
description: Run the Atlas Evolution review with the owner - read the digest, explain what failed and what was learned, and walk the owner through the open proposals. Use when asked for the weekly evolution review, what the agent learned, why it keeps failing at something, or which improvements are pending.
---

# Evolution review

Atlas Evolution measures this profile and proposes improvements. In Assisted mode (the default) it only removes, by
itself, a memory entry repeated word for word between MEMORY.md and USER.md (it keeps a copy of the file first). In
Auto mode, which only the owner turns on, it also applies by itself safe changes that can be undone: it locks skills
the curator keeps rewriting, installs itself from the Hermes plugin catalog in profiles in use that never had it,
moves failed installs to its trash, repairs the gateway's service when no gateway is alive and frees old tool versions
with a weekly `hermes pm gc` (which cannot be undone), each in the history. It never updates itself: updates come
from Hermes. Everything else waits for the owner. In this conversation you
inform and recommend; the owner decides in their own terminal. Answer in the owner's language (Brazilian Portuguese
or English), with the plugin's names for things: "Auto mode / Modo Auto", "Assisted / Assistido", "Manual / Manual",
"lessons waiting for you / lições esperando você", "learning pass / rodada de aprendizado", "built-in template /
padrão fixo", "Runtime guard / Proteção do Hermes", "Hermes doctor / checagem do Hermes (`hermes doctor`)", "the menu
/ o menu". In Portuguese, never write English words inside the sentence; quote Hermes' own English text as such
("texto do Hermes, em inglês").

Commands name the profile: `hermes -p <profile> evolution …`, with the profile this conversation runs in (`default`
is the root profile). `status` names it in its title, and every command it and the digest print already carries it;
copy those as they are. Without `-p`, Hermes may follow another profile that `hermes profile use` made sticky.

## Steps

1. Run `hermes -p <profile> evolution status` (add `--lang pt` for Portuguese). Mention first what it lists under
   "Needs attention / Pede atenção" (no events today, a failed learning pass, a busy profile without the plugin, a
   setting it could not use). Then read the digest: `hermes -p <profile> evolution report --days 7`. Both only read
   (except `status` in a profile that never had a learning pass, where it runs the first one; `--no-learn` only
   reads). `hermes -p <profile> evolution learn` runs a learning pass now; with the reflector on it may ask the model
   for up to 3 lesson drafts, which wait for the owner.
2. Summarize the digest in this order: what changed versus the previous week (the verdict line); the failures that
   repeat, most frequent first, and whether each has a lesson; skills that do worse than the profile as a whole; the
   self-improvement loop (background reviews, how many changed nothing, the most rewritten skills); lessons with a
   measured effect, the ones that helped and the ones that did not; the Hermes health section.
3. List the open proposals with `hermes -p <profile> evolution proposals list`, highest risk first. For each one say, in plain
   words, what it is about, the numbers behind it, what approving does, and the risk as a word ("high risk / risco
   alto", "medium / médio", "low / baixo"), never as a code. Give your recommendation and why.
   - Lesson text: from your shell the plugin leaves out text written by the owner, a model or an agent (`lesson:
     null`, `text_hidden: true`), because this shell may be answering someone else's chat. Do not guess it. Say it is
     hidden here and that the owner reads it in the menu `hermes -p <profile> evolution` (option 1 "Lessons waiting
     for you / Lições esperando você", option 2 "Open proposals / Propostas abertas"), or in the dashboard's panel
     with a login. Lessons from the plugin's built-in templates are shown in `hermes -p <profile> evolution lessons
     list`.
4. Hand the decisions to the owner, to run in their own terminal: the menu `hermes -p <profile> evolution`, or a
   command such as `hermes -p <profile> evolution proposals approve 12` or `… proposals reject 12`. Rejecting a
   lesson's proposal turns that lesson down for good; rejecting a proposal to retire a lesson keeps it active; any
   other rejected proposal comes back after 30 days only if the situation is still there. For a change the plugin can apply (a skill lock, a
   change in another profile) approving applies it at once with Hermes' own command and records its undo; the owner
   does not run `hermes curator lock` separately. For a change only the owner can apply (archiving a skill, a
   Hermes setting) approving shows the command to run. Your own shell cannot approve, reject or activate anything:
   the plugin refuses it by design.
5. Close with what was decided, what will be measured next week, and the one next step the owner takes (a command
   to run or a menu option), so every recommendation ends with something to do.

## Rules

- Report only numbers that appear in the digest or the status. Where they say "not measured", say so.
- A verdict compares the week before with the week after a change: a correlation. Mention concurrent changes when
  there are any.
- Never call a high-risk proposal routine, and never present a hidden lesson as if you had read it.
- To suggest a new lesson, use the `hermes_atlas_evolution_lessons` tool with `action: propose` (when the owner has
  turned the agent tools on). It waits in the owner's queue.
