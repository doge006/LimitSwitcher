"""Claude Code and Codex adapters: read/write the live login and fetch real usage.

Endpoints, headers and response shapes follow the vendored Codex Vitals clients
(after Codex Vitals: ClaudeUsageClient.swift, codex_api.py; see SOURCE_PROVENANCE.md). Standard library only.
"""
import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import ssl
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from . import tls
from .connections import urlopen  # kept-open connections: a usage check is not a TLS handshake each time
from .vault import atomic_write

TIMEOUT = 15
log = logging.getLogger("account_switcher.providers")


class ProviderError(Exception):
    """Short, user-facing problem description."""

    def __init__(self, message, retry_after=None, relogin=False, rate_limited=False, transient=False):
        super().__init__(message)
        self.retry_after, self.relogin, self.rate_limited = retry_after, relogin, rate_limited
        self.transient = transient  # the service or network had a hiccup: retry soon, keep the numbers


@dataclass
class LiveLogin:
    identity: str   # stable key (account UUID / ChatGPT account id, else email)
    email: str
    plan: str
    secret: dict    # everything needed to restore this login later


def _detail(error):
    """The error body, short, for app.log: anything token-shaped is masked (it never should be
    there, but app.log must never hold a secret)."""
    try:
        text = error.read(400).decode("utf-8", "replace")
    except OSError:
        return ""
    return re.sub(r"[A-Za-z0-9._~+/=-]{40,}", "[…]", " ".join(text.split()))[:300]


def _http(method, url, headers, body=None, attempt=0):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, method=method, headers=dict(headers, **({"Content-Type": "application/json"} if data else {})))
    try:
        with urlopen(request, timeout=TIMEOUT, context=tls.context()) as response:
            return response.status, json.loads(response.read() or b"null")
    except HTTPError as error:
        retry = error.headers.get("Retry-After") if error.headers else None
        if error.code == 429:
            wait = float(retry) if retry and retry.strip().isdigit() else None
            try:
                detail = error.read(300).decode("utf-8", "replace")
            except OSError:
                detail = ""
            where = urlsplit(url)
            log.warning("rate limited by %s%s (Retry-After: %s) %s", where.netloc, where.path, retry, " ".join(detail.split()))
            raise ProviderError("Rate limited by the usage API; retrying automatically", retry_after=wait, rate_limited=True)
        if error.code in (401, 403):
            where = urlsplit(url)
            detail = _detail(error)
            log.warning("%s%s refused the login (%s): %s", where.netloc, where.path, error.code, detail)
            if error.code == 403 and ("error_code\":1010" in detail or "Error 1010" in detail):
                # Cloudflare turned this client away by its signature: nothing wrong with the login.
                raise ProviderError("Claude's sign-in service blocked this request; retrying", transient=True)
            raise ProviderError("Login expired", relogin=True)
        if error.code >= 500:  # the service is briefly unavailable (503 and friends): one quick retry
            if attempt == 0:
                time.sleep(2)
                return _http(method, url, headers, body, attempt=1)
            raise ProviderError(f"Usage service unavailable ({error.code})", transient=True)
        where = urlsplit(url)
        log.warning("%s%s answered %s: %s", where.netloc, where.path, error.code, _detail(error))
        raise ProviderError(f"Usage API error {error.code}")
    except URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise ProviderError("Couldn't verify the usage API's certificate (run Update to repair)")
        raise ProviderError("Offline or usage API unreachable", transient=True)
    except (TimeoutError, OSError):
        raise ProviderError("Offline or usage API unreachable", transient=True)
    except ValueError:
        raise ProviderError("Unexpected usage API response")


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _fingerprint(path):
    """Whether a small login file changed: its time, size and contents. Windows keeps file times
    to about 15 ms, so two writes close together (a switch, then Claude Code renewing) can leave
    the same time behind."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(65537)
        return os.stat(path).st_mtime_ns, len(data), hashlib.sha256(data).hexdigest()[:16]
    except OSError:
        return None


def _iso_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _jwt_payload(token):
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (AttributeError, IndexError, ValueError):
        return {}


# ---------- subscription renewal / end (best effort) ----------
DATE_KEYS = ("current_period_end", "renews_at", "renewal_date", "next_billing_date", "next_billing_at",
             "next_charge_date", "period_end", "billing_period_end", "subscription_expires_at", "expires_at",
             "active_until", "chatgpt_subscription_active_until")
CANCEL_DATE_KEYS = ("cancel_at", "cancels_at", "ends_at")


def _ts(value):
    """ISO string or epoch (s/ms) -> epoch seconds, or None."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    return _iso_ts(value)


