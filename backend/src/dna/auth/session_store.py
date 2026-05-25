"""Session store for DNA auth — MongoDB-backed (default) with Redis as an option.

Responsibilities
----------------
- Create, read, update, delete user sessions keyed by ``session_id``.
- Manage the JWT revocation blocklist (by ``jti``).
- Manage ephemeral OAuth2 state tokens for CSRF protection.

Storage backends
----------------
``SESSION_BACKEND=mongo``  (default) — uses the same MongoDB instance as the
                             rest of DNA.  No extra service required.
``SESSION_BACKEND=redis``  — original Redis backend, kept for deployments that
                             already have Redis available.

MongoDB collection schema
--------------------------
Collection ``dna_sessions``:
    _id          : session_id (str)
    jti          : current JWT id — old JWTs with a different jti are rejected
    email        : str
    name         : str
    auth_provider: 'shotgrid_pat' | 'shotgrid_sso' | 'google'
    created_at   : float (unix timestamp)
    expires_at   : datetime  ← TTL index on this field
    shotgrid     : Optional sub-document (only for shotgrid_* providers)
      user_id    : int
      access_token : str
      refresh_token: str | null
      password   : str | null  (PAT path only)

Collection ``dna_oauth_states``:
    _id          : state token (str)
    expires_at   : datetime  ← TTL index

Collection ``dna_token_blocklist``:
    _id          : jti (str)
    expires_at   : datetime  ← TTL index

Environment variables
---------------------
``SESSION_BACKEND``      - Default: ``mongo``
``MONGODB_URL``          - Default: ``mongodb://localhost:27017``
``MONGODB_DB``           - Default: ``dna``
``SESSION_TTL_SECONDS``  - Default: ``28800`` (8 hours)
``OAUTH_STATE_TTL``      - Default: ``600``   (10 minutes)
``REDIS_URL``            - Only used when SESSION_BACKEND=redis
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ── Provider-specific credential models ──────────────────────────────────────
#
# Each auth provider that stores credentials in the session gets its own typed
# dataclass. When a new production-tracking provider is added (e.g. Ftrack),
# add a new FtrackCredentials dataclass and an optional field on UserSession.
# Existing providers and their credentials are never touched.


@dataclass
class ShotGridCredentials:
    """Credentials for ShotGrid PAT and ShotGrid SSO sessions.

    These fields are ShotGrid-specific and should never be accessed by code
    that is not in the ShotGrid auth or prodtrack provider.

    Fields
    ------
    user_id       : Integer primary key of the HumanUser record in ShotGrid.
    access_token  : ShotGrid Bearer access token — used for all SG API calls.
    refresh_token : ShotGrid refresh token — used to obtain a new access_token.
    password      : Legacy Login password — stored only for the PAT path because
                    shotgun_api3 requires username+password, not a Bearer token.
                    None for SSO sessions.
    """

    user_id: int
    access_token: str
    refresh_token: Optional[str] = None
    password: Optional[str] = None


# ── Core session model ────────────────────────────────────────────────────────


@dataclass
class UserSession:
    """Provider-agnostic session stored in the backend.

    Generic identity fields live at the top level.  Provider-specific
    credentials are nested in typed sub-objects (``shotgrid``, and in future
    ``ftrack``, etc.) so they can evolve independently.

    Fields
    ------
    session_id    : UUID — primary key, stored in the DNA JWT as ``session_id``.
    jti           : Current JWT id — every request validates that
                    ``claims["jti"] == session.jti``.  Rotated on token refresh
                    so old JWTs are automatically invalidated without a separate
                    blocklist lookup.
    email         : Canonical user email, provider-agnostic.
    name          : Display name.
    auth_provider : Which auth path created this session.
    created_at    : Unix timestamp of session creation.
    shotgrid      : ShotGrid-specific credentials.  None for Google sessions.
    """

    session_id: str
    jti: str
    email: str
    name: str
    auth_provider: str          # 'shotgrid_pat' | 'shotgrid_sso' | 'google'
    created_at: float = field(default_factory=time.time)

    # ── Provider credentials — add new providers here ─────────────────── #
    shotgrid: Optional[ShotGridCredentials] = None
    # future: ftrack: Optional[FtrackCredentials] = None

    # ── Serialisation helpers ──────────────────────────────────────────── #

    def to_dict(self) -> dict:
        """Return a plain dict suitable for JSON or MongoDB storage."""
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "UserSession":
        """Reconstruct from a plain dict (MongoDB document or JSON)."""
        sg_raw = data.pop("shotgrid", None)
        session = cls(**data)
        if sg_raw:
            session.shotgrid = ShotGridCredentials(**sg_raw)
        return session

    # Legacy aliases — kept so the existing call-sites in shotgrid_sso.py and
    # prodtrack_provider_base.py continue to work during the transition.
    # Remove these once all call-sites are updated.
    @property
    def sg_token(self) -> str:
        return self.shotgrid.access_token if self.shotgrid else ""

    @sg_token.setter
    def sg_token(self, value: str) -> None:
        if self.shotgrid:
            self.shotgrid.access_token = value

    @property
    def sg_user_id(self) -> int:
        return self.shotgrid.user_id if self.shotgrid else 0

    @property
    def sg_password(self) -> Optional[str]:
        return self.shotgrid.password if self.shotgrid else None

    @property
    def refresh_token(self) -> Optional[str]:
        return self.shotgrid.refresh_token if self.shotgrid else None

    @refresh_token.setter
    def refresh_token(self, value: Optional[str]) -> None:
        if self.shotgrid:
            self.shotgrid.refresh_token = value


@dataclass
class OAuthState:
    """Short-lived CSRF state token for OAuth2 Authorization Code flow."""

    code_verifier: str
    redirect_uri: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "OAuthState":
        return cls(**data)

    # Legacy aliases for backward compatibility
    def to_redis(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_redis(cls, raw: str) -> "OAuthState":
        return cls.from_dict(json.loads(raw))


# ── Abstract interface ────────────────────────────────────────────────────────
#
# Any new storage backend (DynamoDB, Postgres, etc.) implements this interface.
# The rest of the codebase only depends on AbstractSessionStore, never on a
# concrete implementation.


class AbstractSessionStore(ABC):
    """Interface for DNA session storage."""

    # ── Sessions ───────────────────────────────────────────────────────── #

    @abstractmethod
    def create_session(self, session: UserSession) -> None:
        """Persist a new session."""

    @abstractmethod
    def get_session(self, session_id: str) -> Optional[UserSession]:
        """Return session or None if absent / expired."""

    @abstractmethod
    def update_session(self, session: UserSession) -> None:
        """Overwrite an existing session and reset its TTL."""

    @abstractmethod
    def delete_session(self, session_id: str) -> None:
        """Delete a session (called on logout)."""

    @abstractmethod
    def get_session_ttl(self, session_id: str) -> int:
        """Return remaining TTL in seconds, or -2 if absent."""

    # ── JWT blocklist ──────────────────────────────────────────────────── #

    @abstractmethod
    def revoke_token(self, jti: str, remaining_ttl_seconds: int) -> None:
        """Add a JWT jti to the revocation blocklist."""

    @abstractmethod
    def is_token_revoked(self, jti: str) -> bool:
        """Return True if the jti is on the blocklist."""

    # ── OAuth2 CSRF state ──────────────────────────────────────────────── #

    @abstractmethod
    def store_oauth_state(self, state: str) -> None:
        """Persist a CSRF state token."""

    @abstractmethod
    def consume_oauth_state(self, state: str) -> bool:
        """Atomically consume a CSRF state token. Returns True if it existed."""

    # ── Health ─────────────────────────────────────────────────────────── #

    @abstractmethod
    def ping(self) -> bool:
        """Return True if the backend is reachable."""


# ── MongoDB implementation (default) ─────────────────────────────────────────


class MongoSessionStore(AbstractSessionStore):
    """MongoDB-backed session store.

    Uses the same MongoDB instance as the rest of DNA — no extra service.

    Collections
    -----------
    dna_sessions       — user sessions, TTL-indexed on ``expires_at``
    dna_oauth_states   — CSRF state tokens, TTL-indexed on ``expires_at``
    dna_token_blocklist — revoked JTIs, TTL-indexed on ``expires_at``

    TTL notes
    ---------
    MongoDB's TTL background thread runs every ~60 seconds.  Documents are
    deleted *after* ``expires_at``, so entries may linger up to 60 s longer
    than their TTL — this only affects cleanup timing, not correctness.
    Blocklist entries staying slightly longer is *more* conservative (safer).
    """

    def __init__(
        self,
        mongo_url: Optional[str] = None,
        db_name: Optional[str] = None,
        session_ttl: Optional[int] = None,
        state_ttl: Optional[int] = None,
    ) -> None:
        try:
            from pymongo import MongoClient, ASCENDING
            from pymongo.errors import ConnectionFailure
        except ImportError:
            raise ImportError(
                "pymongo is required for MongoDB session storage. "
                "Install with: pip install pymongo"
            )

        self._mongo_url = mongo_url or os.getenv("MONGODB_URL", "mongodb://localhost:27017")
        self._db_name = db_name or os.getenv("MONGODB_DB", "dna")
        self.session_ttl = session_ttl or int(os.getenv("SESSION_TTL_SECONDS", "28800"))
        self.state_ttl = state_ttl or int(os.getenv("OAUTH_STATE_TTL", "600"))

        self._client = MongoClient(
            self._mongo_url,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
        )
        db = self._client[self._db_name]
        self._sessions = db["dna_sessions"]
        self._states = db["dna_oauth_states"]
        self._blocklist = db["dna_token_blocklist"]

        # Ensure TTL indexes exist (idempotent)
        self._sessions.create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,
            background=True,
        )
        self._states.create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,
            background=True,
        )
        self._blocklist.create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,
            background=True,
        )

    def _expires_at(self, ttl_seconds: int) -> datetime:
        return datetime.fromtimestamp(time.time() + ttl_seconds, tz=timezone.utc)

    # ── Sessions ───────────────────────────────────────────────────────── #

    def create_session(self, session: UserSession) -> None:
        doc = session.to_dict()
        doc["_id"] = doc.pop("session_id")
        doc["expires_at"] = self._expires_at(self.session_ttl)
        self._sessions.insert_one(doc)

    def get_session(self, session_id: str) -> Optional[UserSession]:
        doc = self._sessions.find_one({"_id": session_id})
        if doc is None:
            return None
        try:
            doc["session_id"] = doc.pop("_id")
            doc.pop("expires_at", None)
            return UserSession.from_dict(doc)
        except (KeyError, TypeError):
            return None

    def update_session(self, session: UserSession) -> None:
        doc = session.to_dict()
        doc.pop("session_id")
        doc["expires_at"] = self._expires_at(self.session_ttl)
        self._sessions.replace_one(
            {"_id": session.session_id},
            {**doc, "_id": session.session_id},
            upsert=True,
        )

    def delete_session(self, session_id: str) -> None:
        self._sessions.delete_one({"_id": session_id})

    def get_session_ttl(self, session_id: str) -> int:
        doc = self._sessions.find_one({"_id": session_id}, {"expires_at": 1})
        if not doc or "expires_at" not in doc:
            return -2
        remaining = doc["expires_at"].timestamp() - time.time()
        return max(0, int(remaining))

    # ── JWT blocklist ──────────────────────────────────────────────────── #

    def revoke_token(self, jti: str, remaining_ttl_seconds: int) -> None:
        if remaining_ttl_seconds <= 0:
            return
        self._blocklist.replace_one(
            {"_id": jti},
            {"_id": jti, "expires_at": self._expires_at(remaining_ttl_seconds)},
            upsert=True,
        )

    def is_token_revoked(self, jti: str) -> bool:
        return self._blocklist.find_one({"_id": jti}) is not None

    # ── OAuth2 CSRF state ──────────────────────────────────────────────── #

    def store_oauth_state(self, state: str) -> None:
        self._states.replace_one(
            {"_id": state},
            {"_id": state, "expires_at": self._expires_at(self.state_ttl)},
            upsert=True,
        )

    def consume_oauth_state(self, state: str) -> bool:
        """Atomically consume — findOneAndDelete is atomic in MongoDB."""
        result = self._states.find_one_and_delete({"_id": state})
        return result is not None

    # ── Health ─────────────────────────────────────────────────────────── #

    def ping(self) -> bool:
        try:
            self._client.admin.command("ping")
            return True
        except Exception:
            return False


# ── Redis implementation (kept for deployments that already use Redis) ────────


class RedisSessionStore(AbstractSessionStore):
    """Redis-backed session store — original implementation.

    Use this by setting SESSION_BACKEND=redis in the environment.
    Kept for backward compatibility and for deployments that prefer Redis
    (e.g. when a managed Redis service with persistence is already available).
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
        try:
            import redis as redis_lib
            self._redis_lib = redis_lib
        except ImportError:
            raise ImportError(
                "redis-py is required for Redis session storage. "
                "Install with: pip install redis"
            )

        self._redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.session_ttl = session_ttl or int(os.getenv("SESSION_TTL_SECONDS", "28800"))
        self.state_ttl = state_ttl or int(os.getenv("OAUTH_STATE_TTL", "600"))
        self._client = self._redis_lib.from_url(
            self._redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30,
        )

    def _session_key(self, session_id: str) -> str:
        return f"{self._SESSION_PREFIX}:{session_id}"

    def create_session(self, session: UserSession) -> None:
        self._client.setex(
            self._session_key(session.session_id),
            self.session_ttl,
            json.dumps(session.to_dict()),
        )

    def get_session(self, session_id: str) -> Optional[UserSession]:
        raw = self._client.get(self._session_key(session_id))
        if raw is None:
            return None
        try:
            return UserSession.from_dict(json.loads(raw))
        except (KeyError, json.JSONDecodeError, TypeError):
            return None

    def update_session(self, session: UserSession) -> None:
        self._client.setex(
            self._session_key(session.session_id),
            self.session_ttl,
            json.dumps(session.to_dict()),
        )

    def delete_session(self, session_id: str) -> None:
        self._client.delete(self._session_key(session_id))

    def get_session_ttl(self, session_id: str) -> int:
        return self._client.ttl(self._session_key(session_id))

    def revoke_token(self, jti: str, remaining_ttl_seconds: int) -> None:
        if remaining_ttl_seconds <= 0:
            return
        self._client.setex(
            f"{self._BLOCKLIST_PREFIX}:{jti}",
            remaining_ttl_seconds,
            "1",
        )

    def is_token_revoked(self, jti: str) -> bool:
        return bool(self._client.exists(f"{self._BLOCKLIST_PREFIX}:{jti}"))

    def store_oauth_state(self, state: str) -> None:
        self._client.setex(f"{self._STATE_PREFIX}:{state}", self.state_ttl, "1")

    def consume_oauth_state(self, state: str) -> bool:
        raw = self._client.getdel(f"{self._STATE_PREFIX}:{state}")
        return raw is not None

    def ping(self) -> bool:
        try:
            return self._client.ping()
        except Exception:
            return False


# ── Backward-compat alias ─────────────────────────────────────────────────────
# ``SessionStore`` was the original name before the Redis/Mongo split.
# Kept so any import that hasn't been updated yet still resolves.
SessionStore = AbstractSessionStore


# ── Singleton factory ─────────────────────────────────────────────────────────


_session_store: Optional[AbstractSessionStore] = None


def get_session_store() -> AbstractSessionStore:
    """Return the application-wide session store singleton.

    Backend is selected by the SESSION_BACKEND environment variable:
        mongo  (default) — MongoSessionStore, uses existing MONGODB_URL
        redis            — RedisSessionStore, requires REDIS_URL

    Call from FastAPI dependency injection:
        SessionStoreDep = Annotated[AbstractSessionStore, Depends(get_session_store)]
    """
    global _session_store
    if _session_store is None:
        backend = os.getenv("SESSION_BACKEND", "mongo").lower()
        if backend == "redis":
            _session_store = RedisSessionStore()
        else:
            _session_store = MongoSessionStore()
    return _session_store
