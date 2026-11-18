"""0.12.0: the cross-profile actions of the independent review (auditB), each confirmed finding as a regression:
safe reading of what the plugin did not write (F1), the copy kept named on every memory removal (F2), a review that
stays fast on a full memory file (F3, in test_menu_082), the newest decision is the only one undone (F4), the Mode
auto installs only the commit the lead runs (F6, in test_environment_085), profiles found the way Hermes finds them
(F7), install.sh reads the latest commit once, treats the end of input as "quit" and never globs (F8, F9), Windows:
launcher, read-only files, the shared lock, process trees (F10), an undo whose intent is written first (F11), a fork
never replaced and the owner and mode of a memory file kept (F12), and the per-profile opt-out `fleet_managed`."""

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_atlas_evolution import actions, admin, decisions, environment, fleet_update, proposals
from hermes_atlas_evolution.experience import experience

from test_environment_085 import NOW, Fleet, plugin
from test_panel_decisions import Base

os.environ.setdefault("ATLAS_EVOLUTION_NO_HERMES", "1")   # never the real Hermes of this machine
POSIX = unittest.skipUnless(os.name == "posix", "POSIX only: named pipes, links, process groups")
INSTALL = Path(__file__).resolve().parent.parent / "install.sh"
LOCK = {"rewrites": 75, "flipflops": 0, "window_days": 7, "review_first": "hermes curator ledger --skill x"}


def propose(home: Path, kind: str, subject: str, *, code: str | None = None, evidence=None, now=NOW) -> int:
    with experience(home) as conn:
        return conn.execute("""INSERT INTO proposal(code, kind, risk, subject, evidence, command, status, created, updated)
            VALUES (?, ?, 'medium', ?, ?, '', 'open', ?, ?)""", (code or f"{kind}:{subject}", kind, subject,
                                                                 json.dumps(evidence or {}), now, now)).lastrowid


