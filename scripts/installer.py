"""LimitSwitcher installer and updater from source, for Windows.

    python scripts/installer.py            install, or update an existing install
    python scripts/installer.py --branch X switch to (and update) another branch
    python scripts/installer.py --force    update even with local edits (saved first)
    python scripts/installer.py --no-launch

Started by Install.cmd / Update.cmd, which make sure Python and git exist first. Each run:
1. Brings this folder up to date from GitHub (git; local edits and local-only commits are
   never lost: they are stashed or saved to a backup branch, and only with --force).
2. Creates or refreshes a private Python environment in .venv with the requirements for
   this OS (only reinstalled when they change).
3. Adds a Start menu shortcut. The app itself registers to start at sign-in.
4. Restarts LimitSwitcher on the new version (or starts it, on first install).
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/doge006/LimitSwitcher.git"
REQUIREMENTS = ROOT / "requirements-native.txt"
VENV = ROOT / ".venv"
RUNTIME = ROOT / ".runtime"
APP_NAME = "LimitSwitcher"
WINDOWS = sys.platform == "win32"


def say(text, tone=""):
    colours = {"ok": "32", "warn": "33", "info": "36", "error": "31", "dim": "90"}
    if tone and sys.stdout.isatty() and not WINDOWS:
        text = f"\033[{colours[tone]}m{text}\033[0m"
    print(text, flush=True)


def git(*args, check=True):
    done = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


# ---------- 1. code ----------
def local_work_on_github(target):
    """True when nothing is lost by taking GitHub's version (same files as a GitHub commit,
    or every local-only commit has an equivalent change there)."""
    tree = git("rev-parse", "HEAD^{tree}")
    if tree in git("log", "--format=%T", "--max-count=500", f"origin/{target}").split():
        return True
    done = subprocess.run(["git", "-C", str(ROOT), "cherry", f"origin/{target}", "HEAD"], capture_output=True, text=True)
    return done.returncode == 0 and not any(line.startswith("+") for line in done.stdout.splitlines())


def update_code(branch, force):
    if shutil.which("git") is None:
        raise RuntimeError("git is not installed" + (" (winget install --id Git.Git -e)" if WINDOWS
                                                      else " (xcode-select --install)"))
    if not (ROOT / ".git").exists():
        target = branch or "main"
        say(f"This folder is not a git checkout yet; converting it ({target}).", "warn")
        git("init", "--quiet")
        git("remote", "add", "origin", REPO)
        git("fetch", "--quiet", "origin", target)
        git("checkout", "--quiet", "-f", "-B", target, f"origin/{target}")
        git("branch", "--quiet", f"--set-upstream-to=origin/{target}")
        return "updated", None
    current = git("rev-parse", "--abbrev-ref", "HEAD")
    # main unless asked otherwise: a folder left on a working branch must not keep running old code
    target = branch or "main"
    say(f"Checking GitHub for updates to {target}...", "dim")
    git("fetch", "--quiet", "origin", target)
    before = git("rev-parse", "HEAD")
    if before == git("rev-parse", f"origin/{target}") and current == target:
        return "current", before
    if git("status", "--porcelain", "--untracked-files=no"):
        if not force:
            say("An update is available, but you have local edits to these files:", "warn")
            print(git("status", "--short", "--untracked-files=no"))
            say("Commit or stash them, or rerun with --force to back them up and update anyway.", "warn")
            return "blocked", before
        git("stash", "push", "--quiet", "-m", "installer backup " + time.strftime("%Y-%m-%d %H:%M"))
        say("Your local edits were saved with 'git stash' (see: git stash list).", "warn")
    if current != target:
        exists = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"refs/heads/{target}"],
                                capture_output=True).returncode == 0
        git("switch", "--quiet", target) if exists else git("switch", "--quiet", "-c", target, "--track", f"origin/{target}")
        say(f"Switched to branch {target}.", "info")
    merged = subprocess.run(["git", "-C", str(ROOT), "merge", "--quiet", "--ff-only", f"origin/{target}"],
                            capture_output=True, text=True).returncode == 0
    if not merged:
        safe = local_work_on_github(target)
        if not safe and not force:
            say(f"Your local {target} has commits that are not on GitHub:", "warn")
            print(git("log", "--oneline", "--no-decorate", "--max-count=10", f"origin/{target}..HEAD"))
            say("Rerun with --force to save them on a backup branch and take GitHub's version.", "warn")
            return "blocked", before
        backup = "backup/update-" + time.strftime("%Y%m%d-%H%M%S")
        git("branch", backup)
        git("reset", "--quiet", "--hard", f"origin/{target}")
        say(f"Old history kept on branch {backup}.", "dim" if safe else "warn")
    return "updated", before


# ---------- 2. Python environment ----------
def venv_python(windowless=False):
    if WINDOWS:
        return VENV / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    return VENV / "bin" / "python3"


def ensure_environment():
    if not venv_python().exists():
        say("Creating a private Python environment...", "info")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    wanted = hashlib.sha256(REQUIREMENTS.read_bytes() + sys.platform.encode()).hexdigest()
    stamp = VENV / ".requirements.sha256"
    if stamp.exists() and stamp.read_text().strip() == wanted:
        return
    say("Installing requirements...", "info")
    subprocess.run([str(venv_python()), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "--upgrade", "pip"], check=False)
    subprocess.run([str(venv_python()), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "-r", str(REQUIREMENTS)], check=True)
    stamp.write_text(wanted)


# ---------- 3. where the app shows up ----------
def windows_shortcut():
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "scripts" / "shortcut.ps1"), "-Python", str(venv_python(windowless=True))], check=False)


# ---------- 4. running copy ----------
def stop_running():
    """Ask a running copy to quit (it undoes its Codex / Claude changes on the way out): this
    folder's, and any other copy running for this user (an installed one), since only one may run."""
    other = log_path().with_name("running.url")
    if other.exists() and other.read_text(encoding="utf-8", errors="replace").strip() != _read(RUNTIME / "tray.url"):
        _stop(other)
    return _stop(RUNTIME / "tray.url")


