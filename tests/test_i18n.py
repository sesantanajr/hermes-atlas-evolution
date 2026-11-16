import ast
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import i18n
from hermes_atlas_evolution.advisory import EFFORTS, KINDS, plan
from hermes_atlas_evolution.catalog import PAIRS, PATTERNS
from hermes_atlas_evolution.native_actions import HANDOFF_RULES, PLAYBOOKS
from hermes_atlas_evolution.operations import close_observers

PACKAGE = Path(__file__).resolve().parent.parent / "hermes_atlas_evolution"
PORTUGUESE = re.compile(r"[ãõçáéíóúâêôàÁÉÍÓÚ]|\b(?:não|são|para|com|uma|pelo|pela|está|foram|antes|depois|rotina|falha|"
                        r"memória|os|as|de|do|da|em|que)\b", re.I)
DIAGNOSTIC_MODULES = ("reasoning", "diagnostics", "workloads", "native_signals", "linux_host", "inventory", "ops_metrics")


def literals(module: str):
    """Human sentences written in a module, excluding docstrings, SQL and regular expressions."""
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            value = node.value
            if (" " in value and len(value) >= 12 and not value.lstrip().upper().startswith(("SELECT", "INSERT", "CREATE",
                                                                                              "UPDATE", "DELETE", "WITH"))
                    and not re.search(r"[\\^$|()\[\]]{3,}", value)):
                yield node.lineno, value


class CatalogTest(unittest.TestCase):
    def test_pairs_are_unique_in_both_directions(self):
        english = [en for en, _ in PAIRS]
        portuguese = [pt for _, pt in PAIRS]
        self.assertEqual(len(english), len(set(english)))
        self.assertEqual(len(portuguese), len(set(portuguese)))
        for en, pt in PAIRS:
            self.assertNotEqual(en, pt)
            self.assertEqual(en.endswith("."), pt.endswith("."), en)

    def test_every_portuguese_diagnostic_sentence_has_an_english_pair(self):
        catalogued = {pt for _, pt in PAIRS}
        fragments = {"A medição de ", " do dia está acima da faixa recente."}   # parts of a catalogued pattern
        missing = [f"{module}:{line}: {value}" for module in DIAGNOSTIC_MODULES for line, value in literals(module)
                   if PORTUGUESE.search(value) and value not in catalogued and value not in fragments]
        self.assertEqual(missing, [])

    def test_every_help_text_and_planning_sentence_has_a_portuguese_pair(self):
        catalogued = {en for en, _ in PAIRS}
        cli = (PACKAGE / "cli.py").read_text(encoding="utf-8")
        helps = re.findall(r'help=_\("([^"]+)"\)', cli)
        self.assertGreater(len(helps), 30)
        self.assertEqual([item for item in helps if item not in catalogued], [])
        self.assertEqual([item for item in [*PLAYBOOKS.values(), HANDOFF_RULES] if item not in catalogued], [])
        for kind in KINDS:
            for effort in EFFORTS:
                portuguese = json.dumps(i18n.localize(plan(kind, effort, durable=effort == "deep"), "pt"), ensure_ascii=False)
                left = [value for value in re.findall(r'"([A-Z][^"]{10,})"', portuguese) if not PORTUGUESE.search(value)]
                self.assertEqual(left, [], f"{kind}/{effort}")

    def test_patterns_translate_both_ways_and_keep_identifiers(self):
        self.assertEqual(i18n.text("A medição de tokens de entrada do dia está acima da faixa recente.", "en"),
                         "Today's input tokens is above the recent range.")
        self.assertEqual(i18n.text("Today's research time is above the recent range.", "pt"),
                         "A medição de tempo de pesquisa do dia está acima da faixa recente.")
        self.assertEqual(i18n.text("Unknown proposal id 12", "pt"), "Id de proposta desconhecido: 12")
        self.assertEqual(i18n.text("Proposal 3 is already applied", "pt"), "A proposta 3 já está applied")
        self.assertEqual(len(PATTERNS), len({entry[0] for entry in PATTERNS}))

    def test_localize_changes_sentences_only(self):
        source = {"code": "tool_errors", "severity": "warning", "count": 3, "action": "Review provider credential status using Hermes diagnostics.",
                  "nested": [{"hypothesis": "Há recusas HTTP de autenticação ou autorização."}], "label": "web_search",
                  "free": "A sentence nobody catalogued."}
        english = i18n.localize(source, "en")
        portuguese = i18n.localize(source, "pt")
        self.assertEqual(english["nested"][0]["hypothesis"], "There are HTTP authentication or authorization refusals.")
        self.assertEqual(portuguese["action"], "Revise o estado das credenciais do provedor com o diagnóstico do Hermes.")
        for result in (english, portuguese):
            self.assertEqual((result["code"], result["severity"], result["count"], result["label"], result["free"]),
                             ("tool_errors", "warning", 3, "web_search", "A sentence nobody catalogued."))
        self.assertIs(i18n.localize(source, "fr"), source)


