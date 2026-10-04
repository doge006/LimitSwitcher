"""Real accounts: saved logins, live usage, switching and automatic failover.

How it works
- Whatever Claude Code / Codex login is active on this PC is imported automatically, so
  signing in to another account (in the CLI, or via "Add account") adds it here.
- Claude: switching saves the outgoing account's latest tokens, then writes the chosen
  account's login into the files Claude Code reads; running sessions pick it up on their next
  request. AFK: a StopFailure hook asks claude_limit() what to do when a turn hits a limit.
- Codex: while the app runs, Codex sends its requests through the local router
  (codex_proxy.py), so switching just changes the account the router uses; every session
  follows on its next request, and a request that hits a usage limit is retried on another
  account. When the app quits, the chosen account is written into ~/.codex/auth.json.
- Usage is fetched from each provider's own usage endpoint (read-only; it does not use any
  quota). To stay well clear of the endpoints' rate limits, each account has its own schedule:
  the account in use every 5 minutes (3 when close to a limit), others every 15 minutes or
  just after one of their windows resets. Known reset times are applied locally in between,
  requests are spaced out, and 429s back off exponentially (up to an hour).
- Subscription renewal / end dates are checked at most once a day per account; a date you
  enter by hand always wins.
- Tokens in the official login files belong to the official clients and are never rotated
  here. Other saved accounts are refreshed here when needed (for usage checks, and by the
  Codex router just before their access token expires).
"""
import contextlib
import faulthandler
import json
import logging
import math
import os
from pathlib import Path
import random
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import processes
from .core import Account, Router
from .providers import PROVIDERS, ProviderError, _jwt_payload
from .vault import Vault, atomic_write, data_dir

# Near-live usage for the accounts in use, gently for the rest. Each account also has a pace
# (1 = normal) that doubles when the provider rate limits it and eases back after successes,
# so the app settles at whatever rate the provider accepts.
ACTIVE_INTERVAL, URGENT_INTERVAL, IDLE_INTERVAL = 90, 45, 300  # idle: may be in use in a cloud session or elsewhere
FRESH_ENOUGH = 45           # opening the panel refreshes only data older than this
STALE_AFTER = 1800          # older numbers are shown with their age
MAX_PACE = 8
# Claude: every minute (asked the way Claude Code asks, see providers.CLAUDE_CODE_AGENT); the
# account in use follows live through Claude Code's status line instead when it's on.
PROVIDER_INTERVALS = {"claude": (60, 60)}   # (in use, near a limit) when not live
# Accounts not in use: Claude every minute too (and just after a window resets); they may be in
# use on another computer, and nothing else reports that. That computer may check them too: a rate
# limit slows an account down, at most 3x (IDLE_PACE_CAP).
PROVIDER_IDLE = {"claude": 60}
IDLE_PACE_CAP = 3
UI_FRESH_IDLE = 300         # opening the panel or the full view refreshes accounts not in use older than this
LIVE_FRESH = 900            # status line data this recent counts as live
LIVE_API_INTERVAL = 1800    # while live, the API only fills in the rest (model limits, credits)
SWAP_SETTLE = 20            # status line reports right after a switch may still be the old account
MANUAL_MIN_GAP = 30         # the Refresh button cannot hammer the API
STARTUP_DELAY = 4           # the first usage check waits for the network and the sign-in files to settle at boot
SPACING = 1.5               # seconds between consecutive API calls
SUBSCRIPTION_INTERVAL = 86400
SUBSCRIPTION_LOGIC = 2      # bump when detection changes, so every account is re-checked
MAX_BACKOFF = 3600
WAITING_LOOK = 20           # while the client renews the login in use: how often its login file is looked at
SIGNED_OUT_AFTER = 300      # the client hasn't replaced a refused login in this long: it was signed out, not renewing
WINDOW_KEYS = {300: ("five_hour", "5-hour"), 10080: ("weekly", "Weekly"), 43200: ("monthly", "30-day")}
NEAR_RESET = 15 * 60     # seconds: a 5-hour limit that resets this soon is waited out, not swapped away from
PENDING_TTL = 6 * 3600   # a continue nobody answered is forgotten (the hook gives up after this too)
LARGE_CONTEXT = 400_000  # tokens (about 6% of a plan's 5-hour usage at ~10% per 700k): costly to load on an account that hasn't cached it
AFK_NOTE = "The usage limit was reached, so the session moved to another account. Continue exactly where you left off."
AFK_COMPACTED = (" Some older tool outputs in this conversation were shortened to save tokens on the new account; each says what it "
                 "held. Re-run the tool before relying on exact details from one of them.")
JEV_WAIT = 240           # seconds a session's Jev compaction may take (3 tries 30 s apart) before it goes on without
JEV_SHOWN = 120          # seconds the status line says a compaction saved something, after it
JEV_AGAIN = 900          # a session compacted (or tried) this recently is not compacted again
MOD_SESSION_FRESH = 120  # a session's limit-status mod reported this recently: it is there to compact
AFK_RESUMED = "The usage limit has reset. Continue exactly where you left off."



def login_mark(secret):
    """A short fingerprint of a saved login's refresh token (never the token itself), to tell
    one saved login from the next: a login the provider refused is not tried again."""
    import hashlib
    secret = secret or {}
    token = (((secret.get("credentials") or {}).get("claudeAiOauth") or {}).get("refreshToken")
             or (((secret.get("auth") or {}).get("tokens") or {}).get("refresh_token")) or "")
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""

SLOW_REFRESH = 180  # seconds: a usage refresh this slow writes where every thread is stuck into app.log
_trace = []


def _trace_file():
    """app.log, opened once for the stack dump of a stuck refresh (None when it can't be opened)."""
    if not _trace:
        try:
            _trace.append(open(data_dir() / "app.log", "a", encoding="utf-8"))
        except OSError:
            _trace.append(None)
    return _trace[0]


@contextlib.contextmanager
def watched(seconds=SLOW_REFRESH):
    """Around one usage refresh: if it hasn't finished after `seconds`, every thread's stack goes into
    app.log (so a refresh that hangs shows where), and a slow one is logged when it ends."""
    handle = _trace_file()
    if handle is not None:
        try:
            faulthandler.dump_traceback_later(seconds, repeat=False, file=handle)
        except (OSError, ValueError, RuntimeError):
            handle = None
    started = time.monotonic()
    try:
        yield
    finally:
        if handle is not None:
            faulthandler.cancel_dump_traceback_later()
        took = time.monotonic() - started
        if took > 60:
            logging.getLogger("account_switcher").warning("usage refresh took %d s", took)


def level_color(left):
    """Green with plenty left, yellow in the middle, red when low (the full view's thresholds)."""
    return "good" if left > 30 else "warn" if left > 10 else "bad"


def report_key(windows):
    """Usage numbers [(window minutes, used %, reset)] in a form two reports of the same numbers share."""
    return tuple(sorted((minutes, round(used), round(reset / 60) if reset else None) for minutes, used, reset in windows))


class StatusLine(str):
    """The line for Claude Code's status line, as plain text, with its coloured pieces in `parts`:
    [{"t": text, "c": colour name or None}] (dim, label, good, warn, bad)."""

    def __new__(cls, groups):
        parts = []
        for index, group in enumerate(groups):
            if index:
                parts.append({"t": " · ", "c": "dim"})
            parts.extend({"t": text, "c": color} for text, color in group)
        line = super().__new__(cls, "".join(piece["t"] for piece in parts))
        line.parts = parts
        return line


