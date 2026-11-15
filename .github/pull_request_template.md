## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- The problem, with the evidence you have. -->

## How it was tested

<!-- Commands you ran and what they showed. -->

- [ ] `python -m unittest discover -s tests -v` passes
- [ ] `hermes plugins validate .` passes without warnings, or I could not run it (say why)
- [ ] A fix comes with a regression test that fails without it

## Privacy and safety

- [ ] No conversation text, tool argument, tool output, memory content or raw log line is stored or logged
- [ ] Any new stored value is a counter, a fixed label or a filtered identifier, and a test proves it
- [ ] Hook callbacks still fail open and write through the shared observer connection
- [ ] Nothing new reaches the agent or changes Hermes without an explicit operator setting
- [ ] No network access and no new runtime dependency

## Documentation

- [ ] `README.md` and `README.pt-BR.md`, or `docs/` and `docs/pt-BR/`, are updated together
- [ ] Every command, option, setting and number in the docs exists in the code
- [ ] `CHANGELOG.md` has an entry
