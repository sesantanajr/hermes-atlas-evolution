"""0.12.0: the terminal, the menu and the panel say each thing once, in one command shape, in the reader's language,
and keep to themselves what only the owner may read."""

import contextlib
import io
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import cli, lessons, menu, proposals, report, status
from hermes_atlas_evolution.evolve import CONFIG_UNREAD, options
from hermes_atlas_evolution.experience import experience

from test_cli_090 import FORM, NOW, Base
from test_menu_090 import MenuBase
from test_panel_090 import load_api, runtime

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "hermes-atlas-evolution"
SECRET = "When the CSV import fails on encoding, convert it to UTF-8 with iconv."
ACTIVE = "Before deploying anything, run the smoke tests in staging first."


class Typed(io.StringIO):
    """Answers typed in a terminal."""

    def isatty(self):
        return True


class UiBase(Base):
    def settings_json(self, **evolution):
        (self.home / "evolution").mkdir(exist_ok=True)
        (self.home / "evolution" / "settings.json").write_text(json.dumps({"evolution": evolution}), encoding="utf-8")

    def typed(self, argv, answers="", *, language="pt"):
        """(stdout, stderr, the SystemExit or None) of a command run in a terminal that answers `answers`."""
        args = self.parser(language).parse_args(argv)
        out, err, ended = io.StringIO(), io.StringIO(), None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("sys.stdin", Typed(answers)):
            try:
                cli.execute(args, self.home, runtime_config=FORM)
            except SystemExit as error:
                ended = error
        return out.getvalue(), err.getvalue(), ended

    def refused(self, argv, *, language="pt") -> str:
        """What argparse says, on stderr, when it refuses `argv`."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ended:
            self.parser(language).parse_args(argv)
        self.assertEqual(ended.exception.code, 2)
        return err.getvalue()

    def learned_in_english(self):
        """A built-in template lesson whose proposal was written by an English learning pass."""
        self.failures()
        with patch("time.time", return_value=NOW):
            self.run_cli(["learn", "--lang", "en"])


class TrustedSettingsTest(UiBase):
    """A3: settings.json, which the agent's file tools can write, cannot widen what the plugin does."""

    def test_hints_and_a_higher_reflector_limit_count_only_from_hermes_configuration(self):
        self.settings_json(tool_hints=True, reflector_daily_limit=40)
        chosen = options(cli.effective_settings(self.home, {}), {}, strict=False)
        self.assertEqual((chosen["tool_hints"], chosen["reflector_daily_limit"]), (False, 6))
        self.assertEqual(cli.ignored_settings(cli.settings(self.home)), {"tool_hints": True, "reflector_daily_limit": 40})
        state = status.build(self.home, now=NOW, config=cli.effective_settings(self.home, {}))
        self.assertEqual(state["settings_ignored"], ["reflector_daily_limit", "tool_hints"])
        self.assertIn("o que ele faz sozinho", status.markdown(state, "pt"))
        form = {"plugins": {"entries": {PLUGIN: {"settings": {"tool_hints": True, "reflector_daily_limit": 20}}}}}
        chosen = options(cli.effective_settings(self.home, form), form, strict=False)
        self.assertEqual((chosen["tool_hints"], chosen["reflector_daily_limit"]), (True, 20))

    def test_settings_json_may_still_narrow_them(self):
        self.settings_json(tool_hints=False, reflector_daily_limit=2)
        chosen = options(cli.effective_settings(self.home, {}), {}, strict=False)
        self.assertEqual((chosen["tool_hints"], chosen["reflector_daily_limit"]), (False, 2))
        self.assertEqual(cli.ignored_settings(cli.settings(self.home)), {})
        self.settings_json(reflector_daily_limit=6)   # the default itself widens nothing
        self.assertEqual(cli.ignored_settings(cli.settings(self.home)), {})


class LessonTextTest(UiBase):
    """C2: a proposal keeps the words of the pass that wrote it; every place renders its lesson now, in the reader's
    language."""

    def test_the_terminal_and_the_panel_say_an_english_pass_lesson_in_portuguese(self):
        self.learned_in_english()
        with experience(self.home) as conn:
            stored = json.loads(conn.execute("SELECT evidence FROM proposal WHERE kind='lesson'").fetchone()[0])
        self.assertTrue(stored["lesson"].startswith("When calling"))   # what proposals.py keeps
        listed = self.run_cli(["proposals", "list"])
        self.assertIn("Ao chamar `files_search`", listed)
        self.assertNotIn("When calling", listed)
        shown = json.loads(self.run_cli(["proposals", "list", "--format", "json"]))
        self.assertTrue(shown[0]["evidence"]["lesson"].startswith("Ao chamar"))
        _out, asked, ended = self.typed(["proposals", "approve", "1"], "n\n")
        self.assertIn("Ao chamar `files_search`", asked)   # the confirmation shows what the agent would read
        self.assertIn("Cancelado", str(ended))
        api = load_api()
        self.addCleanup(lambda: api._module("operations").close_observers())
        snap = api.snapshot(self.home, now=NOW, runtime_config=runtime("pt"), can_decide=True)
        self.assertTrue(snap["proposals"][0]["evidence"]["lesson"].startswith("Ao chamar"))
        self.assertFalse(snap["proposals"][0]["explain"]["what"].startswith("When calling"))

    def test_rows_given_are_left_as_they_were(self):
        self.learned_in_english()
        with experience(self.home) as conn:
            rows = proposals.listing(conn, status="open")
            before = json.dumps(rows)
            cli.lesson_texts(conn, rows, "pt")
        self.assertEqual(json.dumps(rows), before)


