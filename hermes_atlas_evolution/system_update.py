"""Keep the operating system up to date without breaking it or Hermes. Linux with apt (Ubuntu, Debian).

Three steps, each safe on its own:
- `check` reads, never changes and never takes a lock: the updates waiting (security ones apart), a reboot the
  system waits for, a package manager left half-way (`dpkg --audit`), apt's own consistency check (without its lock),
  a package manager working right now (a dpkg or apt lock held, read from /proc/locks, or an apt, dpkg or
  unattended-upgrade run), packages held (and the ones a run of this plugin held and could not release), room on
  /, /boot and /var, automatic security updates, and whether Hermes comes back after a reboot. While a package
  manager works, what it leaves half-way for a moment is not reported. The learning pass runs it once a day.
- `plan` refreshes the package lists (`apt-get update`) and simulates the upgrade (`apt-get -s`), flagging packages
  that restart the containers, need a reboot, touch remote access, the boot loader or the package manager.
- `upgrade` and `repair` are the owner's, as root in their own terminal: never the agent, never by themselves, and
  only for a Hermes that belongs to root (a profile or plugin another user can write would let that user, the
  agent included, steer what root runs: there the owner runs apt themselves). Their files (the list of every
  package with its version, a copy of /etc, the script, its log and report) live in a root-only folder outside every
  Hermes profile, `/var/lib/hermes-atlas-evolution/system/<time>-<random>/` (never in a profile's backups; the latest
  5 runs are kept), each file created new (never through a link). The run happens in its own systemd unit, so a
  closed terminal, a dropped SSH connection or a restarted gateway never leaves dpkg half-way; it keeps every
  configuration file the owner changed, restarts no service by itself, and holds the packages the owner left out
  only for that run (released on exit and on SIGTERM, SIGINT or SIGHUP; a run killed outright is found by `check`
  and its holds released by `repair`). After: dpkg, apt, every service that was running (a service that ended
  normally is not a failure) and Hermes are checked again, and the report lists what changed and how to go back.
Nothing here reboots the machine: the report says when a reboot is due.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path

CHECK_EVERY_SECONDS = 86_400
STATE_DIR = "/var/lib/hermes-atlas-evolution/system"
RUNS_KEPT = 5
BUSY_STUCK_SECONDS = 6 * 3600
# Packages worth a word before an upgrade: (what happens, the packages, as prefixes)
ATTENTION = (
    ("restarts_containers", ("docker.io", "docker-ce", "containerd", "containerd.io", "runc", "podman")),
    ("needs_reboot", ("libc6", "systemd", "linux-image", "linux-generic", "linux-modules", "dbus", "libssl", "openssl")),
    ("restarts_web", ("nginx", "apache2", "caddy", "haproxy")),
    ("remote_access", ("openssh-server", "sudo", "pam")),
    ("boot", ("grub", "initramfs-tools", "shim-signed")),
    ("package_manager", ("apt", "dpkg", "libapt-pkg")),
)
# A run of these is a package manager at work (packagekitd idles for minutes after each `apt-get update` and holds
# nothing: only the locks it takes count, and /proc/locks shows them).
BUSY_PROCESSES = ("apt", "apt-get", "aptitude", "dpkg")
LOCK_FILES = ("/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock", "/var/lib/apt/lists/lock",
              "/var/cache/apt/archives/lock")
MIN_FREE = {"/": 1_500_000_000, "/boot": 150_000_000, "/var": 1_000_000_000}
PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9.+-]{0,99}(?::[a-z0-9][a-z0-9-]{0,31})?")
_ENV = {"LC_ALL": "C", "LANG": "C", "DEBIAN_FRONTEND": "noninteractive"}


class Host:
    """The machine the checks read: commands and files. Tests replace it."""

    def __init__(self, root: Path = Path("/"), runner=None):
        self.root = Path(root)
        self.runner = runner

    def run(self, argv: list, *, timeout: int = 60) -> tuple[int, str, str]:
        if self.runner is not None:
            return self.runner(list(argv), timeout)
        try:
            done = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                                  check=False, stdin=subprocess.DEVNULL, env={**os.environ, **_ENV})
        except FileNotFoundError:
            return 127, "", f"{argv[0]}: not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"{argv[0]}: timed out"
        except OSError as error:
            return 126, "", f"{argv[0]}: {type(error).__name__}"
        return done.returncode, done.stdout or "", done.stderr or ""

    def path(self, name: str) -> Path:
        return self.root / name.lstrip("/")

    def read(self, name: str, limit: int = 65_536) -> str | None:
        try:
            with self.path(name).open("r", encoding="utf-8", errors="replace") as stream:
                return stream.read(limit)
        except OSError:
            return None

    def which(self, name: str) -> bool:
        if self.runner is not None:   # a test host answers for its own commands
            return self.run(["which", name])[0] == 0
        return shutil.which(name) is not None

    def root_user(self) -> bool:
        if self.runner is not None:
            return self.run(["id", "-u"])[1].strip() == "0"
        return hasattr(os, "geteuid") and os.geteuid() == 0

    def root_owned(self, path: Path) -> bool:
        """`path` and every folder above it belong to root and no one else can write them."""
        if self.runner is not None:
            return self.run(["root-owned", str(path)])[0] == 0
        def chain(start: Path) -> bool:
            for part in [start, *start.parents]:
                info = os.lstat(part)
                if info.st_uid != 0:
                    return False
                if stat.S_ISLNK(info.st_mode):
                    continue   # a link's own mode means nothing: only its owner (root) can change it
                if info.st_mode & 0o022 and not (part == Path("/tmp") or info.st_mode & 0o1000):
                    return False
            return True

        try:
            return chain(Path(path)) and chain(Path(path).resolve())
        except (OSError, RuntimeError):
            return False

    def free(self, mount: str) -> int | None:
        try:
            return shutil.disk_usage(self.path(mount)).free
        except OSError:
            return None

    def processes(self) -> list[str]:
        names = []
        try:
            entries = list(self.path("/proc").iterdir())
        except OSError:
            return names
        for entry in entries:
            if entry.name.isdigit():
                try:
                    stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
                    if stat.rsplit(")", 1)[1].split()[0] in ("Z", "X"):
                        continue   # a dead process nobody reaped yet runs nothing
                    name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
                    if name == "unattended-upgr":
                        # unattended-upgrades keeps `unattended-upgrade-shutdown --wait-for-signal` running for good: it
                        # waits for a shutdown and holds no lock. Only the upgrade run itself is busy.
                        line = (entry / "cmdline").read_bytes()[:4096].replace(b"\0", b" ")
                        if b"unattended-upgrade-shutdown" in line:
                            continue
                    names.append(name)
                except (OSError, IndexError):
                    continue
        return names

    def held_locks(self) -> list[str]:
        """The package manager's lock files someone holds now, from /proc/locks (nothing is taken to find out)."""
        wanted = {}
        for name in LOCK_FILES:
            try:
                info = os.stat(self.path(name))
            except OSError:
                continue
            wanted[(os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)] = name
        held = []
        for line in (self.read("/proc/locks", 1_000_000) or "").splitlines():
            parts = line.split()
            if len(parts) < 6 or parts[1] in ("->",):
                continue
            try:
                major, minor, inode = parts[5].split(":")
                key = (int(major, 16), int(minor, 16), int(inode))
            except ValueError:
                continue
            if key in wanted:
                held.append(wanted[key])
        return sorted(set(held))

    def _age(self, pid: int) -> float | None:
        try:
            started = int((self.path(f"/proc/{pid}") / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19])
            uptime = float((self.read("/proc/uptime") or "").split()[0])
            return uptime - started / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError, AttributeError):
            return None

    def busy_age(self) -> float | None:
        """Seconds the oldest package manager process, or holder of a dpkg or apt lock, has been running."""
        pids = set()
        try:
            entries = [entry for entry in self.path("/proc").iterdir() if entry.name.isdigit()]
        except OSError:
            entries = []
        for entry in entries:
            try:
                if (entry / "comm").read_text(encoding="utf-8", errors="replace").strip() in BUSY_PROCESSES:
                    pids.add(int(entry.name))
            except OSError:
                continue
        wanted = set()
        for name in LOCK_FILES:
            try:
                info = os.stat(self.path(name))
                wanted.add((os.major(info.st_dev), os.minor(info.st_dev), info.st_ino))
            except (OSError, AttributeError):
                continue
        for line in (self.read("/proc/locks", 1_000_000) or "").splitlines():
            parts = line.split()
            try:
                major, minor, inode = parts[5].split(":")
                if (int(major, 16), int(minor, 16), int(inode)) in wanted:
                    pids.add(int(parts[4]))
            except (ValueError, IndexError):
                continue
        ages = [age for age in (self._age(pid) for pid in pids) if age is not None]
        return max(ages) if ages else None


