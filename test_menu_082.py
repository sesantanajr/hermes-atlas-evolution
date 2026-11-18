"""0.8.2: the panel recognizes a real login (HTTPS cookies, Hermes' verified session, secondary profiles), the
interactive terminal menu, and the care of Hermes' memory."""

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import admin, decisions, memory_care
from hermes_atlas_evolution.cli import execute
from hermes_atlas_evolution.experience import experience
from hermes_atlas_evolution.menu import Console, Menu
from hermes_atlas_evolution.operations import close_observers

from test_panel_decisions import WRITTEN, Base, load_api

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine

NOW = time.time()


class LoginRuleTest(unittest.TestCase):
    """The production panel read as read-only with a real password login: the cookie was `__Host-` prefixed over
    HTTPS, and a secondary profile's panel gets no cookie at all."""

    def setUp(self):
        self.api = load_api()

    def test_a_cookie_name_is_never_a_login_where_hermes_publishes_no_session(self):
        policy = self.api.decision_policy   # 0.13.0: an unverified cookie name no longer decides (read-only)
        for cookie in ("__Host-hermes_session_at=abc", "__Secure-hermes_session_at=abc",
                       "theme=dark; __Host-hermes_session_at_gw1=abc", "hermes_session_at=abc"):
            self.assertEqual(policy({"cookie": cookie}, {}), (False, "open_dashboard"), cookie)
        for cookie in ("__Host-hermes_session_at=", "x__Host-hermes_session_at=abc", "hermes_session_rt=abc"):
            self.assertEqual(policy({"cookie": cookie}, {}), (False, "open_dashboard"), cookie)

    def test_in_the_dashboard_process_only_the_session_hermes_verified_counts(self):
        policy = self.api.decision_policy
        gated = SimpleNamespace(auth_required=True)
        login = SimpleNamespace(session=SimpleNamespace(provider="basic", user_id="owner"))
        self.assertEqual(policy({}, {}, state=login, app_state=gated), (True, "login"))
        # the gate ran but left no session (a token route, a public path): a cookie name proves nothing there
        self.assertEqual(policy({"cookie": "__Host-hermes_session_at=abc"}, {}, state=SimpleNamespace(), app_state=gated),
                         (False, "open_dashboard"))
        shared = SimpleNamespace(session=SimpleNamespace(provider="session-token"))
        self.assertEqual(policy({}, {}, state=shared, app_state=gated), (False, "shared_token"))
        self.assertEqual(policy({"authorization": "Bearer t"}, {}, state=login, app_state=gated), (False, "open_dashboard"))

    def test_a_plugin_host_never_decides(self):
        """0.8.2 review: the plugin host cannot see the login, nor (Hermes strips it from its environment) whether a
        shared session token is configured, nor reliably how the dashboard admits people."""
        policy = self.api.decision_policy
        for verdict in (True, False):
            state = SimpleNamespace(hosted_http_authenticated=verdict)
            self.assertEqual(policy({}, {}, state=state), (False, "other_profile"))
            self.assertEqual(policy({"cookie": "__Host-hermes_session_at=abc"}, {}, state=state,
                                    app_state=SimpleNamespace(auth_required=True)), (False, "other_profile"))

    def test_routes_decide_for_a_verified_login_and_for_another_profile_from_the_dashboard_process(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI is installed with Hermes' dashboard only")
        import sys

        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.addCleanup(close_observers)
        root = Path(folder.name)
        enabled = "plugins:\n  enabled: [hermes-atlas-evolution]\n"
        for home in (root, root / "profiles" / "second", root / "profiles" / "off"):
            (home / "plugins" / "hermes-atlas-evolution").mkdir(parents=True)
            (home / "plugins" / "hermes-atlas-evolution" / "plugin.yaml").write_text("name: x\n", encoding="utf-8")
            (home / "config.yaml").write_text(enabled if home.name != "off" else "plugins:\n  enabled: []\n",
                                              encoding="utf-8")
        modules = patch.dict(sys.modules, {"hermes_constants": SimpleNamespace(get_hermes_home=lambda: str(root))})
        modules.start()
        self.addCleanup(modules.stop)

        def app_with(state: dict, *, auth_required=None):
            app = FastAPI()
            if auth_required is not None:
                app.state.auth_required = auth_required

            @app.middleware("http")
            async def hermes_gate(request, call_next):
                for key, value in state.items():
                    setattr(request.state, key, value)
                return await call_next(request)

            app.include_router(self.api.router, prefix="/p")
            return TestClient(app)

        logged = app_with({"session": SimpleNamespace(provider="basic")}, auth_required=True)
        own = logged.get("/p/snapshot").json()
        self.assertEqual((own["profile"], own["can_decide"]), ("default", True))
        other = logged.get("/p/snapshot?profile=current&target=second").json()
        self.assertEqual((other["profile"], other["can_decide"]), ("second", True))
        decided = logged.post("/p/decide?profile=current&target=second", headers={"X-Atlas-Evolution": "1"},
                              json={"kind": "memory", "action": "remove", "file": "MEMORY.md", "fingerprint": "0" * 16})
        self.assertEqual(decided.status_code, 400)   # it reached profile `second`, which has no memory file
        for bad in ("off", "missing", "..", "../second", "second/..", "-x"):
            self.assertEqual(logged.get(f"/p/snapshot?target={bad}").status_code, 400, bad)
        self.assertEqual(logged.get("/p/snapshot?profile=second&target=second").status_code, 400)   # scoped elsewhere
        hosted = app_with({"hosted_http_authenticated": True})
        snap = hosted.get("/p/snapshot").json()
        self.assertEqual((snap["can_decide"], snap["decide_reason"]), (False, "other_profile"))
        self.assertEqual(snap["commands"]["menu"], "hermes -p default evolution")   # explicit even for default
        self.assertEqual(hosted.get("/p/snapshot?profile=current&target=second").status_code, 400)
        self.assertEqual(hosted.post("/p/decide", json={"kind": "undo", "id": 1},
                                     headers={"X-Atlas-Evolution": "1"}).status_code, 403)
        shared = app_with({"session": SimpleNamespace(provider="session-token")}, auth_required=True)
        self.assertEqual(shared.get("/p/snapshot?profile=current&target=second").json()["decide_reason"], "shared_token")
        loopback = app_with({}, auth_required=False)
        self.assertFalse(loopback.get("/p/snapshot?profile=current&target=second").json()["can_decide"])
        # 0.8.2 re-review: a Desktop grant is bound to its own profile, `target` included
        ticket = app_with({"native_http_principal": {"profile_id": str(root)}}, auth_required=False)
        self.assertTrue(ticket.get("/p/snapshot").json()["can_decide"])
        self.assertEqual(ticket.get("/p/snapshot?profile=current&target=second").json()["decide_reason"], "other_profile")
        self.assertEqual(ticket.post("/p/decide?profile=current&target=second", headers={"X-Atlas-Evolution": "1"},
                                     json={"kind": "undo", "id": 1}).status_code, 403)
        own_ticket = app_with({"native_http_principal": {"profile_id": str(root / "profiles" / "second")}},
                              auth_required=False)
        self.assertTrue(own_ticket.get("/p/snapshot?profile=current&target=second").json()["can_decide"])


class ProfileHomeTest(unittest.TestCase):
    def test_only_profiles_of_this_hermes_with_the_plugin_enabled(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        on = {"plugins": {"enabled": ["hermes-atlas-evolution"]}}
        for home in (root, root / "profiles" / "work"):
            (home / "plugins" / "hermes-atlas-evolution").mkdir(parents=True)
            (home / "plugins" / "hermes-atlas-evolution" / "plugin.yaml").write_text("name: x\n", encoding="utf-8")
        outside = Path(folder.name) / "elsewhere"
        outside.mkdir()
        read = lambda home: on  # noqa: E731
        self.assertEqual(admin.profile_home(root / "profiles" / "work", "default", read_config=read)[0], root)
        self.assertEqual(admin.profile_home(root, "work", read_config=read)[0], root / "profiles" / "work")
        for name in ("..", "../elsewhere", "work/x", "", None, "nope"):
            with self.assertRaises(ValueError):
                admin.profile_home(root, name, read_config=read)
        if os.name != "nt":
            (root / "profiles" / "link").symlink_to(root / "profiles" / "work")
            with self.assertRaises(ValueError):
                admin.profile_home(root, "link", read_config=read)
        for config in ({}, {"plugins": {"enabled": []}},
                       {"plugins": {"enabled": ["hermes-atlas-evolution"], "disabled": ["hermes-atlas-evolution"]}}):
            with self.assertRaises(ValueError):
                admin.profile_home(root, "work", read_config=lambda home, config=config: config)


class ReviewFixesTest(Base):
    def test_lessons_off_written_by_hermes_as_false_reads_as_off(self):
        from hermes_atlas_evolution.evolve import options

        self.assertEqual(options({"evolution": {"lessons": False}}, {}, strict=True)["lessons"], "off")
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {"lessons": False}}}}}
        self.assertEqual(admin.current_settings(self.home, runtime)["lessons"], "off")
        with patch("hermes_atlas_evolution.menu.interactive", return_value=False):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                execute(SimpleNamespace(lang="en", evolution_action=None), self.home, runtime_config=runtime)
        self.assertIn("Atlas Evolution", stream.getvalue())

    def test_a_writer_that_hangs_is_a_refusal_not_a_crash(self):
        import subprocess

        def hang(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 60)

        with self.assertRaises(ValueError):
            admin.cli_writer(self.home, runner=hang, executable="/usr/local/bin/hermes")({"lessons": "auto"})

    def test_settings_written_before_a_failure_stay_in_the_history(self):
        calls = []

        def writer(values):
            calls.append(values)
            if len(calls) == 2:
                raise ValueError("Hermes refused the setting")
            return list(values)

        with self.assertRaises(ValueError):
            admin.write_settings(self.home, {}, {"lessons": "suggest", "reflector_daily_limit": 9}, now=NOW,
                                 writer=writer, actor="terminal")
        with experience(self.home) as conn:
            logged = [row[0] for row in conn.execute("SELECT target FROM decision_log")]
        self.assertEqual(logged, ["lessons"])   # written, so it can be undone

    def test_byte_order_mark_and_identical_repeats(self):
        path = write_memory(self.home, "MEMORY.md", ["Same fact.", "Same fact."])
        path.write_bytes("\ufeff".encode("utf-8") + path.read_bytes())
        found = memory_care.review(self.home, {})
        self.assertEqual([issue["kind"] for issue in found["issues"]], ["exact"])
        self.assertEqual(found["issues"][0]["tokens_per_turn"], 0)   # Hermes already sends it once
        self.assertEqual(found["tokens_saved_per_turn"], 0)

    def test_a_digit_that_is_not_ascii_is_just_an_invalid_choice(self):
        script = Script("²", "１", "0")
        Menu(self.home, {}, lang="en", console=Console(read=script.read, write=script.write, width=100, color=False),
             clock=lambda: NOW).run()
        # the main screen suggests turning Auto on (assist is the default), so it also takes letters
        self.assertEqual(script.text.count("Type a number or a letter from the list."), 2)
        self.assertIn("Bye.", script.text)


