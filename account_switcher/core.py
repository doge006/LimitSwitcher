"""Accounts and routing (which account each provider uses), independent of the UI."""
from dataclasses import dataclass, field
import time


@dataclass
class Account:
    id: str
    provider: str
    alias: str
    five_hour: float
    weekly: float
    reset_at: float
    weekly_reset_at: float
    exhausted: bool = False
    plan: str = ""
    email: str = ""
    # Model-specific caps, e.g. ("Weekly · Fable", used_percent, reset_at).
    # Shown in the UI; routing stays on the account-wide windows.
    model_windows: tuple = field(default_factory=tuple)
    # Live accounts carry provider-reported windows here instead of the demo fields:
    # [{"key", "label", "used", "resetsAt", "scope": "account" | "model"}]. None = demo.
    usage: list = None
    status: str = ""        # "" when fresh, otherwise a short problem description
    updated_at: float = 0.0
    subscription: dict = None   # {"at": ts, "ends": bool|None, "source": "manual"|"auto"}
    credits: dict = None        # Codex credits / Claude extra usage, as reported
    pinned: bool = False        # held by a window of its own (profiles.py)
    window: int = 0             # that window's number in the list
    signed_out: bool = False    # in use, but the client's login file has no login: a switch to it puts it back

    def account_windows(self):
        return [w for w in self.windows() if w["scope"] == "account"]

    @property
    def eligible(self):
        # A reset timestamp alone is insufficient evidence of restored quota. Unknown
        # usage is not treated as exhausted, so a manual swap is still possible.
        return not self.exhausted and all(w["used"] < 100 for w in self.account_windows())

    @property
    def headroom(self):
        """Remaining percent of the tightest account-wide window (-1 when unknown)."""
        windows = self.account_windows()
        return 100 - max(w["used"] for w in windows) if windows else -1

    @property
    def renews_at(self):
        """When usage next renews: when a hit limit clears, else the soonest reset."""
        windows = [w for w in self.account_windows() if w.get("resetsAt")]
        spent = [w["resetsAt"] for w in windows if w["used"] >= 100]
        if spent:
            return max(spent)
        return min((w["resetsAt"] for w in windows), default=None)

    def windows(self):
        """Provider-neutral usage windows (plan section 6: UsageWindow)."""
        if self.usage is not None:
            return self.usage
        rows = [{"key": "five_hour", "label": "5-hour", "used": self.five_hour, "resetsAt": self.reset_at, "scope": "account"},
                {"key": "weekly", "label": "Weekly", "used": self.weekly, "resetsAt": self.weekly_reset_at, "scope": "account"}]
        rows += [{"key": f"model-{i}", "label": label, "used": used, "resetsAt": reset, "scope": "model"}
                 for i, (label, used, reset) in enumerate(self.model_windows)]
        return rows


class Router:
    def __init__(self, accounts):
        self.accounts = accounts
        self.active = {}
        self.auto_swap = True
        for account in accounts:
            self.active.setdefault(account.provider, account.id)

    def current(self, provider):
        return next(a for a in self.accounts if a.id == self.active[provider])

    def swap(self, account_id):
        account = next(a for a in self.accounts if a.id == account_id)
        if not account.eligible:
            raise ValueError("This account has no confirmed available quota.")
        self.active[account.provider] = account.id
        return account


def demo_accounts():
    now = time.time()
    return [
        Account("claude-a", "claude", "Claude · Personal (synthetic)", 64, 42, now + 7200, now + 3 * 86400,
                plan="Max 5x", email="personal@example.com", model_windows=(("Weekly · Fable", 40, now + 3 * 86400),)),
        Account("claude-b", "claude", "Claude · Second (synthetic)", 18, 27, now + 12600, now + 5 * 86400,
                plan="Max 20x", email="second@example.com", model_windows=(("Weekly · Fable", 22, now + 5 * 86400),)),
        Account("codex-a", "codex", "Codex · Personal (synthetic)", 36, 51, now + 5400, now + 2 * 86400,
                plan="Pro", email="personal@example.com"),
        Account("codex-b", "codex", "Codex · Second (synthetic)", 8, 12, now + 14400, now + 6 * 86400,
                plan="Plus", email="second@example.com"),
    ]
