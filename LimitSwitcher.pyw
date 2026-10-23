"""Start LimitSwitcher in the tray without a console window (used by the Start menu shortcut
and start at sign-in). A startup error is written to app.log, since there is no console to print it to."""
import os
import sys

root = os.path.dirname(os.path.abspath(__file__))
os.chdir(root)
sys.path.insert(0, root)
if sys.platform == "win32" and os.path.basename(sys.executable).lower() == "limitswitch.exe":
    # Started by LimitSwitcher.exe, which runs Python in its own process (so Task Manager shows
    # LimitSwitcher). Helpers the app starts (Claude Code's hook and status line) need Python itself.
    sys.executable = sys._base_executable = os.path.join(root, "runtime", "pythonw.exe")
if sys.prefix == sys.base_prefix:  # started by a system Python, not the .venv's: use the .venv's packages
    import glob
    import site
    for folder in glob.glob(os.path.join(root, ".venv", "lib", "python%d.%d" % sys.version_info[:2], "site-packages")):
        site.addsitedir(folder)


def report(text):
    if sys.platform == "win32":
        folder = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "AccountSwitcher")
    else:
        folder = os.path.expanduser("~/.local/share/account-switcher")
    os.makedirs(folder, exist_ok=True)
    log = os.path.join(folder, "app.log")
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("Startup failed:\n" + text + "\n")


if sys.platform != "win32" and sys.stderr is not None:
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)  # kill -USR1: where is it stuck?

try:
    from account_switcher.tray import main
    main(["--quiet", "--url-file", os.path.join(root, ".runtime", "tray.url"), *sys.argv[1:]])
except SystemExit:
    raise
except BaseException:
    import traceback
    report(traceback.format_exc())
    raise
