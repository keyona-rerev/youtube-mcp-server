"""Runtime configuration read from the environment.

Two concerns live here:

1. Cookies. YouTube challenges datacenter IPs with a bot check. A cookie jar
   from a signed-in browser session answers it. The jar is supplied as an
   environment variable so it never enters the repository.

2. The shared secret used to gate HTTP requests. See server.py.
"""

from __future__ import annotations

import base64
import binascii
import os
import pathlib
import tempfile

COOKIE_ENV = "YOUTUBE_COOKIES"
TOKEN_ENV = "MCP_AUTH_TOKEN"

_NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
_cookie_path: str | None = None


def _decode(raw: str) -> str:
    """Accept the jar either verbatim or base64-encoded.

    Some UIs mangle the tab characters that the Netscape format requires, so
    base64 is offered as a lossless alternative.
    """
    if raw.lstrip().startswith("#"):
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return raw
    return decoded if decoded.lstrip().startswith("#") else raw


def cookie_file() -> str | None:
    """Materialize the cookie jar on disk once, then return its path.

    Returns None when no jar is configured, which is a supported state: the
    server works without cookies until YouTube decides to challenge it.
    """
    global _cookie_path
    if _cookie_path is not None:
        return _cookie_path or None

    raw = os.environ.get(COOKIE_ENV, "").strip()
    if not raw:
        _cookie_path = ""
        return None

    content = _decode(raw)
    if not content.lstrip().startswith("#"):
        content = f"{_NETSCAPE_HEADER}\n{content}"
    if not content.endswith("\n"):
        content += "\n"

    path = pathlib.Path(tempfile.gettempdir()) / "youtube_cookies.txt"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    _cookie_path = str(path)
    return _cookie_path


def with_cookies(opts: dict) -> dict:
    """Add the cookie jar to a yt-dlp options dict when one is configured."""
    path = cookie_file()
    if not path:
        return opts
    return {**opts, "cookiefile": path}


def cookie_status() -> str:
    """One line for the startup log. Never prints cookie contents."""
    path = cookie_file()
    if not path:
        return f"cookies: none ({COOKIE_ENV} not set)"
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return "cookies: unreadable"
    entries = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
    tabbed = sum(1 for ln in entries if "\t" in ln)
    if entries and not tabbed:
        return (
            f"cookies: {len(entries)} entries but NO TAB characters — the "
            "Netscape format needs tabs. Re-supply the jar base64-encoded."
        )
    return f"cookies: loaded, {tabbed} entries"


def auth_token() -> str | None:
    """The shared secret required on HTTP requests, if one is configured."""
    return os.environ.get(TOKEN_ENV, "").strip() or None
