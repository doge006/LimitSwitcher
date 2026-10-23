"""TLS for the app's HTTPS requests (usage APIs, the Codex router's upstream).

Some Pythons come with no trusted certificates, so every HTTPS request would fail
verification. When Python's own store is empty, the certificates from certifi are used
instead, if it is installed."""
import ssl
from urllib.request import HTTPSHandler

_context = None


def context():
    global _context
    if _context is None:
        ctx = ssl.create_default_context()
        if not ctx.cert_store_stats().get("x509_ca"):
            try:
                import certifi
                ctx.load_verify_locations(cafile=certifi.where())
            except (ImportError, OSError):
                pass
        _context = ctx
    return _context


def https_handler():
    return HTTPSHandler(context=context())
