# Contributing to Atlas Evolution

[![Checks](https://github.com/sesantanajr/hermes-atlas-evolution/actions/workflows/checks.yml/badge.svg)](https://github.com/sesantanajr/hermes-atlas-evolution/actions/workflows/checks.yml)

Thank you for considering a contribution. This project is a plugin for Hermes Agent that runs inside other people's agents, so three things matter more than anything else: it must never slow down or break a turn, it must never store what users say, and the operator decides what reaches the agent.

> **Em português:** abra uma issue antes de uma mudança grande; problemas de segurança vão pelo [SECURITY.md](SECURITY.md). A suíte de testes é mantida fora deste repositório; o CI daqui roda lint, compilação e um teste de fumaça (só biblioteca padrão, Python 3.11 ou mais novo). A documentação é bilíngue: o `README.md` tem as duas línguas numa página só (a seção em português e a em inglês mudam juntas), e o que muda em `docs/en-US/` muda igual em `docs/pt-BR/`; as imagens ficam no ramo `assets` (o Hermes copia o ramo padrão inteiro para cada perfil), e toda frase que uma pessoa ou o agente lê precisa do par em português no `hermes_atlas_evolution/catalog.py`. A documentação descreve o que o plugin faz hoje; o histórico de versões fica só no CHANGELOG. As regras de privacidade e segurança abaixo valem para qualquer pull request.

## Before you start

- Open an issue for anything larger than a small fix, so the approach can be agreed first.
- Security problems go through [SECURITY.md](SECURITY.md), not public issues.
- Documentation is bilingual. `README.md` holds both languages on one page: a change to its English section needs the same change in its Portuguese section, and a change to `docs/en-US/` the same change in `docs/pt-BR/`. `README.pt-BR.md` only points to the Portuguese section. If you do not write Portuguese, say so in the pull request and a maintainer will translate.
- Images (screenshots, the banner) live on the `assets` branch, never on `main`: Hermes copies the default branch into every profile it installs the plugin in. Reference them as `https://raw.githubusercontent.com/sesantanajr/hermes-atlas-evolution/assets/<path>`.
- So is everything a person or the agent reads. A new message needs its pt-BR pair in `hermes_atlas_evolution/catalog.py` (a pattern when it carries values); the i18n test of the external suite walks every `raise ValueError(...)` and `raise SystemExit(...)` of the package and fails when one has no Portuguese form.

## Development setup and checks

You need Python 3.11 or later. There is nothing to install: the plugin uses the standard library only.

```sh
git clone https://github.com/sesantanajr/hermes-atlas-evolution
cd hermes-atlas-evolution
python -m compileall -q hermes_atlas_evolution dashboard __init__.py
python -m hermes_atlas_evolution --home /tmp/evolution-test update --list
```

The unit test suite is maintained outside this repository, so the published plugin carries only what Hermes loads. GitHub Actions ([`.github/workflows/checks.yml`](.github/workflows/checks.yml)) runs `ruff` for undefined names, unused imports and syntax the oldest supported Python refuses, byte-compiles everything with Python 3.11, checks `install.sh` and runs a smoke test of the standalone CLI on Linux (Python 3.11 to 3.14), macOS and Windows. Compute dates with `experience.utc_datetime` or `utc_day`, never `datetime.fromtimestamp`, which refuses times before 1970 on Windows.

With a Hermes Agent 0.23.1 installation, also run the admission checks. `validate` must report the security scan as `safe`:

```sh
hermes plugins validate . --json
hermes plugins doctor --ci hermes-atlas-evolution   # with the plugin installed in a test profile
```

To try a command, point the standalone runner at a throwaway profile directory. Do not test against a profile you use.

```sh
mkdir /tmp/evolution-test
python -m hermes_atlas_evolution --home /tmp/evolution-test report
```

## Code style

- **Standard library only.** The plugin has no runtime dependency. Optional integrations are imported lazily and must degrade when the package is missing.
- **Public plugin API only.** Use `ctx` and the modules of the published Hermes plugin API. The one exception is read-only introspection of what Hermes has already loaded (the health radar's plugin list, the guard's view of where Hermes lives, the Hermes version the command reads, the session origin the turn hooks ask for, the plugin isolation mode `status` reads, the administrator's managed configuration files and the `hermes` launcher next to the loaded `hermes_cli`): look the module up in `sys.modules` without importing it, wrap the call, and skip the reading when it is missing or fails. `hermes plugins validate` lists imports of private Hermes modules. Look up optional `ctx` methods with `getattr` so that an older or stricter host still loads the plugin. Hermes' doctor checks are the one place that calls Hermes internals (`hermes_cli.doctor.DOCTOR_CHECKS`): only in a separate short-lived process of Hermes' Python, never in the plugin's process, and a Hermes without them must leave the plugin working and say so.
- **Tests use `unittest`** and temporary directories. A fix comes with a regression test that fails without it. Close every connection a test opens (`addCleanup`): the suite should print no `ResourceWarning`.
- **The dashboard panel** (`dashboard/`) is plain JavaScript on the host's plugin SDK, with no build step and no dependency. It renders text with React elements, never `innerHTML`. Its API reads through one `GET` route and decides through one `POST` route, only for a logged-in owner ([the dashboard panel](docs/en-US/hermes-integration.md#the-dashboard-panel)).
- **Thresholds are named constants** at the top of their module. When you change one, update `docs/en-US/evolution-loop.md` and `docs/pt-BR/evolution-loop.md` in the same pull request.
- **Validate at the edge.** Functions that receive settings or hook payloads check types and ranges and raise `ValueError` with a message an operator can act on. Inside the gateway, settings fall back to safe defaults instead.
- **Bound every read.** Files and databases that belong to Hermes are opened read-only, with a size limit and, where a scan can be slow, a deadline.
- There is no formatter configuration in the repository. Follow the style of the module you are editing: a module docstring that says what the module is for, type hints, and comments only where the reason is not obvious.

## Translations

Everything a person or the agent reads exists in English and Brazilian Portuguese, and the catalog must stay complete.

- A new sentence in an agent tool reply, a refusal, an error message or the command help needs its pair in `hermes_atlas_evolution/catalog.py`: an exact `(English, Portuguese)` entry in `PAIRS`, or a pattern in `PATTERNS` when it carries values. The i18n test of the external suite extracts the command help and the error messages from the code and fails when one lacks its pair. For any other new text, add the pair and a test that the output comes out translated.
- A new lesson template in `lessons.TEMPLATES`, a digest label in `report._TEXT`, a `status` label in `status._TEXT` and a reflector instruction in `reflector.INSTRUCTIONS` are written in both languages next to each other.
- Codes, field names, identifiers and numbers are never translated.
- Write Portuguese as spoken in Brazil, with the same meaning as the English, not a word-for-word copy.

## Privacy and safety rules

Every change must keep these. A pull request that breaks one will not be merged.

1. **No content is stored or logged.** Conversation text, tool arguments, tool output, memory content and raw log lines may be inspected in memory to classify an event, and are then discarded. The only exception is the reflector's scrubbed excerpt, which exists only while the reflector is on.
2. **Only safe values reach the database.** A stored value is a counter, a member of a fixed list, or a label that passed `experience.label` or `signals._clean_hint`. Session and turn identifiers are stored only as hashes.
3. **No network access of the plugin's own and no telemetry.** A model may be called only through Hermes' `ctx.llm`, by a feature the operator can switch off with its own setting, within a per-pass and daily cap, and its output starts in quarantine. The reflector is the only such feature; it is on by default, and its drafts stay in quarantine in every mode.
4. **Hooks fail open and stay cheap.** A hook callback never raises into a turn. Hook writes go through `experience.record` or `operations.record`, which queue the update while the database is busy.
5. **The plugin changes only its own data.** Anything that would change Hermes (a skill, a setting) is shown as a command for the operator.
6. **Only fixed templates reach the agent without approval, and only the operator decides.** Text written by a model, by the agent or by a person reaches the agent only after the operator approves it. Lessons rendered from the fixed templates in the code are active at once in the default `auto` mode, are measured, and are retired when they do not help; `suggest` makes them wait for approval too. New surfaces that show learned text to the agent are off until the operator turns them on. The runtime guard's refusal, on by default, is fixed text in the code, not learned content. A command that activates, approves or retires something calls the operator guard (`cli._operator_only`) and shows the exact text before it takes effect.
7. **Refused approvals are never advice.** No signal, template or draft may teach the agent to reach an outcome the user's approval refused.
8. **Prove it in a test.** When you add a signal, add a test that feeds it sensitive-looking input and asserts that the text is absent from the stored rows and from the command output.

## Documentation rule

Every command, option, setting key, hook name and number in the documentation must exist in the code. Describe what the plugin does now, in short sentences that tell the reader what to do; version history belongs only in `CHANGELOG.md` and `CHANGELOG.pt-BR.md`. `SECURITY.md` and this file are in English with a short Portuguese summary at the top: keep it in step. Check commands with `python -m hermes_atlas_evolution --home /tmp/x <command> --help`, and state plainly what has not been verified. Do not add benchmarks, user counts or results that were not measured.

Keep the README short. It covers installing, updating, migrating, what the plugin does, the main commands and settings, and a short privacy list; everything else goes to `docs/en-US/` and `docs/pt-BR/`, with a link from the README. Development instructions live here, not in the README.

Hermes scans every file of a plugin before it installs or updates it, and a critical finding blocks both, with no override. Do not quote prompt-injection phrases verbatim in the documentation or the skill; describe what a filter refuses instead. Before a release, `hermes plugins validate .` must report the security scan as passed.

## Where to help

None of these exists yet. Open an issue first, so the approach can be agreed.

- A bank of regression evaluations, to test a change against fixed tasks and not only against the following week.
- Sending the skills that fail most, by outcome, to an optimizer such as GEPA through NousResearch's `hermes-agent-self-evolution`, with the results going through the same proposal queue.
- Field results: run the loop on a real gateway for a few weeks and compare the lesson verdicts and the skills flagged by outcome with a manual review of the same turns. [Hermes integration](docs/en-US/hermes-integration.md#where-the-next-validation-has-the-most-value) lists where validation has the most value.

## Pull requests

- Keep one change per pull request and describe how you tested it.
- Add a line to `CHANGELOG.md` and `CHANGELOG.pt-BR.md` under the next version.
- By contributing you agree that your work is released under the [MIT license](LICENSE).
