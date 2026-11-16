"""Keep the operating system up to date without breaking it or Hermes (0.8.4). Linux with apt (Ubuntu, Debian).

Three steps, each safe on its own:
- `check` reads, never changes: the updates waiting (security ones apart), a reboot Hermes or the system is waiting
  for, a package manager left half-way (`dpkg --audit`), a package manager busy right now, held packages, room on
  /, /boot and /var, whether automatic security updates are on, and whether Hermes comes back after a reboot (its
  systemd units enabled; a user unit, like the gateway's, needs the user's linger). The learning pass runs it at most
  once a day and the menu shows what it found.
- `plan` refreshes the package lists (`apt-get update`) and simulates the upgrade (`apt-get -s`): what would be
  installed, upgraded or removed, and the packages that need attention: one that restarts the containers (docker,
  containerd), one that needs a reboot (libc6, systemd, the kernel), one that Hermes' own tools rely on.
- `upgrade` is the owner's, in their own terminal (never the agent, never by itself), and only when `check` finds
  nothing in the way. Before: the list of every installed package with its version and a copy of /etc, owner-only,
  in `<profile>/evolution/system/<time>/`. Then `apt-get --with-new-pkgs upgrade` (never removes a package; `full`
  is apt's full-upgrade, which may) runs in its own systemd unit, so a closed terminal or a restarted gateway never
  leaves dpkg half-way; it keeps every configuration file the owner changed, restarts no service by itself
  (needrestart only lists them) and may skip packages for this run (held, and released at the end). After: dpkg and
  apt are checked again, every service that was running is checked again, Hermes is asked for its version, and the
  report lists what changed, with the versions to go back to.
Nothing here reboots the machine: the report says when a reboot is due.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

CHECK_EVERY_SECONDS = 86_400
# Packages worth a word before an upgrade: (what happens, the packages, as prefixes)
ATTENTION = (
    ("restarts_containers", ("docker.io", "docker-ce", "containerd", "containerd.io", "runc", "podman")),
    ("needs_reboot", ("libc6", "systemd", "linux-image", "linux-generic", "linux-modules", "dbus", "libssl", "openssl")),
    ("restarts_web", ("nginx", "apache2", "caddy", "haproxy")),
    ("remote_access", ("openssh-server", "sudo", "pam")),
    ("boot", ("grub", "initramfs-tools", "shim-signed")),
    ("package_manager", ("apt", "dpkg", "libapt-pkg")),
)
BUSY_PROCESSES = ("apt", "apt-get", "aptitude", "dpkg", "unattended-upgr", "packagekitd")
MIN_FREE = {"/": 1_500_000_000, "/boot": 150_000_000, "/var": 1_000_000_000}
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
            if len(parts) < 2 or not parts[0].endswith(".service") or "@" in parts[0]:
                continue
            unit, state = parts[0], parts[1]
            code_active, active, _err = host.run(["systemctl", *prefix, "is-active", unit], timeout=10)
            units.append({"unit": unit, "scope": scope, "enabled": state in ("enabled", "enabled-runtime", "static",
                                                                             "alias", "generated"),
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


def _linger(host: Host) -> bool | None:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ("root" if os.geteuid() == 0 else None)
    if not user:
        return None
    if host.path(f"/var/lib/systemd/linger/{user}").exists():
        return True
    code, out, _err = host.run(["loginctl", "show-user", user, "-p", "Linger"], timeout=10)
    if code != 0:
        return None
    return out.strip().endswith("=yes")


def _unattended(host: Host) -> dict:
    text = " ".join(filter(None, (host.read("/etc/apt/apt.conf.d/20auto-upgrades"),
                                  host.read("/etc/apt/apt.conf.d/10periodic"))))
    on = bool(re.search(r'APT::Periodic::Unattended-Upgrade\s+"(?!0)\d+"', text))
    installed = host.path("/usr/bin/unattended-upgrade").exists() or host.path("/usr/bin/unattended-upgrades").exists()
    log = host.path("/var/log/unattended-upgrades/unattended-upgrades.log")
    try:
        last = log.stat().st_mtime
    except OSError:
        last = None
    return {"installed": installed, "enabled": on and installed, "last_run": last}


def _last_refresh(host: Host) -> float | None:
    for name in ("/var/lib/apt/periodic/update-success-stamp", "/var/lib/apt/lists/partial"):
        try:
            return host.path(name).stat().st_mtime
        except OSError:
            continue
    return None


def check(host: Host | None = None, *, now: float | None = None) -> dict:
    """What stands between this machine and a safe upgrade, and what is waiting. Reads only."""
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
    code, audit, _err = host.run(["dpkg", "--audit"], timeout=60)
    report["dpkg_audit"] = audit.strip().splitlines()[:20]
    report["dpkg_ok"] = code == 0 and not audit.strip()
    report["root"] = host.root_user()
    # Without apt's lock (Debug::NoLocking): a check never makes unattended-upgrades or the owner's apt wait.
    code, out, err = host.run(["apt-get", "-o", "Debug::NoLocking=1", "check"], timeout=120)
    report["apt_ok"] = code == 0
    report["apt_error"] = None if code == 0 else " ".join((err or out).split())[:200]
    busy = sorted({name for name in host.processes() if name in BUSY_PROCESSES})
    report["busy"] = busy
    code, held, _err = host.run(["apt-mark", "showhold"], timeout=30)
    report["held"] = held.split() if code == 0 else []
    report["disk"] = {mount: host.free(mount) for mount in MIN_FREE}
    report["unattended"] = _unattended(host)
    report["last_refresh"] = _last_refresh(host)
    report["units"] = _user_units(host)
    report["running"] = running_services(host)
    report["linger"] = _linger(host)
    findings = []
    if not report["dpkg_ok"]:
        findings.append("dpkg_interrupted")
    if report["apt_ok"] is False:
        findings.append("apt_broken")
    if busy:
        findings.append("package_manager_busy")
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
    if any(unit["scope"] == "system" and not unit["enabled"] for unit in report["units"]):
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


def _snapshot(host: Host, folder: Path) -> dict:
    """Every installed package with its version, and a copy of /etc, owner-only."""
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    code, out, _err = host.run(["dpkg-query", "-W", "-f=${Package}\\t${Version}\\t${db:Status-Abbrev}\\n"], timeout=120)
    packages = folder / "packages-before.tsv"
    fd = os.open(packages, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(out if code == 0 else "")
    archive = folder / "etc-before.tar.gz"
    fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        def keep(info):
            return info if (info.isfile() or info.isdir() or info.issym()) else None
        try:
            tar.add(str(host.path("/etc")), arcname="etc", filter=keep)
        except OSError:
            pass
    return {"packages": packages.name, "etc": archive.name, "count": len(out.splitlines()) if code == 0 else 0}


def script(folder: Path, *, full: bool, skip: list[str]) -> str:
    """The shell script the upgrade unit runs. Only package names that apt itself would accept reach it."""
    names = [name for name in skip if re.fullmatch(r"[a-z0-9][a-z0-9.+-]{0,99}", name)]
    action = "full-upgrade" if full else "--with-new-pkgs upgrade"
    log, status = folder / "upgrade.log", folder / "status"
    return f"""#!/bin/sh
