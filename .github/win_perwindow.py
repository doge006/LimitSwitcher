"""Separate accounts per window on a real Windows machine (GitHub Actions, workflow "Windows app",
job "smoke"): what the Linux tests can only simulate. Nothing real is touched; Claude Code and the
app's local API are stand-ins that record what they get.

    python .github/win_perwindow.py

1. The `claude` wrapper (claude.cmd first on PATH): with the app running, the window starts as an
   ordinary one with the router's address and its id; arguments (& and % included) pass through
   unchanged, the exit code comes back, no process of the wrapper's stays, its variables don't outlive
   the window, and print mode, subcommands, the app not running and its Python gone go straight to
   the real `claude`.
2. A window from 1.3.x (a config folder with junctions and hard links into ~/.claude): removing it
   removes the links only.
3. Pointing out a window: a console window is found from the process that runs in it.
4. The window, router and connection tests, run here on Windows.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from account_switcher import processes, profiles  # noqa: E402

failures = []
ANSWER = {"window": "1a2b3c4d", "baseUrl": "http://127.0.0.1:47831/s3cret/1a2b3c4d"}


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def fake_claude(folder):
    """claude.cmd that writes its arguments, environment and parent processes to record.json, then exits with 7."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fake.py").write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from account_switcher import processes\n"
        "wrappers = [p.pid for p in processes.listing(['python', 'pythonw', 'py'])\n"
        "            if 'account_switcher\\\\window.py' in (p.command or '').replace('/', '\\\\')]\n"
        "json.dump({'args': sys.argv[1:], 'base': os.environ.get('ANTHROPIC_BASE_URL'),\n"
        "           'window': os.environ.get('LIMITSWITCHER_WINDOW'), 'config': os.environ.get('CLAUDE_CONFIG_DIR'),\n"
        "           'leftover': [k for k in ('LIMITSWITCHER_WRAPPER_DIR', 'LIMITSWITCHER_ENV') if os.environ.get(k)],\n"
        "           'wrappers': wrappers},\n"
        "          open(os.environ['FAKE_RECORD'], 'w'))\n"
        "sys.exit(7)\n", encoding="utf-8")
    (folder / "claude.cmd").write_text(f'@"{sys.executable}" "%~dp0fake.py" %*\r\n@exit /b %ERRORLEVEL%\r\n', encoding="utf-8")


class App:
    """The app's /api/window: registers the window and answers with the router's address."""

    def __init__(self):
        self.asked = []
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                app.asked.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                raw = json.dumps(ANSWER).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def state(self, folder):
        path = Path(folder) / "afk-hook.json"
        path.write_text(json.dumps({"url": f"http://127.0.0.1:{self.server.server_port}/api/afk", "token": "t"}))
        return path

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run(bin_folder, fake_folder, record, *args, after=""):
    """`claude <args>` in cmd.exe as a terminal would run it; `after` runs in the same cmd.exe afterwards."""
    env = dict(os.environ, PATH=os.pathsep.join([str(bin_folder), str(fake_folder), os.environ["PATH"]]),
               FAKE_RECORD=str(record))
    for name in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "LIMITSWITCHER_WINDOW"):
        env.pop(name, None)
    record.unlink(missing_ok=True)
    quoted = " ".join(f'"{a}"' if any(c in a for c in ' &%') else a for a in args)
    line = f"claude {quoted}" + (f" & {after}" if after else "")
    done = subprocess.run(f'cmd.exe /d /c "{line}"', env=env, capture_output=True, text=True, timeout=60)
    data = json.loads(record.read_text()) if record.exists() else None
    return done.returncode, data, done.stdout + done.stderr


