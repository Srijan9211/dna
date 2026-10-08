"""Signed URLs for tracker media.

Some trackers (AYON) serve thumbnails and movies only to authenticated requests, but a
browser cannot attach an ``Authorization`` header to ``<img src>`` or ``<video src>``.
So DNA hands the browser a short-lived, signed URL to its own proxy route
(``GET /media/versions/{id}/{kind}?token=...``); the route verifies the token and
streams the media from the tracker using DNA's credentials, acting as the user the
token was issued to.

A token binds one version, one kind (``thumbnail`` / ``movie``) and one user, and
expires, so a copied URL is useful only briefly and only for what it names.  It is
HMAC-SHA256 signed with the JWT secret (or a per-process random key when none is set,
which only means URLs do not survive a restart).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

KINDS = ("thumbnail", "movie")
DEFAULT_TTL_SECONDS = 15 * 60

# Used when no JWT secret is configured (auth disabled); lives for the process.
_EPHEMERAL_KEY = secrets.token_bytes(32)


@dataclass
class MediaStream:
    """A tracker response being relayed to the browser."""

    status: int
    headers: dict[str, str]
    chunks: Iterator[bytes]
    close: Callable[[], None] = lambda: None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key(secret: Optional[str]) -> bytes:
    """The signing key: derived from the JWT secret for this purpose only, so a media
    link's signature can never double as (or be confused with) another token's."""
    if not secret:
        return _EPHEMERAL_KEY
    return hmac.new(
        secret.encode("utf-8"), b"dna.media-proxy.v1", hashlib.sha256
    ).digest()


def has_stable_key(secret: Optional[str]) -> bool:
    """False when links are signed with a per-process key (they then break across
    restarts and across worker processes)."""
    return bool(secret)


_TOKEN_IN_URL = re.compile(r"([?&]token=)[^&\s\"]+")


def _redact(match: "re.Match[str]") -> str:
    return f"{match.group(1)}***"


class RedactTokens(logging.Filter):
    """Keep signed media tokens out of access logs (they travel in the query string
    because ``<img>``/``<video>`` cannot send a header)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_IN_URL.sub(_redact, a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_URL.sub(_redact, record.msg)
        return True


def sign_token(
    *,
    version_id: Any,
    kind: str,
    login: Optional[str],
    secret: Optional[str],
    ttl: int = DEFAULT_TTL_SECONDS,
    now: Optional[float] = None,
) -> str:
    if kind not in KINDS:
        raise ValueError(f"Unknown media kind '{kind}'")
    payload = {
        "v": version_id,
        "k": kind,
        "u": login,
        "exp": int((now if now is not None else time.time()) + ttl),
    }
    body = _b64(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(_key(secret), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def verify_token(
    token: str,
    *,
    version_id: Any,
    kind: str,
    secret: Optional[str],
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Return the token's claims.

    Raises:
        ValueError: malformed, wrongly signed, expired, or issued for a different
            version or kind.  The message is deliberately uniform for the first
            cases, so it does not help someone forge a token.
    """
    try:
        body, _, signature = token.partition(".")
        expected = hmac.new(_key(secret), body.encode("ascii"), hashlib.sha256).digest()
        if not signature or not hmac.compare_digest(_unb64(signature), expected):
            raise ValueError
        claims = json.loads(_unb64(body))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Invalid media link.") from None
    if claims.get("exp", 0) < (now if now is not None else time.time()):
        raise ValueError("This media link has expired. Reload the page.")
    if claims.get("v") != version_id or claims.get("k") != kind:
        raise ValueError("Invalid media link.")
    return claims


def media_url(version_id: Any, kind: str, token: str) -> str:
    """The relative proxy URL (the frontend prefixes the API base)."""
    return f"/media/versions/{version_id}/{kind}?token={token}"
