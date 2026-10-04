"""Release workflow: the copy LimitSwitcher-Setup.exe installed is running. Its full view window
opened, the Start menu shortcut is there, app.log has no errors, and it quits through its API."""
import ctypes
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

folder = Path(sys.argv[1])
failures = []


def check(ok, text):
    print(("PASS " if ok else "FAIL ") + text, flush=True)
    if not ok:
        failures.append(text)


import subprocess
listed = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout.lower()
check('"limitswitcher.exe"' in listed, "the app runs as LimitSwitcher.exe (Task Manager shows LimitSwitcher)")
check('"pythonw.exe"' not in listed, "no separate Python process")
check((folder / "LimitSwitcherStatus.exe").exists(), "the status line launcher is installed (Task Manager: LimitSwitcher Status)")
user32 = ctypes.windll.user32
for _ in range(40):  # a first start on a fresh runner can be slow (the new exe is scanned)
    if user32.FindWindowW("AccountSwitcherFullView", None):
        break
    time.sleep(0.5)
check(bool(user32.FindWindowW("AccountSwitcherFullView", None)), "the full view window opened (native, bundled Python)")
link = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "LimitSwitcher.lnk"
for _ in range(20):
    if link.exists():
        break
    time.sleep(0.5)
check(link.exists(), "the Start menu shortcut is there")
log = Path(os.environ["LOCALAPPDATA"]) / "LimitSwitcher" / "app.log"
text = log.read_text(encoding="utf-8") if log.exists() else ""
print("---- app.log ----\n" + text)
check("started, version" in text, "app.log says it started")
check("Traceback" not in text and " ERROR " not in text, "no errors in app.log")
base, token = (folder / ".runtime" / "tray.url").read_text().strip().split("/#token=")
request = Request(base + "/api/shutdown", data=b"{}", method="POST",
                  headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
    check(json.load(response).get("ok") is True, "it quits when asked")
sys.exit(1 if failures else 0)