class PanelPrivacyTest(UiBase):
    """C3: without a login the panel shows what an agent's shell shows: no text the owner, the reflector or the
    agent wrote, in any field."""

    def setUp(self):
        super().setUp()
        with experience(self.home) as conn:
            lessons.add_custom(conn, SECRET, tool="terminal", origin="operator", now=NOW)
            lessons.add_custom(conn, ACTIVE, tool="general", origin="operator", now=NOW, active=True)
            proposals.refresh(conn, now=NOW)
        self.api = load_api()
        self.addCleanup(lambda: self.api._module("operations").close_observers())

    def test_no_written_lesson_reaches_a_panel_without_a_login(self):
        snap = self.api.snapshot(self.home, now=time.time(), runtime_config=runtime("pt"), can_decide=False)
        whole = json.dumps(snap, ensure_ascii=False)
        for sentence in (SECRET, ACTIVE, "iconv", "smoke tests"):
            self.assertNotIn(sentence, whole)
        self.assertTrue(all(row["text_hidden"] and row["lesson"] is None for row in snap["lessons"]))
        self.assertTrue(all(row["text_hidden"] and "fingerprint" not in row for row in snap["pending"]))
        lesson_proposals = [row for row in snap["proposals"] if row["kind"] == "lesson"]
        self.assertTrue(lesson_proposals)
        self.assertTrue(all(row["evidence"]["lesson"] is None and not row["explain"]["what"] for row in lesson_proposals))
        self.assertIn("(texto oculto: leia no seu próprio terminal)", snap["report_md"])
        script = (ROOT / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")
        self.assertIn('textHidden: "(text hidden: read it in your terminal)"', script)
        self.assertIn('textHidden: "(texto oculto: leia no seu terminal)"', script)

    def test_the_owners_login_reads_them(self):
        snap = self.api.snapshot(self.home, now=time.time(), runtime_config=runtime("pt"), can_decide=True)
        whole = json.dumps(snap, ensure_ascii=False)
        self.assertIn(SECRET, whole)
        self.assertIn(ACTIVE, snap["report_md"])
        self.assertTrue(all(row.get("fingerprint") for row in snap["pending"]))


class RejectWordsTest(UiBase):
    """C4: rejecting a lesson's proposal decides the lesson; the message says what happened to it."""

    def test_a_lesson_proposal_is_turned_down_and_a_retire_proposal_keeps_the_lesson(self):
        self.failures()
        with patch("time.time", return_value=NOW):
            self.run_cli(["learn"])
        said = self.run_cli(["proposals", "reject", "1"])
        self.assertEqual(said, "Proposta #1 rejeitada: a lição dela foi recusada e não será mostrada ao agente.\n")
        with experience(self.home) as conn:
            kept = lessons.add_custom(conn, ACTIVE, tool="general", origin="operator", now=NOW, active=True)
            conn.execute("UPDATE lesson SET verdict='ineffective', rate_before=0.2, rate_after=0.3 WHERE id=?", (kept,))
            proposals.refresh(conn, now=NOW)
            number = conn.execute("SELECT id FROM proposal WHERE kind='lesson_retire' AND status='open'").fetchone()[0]
        said = self.run_cli(["proposals", "reject", str(number), "--lang", "en", "--yes"])   # --yes: still accepted
        self.assertEqual(said, f"Proposal #{number} rejected: its lesson stays active, and Auto mode will not retire it.\n")
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT status FROM lesson WHERE id=?", (kept,)).fetchone()[0], "active")

    def test_the_help_says_the_same(self):
        for language, words in (("pt", "recusada de vez"), ("en", "turned down for good")):
            reject = self.subparser(language, "proposals", "reject")
            shown = " ".join(self.subparser(language, "proposals").format_help().split())
            self.assertIn(words, shown)
            self.assertNotIn("--yes", reject.format_help())   # it confirmed nothing: no longer offered

    def subparser(self, language, *names):
        found = self.parser(language)
        for name in names:
            found = next(action for action in found._actions if hasattr(action, "choices")
                         and isinstance(action.choices, dict) and name in action.choices).choices[name]
        return found


class ArgumentsTest(UiBase):
    """C6, C17, D11 and the help in the --lang language."""

    def test_numbers_out_of_range_name_the_range_in_the_readers_language(self):
        said = self.refused(["decisions", "list", "--limit", "0"])
        self.assertIn("erro: argumento --limit: use um número inteiro de 1 a 500 (não 0)", said)
        self.assertNotIn("499", said)   # never argparse's list of every value
        self.assertTrue(said.startswith("uso: "))
        english = self.refused(["decisions", "list", "--limit", "0"], language="en")
        self.assertIn("error: argument --limit: use a whole number from 1 to 500 (not 0)", english)
        self.assertLess(len(english), 400)
        for argv, words in ((["report", "--weekday", "9"], "de 0 a 6"), (["report", "--hour", "24"], "de 0 a 23"),
                            (["report", "--days", "99"], "de 1 a 35"), (["usage", "--days", "x"], "de 1 a 35"),
                            (["skills", "--days", "0"], "de 1 a 35")):
            with self.subTest(argv=argv):
                self.assertIn(words, self.refused(argv))
        self.assertIn("opção inválida", self.refused(["status", "--format", "xml"]))
        self.assertIn("argumentos não reconhecidos", self.refused(["status", "--bogus"]))
        self.assertEqual(self.parser("pt").parse_args(["report", "--days", "35"]).days, 35)

    def test_every_option_and_argument_explains_itself(self):
        parser = self.parser("en")
        commands = next(action for action in parser._actions if hasattr(action, "choices")
                        and isinstance(action.choices, dict))
        lessons_commands = next(action for action in commands.choices["lessons"]._actions
                                if isinstance(getattr(action, "choices", None), dict))
        decisions_commands = next(action for action in commands.choices["decisions"]._actions
                                  if isinstance(getattr(action, "choices", None), dict))
        for shown, words in ((commands.choices["status"], "Output: md, readable"),
                             (commands.choices["report"], "Output: md, readable"),
                             (commands.choices["report"], "How many days to look at"),
                             (commands.choices["report"], "the day of the week, 0 (Sunday) to 6"),
                             (commands.choices["report"], "the hour of the day, 0 to 23"),
                             (commands.choices["usage"], "How many days to look at"),
                             (commands.choices["owner"], "Output: md, readable"),
                             (lessons_commands.choices["add"], "The tool the lesson is about"),
                             (lessons_commands.choices["add"], "one sentence that says what to do instead"),
                             (lessons_commands.choices["import"], "(`default` = the root profile)"),
                             (decisions_commands.choices["undo"], "The decision's number (#), from decisions list")):
            with self.subTest(words=words):
                self.assertIn(words, " ".join(shown.format_help().split()))
        portuguese = self.parser("pt")
        commands = next(action for action in portuguese._actions if isinstance(getattr(action, "choices", None), dict))
        imported = next(action for action in commands.choices["lessons"]._actions
                        if isinstance(getattr(action, "choices", None), dict)).choices["import"]
        self.assertIn("(`default` = o perfil raiz)", " ".join(imported.format_help().split()))
        self.assertIn("--from PERFIL", imported.format_help())

    def test_the_help_follows_lang_on_the_command_line(self):
        self.assertEqual(cli.argv_language(["status", "--lang", "pt"]), "pt")
        self.assertEqual(cli.argv_language(["--lang=en", "report"]), "en")
        self.assertIsNone(cli.argv_language(["--lang", "fr"]))
        self.assertIsNone(cli.argv_language([]))
        import argparse

        with patch.object(sys, "argv", ["hermes", "evolution", "--help", "--lang", "pt"]), \
                patch("hermes_atlas_evolution.i18n.resolve", return_value="en"):
            parser = argparse.ArgumentParser(prog="hermes evolution")
            cli.setup(parser)
        self.assertTrue(parser.format_help().startswith("uso: "))

    def test_owner_has_the_json_of_scripts(self):
        listed = json.loads(self.run_cli(["owner", "--format", "json"]))
        self.assertEqual(listed["owner_sender_ids"], [])
        self.assertEqual(listed["commands"]["add"], "hermes -p default evolution owner add <id>")
        added = json.loads(self.run_cli(["owner", "add", "555", "--format", "json"]))
        self.assertEqual((added["changed"], added["value"]), (True, ["555"]))
        self.assertTrue(added["run_this"].startswith("hermes -p default config set "))

    def test_an_empty_list_of_every_proposal_says_there_is_none_yet(self):
        self.assertIn("Nenhuma proposta ainda.", self.run_cli(["proposals", "list", "--all"]))
        self.assertIn("No proposal yet.", self.run_cli(["proposals", "list", "--all", "--lang", "en"]))
        self.assertIn("Nada aguardando você.", self.run_cli(["proposals", "list"]))


class MissingHomeTest(UiBase):
    """C7: a --home that does not exist is said in words, in the asked language."""

    def test_the_runner_names_the_folder_and_what_to_pass(self):
        missing = self.home / "nope"
        for language, words in (("pt", "Não há uma pasta de perfil do Hermes em"),
                                ("en", "There is no Hermes profile folder at")):
            with patch.object(sys, "argv", ["python -m hermes_atlas_evolution", "--home", str(missing), "--lang",
                                            language, "status"]), self.assertRaises(SystemExit) as ended:
                cli.main()
            self.assertIn(words, str(ended.exception.code))
            self.assertNotIn("Errno", str(ended.exception.code))
        self.assertFalse(missing.exists())   # nothing was created there


class EnvironmentModeTest(UiBase):
    """C5: `environment` shows the mode set in Hermes' configuration."""

    def test_the_mode_comes_from_config_yaml(self):
        form = {"plugins": {"entries": {PLUGIN: {"settings": {"mode": "manual"}}}}}
        with patch("hermes_atlas_evolution.status.environment_markdown", return_value="x\n") as shown:
            self.run_cli(["environment"], runtime_config=form)
        self.assertEqual(shown.call_args.kwargs["mode"], "manual")


class PanelErrorsTest(UiBase):
    """C8, C9: no dead refusal code, and an unexpected failure is a code and a sentence, never Python's words."""

    def test_managed_is_gone(self):
        for path in (ROOT / "dashboard" / "plugin_api.py", ROOT / "dashboard" / "dist" / "index.js"):
            text = path.read_text(encoding="utf-8").replace('"managed"', "managed:")
            self.assertIsNone(re.search(r"(?<![\w])managed:", text))   # fleet_managed is a setting, not the old code

    def test_an_unexpected_failure_of_a_decision_keeps_its_details_out(self):
        api = load_api()

        def decide(*_args, **_kwargs):
            raise RuntimeError("secret detail /root/.hermes/auth.json")
        with patch.object(api, "decide", decide), self.assertLogs("hermes_atlas_evolution.panel", "WARNING"):
            code, reply = api.decide_reply(self.home, runtime("pt"), {"kind": "undo", "id": 1}, now=time.time())
        self.assertEqual((code, reply["error"]), (500, "unexpected"))
        self.assertTrue(reply["message"].startswith("Algo inesperado"))
        self.assertNotIn("secret", json.dumps(reply))

    def test_an_unexpected_failure_of_the_snapshot_is_a_code_and_a_sentence(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI is installed with Hermes' dashboard only")
        api = load_api()
        self.addCleanup(lambda: api._module("operations").close_observers())
        app = FastAPI()
        app.include_router(api.router, prefix="/p")
        api._current = lambda target=None: (self.home, runtime("pt"))
        with patch.object(api, "snapshot", side_effect=RuntimeError("secret /root/x")), \
                self.assertLogs("hermes_atlas_evolution.panel", "WARNING"):
            reply = TestClient(app).get("/p/snapshot")
        self.assertEqual((reply.status_code, reply.json()["error"]), (500, "unexpected"))
        self.assertIn("hermes -p default evolution status", reply.json()["message"])
        self.assertNotIn("secret", reply.text)


class CommandShapeTest(UiBase):
    """C11: one shape for every command printed: `hermes -p <profile> …`, the one the menu also reads."""

    def setUp(self):
        super().setUp()
        self.work = self.home / "profiles" / "work"
        self.work.mkdir(parents=True)

    def test_status_digest_owner_and_notice_name_the_profile(self):
        state = status.build(self.work, now=NOW, config={})
        state["attention"] += ["lessons_waiting", "digest_unscheduled", "owner_unknown"]
        text = status.markdown(state, "en")
        self.assertIn("`hermes -p work evolution lessons review`", text)
        self.assertNotRegex(text, r"`hermes (evolution|config set)")
        built = report.build(self.work, now=NOW, days=7, lang="pt")
        built["proposals"] = [{"id": 2, "kind": "skill_lock", "risk": "low", "subject": "deploy", "evidence": {},
                               "command": "hermes curator lock deploy"}]
        digest = report.markdown(built, "pt")
        self.assertIn("`hermes -p work evolution proposals reject <id>`", digest)
        self.assertNotRegex(digest, r"`hermes evolution")
        self.assertTrue(cli.settings_command(self.work, "owner_sender_ids", ["1"]).startswith(
            "hermes -p work config set plugins.entries.hermes-atlas-evolution.settings.owner_sender_ids"))
        with experience(self.work) as conn:
            lessons.add_custom(conn, ACTIVE, tool="general", origin="operator", now=NOW)
        self.assertIn("`hermes -p work evolution status`", status.pending_notice(self.work, now=NOW)["message"])

    def test_the_rewrite_leaves_named_profiles_paths_and_ids_alone(self):
        for text in ("`hermes -p x evolution status`", "`hermes --profile x evolution status`", "hermes-atlas-evolution",
                     "/usr/bin/hermes evolution status", "plugins.entries.hermes-atlas-evolution.settings", None):
            self.assertEqual(cli.profile_commands(text, "work"), text)
        self.assertEqual(cli.profile_commands("run `hermes evolution learn`.", "work"), "run `hermes -p work evolution learn`.")
        self.assertEqual(cli.profile_commands("`hermes config set a b`", "work"), "`hermes -p work config set a b`")

    def test_the_menu_reads_every_shape(self):
        for shape in ("hermes evolution owner", "hermes -p work evolution owner", "hermes --profile work evolution owner"):
            with self.subTest(shape=shape):
                self.assertEqual(menu.in_menu(f"Se um deles é você, rode `{shape}` no seu próprio terminal:", "pt"),
                                 "Se um deles é você, rode a opção 7 deste menu:")
        self.assertEqual(menu.in_menu("Decide on each of these in your terminal with `hermes -p work evolution proposals "
                                      "approve <id>` or `hermes -p work evolution proposals reject <id>`, or in the menu "
                                      "(`hermes -p work evolution`).", "en"),
                         "Decide on each of these in option 2 of this menu.")

    def test_the_terminal_prints_stored_commands_with_the_profile(self):
        with experience(self.home) as conn:
            kept = lessons.add_custom(conn, ACTIVE, tool="general", origin="operator", now=NOW, active=True)
            conn.execute("UPDATE lesson SET verdict='ineffective', rate_before=0.2, rate_after=0.3 WHERE id=?", (kept,))
            proposals.refresh(conn, now=NOW)
        listed = self.run_cli(["proposals", "list"])
        self.assertIn(f"`hermes -p default evolution lessons retire {kept}`", listed)
        self.assertNotRegex(listed, r"`hermes evolution")
        api = load_api()
        self.addCleanup(lambda: api._module("operations").close_observers())
        snap = api.snapshot(self.work, now=time.time(), runtime_config=runtime("en"), can_decide=True)
        self.assertTrue(all(command.startswith("hermes -p work evolution") for command in snap["commands"].values()))
        self.assertNotRegex(json.dumps(snap), r"hermes evolution")


class WordingTest(UiBase):
    """C12, C13, C14: one word for each thing, the time a pass may take, and no colour where it cannot show."""

    def test_portuguese_says_rodada_licoes_and_pede_atencao(self):
        from hermes_atlas_evolution.catalog import PAIRS

        for _english, portuguese in PAIRS:
            self.assertNotIn("passada", portuguese)
            self.assertNotIn("playbook", portuguese.lower())
        self.assertEqual(status._TEXT["pt"]["attention"], "Pede atenção")
        self.assertEqual(cli._H["pt"]["attention"], "Pede atenção")
        self.assertEqual(menu.TEXT["pt"]["attention_title"], "Pede atenção:")
        self.assertEqual(status._TEXT["pt"]["none"], "Nada pede atenção.")

    def test_the_learning_pass_says_how_long_it_may_take(self):
        from hermes_atlas_evolution.autopilot import LEASE_SECONDS

        said = []
        console = menu.Console(read=lambda _prompt: "", write=said.append, width=100, color=False)
        with patch("hermes_atlas_evolution.autopilot.run_exclusive", return_value={}):
            menu.Menu(self.home, {}, lang="pt", console=console).learn()
        self.assertIn(f"até {(LEASE_SECONDS - 180) // 60} minutos", "".join(said))
        self.assertNotIn("dois minutos", json.dumps(menu.TEXT, ensure_ascii=False))
        self.assertNotIn("two minutes", json.dumps(menu.TEXT))

    def test_no_colour_on_a_dumb_terminal_or_with_no_color(self):
        with patch("sys.stdout.isatty", return_value=True):
            with patch.dict(os.environ, {"TERM": "dumb", "NO_COLOR": ""}):
                self.assertFalse(menu.Console(width=80).color)
            with patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": "1"}):
                self.assertFalse(menu.Console(width=80).color)
            with patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": ""}):
                self.assertEqual(menu.Console(width=80).color, os.name != "nt")


class ReviewOriginTest(UiBase):
    """C15: `lessons review` names a lesson someone wrote by who wrote it, never as "another error"."""

    def test_written_lessons_are_named_by_their_origin(self):
        with experience(self.home) as conn:
            mine = lessons.add_custom(conn, SECRET, tool="terminal", origin="operator", now=NOW)
            agents = lessons.add_custom(conn, ACTIVE, tool="general", origin="agent", now=NOW)
        _out, asked, _ended = self.typed(["lessons", "review"], "\n\n")
        self.assertIn(f"#{mine} · terminal · lição sua", asked)
        self.assertIn(f"#{agents} · general · lição escrita pelo agente", asked)
        self.assertNotIn("outro erro", asked)
        _out, asked, _ended = self.typed(["lessons", "review", "--lang", "en"], "\n\n", language="en")
        self.assertIn(f"#{mine} · terminal · your lesson", asked)
        self.assertNotIn("another error", asked)


class StatusAlwaysAnswersTest(UiBase):
    """status renders whatever the settings hold, and lists what it could not use under its attention heading."""

    def test_a_broken_settings_file_or_value_is_listed_not_fatal(self):
        (self.home / "evolution").mkdir(exist_ok=True)
        (self.home / "evolution" / "settings.json").write_text("{broken", encoding="utf-8")
        shown = self.run_cli(["status", "--no-learn"])
        self.assertIn("Estas configurações não puderam ser usadas", shown)
        self.assertIn("`settings.json`", shown)
        self.settings_json(slow_turn_seconds=1)   # Hermes' configuration (FORM) sets lessons: this one is settings.json's
        shown = self.run_cli(["status", "--no-learn", "--lang", "en"])
        self.assertIn("These settings could not be used, so a safe value applies until they are fixed: "
                      "`slow_turn_seconds`", shown)
        with self.assertRaises(SystemExit):   # the commands that act stay strict
            self.run_cli(["lessons", "list"])

    def test_an_unread_configuration_is_said_first(self):
        shown = self.run_cli(["status", "--no-learn", "--lang", "pt"], runtime_config={CONFIG_UNREAD: True})
        attention = shown.split("## Pede atenção")[1]
        self.assertTrue(attention.strip().startswith("- O config.yaml não foi lido: instale o PyYAML"), attention)
        state = status.build(self.home, now=NOW, config={}, runtime_config={})
        self.assertFalse(state["config_unread"])

    def test_the_first_pass_is_announced_once(self):
        shown = self.run_cli(["status", "--no-learn"])
        self.assertIn("nenhuma ainda neste perfil", shown)
        self.assertEqual(shown.count("evolution learn`"), 1, shown)


class MenuLettersTest(UiBase):
    """The menu's attention items name the letter of the suggestion that does them."""

    def test_an_item_a_suggestion_does_says_its_letter(self):
        with experience(self.home) as conn:
            lessons.add_custom(conn, SECRET, tool="terminal", origin="operator", now=NOW)
        console = menu.Console(read=lambda _prompt: "", write=lambda _text: None, width=100, color=False)
        shown = menu.Menu(self.home, FORM, lang="pt", console=console)
        state = shown.state()
        ideas = shown.suggestions(state, shown.open_proposals())
        letters = shown.letters(ideas)
        self.assertEqual(letters.get("lessons_waiting"), "a")
        lines = shown.attention_lines(state, letters)
        self.assertIn("Há lições esperando a sua aprovação: opção 1 deste menu (letra a abaixo).", lines)
        if "digest_unscheduled" in letters:
            self.assertTrue(any(re.search(rf"\(letra {letters['digest_unscheduled']} abaixo\)", line) for line in lines))
        self.assertFalse(any("letra" in line for line in shown.attention_lines(state)))   # no letters given: none said

DELETED_UNIT = {"unit": "hermes-gateway.service", "scope": "user", "exec_missing": True, "active_state": "active",
                "sub_state": "running", "exec_path": "/root/.hermes/tools/python-3.14.9/bin/python3", "enabled": True}


class HealthWordsTest(UiBase):
    """The health agent's findings and gateway states, said in both languages."""

    def stored(self, home, care, system=None):
        with experience(home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)", (json.dumps(care),))
            if system is not None:
                conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('system_check', ?)", (json.dumps(system),))

    def test_a_gateway_running_a_deleted_program_is_said_with_its_fix(self):
        work = self.home / "profiles" / "work"
        work.mkdir(parents=True)
        care = {"at": NOW, "gateway": {"running": True, "state": "running"}, "gateway_unit": DELETED_UNIT,
                "findings": ["gateway_program_deleted"]}
        system = {"at": NOW, "supported": True, "units": [DELETED_UNIT], "findings": ["unit_exec_missing"]}
        self.stored(work, care, system)
        state = status.build(work, now=NOW, config={}, live=False)
        self.assertIn("hermes_gateway_program_deleted", state["attention"])
        self.assertNotIn("os_unit_exec_missing", state["attention"])   # the gateway's own line says it, with its fix
        english, portuguese = status.markdown(state, "en"), status.markdown(state, "pt")
        self.assertIn("The gateway runs, but the program its service `hermes-gateway.service` starts "
                      "(`/root/.hermes/tools/python-3.14.9/bin/python3`) was deleted under it", english)
        self.assertIn("Fix the service now: `hermes -p work evolution care fix-gateway`.", english)
        self.assertIn("foi apagado por baixo dele (o Hermes trocou o Python): o próximo reinício falha. Corrija o "
                      "serviço agora: `hermes -p work evolution care fix-gateway`.", portuguese)
        # a look now decides: still running a deleted program, or repaired
        self.assertIn("hermes_gateway_program_deleted",
                      status._apply_live([], {"units": [DELETED_UNIT]}, care, system, work))
        fixed = {**DELETED_UNIT, "exec_missing": False}
        self.assertNotIn("hermes_gateway_program_deleted",
                         status._apply_live(["hermes_gateway_program_deleted"], {"units": [fixed]}, care, system, work))
        from hermes_atlas_evolution.evolution_index import _HEALTH_HIGH

        self.assertIn("gateway_program_deleted", _HEALTH_HIGH)

    def test_a_degraded_or_starting_gateway_runs_and_says_so(self):
        for lang, state, words in (("en", "degraded", "running (degraded: no chat platform connected yet; scheduled "
                                                    "tasks still run)"),
                                   ("pt", "degraded", "rodando (degradado: nenhuma plataforma de chat conectada ainda; "
                                                    "as tarefas agendadas seguem rodando)"),
                                   ("en", "starting", "starting"), ("pt", "starting", "subindo")):
            with self.subTest(lang=lang, state=state):
                gate = {"running": True, "state": state, "version": "0.23.1"}
                self.assertTrue(status.gateway_words(gate, lang).startswith(words + " · "))
                screen = menu.Menu(self.home, {}, lang=lang, console=menu.Console(read=lambda _p: "",
                                                                                   write=lambda _t: None, color=False))
                self.assertTrue(screen.gateway_state(gate).startswith(words + " · "))
        self.assertTrue(status.gateway_words({"running": True, "state": "running"}, "pt").startswith("rodando"))
        self.assertEqual(status.gateway_words({"running": False, "state": "degraded"}, "en"), "not running")


class GcUnsafeMenuTest(MenuBase):
    def test_a_store_without_facts_json_is_said_in_the_menu(self):
        from hermes_atlas_evolution import hermes_care

        care = {"gateway": {"running": True}, "database": {}, "memory": {}, "load": {}, "sizes": {}, "unused_tools": [],
                "reclaimable": 0, "findings": [], "in_use_unreferenced": [], "gc_model": hermes_care.GC_MODEL,
                "gc_unsafe": ["no_facts:/root/.hermes/tools"]}
        for lang, words in (("pt", "Agora não: o depósito /root/.hermes/tools não tem facts.json: o `hermes pm gc` "
                                   "apagaria todas as ferramentas dele"),
                            ("en", "Not now: the store /root/.hermes/tools has no facts.json: `hermes pm gc` would "
                                   "delete every tool in it")):
            with patch.object(hermes_care, "cached", return_value=care), \
                    patch.object(hermes_care, "run_action") as ran:
                shown = " ".join(self.menu("12", "5", "", "", lang=lang).split())
            ran.assert_not_called()
            self.assertIn(words, shown)
            self.assertIn("hermes pm doctor.", shown)


class LaterStatesTest(UiBase):
    """A reflector switched on while running, and Hermes' accounting read in part."""

    def test_the_reflector_waits_for_a_restart(self):
        for lang, words in (("pt", "**Refletor:** liga depois de `hermes gateway restart`"),
                            ("en", "**Reflector:** on after `hermes gateway restart`")):
            shown = cli._learn_md({"reflector": {"status": "needs_restart"}}, self.home, lang, quiet=False)
            self.assertIn(words, shown)

    def test_partial_accounting_is_a_floor(self):
        native = {"status": "partial", "totals": {"estimated_cost_usd": 12.5, "api_call_count": 20_000},
                  "coverage": {"partial": True, "row_limit": 20_000, "covered_from_epoch": NOW - 3600}}
        for lang, words in (("pt", "pelo menos US$ 12,50 em 20.000 chamadas (os 20.000 registros mais recentes)"),
                            ("en", "at least US$ 12.50 in 20,000 calls (the newest 20,000 records)")):
            self.assertIn(words, cli._usage_md({"observer_usage": {}, "hermes_native_billing": native}, lang, days=7))
        with patch("hermes_atlas_evolution.billing.native_billing", return_value=native):
            built = report.build(self.home, now=NOW, days=7, lang="pt")
        self.assertEqual((built["native_cost"]["partial"], built["native_cost"]["row_limit"]), (True, 20_000))
        self.assertIn("pelo menos US$ 12,50 (os 20.000 registros mais recentes)", report.markdown(built, "pt"))
        self.assertIn("at least US$ 12.50 (the newest 20,000 records)", report.markdown(built, "en"))


class ProfileScopedCommandsTest(UiBase):
    """F9: Hermes' own commands for one profile name it too; the gateway's name the profile whose gateway serves it;
    what the lead profile wrote keeps the lead's `-p`."""

    def setUp(self):
        super().setUp()
        self.work = self.home / "profiles" / "work"
        self.work.mkdir(parents=True)
        (self.work / "config.yaml").touch()

    def test_the_rewrite_knows_which_profile_each_command_acts_on(self):
        text = "Run `hermes gateway restart`; `hermes plugins list`; `hermes cron list`; `hermes pm gc`; `hermes evolution learn`"
        self.assertEqual(cli.profile_commands(text, "work", gateway="default"),
                         "Run `hermes -p default gateway restart`; `hermes -p work plugins list`; `hermes -p work cron "
                         "list`; `hermes pm gc`; `hermes -p work evolution learn`")   # pm: one tool store per install
        self.assertEqual(cli.profile_commands(text, "work"),   # a text about other profiles too: only its own commands
                         text.replace("`hermes evolution learn`", "`hermes -p work evolution learn`"))
        self.assertEqual(cli.gateway_profile(self.work), "work")
        with patch("hermes_atlas_evolution.hermes_care._served_by_root", return_value=self.home):
            self.assertEqual(cli.gateway_profile(self.work), "default")

    def test_status_and_the_menu_name_this_profile_and_the_gateway_that_serves_it(self):
        state = status.build(self.work, now=NOW, config={}, live=False)
        state["care"] = {"gateway": {"served_by_root": True}}
        state["attention"] = ["stale_runtime", "no_events_today"]
        text = status.markdown(state, "en")
        self.assertIn("(`hermes -p work plugins update` does not reload a running gateway). Run "
                      "`hermes -p default gateway restart`", text)
        self.assertIn("(`hermes -p work plugins list`)", text)
        screen = menu.Menu(self.work, {}, lang="pt", console=menu.Console(read=lambda _p: "", write=lambda _t: None,
                                                                          color=False))
        self.assertIn("hermes -p work gateway restart", screen.t["reload"][0])

    def test_the_leads_commands_keep_the_leads_profile(self):
        with experience(self.home) as conn:   # the lead (default) wrote it for itself
            conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
                VALUES ('env:x', 'plugin_update', 'low', 'x', '{}', 'hermes evolution update --profiles x', 'open', ?, ?)""",
                         (NOW, NOW))
        said = []
        screen = menu.Menu(self.work, {}, lang="en", console=menu.Console(read=lambda _p: "", write=said.append,
                                                                          color=False))
        item = screen.open_proposals(self.home)[0]
        self.assertEqual(item["command"], "hermes -p default evolution update --profiles x")
        screen.io.say(item["command"])   # printed in profile work, it stays the lead's
        self.assertEqual(said[-1], "hermes -p default evolution update --profiles x\n")
        found = {"at": NOW, "profiles": [], "findings": [{"kind": "plugin_update", "subject": "x", "risk": "low",
                                                          "evidence": {}, "command": "hermes evolution update --profiles x"}]}
        with patch("hermes_atlas_evolution.environment.lead_home", return_value=self.home), \
                patch.object(status, "_environment", return_value=found):
            shown = status.environment_markdown(self.work, "en")
        self.assertIn("`hermes -p default evolution update --profiles x`", shown)
        self.assertNotIn("-p work evolution update", shown)


class BrokenSettingsTest(UiBase):
    """F10: a settings.json that is not JSON is named, with what to do, in the reader's language."""

    def test_every_command_says_which_file_and_what_to_do(self):
        (self.home / "evolution").mkdir(exist_ok=True)
        (self.home / "evolution" / "settings.json").write_text('{"evolution": {lessons: 1}}', encoding="utf-8")
        for argv, words in ((["report"], "Evolution: O evolution/settings.json não é um JSON válido: corrija-o, ou "
                                          "tire-o do lugar"),
                            (["lessons", "list", "--lang", "en"], "Evolution: evolution/settings.json is not valid "
                                                                  "JSON: correct it, or move it away")):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as ended:
                self.run_cli(argv)
            self.assertIn(words, str(ended.exception))
            self.assertNotIn("Expecting", str(ended.exception))
        (self.home / "evolution" / "settings.json").write_bytes(b'{"evolution": "\xff"}')   # not UTF-8 either
        with self.assertRaises(SystemExit) as ended:
            self.run_cli(["report"])
        self.assertIn("não é um JSON válido", str(ended.exception))


class LabFindingsTest(UiBase):
    """What a real Hermes 0.23.2 showed: no fixed Hermes version in the texts, the failing lines of a command first,
    commands in backticks and lower case after a colon."""

    def test_no_text_names_a_fixed_hermes_version(self):
        from hermes_atlas_evolution import explain

        for name, texts in (("status", status._TEXT), ("cli", cli._H), ("menu", menu.TEXT)):
            with self.subTest(texts=name):
                self.assertNotRegex(json.dumps(texts, ensure_ascii=False), r"Hermes 0\.\d+\.\d+")
        for lang in ("en", "pt"):
            told = explain.describe({"kind": "skill_archive", "subject": "x", "evidence": {}}, lang)
            self.assertNotRegex(json.dumps(told, ensure_ascii=False), r"0\.23\.\d")

    def test_a_failed_command_shows_what_did_not_pass_first(self):
        step = {"command": "hermes pm doctor", "exit": 1, "tail": ["✓ node", "✗ uv: missing", "✓ python", "✓ git"]}
        self.assertEqual(cli.step_lines(step)[0], "✗ uv: missing")
        self.assertEqual(cli.step_lines({"problems": ["✗ facts.json unreadable"], "tail": ["✓ a", "✓ b"]}),
                         ["✗ facts.json unreadable", "✓ a", "✓ b"])
        self.assertEqual(len(cli.step_lines({"tail": [f"✓ {n}" for n in range(30)]})), 12)
        shown = cli._care_action_md({"ok": False, "steps": [step]}, "pm-doctor", "pt")
        self.assertIn("`hermes pm doctor` terminou com o código 1.", shown)
        body = shown.split("primeiro o que não passou")[1]
        self.assertLess(body.index("✗ uv: missing"), body.index("✓ node"))

    def test_commands_in_backticks_and_lower_case_after_a_colon(self):
        from hermes_atlas_evolution import explain

        playbook = (ROOT / "hermes_atlas_evolution" / "knowledge" / "playbook.json").read_text(encoding="utf-8")
        self.assertIn("(`hermes profile delete <nome>`)", playbook)
        self.assertIn("(`hermes profile delete <name>`)", playbook)
        self.assertNotRegex(playbook, r"[^`]hermes profile delete <")
        item = {"kind": "doctor_security", "subject": "default/security.x",
                "evidence": {"profile": "default", "hermes": "Something Hermes says"}}
        self.assertIn("No perfil `default`: o `hermes doctor` diz, em inglês", explain.describe(item, "pt")["what"])
        self.assertIn("In profile `default`: `hermes doctor` says", explain.describe(item, "en")["what"])


if __name__ == "__main__":
    unittest.main()


class Launcher0232Test(unittest.TestCase):
    """Hermes 0.23.2's launcher runs `python3 -I -c '<launcher script>' "$@"`: the owner's own `hermes evolution ...`
    in their terminal is this command's launcher, and the same command from an agent's shell under a gateway is not."""

    SCRIPT = ("import os, re, sys\nsys.path.insert(0, '/usr/local/lib/hermes-agent')\nfrom hermes_cli.main import main\n"
              "sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\nsys.exit(main())\n")
    PY = "/root/.hermes/tools/python-3.14.9+20261021-linux-x64/bin/python3"

    def launcher(self, *words):
        return [self.PY, "-I", "-c", self.SCRIPT, *words]

    def test_the_owners_own_launcher_is_recognised(self):
        self.assertTrue(cli._own_launcher(self.launcher("evolution", "owner", "list")))
        self.assertTrue(cli._own_launcher(self.launcher("-p", "work", "evolution", "lessons", "approve", "3")))
        self.assertFalse(cli._own_launcher(self.launcher("gateway", "run")))
        self.assertFalse(cli._own_launcher(self.launcher("-z", "run hermes evolution for me")))

    def test_a_terminal_is_the_owner_and_an_agent_shell_under_a_gateway_is_not(self):
        terminal = {100: (90, self.launcher("evolution", "owner", "list")), 90: (1, ["-bash"])}
        self.assertFalse(cli._under_hermes(start=100, read=terminal.get))
        agent = {100: (90, self.launcher("evolution", "lessons", "approve", "3")), 90: (80, ["/bin/bash", "-c", "x"]),
                 80: (1, self.launcher("gateway", "run"))}
        self.assertTrue(cli._under_hermes(start=100, read=agent.get))
