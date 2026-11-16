---
name: evolution-review
description: Run the Atlas Evolution review with the owner - read the digest, explain what failed and what was learned, and walk the owner through the open proposals. Use when asked for the weekly evolution review, what the agent learned, why it keeps failing at something, or which improvements are pending.
---

# Evolution review

Atlas Evolution measures this profile and proposes improvements. It never changes Hermes by itself, and
only the owner can decide on its proposals. This skill is the conversation in which the owner decides.
You inform and recommend; the owner runs the decisions in their own terminal.

## Steps

1. Check that the plugin is working here with `hermes evolution status` (add `--lang pt` for Portuguese). It
   shows today's events, the last background pass and any busy profile that lacks the plugin; mention those
   first; a profile marked with both copies still runs the old 0.5.0 code. Then get the digest with
   `hermes evolution report --days 7`. The loop runs by itself in the background, so the numbers are recent.
   `report` only reads; `status` only reads too, except in a profile where no pass ever ran, where it runs
   the first one. `hermes evolution learn` runs a pass now: it measures and, with the reflector on, may ask
   the model for up to 3 lesson drafts, which stay in quarantine.
2. Summarize it in the owner's language, in this order:
   - what changed versus the previous week (the verdict line);
   - the failures that repeat, most frequent first, and whether each already has a lesson;
   - skills by outcome: which skills go worse than the profile as a whole;
   - the self-improvement loop: how many background reviews ran, how many produced nothing, and which
     skills were rewritten the most;
   - lessons with a measured effect, naming the ones that worked and the ones that did not;
   - the "Hermes health" section: sessions that end as orphans, the context per call, weak compactions,
     plugin problems and commands the runtime guard vetoed.
3. List the open proposals with `hermes evolution proposals list`, highest risk first. For each one, read
   out the exact lesson text when there is one, the evidence behind it, and what its command does. Give your
   recommendation and the reason.
4. Hand the decisions to the owner. Give them the commands to run in their own terminal, for example
   `hermes evolution proposals approve 12` or `hermes evolution proposals reject 12`, and for native changes
   the command the proposal prints, such as `hermes curator lock <skill>`. Commands you run yourself cannot
   approve, reject or activate anything; the plugin refuses them by design.
5. Close with what was decided and what will be measured next week.

## Rules

- Report only numbers that appear in the digest. Where it says "not measured", say so.
- A verdict compares the week before with the week after a change. It is a correlation: mention the
  number of concurrent changes when it is not zero.
- Never present a lesson without its exact text, and never call a high-risk proposal routine.
- To suggest a new lesson, use the `hermes_atlas_evolution_lessons` tool with `action: propose`. It is stored in
  quarantine and appears in the queue for the owner.
