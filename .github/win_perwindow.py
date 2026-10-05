"""Separate accounts per window on a real Windows machine (GitHub Actions, workflow "Windows app",
job "smoke"): what the Linux tests can only simulate. Nothing real is touched; Claude Code is a
stand-in that records how it was started.

    python .github/win_perwindow.py

1. The `claude` wrapper (claude.cmd first on PATH): arguments pass through unchanged, the exit
   code comes back, and with the app's Python gone it steps aside to the real `claude`.
2. Sharing ~/.claude without symlink rights: folders as junctions, files as hard links, and
   removing a profile removes the links only.
3. Pointing out a window: a console window is found from the process that runs in it.
4. The profile and connection tests, run here on Windows.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from account_switcher import highlight, profiles  # noqa: E402

failures = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def fake_claude(folder):
    """claude.cmd that writes its arguments and environment to record.json, then exits with 7."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fake.py").write_text(
        "import json, os, sys\n"
        "json.dump({'args': sys.argv[1:], 'config': os.environ.get('CLAUDE_CONFIG_DIR'),\n"
        "           'wrapper': os.environ.get('LIMITSWITCHER_WRAPPER_DIR')},\n"
        "          open(os.environ['FAKE_RECORD'], 'w'))\n"
        "sys.exit(7)\n", encoding="utf-8")
    (folder / "claude.cmd").write_text(f'@"{sys.executable}" "%~dp0fake.py" %*\r\n@exit /b %ERRORLEVEL%\r\n', encoding="utf-8")


def run_claude(bin_folder, fake_folder, record, *args):
    env = dict(os.environ, PATH=os.pathsep.join([str(bin_folder), str(fake_folder), os.environ["PATH"]]),
               FAKE_RECORD=str(record))
    env.pop("CLAUDE_CONFIG_DIR", None)
    record.unlink(missing_ok=True)
    done = subprocess.run(["cmd.exe", "/d", "/c", "claude", *args], env=env, capture_output=True, text=True, timeout=60)
    data = json.loads(record.read_text()) if record.exists() else None
    return done.returncode, data, done.stdout + done.stderr


def wrapper():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fake, record = tmp / "real", tmp / "record.json"
        fake_claude(fake)
        args = ["--model", "opus fast", "fix it & test", "100%"]
        # The app isn't running (no state file): the window shares the main login, unchanged.
        folder = profiles.install_wrapper(tmp / "store", sys.executable, tmp / "store" / "running.json")
        code, data, output = run_claude(folder, fake, record, *args)
        check(code == 7, f"the wrapper returns Claude Code's exit code (got {code}) {output.strip()}")
        check(data is not None and data["args"] == args, f"arguments pass through unchanged: {data and data['args']}")
        check(data is not None and not data["config"] and not data["wrapper"], f"no profile, no leftover variables: {data}")
        # Uninstalled app (its Python gone): the wrapper steps aside to the real claude.
        folder = profiles.install_wrapper(tmp / "store", tmp / "gone" / "python.exe", tmp / "store" / "running.json")
        code, data, output = run_claude(folder, fake, record, *args)
        check(code == 7 and data is not None and data["args"][:2] == args[:2],
              f"with the app's Python gone, the real claude runs (code {code}, {data and data['args']}) {output.strip()}")


def links():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "home" / ".claude"
        (source / "projects").mkdir(parents=True)
        (source / "projects" / "s.jsonl").write_text("{}")
        (source / "settings.json").write_text('{"model": "opus"}')
        profile = tmp / "profile"
        profile.mkdir()

        def refuse(*args, **kwargs):
            raise OSError("A required privilege is not held by the client")
        with mock.patch.object(profiles.os, "symlink", refuse):
            how_dir = profiles._link(source / "projects", profile / "projects")
            how_file = profiles._link(source / "settings.json", profile / "settings.json")
        check(how_dir == "junction" and (profile / "projects" / "s.jsonl").exists(), f"a folder is shared as a {how_dir}")
        check(how_file == "hard link" and os.path.samefile(source / "settings.json", profile / "settings.json"),
              f"a file is shared as a {how_file}")
        (profile / "settings.json").write_text('{"model": "sonnet"}')
        check("sonnet" in (source / "settings.json").read_text(), "a change made through the link is in ~/.claude")
        profiles.remove(profile, keychain=False)
        check(not profile.exists(), "the profile folder is gone")
        check((source / "projects" / "s.jsonl").exists() and (source / "settings.json").exists(),
              "removing it never touched ~/.claude")


def window():
    process = subprocess.Popen(["cmd.exe", "/k", "title Claude Code test window"], creationflags=subprocess.CREATE_NEW_CONSOLE)
    try:
        found = None
        for _ in range(40):
            found = highlight.find(process.pid)
            if found:
                break
            time.sleep(0.25)
        check(bool(found), f"the window of a console process is found (pid {process.pid}, window {found})")
        check(highlight.window_of(process.pid), "it is pointed out (outline and flash)")
        time.sleep(highlight.SHOW_FOR + 0.5)
    finally:
        process.kill()


def tests():
    done = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_profiles", "test_connections"],
                          cwd=ROOT / "tests", env=dict(os.environ, PYTHONPATH=str(ROOT), NO_PROXY="*"),
                          capture_output=True, text=True, timeout=600)
    print(done.stderr[-6000:], flush=True)
    check(done.returncode == 0, "the profile and connection tests pass on Windows")


for step in (wrapper, links, window, tests):
    try:
        step()
    except Exception as error:  # one broken step doesn't hide the others
        import traceback
        traceback.print_exc()
        check(False, f"{step.__name__}: {error!r}")

if failures:
    sys.exit(f"{len(failures)} check(s) failed")
print("all per-window checks passed")
