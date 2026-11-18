## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- The problem, with the evidence you have. -->

## How it was tested

<!-- Commands you ran and what they showed. -->

- [ ] `python -m unittest discover -s tests -v` passes, with the suite of the `tests` branch checked out in `tests/` (see CONTRIBUTING.md)
- [ ] It also passes with the Python interpreter of a Hermes installation, or I had none available
- [ ] `hermes plugins validate .` passes without warnings, or I could not run it (say why)
- [ ] A fix comes with a regression test that fails without it, pushed to a `tests-<your-branch>` branch (or the PR says it is needed)

## Privacy and safety

- [ ] No conversation text, tool argument, tool output, memory content or raw log line is stored or logged
- [ ] Any new stored value is a counter, a fixed label or a filtered identifier, and a test proves it
- [ ] Hook callbacks still fail open and write through the shared observer connection
- [ ] Nothing new reaches the agent without the operator's approval or setting, and a new change to Hermes is reversible, recorded with its undo and follows the mode (Manual changes nothing by itself)
- [ ] Decisions still refuse to run in an agent's shell, and a refused approval never becomes advice
- [ ] No network access of its own and no self-updating code (catalog rule 3), no new model call outside the reflector, and no new runtime dependency
- [ ] No child process inherits `HERMES_YOLO_MODE`, `HERMES_YOLO` or `HERMES_ACP_AUTO_APPROVE`, and nothing reads `.env`, `auth.json` or another credential store (catalog rules 11 and 12)

## Documentation

- [ ] Both language sections of `README.md`, and `docs/en-US/` and `docs/pt-BR/`, are updated together
- [ ] The README's Disclosure section still matches the code (catalog rule 13), and the README stays Markdown only, with no raw HTML
- [ ] New text a person or the agent reads has its English and Portuguese pair in `hermes_atlas_evolution/catalog.py`
- [ ] Every command, option, setting and number in the docs exists in the code, and commands read `hermes -p <profile> evolution …`
- [ ] No shipped file shows a command piped into a shell (Hermes' install scan reads every file)
- [ ] `CHANGELOG.md` and `CHANGELOG.pt-BR.md` have an entry
