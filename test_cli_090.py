"""0.9.0: every command a person runs reads as text in their language, and the messages say what is true."""

import argparse
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import cli, status
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.operations import close_observers
from hermes_atlas_evolution.signals import record_tool

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine

NOW = 1_794_800_000.0
DAY = 86_400
FORM = {"display": {"language": "pt"},   # the profile speaks Portuguese; --lang en overrides
        "plugins": {"entries": {"hermes-atlas-evolution": {"settings": {"lessons": "suggest"}}}}}
CARE = {"at": NOW, "gateway": {"running": True, "version": "0.23.1", "rss": 640 * 2**20, "heartbeat_age": 12,
                               "stale": False},
        "database": {"present": True, "journal": "wal", "wal": 900_000, "quick_check": "ok"},
        "free": 67 * 2**30, "total": 193 * 2**30,
        "memory": {"total": 12 * 2**30, "available": 7 * 2**30, "swap_total": 12 * 2**30, "swap_used": 11 * 2**30},
        "load": {"1m": 1.0, "15m": 1.1, "cpus": 6}, "sizes": {"tools": 2 * 2**30, "evolution": 241_664}, "growth": [],
        "unused_tools": [{"folder": "node-22.23.4", "package": "node", "bytes": 200 * 2**20}],
        "reclaimable": 700 * 2**20, "findings": ["swap_full", "unused_tools"]}
SYSTEM = {"at": NOW, "supported": True, "os": {"name": "Ubuntu 26.04.1 LTS"}, "kernel": "7.0.0", "security": 2,
          "upgradable": [{"name": "libc6", "from": "1", "to": "2", "security": True}], "reboot_required": True,
          "reboot_packages": ["libc6"], "dpkg_ok": True, "apt_ok": True, "busy": [], "held": [], "plugin_holds": [],
          "unattended": {"enabled": True}, "units": [], "linger": True, "last_refresh": NOW - 3600,
          "findings": ["reboot_required", "security_updates"], "ready": True}
# Raw codes and English state words that must never reach a Portuguese reader.
LEAKS = re.compile(r'^\s*[\[{]|"basis"|hermes_skill_usage_sidecar|no profile has Atlas Evolution on|"mode": "full"|'
                   r"\b(applied|rolled_back|candidate|quarantined|FileNotFoundError|PermissionError)\b")


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        self.home = Path(folder.name)
        (self.home / "skills").mkdir()
        (self.home / "logs").mkdir()
        tree = patch.multiple("hermes_atlas_evolution.cli", _under_hermes=lambda **_: False,
                              _in_hermes_cgroup=lambda **_: False)   # not the host's process tree
        tree.start()
        self.addCleanup(tree.stop)
        clean = patch.dict(os.environ, {name: "" for name in cli.AGENT_MARKERS})
        clean.start()
        self.addCleanup(clean.stop)

    def parser(self, language="pt"):
        parser = argparse.ArgumentParser(prog="hermes evolution")
        with patch("hermes_atlas_evolution.i18n.resolve", return_value=language):
            cli.setup(parser)
        return parser

    def run_cli(self, argv, *, language="pt", runtime_config=FORM, stdin_tty=False):
        """`hermes evolution <argv>` as Hermes runs it: the parser built by setup, then execute."""
        args = self.parser(language).parse_args(argv)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), patch("sys.stdin.isatty", return_value=stdin_tty):
            cli.execute(args, self.home, runtime_config=runtime_config)
        return stream.getvalue()

    def failures(self):
        body = json.dumps({"error": "tool_call to 'files_search' is missing required argument(s): servico."})
        for offset in range(2):
            for _ in range(2):
                record_tool(self.home, {"tool_name": "tool_call", "status": "error", "result": body,
                                        "session_id": f"s{offset}", "args": {"calls": [{"name": "files_search"}]}},
                            now=NOW - offset * DAY)