class LiveAccounts:
    def __init__(self, notify=lambda *_: None, vault=None, providers=None):
        self.notify = notify
        self.vault = vault or Vault()
        self.providers = providers or {name: cls() for name, cls in PROVIDERS.items()}
        self.lock = threading.RLock()
        meta = self.vault.load_meta()
        # Every saved setting comes back (panel size, taskbar view and its display, ...), not just these.
        for entry in (meta.get("accounts") or {}).values():  # a hiccup's back-off doesn't outlive the app
            if entry.get("backoffKind") == "transient":
                entry.update(backoffUntil=0.0, backoffFailures=0, backoffKind=None)
            entry.pop("waitingMark", None)  # the first check after a restart asks again
            entry.pop("waitingSince", None)
            entry["pace"] = 1.0  # a slower pace from rate limits starts over (a real Retry-After still holds)
        self.meta = {**meta, "accounts": meta.get("accounts", {}), "autoSwap": meta.get("autoSwap", True),
                     "afk": meta.get("afk", False), "selected": meta.get("selected", {})}
        self.active = {}
        self.live_ids = {}       # provider -> account in the official login file
        self.live_since = {}     # provider -> when the account in the login file last changed
        self.routed = set()      # providers whose requests go through the local router
        self.token_locks = {}
        self.afk_sessions = {}   # Claude session -> {"continues": [times], "waiting": bool}
        self.afk_lock = threading.Lock()  # one limit report at a time (several hooks can ask at once)
        self.pending_resumes = {}  # Claude session -> {"tokens", "at", "status": pending|approved|declined}
        self.afk_continues = []  # every session's continues (times): a cap that no session id can dodge
        self.session_reports = {}  # Claude session -> its last status line numbers
        self.session_moved_at = 0.0  # when a session's numbers last moved (it got a reply)
        self.stale_reports = set()  # numbers from before the last Claude login change (see _login_changed)
        self.mod_sessions = {}   # Claude session -> when its limit-status mod last reported
        self.compactions = {}    # Claude session -> its Jev compaction before a swap (see claude_limit)
        self.signatures = {}
        self.last_refresh = 0.0
        self.last_manual = 0.0
        self.spacing = SPACING
        self.on_new_account = lambda: None
        self.on_limit = lambda: None   # a client reported a limit: fetch fresh usage soon
        self.on_swap = lambda provider: None
        self.logins = {}   # provider -> running login process info

    # ---------- account list ----------
    def accounts(self):
        with self.lock:
            rows = []
            now = time.time()
            for account_id, m in self.meta["accounts"].items():
                status = m.get("status", "")
                if status.startswith("Rate limited") and m.get("backoffUntil", 0.0) > now:
                    # The retry time in the current clock setting (it may have changed since)
                    status = f"Rate limited by {m['provider'].title()} · retrying at " + \
                        clock_text(m["backoffUntil"], self.meta.get("clock24"))
                rows.append(Account(account_id, m["provider"], m.get("email") or m["identity"], 0, 0, 0, 0,
                                    plan=m.get("plan", ""), email=m.get("email", ""),
                                    usage=project(m.get("usage") or [], now),
                                    status=status, updated_at=m.get("updatedAt", 0.0),
                                    subscription=subscription_view(m), credits=m.get("credits")))
            order = {"claude": 0, "codex": 1}
            return sorted(rows, key=lambda a: (order.get(a.provider, 9), a.email or a.alias))

    def save(self):
        self.vault.save_meta(self.meta)

    def find(self, provider, identity):
        return next((i for i, m in self.meta["accounts"].items()
                     if m["provider"] == provider and m["identity"] == identity), None)

    # ---------- live login import ----------
    def sync_live(self, force=False):
        """Import the login each official client is using now; cheap when nothing changed."""
        changed = False
        with self.lock:
            for name, provider in self.providers.items():
                signature = provider.signature()
                if not force and self.signatures.get(name) == signature and name in self.active:
                    continue
                self.signatures[name] = signature
                login = provider.read_live()
                if login is None:
                    self.live_ids.pop(name, None)
                    if name not in self.routed:
                        changed |= self.active.pop(name, None) is not None
                    continue
                account_id = self.adopt(name, login)
                previous, self.live_ids[name] = self.live_ids.get(name), account_id
                if previous != account_id:
                    self._login_changed(name, previous)
                    if previous is not None:  # a switch by the app itself sets live_ids directly
                        logging.getLogger("account_switcher").warning(
                            "%s's login changed to %s outside LimitSwitcher", name.title(), login.email or "?")
                # Routed: the router decides; only a different login in the file (the user
                # signed in to another account) changes the account in use.
                follow = name not in self.routed or previous != account_id or name not in self.active
                if follow and self.active.get(name) != account_id:
                    self.active[name] = account_id
                    changed = True
            if changed:
                self.save()
        return changed

    def adopt(self, provider, login, source="the official client (signed in or renewed there)"):
        """Store (or update) a login; returns its account id."""
        account_id = self.find(provider, login.identity)
        if account_id is None:
            account_id = uuid.uuid4().hex[:12]
            self.meta["accounts"][account_id] = {"provider": provider, "identity": login.identity,
                                                 "addedAt": time.time(), "usage": []}
            self.notify("log", f"Added {provider.title()} account {self.shown_name(account_id)}")
            self.on_new_account()  # fetch its usage now rather than at the next scheduled check
        entry = self.meta["accounts"][account_id]
        entry.update(email=login.email or entry.get("email", ""), plan=login.plan or entry.get("plan", ""))
        try:
            before = login_mark(self.vault.read_secret(account_id))
        except (OSError, ValueError):
            before = ""
        if login_mark(login.secret) != before:  # where each saved login came from, for "sign in again" puzzles
            logging.getLogger("account_switcher").warning(
                "%s login of %s saved from %s", provider.title(), login.email or login.identity, source)
            entry.pop("deadLogin", None)
            entry.pop("waitingMark", None)
            entry.pop("waitingSince", None)
            if entry.get("status", "").startswith(("Waiting for", "Signed out")):
                entry.update(status="", attemptedAt=0.0)  # the client renewed it: check it now
        self.vault.write_secret(account_id, login.secret)
        if entry.get("status", "").startswith("Login expired"):
            entry["status"] = ""
        return account_id

    # ---------- usage ----------
    def due(self, account_id, meta, is_active, now):
        """Wall-clock time this account's usage should next be fetched."""
        updated = meta.get("updatedAt", 0.0)
        last = max(updated, meta.get("attemptedAt", 0.0))
        held = meta.get("backoffUntil", 0.0)   # rate limited: not before this
        pace = meta.get("pace", 1.0)
        if not last:
            return held                     # never fetched: now
        if meta.get("waitingMark"):
            return last + WAITING_LOOK  # only the login file is looked at (refresh skips the API call)
        if meta.get("status") or not updated:
            return max(held, last + max(ACTIVE_INTERVAL * pace, 300))   # failing: retry gently, never in a loop
        if is_active:
            if now - meta.get("liveAt", 0.0) < LIVE_FRESH:  # followed live: the API only fills in the rest
                return max(held, meta.get("apiAt", updated) + LIVE_API_INTERVAL)
            usage = project(meta.get("usage") or [], now)
            near = any(w["scope"] == "account" and w["used"] >= 90 for w in usage)
            active, urgent = PROVIDER_INTERVALS.get(meta.get("provider"), (ACTIVE_INTERVAL, URGENT_INTERVAL))
            return max(held, meta.get("apiAt", updated) + (urgent if near else active) * pace)
        # Inactive: usage only changes when a window resets (or if used elsewhere).
        resets = [w["resetsAt"] + 30 for w in meta.get("usage") or [] if w.get("resetsAt") and w["resetsAt"] > updated]
        idle = PROVIDER_IDLE.get(meta.get("provider"), IDLE_INTERVAL) * min(pace, IDLE_PACE_CAP)
        return max(held, min([updated + idle] + resets))

    def refresh(self, only=None, force=False, max_age=None):
        """Fetch usage for accounts that are due (or all/one when forced). Network calls happen
        outside the lock and are spaced out so bursts never hit the provider."""
        self.sync_live()
        now = time.time()
        with self.lock:
            targets = []
            for i, m in self.meta["accounts"].items():
                if only is not None and i != only:
                    continue
                is_active = i == self.active.get(m["provider"])
                is_live = i == self.live_ids.get(m["provider"])
                if max_age is not None:
                    due = m.get("updatedAt", 0.0) + (max_age if is_active else max(max_age, UI_FRESH_IDLE))
                else:
                    due = 0.0 if (force or only) else self.due(i, m, is_active, now)
                held = m.get("backoffUntil", 0.0) > now
                if held and force and m.get("backoffKind") != "rate":
                    held = False  # Refresh retries after a hiccup (offline, timeout); only a real 429 holds
                if due <= now and not held:
                    targets.append((i, dict(m), is_active, is_live))
        # The accounts in use first: their numbers matter now; the rest can follow a few seconds later.
        targets.sort(key=lambda target: (not target[2], not target[3]))
        fetched = False
        subscriptions = []
        for account_id, meta, is_active, is_live in targets:
            provider = self.providers.get(meta["provider"])
            # One renewal at a time per login, reading the saved login inside the lock: a renewal
            # that ran just before (Auto resume's check, the Codex router) leaves its new tokens here,
            # and renewing again with the old single-use token would kill the login.
            with self.token_locks.setdefault(account_id, threading.Lock()):
                try:
                    secret = self.vault.read_secret(account_id)
                except (OSError, ValueError):
                    secret = None
                if provider is None or secret is None:
                    self._set(account_id, status="Saved login missing; sign in again")
                    continue
                if meta.get("deadLogin") and meta["deadLogin"] == login_mark(secret) and not force:
                    continue  # refused before, and nothing has replaced it: only a new sign-in (or Refresh) tries again
                if meta.get("waitingMark") and meta["waitingMark"] == login_mark(secret) and not force:
                    # The client hasn't renewed it yet: nothing to ask the API. A client renews within
                    # seconds; one that hasn't after minutes was signed out (the login was revoked).
                    since = meta.get("waitingSince") or 0.0
                    if since and time.time() - since > SIGNED_OUT_AFTER and not meta.get("status", "").startswith("Signed out"):
                        client = "Claude Code" if meta["provider"] == "claude" else meta["provider"].title()
                        self._set(account_id, status=f"Signed out · sign in to {client} again")
                    continue
                if fetched:
                    time.sleep(self.spacing)
                fetched = True
                self._set(account_id, attemptedAt=time.time())
                try:
                    # Never rotate the tokens in the official login file: the client owns those.
                    # A renewed login is saved at once: the old refresh token is spent, and the usage
                    # call after it may still fail.
                    windows, plan, updated = provider.fetch(secret, allow_refresh=not (is_active or is_live),
                                                            save=lambda renewed, i=account_id: self.vault.write_secret(i, renewed))
                except ProviderError as error:
                    if error.rate_limited:
                        # Wait what the provider asks (Retry-After), else back off exponentially;
                        # a little jitter, and a slower pace from now on. Kept across restarts.
                        failures = meta.get("backoffFailures", 0)
                        wait = error.retry_after or min(MAX_BACKOFF, 60 * 2 ** failures)
                        wait = min(MAX_BACKOFF, max(30, wait)) * random.uniform(1.0, 1.15)
                        self._set(account_id, backoffUntil=time.time() + wait, backoffFailures=failures + 1,
                                  backoffKind="rate", pace=min(MAX_PACE, meta.get("pace", 1.0) * 2))
                        logging.getLogger("account_switcher").warning(
                            "%s usage check for %s (%s) rate limited: next try in %d min",
                            meta["provider"], meta.get("email") or account_id, "in use" if is_active else "not in use", wait / 60)
                    if error.transient:
                        # A hiccup (503, timeout, offline): keep the numbers and say nothing; retry after
                        # 1, 2, 4... min. Only a problem that lasts gets shown.
                        failures = meta.get("backoffFailures", 0)
                        wait = min(900, 60 * 2 ** failures) * random.uniform(1.0, 1.15)
                        self._set(account_id, backoffUntil=time.time() + wait, backoffFailures=failures + 1,
                                  backoffKind="transient")
                        if failures + 1 < 3:
                            continue
                        self._set(account_id, status=f"{meta['provider'].title()}'s usage service isn't answering · retrying")
                        continue
                    message = str(error)
                    if error.rate_limited:  # temporary: say until when
                        message = f"Rate limited by {meta['provider'].title()} · retrying at " + \
                            clock_text(time.time() + wait, self.meta.get("clock24"))
                    if error.relogin and (is_active or is_live):
                        message = "Waiting for Claude Code" if meta["provider"] == "claude" else f"Waiting for {meta['provider'].title()}"  # short: it must fit a card's hint line
                        if meta.get("waitingMark") != login_mark(secret):
                            self._set(account_id, waitingMark=login_mark(secret), waitingSince=time.time())
                    elif error.relogin:
                        self._set(account_id, deadLogin=login_mark(secret))
                    self._set(account_id, status=message)
                    continue
                except Exception as error:  # a malformed response must not stop the loop
                    self._set(account_id, status=f"Usage unavailable ({type(error).__name__})")
                    continue
                eased = {"pace": max(1.0, meta.get("pace", 1.0) * 0.5)} if meta.get("pace", 1.0) > 1 else {}
                self._set(account_id, backoffUntil=0.0, backoffFailures=0, backoffKind=None, waitingMark=None, waitingSince=None, **eased)
                if updated is not None:
                    self.vault.write_secret(account_id, updated)
                    secret = updated
            windows = self.keep_live(account_id, windows)
            self._set(account_id, usage=windows, plan=plan or meta.get("plan", ""), status="", updatedAt=time.time(), apiAt=time.time(),
                      credits=getattr(provider, "last_credits", None))
            self.record_fields(meta["provider"] + "-usage", getattr(provider, "last_fields", None))
            subscriptions.append((account_id, meta, provider, secret))
            if len(targets) > 1:
                self.notify("accounts", None)  # show each account as it arrives, not after all of them
        for account_id, meta, provider, secret in subscriptions:  # renewal dates after every account's usage
            self.check_subscription(account_id, meta, provider, secret)
        self.last_refresh = time.monotonic()
        with self.lock:
            self.save()
        self.notify("accounts", None)

    def check_subscription(self, account_id, meta, provider, secret):
        """Renewal / end date, at most once a day, never when a manual date is set."""
        fresh = time.time() - meta.get("subscriptionCheckedAt", 0) < SUBSCRIPTION_INTERVAL
        if meta.get("subscriptionManual") or (fresh and meta.get("subscriptionLogic") == SUBSCRIPTION_LOGIC):
            return
        if not hasattr(provider, "subscription"):
            return
        time.sleep(self.spacing)
        estimated = False
        try:
            result = provider.subscription(secret)
            at, ends, paths = result[:3]
            estimated = bool(result[3]) if len(result) > 3 else False
        except Exception:
            at, ends, paths = None, None, []
        self._set(account_id, subscription={"at": at, "ends": ends, "estimated": estimated} if at else None,
                  subscriptionCheckedAt=time.time(), subscriptionLogic=SUBSCRIPTION_LOGIC)
        self.record_fields(meta["provider"], paths)

    def record_fields(self, provider, paths):
        """Keep the *names* of fields the account endpoints return (no values), so renewal
        detection can be matched to what the provider actually sends."""
        if not paths:
            return
        path = self.vault.root / "subscription-fields.json"
        try:
            known = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = {}
        known[provider] = sorted(set(known.get(provider, [])) | set(paths))[:400]
        try:
            atomic_write(path, json.dumps(known, indent=2).encode())
        except OSError:
            pass

    def set_subscription(self, account_id, at, ends):
        """Manual renewal / end date from the full view; at=None clears it."""
        with self.lock:
            entry = self.meta["accounts"].get(account_id)
            if entry is None:
                raise ValueError("Unknown account")
            if at is None:
                entry.pop("subscriptionManual", None)
                entry["subscriptionCheckedAt"] = 0  # look it up again
            else:
                entry["subscriptionManual"] = {"at": float(at), "ends": bool(ends)}
            self.save()
        self.notify("accounts", None)

    def next_delay(self):
        """Seconds until the earliest account is due (bounded), so the loop sleeps in between."""
        now = time.time()
        with self.lock:
            dues = [self.due(i, m, i == self.active.get(m["provider"]), now) for i, m in self.meta["accounts"].items()]
        return max(20.0, min([ACTIVE_INTERVAL] + [d - now for d in dues]))

    def _set(self, account_id, **fields):
        with self.lock:
            if account_id in self.meta["accounts"]:
                self.meta["accounts"][account_id].update(fields)

    # ---------- switching ----------
    def swap(self, account_id, reason="manual"):
        with self.lock:
            target = self.meta["accounts"].get(account_id)
            if target is None:
                raise ValueError("Unknown account")
            name = target["provider"]
            logging.getLogger("account_switcher").warning(
                "switching %s to %s (%s)", name.title(), target.get("email") or "?", reason)
            provider = self.providers[name]
            if self.active.get(name) == account_id and self.live_ids.get(name) == account_id:
                return
            secret = self.vault.read_secret(account_id)
            if secret is None:
                raise RuntimeError("This account's saved login is missing; sign in again")
            # Claude Code's own locks: a renewal of the outgoing login finishes first (and is
            # saved below), and none starts in the middle of the switch (claude_locks.py).
            with (provider.locked() if hasattr(provider, "locked") else contextlib.nullcontext()):
                self.sync_live(force=True)  # saves the outgoing account's newest tokens first
                # Write the login file too: new sessions (and Codex's own /status) then show the
                # chosen account even without the router; the router covers sessions already open.
                if self.live_ids.get(name) != account_id:
                    before = provider.read_live()
                    provider.write_live(secret)
                    after = provider.read_live()
                    if after is None or after.identity != target["identity"]:
                        if before is not None:
                            provider.write_live(before.secret)  # put things back exactly as they were
                        raise RuntimeError("Switch could not be verified; your previous login was restored")
                    self.signatures[name] = provider.signature()
                    previous = self.live_ids.get(name)
                    self.live_ids[name] = account_id
                    self._login_changed(name, previous)
            self.active[name] = account_id
            if name in self.routed:
                self.meta["selected"][name] = account_id
            self.save()
        self.on_swap(name)
        if reason == "quiet":
            return
        who = self.shown_name(account_id)  # its name in name mode, never the email
        self.notify("log", f"{name.title()} now uses {who}" + (" (automatic)" if reason != "manual" else ""))
        self.notify("accounts", None)

    # ---------- routing (Codex through the local router) ----------
    def enable_routing(self, name):
        with self.lock:
            self.routed.add(name)
            chosen = self.meta["selected"].get(name)
            if chosen in self.meta["accounts"] and self.meta["accounts"][chosen]["provider"] == name:
                self.active[name] = chosen
            elif name in self.active:
                self.meta["selected"][name] = self.active[name]
            self.save()
        self.notify("accounts", None)

    def disable_routing(self, name):
        """Stop routing; write the chosen account into the login file so Codex keeps using it."""
        with self.lock:
            chosen = self.active.get(name)
            self.routed.discard(name)
            self.sync_live(force=True)
            if chosen and chosen != self.active.get(name) and chosen in self.meta["accounts"]:
                try:
                    self.swap(chosen, reason="quiet")
                except (RuntimeError, ValueError, OSError):
                    pass

    def route(self, name):
        with self.lock:
            return self.active.get(name) if name in self.routed else None

    def credentials(self, account_id):
        """Current tokens for a routed account: the official login file's for the account it
        holds (Codex keeps those fresh), our saved copy (refreshed here) for the others."""
        with self.lock:
            entry = self.meta["accounts"].get(account_id)
            live = account_id == self.live_ids.get(entry["provider"]) if entry else False
        if entry is None:
            raise ValueError("Unknown account")
        provider = self.providers[entry["provider"]]
        if live:
            login = provider.read_live()
            if login is not None and login.identity == entry["identity"]:
                tokens = login.secret["auth"]["tokens"]
                return tokens["access_token"], tokens.get("account_id")
        with self.token_locks.setdefault(account_id, threading.Lock()):
            secret = self.vault.read_secret(account_id)
            if secret is None:
                raise RuntimeError("Saved login missing")
            tokens = secret["auth"]["tokens"]
            expires = _jwt_payload(tokens.get("access_token")).get("exp")
            if isinstance(expires, (int, float)) and expires - time.time() < 300:
                secret = provider.refresh(secret, "Codex router, saved login about to expire")
                self.vault.write_secret(account_id, secret)
                tokens = secret["auth"]["tokens"]
            return tokens["access_token"], tokens.get("account_id")

    def limit_hit(self, account_id, resets_at=None):
        """A routed request found this account out of quota. Mark it, and (with Auto swap)
        move to the account with the most headroom. Returns the account to retry on, or None."""
        with self.lock:
            entry = self.meta["accounts"].get(account_id)
            if entry is None:
                return None
            name = entry["provider"]
            if self.active.get(name) != account_id:
                return self.active.get(name)  # a parallel request already moved on
            usage = [dict(w) for w in entry.get("usage") or []]
            window = next((w for w in usage if w["key"] == "five_hour"), None)
            if window is None:
                window = {"key": "five_hour", "label": "5-hour", "scope": "account"}
                usage.insert(0, window)
            window.update(used=100.0, resetsAt=float(resets_at) if resets_at else window.get("resetsAt"))
            entry["usage"] = usage
            auto = self.meta["autoSwap"]
        self.on_limit()
        best = self._best_other(name, account_id, allow_unknown=True) if auto else None
        if best is None:
            self.notify("accounts", None)
            return None
        self.swap(best.id, reason="auto")
        return best.id

    def mark_used_up(self, account_id):
        """A turn just ended on a usage limit: show the account's fullest window as used up (the
        5-hour one when nothing is known), until real numbers say otherwise."""
        with self.lock:
            entry = self.meta["accounts"].get(account_id)
            if entry is None:
                return
            usage = [dict(w) for w in entry.get("usage") or []]
            windows = [w for w in usage if w.get("scope", "account") == "account"]
            if any(w.get("used", 0) >= 100 for w in windows):
                return
            window = max(windows, key=lambda w: w.get("used", 0), default=None)
            if window is None:
                window = {"key": "five_hour", "label": "5-hour", "scope": "account", "resetsAt": None}
                usage.insert(0, window)
            window["used"] = 100.0
            entry["usage"] = usage
            entry.pop("liveAt", None)  # and ask the API again at its normal pace
        self.notify("accounts", None)

    def usable(self, account_id):
        account = next((a for a in self.accounts() if a.id == account_id), None)
        return account is not None and account.eligible and not account.status

    def turn_start(self, account_id):
        """A routed session starts a new turn. If its account is already used up, move now
        (between turns) rather than letting the first request of the turn fail."""
        if not self.meta["autoSwap"]:
            return account_id
        account = next((a for a in self.accounts() if a.id == account_id), None)
        if account is None or account.eligible:
            return account_id
        best = self._best_other(account.provider, account_id)
        if best is None:
            return account_id
        self.swap(best.id, reason="auto")
        return best.id

    def keep_live(self, account_id, windows):
        """The API's windows, except where Claude Code's status line reported more use of the same
        window since: usage never goes down within a window, and the usage API can lag behind the
        live numbers, which would otherwise step back for a while."""
        with self.lock:
            entry = self.meta["accounts"].get(account_id) or {}
            if time.time() - entry.get("liveAt", 0) > LIVE_FRESH:
                return windows
            ours = {w["key"]: w for w in entry.get("usage") or []}
        merged = []
        for window in windows:
            live = ours.get(window.get("key"))
            if live and window.get("resetsAt") is not None and live.get("resetsAt") is not None \
                    and abs(live["resetsAt"] - window["resetsAt"]) < 120 \
                    and window.get("used", 0) < live.get("used", 0) < 100:
                window = dict(window, used=live["used"])
            merged.append(window)
        return merged

    def observe(self, account_id, windows, add=False):
        """Live usage the service reported with a response: [(window minutes, used %, reset)].
        add: also windows the account doesn't show yet (the source is certain of them)."""
        changed = False
        with self.lock:
            entry = self.meta["accounts"].get(account_id)
            if entry is None:
                return
            usage = [dict(w) for w in entry.get("usage") or []]
            for minutes, used, reset in windows:
                key = WINDOW_KEYS.get(minutes, (f"window-{minutes * 60}",))[0]
                window = next((w for w in usage if w["key"] == key), None)
                if window is None and add and minutes in WINDOW_KEYS:
                    window = {"key": key, "label": WINDOW_KEYS[minutes][1], "used": 0.0, "resetsAt": None, "scope": "account"}
                    usage.append(window)
                    changed = True
                if window is None or minutes <= 0:
                    continue  # only windows the account really has (headers may report others)
                same_window = reset is not None and window.get("resetsAt") is not None \
                    and abs(window["resetsAt"] - reset) < 120
                if same_window and used < window.get("used", 0.0):
                    continue  # usage never goes down within a window: this report is older than what we have
                if round(window.get("used", -1)) != round(used) or window.get("resetsAt") != reset:
                    changed = True
                window.update(used=float(used), resetsAt=reset)
            entry["usage"] = usage
            entry["updatedAt"] = time.time()
        if changed:
            self.notify("accounts", None)
        return changed

    def _login_changed(self, name, previous):
        """The account in a client's login file changed (under the lock). Claude Code sessions keep
        showing the numbers of their last reply until they get a new one, so every report they made
        so far, and the outgoing account's own numbers, are the old account's: a report still
        carrying them never counts for the new one (a newly added account showed the old one's
        0% left until it was used)."""
        self.live_since[name] = time.time()
        if name != "claude":
            return
        stale = set(self.session_reports.values())
        outgoing = self.meta["accounts"].get(previous) if previous else None
        if outgoing:
            minutes = {"five_hour": 300, "weekly": 10080}
            stale.add(report_key((minutes[w["key"]], w.get("used", 0.0), w.get("resetsAt"))
                                 for w in outgoing.get("usage") or [] if w.get("key") in minutes))
        self.stale_reports = stale

    def statusline(self, limits, session=None, model=None, effort=None):
        """Live usage from a Claude Code status line (rate_limits), for the account signed in to
        Claude Code. Returns the compact status line text.

        Every open Claude Code session reports the numbers from its own last reply, also long
        after it (an idle session may still hold another account's numbers). So a session's report
        counts only once its numbers moved since its previous one, i.e. it just got a reply. A
        session's first report counts only while no other session is busy. Otherwise an old
        session and a fresh one take turns and the bar jumps between their numbers."""
        name = "claude"
        self.sync_live()
        account_id = self.live_ids.get(name)
        now = time.time()
        entry = self.meta["accounts"].get(account_id) if account_id else None
        if entry is None:
            return None
        settled = now - self.live_since.get(name, 0.0) >= SWAP_SETTLE
        if isinstance(limits, dict) and settled:
            windows = []
            for key, minutes in (("five_hour", 300), ("seven_day", 10080)):
                window = limits.get(key)
                if isinstance(window, dict) and isinstance(window.get("used_percentage"), (int, float)):
                    reset = window.get("resets_at")
                    windows.append((minutes, float(window["used_percentage"]),
                                    float(reset) if isinstance(reset, (int, float)) else None))
            # Claude Code repeats its last numbers with every reply, also after a limit when it gets
            # no new ones. Only a report that changes something counts as live; otherwise the API
            # goes back to its normal pace and catches what the status line misses.
            key = report_key(windows)
            fresh = key not in self.stale_reports  # the previous account's numbers (see _login_changed)
            if session is not None:
                with self.lock:
                    previous = self.session_reports.get(session)
                    self.session_reports[session] = key
                    if len(self.session_reports) > 200:  # sessions come and go
                        self.session_reports.pop(next(iter(self.session_reports)))
                    if previous is None:  # a session's first report: fine unless another one is busy now
                        fresh = fresh and now - self.session_moved_at >= LIVE_FRESH
                    else:
                        fresh = fresh and previous != key
                    if fresh and previous is not None:
                        self.session_moved_at = now
            if windows and fresh and self.observe(account_id, windows, add=True):
                with self.lock:
                    entry["liveAt"] = now
                    if entry.get("status", "").startswith("Rate limited"):
                        entry["status"] = ""  # live numbers: the API's rate limit no longer matters
        compacting = bool(session) and self.compacting(session)
        # While Jev compacts the icon turns yellow and "Jev compacting…" follows the app's name.
        groups = [[("⇄", "warn" if compacting else "good"), (" ", None), ("LimitSwitcher", "dim")]]
        if compacting:
            groups.append([("Jev compacting…", "warn")])
        job = self.compactions.get(session or "")
        if job and job["status"] == "done" and job.get("saved") and now - job.get("finishedAt", 0) < JEV_SHOWN \
                and round(job["saved"] / 1000) > 0:  # what it saved, for a little while after
            groups.append([("Jev compacted ", "dim"), (f"~{round(job['saved'] / 1000)}k", "good"), (" tokens saved", "dim")])
        groups.append([(self.shown_name(account_id), "dim")])
        if model:  # "Opus 5.5 (high)": what the session runs on, as Claude Code reports it
            groups.append([(model[:40], None)] + ([(" ", None), (f"({effort[:12]})", "dim")] if effort else []))
        for window in project(entry.get("usage") or [], now):
            if window.get("scope") == "account" and window["key"] in ("five_hour", "weekly"):
                label = "5h" if window["key"] == "five_hour" else "1w"
                left = max(0, math.floor(100 - window["used"] + 1e-6))  # rounded down
                groups.append([(label, "label"), (" ", None), (f"{left}%", level_color(left)), (" left", "dim")])
        return StatusLine(groups)

    def shown_name(self, account_id):
        """The account as the app shows it: its email, or in name mode its name ("Claude 2" when
        it has none), like every other surface."""
        entry = self.meta["accounts"].get(account_id) or {}
        if not self.meta.get("nameMode"):
            return entry.get("email") or entry.get("identity") or entry.get("provider", "").title()
        if entry.get("label"):
            return entry["label"]
        same = sorted((m.get("email") or m.get("identity") or "", i) for i, m in self.meta["accounts"].items()
                      if m.get("provider") == entry.get("provider"))
        number = next((n for n, (_, i) in enumerate(same, 1) if i == account_id), 1)
        return f"{entry.get('provider', '').title()} {number}"

    def claude_limits(self):
        """The freshest 5-hour and weekly numbers the app has for the account in Claude Code,
        in Claude Code's own rate_limits shape (for the user's own status line command: an idle
        session would otherwise show the numbers from its last reply, however old)."""
        account_id = self.live_ids.get("claude")
        entry = self.meta["accounts"].get(account_id) if account_id else None
        if entry is None:
            return None
        limits = {}
        for window in project(entry.get("usage") or [], time.time()):
            key = {"five_hour": "five_hour", "weekly": "seven_day"}.get(window.get("key"))
            if window.get("scope") == "account" and key:
                limits[key] = {"used_percentage": window["used"], "resets_at": window.get("resetsAt")}
        return limits or None

    def _best_other(self, name, current, allow_unknown=False):
        """The other account with the most headroom. allow_unknown also accepts accounts whose
        usage has not been read yet (after the known ones): a client just hit a limit, and
        trying one is better than stopping."""
        candidates = [a for a in self.accounts() if a.provider == name and a.id != current
                      and a.eligible and not a.status and (a.headroom > 0 or (allow_unknown and a.headroom < 0))]
        return max(candidates, key=lambda a: a.headroom) if candidates else None

    def resets_soon(self, account):
        """True when Claude's only used-up limit is the 5-hour one and it resets within
        NEAR_RESET: switching would spend another account's quota (and its cold cache) to save
        a few minutes. Turned off with the "Wait for a near reset" setting."""
        if account is None or account.provider != "claude" or not self.meta.get("waitNearReset", True):
            return False
        spent = [w for w in account.account_windows() if w["used"] >= 100]
        now = time.time()
        return bool(spent) and all(w["key"] == "five_hour" and w.get("resetsAt") and 0 < w["resetsAt"] - now <= NEAR_RESET
                                   for w in spent)

    def _after_swap(self, session, tokens, afk, now, compacted=False):
        """What the session does once its account was swapped (and compacted, when that was on)."""
        state = self.afk_sessions.setdefault(session or "?", {"continues": [], "waiting": False})
        if afk and self.meta.get("afkSkipLarge", True) and isinstance(tokens, int) and tokens >= LARGE_CONTEXT:
            # Continuing would load the whole session uncached on the new account: ask the user
            # first instead of spending their usage.
            self.pending_resumes[session or "?"] = {"tokens": tokens, "at": now, "seen": now, "status": "pending"}
            self.notify("log", f"Large session (~{tokens // 1000}k tokens) is waiting for your OK to continue on the new account")
            return {"action": "wait", "seconds": 5}
        if not afk:
            return {"action": "stop"}  # the next message goes to the new account
        state["continues"].append(now)
        self.afk_continues.append(now)
        state["waiting"] = False
        return {"action": "continue", "message": AFK_NOTE + (AFK_COMPACTED if compacted else "")}

    # ---------- Jev compaction before a swap (mods/limit-status + mods/jev-compact) ----------
    def note_mod_session(self, session):
        """A session's limit-status mod reported: it is there to run a compaction."""
        if not session:
            return
        with self.lock:
            self.mod_sessions[session] = time.time()
            if len(self.mod_sessions) > 200:
                self.mod_sessions.pop(next(iter(self.mod_sessions)))

    def jev_wanted(self, session, now):
        """Compact this session with Jev before it goes on: the setting is on, both plugins are
        installed, the session's own mod is running, a key is set, and it wasn't just done."""
        if not session or not self.meta.get("jevCompact") or not self.meta.get("jevModInstalled"):
            return False
        if now - self.mod_sessions.get(session, 0.0) > MOD_SESSION_FRESH:
            return False  # an old session that hasn't loaded the mod: nobody would run it
        job = self.compactions.get(session)
        if job is not None and now - job["at"] < JEV_AGAIN:
            return False
        from . import mod
        return mod.jev_key_present(Path(self.vault.root) / "afk-hook.json")

    def compaction_request(self, session):
        """For the session's mod (in its status line report): the compaction to run, if one is due."""
        job = self.compactions.get(session or "")
        if job is None or job["status"] not in ("asked", "running"):
            return None
        job["status"] = "running"
        return {"id": job["id"]}

    def compaction_done(self, session, job_id, outcome, saved=None, reason=None):
        """The session's mod finished the compaction (done), or gave up on it (skipped, failed)."""
        job = self.compactions.get(session or "")
        if job is None or job["id"] != job_id or job["status"] not in ("asked", "running"):
            return False
        job.update(status=outcome if outcome in ("done", "skipped", "failed") else "failed", finishedAt=time.time())
        if outcome == "done" and isinstance(saved, int) and saved > 0:
            job["saved"] = saved
            self.notify("log", f"Jev compaction: ~{round(saved / 1000)}k tokens less for the new account to load")
        elif reason:
            self.notify("log", f"Jev compaction {'skipped' if outcome == 'skipped' else 'failed'}: {str(reason)[:200]}")
        return True

    def compacting(self, session):
        job = self.compactions.get(session or "")
        return job is not None and job["status"] in ("asked", "running") and time.time() - job["at"] < JEV_WAIT

    def resume_decision(self, session, approve):
        """The user's answer to "continue this large session?" (the hook is polling for it)."""
        with self.afk_lock:
            entry = self.pending_resumes.get(session)
            if entry is not None and entry["status"] == "pending":
                entry["status"] = "approved" if approve else "declined"
        self.notify("accounts", None)

    def pending_list(self):
        now = time.time()
        with self.afk_lock:
            self.pending_resumes = {s: e for s, e in self.pending_resumes.items() if now - e["at"] < PENDING_TTL}
            # a hook that stopped asking (its Claude Code was closed) is no longer waiting for an answer
            return [{"session": s, "tokens": e["tokens"], "at": e["at"]}
                    for s, e in self.pending_resumes.items() if e["status"] == "pending" and now - e["seen"] < 60]

    # ---------- AFK (Claude Code) ----------
    def claude_limit(self, session, tokens=None):
        """Called by the StopFailure hook when a Claude turn ended on a usage limit.
        Auto swap moves to another account now; AFK also continues the session (or waits
        for a reset when no account has room). One report at a time: hooks that ask together
        (several waits ending at once) must not all get "continue"."""
        with self.afk_lock:
            answer = self._claude_limit(session, tokens)
        logging.getLogger("account_switcher").warning("auto resume: a limit in session %s -> %s",
                                                      (session or "?")[:8], answer.get("action"))
        return answer

    def _claude_limit(self, session, tokens=None):
        afk, auto = bool(self.meta.get("afk")), bool(self.meta["autoSwap"])
        if not (afk or auto):
            return {"action": "stop"}
        job = self.compactions.get(session or "?")
        if job is not None and not job.get("answered"):
            # The account was swapped and the session is being compacted: it goes on once that is done
            now = time.time()
            if job["status"] in ("asked", "running"):
                if now - job["at"] < JEV_WAIT:
                    return {"action": "wait", "seconds": 3}
                job.update(status="failed", reason="it took too long")
                self.notify("log", "Jev compaction took too long: the session goes on without it")
            if not job.get("restamped"):
                # The compaction rewrote the transcript: the hook must not take that for the session
                # having gone on by itself, so it looks at the transcript afresh before continuing
                job["restamped"] = True
                return {"action": "wait", "seconds": 1, "restamp": True}
            job["answered"] = True
            tokens = job["tokens"]
            if isinstance(tokens, int) and job.get("saved"):
                tokens = max(0, tokens - job["saved"])
            return self._after_swap(session, tokens, job["afk"], now, compacted=job["status"] == "done")
        pending = self.pending_resumes.get(session or "?")
        if pending is not None:  # a large session the user was asked about
            now = pending["seen"] = time.time()
            if pending["status"] == "approved":
                del self.pending_resumes[session or "?"]
                state = self.afk_sessions.setdefault(session or "?", {"continues": [], "waiting": False})
                state["continues"].append(now)
                self.afk_continues.append(now)
                return {"action": "continue", "message": AFK_NOTE}
            if pending["status"] == "declined" or now - pending["at"] > PENDING_TTL:
                del self.pending_resumes[session or "?"]
                return {"action": "stop"}
            return {"action": "wait", "seconds": 5}
        current = self.active.get("claude")
        if current is None:
            return {"action": "stop"}
        now = time.time()
        state = self.afk_sessions.setdefault(session or "?", {"continues": [], "waiting": False})
        state["continues"] = [t for t in state["continues"] if now - t < 600]
        self.afk_continues = [t for t in self.afk_continues if now - t < 600]
        if state["continues"] and now - state["continues"][-1] < 90:
            # Reported again right after continuing: the same limit twice, or the continued turn
            # failed at once (the new account is used up too). Either way: no second wake.
            return {"action": "stop"}
        if len(state["continues"]) >= 3 or len(self.afk_continues) >= 6:  # something keeps failing: do not loop
            return {"action": "wait", "seconds": 900}
        before = (self.meta["accounts"].get(current) or {}).get("apiAt")
        self.refresh(only=current)  # fresh numbers for the account that just hit its limit
        if (self.meta["accounts"].get(current) or {}).get("apiAt") == before:
            self.mark_used_up(current)  # none (rate limited, offline): the limit itself says it's used up
        self.on_limit()
        account = next((a for a in self.accounts() if a.id == current), None)
        hold = auto and self.resets_soon(account)
        if hold:
            self.notify("log", "Not switching: the 5-hour limit resets in "
                        f"{max(1, round((min(w['resetsAt'] for w in account.account_windows() if w['used'] >= 100) - now) / 60))} min")
        best = self.confirmed_other("claude", current) if auto and not hold else None
        if best is not None:
            self.swap(best.id, reason="auto")
            if self.jev_wanted(session, now):
                # The new account has none of the session cached: shrink what it will load first. Jev
                # does it, not Claude, so it works on a used-up account; the swap itself can't wait
                # for it and needn't (the background check may swap any moment), the session can.
                self.compactions[session] = {"id": secrets.token_hex(8), "status": "asked", "at": now,
                                             "tokens": tokens, "afk": afk}
                self.notify("log", "Compacting the session with Jev before it goes on")
                return {"action": "wait", "seconds": 3}
            return self._after_swap(session, tokens, afk, now)
        if afk and self.meta.get("afkSkipLarge", True) and isinstance(tokens, int) and tokens >= LARGE_CONTEXT:
            afk = False  # waiting for the reset would also load the whole session uncached: not by itself
        if not afk:
            return {"action": "stop"}
        account = next((a for a in self.accounts() if a.id == current), None)
        if state["waiting"] and account is not None and all(w["used"] < 100 for w in account.windows()):
            state["continues"].append(now)
            self.afk_continues.append(now)
            state["waiting"] = False
            return {"action": "continue", "message": AFK_RESUMED}
        state["waiting"] = True
        resets = [w["resetsAt"] for a in self.accounts() if a.provider == "claude"
                  for w in a.windows() if w["used"] >= 100 and w.get("resetsAt")]
        wait = min(resets) - now + 30 if resets else 900
        return {"action": "wait", "seconds": max(60, min(wait, 6 * 3600))}

    def confirmed_other(self, name, current):
        """The account to continue on after a limit: the best other one whose numbers were just
        checked (or are under 5 minutes old) and show room. Stale numbers can make an account
        that is used up elsewhere look free, and continuing onto it fails at once."""
        tried = set()
        for _ in range(2):  # the best, and if that turns out used up, the next best
            candidates = [a for a in self.accounts() if a.provider == name and a.id != current and a.id not in tried
                          and a.eligible and (not a.status or a.status.startswith("Rate limited"))]
            if not candidates:
                return None
            best = max(candidates, key=lambda a: a.headroom)
            tried.add(best.id)
            entry = self.meta["accounts"].get(best.id) or {}
            if time.time() - entry.get("updatedAt", 0.0) > 300:
                self.refresh(only=best.id)
                entry = self.meta["accounts"].get(best.id) or {}
            fresh = time.time() - entry.get("updatedAt", 0.0) <= 300 or self.reset_since(entry)
            account = next((a for a in self.accounts() if a.id == best.id), None)
            if fresh and account is not None and account.eligible and account.headroom > 0:
                return account
        return None

    @staticmethod
    def reset_since(entry):
        """The account was used up and that window has reset since its numbers were read: it has
        room again, also when the API can't be asked right now (a 429 after a limit is common)."""
        now = time.time()
        windows = [w for w in entry.get("usage") or [] if w.get("scope", "account") == "account"]
        spent = [w for w in windows if w.get("used", 0) >= 100]
        return bool(spent) and all(w.get("resetsAt") and w["resetsAt"] <= now for w in spent)

    def auto_swap(self):
        """Move off an account that has used up a limit, to the one with the most headroom."""
        if not self.meta["autoSwap"]:
            return []
        moved = []
        accounts = self.accounts()
        for name in self.providers:
            current = next((a for a in accounts if a.id == self.active.get(name)), None)
            if current is None or current.eligible or self.resets_soon(current):
                continue
            best = self._best_other(name, current.id)
            if best is None:
                self.recheck_spent(name, current.id)
                continue
            try:
                self.swap(best.id, reason="auto")
                moved.append(best.id)
            except (RuntimeError, ValueError, OSError) as error:
                self.notify("log", f"Automatic switch failed: {error}")
        return moved

    def recheck_spent(self, name, current_id):
        """No other account has room: before giving up, ask again about the ones that only look used
        up because their numbers are over a minute old (at most once a minute; 429 holds stay)."""
        now = time.time()
        if now - getattr(self, "last_recheck", 0.0) < 60:
            return
        self.last_recheck = now
        stale = [i for i, m in self.meta["accounts"].items() if m["provider"] == name and i != current_id
                 and now - m.get("updatedAt", 0.0) > 60 and m.get("backoffKind") != "rate"]
        for account_id in stale:
            self.refresh(only=account_id)
        if stale and self._best_other(name, current_id) is not None:
            self.auto_swap()

    def remove(self, account_id):
        with self.lock:
            entry = self.meta["accounts"].get(account_id)
            if entry is None:
                raise ValueError("Unknown account")
            if self.active.get(entry["provider"]) == account_id:
                raise RuntimeError("Switch to another account before removing this one")
            del self.meta["accounts"][account_id]
            self.vault.delete_secret(account_id)
            self.save()
        self.notify("accounts", None)

    # ---------- adding accounts ----------
    def add(self, name, expect=None):
        """Run the official sign-in in an isolated folder so the current login is untouched.
        expect: the account being signed back in ("Sign in again"), to say so if another lands."""
        provider = self.providers.get(name)
        if provider is None:
            raise ValueError("Unknown provider")
        if name in self.logins:
            raise RuntimeError(f"A {name.title()} sign-in is already open")
        # The real path (/private/var/... on macOS): Claude Code names its Keychain item after it.
        directory = Path(tempfile.mkdtemp(prefix=f"account-switcher-{name}-")).resolve()
        command, env = provider.login_command(directory)
        if sys.platform == "darwin":
            # In Terminal: it has the user's PATH (an app opened from Finder does not) and a
            # window to sign in from. A .command file, which Terminal runs by itself: telling
            # Terminal what to do (AppleScript) needs a permission macOS silently refuses.
            # The login is picked up from its folder as it lands.
            import shlex
            script = directory / "Sign in.command"
            script.write_text("#!/bin/sh\necho $$ > " + shlex.quote(str(directory / "login.pid")) + "\n"
                              + "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
                              + " ".join(shlex.quote(c) for c in command)
                              + "\necho\necho 'Done. You can close this window.'\n")
            script.chmod(0o700)
            process = subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(script)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            watch_process = False
        else:
            executable = shutil.which(command[0])
            if not executable:
                shutil.rmtree(directory, ignore_errors=True)
                raise RuntimeError(f"{name.title()} CLI not found on PATH")
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            process = subprocess.Popen([executable, *command[1:]], env=dict(os.environ, **env), creationflags=flags)
            watch_process = True
        self.logins[name] = process
        self.notify("log", f"{name.title()} sign-in opened in a new window")
        threading.Thread(target=self._finish_login, args=(name, process, provider.isolated(directory), directory, watch_process, expect),
                         daemon=True).start()

    def _finish_login(self, name, process, isolated, directory, watch_process=True, expect=None):
        try:
            # Watch for the login file (the CLI may stay open); stop after 10 minutes.
            deadline = time.monotonic() + 600
            login = None
            while time.monotonic() < deadline:
                login = isolated.read_live()
                if login or (watch_process and process.poll() is not None):
                    login = login or isolated.read_live()
                    break
                time.sleep(1.5)
            if login:
                with self.lock:
                    account_id = self.adopt(name, login, source="a sign-in in LimitSwitcher")
                    self.save()
                if hasattr(isolated, "forget"):
                    isolated.forget()
                self._set(account_id, backoffUntil=0.0, backoffFailures=0, pace=1.0, status="")
                wanted = self.meta["accounts"].get(expect) if expect else None
                if wanted and account_id != expect:  # the browser was signed in to another account
                    self.notify("log", f"Signed in as {self.shown_name(account_id)}, not "
                                       f"{self.shown_name(expect)}. To fix that account, sign out of "
                                       f"claude.ai in the browser (or switch accounts there), then click Sign in again.")
                self.refresh(only=account_id)
            else:
                self.notify("log", f"{name.title()} sign-in closed without a login")
        finally:
            # End the sign-in with everything it started: on Windows `claude` is a .cmd shim, so ending
            # only that leaves the real Claude Code running (and spending usage) after the window is gone.
            if watch_process:
                if process.poll() is None:
                    processes.end_tree(process.pid)
            else:  # macOS: Terminal's script wrote its own id; the window stays, the CLI inside must not
                try:
                    processes.end_tree(int((directory / "login.pid").read_text().strip()))
                except (OSError, ValueError):
                    pass
            self.logins.pop(name, None)
            shutil.rmtree(directory, ignore_errors=True)
            self.notify("accounts", None)

