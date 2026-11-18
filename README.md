# Atlas Evolution · test suite

This branch holds only the test suite. It lives apart from `main` because Hermes clones the default branch into
every profile, so installs stay small. CI fetches this branch into `tests/` (or `tests-<branch>`, with `/` as `-`,
when a pull request needs new tests). To run it locally, from a checkout of `main`:

```bash
git fetch origin tests
mkdir -p tests && git archive FETCH_HEAD | tar -x -C tests
HOME="$PWD/.scratch" HERMES_HOME="$PWD/.scratch/.hermes" python3 -m unittest discover -s tests
```

---

_Este ramo guarda só a suíte de testes. Ele fica separado do `main` porque o Hermes clona o ramo padrão em cada
perfil, e assim a instalação continua pequena. O CI busca este ramo para `tests/` (ou `tests-<ramo>`, com `/`
trocado por `-`, quando um pull request precisa de testes novos). Para rodar localmente, a partir do `main`, use os
comandos acima._
