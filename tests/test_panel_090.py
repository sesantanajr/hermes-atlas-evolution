"""0.9.0: the panel speaks the profile's language and words every refusal in it."""

import importlib.util
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution.operations import close_observers

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "hermes-atlas-evolution"
STALE = "Not so any more: checked again on disk, the situation changed or is gone; nothing was run"


def load_api():
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_090", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime(language=None):
    """Hermes' config.yaml with the plugin's settings form, as the dashboard reads it."""
    settings = {} if language is None else {"language": language}
    return {"plugins": {"entries": {PLUGIN: {"settings": settings}}}}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "profiles" / "work"
        self.home.mkdir(parents=True)
        # no language from the machine: only the setting and the dashboard decide
        environment = patch.dict(os.environ, {"HERMES_LANGUAGE": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.api = load_api()
        self.addCleanup(close_observers)
        self.addCleanup(lambda: self.api._module("operations").close_observers())


class LanguageTest(Base):
    def test_the_profile_setting_wins_and_auto_follows_the_dashboard(self):
        choose = self.api.panel_language
        self.assertEqual(choose("en", "pt", "pt"), "en")
        self.assertEqual(choose("pt", "en", "en"), "pt")
        self.assertEqual(choose("auto", "pt-BR", "en"), "pt")
        self.assertEqual(choose("auto", "en-US", "pt"), "en")
        self.assertEqual(choose("auto", None, "pt"), "pt")   # no dashboard language: the profile's, as Hermes resolves it
        self.assertEqual(choose(None, "fr", None), "en")

    def test_the_snapshot_uses_the_language_setting_not_the_dashboards(self):
        snap = self.api.snapshot(self.home, now=time.time(), runtime_config=runtime("pt"), ui_lang="en")
        self.assertEqual(snap["lang"], "pt")
        snap = self.api.snapshot(self.home, now=time.time(), runtime_config=runtime("en"), ui_lang="pt")
        self.assertEqual(snap["lang"], "en")
        self.assertEqual(snap["settings"]["values"]["language"], "en")

    def test_auto_follows_the_dashboard_then_the_profile(self):
        snap = self.api.snapshot(self.home, now=time.time(), runtime_config=runtime("auto"), ui_lang="pt")
        self.assertEqual(snap["lang"], "pt")
        snap = self.api.snapshot(self.home, now=time.time(), runtime_config=runtime(), ui_lang="en")
        self.assertEqual(snap["lang"], "en")
        with patch.dict(os.environ, {"HERMES_LANGUAGE": "pt_BR"}):
            snap = self.api.snapshot(self.home, now=time.time(), runtime_config=runtime("auto"))
        self.assertEqual(snap["lang"], "pt")

    def test_an_explicit_lang_still_forces_the_snapshot(self):
        snap = self.api.snapshot(self.home, now=time.time(), runtime_config=runtime("en"), lang="pt")
        self.assertEqual(snap["lang"], "pt")

    def test_a_decision_without_lang_speaks_the_profiles_language(self):
        seen = []

        def decide(home, config, body, *, now, lang="en", writer=None):
            seen.append(lang)
            return {"ok": True}
        with patch.object(self.api, "decide", decide):
            self.api.decide_reply(self.home, runtime("pt"), {"kind": "x"}, now=time.time())
            self.api.decide_reply(self.home, runtime("pt"), {"kind": "x"}, now=time.time(), hint="en")
            self.api.decide_reply(self.home, runtime("auto"), {"kind": "x"}, now=time.time(), hint="pt")
            self.api.decide_reply(self.home, runtime("auto"), {"kind": "x"}, now=time.time())
        self.assertEqual(seen, ["pt", "pt", "pt", "en"])


class DecideReplyTest(Base):
    def test_refusals_of_the_plugin_come_in_the_profiles_language(self):
        code, reply = self.api.decide_reply(self.home, runtime("pt"), {"kind": "undo", "id": 999}, now=time.time())
        self.assertEqual((code, reply["error"]), (400, "invalid"))
        self.assertEqual(reply["message"], "Id de decisão desconhecido: 999")
        code, reply = self.api.decide_reply(self.home, runtime("pt"), {"kind": "nothing"}, now=time.time())
        self.assertEqual((code, reply["message"]), (400, "Decisão inválida"))
        code, reply = self.api.decide_reply(self.home, runtime("pt"), {"kind": "lesson"}, now=time.time())   # no id
        self.assertEqual((code, reply["message"]), (400, "Decisão inválida"))
        code, reply = self.api.decide_reply(self.home, runtime("en"), {"kind": "nothing"}, now=time.time(), hint="pt")
        self.assertEqual(reply["message"], "Invalid decision")

    def test_a_sentence_from_hermes_is_quoted_in_a_portuguese_frame(self):
        def decide(*_args, **_kwargs):
            raise ValueError("error: something Hermes printed")
        with patch.object(self.api, "decide", decide):
            code, reply = self.api.decide_reply(self.home, runtime("pt"), {"kind": "x"}, now=time.time())
        self.assertEqual(code, 400)
        self.assertEqual(reply["message"], "Não foi feito: error: something Hermes printed")

    def test_a_managed_setting_is_said_in_the_profiles_language(self):
        def decide(*_args, **_kwargs):
            raise PermissionError("Unknown setting")
        with patch.object(self.api, "decide", decide):
            code, reply = self.api.decide_reply(self.home, runtime("pt"), {"kind": "settings"}, now=time.time())
        self.assertEqual((code, reply["error"]), (403, "managed"))
        self.assertTrue(reply["message"].startswith("O seu administrador gerencia isto"))
        self.assertIn("Configuração desconhecida", reply["message"])

    def test_a_busy_store_is_said_in_the_profiles_language(self):
        def decide(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")
        with patch.object(self.api, "decide", decide):
            code, reply = self.api.decide_reply(self.home, runtime("pt"), {"kind": "x"}, now=time.time())
        self.assertEqual((code, reply["error"]), (503, "busy"))
        self.assertIn("ocupado", reply["message"])

    def test_reasons_that_are_sentences_are_translated_and_codes_stay_codes(self):
        result = {"applied": [3], "reason": "changed_since",
                  "skipped": [{"id": 1, "reason": STALE}, {"id": 2, "reason": "one_by_one"}, "odd"]}

        def decide(*_args, **_kwargs):
            return result
        with patch.object(self.api, "decide", decide):
            code, reply = self.api.decide_reply(self.home, runtime("pt"), {"kind": "apply_all", "ids": [1, 2, 3]},
                                                now=time.time())
        self.assertEqual(code, 200)
        self.assertEqual(reply["reason"], "changed_since")
        self.assertNotEqual(reply["skipped"][0]["reason"], STALE)
        self.assertEqual(reply["skipped"][0]["reason"], self.api._module("i18n").text(STALE, "pt"))
        self.assertEqual(reply["skipped"][1:], [{"id": 2, "reason": "one_by_one"}, "odd"])
        self.assertEqual(result["skipped"][0]["reason"], STALE)   # the plugin's own result is left as it was

    def test_refusal_codes_have_both_languages(self):
        for code in ("busy", "open_dashboard", "shared_token", "other_profile", "missing_header", "managed", "refused"):
            self.assertNotEqual(self.api.message(code, "en"), self.api.message(code, "pt"))
        self.assertEqual(self.api.message("busy", "fr"), self.api.message("busy", "en"))
        self.assertEqual(self.api.message("whatever", "pt"), "whatever")


class EnvironmentDoctorTest(Base):
    def test_the_snapshot_carries_the_doctor_only_when_there_is_one(self):
        snap = self.api.snapshot(self.home, now=time.time(), runtime_config=runtime("en"))
        self.assertEqual(snap["environment"], {})
        status = self.api._module("status")
        found = {"findings": [], "doctor": {"work": {"ok": True, "fail": 1, "warn": 2, "at": 1.0}, "broken": "x"}}
        with patch.object(status, "_environment", lambda home: found):
            self.assertEqual(self.api.environment_extra(self.home),
                             {"doctor": {"work": {"ok": True, "fail": 1, "warn": 2, "at": 1.0}}})
        with patch.object(status, "_environment", lambda home: {"findings": []}):
            self.assertEqual(self.api.environment_extra(self.home), {})

        def broken(home):
            raise RuntimeError("damaged")
        with patch.object(status, "_environment", broken):
            self.assertEqual(self.api.environment_extra(self.home), {})


class BundleTest(unittest.TestCase):
    def test_the_panel_sends_the_profiles_language_and_words_its_codes(self):
        script = (ROOT / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")
        self.assertIn("data && TEXT[data.lang] ? data.lang : hint", script)   # the snapshot's language wins
        self.assertNotIn("toFixed(digits) : value.toLocaleString()", script)   # never the browser's locale
        english, portuguese = script.split("\n    pt: {", 1)
        for code in ("busy", "open_dashboard", "missing_header", "other_profile", "changed_since", "already_undone",
                     "nothing_to_undo", "no_room", "one_by_one"):
            for half in (english, portuguese):   # in English and in Portuguese
                self.assertIn(code + ": ", half.split("reasons: {", 1)[1].split("}", 1)[0], code)
        self.assertIn('"aria-labelledby": "hae-confirm-title"', script)
        self.assertIn('role: "tabpanel"', script)
        self.assertIn("environment || {}).doctor", script)


if __name__ == "__main__":
    unittest.main()
