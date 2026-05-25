"""Session store for DNA auth — MongoDB-backed.

Responsibilities
----------------
- Create, read, update, delete user sessions keyed by ``session_id``.
- Manage the JWT revocation blocklist (by ``jti``).
- Manage ephemeral OAuth2 state tokens for CSRF protection.

MongoDB collection schema
--------------------------
Collection ``dna_sessions``:
    _id          : session_id (str)
    jti          : current JWT id — old JWTs with a different jti are rejected
    email        : str
    name         : str
    auth_provider: 'shotgrid_pat'
    created_at   : float (unix timestamp)
    expires_at   : datetime  ← TTL index on this field
    shotgrid     : sub-document
      user_id    : int
      access_token : str
      refresh_token: str | null
      password   : str | null  (PAT path — username + Legacy Password)

Collection ``dna_oauth_states``:
    _id          : state token (str)
    expires_at   : datetime  ← TTL index

Collection ``dna_token_blocklist``:
    _id          : jti (str)
    expires_at   : datetime  ← TTL index

Environment variables
---------------------
``MONGODB_URL``          - Default: ``mongodb://localhost:27017``
``MONGODB_DB``           - Default: ``dna``
``SESSION_TTL_SECONDS``  - Default: ``28800`` (8 hours)
``OAUTH_STATE_TTL``      - Default: ``600``   (10 minutes)
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
    """Credentials for ShotGrid PAT sessions.

    These fields are ShotGrid-specific and should never be accessed by code
    that is not in the ShotGrid auth or prodtrack provider.

    Fields
    ------
    user_id       : Integer primary key of the HumanUser record in ShotGrid.
    access_token  : ShotGrid Bearer access token — used for all SG API calls.
    refresh_token : ShotGrid refresh token — used to obtain a new access_token.
    password      : Legacy Login password — stored because shotgun_api3 requires
                    username+password, not a Bearer token.
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
    shotgrid      : ShotGrid-specific credentials.
    """

    session_id: str
    jti: str
    email: str
    name: str
    auth_provider: str          # 'shotgrid_pat'
    created_at: float = field(default_factory=time.time)

    # ── Provider credentials — add new providers here ─────────────────── #
    shotgrid: Optional[ShotGridCredentials] = None
    # future: ftrack: Optional[FtrackCredentials] = None

    # ── Serialisation helpers ──────────────────────────────────────────── #

    def to_dict(self) -> dict:
        """Return a plain dict suitable for JSON or MongoDB storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UserSession":
        """Reconstruct from a plain dict (MongoDB document or JSON)."""
        sg_raw = data.pop("shotgrid", None)
        session = cls(**data)
        if sg_raw:
            session.shotgrid = ShotGridCredentials(**sg_raw)
        return session

    # Legacy property aliases — kept so existing call-sites continue to work.
    # Update call-sites to use session.shotgrid.* directly when convenient.
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


# ── Abstract interface ────────────────────────────────────────────────────────
#
# Any new storage backend (DynamoDB, Postgres, Redis, etc.) implements this
# interface. The rest of the codebase only depends on AbstractSessionStore,
# never on a concrete implementation.


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


# ── MongoDB implementation ────────────────────────────────────────────────────


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


# ── Singleton factory ─────────────────────────────────────────────────────────

# Backward-compat alias — kept so any existing import of ``SessionStore`` still resolves.
SessionStore = AbstractSessionStore

_session_store: Optional[AbstractSessionStore] = None


def get_session_store() -> AbstractSessionStore:
    """Return the application-wide session store singleton (MongoDB)."""
    global _session_store
    if _session_store is None:
        _session_store = MongoSessionStore()
    return _session_store
