"""0.9.0: the terminal menu reads the same in Portuguese and English, with no codes, no Markdown marks and no
internal names: the undo sentence, a proposal's lesson, the skip reasons, the lesson mode, the settings' errors, the
health screen, Hermes' doctor (from the menu and per profile), and the skill and settings form the agent and Hermes
show."""

import json
import os
import re
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_atlas_evolution import admin, hermes_care, menu, proposals
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.menu import Console, Menu, plain

from test_menu_082 import Script
from test_panel_decisions import Base

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine

NOW = time.time()
ROOT = Path(__file__).resolve().parent.parent


def propose(home: Path, kind: str, subject: str, evidence: dict, command: str, risk: str = "medium") -> int:
    with experience(home) as conn:
        cursor = conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""", (f"{kind}:{subject}", kind, risk, subject, json.dumps(evidence),
                                                        command, NOW, NOW))
        return cursor.lastrowid


class MenuBase(Base):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"NO_COLOR": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def make(self, *answers, lang="en", runtime=None, writer=None, home=None):
        script = Script(*answers)
        return script, Menu(home or self.home, runtime or {}, lang=lang,
                            console=Console(read=script.read, write=script.write, width=110, color=False),
                            writer=writer, clock=lambda: NOW)

    def menu(self, *answers, **options):
        script, screen = self.make(*answers, **options)
        screen.run()
        return script.text


class UndoAndLessonTest(MenuBase):
    TRASH = {"applied": True, "id": 7, "decision": 3,
             "undo": "put second/plugins/.install-abc back from the plugin's trash",
             "undo_args": ["@second", "__restore__", ".install-abc"]}

    def test_the_undo_sentence_is_in_the_menus_language(self):
        item = {"kind": "plugin_leftover", "subject": "second/.install-abc", "evidence": {}}
        _script, screen = self.make(lang="pt")
        self.assertEqual(screen.undo_text(self.TRASH, item),
                         "trazer second/plugins/.install-abc de volta da lixeira do plugin")
        older = {key: value for key, value in self.TRASH.items() if key != "undo_args"}   # a result without the args
        self.assertEqual(screen.undo_text(older, item), "trazer second/plugins/.install-abc de volta da lixeira do plugin")
        self.assertEqual(screen.undo_text({"applied": True, "undo": "hermes curator unlock x"}, {"kind": "advice"}),
                         "hermes curator unlock x")

    def test_applying_from_the_menu_says_the_undo_in_portuguese(self):
        propose(self.home, "skill_lock", "hermes-admin", {"rewrites": 75, "flipflops": 0, "window_days": 7},
                "hermes curator lock hermes-admin")
        with patch.object(admin, "decide", return_value=dict(self.TRASH)):
            shown = self.menu("2", "1", "a", "", "0", lang="pt")
        self.assertIn("Aplicada. Para desfazer: trazer second/plugins/.install-abc de volta da lixeira do plugin", shown)
        self.assertNotIn("put back", shown)

    def test_a_lesson_proposal_shows_the_lesson_in_the_menus_language_under_its_own_label(self):
        with experience(self.home) as conn:
            conn.execute("""INSERT INTO lesson(signature, tool, klass, hint, origin, status, created, updated)
                VALUES ('sig1', 'files_search', 'missing_argument', 'query', 'derived', 'candidate', 1, 1)""")
            proposals.refresh(conn, now=NOW, lang="en")   # the last pass ran in English
            english = json.loads(conn.execute("SELECT evidence FROM proposal WHERE kind='lesson'").fetchone()[0])["lesson"]
            row = conn.execute("SELECT * FROM lesson").fetchone()
        from hermes_atlas_evolution import lessons

        portuguese = lessons.render(row, "pt")
        self.assertNotEqual(english, portuguese)
        shown = self.menu("2", "1", "", "", "0", lang="pt")
        self.assertIn("Lição:", shown)
        self.assertIn(portuguese.split()[0], shown)
        self.assertNotIn(english, shown)
        self.assertNotIn("O que foi visto", shown)

    def test_skip_reasons_are_sentences(self):
        a = propose(self.home, "skill_lock", "one", {"rewrites": 9, "window_days": 7}, "hermes curator lock one")
        b = propose(self.home, "skill_lock", "two", {"rewrites": 9, "window_days": 7}, "hermes curator lock two")
        outcome = {"applied": [], "skipped": [{"id": a, "reason": "busy"}, {"id": b, "reason": "one_by_one"}]}
        with patch.object(admin, "decide", return_value=outcome):
            shown = self.menu("2", "t", "s", "", "0", lang="pt")
        self.assertIn("o banco do Evolution estava ocupado", shown)
        self.assertIn("abra-a sozinha e escolha qual plugin fica", shown)
        self.assertNotRegex(shown, r"#\d+: (busy|one_by_one)")

    def test_an_unknown_key_is_said_not_ignored(self):
        propose(self.home, "skill_lock", "hermes-admin", {"rewrites": 75, "window_days": 7}, "hermes curator lock hermes-admin")
        shown = self.menu("2", "1", "z", "", "0", lang="pt")
        self.assertIn("Essa tecla não é uma das opções mostradas: nada foi feito.", shown)
        derived, _draft, _agent = self.lessons_waiting()
        shown = self.menu("1", "1", "q", "", "0")
        self.assertIn("That key is not one of the options shown: nothing was done.", shown)


class MainScreenTest(MenuBase):
    def test_lesson_mode_and_mode_read_as_names_with_agreement(self):
        derived, _draft, _agent = self.lessons_waiting()
        with experience(self.home) as conn:
            from hermes_atlas_evolution import decisions

            decisions.decide_lesson(conn, derived, "approve", now=NOW, actor="dashboard")
        shown = self.menu("0", lang="pt")
        self.assertIn("Lições: automáticas · 1 ativa · 2 esperando você · ", shown)
        self.assertIn("Modo: Auto", shown)
        self.assertNotIn("modo auto", shown)
        self.assertIn("Lessons: automatic · 1 active", self.menu("0"))
        self.assertIn("(ainda não verificado)", shown)
        self.assertNotIn("(…)", shown)
        settings = self.menu("6", "1", "", "", "0", lang="pt")
        for text in ("Lições: automáticas", "2 com aprovação: toda lição espera você", "3 desligadas: só medir"):
            self.assertIn(text, settings)
        self.assertNotIn("suggest", settings)

    def test_empty_enter_with_suggestions_asks_for_a_number_or_a_letter(self):
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {"mode": "manual"}}}}}
        shown = self.menu("", "0", lang="pt", runtime=runtime)
        self.assertIn("Ligar o Modo Auto", shown)
        self.assertIn("Digite um número ou uma letra da lista.", shown)
        english = self.menu("", "0", runtime=runtime)
        self.assertIn("Turn on Auto mode", english)
        self.assertIn("Type a number or a letter from the list.", english)

    def test_the_menu_does_not_send_the_owner_to_the_menu(self):
        self.lessons_waiting()
        for lang, gone in (("pt", "decida pelo menu"), ("en", "decide in the menu")):
            shown = self.menu("0", lang=lang)
            self.assertNotIn(gone, shown)
            self.assertNotIn("`", shown)


class SettingsErrorsTest(MenuBase):
    def test_a_refused_value_names_the_setting_and_stays_in_the_settings(self):
        written = []
        shown = self.menu("6", "3", "abc", "", "0", lang="pt", writer=lambda values: written.append(values) or list(values))
        self.assertIn("Não feito: “Chamadas do refletor por dia” aceita só um número inteiro", shown)
        self.assertNotIn("reflector_daily_limit", shown)
        after = shown.split("Não feito:")[1]
        self.assertIn("Número para mudar", after.split("Escolha:")[0])   # the settings again, not the main screen
        self.assertEqual(written, [])
        out_of_range = self.menu("6", "3", "500", "", "0", writer=lambda values: list(values))
        self.assertIn("Not done:", out_of_range)
        self.assertNotIn("reflector_daily_limit", out_of_range)

    def test_internal_names_in_messages_read_as_labels(self):
        _script, screen = self.make(lang="en")
        self.assertEqual(screen.named("evolution.slow_turn_seconds must be an integer from 5 to 3600"),
                         "“A turn is slow after” must be an integer from 5 to 3600")
        self.assertEqual(screen.named("language must be auto, en or pt"), "“Language” must be auto, en or pt")
        self.assertEqual(screen.named("Use the language en or pt"), "Use the language en or pt")   # a plain word stays

    def test_turning_on_the_agent_tools_says_a_restart_is_needed(self):
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {}}}}}

        def writer(values):
            runtime["plugins"]["entries"]["hermes-atlas-evolution"]["settings"].update(values)
            return list(values)

        shown = self.menu("6", "9", "y", "", "0", runtime=runtime, writer=writer)
        self.assertIn("Applies after hermes gateway restart (in the terminal, at the next session): “Let the agent read and"
                      " propose lessons”", shown)


class MarkdownTest(unittest.TestCase):
    SAMPLE = """# Atlas Evolution 0.9.0 · profile `work`