def wrapper():
    app = App()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake, record = tmp / "real", tmp / "record.json"
            fake_claude(fake)
            args = ["--model", "opus fast", "fix it & test", "100%"]
            folder = profiles.install_wrapper(tmp / "store", sys.executable, app.state(tmp))
            before = set(os.listdir(os.environ.get("TEMP", tmp)))
            code, data, output = run(folder, fake, record, *args)
            check(code == 7, f"the wrapper returns Claude Code's exit code (got {code}) {output.strip()}")
            check(data is not None and data["args"] == args, f"arguments pass through unchanged: {data and data['args']}")
            check(data is not None and data["base"] == ANSWER["baseUrl"] and data["window"] == ANSWER["window"],
                  f"the window gets the router's address and its id: {data and (data['base'], data['window'])}")
            check(data is not None and not data["config"] and not data["leftover"],
                  f"an ordinary window: no config folder of its own, nothing of the wrapper's left: {data}")
            check(data is not None and data["wrappers"] == [],
                  f"no process of the wrapper's stays while Claude Code runs: {data and data['wrappers']}")
            check(len(app.asked) == 1 and isinstance(app.asked[0].get("pid"), int) and app.asked[0]["pid"] > 0,
                  f"the app was asked once, with a process to watch: {app.asked}")
            left = set(os.listdir(os.environ.get("TEMP", tmp))) - before
            check(not [n for n in left if n.startswith("limitswitcher-")], f"the variables' file is gone: {sorted(left)}")
            code, data, output = run(folder, fake, record, after="set LIMITSWITCHER")
            check(data is not None and data["window"] and "LIMITSWITCHER_WINDOW=" not in output and "LIMITSWITCHER_ENV=" not in output,
                  f"the variables end with the window (the shell doesn't keep them): {output.strip()[-300:]}")
            asked = len(app.asked)
            for straight in (["-p", "hi & bye"], ["auth", "status"], ["--version"]):
                code, data, output = run(folder, fake, record, *straight)
                check(code == 7 and data is not None and data["args"] == straight and not data["base"],
                      f"`claude {' '.join(straight)}` goes straight through: {data}")
            check(len(app.asked) == asked, "...without asking the app")
            app.close()
            code, data, output = run(folder, fake, record, *args)
            check(code == 7 and data is not None and data["args"] == args and not data["base"],
                  f"the app not running: a plain window ({data}) {output.strip()}")
            # Uninstalled app (its Python gone): the wrapper steps aside to the real claude.
            folder = profiles.install_wrapper(tmp / "store", tmp / "gone" / "python.exe", tmp / "store" / "running.json")
            code, data, output = run(folder, fake, record, *args)
            check(code == 7 and data is not None and data["args"][:2] == args[:2],
                  f"with the app's Python gone, the real claude runs (code {code}, {data and data['args']}) {output.strip()}")
    finally:
        app.server.server_close()


def legacy_folder():
    """A 1.3.x window's folder: ~/.claude's folders as junctions, its files as hard links."""
    import _winapi
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "home" / ".claude"
        (source / "projects").mkdir(parents=True)
        (source / "projects" / "s.jsonl").write_text("{}")
        (source / "settings.json").write_text('{"model": "opus"}')
        profile = tmp / "profiles" / "window-1a2b3c4d"
        profile.mkdir(parents=True)
        _winapi.CreateJunction(str(source / "projects"), str(profile / "projects"))
        os.link(source / "settings.json", profile / "settings.json")
        check(profiles._is_link(profile / "projects"), "a junction is seen as a link")
        profiles.remove(profile, keychain=False)
        check(not profile.exists(), "the old window's folder is gone")
        check((source / "projects" / "s.jsonl").exists() and (source / "settings.json").exists(),
              "removing it never touched ~/.claude")


def window():
    """Found from a process with no console of its own, as the app runs (pythonw): this script has
    one, and a console window can't be looked up from a process that has one."""
    process = subprocess.Popen(["cmd.exe", "/k", "title Claude Code test window"], creationflags=subprocess.CREATE_NEW_CONSOLE)
    try:
        # FreeConsole: a venv's python.exe starts the real one as its child, which gets a console of
        # its own when its parent has none, so the probe lets go of it first.
        probe = ("import ctypes, sys, time; ctypes.windll.kernel32.FreeConsole()\n"
                 "sys.path.insert(0, sys.argv[1]); from account_switcher import highlight\n"
                 "found = None\n"
                 "for _ in range(40):\n"
                 "    found = highlight.find(int(sys.argv[2]))\n"
                 "    if found: break\n"
                 "    time.sleep(0.25)\n"
                 "shown = bool(found) and highlight.window_of(int(sys.argv[2]))\n"
                 "print(found, shown, flush=True)\n"
                 "time.sleep(highlight.SHOW_FOR + 0.5)\n")
        done = subprocess.run([sys.executable, "-c", probe, str(ROOT), str(process.pid)], capture_output=True, text=True,
                              timeout=60, creationflags=subprocess.DETACHED_PROCESS)
        found, _, shown = done.stdout.strip().partition(" ")
        check(found not in ("", "None"), f"the window of a console process is found (pid {process.pid}, window {found})\n{done.stderr.strip()}")
        check(shown == "True", "it is pointed out (outline and flash)")
    finally:
        process.kill()


def tests():
    done = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_windows", "test_claude_router", "test_connections"],
                          cwd=ROOT / "tests", env=dict(os.environ, PYTHONPATH=str(ROOT), NO_PROXY="*"),
                          capture_output=True, text=True, timeout=600)
    print(done.stderr[-8000:], flush=True)
    check(done.returncode == 0, "the window, router and connection tests pass on Windows")


for step in (wrapper, legacy_folder, window, tests):
    try:
        step()
    except Exception as error:  # one broken step doesn't hide the others
        import traceback
        traceback.print_exc()
        check(False, f"{step.__name__}: {error!r}")

if failures:
    sys.exit(f"{len(failures)} check(s) failed")
print("all per-window checks passed")