class ResolveTest(unittest.TestCase):
    def test_order_explicit_then_hermes_then_locale_then_english(self):
        clean = {key: "" for key in ("HERMES_LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")}
        with patch.dict(os.environ, clean):
            self.assertEqual(i18n.resolve("pt-BR", {"display": {"language": "en"}}), "pt")
            self.assertEqual(i18n.resolve("auto", {"display": {"language": "pt"}}), "pt")
            self.assertEqual(i18n.resolve(None, {"display": {"language": "zh"}}), "en")
            with patch.dict(os.environ, {"LANG": "pt_BR.UTF-8"}):
                self.assertEqual(i18n.resolve(None, {}), "pt")
                with patch.dict(os.environ, {"HERMES_LANGUAGE": "en"}):
                    self.assertEqual(i18n.resolve(None, {"display": {"language": "pt"}}), "en")
        for tag, expected in (("pt", "pt"), ("PT_br", "pt"), ("en-US", "en"), ("auto", None), ("", None), (7, None)):
            self.assertEqual(i18n.normalize(tag), expected)


class CommandLineLanguageTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        process_tree = patch.multiple("hermes_atlas_evolution.cli", _under_hermes=lambda **_: False,
                                      _in_hermes_cgroup=lambda **_: False)   # not the host's process tree
        process_tree.start()
        self.addCleanup(process_tree.stop)

    def run_cli(self, **arguments):
        from hermes_atlas_evolution.cli import execute

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            execute(SimpleNamespace(**arguments), self.home, runtime_config={"display": {"language": "pt"}})
        return stream.getvalue()

    def test_output_follows_the_profile_language_unless_lang_is_given(self):
        portuguese = json.loads(self.run_cli(evolution_action="ops", ops_action="plan", kind="research", effort="quick",
                                             durable=False, lang=None))
        self.assertEqual(portuguese["steps"][0], "Escreva as perguntas e as evidências necessárias para respondê-las.")
        english = json.loads(self.run_cli(evolution_action="ops", ops_action="plan", kind="research", effort="quick",
                                          durable=False, lang="en"))
        self.assertEqual(english["steps"][0], "Write the questions and evidence needed to answer them.")
        digest = self.run_cli(evolution_action="report", format="md", days=7, lang=None, schedule_spec=False)
        self.assertTrue(digest.startswith("# Atlas Evolution · resumo de 7 dias"))

    def test_errors_are_translated(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(evolution_action="proposals", proposals_action="approve", id=41, note=None, lang=None)
        self.assertEqual(str(raised.exception), "Evolution: Id de proposta desconhecido: 41")

    def test_help_is_built_in_the_profile_language(self):
        import argparse

        from hermes_atlas_evolution.cli import setup

        for language, expected in (("pt", "Imprime o resumo da evolução"), ("en", "Print the evolution digest")):
            parser = argparse.ArgumentParser(prog="hermes evolution")
            with patch("hermes_atlas_evolution.i18n.resolve", return_value=language):
                setup(parser)
            self.assertIn(expected, parser.format_help())
        for argv in (["--lang", "pt", "ops", "inspect"], ["ops", "inspect", "--lang", "pt"], ["report", "--lang", "pt"],
                     ["lessons", "list", "--lang", "pt"], ["proposals", "approve", "3", "--lang", "pt"], ["skills"]):
            parsed = parser.parse_args(argv)
            self.assertEqual(getattr(parsed, "lang", None), None if argv == ["skills"] else "pt", argv)



def raised_messages():
    """(module, line, sample) for every `raise ValueError(...)` / `raise SystemExit(...)` with a written message.
    An f-string is rendered with 7 in place of each value, the way a PATTERN must match it."""
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call) or not node.exc.args:
                continue
            name = getattr(node.exc.func, "id", None) or getattr(node.exc.func, "attr", None)
            message = node.exc.args[0]
            if name not in ("ValueError", "SystemExit"):
                continue
            if isinstance(message, ast.Constant) and isinstance(message.value, str):
                yield path.name, node.lineno, message.value
            elif isinstance(message, ast.JoinedStr):
                yield path.name, node.lineno, "".join(part.value if isinstance(part, ast.Constant) else "7"
                                                      for part in message.values)


