"""A stand-in for Claude Code, for test_windows_sim.py: it behaves the way the real one does where
LimitSwitcher can see it, with fake accounts.

- Its login is in ~/.claude (CLAUDE_CONFIG_DIR if set), read again for every request, as Claude Code does.
- A turn is a request to ANTHROPIC_BASE_URL (else FAKE_ANTHROPIC, "Anthropic") with that login; the
  answer says which account it was billed to and that account's numbers (fake Anthropic: test_windows_sim).
- After every turn it runs the status line command from settings.json, with those numbers.
- A turn answered with a usage limit (429) runs the StopFailure hook from settings.json and waits for
  it, as the real hook's asyncRewake does.
- "renew" renews its login (new access and refresh tokens), as Claude Code does on its own.
- "swapaccount:<name>" does what the mod's /swapaccount does: asks the app (FAKE_STATE: its hook
  state file) and, when the app says so, points this process at the router from its next request on.

FAKE_STEPS: turn | renew | swapaccount:<name> | sleep:<s>, comma separated; or "control": then it does what the test
writes into <FAKE_CTL>/<FAKE_NAME>.<n> (n = 1, 2, ...: turn, renew or exit), one file at a time.
Every step is logged to FAKE_LOG (JSON lines).
"""
import json
import os
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


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


def log(**entry):
    entry.update(window=os.environ.get("FAKE_NAME"), pid=os.getpid(), at=time.time(),
                 routed=os.environ.get("LIMITSWITCHER_WINDOW") or None)
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def run(command, event):
    done = subprocess.run(command, shell=True, input=json.dumps(event), capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def ask(token, session):
    """(status, answer) from "Anthropic", through the router when the window has one."""
    base = os.environ.get("ANTHROPIC_BASE_URL") or os.environ["FAKE_ANTHROPIC"]
    request = Request(base.rstrip("/") + "/v1/messages?beta=true", method="POST",
                      data=json.dumps({"model": "opus", "messages": [{"role": "user", "content": "hi"}]}).encode(),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                               "anthropic-beta": "oauth-2025-04-20", "X-Claude-Code-Session-Id": session})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=60) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def turn(session):
    oauth, own = login()
    status, answer = ask(oauth["accessToken"], session)
    if status == 429:
        hooks = [h["command"] for group in (settings().get("hooks") or {}).get("StopFailure") or []
                 for h in group.get("hooks") or []]
        results = [run(c, {"error": "rate_limit", "session_id": session}) for c in hooks]
        log(step="limit", email=answer.get("email"), own=own, hook=[r[0] for r in results],
            message=" ".join(r[2] for r in results))
        return
    if status != 200:
        log(step="error", status=status, answer=answer)
        return
    line = settings().get("statusLine") or {}
    shown = None
    if line.get("command"):
        now = time.time()
        _, shown, _ = run(line["command"], {"session_id": session, "model": {"display_name": "Opus"},
                                            "rate_limits": {"five_hour": {"used_percentage": answer["five"], "resets_at": now + 3600},
                                                            "seven_day": {"used_percentage": answer["week"], "resets_at": now + 86400}}})
    log(step="turn", email=answer["email"], token=answer["token"], own=own, sent=oauth["accessToken"], line=shown)


def renew():
    folder = config_dir()[0]
    path = os.path.join(folder, ".credentials.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    oauth = data["claudeAiOauth"]
    oauth.update(accessToken=oauth["accessToken"] + "r", refreshToken=oauth["refreshToken"] + "r",
                 expiresAt=int((time.time() + 3600) * 1000))
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(path + ".tmp", path)
    log(step="renew", token=oauth["accessToken"], refresh=oauth["refreshToken"])


def swapaccount(session, name):
    with open(os.environ["FAKE_STATE"], encoding="utf-8") as handle:
        state = json.load(handle)
    body = {"session": session, "account": name, "cwd": os.getcwd()}
    if os.environ.get("LIMITSWITCHER_WINDOW"):
        body["window"] = os.environ["LIMITSWITCHER_WINDOW"]
    request = Request(state["url"].rsplit("/", 1)[0] + "/swapaccount", data=json.dumps(body).encode(), method="POST",
                      headers={"Authorization": "Bearer " + state["token"], "Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=60) as response:
        answer = json.load(response)
    if isinstance(answer.get("window"), str) and str(answer.get("baseUrl", "")).startswith("http://127.0.0.1:"):
        os.environ["ANTHROPIC_BASE_URL"] = answer["baseUrl"]  # the mod's $.env.set: this process, from now on
        os.environ["LIMITSWITCHER_WINDOW"] = answer["window"]
    log(step="swapaccount", text=answer.get("text"))


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
        elif name == "swapaccount":
            swapaccount(session, arg)
        elif name == "sleep":
            time.sleep(float(arg))
        elif name == "exit":
            break
    log(step="exit")


if __name__ == "__main__":
    main()