def clock_text(ts, clock24=None):
    """A time of day in the app's clock setting (Settings → 24-hour clock; unset: the system's)."""
    if clock24 is None:
        from .clock import clock_12h
        clock24 = not clock_12h()
    moment = time.localtime(ts)
    if clock24:
        return time.strftime("%H:%M", moment)
    return time.strftime("%I:%M %p", moment).lstrip("0")


def project(usage, now):
    """Apply reset times that have passed since the last fetch, so the display is right
    without calling the API. Only account-wide and model windows with a known reset."""
    shown = []
    for window in usage:
        if window.get("resetsAt") and window["resetsAt"] <= now:
            window = dict(window, used=0.0, resetsAt=None, projected=True)
        shown.append(window)
    return shown


def subscription_view(meta):
    manual = meta.get("subscriptionManual")
    if manual:
        return dict(manual, source="manual")
    auto = meta.get("subscription")
    if auto and auto.get("at") and auto["at"] > time.time() - 86400:
        return dict(auto, source="auto")
    return None


class LiveRouter(Router):
    """Router view over LiveAccounts so the controller and UIs work unchanged."""

    def __init__(self, manager):
        self.manager = manager

    @property
    def accounts(self):
        return self.manager.accounts()

    @property
    def active(self):
        return self.manager.active

    @property
    def auto_swap(self):
        return self.manager.meta["autoSwap"]

    @auto_swap.setter
    def auto_swap(self, value):
        with self.manager.lock:
            self.manager.meta["autoSwap"] = bool(value)
            self.manager.save()