def key_paths(data, prefix=""):
    """Field names only (never values), for diagnosing which fields an endpoint offers."""
    paths = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            paths += key_paths(value, path)
    elif isinstance(data, list) and data:
        paths += key_paths(data[0], prefix + "[]")
    return paths


def subscription_from(data):
    """(renews_or_ends_at, ends) from an account/billing payload.

    ends: True when set to end, False when it says it will renew, None when unknown.
    """
    found, ends = None, None
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                k = str(key).lower()
                if k in CANCEL_DATE_KEYS and _ts(value):
                    found, ends = _ts(value), True
                elif k in DATE_KEYS and found is None and _ts(value):
                    found = _ts(value)
                elif k == "cancel_at_period_end" and isinstance(value, bool):
                    ends = True if value else (ends if ends else False)
                elif k in ("will_renew", "auto_renew", "autorenew", "is_auto_renew") and isinstance(value, bool):
                    ends = True if not value else (ends if ends else False)
                elif k in ("subscription_status",) and str(value).lower() in ("canceled", "cancelled", "non_renewing", "ending"):
                    ends = True
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found, ends


def next_monthly(start, now=None):
    """Next monthly anniversary of a subscription start (Claude reports only the start)."""
    if not start:
        return None
    import calendar
    from datetime import datetime, timezone
    now = now or time.time()
    first = datetime.fromtimestamp(start, timezone.utc)
    year, month = first.year, first.month
    while True:
        day = min(first.day, calendar.monthrange(year, month)[1])
        candidate = first.replace(year=year, month=month, day=day).timestamp()
        if candidate > now:
            return candidate
        month += 1
        if month > 12:
            year, month = year + 1, 1


def banked_resets(body):
    """A count of saved/banked limit resets, if the usage response reports one (best effort)."""
    found = None
    stack = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                k = str(key).lower()
                is_count = isinstance(value, int) and not isinstance(value, bool)
                if is_count and "reset" in k and any(w in k for w in ("bank", "available", "remaining", "count", "saved", "credit")):
                    found = value if found is None else max(found, value)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def with_resets(credits, body):
    resets = banked_resets(body)
    if resets is None:
        return credits
    return dict(credits or {"kind": "none", "enabled": False}, resets=resets)


def _plan_name(raw):
    names = {"max": "Max", "pro": "Pro", "plus": "Plus", "team": "Team", "enterprise": "Enterprise",
             "business": "Business", "free": "Free", "prolite": "Pro Lite", "edu": "Edu"}
    return names.get(str(raw or "").lower().replace("_", "").replace(" ", ""), str(raw or "").title())


# Claude's usage and profile endpoints are the ones Claude Code itself calls, and they throttle
# other clients hard (429s with a ~20-minute retry-after). They're asked the way Claude Code asks.
CLAUDE_CODE_AGENT = "claude-code/2.1.0"
# The token endpoint sits behind Cloudflare, which refuses urllib's default agent (error 1010,
# reported as a 403) and throttles the one above: it's asked the way Claude Code's own client asks.
CLAUDE_TOKEN_AGENT = "claude-cli/2.1.218 (external, cli)"