def _read(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _stop(url_file):
    try:
        url = url_file.read_text(encoding="utf-8").strip()
        base, token = url.split("/#token=")
        request = Request(base + "/api/shutdown", data=b"{}", method="POST",
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        build_opener(ProxyHandler({})).open(request, timeout=5).read()
    except (OSError, ValueError):
        try:
            url_file.unlink()  # stale file from a crashed run
        except OSError:
            pass
        return False
    say("Closing the running LimitSwitcher...", "info")
    for _ in range(80):
        if not url_file.exists():
            break
        time.sleep(0.25)
    return True


def stop_leftovers():
    """Windows: end any copy of the app from this folder that is still running (one whose quit
    request could not reach it, e.g. its URL file was lost). Otherwise the new copy would find it,
    hand over to it, and the old code would keep running after an update."""
    if not WINDOWS:
        return
    folder = str(ROOT).replace("'", "''")
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine "
              f"-and $_.CommandLine.Contains('{folder}') "
              "-and $_.CommandLine -match '(LimitSwitcher|AccountSwitcher)[.]pyw|account_switcher[.]tray' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, check=False)
    ended = done.stdout.split()
    if ended:
        say(f"Closed {len(ended)} older LimitSwitcher process(es) that were still running.", "info")
        try:
            (RUNTIME / "tray.url").unlink()
        except OSError:
            pass
        time.sleep(1)


def log_path():
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LimitSwitcher" / "app.log"


def started(seconds=20):
    """True once the app is up (it writes its private URL when its server is running)."""
    for _ in range(seconds * 4):
        if (RUNTIME / "tray.url").exists():
            return True
        time.sleep(0.25)
    return False


def start():
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(venv_python(windowless=True)), str(ROOT / "LimitSwitcher.pyw")], cwd=str(ROOT),
                     creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=not WINDOWS)


class Tee:
    """Everything the installer prints also goes to update.log next to app.log, so what an update
    said can be read afterwards (the window may have closed)."""

    def __init__(self, stream, path):
        self.stream, self.file = stream, None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.file = open(path, "w", encoding="utf-8")
            self.file.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        except OSError:
            pass

    def write(self, text):
        self.stream.write(text)
        if self.file:
            self.file.write(text)
            self.file.flush()
        return len(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def main(argv=None):
    sys.stdout = Tee(sys.stdout, log_path().with_name("update.log"))
    parser = argparse.ArgumentParser(description="Install or update LimitSwitcher.")
    parser.add_argument("--branch", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument("--skip-code", action="store_true", help="do not touch the code (use this folder as it is)")
    args = parser.parse_args(argv)
    if not WINDOWS:
        say("This installer is for Windows.", "error")
        return 1
    if sys.version_info < (3, 10):
        say(f"Python 3.10 or newer is needed (this is {sys.version.split()[0]}).", "error")
        return 1
    say("LimitSwitcher installer - Windows", "info")
    try:
        status, before = ("current", None) if args.skip_code else update_code(args.branch, args.force)
        if status == "blocked":
            return 1
        if status == "updated" and before:
            after = git("rev-parse", "HEAD")
            say(f"Updated {before[:7]} -> {after[:7]}:", "ok")
            print(git("log", "--oneline", "--no-decorate", "--max-count=15", f"{before}..{after}"))
        elif status == "current":
            say("Code is up to date.", "ok")
        was_running = stop_running()
        stop_leftovers()
        ensure_environment()
        windows_shortcut()
        if not args.no_launch:
            start()
            if not started():
                say("LimitSwitcher didn't start. The error:", "error")
                try:
                    print("\n".join(log_path().read_text(encoding="utf-8").splitlines()[-25:]))
                except OSError:
                    python = venv_python()
                    say(f"(no log yet) To see it, run:  \"{python}\" \"{ROOT / 'LimitSwitcher.pyw'}\"", "warn")
                return 1
            say("Restarted LimitSwitcher." if was_running else "Started LimitSwitcher.", "ok")
        say("Done. It's in the Start menu.", "ok")
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        say(str(error), "error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