class Hermes:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, command, **kwargs):
        self.calls.append(command[1:])
        if self.fail_on and self.fail_on in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="refused")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def alive(pid: int) -> bool:
    """Whether a process still runs. A zombie (state Z: ended, not yet reaped, as under a container's PID 1 that
    reaps nothing) has ended."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as stream:
            return stream.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True   # no /proc (macOS): kill(0) answered


# ── F1: a skill folder the owner locked, read safely ─────────────────────────────────────────────────────────────
class HeldByOwnerTest(Base):
    @POSIX
    def test_a_named_pipe_or_a_device_among_the_skills_never_hangs_the_pass(self):
        skills = self.home / "skills"
        for folder in ("evil", "zero", "good"):
            (skills / folder).mkdir(parents=True)
        os.mkfifo(skills / "evil" / "SKILL.md")
        os.symlink("/dev/zero", skills / "zero" / "SKILL.md")
        (skills / "good" / "SKILL.md").write_text("---\nname: wanted\ndescription: x\n---\nbody\n", encoding="utf-8")
        found = {}
        worker = threading.Thread(target=lambda: found.update(
            held=actions._held_by_owner(self.home, "wanted", {"good": {"locked": True}}),
            free=actions._held_by_owner(self.home, "evil", {"good": {"locked": True}})), daemon=True)
        worker.start()
        worker.join(10)
        if worker.is_alive():   # release the pipe so the suite can go on, then fail
            os.close(os.open(skills / "evil" / "SKILL.md", os.O_WRONLY | os.O_NONBLOCK))
            self.fail("the skill walk blocked on a named pipe")
        self.assertEqual(found, {"held": True, "free": False})   # the folder that declares the name counts

    @POSIX
    def test_a_manifest_that_is_a_pipe_is_not_read(self):
        folder = self.home / "plugins" / fleet_update.PLUGIN
        folder.mkdir(parents=True)
        os.mkfifo(folder / "plugin.yaml")
        row = fleet_update.inspect("default", self.home)   # would block on the pipe
        self.assertFalse(row["installed"])


# ── F7: profiles as Hermes finds them ────────────────────────────────────────────────────────────────────────────
class ProfilesTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()

    def make(self, path: Path, *, marker="config.yaml") -> Path:
        path.mkdir(parents=True, exist_ok=True)
        if marker:
            (path / marker).write_text("model: x\n", encoding="utf-8")
        return path

    @POSIX
    def test_a_default_home_linked_into_another_profiles_folder_stays_the_default(self):
        # auditB/rootof: ~/.hermes -> /srv/profiles/alice; /srv/profiles/bob must never be managed from here
        alice = self.make(self.base / "srv" / "profiles" / "alice")
        self.make(self.base / "srv" / "profiles" / "bob")
        home = self.base / "u" / ".hermes"
        home.parent.mkdir()
        home.symlink_to(alice)
        self.assertEqual(environment.root_of(home), home)
        self.assertEqual(admin.profile_name(home), "default")
        self.assertEqual([name for name, _ in environment.profiles(environment.root_of(home))], ["default"])
        self.assertEqual(admin.launch_spelling(home), (home, "default"))
        # a home that only leads into a folder named `profiles` of something that is no Hermes home: not a profile
        other = self.base / "elsewhere"
        other.symlink_to(alice)
        self.assertEqual(environment.home_parts(other), (other, "default"))
        # into a real Hermes home's profiles folder: that profile, of that root
        real = self.make(self.base / "hermes")
        work = self.make(real / "profiles" / "work")
        link = self.base / "work-link"
        link.symlink_to(work)
        self.assertEqual(environment.home_parts(link), (real, "work"))
        self.assertEqual(admin.launch_spelling(link), (real / "profiles" / "work", "work"))

    @POSIX
    def test_a_linked_profile_folder_runs_hermes_with_its_own_spelling(self):
        root = self.make(self.base / "hermes")
        data = self.make(self.base / "data" / "work")
        (root / "profiles").mkdir()
        (root / "profiles" / "work").symlink_to(data)
        calls = []

        def run(argv, **kwargs):
            calls.append((argv[1:3], kwargs["env"]["HERMES_HOME"]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        admin.run_hermes(root / "profiles" / "work", ["plugins", "list"], runner=run, executable="/x/hermes")
        # Hermes finds the profile from HERMES_HOME's spelling: the resolved /data/work would be a root of its own
        self.assertEqual(calls, [(["--profile", "work"], str(root / "profiles" / "work"))])

    def test_only_folders_hermes_lists_are_profiles(self):
        root = self.make(self.base / "hermes")
        self.make(root / "profiles" / "work")
        self.make(root / "profiles" / "default")              # never a second default
        self.make(root / "profiles" / "ghost", marker=None)   # what cron or a log rotation leaves
        self.make(root / "profiles" / "soul", marker="SOUL.md")
        self.make(root / "profiles" / "gone")
        (root / "profiles" / ".deleted").mkdir()
        (root / "profiles" / ".deleted" / "gone").write_text("deleted\n", encoding="utf-8")
        self.assertEqual([name for name, _ in environment.profiles(root)], ["default", "soul", "work"])
        self.assertTrue(environment.deleted_profile(root / "profiles" / "gone"))
        plugin(root / "profiles" / "gone", "hermes-atlas-evolution")
        with self.assertRaises(ValueError):   # a tombstoned profile is never served by the panel either
            admin.profile_home(root, "gone", read_config=lambda home: {"plugins": {"enabled": ["hermes-atlas-evolution"]}})


# ── F4: only the newest decision on a proposal is undone ─────────────────────────────────────────────────────────
class UndoOrderTest(Base):
    def test_undoing_an_older_rejection_never_reopens_what_was_refused_since(self):
        proposal = propose(self.home, "skill_lock", "hermes-admin", evidence=LOCK)
        with experience(self.home) as conn:
            first = decisions.decide_proposal(conn, proposal, "reject", now=NOW)["decision"]
            row = conn.execute("SELECT * FROM proposal WHERE id=?", (proposal,)).fetchone()
            later = NOW + proposals.REOPEN_AFTER_SECONDS + 1   # the situation is still there: the proposal reopens
            proposals._upsert(conn, set(), row["code"], row["kind"], "medium", row["subject"], LOCK, "", later)
            self.assertEqual(conn.execute("SELECT status FROM proposal WHERE id=?", (proposal,)).fetchone()[0], "open")
            second = decisions.decide_proposal(conn, proposal, "reject", now=later)["decision"]
            listed = {item["id"]: item["undoable"] for item in decisions.history(conn)}
            self.assertEqual(listed, {first: False, second: True})
            refused = decisions.undo(conn, first, now=later + 1)
            self.assertEqual((refused["undone"], refused["reason"], refused["later_decision"]),
                             (False, "changed_since", second))
            self.assertEqual(conn.execute("SELECT status FROM proposal WHERE id=?", (proposal,)).fetchone()[0], "rejected")
            self.assertIsNotNone(conn.execute("SELECT 1 FROM cursor WHERE key='auto_skip:skill_lock:hermes-admin'").fetchone())
            self.assertTrue(decisions.undo(conn, second, now=later + 2)["undone"])   # the newest one is undone
            self.assertEqual(conn.execute("SELECT status FROM proposal WHERE id=?", (proposal,)).fetchone()[0], "open")
        done = actions.automatic(self.home, now=later + 3, mode="full", runner=Hermes(), usage={},
                                 remote=lambda: None)
        self.assertEqual(len(done["applied"]), 1)   # the owner took the newest "no" back: auto may apply it now


# ── F11: the owner's "no" is written before Hermes runs ──────────────────────────────────────────────────────────
class UndoIntentTest(Base):
    def setUp(self):
        super().setUp()
        executable = patch.object(admin, "hermes_executable", return_value="/x/hermes")
        executable.start()
        self.addCleanup(executable.stop)

    def test_a_busy_database_after_hermes_undid_it_never_lets_auto_apply_it_again(self):
        proposal = propose(self.home, "skill_lock", "hermes-admin", evidence=LOCK)
        hermes = Hermes()
        applied = actions.apply_proposal(self.home, proposal, now=NOW, runner=hermes, usage={})
        broken = sqlite3.OperationalError("database is locked")
        with patch.object(decisions, "mark_undone", side_effect=broken), patch("time.sleep"), \
                self.assertRaises(sqlite3.OperationalError):
            admin.decide(self.home, {}, {"kind": "undo", "id": applied["decision"]}, now=NOW + 1, runner=hermes)
        self.assertEqual(hermes.calls[-1][-3:], ["curator", "unlock", "hermes-admin"])   # Hermes undid it
        with experience(self.home) as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM cursor WHERE key='auto_skip:skill_lock:hermes-admin'").fetchone())
            conn.execute("UPDATE proposal SET status='open' WHERE id=?", (proposal,))   # reopened, however it happens
        done = actions.automatic(self.home, now=NOW + 2 * 86_400, mode="full", runner=hermes, usage={},
                                 remote=lambda: None)
        self.assertEqual(done["applied"], [])

    def test_hermes_refusing_the_undo_leaves_the_change_and_its_record(self):
        proposal = propose(self.home, "skill_lock", "hermes-admin", evidence=LOCK)
        applied = actions.apply_proposal(self.home, proposal, now=NOW, runner=Hermes(), usage={})
        with self.assertRaises(ValueError):
            admin.decide(self.home, {}, {"kind": "undo", "id": applied["decision"]}, now=NOW + 1,
                         runner=Hermes(fail_on="unlock"))
        with experience(self.home) as conn:
            row = conn.execute("SELECT status, note FROM proposal WHERE id=?", (proposal,)).fetchone()
            self.assertEqual(tuple(row), ("applied", None))
            self.assertIsNone(conn.execute("SELECT undone FROM decision_log WHERE id=?", (applied["decision"],)).fetchone()[0])

    def test_an_undo_in_progress_counts_as_a_refusal_when_the_proposal_comes_back(self):
        code = "env:self_missing:work:hermes-atlas-evolution"
        proposal = propose(self.home, "self_missing", "work/hermes-atlas-evolution", code=code)
        with experience(self.home) as conn:
            conn.execute("UPDATE proposal SET status='applied', note='undoing' WHERE id=?", (proposal,))
            proposals._upsert(conn, set(), code, "self_missing", "low", "work/hermes-atlas-evolution", {}, "", NOW + 2 * 86_400)
            self.assertEqual(conn.execute("SELECT status FROM proposal WHERE id=?", (proposal,)).fetchone()[0], "applied")
            proposals._upsert(conn, set(), code, "self_missing", "low", "work/hermes-atlas-evolution", {}, "",
                              NOW + proposals.REOPEN_AFTER_SECONDS + 1)
            self.assertEqual(conn.execute("SELECT status FROM proposal WHERE id=?", (proposal,)).fetchone()[0], "open")


class FleetUndoTest(Fleet):
    def test_the_shared_lock_failing_runs_nothing(self):
        home = self.profile("work", enabled=["hermes-atlas-evolution"])
        leftover = home / "plugins" / ".install-zz9"
        leftover.mkdir(parents=True)
        os.utime(leftover, (NOW - 9 * 86_400, NOW - 9 * 86_400))
        item = next(found for found in self.findings() if found["kind"] == "plugin_leftover")
        result = actions.apply_proposal(self.root, self.propose(item), now=NOW)
        with patch.object(environment, "remember", side_effect=TimeoutError("busy")), self.assertRaises(TimeoutError):
            admin.decide(self.root, {}, {"kind": "undo", "id": result["decision"]}, now=NOW + 1)
        self.assertFalse(leftover.exists())   # nothing moved back: the undo can be tried again
        with experience(self.root) as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM cursor WHERE key=?", (f"auto_skip:{item['code']}",)).fetchone())
        self.assertTrue(admin.decide(self.root, {}, {"kind": "undo", "id": result["decision"]}, now=NOW + 2)["undone"])
        self.assertTrue(leftover.is_dir())

    def test_a_folder_purged_from_the_trash_is_no_longer_offered_to_undo(self):
        home = self.profile("work", enabled=["hermes-atlas-evolution"])
        leftover = home / "plugins" / ".install-zz8"
        (leftover / "pack").mkdir(parents=True)
        os.utime(leftover, (NOW - 9 * 86_400, NOW - 9 * 86_400))
        item = next(found for found in self.findings() if found["kind"] == "plugin_leftover")
        result = actions.apply_proposal(self.root, self.propose(item), now=NOW)
        kept = self.root / "evolution" / "trash" / "work" / ".install-zz8"
        os.chmod(kept / "pack", 0o555)   # git keeps its packs read-only
        old = time.time() - actions.TRASH_KEEP_SECONDS - 60
        os.utime(kept, (old, old))
        self.assertEqual(actions.empty_trash(self.root, now=time.time()), 1)
        self.assertFalse(kept.exists())
        with experience(self.root) as conn:
            self.assertFalse(next(entry for entry in decisions.history(conn) if entry["id"] == result["decision"])["undoable"])
        refused = admin.decide(self.root, {}, {"kind": "undo", "id": result["decision"]}, now=NOW + 1)
        self.assertEqual((refused["undone"], refused.get("purged")), (False, True))


# ── F10: Windows ────────────────────────────────────────────────────────────────────────────────────────────────
class WindowsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()

    def test_the_launcher_is_the_program_beside_the_shim_or_in_hermes_install(self):
        install = self.base / "hermes-agent"
        (install / "hermes_cli").mkdir(parents=True)
        (install / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        bin_dir = install / ".hermes" / "bin"
        bin_dir.mkdir(parents=True)
        shim = bin_dir / "hermes.cmd"
        shim.write_text("@echo off\n", encoding="utf-8")
        module = types.ModuleType("hermes_cli")
        module.__file__ = str(install / "hermes_cli" / "__init__.py")
        with patch.object(admin, "_WINDOWS", True), patch("shutil.which", return_value=str(shim)), \
                patch.dict(sys.modules, {"hermes_cli": module}), patch.object(sys, "argv", ["python"]):
            self.assertEqual(admin.hermes_executable(), str(shim))   # only the shim: used, with strict quoting
            (install / "venv" / "Scripts").mkdir(parents=True)
            (install / "venv" / "Scripts" / "hermes.exe").write_bytes(b"MZ")
            self.assertEqual(admin.hermes_executable(), str(install / "venv" / "Scripts" / "hermes.exe"))
            (bin_dir / "hermes.exe").write_bytes(b"MZ")
            self.assertEqual(admin.hermes_executable(), str(bin_dir / "hermes.exe"))   # beside the shim PATH names

    def test_a_batch_launcher_gets_every_argument_quoted_or_refused(self):
        line = admin._batch_command_line([r"C:\h\hermes.cmd", "--profile", "work", "cron", "create", "0 8 * * 1"])
        self.assertEqual(line, r'"C:\h\hermes.cmd" "--profile" "work" "cron" "create" "0 8 * * 1"')
        for bad in ('["api_server"]', "50%", "a&calc", "x|y", "a^b", "line\nbreak", "ends\\"):
            with self.assertRaises(ValueError, msg=bad):
                admin._batch_command_line(["hermes.cmd", "config", "set", "key", bad])

    def test_a_read_only_tree_is_deleted_from_the_trash(self):
        pack = self.base / "trash" / ".install-ro" / ".git" / "objects" / "pack"
        pack.mkdir(parents=True)
        (pack / "pack-1.pack").write_bytes(b"x")
        os.chmod(pack / "pack-1.pack", 0o444)
        os.chmod(pack, 0o555)
        old = NOW - actions.TRASH_KEEP_SECONDS - 60
        os.utime(self.base / "trash" / ".install-ro", (old, old))
        self.assertEqual(actions._purge(self.base / "trash", now=NOW), 1)
        self.assertFalse((self.base / "trash" / ".install-ro").exists())

    @POSIX
    def test_making_a_file_writable_again_never_follows_a_link(self):
        outside = self.base / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        os.chmod(outside, 0o444)
        link = self.base / "link"
        link.symlink_to(outside)
        actions._writable_again(os.unlink, str(link), None)
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o444)   # the target's mode is untouched

    def test_the_shared_state_is_locked_without_fcntl(self):
        state = {"held": 0, "calls": []}

        class FakeMsvcrt:
            LK_NBLCK, LK_UNLCK = 2, 0

            @staticmethod
            def locking(fd, mode, size):
                state["calls"].append(mode)
                if mode == FakeMsvcrt.LK_NBLCK:
                    if state["held"]:
                        raise OSError(13, "locked")
                    state["held"] = 1
                else:
                    state["held"] = 0

        folder = self.base / "evolution"
        folder.mkdir()
        with patch.dict(sys.modules, {"fcntl": None, "msvcrt": FakeMsvcrt}):
            with environment.exclusive_lock(folder / ".fleet-state.lock", wait_s=1):
                with self.assertRaises(TimeoutError):
                    with environment.exclusive_lock(folder / ".fleet-state.lock", wait_s=0.2):
                        pass
            environment.remember(self.base, seen=["work"])
        self.assertEqual(state["held"], 0)
        self.assertIn(FakeMsvcrt.LK_UNLCK, state["calls"])
        self.assertEqual(environment.state(self.base)["seen"], ["work"])

    @POSIX
    def test_a_timeout_ends_everything_the_command_started(self):
        marker = self.base / "child.pid"
        script = f"sleep 60 & echo $! > '{marker}'; wait"
        with self.assertRaises(subprocess.TimeoutExpired):
            admin.run_command(["/bin/sh", "-c", script], env=dict(os.environ), cwd=str(self.base), timeout=1)
        child = int(marker.read_text().strip())
        for _ in range(50):
            if not alive(child):
                break
            time.sleep(0.1)
        else:
            os.kill(child, 9)
            self.fail("the command's own child outlived the timeout")
        done = admin.run_command(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], env=dict(os.environ),
                                 cwd=str(self.base), timeout=10)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "out\n", "err\n"))


# ── F12: a fork's checkout is never replaced; the opt-out of the fleet ──────────────────────────────────────────
class ForkTest(Fleet):
    def checkout(self, home: Path, repo: str, *, pinned: bool = False) -> None:
        plugin(home, "hermes-atlas-evolution", repo=repo)
        if pinned:
            (home / "plugins" / ".install-metadata.json").write_text(json.dumps(
                {"hermes-atlas-evolution": {"pinned": True, "revision": "c" * 40}}), encoding="utf-8")

    def test_a_failed_update_is_installed_again_only_over_this_repository(self):
        ours = self.profile("ours", enabled=["hermes-atlas-evolution"])
        fork = self.profile("fork", enabled=["hermes-atlas-evolution"])
        self.checkout(ours, "https://github.com/sesantanajr/hermes-atlas-evolution.git")
        self.checkout(fork, "git@github.com:someone/hermes-atlas-evolution.git")
        rows = {row["profile"]: row for row in fleet_update.plan(self.root)}
        self.assertEqual((rows["ours"]["origin"], rows["fork"]["origin"]), ("self", "other"))
        hermes = Hermes(fail_on="update")
        done = fleet_update.run([rows["ours"], rows["fork"]], runner=hermes, executable="/x/hermes", remote=lambda: None)
        commands = [call[2:4] for call in hermes.calls]
        self.assertEqual(commands, [["plugins", "update"], ["plugins", "install"], ["plugins", "update"]])
        self.assertEqual(done[1]["commands"][-1]["after"], "not_ours")

    def test_a_pinned_fork_is_left_as_it_is(self):
        fork = self.profile("fork", enabled=["hermes-atlas-evolution"])
        self.checkout(fork, "https://github.com/someone/hermes-atlas-evolution", pinned=True)
        row = fleet_update.inspect("fork", fork)
        self.assertIsNone(fleet_update.step(row, install_missing=False, target="d" * 40))
        done = fleet_update.run([row], runner=Hermes(), executable="/x/hermes", target="d" * 40)
        self.assertEqual((done[0]["status"], done[0]["reason"]), ("skipped", "fork"))

    def test_a_profile_that_opted_out_is_never_changed_by_itself(self):
        self.checkout(self.root, "https://github.com/sesantanajr/hermes-atlas-evolution")
        self.profile("fresh", entries={"hermes-atlas-evolution": {"settings": {"fleet_managed": False}}})
        other = self.profile("other")
        (other / "evolution").mkdir()
        (other / "evolution" / "settings.json").write_text(json.dumps({"evolution": {"fleet_managed": False}}),
                                                           encoding="utf-8")
        self.assertFalse(environment.fleet_managed(self.root / "profiles" / "fresh"))
        self.assertFalse(environment.fleet_managed(other))
        self.assertTrue(environment.fleet_managed(self.root))
        for item in self.findings():
            if item["kind"] == "self_missing":
                self.propose(item)
        calls = []
        with patch.object(admin, "hermes_executable", return_value="/x/hermes"), \
                patch("hermes_atlas_evolution.admin.run_command", lambda argv, **k: calls.append(argv)), \
                patch("hermes_atlas_evolution.fleet_update.auto_sync", lambda *a, **k: None):
            done = actions.automatic(self.root, now=NOW, mode="full", usage={}, remote=lambda: "a" * 40)
        self.assertEqual((done["applied"], calls), ([], []))
        lead = fleet_update.inspect("default", self.root)
        synced = self.profile("synced", enabled=["hermes-atlas-evolution"],
                              entries={"hermes-atlas-evolution": {"settings": {"fleet_managed": "false"}}})
        self.checkout(synced, "https://github.com/sesantanajr/hermes-atlas-evolution", pinned=True)
        due = fleet_update.due([fleet_update.inspect("synced", synced)], lead, "a" * 40, {}, now=NOW,
                               mode_of=lambda home: "auto")
        self.assertEqual(due, [])


    def test_a_profile_whose_configuration_cannot_be_read_is_left_alone_this_pass(self):
        # review of 0.12.0: an unreadable config.yaml (invalid while edited, over 8 MiB) read as "plugin not on there"
        # let the Mode auto act in a profile whose `fleet_managed: false` it could not see
        work = self.profile("work", enabled=["hermes-atlas-evolution"],
                            entries={"hermes-atlas-evolution": {"settings": {"fleet_managed": False}}})
        plugin(work, "hermes-atlas-evolution")
        leftover = work / "plugins" / ".install-zz7"
        leftover.mkdir()
        os.utime(leftover, (NOW - 9 * 86_400, NOW - 9 * 86_400))
        item = next(found for found in self.findings() if found["kind"] == "plugin_leftover")
        self.propose(item)
        self.configs[str(work.resolve())] = None   # config.yaml cannot be read right now
        self.assertFalse(environment.fleet_managed(work))
        self.assertFalse(actions._target_allows(self.root, item["subject"]))
        with patch("hermes_atlas_evolution.fleet_update.auto_sync", lambda *a, **k: None):
            done = actions.automatic(self.root, now=NOW, mode="full", usage={}, remote=lambda: None)
        self.assertEqual(done["applied"], [])
        self.assertTrue(leftover.is_dir())   # nothing moved to the trash
        self.configs[str(work.resolve())] = {"plugins": {"enabled": ["hermes-atlas-evolution"]}}   # readable, no opt-out
        self.assertTrue(environment.fleet_managed(work))


class YoungProfileTest(Fleet):
    def test_a_profile_made_minutes_ago_is_not_unused(self):
        # lab on Hermes 0.23.2: `hermes profile create second` showed "Profile unused for 30 days · second" at once
        from hermes_atlas_evolution import ecosystem

        second = self.profile("second")
        (second / ".env").write_text("", encoding="utf-8")
        year_ago = time.time() - 365 * 86_400
        os.utime(second / "config.yaml", (year_ago, year_ago))   # a clone copies config.yaml with its old date

        def kinds(now):
            reading = environment.scan(self.root, now=now, bundled=self.bundled / "plugins", sessions=lambda home, since: 0)
            return {item["subject"] for item in environment.findings(reading, {"entries": {}, "removed": []},
                                                                     ecosystem.knowledge(), now=now)
                    if item["kind"] == "profile_idle"}

        self.assertEqual(kinds(time.time()), set())
        self.assertEqual(kinds(time.time() + 31 * 86_400), {"second"})   # a month on, still without a session
        self.assertIsNone(environment._created(self.root / "profiles" / "missing"))


# ── F8, F9, F12: install.sh ─────────────────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "POSIX sh")
class InstallScriptTest(unittest.TestCase):
    SHELLS = [name for name in ("sh", "dash") if shutil.which(name)]

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log, self.git_log = self.base / "hermes.log", self.base / "git.log"
        self.fake("hermes", f'printf "%s|%s\\n" "$HERMES_HOME" "$*" >> "{self.log}"\n'
                            'case "$*" in *"$FAIL_ON"*) [ -n "$FAIL_ON" ] && exit 1 ;; esac\nexit 0\n')
        self.fake("git", f'[ "$1" = ls-remote ] || exit 1\necho call >> "{self.git_log}"\n'
                         '[ -n "${GIT_DOWN:-}" ] && exit 2\nprintf "%s\\tHEAD\\n" "$(printf e%.0s $(seq 40))"\n')
        self.fake("systemctl", "exit 0\n")   # never this machine's
        self.root = self.base / "home" / ".hermes"
        self.profile(self.root)

    def fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def profile(self, home: Path, *, installed=True, record=None, origin="https://github.com/sesantanajr/hermes-atlas-evolution.git"):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("model: x\n", encoding="utf-8")
        if installed:
            plugin(home, "hermes-atlas-evolution", repo=origin)
        if record is not None:
            (home / "plugins").mkdir(parents=True, exist_ok=True)
            (home / "plugins" / ".install-metadata.json").write_text(json.dumps(record), encoding="utf-8")
        return home

    def run_script(self, *args, shell="sh", script=None, env=None, cwd=None, stdin=subprocess.DEVNULL):
        environ = {"PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin", "HOME": str(self.base / "home"), "FAIL_ON": "",
                   **(env or {})}
        return subprocess.run([shell, str(script or INSTALL), *args], env=environ, cwd=str(cwd or self.base),
                              stdin=stdin, capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def test_the_latest_commit_is_read_once_and_never_passed_empty(self):
        pin = {"hermes-atlas-evolution": {"pinned": True, "revision": "c" * 40}}
        for name in ("work", "lab"):
            self.profile(self.root / "profiles" / name, record=pin)
        for shell in self.SHELLS:
            for path in (self.log, self.git_log):
                path.unlink(missing_ok=True)
            done = self.run_script("--all", "--yes", shell=shell)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(len(self.git_log.read_text().splitlines()), 1, shell)   # once for every profile
            refs = [line.rsplit("--ref ", 1)[1] for line in self.calls() if "--ref" in line]
            self.assertEqual(refs, ["e" * 40, "e" * 40], shell)
            self.log.unlink(missing_ok=True)
            down = self.run_script("--all", "--yes", shell=shell, env={"GIT_DOWN": "1"})
            self.assertNotEqual(down.returncode, 0)
            self.assertFalse([line for line in self.calls() if "--ref" in line], shell)   # never `--ref ""`

    def test_profiles_are_the_ones_hermes_lists_and_a_name_is_never_a_file_pattern(self):
        self.profile(self.root / "profiles" / "work")
        self.profile(self.root / "profiles" / "default")
        (self.root / "profiles" / "ghost").mkdir()   # no identity file
        self.profile(self.root / "profiles" / "gone")
        (self.root / "profiles" / ".deleted").mkdir()
        (self.root / "profiles" / ".deleted" / "gone").write_text("deleted\n", encoding="utf-8")
        done = self.run_script("--all", "--yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([line.split("|")[0] for line in self.calls()], [str(self.root), str(self.root / "profiles" / "work")])
        here = self.base / "cwd"
        here.mkdir()
        (here / "default").write_text("", encoding="utf-8")   # `*` would have globbed to this name
        self.log.unlink()
        star = self.run_script("--profiles", "*", "--yes", cwd=here)
        self.assertEqual(star.returncode, 2)
        self.assertIn("Unknown profile: *", star.stderr)
        self.assertEqual(self.calls(), [])

    def test_without_a_terminal_it_asks_to_choose_and_says_so(self):
        done = self.run_script(env={"FAIL_ON": ""})
        self.assertEqual(done.returncode, 2)
        self.assertIn("Choose the profiles", done.stderr)
        self.assertIn("without a terminal it stops", self.run_script("--help").stdout)
        self.assertEqual(self.calls(), [])

    def test_the_end_of_input_at_the_question_changes_nothing(self):
        import pty

        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(["sh", str(INSTALL)], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env={"PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin",
                                            "HOME": str(self.base / "home"), "FAIL_ON": ""}, text=True)
            time.sleep(0.5)
            os.write(master, b"\x04")   # Ctrl-D: the end of input, not "Enter = all"
            out, err = process.communicate(timeout=30)
        finally:
            os.close(master)
            os.close(slave)
        self.assertEqual(process.returncode, 0, err)
        self.assertIn("Nothing changed.", out)
        self.assertEqual(self.calls(), [])

    def test_the_record_is_this_plugins_own_and_a_fork_is_never_replaced(self):
        record = {"another-plugin": {"pinned": True, "revision": "f" * 40,
                                     "source": "https://github.com/x/hermes-atlas-evolution-tools"},
                  "hermes-atlas-evolution": {"pinned": False, "source": "sesantanajr/hermes-atlas-evolution"}}
        self.profile(self.root / "profiles" / "work", record=record)
        self.profile(self.root / "profiles" / "fork", origin="git@github.com:someone/hermes-atlas-evolution-fork.git")
        done = self.run_script("--all", "--yes", env={"FAIL_ON": "plugins update"})
        self.assertNotEqual(done.returncode, 0)
        lines = self.calls()
        work = [line.split("|", 1)[1] for line in lines if line.startswith(str(self.root / "profiles" / "work") + "|")]
        fork = [line.split("|", 1)[1] for line in lines if line.startswith(str(self.root / "profiles" / "fork") + "|")]
        self.assertIn("(installed)", done.stdout)   # another plugin's pin is not this one's
        self.assertEqual(len(work), 2)
        self.assertIn("plugins install https://github.com/sesantanajr/hermes-atlas-evolution.git --force", work[1])
        self.assertEqual(len(fork), 1)   # the update only: never the upstream installed over a fork
        self.assertIn("does not come from", done.stderr)

    def test_a_download_cut_short_runs_nothing(self):
        text = INSTALL.read_text(encoding="utf-8")
        cut = self.base / "cut.sh"
        cut.write_text(text[:text.rindex('main "$@"')], encoding="utf-8")
        done = self.run_script("--all", "--yes", script=cut)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue(text.rstrip().endswith('main "$@"'))


if __name__ == "__main__":
    unittest.main()