# Atlas Evolution: operating system upgrade, started by the owner. Log: {log}
set -u
umask 077
exec >>"{log}" 2>&1
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 APT_LISTCHANGES_FRONTEND=none LC_ALL=C
SKIP="{' '.join(names)}"
HELD=""
for p in $SKIP; do
  if dpkg-query -W "$p" >/dev/null 2>&1 && ! apt-mark showhold | grep -qx "$p"; then apt-mark hold "$p" && HELD="$HELD $p"; fi
done
release() {{ for p in $HELD; do apt-mark unhold "$p"; done; }}
trap release EXIT
echo "== $(date -u +%FT%TZ) apt-get {action}"
apt-get -y -o Dpkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold {action}
code=$?
echo "== $(date -u +%FT%TZ) exit $code"
printf '%s\\n' "$code" > "{status}"
exit "$code"
"""


def start(host: Host, folder: Path, *, full: bool = False, skip: list[str] = (), unit: str | None = None) -> dict:
    """Start the upgrade in its own systemd unit (or, without systemd, as a process of its own session)."""
    body = script(folder, full=full, skip=list(skip))
    path = folder / "upgrade.sh"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(body)
    unit = unit or f"hermes-evolution-os-upgrade-{int(time.time())}"
    if host.path("/run/systemd/system").is_dir() and host.which("systemd-run"):
        code, out, err = host.run(["systemd-run", f"--unit={unit}", "--collect", "--quiet",
                                   "--property=TimeoutStartSec=3h", "/bin/sh", str(path)], timeout=60)
        if code != 0:
            raise ValueError(" ".join((err or out).split())[:200] or "systemd-run refused the upgrade")
        return {"mode": "systemd", "unit": unit}
    if host.runner is not None:
        code, _out, err = host.run(["/bin/sh", str(path)], timeout=7200)
        return {"mode": "direct", "code": code}
    process = subprocess.Popen(["/bin/sh", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True, env={**os.environ, **_ENV})
    return {"mode": "process", "pid": process.pid}


def finished(folder: Path) -> int | None:
    try:
        return int((folder / "status").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def after(host: Host, folder: Path, before: dict, *, now: float | None = None) -> dict:
    """What the upgrade left: package manager, services that were running, Hermes, and what changed."""
    report = check(host, now=now)
    code, out, _err = host.run(["dpkg-query", "-W", "-f=${Package}\\t${Version}\\t${db:Status-Abbrev}\\n"], timeout=120)
    fd = os.open(folder / "packages-after.tsv", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(out if code == 0 else "")

    def table(text: str) -> dict:
        rows = {}
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows[parts[0]] = parts[1]
        return rows

    old = table((folder / "packages-before.tsv").read_text(encoding="utf-8", errors="replace")
                if (folder / "packages-before.tsv").exists() else "")
    new = table(out if code == 0 else "")
    changed = [{"name": name, "from": old.get(name), "to": new.get(name)} for name in sorted(set(old) | set(new))
               if old.get(name) != new.get(name)]
    stopped = sorted(set(before.get("running") or ()) - set(report.get("running") or ()))
    code, version, _err = host.run(["hermes", "--version"], timeout=120)
    result = {"exit": finished(folder), "changed": changed, "stopped_units": stopped,
              "hermes_ok": code == 0, "hermes_version": version.strip().splitlines()[0][:120] if code == 0 and version.strip() else None,
              "dpkg_ok": report.get("dpkg_ok"), "apt_ok": report.get("apt_ok"),
              "reboot_required": report.get("reboot_required"), "reboot_packages": report.get("reboot_packages", []),
              "go_back": [f"apt-get install {item['name']}={item['from']}" for item in changed if item["from"] and item["to"]][:200]}
    result["ok"] = bool(result["exit"] == 0 and result["dpkg_ok"] and result["apt_ok"] and result["hermes_ok"] and not stopped)
    fd = os.open(folder / "report.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return result


def repair(host: Host | None = None) -> dict:
    """Finish what an interrupted dpkg or apt left half-way (a power cut, a killed terminal): `dpkg --configure -a`,
    then `apt-get -f install`, keeping every configuration file the owner changed. The owner's, in their terminal;
    refused while another package manager runs."""
    host = host or Host()
    if not host.root_user():
        raise ValueError("Repairing the package manager needs root: run this menu as root (sudo)")
    busy = sorted({name for name in host.processes() if name in BUSY_PROCESSES})
    if busy:
        raise ValueError("Another package manager is running now: " + ", ".join(busy))
    steps = []
    for argv in (["dpkg", "--force-confdef", "--force-confold", "--configure", "-a"],
                 ["apt-get", "-y", "-o", "Dpkg::Lock::Timeout=300", "-o", "Dpkg::Options::=--force-confdef",
                  "-o", "Dpkg::Options::=--force-confold", "-f", "install"]):
        code, out, err = host.run(argv, timeout=3600)
        steps.append({"command": " ".join(argv), "exit": code, "tail": (err or out).strip().splitlines()[-3:]})
        if code != 0:
            break
    report = check(host)
    return {"steps": steps, "dpkg_ok": report.get("dpkg_ok"), "apt_ok": report.get("apt_ok"),
            "ok": bool(report.get("dpkg_ok") and report.get("apt_ok") is not False), "findings": report.get("findings")}


def folder_for(home: Path, now: float) -> Path:
    return Path(home) / "evolution" / "system" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def prepare(home: Path, host: Host | None = None, *, now: float | None = None) -> tuple[Path, dict, dict]:
    """The check that must pass and the snapshot taken before an upgrade. Raises ValueError when something is in the
    way (the check's findings say what)."""
    host = host or Host()
    now = time.time() if now is None else now
    before = check(host, now=now)
    if not before["supported"]:
        raise ValueError("This system is not one the plugin upgrades (Linux with apt only)")
    if not before.get("root"):
        raise ValueError("Upgrading the system needs root: run this menu as root (sudo)")
    if not before["ready"]:
        raise ValueError("The system is not ready for an upgrade: " + ", ".join(before["findings"]))
    folder = folder_for(home, now)
    snapshot = _snapshot(host, folder)
    with open(os.open(folder / "check-before.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
              encoding="utf-8") as stream:
        json.dump(before, stream, ensure_ascii=False, indent=2)
    return folder, before, snapshot


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
    found = check(host, now=now)
    keep = {key: found.get(key) for key in ("at", "supported", "os", "kernel", "security", "reboot_required",
                                             "reboot_packages", "dpkg_ok", "apt_ok", "busy", "held", "disk_low",
                                             "unattended", "last_refresh", "linger", "findings", "ready")}
    keep["upgradable"] = len(found.get("upgradable") or [])
    keep["units"] = [{key: unit[key] for key in ("unit", "scope", "enabled", "active")} for unit in found.get("units") or ()]
    conn.execute("INSERT OR REPLACE INTO cursor(key, value) VALUES ('system_check', ?)", (json.dumps(keep),))
    return keep
