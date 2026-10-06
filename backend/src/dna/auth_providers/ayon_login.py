"""AYON login auth provider.

Users sign in with their AYON user name (or email) and password.  The password is
verified once with AYON and dropped; DNA then runs every request with its service
key and ``x-as-user``, so AYON applies that user's own project access.

Everything else (the access token, rotating refresh cookie, reuse detection,
revocation, lifetimes, logout) is :class:`SessionAuthProvider`, shared with the
ShotGrid provider.  This class supplies only what is AYON-specific.

Needs ``AUTH_PROVIDER=ayon``, ``JWT_SECRET_KEY``, ``AYON_URL`` and ``AYON_API_KEY``
(a service user allowed to act as others).
"""

from __future__ import annotations

import logging
from typing import Optional

from dna.auth.ayon_auth_client import AyonAuthClient, AyonUnavailable
from dna.auth.session_store import AbstractSessionStore, AyonCredentials, UserSession
from dna.auth_providers.shotgrid_sso import (
    AuthResult,
    SessionAuthProvider,
)

logger = logging.getLogger(__name__)

SESSION_PROVIDER_NAME = "ayon_login"


class AyonLoginProvider(SessionAuthProvider):
    """AYON user name + password."""

    TRACKER_NAME = "AYON"
    SESSION_PROVIDER_NAME = SESSION_PROVIDER_NAME

    def __init__(
        self,
        session_store: Optional[AbstractSessionStore] = None,
        ayon_auth_client: Optional[AyonAuthClient] = None,
    ) -> None:
        super().__init__(session_store)
        # Built lazily so a missing AYON_URL does not stop the app starting.
        self._ayon_auth_override = ayon_auth_client
        self._ayon_auth: Optional[AyonAuthClient] = None

    def _client(self) -> AyonAuthClient:
        if self._ayon_auth_override is not None:
            return self._ayon_auth_override
        if self._ayon_auth is None:
            self._ayon_auth = AyonAuthClient()
        return self._ayon_auth

    def login(self, username: str, password: str) -> AuthResult:
        """Verify an AYON user name (or email) and password and open a session.

        Raises:
            ValueError: AYON rejected the credentials, or the account is not
                active or has no email address.
            AyonUnavailable: AYON did not answer (a 503, not a wrong password).
        """
        client = self._client()
        login = client.login_user(username, password)
        info = client.get_user_info(login)
        return self._start_session(
            info.email, info.name, ayon=AyonCredentials(username=info.login)
        )

    def get_login_info(self) -> dict:
        # The same user-name + password form; "tracker" lets the UI label it.
        return {"mode": "pat", "tracker": "ayon"}

    def _confirm_access(self, session: UserSession) -> None:
        """End the session if its AYON account is no longer active.

        If AYON is merely unreachable the session is kept and rechecked on the
        next refresh; the absolute lifetime still applies.
        """
        login = session.tracker_login
        if not login:
            self._sessions.delete_session(session.session_id)
            raise ValueError("Your session cannot be renewed. Please log in again.")
        try:
            self._client().confirm_user_active(login)
        except AyonUnavailable as exc:
            logger.warning(
                "AYON unavailable during refresh; keeping session and rechecking "
                "on the next refresh: %s",
                exc,
            )
        except ValueError as exc:
            self._sessions.delete_session(session.session_id)
            raise ValueError(f"{exc} Please log in again.")
