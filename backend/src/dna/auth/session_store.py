"""Redis-backed session store for DNA auth.

Responsibilities
----------------
- Create, read, update, delete user sessions keyed by ``session_id``.
- Manage the JWT revocation blocklist (by ``jti``).
- Manage ephemeral OAuth2 state tokens for CSRF protection.

Redis key schema
----------------
``dna:session:{session_id}``   → JSON session payload      TTL: session lifetime
``dna:blocklist:{jti}``        → "1"                       TTL: remaining JWT lifetime
``dna:oauth_state:{state}``    → JSON {code_verifier, ...} TTL: 600 s

Environment variables
---------------------
``REDIS_URL``            - Default: ``redis://localhost:6379/0``
``SESSION_TTL_SECONDS``  - Default: ``28800`` (8 hours)
``OAUTH_STATE_TTL``      - Default: ``600``   (10 minutes)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

try:
    import redis
except ImportError:
    raise ImportError(
        "redis-py is required for session storage. "
        "Install with: pip install redis"
    )


# ── Data models ──────────────────────────────────────────────────────────────


@dataclass
class UserSession:
    session_id: str
    jti: str
    email: str
    name: str
    sg_user_id: int
    sg_token: str
    refresh_token: Optional[str] = None
    sg_password: Optional[str] = None    # ← ADD THIS
    created_at: float = field(default_factory=time.time)

    def to_redis(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_redis(cls, raw: str) -> "UserSession":
        return cls(**json.loads(raw))


@dataclass
class OAuthState:
    """Short-lived CSRF state token for OAuth2 Authorization Code flow."""

    code_verifier: str
    redirect_uri: str
    created_at: float = field(default_factory=time.time)

    def to_redis(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_redis(cls, raw: str) -> "OAuthState":
        return cls(**json.loads(raw))


# ── Session store ─────────────────────────────────────────────────────────────


class SessionStore:
    """Redis-backed session store.

    All methods are synchronous (redis-py is synchronous by default).
    FastAPI runs synchronous dependencies in a thread pool, so this is safe.
    For a fully async deployment, swap redis-py for ``redis.asyncio``.
    """

    _KEY_PREFIX = "dna"
    _SESSION_PREFIX = f"{_KEY_PREFIX}:session"
    _BLOCKLIST_PREFIX = f"{_KEY_PREFIX}:blocklist"
    _STATE_PREFIX = f"{_KEY_PREFIX}:oauth_state"

    def __init__(
        self,
        redis_url: Optional[str] = None,
        session_ttl: Optional[int] = None,
        state_ttl: Optional[int] = None,
    ) -> None:
        self._redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.session_ttl = session_ttl or int(
            os.getenv("SESSION_TTL_SECONDS", "28800")
        )
        self.state_ttl = state_ttl or int(os.getenv("OAUTH_STATE_TTL", "600"))
        self._client: redis.Redis = redis.from_url(
            self._redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30,
        )

    # ── Health ────────────────────────────────────────────────────────── #

    def ping(self) -> bool:
        """Return True if Redis is reachable."""
        try:
            return self._client.ping()
        except redis.RedisError:
            return False

    # ── Sessions ──────────────────────────────────────────────────────── #

    def _session_key(self, session_id: str) -> str:
        return f"{self._SESSION_PREFIX}:{session_id}"

    def create_session(self, session: UserSession) -> None:
        """Persist a new session with TTL.

        Args:
            session: The UserSession to store.

        Raises:
            redis.RedisError: On connection failure.
        """
        key = self._session_key(session.session_id)
        self._client.setex(key, self.session_ttl, session.to_redis())

    def get_session(self, session_id: str) -> Optional[UserSession]:
        """Retrieve a session by ID.

        Returns None if the session does not exist or has expired.
        """
        raw = self._client.get(self._session_key(session_id))
        if raw is None:
            return None
        try:
            return UserSession.from_redis(raw)
        except (KeyError, json.JSONDecodeError, TypeError):
            return None

    def update_session(self, session: UserSession) -> None:
        """Update an existing session (e.g. after token refresh).

        Resets the TTL so the session stays alive after a refresh.
        """
        key = self._session_key(session.session_id)
        self._client.setex(key, self.session_ttl, session.to_redis())

    def delete_session(self, session_id: str) -> None:
        """Delete a session (called on logout)."""
        self._client.delete(self._session_key(session_id))

    def get_session_ttl(self, session_id: str) -> int:
        """Return the remaining TTL in seconds for a session, or -2 if absent."""
        return self._client.ttl(self._session_key(session_id))

    # ── JWT blocklist (revocation) ────────────────────────────────────── #

    def _blocklist_key(self, jti: str) -> str:
        return f"{self._BLOCKLIST_PREFIX}:{jti}"

    def revoke_token(self, jti: str, remaining_ttl_seconds: int) -> None:
        """Add a JWT to the revocation blocklist.

        Args:
            jti:                   The JWT's unique ID claim.
            remaining_ttl_seconds: Seconds until the JWT would have expired
                                   naturally.  The blocklist entry is kept for
                                   this long so old tokens cannot be replayed.
        """
        if remaining_ttl_seconds <= 0:
            return  # Already expired — no need to blocklist
        self._client.setex(self._blocklist_key(jti), remaining_ttl_seconds, "1")

    def is_token_revoked(self, jti: str) -> bool:
        """Return True if the jti is on the revocation blocklist."""
        return bool(self._client.exists(self._blocklist_key(jti)))

    # ── OAuth2 state (CSRF) ───────────────────────────────────────────── #

    def _state_key(self, state: str) -> str:
        return f"{self._STATE_PREFIX}:{state}"

    def store_oauth_state(self, state: str) -> None:
        """Persist a CSRF state token (presence flag only)."""
        self._client.setex(self._state_key(state), self.state_ttl, "1")

    def consume_oauth_state(self, state: str) -> bool:
        """Consume a CSRF state token — returns True if it existed, False otherwise."""
        key = self._state_key(state)
        raw = self._client.getdel(key)
        return raw is not None


# ── Singleton factory ─────────────────────────────────────────────────────────


_session_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    """Return the application-wide SessionStore singleton.

    Call this from FastAPI dependency injection:

        SessionStoreDep = Annotated[SessionStore, Depends(get_session_store)]
    """
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store