def os_release(host: Host) -> dict:
    found = {}
    for line in (host.read("/etc/os-release") or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            found[key.strip()] = value.strip().strip('"')
    return {"name": found.get("PRETTY_NAME") or found.get("NAME") or "unknown", "id": found.get("ID", ""),
            "version": found.get("VERSION_ID", ""), "like": found.get("ID_LIKE", "")}


def supported(host: Host) -> bool:
    release = os_release(host)
    family = f"{release['id']} {release['like']}"
    return ("debian" in family or "ubuntu" in family) and host.which("apt-get") and host.which("dpkg")


_UPGRADABLE = re.compile(r"^(?P<name>[^/\s]+)/(?P<suite>\S+)\s+(?P<new>\S+)\s+\S+\s+\[upgradable from: (?P<old>[^\]]+)\]")


def upgradable(host: Host) -> list[dict]:
    """What the package lists already know can be upgraded (no refresh: that is `plan`'s)."""
    code, out, _err = host.run(["apt", "list", "--upgradable"], timeout=90)
    found = []
    if code != 0:
        return found
    for line in out.splitlines():
        match = _UPGRADABLE.match(line.strip())
        if match:
            suites = match["suite"].split(",")
            found.append({"name": match["name"], "from": match["old"], "to": match["new"],
                          "security": any(suite.endswith("-security") or "security" in suite for suite in suites)})
    return found


def attention_of(name: str) -> list[str]:
    return [code for code, prefixes in ATTENTION if any(name == p or name.startswith(p) for p in prefixes)]


def _user_units(host: Host) -> list[dict]:
    """Hermes' systemd units, system and user ones, and whether each comes back after a reboot."""
    units = []
    for scope, prefix in (("system", []), ("user", ["--user"])):
        code, out, _err = host.run(["systemctl", *prefix, "list-unit-files", "--no-legend", "--plain", "hermes*"],
                                   timeout=20)
        if code not in (0, 1):
            continue
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2 or not parts[0].endswith(".service") or "@" in parts[0] \
                    or parts[0].startswith("hermes-evolution-os-") or parts[1] in ("transient", "generated"):
                continue   # this plugin's own runs and units systemd made up are not Hermes' services
            unit, state = parts[0], parts[1]
            _code, active, _err = host.run(["systemctl", *prefix, "is-active", unit], timeout=10)
            units.append({"unit": unit, "scope": scope, "enabled": state in ("enabled", "enabled-runtime", "static",
                                                                             "alias", "indirect", "linked",
                                                                             "linked-runtime"),
                          "state": state, "active": active.strip() == "active"})
    return units


def running_services(host: Host) -> list[str]:
    """Every service running now (system and the user's), to see after an upgrade which ones stopped."""
    found = []
    for prefix in ([], ["--user"]):
        code, out, _err = host.run(["systemctl", *prefix, "list-units", "--type=service", "--state=running",
                                    "--no-legend", "--plain"], timeout=20)
        if code == 0:
            found += [("user:" if prefix else "") + line.split()[0] for line in out.splitlines() if line.split()]
    return sorted(set(found))


def failed_services(host: Host) -> list[str]:
    code, out, _err = host.run(["systemctl", "list-units", "--type=service", "--state=failed", "--no-legend", "--plain"],
                               timeout=20)
    return sorted({line.split()[0] for line in out.splitlines() if line.split()}) if code == 0 else []


def owner_of(home: Path) -> str | None:
    """The user a Hermes profile belongs to (whose linger a user unit needs)."""
    try:
        import pwd

        return pwd.getpwuid(os.stat(home).st_uid).pw_name
    except (ImportError, KeyError, OSError):
        return None


def _linger(host: Host, user: str | None = None) -> bool | None:
    user = user or os.environ.get("SUDO_USER") or os.environ.get("USER") or ("root" if host.root_user() else None)
    if not user:
        return None
    if host.path(f"/var/lib/systemd/linger/{user}").exists():
        return True
    code, out, _err = host.run(["loginctl", "show-user", user, "-p", "Linger"], timeout=10)
    if code != 0:
        return None
    return out.strip().endswith("=yes")


def _unattended(host: Host) -> dict:
    # apt's own reading of every file under apt.conf.d (02periodic, 20auto-upgrades, ...), the files when it is absent
    code, out, _err = host.run(["apt-config", "dump", "APT::Periodic::Unattended-Upgrade"], timeout=10)
    if code == 0 and out.strip():
        text = out
    else:
        text = " ".join(filter(None, (host.read("/etc/apt/apt.conf.d/20auto-upgrades"),
                                      host.read("/etc/apt/apt.conf.d/10periodic"),
                                      host.read("/etc/apt/apt.conf.d/02periodic"))))
    on = bool(re.search(r'APT::Periodic::Unattended-Upgrade\s+"(?!0)\d+"', text))
    installed = host.path("/usr/bin/unattended-upgrade").exists() or host.path("/usr/bin/unattended-upgrades").exists()
    log = host.path("/var/log/unattended-upgrades/unattended-upgrades.log")
    try:
        last = log.stat().st_mtime
    except OSError:
        last = None
    return {"installed": installed, "enabled": on and installed, "last_run": last}


def _last_refresh(host: Host) -> float | None:
    """When the package lists were last refreshed: the newest Release file apt fetched (any `apt-get update`, by hand
    or by the timer), or the timer's stamp."""
    times = []
    try:
        lists = host.path("/var/lib/apt/lists")
        for found, entry in enumerate(lists.iterdir()):
            if found > 2000:
                break
            if entry.name.endswith(("_InRelease", "_Release")):
                times.append(entry.stat().st_mtime)
    except OSError:
        pass
    for name in ("/var/lib/apt/periodic/update-success-stamp", "/var/lib/apt/lists/partial"):
        try:
            times.append(host.path(name).stat().st_mtime)
        except OSError:
            continue
    return max(times) if times else None


def native_arch(host: Host) -> str:
    code, out, _err = host.run(["dpkg", "--print-architecture"], timeout=10)
    return out.strip() if code == 0 else ""


def bare(name: str, native: str) -> str:
    """A package name as `apt-mark showhold` prints it: without the machine's own architecture (or `all`)."""
    base, _, arch = name.partition(":")
    return base if arch and arch in (native, "all") else name


def plugin_holds(host: Host) -> list[str]:
    """Packages a run of this plugin held and did not release (it was killed outright)."""
    found = []
    base = host.path(STATE_DIR)
    try:
        runs = sorted(base.iterdir())
    except OSError:
        return []
    for run in runs:
        holds = run / "holds"
        if holds.is_file() and not holds.is_symlink():
            found += _names(holds)
    return sorted(set(found))


def _names(holds: Path) -> list[str]:
    try:
        return [name for name in holds.read_text(encoding="utf-8").split() if PACKAGE_NAME.fullmatch(name)]
    except OSError:
        return []


def check(host: Host | None = None, *, now: float | None = None, home: Path | None = None) -> dict:
    """What stands between this machine and a safe upgrade, and what is waiting. Reads only; takes no lock."""
    host = host or Host()
    now = time.time() if now is None else now
    report: dict = {"at": now, "supported": supported(host), "os": os_release(host)}
    code, kernel, _err = host.run(["uname", "-r"], timeout=10)
    report["kernel"] = kernel.strip() if code == 0 else None
    if not report["supported"]:
        report["findings"] = ["unsupported"]
        report["ready"] = False
        return report
    pending = upgradable(host)
    report["upgradable"] = pending
    report["security"] = sum(item["security"] for item in pending)
    reboot = host.read("/var/run/reboot-required") is not None or host.path("/run/reboot-required").exists()
    report["reboot_required"] = reboot
    report["reboot_packages"] = sorted(set((host.read("/var/run/reboot-required.pkgs") or "").split()))[:20] if reboot else []
    locks = host.held_locks()
    busy = sorted({name for name in host.processes() if name in BUSY_PROCESSES} | {f"lock:{name}" for name in locks})
    report["busy"] = busy
    code, audit, _err = host.run(["dpkg", "--audit"], timeout=60)
    report["dpkg_audit"] = audit.strip().splitlines()[:20]
    report["dpkg_ok"] = code == 0 and not audit.strip()
    report["root"] = host.root_user()
    # Without apt's lock (Debug::NoLocking): a check never makes unattended-upgrades or the owner's apt wait.
    code, out, err = host.run(["apt-get", "-o", "Debug::NoLocking=1", "check"], timeout=120)
    report["apt_ok"] = code == 0
    report["apt_error"] = None if code == 0 else " ".join((err or out).split())[:200]
    code, held, _err = host.run(["apt-mark", "showhold"], timeout=30)
    report["held"] = held.split() if code == 0 else []
    native = native_arch(host)
    held_now = {bare(name, native) for name in report["held"]}
    report["plugin_holds"] = [name for name in plugin_holds(host) if bare(name, native) in held_now]
    report["disk"] = {mount: host.free(mount) for mount in MIN_FREE}
    report["unattended"] = _unattended(host)
    report["last_refresh"] = _last_refresh(host)
    report["units"] = _user_units(host)
    report["running"] = running_services(host)
    report["failed"] = failed_services(host)
    report["owner"] = owner_of(home) if home is not None else None
    report["linger"] = _linger(host, report["owner"])
    findings = []
    if busy:
        findings.append("package_manager_busy")   # what it leaves half-way for a moment is not a finding
        report["busy_seconds"] = host.busy_age()
        if (report["busy_seconds"] or 0) > BUSY_STUCK_SECONDS:
            findings.append("package_manager_stuck")   # an apt left at its question hides everything else
    else:
        if not report["dpkg_ok"]:
            findings.append("dpkg_interrupted")
        if report["apt_ok"] is False:
            findings.append("apt_broken")
    if report["plugin_holds"]:
        findings.append("held_by_plugin")
    low = [mount for mount, free in report["disk"].items() if free is not None and free < MIN_FREE[mount]]
    if low:
        findings.append("disk_low")
        report["disk_low"] = low
    if reboot:
        findings.append("reboot_required")
    if report["security"]:
        findings.append("security_updates")
    if not report["unattended"]["enabled"]:
        findings.append("unattended_off")
    if report["last_refresh"] is not None and now - report["last_refresh"] > 7 * 86_400:
        findings.append("lists_stale")
    # a unit running now that would not come back after a reboot; one the owner left off on purpose is not running
    if any(unit["scope"] == "system" and not unit["enabled"] and unit["active"] for unit in report["units"]):
        findings.append("unit_not_enabled")
    if any(unit["scope"] == "user" for unit in report["units"]) and report["linger"] is False:
        findings.append("user_units_without_linger")   # the gateway would not come back after a reboot
    report["findings"] = findings
    report["ready"] = not ({"dpkg_interrupted", "apt_broken", "package_manager_busy", "disk_low"} & set(findings))
    return report


_INST = re.compile(r"^Inst (?P<name>\S+)(?: \[(?P<old>[^\]]+)\])? \((?P<new>\S+)")
_REMV = re.compile(r"^Remv (?P<name>\S+)(?: \[(?P<old>[^\]]+)\])?")


def plan(host: Host | None = None, *, refresh: bool = True, full: bool = False) -> dict:
    """Refresh the package lists and simulate the upgrade. Changes only apt's lists."""
    host = host or Host()
    result: dict = {"refreshed": False, "full": full}
    if refresh:
        code, out, err = host.run(["apt-get", "-o", "Dpkg::Lock::Timeout=120", "update"], timeout=600)
        result["refreshed"] = code == 0
        if code != 0:
            result["error"] = " ".join((err or out).split())[-300:]
            return result
    command = ["apt-get", "-s", "-o", "Debug::NoLocking=1"]
    command += ["full-upgrade"] if full else ["--with-new-pkgs", "upgrade"]
    code, out, err = host.run(command, timeout=300)
    if code != 0:
        result["error"] = " ".join((err or out).split())[-300:]
        return result
    install, upgrade, remove, kept = [], [], [], []
    lines = out.splitlines()
    for line in lines:
        match = _INST.match(line)
        if match:
            item = {"name": match["name"], "from": match["old"], "to": match["new"],
                    "attention": attention_of(match["name"])}
            (upgrade if match["old"] else install).append(item)
            continue
        match = _REMV.match(line)
        if match:
            remove.append({"name": match["name"], "from": match["old"], "attention": attention_of(match["name"])})
    for index, line in enumerate(lines):
        if line.startswith("The following packages have been kept back"):
            for follow in lines[index + 1:]:
                if not follow.startswith(" "):
                    break
                kept += follow.split()
    result.update({"upgrade": upgrade, "install": install, "remove": remove, "kept_back": kept,
                   "attention": sorted({code for item in upgrade + install + remove for code in item["attention"]})})
    return result


# ── the runs: a root-only folder outside every profile ────────────────────────────────────────────────
def _new_file(path: Path, mode: int = 0o600):
    """A file created new (never an existing one, never through a link)."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    with os.fdopen(_new_file(path, mode), "w", encoding="utf-8") as stream:
        stream.write(text)


def _state_dir(host: Host) -> Path:
    """The runs' root-only folder, made (0700) where missing and refused when anyone else could write it."""
    base = host.path(STATE_DIR)
    for part in reversed([base, *list(base.parents)[:3]]):   # /var/lib/hermes-atlas-evolution/system and its parent
        if not part.exists():
            part.mkdir(mode=0o700)
    if base.is_symlink() or base.parent.is_symlink() or not host.root_owned(base):
        raise ValueError("The plugin's system folder is not root's alone; nothing was run")
    return base


def _new_run(host: Host, now: float) -> Path:
    base = _state_dir(host)
    folder = base / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + secrets.token_hex(4))
    folder.mkdir(mode=0o700)
    runs = sorted(item for item in base.iterdir() if item.is_dir() and not item.is_symlink())
    for old in runs[:-RUNS_KEPT]:
        if not (old / "holds").exists():   # never a run that may still hold packages (its list goes once released)
            shutil.rmtree(old, ignore_errors=True)
    return folder