class RecommendedTest(unittest.TestCase):
    def test_recommended_changes_only_what_differs(self):
        current = {"lessons": "suggest", "reflector": True, "owner_platforms": ["telegram"], "owner_sender_ids": []}
        suggestions = [{"kind": "platform", "value": "api_server"}, {"kind": "sender", "value": "hosted-owner:abc"},
                       {"kind": "platform", "value": "telegram"}]
        self.assertEqual(admin.recommended_settings(current, suggestions),
                         {"lessons": "auto", "mode": "auto", "owner_platforms": ["telegram", "api_server"],
                          "owner_sender_ids": ["hosted-owner:abc"]})
        done = {"lessons": "auto", "reflector": True, "owner_platforms": ["api_server"], "owner_sender_ids": [],
                "mode": "auto"}
        self.assertEqual(admin.recommended_settings(done, [{"kind": "platform", "value": "api_server"}]), {})


def write_memory(home: Path, name: str, entries: list[str]) -> Path:
    path = home / "memories" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(memory_care.DELIMITER.join(entries), encoding="utf-8")
    return path


class MemoryCareTest(Base):
    FACT = "The owner deploys Atlas with patch packages and never copies dev over production."
    SIMILAR = "The owner deploys Atlas with patch packages, and never copies the dev tree over production."

    @staticmethod
    def keep(name, text):
        return {"file": name, "fingerprint": memory_care.fingerprint(text)}

    def test_review_finds_what_repeats_and_the_room_left(self):
        write_memory(self.home, "MEMORY.md", [self.FACT, "Backups live under backups/atlas-configuracoes.",
                                              self.FACT.upper(), self.SIMILAR])
        write_memory(self.home, "USER.md", ["Reports go in Brazilian Portuguese.", self.FACT])
        found = memory_care.review(self.home, {"memory": {"memory_char_limit": 6000, "user_profile_enabled": False}})
        self.assertEqual(found["files"]["MEMORY.md"]["entries"], 4)
        self.assertEqual(found["files"]["MEMORY.md"]["limit"], 6000)
        self.assertFalse(found["files"]["USER.md"]["enabled"])
        kinds = [issue["kind"] for issue in found["issues"]]
        # FACT three times (twice in MEMORY.md, case aside, once in USER.md): two repeats of the first copy (0.12.0
        # groups exact repeats in one pass, k copies make k - 1 issues)
        self.assertEqual(kinds.count("exact"), 2)
        self.assertIn("similar", kinds)
        across = next(issue for issue in found["issues"] if issue["kind"] == "exact"
                      and {entry["file"] for entry in issue["entries"]} == {"MEMORY.md", "USER.md"})
        # USER.md is off here: Hermes does not load it, so its copy is the one to take out (taking out the other
        # would hide the fact from the agent); that copy costs nothing
        self.assertEqual(across["suggested_drop"]["file"], "USER.md")
        self.assertEqual(across["suggested_keep"]["file"], "MEMORY.md")
        self.assertEqual(across["tokens_per_turn"], 0)
        self.assertGreater(found["tokens_saved_per_turn"], 0)   # the upper-case repeat inside MEMORY.md
        both = memory_care.review(self.home, {"memory": {"memory_char_limit": 6000}})
        across = next(issue for issue in both["issues"] if issue["kind"] == "exact"
                      and {entry["file"] for entry in issue["entries"]} == {"MEMORY.md", "USER.md"})
        self.assertEqual(across["suggested_drop"]["file"], "MEMORY.md")   # facts about the user stay in USER.md

    def test_remove_keeps_a_copy_logs_no_text_and_undo_puts_it_back(self):
        path = write_memory(self.home, "MEMORY.md", ["First fact.", self.FACT, "Last fact.", self.FACT])
        mark = memory_care.fingerprint(self.FACT)
        result = admin.decide(self.home, {}, {"kind": "memory", "action": "remove", "file": "MEMORY.md",
                                              "fingerprint": mark, "keep": self.keep("MEMORY.md", self.FACT)},
                              now=NOW, actor="terminal")
        self.assertTrue(result["removed"])
        self.assertEqual(memory_care.parse(path.read_text(encoding="utf-8")), ["First fact.", self.FACT, "Last fact."])
        copy = self.home / "evolution" / "memory-backups" / result["backup"]
        self.assertTrue(copy.is_file())
        if os.name != "nt":
            self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        with experience(self.home) as conn:
            row = conn.execute("SELECT * FROM decision_log WHERE id=?", (result["decision"],)).fetchone()
            listed = decisions.history(conn)
        self.assertNotIn("deploys", row["before"] + row["after"])   # the log keeps fingerprints, never memory text
        self.assertEqual(listed[0]["kind"], "memory")
        self.assertTrue(listed[0]["undoable"])
        self.assertEqual(decisions.masked(listed)[0]["kind"], "memory")
        self.assertFalse(admin.decide(self.home, {}, {"kind": "memory", "action": "remove", "file": "MEMORY.md",
                                                      "fingerprint": "0" * 16, "keep": self.keep("MEMORY.md", self.FACT)},
                                      now=NOW)["removed"])
        with self.assertRaises(ValueError):   # the copy to keep must be named
            admin.decide(self.home, {}, {"kind": "memory", "action": "remove", "file": "MEMORY.md", "fingerprint": mark},
                         now=NOW)
        undone = admin.decide(self.home, {}, {"kind": "undo", "id": result["decision"]}, now=NOW, actor="terminal")
        self.assertTrue(undone["undone"])
        self.assertEqual(memory_care.parse(path.read_text(encoding="utf-8")),
                         ["First fact.", self.FACT, "Last fact.", self.FACT])
        again = admin.decide(self.home, {}, {"kind": "undo", "id": result["decision"]}, now=NOW)
        self.assertFalse(again["undone"])

    def test_the_only_copy_left_is_never_taken_out(self):
        # F2: Hermes drops a repeat of the same text when it writes the file; the owner chose "keep 1, drop 2" before
        path = write_memory(self.home, "MEMORY.md", ["A fact.", self.FACT, self.FACT])
        issue = next(item for item in memory_care.review(self.home, {})["issues"] if item["identical"])
        path.write_text(memory_care.DELIMITER.join(["A fact.", self.FACT]), encoding="utf-8")   # Hermes' own write
        result = memory_care.remove(self.home, "MEMORY.md", issue["suggested_drop"]["fingerprint"],
                                    keep=issue["suggested_keep"], now=NOW)
        self.assertEqual(result, {"removed": False, "reason": "changed"})
        self.assertEqual(memory_care.parse(path.read_text(encoding="utf-8")), ["A fact.", self.FACT])
        # across files: the copy kept in USER.md is gone, so the one in MEMORY.md stays
        write_memory(self.home, "USER.md", ["Something else."])
        gone = memory_care.remove(self.home, "MEMORY.md", memory_care.fingerprint(self.FACT),
                                  keep=self.keep("USER.md", self.FACT), now=NOW)
        self.assertFalse(gone["removed"])
        self.assertIn(self.FACT, memory_care.parse(path.read_text(encoding="utf-8")))
        # the menu's "keep 1, drop 2" of a similar pair: entry 1 was rewritten meanwhile, entry 2 stays
        write_memory(self.home, "MEMORY.md", [self.FACT + " Reworded.", self.SIMILAR])
        kept = memory_care.remove(self.home, "MEMORY.md", memory_care.fingerprint(self.SIMILAR),
                                  keep=self.keep("MEMORY.md", self.FACT), now=NOW)
        self.assertFalse(kept["removed"])
        for bad in (None, {}, {"file": "../x", "fingerprint": "0" * 16}, {"file": "USER.md", "fingerprint": "zz"}):
            with self.assertRaises(ValueError):
                memory_care.remove(self.home, "MEMORY.md", memory_care.fingerprint(self.SIMILAR), keep=bad, now=NOW)

    def test_the_menu_names_the_copy_it_keeps(self):
        path = write_memory(self.home, "MEMORY.md", [self.FACT, "Other.", self.SIMILAR])
        calls = []
        original = memory_care.remove

        def spy(*args, **kwargs):
            calls.append(kwargs.get("keep"))
            return original(*args, **kwargs)

        script = Script("8", "1", "2", "", "0")   # issue 1, keep entry 2 (the similar one): entry 1 goes
        with patch.object(memory_care, "remove", spy):
            Menu(self.home, {}, lang="en", console=Console(read=script.read, write=script.write, width=100,
                                                            color=False), clock=lambda: NOW).run()
        self.assertEqual(calls, [self.keep("MEMORY.md", self.SIMILAR)])
        self.assertEqual(memory_care.parse(path.read_text(encoding="utf-8")), ["Other.", self.SIMILAR])

    def test_a_large_file_is_reviewed_quickly(self):
        # F3: 2,000 entries took 21 s, a 512 KB file hours (every pair normalised again); now well under a second
        entries = [f"fact number {n} about deploy alpha beta gamma {n * 7}" for n in range(2_000)]
        entries += entries[:200]   # exact repeats too
        size = sum(len(item) + 4 for item in entries)   # "\n§\n" is four bytes
        while size < memory_care.MAX_BYTES - 2_000:
            entries.append(f"another remembered fact {len(entries)} with several words in it")
            size += len(entries[-1]) + 4
        path = write_memory(self.home, "MEMORY.md", [])
        path.write_bytes(memory_care.DELIMITER.join(entries).encode("utf-8"))   # "\n" everywhere: \r\n passes the cap
        self.assertLessEqual(path.stat().st_size, memory_care.MAX_BYTES)
        start = time.monotonic()
        found = memory_care.review(self.home, {"memory": {"memory_char_limit": 10**7}})
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 3.0)   # about 0.2 s here; generous for slow CI machines, hours before
        self.assertEqual(sum(1 for item in found["issues"] if item["kind"] == "exact"), min(200, memory_care.MAX_ISSUES))
        self.assertGreaterEqual(found["issues_total"], 200)

    def test_an_unread_configuration_removes_nothing_by_itself(self):
        # F12: which file Hermes loads is unknown, so the copy Hermes loads could be the one taken out
        from hermes_atlas_evolution.evolve import CONFIG_UNREAD

        write_memory(self.home, "MEMORY.md", [self.FACT])
        write_memory(self.home, "USER.md", [self.FACT])
        self.assertEqual(memory_care.cross_file_copies(self.home, {CONFIG_UNREAD: True}), [])
        self.assertEqual(memory_care.cross_file_copies(self.home, {"memory": {"user_profile_enabled": False}}), [])
        self.assertEqual(len(memory_care.cross_file_copies(self.home, {})), 1)
        self.assertFalse(memory_care.review(self.home, {CONFIG_UNREAD: True})["config_known"])

    @unittest.skipUnless(os.name == "posix", "POSIX: named pipes and modes")
    def test_a_named_pipe_or_a_link_is_never_read_and_the_mode_is_kept(self):
        folder = self.home / "memories"
        folder.mkdir(parents=True)
        os.mkfifo(folder / "MEMORY.md")
        os.symlink("/dev/zero", folder / "USER.md")
        found = memory_care.review(self.home, {})   # would hang on the pipe, or read /dev/zero for ever
        self.assertFalse(found["files"]["MEMORY.md"]["present"])
        self.assertFalse(found["files"]["USER.md"]["present"])
        (folder / "MEMORY.md").unlink()
        path = write_memory(self.home, "MEMORY.md", [self.FACT, self.FACT])
        os.chmod(path, 0o640)
        memory_care.remove(self.home, "MEMORY.md", memory_care.fingerprint(self.FACT),
                           keep=self.keep("MEMORY.md", self.FACT), now=NOW)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_undo_never_overflows_the_limit_nor_doubles_an_entry(self):
        path = write_memory(self.home, "USER.md", ["Short.", "A second entry that takes room."])
        write_memory(self.home, "MEMORY.md", ["A second entry that takes room."])
        removed = memory_care.remove(self.home, "USER.md", memory_care.fingerprint("A second entry that takes room."),
                                     keep=self.keep("MEMORY.md", "A second entry that takes room."), now=NOW)
        tight = {"memory": {"user_char_limit": 10}}
        self.assertEqual(admin.decide(self.home, tight, {"kind": "undo", "id": removed["decision"]}, now=NOW)["reason"],
                         "no_room")
        path.write_text("Short.\n§\nA second entry that takes room.", encoding="utf-8")   # the agent wrote it again
        self.assertEqual(admin.decide(self.home, {}, {"kind": "undo", "id": removed["decision"]}, now=NOW)["reason"],
                         "changed_since")

    def test_an_undo_whose_copy_was_pruned_is_not_offered(self):
        write_memory(self.home, "MEMORY.md", [self.FACT, self.FACT])
        removed = memory_care.remove(self.home, "MEMORY.md", memory_care.fingerprint(self.FACT),
                                     keep=self.keep("MEMORY.md", self.FACT), now=NOW)
        (self.home / "evolution" / "memory-backups" / removed["backup"]).unlink()   # pruned (the newest 100 stay)
        with experience(self.home) as conn:
            self.assertFalse(decisions.history(conn)[0]["undoable"])
        self.assertFalse(admin.decide(self.home, {}, {"kind": "undo", "id": removed["decision"]}, now=NOW)["undone"])

    def test_remove_waits_for_hermes_lock(self):
        if os.name == "nt":
            self.skipTest("flock is POSIX")
        import fcntl

        path = write_memory(self.home, "MEMORY.md", [self.FACT, self.FACT])
        with open(path.with_name("MEMORY.md.lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaises(ValueError):
                with memory_care._locked(path, wait_s=0.3):
                    pass
            write_memory(self.home, "USER.md", [self.FACT])
            with self.assertRaises(ValueError):   # across files both locks are taken: USER.md's free, MEMORY.md's not
                memory_care.remove(self.home, "USER.md", memory_care.fingerprint(self.FACT),
                                   keep=self.keep("MEMORY.md", self.FACT), now=NOW)
        with memory_care._locked(path, wait_s=0.3):
            pass

    def test_unknown_files_and_names_are_refused(self):
        with self.assertRaises(ValueError):
            memory_care.remove(self.home, "../config.yaml", "0" * 16, keep={"file": "MEMORY.md", "fingerprint": "0" * 16},
                               now=NOW)
        write_memory(self.home, "MEMORY.md", [self.FACT])
        self.assertEqual(memory_care.restore(self.home, "MEMORY.md", "0" * 16, "../../config.yaml")["reason"], "no_copy")


class Script:
    """Answers typed in order; the transcript of what the menu printed."""

    def __init__(self, *answers):
        self.answers, self.out = list(answers), []

    def read(self, prompt):
        self.out.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def write(self, text):
        self.out.append(text)

    @property
    def text(self) -> str:
        return "".join(self.out)


class MenuTest(Base):
    def menu(self, *answers, lang="en", runtime=None, writer=None):
        script = Script(*answers)
        Menu(self.home, runtime or {}, lang=lang, console=Console(read=script.read, write=script.write, width=100, color=False),
             writer=writer, clock=lambda: NOW).run()
        return script.text

    def test_the_owner_decides_lessons_one_by_one_and_all_at_once(self):
        derived, draft, agent = self.lessons_waiting()
        shown = self.menu("1", "3", "a", "t", "y", "", "0")
        self.assertIn("Lessons waiting for you (3)", shown)
        self.assertIn(WRITTEN, shown)
        self.assertIn("proposed by the agent: decide it separately", shown)
        self.assertIn("Approved: the agent reads it from its next session on.", shown)
        self.assertIn("Approved 2.", shown)
        self.assertIn("Bye.", shown)
        with experience(self.home) as conn:
            status = dict(conn.execute("SELECT id, status FROM lesson").fetchall())
            actors = {row[0] for row in conn.execute("SELECT actor FROM decision_log")}
        self.assertEqual({status[derived], status[draft], status[agent]}, {"active"})
        self.assertEqual(actors, {"terminal"})

    def test_retire_and_undo_from_the_history(self):
        derived, _draft, _agent = self.lessons_waiting()
        with experience(self.home) as conn:
            decisions.decide_lesson(conn, derived, "approve", now=NOW, actor="dashboard")
        shown = self.menu("3", "1", "y", "", "4", "2", "y", "", "0")
        self.assertIn("Retired: the agent stops reading it", shown)
        self.assertIn("retired lesson #1", shown)
        self.assertIn("Undone.", shown)
        with experience(self.home) as conn:
            self.assertEqual(conn.execute("SELECT status FROM lesson WHERE id=?", (derived,)).fetchone()[0], "active")

    def test_settings_change_through_hermes_and_the_recommended_setup(self):
        written = []
        runtime = {"plugins": {"entries": {"hermes-atlas-evolution": {"settings": {}}}}}

        def writer(values):   # as `hermes config set` would: the next read of the configuration sees it
            written.append(values)
            runtime["plugins"]["entries"]["hermes-atlas-evolution"]["settings"].update(values)
            return list(values)

        shown = self.menu("6", "1", "2", "5", "3", "8", "y", "r", "y", "", "0", runtime=runtime, writer=writer)
        self.assertEqual(written[0], {"lessons": "suggest"})
        self.assertEqual(written[1], {"language": "pt"})
        self.assertEqual(written[2], {"tool_hints": True})
        self.assertIn("Applies after hermes -p default gateway restart (in the terminal, at the next session): “Add the lesson to"
                      " the failing tool result”", shown)
        # the recommended setup, after the change above, one setting at a time
        self.assertEqual(written[3:], [{"lessons": "auto"}, {"mode": "auto"}])   # the owner's choice: Auto
        with experience(self.home) as conn:
            logged = [row[0] for row in conn.execute("SELECT target FROM decision_log ORDER BY id")]
        self.assertEqual(logged, ["lessons", "language", "tool_hints", "lessons", "mode"])
        refused = self.menu("6", "4", "abc", "", "0", runtime=runtime, writer=writer)
        self.assertIn("Not done: “Learning pass every” takes a whole number, such as 10.", refused)
        out_of_range = self.menu("6", "3", "500", "", "0", runtime=runtime, writer=writer)
        self.assertIn("Not done:", out_of_range)
        self.assertEqual(len(written), 5)   # nothing written for a refused value

    def test_memory_screen_takes_out_the_copy_the_owner_chooses(self):
        path = write_memory(self.home, "MEMORY.md", [MemoryCareTest.FACT, "Other.", MemoryCareTest.SIMILAR])
        shown = self.menu("8", "1", "1", "", "0")
        self.assertIn("very similar entries", shown)
        self.assertIn("Removed. A copy of the file is in evolution/memory-backups", shown)
        self.assertEqual(memory_care.parse(path.read_text(encoding="utf-8")), [MemoryCareTest.FACT, "Other."])
        quiet = self.menu("8", "", "0")
        self.assertIn("Nothing repeats.", quiet)

    def test_portuguese_and_leaving_with_end_of_input(self):
        shown = self.menu("10", "", "42", lang="pt")
        self.assertIn("Lições esperando você (0)", shown)
        self.assertIn("Digite um número ou uma letra da lista.", shown)
        self.assertIn("Até logo.", shown)


class BareCommandTest(Base):
    def run_text(self, **arguments):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            execute(SimpleNamespace(**{"lang": "en", **arguments}), self.home, runtime_config={})
        return stream.getvalue()

    def test_without_a_terminal_the_bare_command_shows_the_status(self):
        with patch("hermes_atlas_evolution.menu.interactive", return_value=False):
            shown = self.run_text(evolution_action=None)
        self.assertIn("Atlas Evolution", shown)
        with patch("hermes_atlas_evolution.menu.interactive", return_value=False), self.assertRaises(SystemExit) as stop:
            self.run_text(evolution_action="menu")
        self.assertIn("Open the menu in your own terminal", str(stop.exception))

    def test_an_agent_shell_never_gets_the_menu(self):
        opened = []
        with patch("hermes_atlas_evolution.menu.interactive", return_value=True), \
                patch("hermes_atlas_evolution.menu.run", side_effect=lambda *a, **k: opened.append(1)), \
                patch.dict(os.environ, {"HERMES_SESSION_ID": "s1"}):
            self.run_text(evolution_action=None)
        self.assertEqual(opened, [])
        with patch("hermes_atlas_evolution.menu.interactive", return_value=True), \
                patch("hermes_atlas_evolution.menu.run", side_effect=lambda *a, **k: opened.append(1)):
            self.run_text(evolution_action="menu")
        self.assertEqual(opened, [1])

    def test_the_parser_accepts_no_command_and_menu(self):
        import argparse

        from hermes_atlas_evolution.cli import setup

        parser = argparse.ArgumentParser(prog="hermes evolution")
        setup(parser)
        self.assertIsNone(parser.parse_args([]).evolution_action)
        self.assertEqual(parser.parse_args(["menu"]).evolution_action, "menu")


if __name__ == "__main__":
    unittest.main()
