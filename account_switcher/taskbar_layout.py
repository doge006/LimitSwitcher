"""What the taskbar view shows on which display (Windows): per display, two slots, left and right.

A slot is "claude" or "codex" (that provider's account in use), "next:<provider>" (its next
eligible Auto swap account), "acct:<id>" (one account,
whatever is in use) or None (nothing). A display with no slot set shows nothing. Without a saved
layout it is what the app always showed: Claude's and Codex's accounts in use, on the chosen
display ("taskbarDisplay", the main one by default). Pure functions: the settings, the tray and
the tests share them.
"""
PROVIDERS = ("claude", "codex")
MAX_DISPLAYS = 8


def valid_slot(slot):
    return slot is None or slot in PROVIDERS or slot in ("next:claude", "next:codex") or (isinstance(slot, str) and slot.startswith("acct:")
                                                 and 5 < len(slot) <= 64)


def clean(raw):
    """A saved layout checked and normalized ({display: [slot, slot]}), or None if unusable."""
    if not isinstance(raw, dict) or not raw or len(raw) > MAX_DISPLAYS:
        return None
    out = {}
    for key, slots in raw.items():
        if not isinstance(key, str) or not key or len(key) > 40:
            return None
        if not isinstance(slots, (list, tuple)) or len(slots) != 2 or not all(valid_slot(s) for s in slots):
            return None
        out[key] = list(slots)
    return out


def layout(state):
    """The layout in effect: {display id: [left slot, right slot]} ({}: every slot turned off)."""
    if state.get("taskbarLayout") == {}:
        return {}
    return clean(state.get("taskbarLayout")) or {state.get("taskbarDisplay") or "main": ["claude", "codex"]}


def options(state):
    """Dropdown choices: dynamic accounts, fixed accounts, then Off."""
    return [slot for provider in PROVIDERS for slot in (provider, "next:" + provider)] \
        + ["acct:" + a["id"] for a in state.get("accounts") or []] + [None]


def label(state, slot):
    """The slot as the settings show it (an account by its shown name: name mode applies)."""
    if slot is None:
        return "Off"
    if slot in PROVIDERS:
        return slot.title() + " in use"
    if slot in ("next:claude", "next:codex"):
        return slot[5:].title() + " next use"
    account = next((a for a in state.get("accounts") or [] if "acct:" + a["id"] == slot), None)
    return (account.get("name") or account.get("email") or slot.title()) if account else "Removed account"


def with_slot(current, display, index, slot):
    """A copy of `current` with one slot changed (a display left with nothing is dropped)."""
    out = {key: list(slots) for key, slots in current.items()}
    slots = out.setdefault(display, [None, None])
    slots[index] = slot
    if slots == [None, None]:
        out.pop(display)
    return out


def moved(current, display):
    """The tray menu's "On <display>": everything the taskbar shows, on that display alone."""
    shown = next((slots for slots in current.values() if any(slots)), ["claude", "codex"])
    return {display: list(shown)}