def _guard(host: Host, home: Path, *, repair: bool = False) -> None:
    """Only root, only for a Hermes that belongs to root: otherwise whoever can write the profile or the plugin,
    the agent included, could steer what root runs."""
    if not host.root_user():
        raise ValueError("Repairing the package manager needs root: run this menu as root (sudo)" if repair
                         else "Upgrading the system needs root: run this menu as root (sudo)")
    for path in (Path(home), Path(__file__).resolve().parent):
        if not host.root_owned(path):
            raise ValueError("This Hermes is not root's: another user can change what root would run. Update the "
                             "system with apt yourself (the plan shows what would change)")


def _snapshot(host: Host, folder: Path, name: str) -> int:
    code, out, _err = host.run(["dpkg-query", "-W", "-f=${Package}\\t${Version}\\t${db:Status-Abbrev}\\n"], timeout=120)
    _write(folder / name, out if code == 0 else "")
    return len(out.splitlines()) if code == 0 else 0


def _copy_etc(host: Host, folder: Path) -> None:
    with os.fdopen(_new_file(folder / "etc-before.tar.gz"), "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        def keep(info):
            return info if (info.isfile() or info.isdir() or info.issym()) else None
        try:
            tar.add(str(host.path("/etc")), arcname="etc", filter=keep)
        except OSError:
            pass


def clean_skip(skip) -> list[str]:
    """The package names to leave out, as apt names them (`name` or `name:arch`). Anything else is refused, never
    dropped in silence."""
    names = [str(item).strip() for item in skip or () if str(item).strip()]
    wrong = [name for name in names if not PACKAGE_NAME.fullmatch(name)]
    if wrong:
        raise ValueError("Not a package name apt knows: " + ", ".join(wrong[:5]))
    return names


def script(folder: Path, *, full: bool = False, skip: list[str] = (), repair: bool = False) -> str:
    """The shell script a run executes. Paths are quoted; package names are checked by clean_skip."""
    names = clean_skip(skip)
    q = shlex.quote
    log, status, holds = q(str(folder / "run.log")), q(str(folder / "status")), q(str(folder / "holds"))
    after_list = q(str(folder / "packages-after.tsv"))
    if repair:
        body = ("dpkg --force-confdef --force-confold --configure -a && apt-get -y -o Dpkg::Lock::Timeout=600 "
                "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -f install")
    else:
        action = "full-upgrade" if full else "--with-new-pkgs upgrade"
        body = ("apt-get -y -o Dpkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef "
                f"-o Dpkg::Options::=--force-confold {action}")
    return f"""#!/bin/sh
# Atlas Evolution: {'repair of the package manager' if repair else 'operating system upgrade'}, started by the owner.
set -u
umask 077
exec >>{log} 2>&1
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 APT_LISTCHANGES_FRONTEND=none LC_ALL=C
SKIP={q(' '.join(names))}
HELD=""
# A package is taken off the list only once apt-mark really released it. apt-mark fails at once while dpkg holds
# its lock (a dpkg left running by a stopped apt-get): it is tried again for up to 3 minutes in all, inside the
# 4 minutes systemd gives a stopped unit. What stays on the list is found by the check and released by repair.
release() {{
  left=""
  waited=0
  for p in $HELD; do
    until apt-mark unhold "$p"; do
      if [ "$waited" -ge 180 ]; then left="$left $p"; break; fi
      sleep 5
      waited=$((waited + 5))
    done
  done
  HELD="$left"
  if [ -z "$left" ]; then rm -f {holds}; else echo "== still held:$left (the repair releases them)"; fi
}}
trap release EXIT
trap 'release; exit 130' INT TERM HUP
NATIVE=$(dpkg --print-architecture 2>/dev/null || true)
for p in $SKIP; do
  case "$p" in *:"$NATIVE"|*:all) p=${{p%:*}};; esac   # apt-mark showhold prints the bare name for these
  if dpkg-query -W "$p" >/dev/null 2>&1 && ! apt-mark showhold | grep -qx "$p"; then
    echo "$p" >> {holds}
    apt-mark hold "$p" && HELD="$HELD $p"
  fi
done
echo "== $(date -u +%FT%TZ) start"
{body}
code=$?
echo "== $(date -u +%FT%TZ) exit $code"
release   # before the exit code: whoever reads the result sees the packages already released
dpkg-query -W -f='${{Package}}\\t${{Version}}\\t${{db:Status-Abbrev}}\\n' > {after_list} 2>/dev/null || true
printf '%s\\n' "$code" > {status}
exit "$code"
"""


def start(host: Host, folder: Path, *, full: bool = False, skip: list[str] = (), repair: bool = False) -> dict:
    """Start a run in its own systemd unit (without systemd, as a process of its own session)."""
    _write(folder / "run.sh", script(folder, full=full, skip=skip, repair=repair), 0o700)
    unit = f"hermes-evolution-os-{'repair' if repair else 'upgrade'}-{folder.name}"
    if host.path("/run/systemd/system").is_dir() and host.which("systemd-run"):
        code, out, err = host.run(["systemd-run", f"--unit={unit}", "--collect", "--quiet",
                                   "--property=TimeoutStartSec=3h",
                                   "--property=TimeoutStopSec=4min", "/bin/sh", str(folder / "run.sh")], timeout=60)
        if code != 0:
            raise ValueError(" ".join((err or out).split())[:200] or "systemd-run refused the upgrade")
        _write(folder / "unit", unit)
        return {"mode": "systemd", "unit": unit}
    if host.runner is not None:
        code, _out, _err = host.run(["/bin/sh", str(folder / "run.sh")], timeout=7200)
        return {"mode": "direct", "code": code}
    process = subprocess.Popen(["/bin/sh", str(folder / "run.sh")], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True, env={**os.environ, **_ENV})
    _write(folder / "pid", str(process.pid))
    return {"mode": "process", "pid": process.pid}


def finished(folder: Path) -> int | None:
    try:
        return int((folder / "status").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


PREPARE_SECONDS = 1800


def _process_alive(host: Host, pid: int, folder: Path) -> bool:
    """Whether the run's own shell still runs: a zombie, or a pid now used by another program, is not it."""
    try:
        done, _status = os.waitpid(pid, os.WNOHANG)   # reaps it when this process started it
        if done == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        state = (host.path(f"/proc/{pid}") / "stat").read_text(encoding="utf-8", errors="replace")
        if state.rsplit(")", 1)[1].split()[0] in ("Z", "X"):
            return False
        line = (host.path(f"/proc/{pid}") / "cmdline").read_bytes()[:4096]
    except (OSError, IndexError):
        return False
    return str(folder / "run.sh").encode() in line


def progress(host: Host, folder: Path, *, now: float | None = None) -> str:
    """"done"; "running"; "lost" (its unit or process is gone without an exit code: killed, or the machine went
    down); "preparing" (copies being taken, not started yet) or "abandoned" (never started: interrupted while it
    prepared)."""
    if finished(folder) is not None:
        return "done"
    if not (folder / "run.sh").exists():
        try:
            age = (time.time() if now is None else now) - folder.stat().st_mtime
        except OSError:
            return "abandoned"
        return "preparing" if age < PREPARE_SECONDS else "abandoned"
    try:
        unit = (folder / "unit").read_text(encoding="utf-8").strip()
    except OSError:
        unit = ""
    if unit:
        _code, out, _err = host.run(["systemctl", "is-active", unit], timeout=10)
        alive = out.strip() in ("active", "activating", "reloading", "deactivating")
    else:
        try:
            alive = _process_alive(host, int((folder / "pid").read_text(encoding="utf-8").strip()), folder)
        except (OSError, ValueError):
            alive = False
    if alive:
        return "running"
    return "done" if finished(folder) is not None else "lost"   # it may have finished in between


def _hermes_version(host: Host) -> tuple[bool | None, str | None]:
    if host.runner is not None:
        code, out, _err = host.run(["hermes", "--version"], timeout=120)
    else:
        from .admin import hermes_executable

        executable = hermes_executable()
        if executable is None:
            return None, None   # unknown, not a failure: this root's PATH does not reach Hermes
        code, out, _err = host.run([executable, "--version"], timeout=120)
    return code == 0, (out.strip().splitlines()[0][:120] if code == 0 and out.strip() else None)


def after(host: Host, folder: Path, before: dict, *, now: float | None = None) -> dict:
    """What a run left: package manager, services, Hermes, what changed. Written as the run's report."""
    report = check(host, now=now)
    after_from = "run"
    if not (folder / "packages-after.tsv").exists():   # the run was killed before it listed them: taken now
        after_from = "later"
        _snapshot(host, folder, "packages-after.tsv")

    def table(path: Path) -> dict:
        rows = {}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return rows
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows[parts[0]] = parts[1]
        return rows

    old, new = table(folder / "packages-before.tsv"), table(folder / "packages-after.tsv")
    changed = [{"name": name, "from": old.get(name), "to": new.get(name)} for name in sorted(set(old) | set(new))
               if old.get(name) != new.get(name)]
    gone = sorted(set(before.get("running") or ()) - set(report.get("running") or ()))
    stopped, stopped_clean = [], []
    for unit in gone:   # a service that ended normally (idle exit) is not a failure, but is listed
        user = unit.startswith("user:")
        name = unit.split(":", 1)[1] if user else unit
        _code, out, _err = host.run(["systemctl", *(["--user"] if user else []), "show", name, "-p", "ActiveState",
                                     "-p", "Result"], timeout=10)
        values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if values.get("ActiveState") == "failed" or values.get("Result", "success") != "success":
            stopped.append(unit)
        elif values.get("ActiveState") not in ("active", "activating", "reloading"):
            stopped_clean.append(unit)
    newly_failed = sorted(set(report.get("failed") or ()) - set(before.get("failed") or ()))
    hermes_ok, version = _hermes_version(host)
    state = progress(host, folder)
    result = {"exit": finished(folder), "state": state, "changed": changed,
              "stopped_units": sorted(set(stopped) | set(newly_failed)), "stopped_clean": stopped_clean,
              "after_from": after_from, "hermes_ok": hermes_ok, "hermes_version": version,
              "dpkg_ok": report.get("dpkg_ok"), "apt_ok": report.get("apt_ok"),
              "reboot_required": report.get("reboot_required"), "reboot_packages": report.get("reboot_packages", []),
              "plugin_holds": report.get("plugin_holds", []),
              "go_back": [f"apt-get install {item['name']}={item['from']}" for item in changed if item["from"] and item["to"]][:200]}
    result["ok"] = bool(result["exit"] == 0 and result["dpkg_ok"] and result["apt_ok"] is not False
                        and result["hermes_ok"] is not False and not result["stopped_units"])
    path = folder / "report.json"
    if path.exists() and not path.is_symlink():
        path.unlink()
    _write(path, json.dumps(result, ensure_ascii=False, indent=2))
    return result


def runs(host: Host | None = None) -> list[Path]:
    host = host or Host()
    try:
        return sorted(item for item in host.path(STATE_DIR).iterdir() if item.is_dir() and not item.is_symlink())
    except OSError:
        return []


def last_report(host: Host | None = None) -> tuple[Path | None, dict | None]:
    """The latest run and its report, written now when the run ended while nobody waited for it."""
    host = host or Host()
    found = [run for run in runs(host) if (run / "run.sh").exists()]   # never a run that was only prepared
    if not found:
        return None, None
    folder = found[-1]
    try:
        return folder, json.loads((folder / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if progress(host, folder) == "running":
        return folder, {"state": "running"}
    try:
        before = json.loads((folder / "check-before.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    return folder, after(host, folder, before)


def prepare(home: Path, host: Host | None = None, *, now: float | None = None) -> tuple[Path, dict, dict]:
    """The check that must pass and the copies kept before an upgrade. ValueError says what is in the way."""
    host = host or Host()
    now = time.time() if now is None else now
    before = check(host, now=now, home=home)
    if not before["supported"]:
        raise ValueError("This system is not one the plugin upgrades (Linux with apt only)")
    _guard(host, home)
    if not before["ready"]:
        raise ValueError("The system is not ready for an upgrade: " + ", ".join(before["findings"]))
    folder = _new_run(host, now)
    count = _snapshot(host, folder, "packages-before.tsv")
    _copy_etc(host, folder)
    _write(folder / "check-before.json", json.dumps(before, ensure_ascii=False, indent=2))
    return folder, before, {"packages": "packages-before.tsv", "etc": "etc-before.tar.gz", "count": count,
                            "folder": str(folder)}


def repair(home: Path, host: Host | None = None, *, now: float | None = None, wait: bool = True) -> dict:
    """Finish what an interrupted dpkg or apt left half-way, and release the packages a killed run of this plugin
    held: `dpkg --configure -a`, then `apt-get -f install`, keeping every configuration file the owner changed, in
    its own systemd unit like an upgrade. Refused while another package manager works."""
    host = host or Host()
    now = time.time() if now is None else now
    _guard(host, home, repair=True)
    found = check(host, now=now, home=home)
    if found.get("busy"):
        raise ValueError("Another package manager is running now: " + ", ".join(found["busy"]))
    released, kept = [], []
    for name in found.get("plugin_holds") or ():
        if host.run(["apt-mark", "unhold", name], timeout=90)[0] == 0:
            released.append(name)
        else:
            kept.append(name)
    for run in runs(host):   # a list is dropped only when none of its packages is still held by this plugin
        holds = run / "holds"
        if holds.is_file() and not holds.is_symlink() and not (set(_names(holds)) & set(kept)):
            holds.unlink()
    folder = _new_run(host, now)
    _write(folder / "check-before.json", json.dumps(found, ensure_ascii=False, indent=2))
    _snapshot(host, folder, "packages-before.tsv")
    started = start(host, folder, repair=True)
    if wait:
        while progress(host, folder) == "running":
            time.sleep(2)
    report = check(host, now=time.time(), home=home)
    return {"started": started, "released": released, "exit": finished(folder), "state": progress(host, folder),
            "dpkg_ok": report.get("dpkg_ok"), "apt_ok": report.get("apt_ok"), "findings": report.get("findings"),
            "ok": bool(finished(folder) == 0 and report.get("dpkg_ok") and report.get("apt_ok") is not False),
            "folder": str(folder)}


def cached(conn, home: Path, *, now: float, host: Host | None = None, force: bool = False) -> dict | None:
    """The day's check, read from the plugin's store or taken now (the learning pass calls this)."""
    row = conn.execute("SELECT value FROM cursor WHERE key='system_check'").fetchone()
    try:
        last = json.loads(row[0]) if row else None
    except ValueError:
        last = None
    if not force and isinstance(last, dict) and now - float(last.get("at") or 0) < CHECK_EVERY_SECONDS:
        return last
    if os.name != "posix" or not Path("/etc/os-release").exists() and host is None:
        return None
    found = check(host, now=now, home=home)
    keep = {key: found.get(key) for key in ("at", "supported", "os", "kernel", "security", "reboot_required",
                                             "reboot_packages", "dpkg_ok", "apt_ok", "busy", "busy_seconds", "held", "plugin_holds",
                                             "disk_low", "unattended", "last_refresh", "linger", "owner", "findings",
                                             "ready")}
    keep["upgradable"] = len(found.get("upgradable") or [])
    keep["units"] = [{key: unit[key] for key in ("unit", "scope", "enabled", "active")} for unit in found.get("units") or ()]
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('system_check', ?)", (json.dumps(keep),))
    return keep