- **Today:** 3 turns · `hermes evolution learn`
  - nested item
- _italic note_ and snake_case_name stay

## Needs attention

| profile | sessions (7 d) | plugin |
|---|---:|---|
| **work** | 40 | installed |
| default | 2 | not installed |

> A quote.
"""

    def test_no_markdown_marks_are_left(self):
        text = plain(self.SAMPLE, width=100)
        for mark in ("#", "**", "`", "|", "_italic"):
            self.assertNotIn(mark, text)
        lines = text.splitlines()
        self.assertEqual(lines[0], "Atlas Evolution 0.9.0 · profile work")
        self.assertTrue(set(lines[1]) == {"═"})
        self.assertIn("  • Today: 3 turns · hermes evolution learn", lines)
        self.assertIn("    · nested item", lines)
        self.assertIn("snake_case_name", text)
        header = next(line for line in lines if line.strip().startswith("profile"))
        row = next(line for line in lines if line.strip().startswith("work"))
        self.assertEqual(header.index("sessions"), row.index("40"))   # columns line up
        self.assertIn("  │ A quote.", lines)

    def test_a_table_too_wide_becomes_label_value_lines(self):
        table = "| name | what |\n|---|---|\n| one | " + "x" * 120 + " |\n"
        text = plain(table, width=60)
        self.assertIn("• name: one", text)
        self.assertIn("what: " + "x" * 10, text)

    def test_the_digest_and_the_full_status_screens_are_plain(self):
        base = Base("setUp")
        base.setUp()
        self.addCleanup(base.doCleanups)
        script = Script("9", "", "10", "", "0")
        Menu(base.home, {}, lang="pt", console=Console(read=script.read, write=script.write, width=110, color=False),
             clock=lambda: NOW).run()
        text = script.text
        self.assertNotRegex(text, r"(?m)^#|\*\*|`|^\|")
        self.assertNotIn("Playbook", text)
        self.assertNotIn("Autopilot", text)
        self.assertNotIn("passada", text)


class HealthTest(MenuBase):
    CARE = {"gateway": {"running": False, "crashed": False, "version": None, "rss": None, "heartbeat_age": None,
                        "stale": False},
            "database": {"present": True, "journal": "wal", "wal": 241_664, "quick_check": "ok"},
            "memory": {"total": 12 * 2**30, "available": 7 * 2**30, "swap_total": 0, "swap_used": 0},
            "load": {"15m": 1.25, "cpus": 6}, "free": 67 * 2**30, "total": 193 * 2**30,
            "disk_per_day": 2_000_000, "days_to_full": 36_000.4, "sizes": {}, "unused_tools": [], "reclaimable": 0,
            "findings": []}

    def care(self, *answers, lang="pt", care=None, run=None):
        with patch.object(hermes_care, "cached", return_value=care or self.CARE), \
                patch.object(hermes_care, "run_action", return_value=run or {"ok": True, "steps": [{"exit": 0}]}):
            return self.menu("12", *answers, lang=lang)

    def test_a_stopped_gateway_without_data_reads_plainly_and_sizes_are_human(self):
        shown = self.care("")
        self.assertIn("Gateway: parado · sem dados", shown)
        self.assertNotIn("versão ?", shown)
        self.assertIn("WAL 236 KB", shown)
        self.assertNotIn("241664 B", shown)
        self.assertNotIn("enche em", shown)   # 36 000 days is no forecast
        self.assertIn("carga 1,2", shown)
        english = self.care("", lang="en")
        self.assertIn("Gateway: stopped · no data", english)
        soon = dict(self.CARE, days_to_full=12.6)
        self.assertIn("(enche em 13 dias)", self.care("", care=soon))
        running = dict(self.CARE, gateway={"running": True, "version": "0.23.1", "rss": 640 * 2**20, "heartbeat_age": 4000})
        self.assertIn("Gateway: rodando · versão 0.23.1 · memória 640 MB · sinal de vida há 67 min",
                      self.care("", care=running))

    def test_the_doctor_options_say_what_they_do(self):
        shown = self.care("", lang="en")
        for text in ("npm audit", "gh auth status", "imports the dashboard", "minutes"):
            self.assertIn(text, shown)
        fix = self.care("4", "", "", lang="pt")
        for text in ("criar pastas que faltam e o arquivo .env", "migrar o config.yaml", "reparar o banco de sessões",
                     "recriar o lançador hermes", "baixar o Chromium", "guarda o config.yaml, o .env, o banco de sessões"
                     " e as tarefas agendadas (cron)"):
            self.assertIn(text, " ".join(fix.split()))   # wrapped to the terminal's width

    def test_a_doctor_that_found_problems_did_not_fail(self):
        result = {"ok": True, "problems_found": 2, "steps": [{"command": "hermes doctor", "exit": 1, "tail": []}],
                  "doctor": {"problems": ["✗ config.yaml is out of date"], "warnings": ["⚠ gh not logged in"],
                             "summary": ["Found 2 issue(s) to address:"], "found": 2,
                             "actions": ["Run 'hermes config migrate'", "Install ripgrep"]}}
        shown = self.care("3", "s", "", run=result)
        self.assertIn("A checagem do Hermes (hermes doctor) encontrou 2 problemas (texto do Hermes, em inglês):", shown)
        self.assertIn("O que o Hermes diz para fazer (texto do Hermes, em inglês):", shown)
        self.assertIn("1. Run 'hermes config migrate'", shown)
        self.assertIn("2. Install ripgrep", shown)
        self.assertNotIn("falhou", shown.split("Rodar")[1])
        self.assertNotIn("O Hermes respondeu com saída", shown)
        older = dict(result)
        older.pop("problems_found")
        older["ok"] = False
        older["doctor"] = {key: value for key, value in result["doctor"].items() if key not in ("actions", "found")}
        shown = self.care("3", "y", "", lang="en", run=older)
        self.assertIn("Hermes doctor found 1 problem:", shown)
        self.assertNotIn("(texto do Hermes", shown)
        self.assertIn("✗ config.yaml is out of date", shown)
        backup = {"ok": False, "reason": "backup_failed", "steps": [{"exit": 2, "tail": ["disk full"]}]}
        self.assertIn("The backup failed (exit 2), so nothing was repaired:", self.care("4", "y", "", lang="en", run=backup))

    def test_sizes(self):
        self.assertEqual(menu._bytes(241_664), "236 KB")
        self.assertEqual(menu._bytes(24_166, "pt"), "23,6 KB")
        self.assertEqual(menu._bytes(1.5 * 2**30), "1.5 GB")
        self.assertEqual(menu._bytes(700 * 2**20), "700 MB")
        self.assertEqual(menu._bytes(512), "512 B")
        self.assertEqual(menu._bytes(None), "?")


class DoctorLineTest(MenuBase):
    RESULT = {"ok": True, "seconds": 4.2, "checks": [
        {"rows": [{"mark": "fail", "text": "config.yaml is out of date"}, {"mark": "warn", "text": "ripgrep missing"},
                  {"mark": "warn", "text": "Brave Search (not configured)"}, {"mark": "ok", "text": "Python 3.12"}],
         "issues": ["Run hermes config migrate"], "manual": []}]}

    def test_one_line_per_profile_in_both_languages(self):
        try:
            from hermes_atlas_evolution import doctor   # noqa: F401
        except ImportError:
            self.skipTest("doctor.py is not in this checkout")
        _script, screen = self.make(lang="pt")
        line = screen.doctor_line({"at": NOW, "result": self.RESULT})
        self.assertRegex(line, r"^Checagem do Hermes: 1 problema, 1 aviso, 1 opcional não configurado "
                               r"\(verificado em \d\d/\d\d \d\d:\d\d\)$")
        _script, english = self.make(lang="en")
        self.assertRegex(english.doctor_line({"at": NOW, "result": self.RESULT}),
                         r"^Hermes doctor: 1 problem, 1 warning, 1 optional piece not set up \(checked .+\)$")
        self.assertIn("did not run", english.doctor_line({"at": NOW, "result": {"ok": False, "error": "timeout"}}))
        self.assertIsNone(english.doctor_line(None))
        with experience(self.home) as conn:
            conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('doctor_runs', ?)",
                         (json.dumps({"default": {"at": NOW, "result": self.RESULT}}),))
        self.assertIn("default", Menu.doctor_runs(self.home))

    def test_without_the_module_or_its_data_the_screen_goes_on(self):
        self.assertEqual(Menu.doctor_runs(self.home), {})   # nothing stored yet
        _script, screen = self.make()
        self.assertIsNone(screen.doctor_line({"at": NOW, "result": "?"}))
        try:
            from hermes_atlas_evolution import doctor
        except ImportError:
            return
        with patch.object(doctor, "stored", side_effect=KeyError("x")), \
                patch.object(doctor, "summary", side_effect=KeyError("rows")):
            self.assertEqual(Menu.doctor_runs(self.home), {})
            self.assertIsNone(screen.doctor_line({"at": NOW, "result": {"ok": True, "checks": [{}]}}))


def config_schema(text: str) -> dict:
    """{key: {label, description}} of plugin.yaml's config_schema (no YAML library here: Hermes brings its own)."""
    block = text.split("\nconfig_schema:\n", 1)[1]
    found, key, spec, folded = {}, None, {}, None
    for line in block.splitlines():
        if line and not line.startswith(" "):
            break
        head = re.match(r"^  ([a-z_]+):\s*$", line)
        if head:
            key, spec, folded = head.group(1), {}, None
            found[key] = spec
            continue
        field = re.match(r"^    ([a-z_]+):\s*(.*)$", line)
        if field:
            value = field.group(2).strip()
            folded = field.group(1) if value in (">-", ">", "|") else None
            spec[field.group(1)] = "" if folded else value.strip('"')
        elif folded and line.startswith("      "):
            spec[folded] = (spec[folded] + " " + line.strip()).strip()
    return found


