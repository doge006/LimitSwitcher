"""Where saved logins live, encrypted at rest.

Windows: DPAPI (CryptProtectData), tied to your Windows user; files under
%LOCALAPPDATA%\\AccountSwitcher.
Elsewhere (development): owner-only files, not encrypted.
Nothing here ever leaves the machine.
"""
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile

ENTROPY = b"AccountSwitcher.v1"


_moved = {}


def data_dir():
    """Where the accounts and settings live: LimitSwitcher's own folder. Before the app was renamed it
    was AccountSwitcher (account-switcher on Linux): that folder is moved over, once, so saved logins and
    settings carry on; if it can't be moved (something holds a file in it), it keeps being used."""
    override = os.environ.get("ACCOUNT_SWITCHER_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        new, old = base / "LimitSwitcher", base / "AccountSwitcher"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        new, old = base / "limitswitcher", base / "account-switcher"
    if new not in _moved:
        _moved[new] = new
        try:
            if not new.exists() and old.is_dir():
                new.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, new)
        except OSError:
            if old.is_dir() and not new.exists():
                _moved[new] = old  # the old folder stays in use: nothing is lost
    return _moved[new]


if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _args = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.argtypes = _args
    _crypt32.CryptUnprotectData.argtypes = _args
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    def _blob(data):
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        return _Blob(len(data), buffer), buffer

    def _call(fn, data):
        source, keep = _blob(data)
        entropy, keep2 = _blob(ENTROPY)
        out = _Blob()
        if not fn(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(out)):  # UI_FORBIDDEN
            raise OSError(ctypes.get_last_error(), "DPAPI call failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def protect(data: bytes) -> bytes:
        return b"DPAPI" + _call(_crypt32.CryptProtectData, data)

    def unprotect(data: bytes) -> bytes:
        if not data.startswith(b"DPAPI"):
            raise ValueError("Not a DPAPI blob")
        return _call(_crypt32.CryptUnprotectData, data[5:])
else:
    def protect(data: bytes) -> bytes:
        return b"PLAIN" + data

    def unprotect(data: bytes) -> bytes:
        if not data.startswith(b"PLAIN"):
            raise ValueError("Unknown secret format")
        return data[5:]


def atomic_write(path, data: bytes, private=False):
    """Write via a temp file in the same folder, then replace, so readers never see half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if private and sys.platform != "win32":
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class Vault:
    """accounts.json holds metadata and cached usage (no secrets); secrets/<id>.bin holds logins."""

    def __init__(self, root=None):
        self.root = Path(root) if root else data_dir()
        self.meta_path = self.root / "accounts.json"
        self.secret_dir = self.root / "secrets"

    def load_meta(self):
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_meta(self, meta):
        atomic_write(self.meta_path, json.dumps(meta, indent=2, sort_keys=True).encode("utf-8"))

    def read_secret(self, account_id):
        try:
            raw = (self.secret_dir / f"{account_id}.bin").read_bytes()
        except OSError:
            return None
        return json.loads(unprotect(raw).decode("utf-8"))

    def write_secret(self, account_id, secret):
        atomic_write(self.secret_dir / f"{account_id}.bin", protect(json.dumps(secret).encode("utf-8")), private=True)

    def delete_secret(self, account_id):
        try:
            (self.secret_dir / f"{account_id}.bin").unlink()
        except OSError:
            pass