class Claude:
    name = "claude"
    last_credits = None
    USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
    TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
    CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    SHARED_KEYS = ("mcpOAuth", "mcpOAuthClientConfig", "mcpXaaIdp", "mcpXaaIdpConfig", "pluginSecrets")

    def __init__(self, config_dir=None, home=None, keychain=None):
        home = Path(home) if home else Path.home()
        custom = config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
        self.config_dir = Path(custom) if custom else home / ".claude"
        self.config_file = (self.config_dir / ".claude.json") if custom else home / ".claude.json"
        self.credentials_file = self.config_dir / ".credentials.json"
        # On macOS Claude Code keeps its login in the login Keychain, not in the file.
        self.keychain = (sys.platform == "darwin") if keychain is None else keychain
        suffix = "-" + hashlib.sha256(unicodedata.normalize("NFC", str(self.config_dir)).encode()).hexdigest()[:8] if custom else ""
        self.keychain_service = "Claude Code-credentials" + suffix
        user = os.environ.get("USER") or ""
        self.keychain_account = user if re.fullmatch(r"[a-zA-Z0-9._-]+", user) else "claude-code-user"

    def locked(self):
        """Claude Code's own locks on its login (claude_locks.py), for the app's writes to it."""
        from . import claude_locks
        return claude_locks.held(self.config_dir, self.config_file)

    def _credentials_text(self):
        if self.keychain:
            from . import keychain
            text = keychain.get(self.keychain_service, self.keychain_account)
            if text:
                return text
        try:
            return self.credentials_file.read_text(encoding="utf-8")
        except OSError:
            return None

    def _credentials(self):
        text = self._credentials_text()
        try:
            data = json.loads(text) if text else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def signature(self):
        if self.keychain:
            text = self._credentials_text() or ""
            return hashlib.sha256(text.encode()).hexdigest(), _mtime(self.config_file)
        return _fingerprint(self.credentials_file), _mtime(self.config_file)

    def read_live(self):
        credentials, config = self._credentials(), _read_json(self.config_file)
        oauth = (credentials or {}).get("claudeAiOauth")
        account = (config or {}).get("oauthAccount")
        if not isinstance(oauth, dict) or not oauth.get("accessToken") or not isinstance(account, dict):
            return None
        email = account.get("emailAddress") or ""
        identity = account.get("accountUuid") or email
        if not identity:
            return None
        return LiveLogin(identity, email, self.plan(oauth), {"credentials": credentials, "oauthAccount": account})

    @staticmethod
    def plan(oauth):
        plan = _plan_name(oauth.get("subscriptionType"))
        tier = str(oauth.get("rateLimitTier") or "")
        for multiple in ("20x", "5x"):
            if multiple in tier:
                return f"{plan} {multiple}"
        return plan

    def write_live(self, secret):
        live = self._credentials() or {}
        credentials = {k: v for k, v in secret["credentials"].items() if k not in self.SHARED_KEYS}
        credentials.update({k: live[k] for k in self.SHARED_KEYS if k in live})  # MCP logins stay put
        config = _read_json(self.config_file)
        if config is None and self.config_file.exists():
            raise ProviderError("Claude's config file is unreadable; not switching")
        config = config or {}
        config["oauthAccount"] = secret["oauthAccount"]
        if self.keychain:
            from . import keychain
            keychain.put(self.keychain_service, self.keychain_account, json.dumps(credentials))
        else:
            atomic_write(self.credentials_file, json.dumps(credentials, indent=2).encode(), private=True)
        atomic_write(self.config_file, json.dumps(config, indent=2).encode())

    def fetch(self, secret, allow_refresh, save=None):
        """Return (windows, plan, updated_secret_or_None). `save` gets a renewed login the moment
        it exists: the old refresh token is spent by then, so a usage call failing afterwards
        (Anthropic throttles it hard) must not lose the new one."""
        updated = None
        oauth = secret["credentials"]["claudeAiOauth"]
        expires = (oauth.get("expiresAt") or 0) / 1000
        if allow_refresh and expires and expires - time.time() < 300:
            secret = updated = self.refresh(secret, "usage check")
            save and save(secret)
            oauth = secret["credentials"]["claudeAiOauth"]
        try:
            _, body = _http("GET", self.USAGE_URL, {"Authorization": "Bearer " + oauth["accessToken"],
                                                  "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json",
                                                  "User-Agent": CLAUDE_CODE_AGENT})
        except ProviderError as error:
            # Anthropic answers an expired access token with 429, not 401: a saved login (ours to
            # renew) whose token has expired, or whose expiry is unknown, is renewed and asked again.
            expired = not expires or expires < time.time() + 60
            if not ((error.relogin or (error.rate_limited and expired)) and allow_refresh and updated is None):
                raise
            secret = updated = self.refresh(secret, "usage check" if error.relogin else "usage check, expired token")
            save and save(secret)
            oauth = secret["credentials"]["claudeAiOauth"]
            _, body = _http("GET", self.USAGE_URL, {"Authorization": "Bearer " + oauth["accessToken"],
                                                  "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json",
                                                  "User-Agent": CLAUDE_CODE_AGENT})
        self.last_credits = with_resets(self.credits(body or {}), body or {})
        self.last_fields = key_paths(body or {})
        return self.windows(body or {}), self.plan(oauth), updated

    def refresh(self, secret, why=""):
        """Renew the saved login's tokens. The refresh token is single-use: whoever else still
        holds the old one (a Claude Code session, say) can no longer renew with it."""
        oauth = secret["credentials"]["claudeAiOauth"]
        who = (secret.get("oauthAccount") or {}).get("emailAddress") or "?"
        if not oauth.get("refreshToken"):
            log.warning("Claude login of %s: no refresh token saved (%s)", who, why)
            raise ProviderError("Login expired; sign in again", relogin=True)
        try:
            _, token = _http("POST", self.TOKEN_URL, {"Accept": "application/json", "User-Agent": CLAUDE_TOKEN_AGENT},
                             {"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"], "client_id": self.CLIENT_ID})
        except ProviderError as error:
            log.warning("Claude login of %s: renewal refused (%s): %s", who, why, error)
            if error.relogin or "error 400" in str(error):
                raise ProviderError("Login expired; sign in again", relogin=True)
            raise
        log.warning("Claude login of %s: renewed (%s)", who, why)
        expected = secret["oauthAccount"].get("accountUuid")
        actual = ((token or {}).get("account") or {}).get("uuid")
        if expected and actual and expected != actual:
            raise ProviderError("Refresh returned a different account", relogin=True)
        fresh = dict(oauth, accessToken=token["access_token"], expiresAt=int((time.time() + int(token.get("expires_in", 3600))) * 1000))
        if token.get("refresh_token"):
            fresh["refreshToken"] = token["refresh_token"]
        if token.get("scope"):
            fresh["scopes"] = token["scope"].split()
        return dict(secret, credentials=dict(secret["credentials"], claudeAiOauth=fresh))

    @staticmethod
    def windows(body):
        rows = []
        for key, label, field in (("five_hour", "5-hour", "five_hour"), ("weekly", "Weekly", "seven_day")):
            window = body.get(field)
            if isinstance(window, dict) and window.get("utilization") is not None:
                rows.append({"key": key, "label": label, "used": float(window["utilization"]),
                             "resetsAt": _iso_ts(window.get("resets_at")), "scope": "account"})
        seen = set()
        for limit in body.get("limits") or []:
            model = (((limit or {}).get("scope") or {}).get("model") or {}).get("display_name")
            if limit.get("kind") == "weekly_scoped" and model and limit.get("percent") is not None and model not in seen:
                seen.add(model)
                rows.append({"key": "model-" + model.lower().replace(" ", "-"), "label": f"Weekly · {model}",
                             "used": float(limit["percent"]), "resetsAt": _iso_ts(limit.get("resets_at")), "scope": "model"})
        overage = body.get("seven_day_overage_included")
        if not any("fable" in m.lower() for m in seen) and isinstance(overage, dict) and overage.get("utilization") is not None:
            # Codex Vitals reads this field as the Fable weekly cap when no scoped limit is listed.
            rows.append({"key": "model-fable", "label": "Weekly · Fable", "used": float(overage["utilization"]),
                         "resetsAt": _iso_ts(overage.get("resets_at")), "scope": "model"})
        return rows

    PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"

    def subscription(self, secret):
        """(at, ends, field names) from Claude's account profile; best effort, at most daily."""
        oauth = secret["credentials"]["claudeAiOauth"]
        _, body = _http("GET", self.PROFILE_URL, {"Authorization": "Bearer " + oauth["accessToken"],
                                                  "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json",
                                                  "User-Agent": CLAUDE_CODE_AGENT})
        at, ends = subscription_from(body or {})
        estimated = False
        org = (body or {}).get("organization") or {}
        status = str(org.get("subscription_status") or "").lower()
        if status in ("canceled", "cancelled", "non_renewing", "ending", "expired"):
            ends = True
        elif status in ("active", "trialing", "past_due") and ends is None:
            ends = False
        if at is None:
            at = next_monthly(_ts(org.get("subscription_created_at")))
            estimated = at is not None
        return at, ends, key_paths(body or {}), estimated

    @staticmethod
    def credits(body):
        """Extra usage (pay-as-you-go credits) from the usage response, if reported."""
        extra = body.get("extra_usage")
        if not isinstance(extra, dict):
            return None
        info = {"kind": "extra", "enabled": bool(extra.get("is_enabled"))}
        for src, dst in (("monthly_limit", "limit"), ("used_credits", "used"), ("utilization", "utilization")):
            if isinstance(extra.get(src), (int, float)):
                info[dst] = float(extra[src])
        return info

    def login_command(self, directory):
        return ["claude", "auth", "login"], {"CLAUDE_CONFIG_DIR": str(directory)}

    def forget(self):
        """Remove an isolated sign-in's Keychain item once it has been saved (macOS)."""
        if self.keychain:
            from . import keychain
            keychain.delete(self.keychain_service, self.keychain_account)

    def isolated(self, directory):
        return Claude(config_dir=directory)


