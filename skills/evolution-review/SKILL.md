---
name: evolution-review
description: Run the Atlas Evolution review with the owner - read the digest, explain what failed and what was learned, and decide the open proposals one by one. Use when asked for the weekly evolution review, what the agent learned, why it keeps failing at something, or which improvements are pending.
---

# Evolution review

Atlas Evolution measures this profile and proposes improvements. It never applies a change to Hermes by
itself. This skill is the conversation in which the owner decides.

## Steps

1. Get the digest. Run `hermes evolution report --days 7` in the terminal (add `--lang pt` for Portuguese).
   Run `hermes evolution learn` first if the owner wants the freshest numbers.
2. Summarize it in the owner's language, in this order:
   - what changed versus the previous week (the verdict line);
   - the failures that repeat, most frequent first, and whether each already has a lesson;
   - the self-improvement loop: how many background reviews ran, how many produced nothing, and which
     skills were rewritten the most;
   - lessons with a measured effect, naming the ones that worked and the ones that did not.
3. Walk through the open proposals, highest risk first. For each one say what it is, the evidence behind
   it and exactly what the command does. Then ask for a decision.
4. Apply only what the owner approved:
   - a lesson: `hermes evolution proposals approve <id>` (takes effect in new sessions);
   - anything else: approve it, then run the native command it prints, such as
     `hermes curator lock <skill>` or `hermes curator rollback <entry-id>`.
   Reject what the owner declined with `hermes evolution proposals reject <id>`.
5. Close with what was decided and what will be measured next week.

## Rules

- Report only numbers that appear in the digest. Where it says "not measured", say so.
- A verdict compares the week before with the week after a change. It is a correlation: mention the
  number of concurrent changes when it is not zero.
- Never approve a proposal on the owner's behalf, and never unlock, unpin or roll back a skill without
  an explicit yes.
- To record something the owner taught you during the review, use
  `hermes evolution lessons add "<when X, do Y>" --tool <tool>`.