class LiveGateway:
    """Controller backend for real accounts."""
    live = True

    def __init__(self, notify, vault=None, providers=None, background=True):
        self.manager = LiveAccounts(notify, vault, providers)
        self.manager.on_new_account = lambda: self.wake.set() if hasattr(self, "wake") else None
        self.manager.on_limit = lambda: self.wake.set() if hasattr(self, "wake") else None
        self.integrations = None  # Codex router + Claude AFK hook, set up by the tray
        self.router = LiveRouter(self.manager)
        self.notify = notify
        self.quota_observed = False
        self.wake = threading.Event()
        self.stopped = False
        self.force = False
        self.poke_age = None
        self.manager.sync_live(force=True)
        if background:
            threading.Thread(target=self._loop, daemon=True, name="usage-refresh").start()

    def _loop(self):
        delay = STARTUP_DELAY  # asking at the very instant of boot fails for some accounts, shown as "Retrying"
        while not self.stopped:
            self.wake.wait(delay)  # sleeps; no work between refreshes
            self.wake.clear()
            if self.stopped:
                break
            force, self.force = self.force, False
            poke_age, self.poke_age = self.poke_age, None
            try:
                with watched():
                    self.manager.refresh(force=force, max_age=None if force else poke_age)
                    if not force and poke_age is not None:
                        self.manager.refresh()  # anything simply due as well
                    self.manager.auto_swap()
            except Exception as error:
                self.notify("log", f"Usage refresh failed: {error}")
            delay = self.manager.next_delay()

    def poke(self, max_age=FRESH_ENOUGH):
        """Panel or full view opened: fetch only accounts whose data is older than max_age
        (inactive accounts use at least 15 minutes)."""
        self.poke_age = max(FRESH_ENOUGH, max_age or FRESH_ENOUGH)
        self.wake.set()

    def swap(self, account_id):
        self.manager.swap(account_id)
        return next(a for a in self.manager.accounts() if a.id == account_id)

    def reset(self):
        """Refresh button: fetch everything now, at most every MANUAL_MIN_GAP seconds."""
        if time.monotonic() - self.manager.last_manual >= MANUAL_MIN_GAP:
            self.manager.last_manual = time.monotonic()
            self.force = True
        self.wake.set()

    def set_afk(self, enabled):
        with self.manager.lock:
            self.manager.meta["afk"] = bool(enabled)
            self.manager.save()
        if self.integrations:
            self.integrations.apply_afk()

    def apply_preferences(self):
        if self.router.auto_swap:
            self.manager.auto_swap()

    def close(self):
        self.stopped = True
        self.wake.set()
