"""A stand-in for Claude Code, for test_windows_sim.py: it behaves the way the real one does where
LimitSwitcher can see it, with fake accounts.

- Its login is in CLAUDE_CONFIG_DIR (else ~/.claude), read again for every request, as Claude Code does.
- After every turn it runs the status line command from that folder's settings.json, with the
  account's numbers (from FAKE_USAGE: access token -> [5-hour %, weekly %]).
- A turn on an account at 100% ends on a usage limit: it runs the StopFailure hook from settings.json
  and waits for it, as the real hook's asyncRewake does.
- "renew" renews its login (new access and refresh tokens), as Claude Code does on its own.

FAKE_STEPS: turn | renew | sleep:<s>, comma separated; or "control": then it does what the test
writes into <FAKE_CTL>/<FAKE_NAME>.<n> (n = 1, 2, ...: turn, renew or exit), one file at a time.
Every step is logged to FAKE_LOG (JSON lines).
"""
import json
import os
import subprocess
import sys
import time


def config_dir():
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    home = os.path.expanduser("~")
    folder = custom or os.path.join(home, ".claude")
    return folder, os.path.join(folder, ".claude.json") if custom else os.path.join(home, ".claude.json")


def login():
    folder, config = config_dir()
    with open(os.path.join(folder, ".credentials.json"), encoding="utf-8") as handle:
        oauth = json.load(handle)["claudeAiOauth"]
    with open(config, encoding="utf-8") as handle:
        email = json.load(handle)["oauthAccount"]["emailAddress"]
    return oauth, email


def settings():
    try:
        with open(os.path.join(config_dir()[0], "settings.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def usage(token):
    with open(os.environ["FAKE_USAGE"], encoding="utf-8") as handle:
        return json.load(handle).get(token, [0, 0])


def log(**entry):
    entry.update(window=os.environ.get("FAKE_NAME"), pid=os.getpid(), at=time.time(),
                 configDir=os.environ.get("CLAUDE_CONFIG_DIR") or None)
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def run(command, event):
    done = subprocess.run(command, shell=True, input=json.dumps(event), capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def turn(session):
    oauth, email = login()
    five, week = usage(oauth["accessToken"])
    if five >= 100:
        hooks = [h["command"] for group in (settings().get("hooks") or {}).get("StopFailure") or []
                 for h in group.get("hooks") or []]
        results = [run(c, {"error": "rate_limit", "session_id": session}) for c in hooks]
        log(step="limit", email=email, token=oauth["accessToken"], hook=[r[0] for r in results],
            message=" ".join(r[2] for r in results))
        return
    line = settings().get("statusLine") or {}
    shown = None
    if line.get("command"):
        now = time.time()
        _, shown, _ = run(line["command"], {"session_id": session, "model": {"display_name": "Opus"},
                                            "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": now + 3600},
                                                            "seven_day": {"used_percentage": week, "resets_at": now + 86400}}})
    log(step="turn", email=email, token=oauth["accessToken"], line=shown)


def renew():
    folder = config_dir()[0]
    path = os.path.join(folder, ".credentials.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    oauth = data["claudeAiOauth"]
    old = oauth["accessToken"]
    oauth.update(accessToken=old + "r", refreshToken=oauth["refreshToken"] + "r", expiresAt=int((time.time() + 3600) * 1000))
    with open(os.environ["FAKE_USAGE"], encoding="utf-8") as handle:
        table = json.load(handle)
    table[oauth["accessToken"]] = table.get(old, [0, 0])
    with open(os.environ["FAKE_USAGE"] + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(table, handle)
    os.replace(os.environ["FAKE_USAGE"] + ".tmp", os.environ["FAKE_USAGE"])
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(path + ".tmp", path)
    log(step="renew", token=oauth["accessToken"], refresh=oauth["refreshToken"])


def controlled():
    n = 0
    end = time.time() + 300
    while time.time() < end:
        path = os.path.join(os.environ["FAKE_CTL"], f"{os.environ['FAKE_NAME']}.{n + 1}")
        try:
            with open(path, encoding="utf-8") as handle:
                step = handle.read().strip()
        except OSError:
            time.sleep(0.03)
            continue
        if not step:
            time.sleep(0.03)
            continue
        n += 1
        yield step


def main():
    session = "session-" + (os.environ.get("FAKE_NAME") or str(os.getpid()))
    log(step="start", args=sys.argv[1:])
    steps = os.environ.get("FAKE_STEPS", "")
    if steps == "control":
        steps = controlled()
    else:
        steps = filter(None, steps.split(","))
    for step in steps:
        name, _, arg = step.partition(":")
        if name == "turn":
            turn(session)
        elif name == "renew":
            renew()
        elif name == "sleep":
            time.sleep(float(arg))
        elif name == "exit":
            break
    log(step="exit")


if __name__ == "__main__":
    main()