class RaisedMessagesTest(unittest.TestCase):
    # "Evolution: <error>" only frames an error that is itself translated before it is framed.
    FRAMES = {"Evolution: 77"}

    def test_every_error_a_person_can_read_has_a_portuguese_form(self):
        found = list(raised_messages())
        self.assertGreater(len(found), 60)
        missing = [f"{module}:{line}: {sample}" for module, line, sample in found
                   if sample not in self.FRAMES and i18n.text(sample, "pt") == sample]
        self.assertEqual(missing, [])

    def test_the_legacy_command_answers_in_the_profile_language_and_scopes_local_sessions(self):
        from hermes_atlas_evolution.integration import slash

        home = Path(tempfile.mkdtemp())
        self.addCleanup(close_observers)
        local = SimpleNamespace(platform=SimpleNamespace(value="local"), chat_type="dm", chat_id="session-1", user_id="u")
        saved = slash(home, "remember project Atlas runs on the VPS", source=local, lang="pt")
        self.assertRegex(saved, r"^Salva #\d+ \(project\)\.$")
        self.assertIn("Atlas", slash(home, "recall Atlas", source=None, platform="desktop", lang="pt"))   # same scope
        self.assertEqual(slash(home, "forget abc", source=None, platform="cli", lang="pt"),
                         "O ID da memória deve ser um inteiro positivo.")
        self.assertEqual(slash(home, "forget 999", source=None, platform="acp", lang="pt"),
                         "ID de memória indisponível neste escopo.")
        self.assertEqual(slash(home, "recall nothing-like-this", source=None, platform="cli", lang="pt"),
                         "Nenhuma memória correspondente.")

    def test_the_scheduler_scripts_speak_the_chosen_language(self):
        from hermes_atlas_evolution.evolve import digest_schedule_spec
        from hermes_atlas_evolution.native_actions import schedule_spec

        home = Path(tempfile.mkdtemp())
        digest = digest_schedule_spec(home, lang="pt")["script_content"]
        tick = schedule_spec(home, lang="pt")["script_content"]
        self.assertIn("O resumo do Atlas Evolution falhou", digest)
        self.assertIn("O tick do Atlas Evolution falhou", tick)
        for script in (digest, tick):
            compile(script, "script", "exec")
            self.assertIn("O CLI do Hermes não está no PATH do agendador.", script)
        self.assertIn("Atlas Evolution tick failed", schedule_spec(home)["script_content"])

    def test_the_runner_prints_utf8_on_a_console_with_another_code_page(self):
        import sys

        from hermes_atlas_evolution.cli import _utf8_output

        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252")
        with patch.object(sys, "stdout", console), patch.object(sys, "stderr", console):
            _utf8_output()
            print("Resumo → ▲ lições")
            console.flush()
        self.assertEqual(raw.getvalue().decode("utf-8").strip(), "Resumo → ▲ lições")


if __name__ == "__main__":
    unittest.main()
