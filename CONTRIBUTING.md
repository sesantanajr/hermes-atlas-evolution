# Contributing to Atlas Evolution

Thank you for considering a contribution. This project is a plugin for Hermes Agent that runs inside other people's agents, so three things matter more than anything else: it must never slow down or break a turn, it must never store what users say, and the operator decides what reaches the agent.

## Before you start

- Open an issue for anything larger than a small fix, so the approach can be agreed first.
- Security problems go through [SECURITY.md](SECURITY.md), not public issues.
- Documentation is bilingual. A change to `README.md` or `docs/` needs the same change in `README.pt-BR.md` or `docs/pt-BR/`. If you do not write Portuguese, say so in the pull request and a maintainer will translate.

## Development setup and tests

You need Python 3.11 or later. There is nothing to install: the plugin and its tests use the standard library only.

```sh
git clone https://github.com/sesantanajr/hermes-atlas-evolution
cd hermes-atlas-evolution
python -m unittest discover -s tests -v
```

The same suite runs on GitHub Actions for Python 3.11, 3.12 and 3.13 on every push and pull request ([`.github/workflows/tests.yml`](.github/workflows/tests.yml)).

When Hermes Agent is installed, run the suite a second time with the Python interpreter of that installation, the one that can `import agent.redact`. The tests then also exercise Hermes' native redactor and the interpreter your users run:

```sh
/path/to/hermes/python -c "import agent.redact"          # check that it is Hermes' interpreter
/path/to/hermes/python -m unittest discover -s tests -v
```

With a Hermes Agent 0.23.1 installation, also run the admission checks. `validate` must report the security scan as `safe`:

```sh
hermes plugins validate . --json
hermes plugins doctor --ci hermes-atlas-evolution   # with the plugin installed in a test profile
```

To try a command, point the standalone runner at a throwaway profile directory. Do not test against a profile you use.

```sh
mkdir /tmp/evolution-test
python -m hermes_atlas_evolution --home /tmp/evolution-test --hermes-version 0.23.1 report
```

## Code style

- **Standard library only.** The plugin has no runtime dependency. Optional integrations are imported lazily and must degrade when the package is missing.
- **Public plugin API only.** Use `ctx` and the modules of the published Hermes plugin API. The one exception is read-only introspection of what Hermes has already loaded (the health radar's plugin list and the guard's view of where Hermes lives): look the module up in `sys.modules` without importing it, wrap the call, and skip the reading when it is missing or fails. `hermes plugins validate` lists imports of private Hermes modules. Look up optional `ctx` methods with `getattr` so that an older or stricter host still loads the plugin.
- **Tests use `unittest`** and temporary directories. A fix comes with a regression test that fails without it.
- **Thresholds are named constants** at the top of their module. When you change one, update `docs/evolution-loop.md` and `docs/pt-BR/evolution-loop.md` in the same pull request.
- **Validate at the edge.** Functions that receive settings or hook payloads check types and ranges and raise `ValueError` with a message an operator can act on. Inside the gateway, settings fall back to safe defaults instead.
- **Bound every read.** Files and databases that belong to Hermes are opened read-only, with a size limit and, where a scan can be slow, a deadline.
- There is no formatter configuration in the repository. Follow the style of the module you are editing: a module docstring that says what the module is for, type hints, and comments only where the reason is not obvious.

## Translations

Everything a person or the agent reads exists in English and Brazilian Portuguese, and the catalog must stay complete.

- A new sentence in a diagnostic, plan, recommendation, Kanban card, agent tool reply, error message or command help needs its pair in `hermes_atlas_evolution/catalog.py`: an exact `(English, Portuguese)` entry in `PAIRS`, or a pattern in `PATTERNS` when it carries values. `tests/test_i18n.py` extracts the diagnostic sentences, the command help, the Kanban card texts and the plans from the code and fails when one lacks its pair. For any other new text, add the pair and a test that the output comes out translated.
- A new lesson template in `lessons.TEMPLATES`, a digest label in `report._TEXT`, a `status` label in `status._TEXT` and a reflector instruction in `reflector.INSTRUCTIONS` are written in both languages next to each other.
- Codes, field names, identifiers and numbers are never translated.
- Write Portuguese as spoken in Brazil, with the same meaning as the English, not a word-for-word copy.