class Codex:
    name = "codex"
    last_credits = None
    USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
    TOKEN_URL = "https://auth.openai.com/oauth/token"
    CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    ROLES = {18000: ("five_hour", "5-hour"), 604800: ("weekly", "Weekly"), 2592000: ("monthly", "30-day")}

    def __init__(self, codex_home=None, home=None):
        home = Path(home) if home else Path.home()
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME") or home / ".codex")
        self.auth_file = self.codex_home / "auth.json"

    def signature(self):
        return (_fingerprint(self.auth_file),)

    def read_live(self):
        auth = _read_json(self.auth_file)
        tokens = (auth or {}).get("tokens")
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            return None  # API-key mode or signed out: nothing to switch
        claims = _jwt_payload(tokens.get("id_token"))
        info = claims.get("https://api.openai.com/auth") or {}
        email = claims.get("email") or (claims.get("https://api.openai.com/profile") or {}).get("email") or ""
        account_id = tokens.get("account_id") or info.get("chatgpt_account_id")
        identity = account_id or email
        if not identity:
            return None
        return LiveLogin(identity, email, _plan_name(info.get("chatgpt_plan_type")), {"auth": auth})

    def write_live(self, secret):
        atomic_write(self.auth_file, json.dumps(secret["auth"], indent=2).encode(), private=True)

    def fetch(self, secret, allow_refresh, save=None):
        updated = None
        tokens = secret["auth"]["tokens"]
        last = _iso_ts(secret["auth"].get("last_refresh"))
        if allow_refresh and (last is None or time.time() - last > 8 * 86400):
            secret = updated = self.refresh(secret, "usage check")
            save and save(secret)
            tokens = secret["auth"]["tokens"]
        try:
            body = self._usage(tokens)
        except ProviderError as error:
            if not (error.relogin and allow_refresh and updated is None):
                raise
            secret = updated = self.refresh(secret, "usage check")
            save and save(secret)
            tokens = secret["auth"]["tokens"]
            body = self._usage(tokens)
        claims = _jwt_payload(tokens.get("id_token")).get("https://api.openai.com/auth") or {}
        plan = _plan_name(body.get("plan_type") or claims.get("chatgpt_plan_type"))
        resets = self.resets(body)
        self.last_credits = with_resets(self.credits(body), body) if resets is None else \
            dict(self.credits(body) or {"kind": "none", "enabled": False}, resets=resets)
        self.last_fields = key_paths(body)
        return self.windows(body), plan, updated

    @staticmethod
    def resets(body):
        """Banked usage-limit resets (ChatGPT settings: "Usage limit resets")."""
        data = body.get("rate_limit_reset_credits")
        if isinstance(data, dict) and isinstance(data.get("available_count"), int):
            return data["available_count"]
        return None

    @staticmethod
    def credits(body):
        credits = body.get("credits")
        if not isinstance(credits, dict):
            return None
        info = {"kind": "credits", "enabled": bool(credits.get("has_credits")) or bool(credits.get("unlimited")),
                "unlimited": bool(credits.get("unlimited"))}
        try:
            if credits.get("balance") is not None:
                info["balance"] = float(credits["balance"])
        except (TypeError, ValueError):
            pass
        return info

    def _usage(self, tokens):
        headers = {"Authorization": "Bearer " + tokens["access_token"], "User-Agent": "codex-cli",
                   "Accept": "application/json", "Cache-Control": "no-cache"}
        if tokens.get("account_id"):
            headers["ChatGPT-Account-Id"] = tokens["account_id"]
        _, body = _http("GET", self.USAGE_URL, headers)
        return body if isinstance(body, dict) else {}

    def refresh(self, secret, why=""):
        """Renew the saved login's tokens (single-use refresh token, as for Claude)."""
        tokens = secret["auth"]["tokens"]
        who = _jwt_payload(tokens.get("id_token")).get("email") or "?"
        if not tokens.get("refresh_token"):
            log.warning("Codex login of %s: no refresh token saved (%s)", who, why)
            raise ProviderError("Login expired; sign in again", relogin=True)
        try:
            _, body = _http("POST", self.TOKEN_URL, {"Cache-Control": "no-cache"},
                            {"client_id": self.CLIENT_ID, "grant_type": "refresh_token",
                             "refresh_token": tokens["refresh_token"], "scope": "openid profile email"})
        except ProviderError as error:
            log.warning("Codex login of %s: renewal refused (%s): %s", who, why, error)
            raise ProviderError("Login expired; sign in again", relogin=True) if error.relogin else error
        log.warning("Codex login of %s: renewed (%s)", who, why)
        fresh = dict(tokens, access_token=body.get("access_token") or tokens["access_token"],
                     refresh_token=body.get("refresh_token") or tokens["refresh_token"])
        if body.get("id_token"):
            fresh["id_token"] = body["id_token"]
        stamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        return {"auth": dict(secret["auth"], tokens=fresh, last_refresh=stamp)}

    @classmethod
    def windows(cls, body):
        limits = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}
        rows = []
        for field in ("primary_window", "secondary_window"):
            window = limits.get(field)
            if not isinstance(window, dict) or window.get("used_percent") is None:
                continue
            seconds = int(window.get("limit_window_seconds") or 0)
            key, label = cls.ROLES.get(seconds, (f"window-{seconds}", f"{round(seconds / 3600)}-hour"))
            rows.append({"key": key, "label": label, "used": float(window["used_percent"]),
                         "resetsAt": float(window["reset_at"]) if window.get("reset_at") else None, "scope": "account"})
        if limits.get("limit_reached") and rows and all(r["used"] < 100 for r in rows):
            max(rows, key=lambda r: r["used"])["used"] = 100.0  # provider says blocked even if rounding says otherwise
        order = {"five_hour": 0, "weekly": 1, "monthly": 2}
        return sorted(rows, key=lambda r: order.get(r["key"], 3))

    CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"

    def subscription(self, secret):
        """(at, ends, field names): paid-through date from the login token; cancellation best effort."""
        tokens = secret["auth"]["tokens"]
        claims = _jwt_payload(tokens.get("id_token")).get("https://api.openai.com/auth") or {}
        at = _ts(claims.get("chatgpt_subscription_active_until"))
        ends, paths = None, ["id_token." + k for k in claims]
        headers = {"Authorization": "Bearer " + tokens["access_token"], "User-Agent": "codex-cli", "Accept": "application/json"}
        if tokens.get("account_id"):
            headers["ChatGPT-Account-Id"] = tokens["account_id"]
        try:
            _, body = _http("GET", self.CHECK_URL, headers)
            accounts = (body or {}).get("accounts") if isinstance(body, dict) else None
            account = accounts.get(tokens.get("account_id") or "") if isinstance(accounts, dict) else None
            source = account if isinstance(account, dict) else (body or {})
            # Read the whole account entry: the period end sits in "entitlement" but the
            # renew/cancel flag can live beside it (e.g. last_active_subscription.will_renew).
            api_at, ends = subscription_from(source if isinstance(source, dict) else {})
            at = api_at or at
            paths += key_paths(body or {})
        except ProviderError:
            pass  # the token date alone is still useful
        return at, ends, paths

    def login_command(self, directory):
        return ["codex", "login"], {"CODEX_HOME": str(directory)}

    def isolated(self, directory):
        return Codex(codex_home=directory)


PROVIDERS = {"claude": Claude, "codex": Codex}