class ReadableByDefaultTest(Base):
    def test_every_documented_command_prints_text_in_portuguese_and_json_on_request(self):
        self.failures()
        with patch("time.time", return_value=NOW):
            learned = self.run_cli(["learn", "--lang", "pt"])
        self.assertTrue(learned.startswith("# Rodada de aprendizado"), learned)
        self.assertIn("Lições novas", learned)
        self.assertIn("Modo Auto", learned)
        with experience(self.home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('care_check', ?)", (json.dumps(CARE),))
        with patch("hermes_atlas_evolution.hermes_care.cached", return_value=CARE), \
                patch("hermes_atlas_evolution.system_update.check", return_value=SYSTEM):
            screens = {argv[0] + (argv[1] if len(argv) > 1 and not argv[1].startswith("-") else ""): self.run_cli(argv)
                       for argv in (["care"], ["environment"], ["decisions"], ["lessons", "list"],
                                    ["proposals", "list"], ["skills"], ["experiments"], ["system"])}
        for name, text in screens.items():
            with self.subTest(command=name):
                self.assertFalse(LEAKS.search(text), f"{name}: {text}")
                self.assertNotIn("1794", text)   # no epoch time
        self.assertIn("Nenhum perfil tem o Atlas Evolution ligado", screens["environment"])
        self.assertIn("## Aguardando aprovação · 1", screens["lessonslist"])
        self.assertIn("**Lições:** com aprovação (toda lição espera você)", screens["lessonslist"])
        self.assertIn("padrão fixo", screens["lessonslist"])
        self.assertIn("Ao chamar `files_search`", screens["lessonslist"])
        self.assertIn("**Proposta #1** [risco: baixo] Ativar lição #1 · `files_search`", screens["proposalslist"])
        self.assertIn("Se aprovar: a lição fica ativa e o agente passa a lê-la no começo de cada sessão.",
                      screens["proposalslist"])
        self.assertNotIn("Aprovar faz", screens["proposalslist"])
        for name, text in screens.items():   # every recommendation names a command, never a menu path
            with self.subTest(command=name):
                self.assertNotIn("menu →", text)
                self.assertNotRegex(text, r"\w\((s|es|ões|is)\)")
        self.assertIn("Lido dos registros de uso de skills", screens["skills"])
        self.assertIn("0,7 GB", screens["care"])           # sizes for a person, never raw bytes
        self.assertIn("236,0 KB", screens["care"])
        self.assertIn("O swap está quase cheio", screens["care"])
        self.assertIn("Pede atenção", screens["care"])   # one name for the heading everywhere (0.12.0)
        self.assertIn("O sistema espera um reinício (libc6)", screens["system"])
        self.assertRegex(screens["care"], r"Conferido em \d\d/\d\d/\d{4} \d\d:\d\d \S+\.")   # local time and its zone
        # --format json is the structure scripts read, unchanged
        queue = json.loads(self.run_cli(["proposals", "list", "--format", "json"]))
        self.assertEqual(queue[0]["kind"], "lesson")
        self.assertEqual(json.loads(self.run_cli(["proposals", "--format", "json"]))[0]["id"], queue[0]["id"])
        listed = json.loads(self.run_cli(["lessons", "list", "--format", "json"]))
        self.assertEqual(listed["lessons"][0]["status"], "candidate")
        self.assertIn("basis", json.loads(self.run_cli(["skills", "--format", "json"])))
        self.assertEqual(json.loads(self.run_cli(["environment", "--format", "json"]))["lead"], None)
        self.assertEqual(json.loads(self.run_cli(["experiments", "--format", "json"])), [])
        with patch("time.time", return_value=NOW):
            self.assertIn(json.loads(self.run_cli(["learn", "--format", "json"]))["automatic"]["mode"], ("full", "safe", "off"))

    def test_english_follows_lang_and_programmatic_callers_still_get_the_structure(self):
        self.failures()
        with patch("time.time", return_value=NOW):
            self.run_cli(["learn"])
        english = self.run_cli(["lessons", "list", "--lang", "en"])
        self.assertTrue(english.startswith("# Lessons"))
        self.assertIn("When calling `files_search`", english)
        from types import SimpleNamespace

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            cli.execute(SimpleNamespace(evolution_action="experiments", lang="pt"), self.home, runtime_config=FORM)
        self.assertIsInstance(json.loads(stream.getvalue()), list)   # no `format`: the menu and the panel's way

    def test_care_doctor_that_found_problems_is_not_a_failure(self):
        ran = {"ok": True, "problems_found": 2, "steps": [{"command": "hermes doctor", "exit": 1, "tail": []}],
               "doctor": {"problems": ["✗ a", "✗ b"], "warnings": [], "summary": ["Found 2 issue(s)"],
                          "actions": ["Run hermes setup", "Run hermes sessions repair"], "found": 2}}
        with patch("hermes_atlas_evolution.hermes_care.run_action", return_value=ran):
            portuguese = self.run_cli(["care", "doctor", "--yes"])
            english = self.run_cli(["care", "doctor", "--yes", "--lang", "en"])
        self.assertIn("A checagem do Hermes encontrou 2 problemas.", portuguese)
        self.assertIn("1. Run hermes setup", portuguese)
        self.assertIn("## O que o Hermes sugere (texto do Hermes, em inglês)", portuguese)
        self.assertNotIn("falh", portuguese.lower())
        self.assertIn("Hermes doctor found 2 problems.", english)
        self.assertNotIn("fail", english.lower())


class MessagesTest(Base):
    def test_owner_add_says_the_change_applies_without_a_restart(self):
        text = self.run_cli(["owner", "add", "555", "--lang", "en"])
        self.assertNotIn("gateway restart", text)
        self.assertIn("no restart is needed", text)
        self.assertIn("owner_sender_ids '[\"555\"]'", text)
        self.assertIn("não precisa reiniciar", self.run_cli(["owner", "add", "555"]))
        self.assertTrue(self.run_cli(["owner", "--lang", "en"]).startswith("# Which chats count as you"))
        shown = self.run_cli(["owner"])
        self.assertIn("plataformas: nenhuma", shown)
        self.assertIn("a sua CLI", shown)
        self.assertIn("na próxima verificação", self.run_cli(["owner", "add", "555"]))

    def test_without_a_terminal_each_refusal_names_the_command_that_goes_ahead(self):
        with self.assertRaises(SystemExit) as care:
            self.run_cli(["care", "pm-doctor"])
        self.assertIn("`hermes pm doctor`", str(care.exception))
        self.assertIn("`hermes -p default evolution care pm-doctor --yes`", str(care.exception))
        with patch("hermes_atlas_evolution.system_update.repair") as repair, self.assertRaises(SystemExit) as fixed:
            self.run_cli(["system", "repair", "--lang", "en"])
        repair.assert_not_called()
        self.assertIn("dpkg --configure -a", str(fixed.exception))
        self.assertIn("`hermes -p default evolution system repair --yes`", str(fixed.exception))
        planned = {"upgrade": [{"name": "libc6", "from": "1", "to": "2", "attention": []}], "install": [], "remove": [],
                   "kept_back": [], "attention": []}
        with patch("hermes_atlas_evolution.system_update.plan", return_value=planned), \
                patch("hermes_atlas_evolution.system_update.prepare") as prepare, self.assertRaises(SystemExit) as upgrade:
            self.run_cli(["system", "upgrade"])
        prepare.assert_not_called()
        self.assertIn("`hermes -p default evolution system plan`", str(upgrade.exception))
        self.assertIn("`hermes -p default evolution system upgrade --yes`", str(upgrade.exception))
        self.assertIn("Nada mudou", str(upgrade.exception))
        self.failures()
        with patch("time.time", return_value=NOW):
            self.run_cli(["learn"])
        with self.assertRaises(SystemExit) as lesson:
            self.run_cli(["lessons", "approve", "1"])
        self.assertIn("`hermes -p default evolution lessons approve 1 --yes`", str(lesson.exception))

    def test_retiring_a_retired_lesson_records_nothing(self):
        self.failures()
        with patch("time.time", return_value=NOW):
            self.run_cli(["learn"])
        self.assertIn("está ativa agora", self.run_cli(["lessons", "approve", "1", "--yes"]))
        self.assertIn("foi aposentada", self.run_cli(["lessons", "retire", "1"]))
        self.assertEqual(self.run_cli(["lessons", "retire", "1"]), "A lição #1 já está aposentada; nada mudou.\n")
        self.assertEqual(self.run_cli(["lessons", "retire", "1", "--lang", "en"]),
                         "Lesson #1 is already retired; nothing changed.\n")
        history = json.loads(self.run_cli(["decisions", "list", "--format", "json"]))
        self.assertEqual([item["action"] for item in history], ["retire", "approve"])   # no retired → retired
        shown = self.run_cli(["decisions"])
        self.assertIn("aposentou a lição #1", shown)
        self.assertIn("hermes -p default evolution decisions undo <id>", shown)   # the profile named (0.11.0)

    def test_a_decided_proposal_is_said_in_words(self):
        self.failures()
        with patch("time.time", return_value=NOW):
            self.run_cli(["learn"])
        self.assertIn("Proposta #1 aprovada", self.run_cli(["proposals", "approve", "1", "--yes"]))
        with self.assertRaises(SystemExit) as again:
            self.run_cli(["proposals", "reject", "1"])
        self.assertEqual(str(again.exception), "Evolution: A proposta 1 já foi aplicada; nada mudou.")
        listed = self.run_cli(["proposals", "list", "--all"])
        self.assertIn("situação: aplicada", listed)
        self.assertFalse(LEAKS.search(listed), listed)

    def test_a_pack_that_cannot_be_written_is_said_without_an_exception_name(self):
        with self.assertRaises(SystemExit) as refused:
            self.run_cli(["lessons", "export", "--out", str(self.home / "missing" / "pack.json")])
        self.assertNotIn("Error", str(refused.exception))
        self.assertIn("confira se a pasta existe", str(refused.exception))

    def test_help_explains_every_subcommand_in_the_profile_language(self):
        for language, expected in (("pt", ("Lista as lições com o texto exato", "Aposenta uma lição ativa",
                                           "Rejeita uma proposta", "Uma nota guardada com a sua decisão",
                                           "Lista as últimas decisões", "pm-doctor: `hermes pm doctor`",
                                           "termina uma atualização que parou no meio")),
                                   ("en", ("List the lessons with their exact text", "Retire an active lesson",
                                           "Reject a proposal", "A note kept with your decision",
                                           "List the latest decisions", "pm-doctor: `hermes pm doctor`",
                                           "finish an update that stopped half-way"))):
            parser = self.parser(language)
            commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
            shown = " ".join(" ".join(parser.format_help().split()) for parser in
                             [commands.choices[name] for name in ("lessons", "proposals", "decisions", "care", "system")]
                             + [sub for name in ("lessons", "proposals", "decisions") for action in
                                commands.choices[name]._actions if isinstance(action, argparse._SubParsersAction)
                                for sub in action.choices.values()])
            for sentence in expected:
                with self.subTest(language=language, sentence=sentence):
                    self.assertIn(sentence, shown)


class DigestAndStatusTest(Base):
    def test_the_digest_ends_with_one_complete_instruction(self):
        from hermes_atlas_evolution import report

        built = report.build(self.home, now=NOW, days=7, lang="pt")
        built["proposals"] = [
            {"id": 1, "kind": "lesson", "risk": "low", "subject": "files_search", "evidence": {"lesson_id": 4}},
            {"id": 2, "kind": "skill_lock", "risk": "low", "subject": "deploy", "evidence": {},
             "command": "hermes curator lock deploy"}]
        for lang in ("pt", "en"):
            text = report.markdown(built, lang)
            self.assertIn("`hermes -p default evolution proposals reject <id>`", text)
            self.assertNotIn(" `reject <id>`", text)
        built["proposals"] = built["proposals"][:1]   # only lessons: decided with `lessons review`, nothing else said
        text = report.markdown(built, "pt")
        self.assertIn("hermes -p default evolution lessons review", text)
        self.assertNotIn("proposals approve", text)
        self.assertIn("## Lições", text)
        self.assertNotIn("Manual de lições", text)
        self.assertRegex(text.splitlines()[0], r"\(\d\d/\d\d a \d\d/\d\d/\d{4}, UTC\)$")

    def test_status_reads_plainly_counts_what_it_lists_and_shows_hermes_doctor(self):
        report = status.build(self.home, now=NOW, config={})
        report["environment"] = {"summary": {"profiles": 2, "used_7d": 1, "plugins_on": 3, "plugins_idle": 1,
                                             "skills": 9}, "findings": []}
        report["doctor"] = status._doctor(self.home, {"doctor": {
            "default": {"at": NOW, "ok": True, "checks": 31, "fail": 1, "warn": 2, "optional": 4, "issues": 1,
                        "seconds": 3.1},
            "work": {"at": NOW, "ok": False, "error": "timeout"}}})
        portuguese, english = status.markdown(report, "pt"), status.markdown(report, "en")
        self.assertIn("1 perfil · 0 em uso nos últimos 7 dias", portuguese)   # the table below lists this one
        self.assertEqual(portuguese.count("| **default** |"), 1)
        self.assertIn("## Checagem do Hermes (`hermes doctor`)", portuguese)
        self.assertIn("`default`: 1 problema, 2 avisos · 4 itens opcionais não configurados · 31 checagens", portuguese)
        self.assertIn("`work`: não rodou (demorou demais)", portuguese)
        self.assertIn("`default`: 1 problem, 2 warnings · 4 optional items not set up · 31 checks", english)
        self.assertIn("## Hermes doctor", english)
        self.assertEqual(report["doctor"]["default"]["checks"], 31)
        json.dumps(report)   # the JSON of status carries it too
        for text in (portuguese, english):
            self.assertNotIn("0.5.0", text)
            self.assertNotIn("UTC)", text.split("\n")[3] if "Observando" in text else "")
        self.assertIn("**Rodada de aprendizado:** a cada 60 min", portuguese)
        self.assertIn("**Learning pass:** every 60 min", english)
        for wrong in ("Piloto", "piloto", "Guarda de runtime", "modelo fixo", "(s)", "(ões)", "menu →"):
            self.assertNotIn(wrong, portuguese)
        self.assertNotIn("Autopilot", english)
        self.assertIn("0 ativas · 0 aguardando aprovação · 0 aposentadas", portuguese)
        self.assertIn("**Modo Auto:**", portuguese)
        self.assertIn("**Auto mode:**", english)
        self.assertRegex(portuguese, r"\*\*Desde 00:00 UTC")
        self.assertRegex(english, r"\*\*Since 00:00 UTC")
        self.assertNotIn("passada", portuguese)
        report["lessons"]["active"] = 1
        self.assertIn("1 ativa ·", status.markdown(report, "pt"))

    def test_doctor_absent_or_unreadable_is_no_line(self):
        self.assertEqual(status._doctor(self.home, None), {})
        self.assertEqual(status._doctor(self.home, {"doctor": "damaged"}), {})
        report = status.build(self.home, now=NOW, config={})
        self.assertEqual(report["doctor"], {})
        self.assertNotIn("Checagem do Hermes", status.markdown(report, "pt"))

    def test_sizes_and_times_for_a_person(self):
        self.assertEqual(status.human_size(241_664), "236.0 KB")
        self.assertEqual(status.human_size(12 * 2**20), "12.0 MB")
        self.assertEqual(status.human_size(700 * 2**20), "0.7 GB")
        self.assertEqual(status.human_size(None), "?")
        self.assertRegex(status.local_time(NOW, "pt"), r"^\d\d/\d\d/\d{4} \d\d:\d\d \S+$")
        self.assertRegex(status.local_time("2026-11-16T12:00:00+00:00", "en"), r"^\d{4}-\d\d-\d\d \d\d:\d\d \S+$")
        self.assertEqual(status.local_time(None, "en"), "?")


class PlainWordsTest(Base):
    """The owner's standard: proper singular and plural, one word for each thing, a command for every next step."""

    def test_withdrawn_lessons_have_their_own_group_in_status_lessons_and_digest(self):
        from hermes_atlas_evolution import lessons, report

        with experience(self.home) as conn:
            kept = lessons.add_custom(conn, "When a deploy fails, read the release notes first.", tool="terminal",
                                      origin="agent", now=1)
            conn.execute("UPDATE lesson SET status='retired', verdict='withdrawn' WHERE id=?", (kept,))
        listed = self.run_cli(["lessons", "list"])
        self.assertIn("## Descartadas sem decisão (fila limpa) · 1", listed)
        self.assertNotIn("Aposentadas", listed)
        self.assertIn("## Dropped from the queue without a decision · 1", self.run_cli(["lessons", "list", "--lang", "en"]))
        shown = status.markdown(status.build(self.home, now=NOW, config={}), "pt")
        self.assertIn("0 aposentadas · 1 descartada sem decisão (fila limpa)", shown)
        digest = report.markdown(report.build(self.home, now=NOW, days=7, lang="pt"), "pt")
        self.assertIn("aposentadas: 0 · descartadas sem decisão (fila limpa): 1", digest)

    def test_counts_agree_with_their_number(self):
        report = status.build(self.home, now=NOW, config={})
        report["reflector"]["drafts_waiting"] = 1
        report["chats"] = [{"origin": "telegram", "sessions": 1, "covered": False, "senders": 2}]
        report["attention"].append("owner_unknown")
        report["care"], report["system"] = CARE, {**SYSTEM, "security": 1}
        report["attention"] += ["os_security_updates"]
        portuguese, english = status.markdown(report, "pt"), status.markdown(report, "en")
        self.assertIn("1 rascunho aguardando", portuguese)
        self.assertIn("`telegram`: 1 sessão em 7 dias de 2 remetentes", portuguese)
        self.assertIn("1 atualização de segurança do sistema espera: rode `hermes -p default evolution system upgrade`",
                      portuguese)
        self.assertIn("1 security update of the system is waiting: run `hermes -p default evolution system upgrade`",
                      english)
        self.assertIn("1 draft waiting", english)
        for text in (portuguese, english):
            self.assertNotRegex(text, r"\w\((s|es|ões|is)\)")
        from hermes_atlas_evolution.lessons import add_custom
        from hermes_atlas_evolution.status import pending_notice

        with experience(self.home) as conn:
            add_custom(conn, "When deploying, run the smoke tests first.", tool="general", origin="operator", now=NOW)
        self.assertIn("1 lição aguardando a sua aprovação", pending_notice(self.home, now=NOW, lang="pt")["message"])

    def test_learn_names_the_lesson_and_ends_with_a_sentence(self):
        from hermes_atlas_evolution import lessons

        self.failures()
        with experience(self.home) as conn:
            lessons.add_custom(conn, "When a deploy fails, read the release notes first.", tool="terminal",
                               origin="agent", now=1)
        with patch("time.time", return_value=NOW):
            learned = self.run_cli(["learn"])
        self.assertIn("lição escrita pelo agente para `terminal` · decida com `hermes -p default evolution lessons review`",
                      learned)
        self.assertRegex(learned, r"Rodada concluída em \d\d/\d\d/\d{4} \d\d:\d\d \(\S+\)\.\n$")

    def test_the_digest_says_failures_in_words_and_titles_like_the_proposals_screen(self):
        from hermes_atlas_evolution import report

        self.failures()
        built = report.build(self.home, now=NOW, days=7, lang="pt")
        built["proposals"] = [{"id": 5, "kind": "plugin_leftover", "risk": "low", "subject": "default/.install-abc",
                               "evidence": {"profile": "default", "mb": 1.0}, "command": ""}]
        text = report.markdown(built, "pt")
        self.assertIn("faltou um argumento obrigatório (`servico`): 4 vezes, 2 dias", text)
        self.assertNotIn("missing_argument", text)
        self.assertIn("aprovar move `default/plugins/.install-abc` para a lixeira do plugin", text)
        self.assertNotIn("__trash__", text)
        self.assertNotIn("pelo menu", text)
        from hermes_atlas_evolution import explain

        self.assertIn(explain.describe(built["proposals"][0], "pt")["title"], text)
        built["proposals"] = [{"id": 6, "kind": "doctor_security", "risk": "medium", "subject": "work/security.GHSA-x",
                               "evidence": {"profile": "work"}, "command": "hermes -p work doctor"}]
        for lang, words in (("pt", "perfil `work`"), ("en", "profile `work`")):
            text = report.markdown(built, lang)
            self.assertIn(words, text)
            self.assertNotIn("security.GHSA-x", text)
            listed = cli._proposals_md([{**built["proposals"][0], "status": "open"}], self.home, lang, everything=False)
            self.assertIn(words, listed)
            self.assertNotIn("security.GHSA-x", listed)

    def test_maintenance_commands_print_text_by_default_and_json_on_request(self):
        # health, history and tick were removed in 0.11.0 with the legacy stack: usage is what is left
        text = self.run_cli(["usage"])
        self.assertTrue(text.startswith("# "), text)
        self.assertFalse(LEAKS.search(text), text)
        json.loads(self.run_cli(["usage", "--format", "json"]))
        self.assertIn("Uso de modelos e ferramentas", text)

    def test_portuguese_help_has_no_english_from_argparse(self):
        parser = self.parser("pt")
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        for shown in (parser.format_help(), commands.choices["lessons"].format_help(),
                      commands.choices["learn"].format_help()):
            self.assertTrue(shown.startswith("uso: "), shown)
            for english in ("usage:", "options:", "positional arguments:", "show this help message"):
                self.assertNotIn(english, shown)
        self.assertIn("mostra esta ajuda e sai", commands.choices["learn"].format_help())
        self.assertIn("Faz uma rodada de aprendizado", parser.format_help())
        self.assertIn("Adiciona uma lição sua", " ".join(commands.choices["lessons"].format_help().split()))
        self.assertNotIn("passada", parser.format_help())
        self.assertTrue(self.parser("en").format_help().startswith("usage: "))

    def test_schedule_digest_without_hermes_on_the_path_says_where_to_run_it(self):
        with patch("hermes_atlas_evolution.actions.schedule_digest", side_effect=ValueError("The hermes command was not found: run this where `hermes --version` works")), \
                self.assertRaises(SystemExit) as refused:
            self.run_cli(["schedule-digest"])
        self.assertIn("não está no PATH deste terminal", str(refused.exception))
        with patch("hermes_atlas_evolution.actions.schedule_digest", return_value={"scheduled": False, "reason": "already"}):
            self.assertIn("já está agendado", self.run_cli(["schedule-digest"]))


if __name__ == "__main__":
    unittest.main()