## Privacy and safety rules

Every change must keep these. A pull request that breaks one will not be merged.

1. **No content is stored or logged.** Conversation text, tool arguments, tool output, memory content and raw log lines may be inspected in memory to classify an event, and are then discarded. The only exception is the reflector's scrubbed excerpt, which exists only while the reflector is on.
2. **Only safe values reach the database.** A stored value is a counter, a member of a fixed list, or a label that passed `experience.label` or `signals._clean_hint`. Session and turn identifiers are stored only as hashes.
3. **No network access of the plugin's own and no telemetry.** A model may be called only through Hermes' `ctx.llm`, by a feature the operator can switch off with its own setting, within a per-pass and daily cap, and its output starts in quarantine. The reflector is the only such feature; it is on by default since 0.5.0, and its drafts stay in quarantine in every mode.
4. **Hooks fail open and stay cheap.** A hook callback never raises into a turn. Hook writes go through `experience.record` or `operations.record`, which queue the update while the database is busy.
5. **The plugin changes only its own data.** Anything that would change Hermes (a skill, a setting, a Kanban card outside the explicit handoff) is shown as a command for the operator.
6. **Only fixed templates reach the agent without approval, and only the operator decides.** Text written by a model, by the agent or by a person reaches the agent only after the operator approves it. Lessons rendered from the fixed templates in the code are active at once in the default `auto` mode, are measured, and are retired when they do not help; `suggest` makes them wait for approval too. New surfaces that show learned text to the agent are off until the operator turns them on. The runtime guard's refusal, on by default, is fixed text in the code, not learned content. A command that activates, approves or retires something calls the operator guard (`cli._operator_only`) and shows the exact text before it takes effect.
7. **Refused approvals are never advice.** No signal, template or draft may teach the agent to reach an outcome the user's approval refused.
8. **Prove it in a test.** When you add a signal, add a test that feeds it sensitive-looking input and asserts that the text is absent from the stored rows and from the command output.

## Documentation rule

Every command, option, setting key, hook name and number in the documentation must exist in the code. Check commands with `python -m hermes_atlas_evolution --home /tmp/x --hermes-version 0.23.1 <command> --help`, and state plainly what has not been verified. Do not add benchmarks, user counts or results that were not measured.

Keep the README short. It covers installing, updating, migrating, what the plugin does, the main commands and settings, and a short privacy list; everything else goes to `docs/`, with a link from the README. Development instructions live here, not in the README.

Hermes scans every file of a plugin before it installs or updates it, and a critical finding blocks both, with no override. Do not quote prompt-injection phrases verbatim in the documentation or the skill; describe what a filter refuses instead. Before a release, `hermes plugins validate .` must report the security scan as passed.

## Where to help

None of these exists yet. Open an issue first, so the approach can be agreed.

- A panel in Hermes Desktop and the Dashboard for the digest and the proposals.
- A bank of regression evaluations, to test a change against fixed tasks and not only against the following week.
- Sending the skills that fail most, by outcome, to an optimizer such as GEPA through NousResearch's `hermes-agent-self-evolution`, with the results going through the same proposal queue.
- Field results: run the loop on a real gateway for a few weeks and compare the lesson verdicts and the skills flagged by outcome with a manual review of the same turns. [Hermes integration](docs/hermes-integration.md#where-the-next-validation-has-the-most-value) lists where validation has the most value.

## Pull requests

- Keep one change per pull request and describe how you tested it.
- Add a line to `CHANGELOG.md` under the next version.
- By contributing you agree that your work is released under the [MIT license](LICENSE).