class WordsTest(unittest.TestCase):
    def test_no_mixed_words_in_the_menus_texts(self):
        english = json.dumps(menu.TEXT["en"], ensure_ascii=False)
        portuguese = json.dumps(menu.TEXT["pt"], ensure_ascii=False) + json.dumps(menu.FIELDS, ensure_ascii=False)
        for wrong in ("Who is you", "the Mode auto", "Mode auto"):
            self.assertNotIn(wrong, english)
        for wrong in ("Modo auto", "passada", "Cuidado pelo perfil", "modelo fixo", "Autopilot", "Playbook",
                      "suggest:", "off:"):
            self.assertNotIn(wrong, portuguese)
        self.assertEqual(set(menu.TEXT["en"]), set(menu.TEXT["pt"]))   # every sentence in both languages

    def test_the_review_skill_asks_only_for_what_the_agent_can_see(self):
        skill = (ROOT / "skills" / "evolution-review" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("exact lesson text", skill)
        self.assertNotIn("Never present a lesson without its exact text", skill)
        self.assertIn("text_hidden", skill)
        self.assertNotIn("`high`", skill)
        self.assertIn("approve", skill)
        self.assertIn("approving applies it at once", skill)   # since 0.8.3 the approval runs the lock itself
        self.assertNotIn("such as `hermes curator lock <skill>`", skill)

    def test_the_settings_form_labels_are_bilingual_and_match_the_menu(self):
        schema = config_schema((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(len(schema), len(menu.FIELDS))
        labels = {key: (english, portuguese) for key, english, portuguese in menu.FIELDS}
        for key, spec in schema.items():
            self.assertIn(" · ", spec["label"], key)
            self.assertIn(" — ", spec["description"], key)
            names = spec["label"].split(" · ", 1)
            short = lambda name: re.split(r" \(|:", name)[0].strip()   # noqa: E731
            self.assertEqual(tuple(short(name) for name in names), tuple(short(name) for name in labels[key]), key)


class Polish090Test(MenuBase):
    """The owner's reading of 0.9.0: proper singular and plural, the fixed words, and the menu's own options where
    `status` and the digest name a command."""

    def test_no_singular_plural_placeholders_in_the_menus_texts(self):
        texts = json.dumps([menu.TEXT, menu.FIELDS, menu.MODES, menu.ORIGINS], ensure_ascii=False)
        self.assertNotRegex(texts, r"\w\((?:s|es|ões|is|m|ão|ns)\)")
        _script, screen = self.make(lang="pt")
        self.assertEqual(screen.n("and_more", 1), " (+1 outro tipo)")
        self.assertEqual(screen.n("and_more", 2), " (+2 outros tipos)")
        _script, english = self.make()
        self.assertEqual(english.n("and_more", 1), " (+1 other kind)")
        self.assertEqual(english.n("and_more", 3), " (+3 other kinds)")

    def test_the_fixed_words(self):
        portuguese = json.dumps(menu.TEXT["pt"], ensure_ascii=False) + json.dumps(menu.FIELDS, ensure_ascii=False)
        english = json.dumps(menu.TEXT["en"], ensure_ascii=False) + json.dumps(menu.FIELDS, ensure_ascii=False)
        for gone in ("cópias idênticas", "tira cópia", "Guarda de runtime", "guarda de runtime", "Doctor do Hermes",
                     "doctor do Hermes", "ainda não olhou", "aprove sozinha", "job {job}", "são você", "Piloto automático"):
            self.assertNotIn(gone, portuguese)
        for gone in ("identical memory copies", "Hermes' doctor", "approve it on its own", "repeats and room",
                     "is said once", "is you", "are you", "Autopilot"):
            self.assertNotIn(gone, english)
        self.assertIn("remove entradas repetidas da memória (guarda uma cópia do arquivo antes)", portuguese)
        self.assertIn("removes duplicate memory entries (keeps a copy of the file first)", english)
        self.assertIn("Assistido: sozinho, só remove entradas repetidas da memória; o resto espera você.", portuguese)
        self.assertIn("Assisted: on its own it only removes duplicate memory entries; everything else waits for you.",
                      english)
        shown = self.menu("5", "", "0", lang="pt")
        self.assertIn("Nenhuma ação automática nos últimos 7 dias.", shown)
        self.assertIn("No automatic action in the last 7 days.", self.menu("5", "", "0"))

    def test_status_commands_become_this_menus_options(self):
        cases = {
            ("pt", None): ("Versões antigas ocupam 0,7 GB e nada as usa: `hermes evolution care gc` as remove.",
                           "Versões antigas ocupam 0,7 GB e nada as usa: a opção 12 deste menu, depois a 5 "
                           "(`hermes pm gc`), as remove."),
            ("pt", 12): ("Versões antigas ocupam 0,7 GB e nada as usa: menu → Saúde do Hermes → `hermes pm gc`.",
                         "Versões antigas ocupam 0,7 GB e nada as usa: opção 5 abaixo (`hermes pm gc`)."),
            ("en", 12): ("Old versions take 0.7 GB and nothing uses them: menu → Hermes health → `hermes pm gc`.",
                         "Old versions take 0.7 GB and nothing uses them: option 5 below (`hermes pm gc`)."),
        }
        for (lang, screen), (text, expected) in cases.items():
            self.assertEqual(menu.in_menu(text, lang, screen=screen), expected)
        sentences = {
            "ainda não rodou neste perfil; `hermes evolution learn` roda uma agora":
                "ainda não rodou neste perfil; a opção 11 deste menu roda uma agora",
            "Se um deles é você, rode `hermes evolution owner` no seu próprio terminal:":
                "Se um deles é você, rode a opção 7 deste menu:",
            "1 coisa para arrumar: `hermes evolution environment`": "1 coisa para arrumar: opção 14 deste menu",
            "Decida cada uma no seu terminal com `hermes evolution proposals approve <id>` ou "
            "`hermes evolution proposals reject <id>`, ou pelo menu (`hermes evolution`).":
                "Decida cada uma na opção 2 deste menu.",
        }
        for text, expected in sentences.items():
            self.assertEqual(menu.in_menu(text, "pt"), expected)
        self.assertEqual(menu.in_menu("Decide on each of these in your terminal with `hermes evolution proposals approve "
                                      "<id>` or `hermes evolution proposals reject <id>`, or in the menu "
                                      "(`hermes evolution`).", "en"), "Decide on each of these in option 2 of this menu.")

    def test_the_health_screen_says_sizes_and_its_own_options_in_portuguese(self):
        care = dict(HealthTest.CARE, findings=["swap_full", "unused_tools"], reclaimable=700 * 2**20,
                    memory={"total": 12 * 2**30, "available": 7 * 2**30, "swap_total": 12 * 2**30,
                            "swap_used": 11 * 2**30},
                    unused_tools=[{"folder": "node-22.23.4", "bytes": 200 * 2**20}])
        with patch.object(hermes_care, "cached", return_value=care):
            shown = self.menu("12", "", "0", lang="pt")
        page = " ".join(shown.split("Saúde do Hermes (lida")[1].split())
        self.assertIn("(11,0 GB de 12,0 GB)", page)
        self.assertIn("ocupam 0,7 GB", page)
        self.assertIn("opção 5 abaixo (hermes pm gc)", page)
        self.assertNotIn("menu →", page)
        self.assertNotIn("hermes evolution care", page)
        self.assertNotRegex(page, r"\d\.\d GB")
        self.assertIn("Banco de sessões: modo WAL", page)

    def test_proposals_name_the_profile_the_risk_and_their_own_id(self):
        propose(self.home, "skill_lock", "hermes-admin", {"rewrites": 75, "window_days": 7},
                "hermes curator lock hermes-admin")
        doctor = propose(self.home, "doctor_checkpoints", "work/checkpoints",
                         {"bytes": 614 * 2**20, "projects": 9, "cap": 500 * 2**20}, "hermes -p work checkpoints status",
                         "low")
        shown = self.menu("2", "2", "", "", "0", lang="pt")
        self.assertIn(f"· perfil work (proposta #{doctor})", shown)
        self.assertIn("[risco: baixo]", shown)
        self.assertIn(f"Proposta #{doctor} · ", shown)
        self.assertIn("· perfil work · risco: baixo", shown)
        self.assertNotIn("work/checkpoints", shown)
        self.assertIn("t aplicar a mudança · Enter volta", shown)
        english = self.menu("2", "", "0")
        self.assertIn(f"· profile work (proposal #{doctor})", english)
        self.assertIn("[risk: low]", english)

    def test_hermes_english_text_is_said_as_such_in_portuguese(self):
        result = {"ok": True, "problems_found": 1, "steps": [{"command": "hermes doctor", "exit": 1, "tail": []}],
                  "doctor": {"problems": ["✗ config.yaml is out of date"], "warnings": [], "summary": [], "found": 1,
                             "actions": ["Run 'hermes config migrate'"]}}
        with patch.object(hermes_care, "cached", return_value=HealthTest.CARE), \
                patch.object(hermes_care, "run_action", return_value=result):
            shown = self.menu("12", "3", "s", "", "0", lang="pt")
        self.assertIn("A checagem do Hermes (hermes doctor) encontrou 1 problema (texto do Hermes, em inglês):", shown)
        self.assertIn("O que o Hermes diz para fazer (texto do Hermes, em inglês):", shown)


if __name__ == "__main__":
    unittest.main()
