"""Production ShotGrid SSO Auth Provider — final version after full investigation.

Two auth paths (see INVESTIGATION_FINDINGS.md):
  1. AMI flow (primary)  — session_token grant, no PAT, launched from ShotGrid
  2. Password flow (fallback) — username + Legacy Password, requires PAT on cloud

Both paths:
  - Call ShotGrid's /api/v1/auth/access_token endpoint
  - Store SG tokens server-side in Redis (NEVER sent to client)
  - Mint a minimal DNA JWT (jti, session_id, email, sub, exp — no sg_token)
  - Use ShotGridConnectionPool for connection reuse across requests
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Optional

try:
    import jwt as pyjwt
except ImportError:
    raise ImportError("PyJWT is required: pip install PyJWT")

from dna.auth.session_store import SessionStore, UserSession
from dna.auth.shotgrid_auth_client import ShotGridAuthClient
from dna.auth_providers.auth_provider_base import AuthProviderBase


class ShotGridSSOProvider(AuthProviderBase):
    """Production auth provider for ShotGrid.

    PRIMARY (AMI):   User launches DNA from ShotGrid UI → session_token grant
    FALLBACK (PASS): Standalone access → password grant (Legacy Login + PAT)
    """

    _REFRESH_GRACE_SECONDS = 60

    def __init__(
        self,
        session_store: Optional[SessionStore] = None,
        sg_auth_client: Optional[ShotGridAuthClient] = None,
    ) -> None:
        self._secret = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_USE_A_REAL_SECRET_32CHARS")
        self._algorithm = os.getenv("JWT_ALGORITHM", "HS256")
        self._expire_seconds = int(os.getenv("JWT_EXPIRE_MINUTES", "480")) * 60

        if self._secret == "CHANGE_ME_USE_A_REAL_SECRET_32CHARS":
            import warnings
            warnings.warn("JWT_SECRET_KEY is using the insecure default.", stacklevel=2)

        self._sessions: SessionStore = session_store or _lazy_session_store()
        # _sg_auth is initialised lazily via _get_sg_auth() to avoid failing
        # at startup when SHOTGRID_URL is not yet configured (e.g. GET /auth/login
        # only needs mode detection, not the SG client).
        self._sg_auth_override: Optional[ShotGridAuthClient] = sg_auth_client

    def _get_sg_auth(self) -> "ShotGridAuthClient":
        """Return the ShotGrid auth client, initialising it on first use."""
        if self._sg_auth_override is not None:
            return self._sg_auth_override
        return _lazy_sg_auth_client()

    # ── AuthProviderBase ──────────────────────────────────────────────── #

    def validate_token(self, token: str) -> dict:
        """Validate DNA JWT + Redis revocation blocklist check.

        Raises:
            ValueError: Missing, malformed, expired, or revoked token.
        """
        claims = self._decode_jwt(token)
        jti = claims.get("jti")
        if not jti:
            raise ValueError("Token is missing the 'jti' claim.")
        if self._sessions.is_token_revoked(jti):
            raise ValueError("Token has been revoked. Please log in again.")
        return claims

    # ── AMI flow: PRIMARY auth path (no PAT needed) ───────────────────── #

    def login_via_ami(self, sg_session_token: str, entity_context: dict) -> dict:
        """Complete the AMI login flow using the ShotGrid session_token.

        Called by POST /auth/ami-callback when ShotGrid POSTs to DNA as
        part of an Action Menu Item (AMI) invocation.

        ShotGrid sends the active user's session_token in the POST payload.
        We exchange it for a Bearer access_token via the session_token grant,
        resolve user info, and create a Redis session.

        NO PAT required. NO Legacy Password. This is the recommended flow.

        AMI Setup (SG Admin must do once per site):
            Admin → Action Menu Items → Create:
              Entity Types: Playlist (and/or Version, Shot, etc.)
              URL: POST https://<dna-backend>/auth/ami-callback
              Token type: User  (sends active user's session_token)

        Args:
            sg_session_token: 'session_token' from the ShotGrid AMI POST body.
            entity_context:   Entity data from the AMI POST body (e.g. project_id,
                              entity_type, entity_id) — stored in session for
                              the frontend to use as initial context.

        Returns:
            Auth token response dict with 'access_token' (DNA JWT).

        Raises:
            ValueError: SG rejected the session_token or user info missing.
        """
        # Exchange SG session_token for a Bearer access_token
        sg_token_set = self._get_sg_auth().login_via_session_token(sg_session_token)

        # Resolve user identity from the access_token
        user_info = self._get_sg_auth().get_user_info(sg_token_set.access_token)

        # Create Redis session — SG tokens stored server-side only
        session_id = str(uuid.uuid4())
        jti = str(uuid.uuid4())
        session = UserSession(
            session_id=session_id,
            jti=jti,
            email=user_info.email,
            name=user_info.name,
            sg_user_id=user_info.sg_user_id,
            sg_token=sg_token_set.access_token,   # NEVER sent to client
            refresh_token=sg_token_set.refresh_token,
        )
        self._sessions.create_session(session)

        # Mint minimal DNA JWT
        access_token = self._mint_jwt(
            jti, session_id, user_info.email, user_info.name, user_info.sg_user_id
        )

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._expire_seconds,
            "refresh_token": None,
            # Return entity context so the frontend can deep-link to the right
            # playlist/shot immediately after the AMI launch
            "entity_context": entity_context,
            "user": {
                "id": user_info.sg_user_id,
                "email": user_info.email,
                "name": user_info.name,
                "shotgrid_user_id": user_info.sg_user_id,
            },
        }

    # ── Password flow: FALLBACK for standalone access ─────────────────── #

    def login(self, username: str, password: str) -> dict:
        """Authenticate with ShotGrid username + Legacy Password.

        FALLBACK PATH: Use when DNA is accessed outside of ShotGrid (standalone).

        Cloud sites: Requires PAT setup. Each user must:
          1. Generate PAT at profile.autodesk.com → Security → Personal Access
             Tokens → scope: Flow Production Tracking.
          2. Bind PAT: SG → Account Settings → Legacy Login and Personal Access
             Token → paste PAT code.
          This is per-user and cannot be admin-provisioned.

        On-prem sites (SG_SITE_TYPE=onprem): No PAT needed.
          Use the user's actual ShotGrid password or LDAP/AD password.

        Args:
            username: ShotGrid username (email on cloud, login on some on-prem sites).
            password: ShotGrid Legacy Login password (cloud) or actual password (on-prem).

        Returns:
            Auth token response dict with 'access_token' (DNA JWT).

        Raises:
            ValueError: SG rejected credentials, or user info missing.
        """
        sg_token_set = self._get_sg_auth().login_user(username, password)
        user_info = self._get_sg_auth().get_user_info(sg_token_set.access_token, username=username)

        session_id = str(uuid.uuid4())
        jti = str(uuid.uuid4())
        session = UserSession(
            session_id=session_id,
            jti=jti,
            email=user_info.email,
            name=user_info.name,
            sg_user_id=user_info.sg_user_id,
            sg_token=username,
            refresh_token=sg_token_set.refresh_token,
            sg_password=password,           # ← stored in Redis, never sent to client
        )
        self._sessions.create_session(session)

        access_token = self._mint_jwt(
            jti, session_id, user_info.email, user_info.name, user_info.sg_user_id
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._expire_seconds,
            "refresh_token": None,
            "user": {
                "id": user_info.sg_user_id,
                "email": user_info.email,
                "name": user_info.name,
                "shotgrid_user_id": user_info.sg_user_id,
            },
        }

    # ── Login info (mode detection for frontend) ─────────────────────── #

    def get_login_info(self) -> dict:
        """Return the configured auth mode so the frontend can render the correct UI.

        Returns:
            {"mode": "pat"} for standalone username+password login, or
            {"mode": "sso", "redirect_url": "..."} for ShotGrid login page redirect.
        """
        auth_mode = os.getenv("SHOTGRID_AUTH_MODE", "pat").lower()
        if auth_mode == "sso":
            redirect_url = self._build_sg_sso_redirect()
            return {"mode": "sso", "redirect_url": redirect_url}
        return {"mode": "pat"}

    def _build_sg_sso_redirect(self) -> str:
        """Build the ShotGrid login page redirect URL.

        Redirects the user to ShotGrid's own login page.
        ShotGrid handles Autodesk Identity internally — no APS app needed.

        ⚠️ Pending confirmation from Tommy S (Autodesk) that ShotGrid
        supports redirect_uri on its /auth/login endpoint.
        """
        import secrets
        state = secrets.token_urlsafe(32)
        callback_url = os.getenv(
            "AUTH_CALLBACK_URL", "http://localhost:8080/auth/callback"
        )
        self._sessions.store_oauth_state(state)
        sg_url = os.getenv("SHOTGRID_URL", "").rstrip("/")
        return (
            f"{sg_url}/auth/login"
            f"?redirect_uri={callback_url}"
            f"&state={state}"
        )

    # ── SSO callback (ShotGrid login page redirect) ───────────────────── #

    def handle_sg_sso_callback(
        self, session_token: str, state: Optional[str] = None
    ) -> dict:
        """Complete the ShotGrid SSO login flow.

        Called when ShotGrid redirects back to /auth/callback with
        ?session_token=...&state=...

        The session_token is exchanged for a Bearer access_token via
        ShotGrid's grant_type=session_token endpoint.
        No APS app required — works with any ShotGrid deployment.

        ⚠️ Pending confirmation from Tommy S (Autodesk) that ShotGrid
        sends session_token (not auth code) in the redirect_uri callback.

        Args:
            session_token: ShotGrid session_token from callback URL.
            state:         CSRF state parameter (validated if present).

        Returns:
            DNA auth token response dict with 'access_token'.
        """
        auth_mode = os.getenv("SHOTGRID_AUTH_MODE", "pat").lower()
        if auth_mode != "sso":
            raise ValueError(
                "SSO callback requires SHOTGRID_AUTH_MODE=sso. "
                f"Current mode is '{auth_mode}'. Use POST /auth/login instead."
            )

        if state and not self._sessions.consume_oauth_state(state):
            raise ValueError(
                "OAuth2 state is invalid or expired. Please log in again."
            )

        sg_token_set = self._get_sg_auth().login_via_session_token(session_token)
        user_info = self._get_sg_auth().get_user_info(sg_token_set.access_token)

        session_id = str(uuid.uuid4())
        jti = str(uuid.uuid4())
        session = UserSession(
            session_id=session_id,
            jti=jti,
            email=user_info.email,
            name=user_info.name,
            sg_user_id=user_info.sg_user_id,
            sg_token=user_info.login or user_info.email,
            refresh_token=sg_token_set.refresh_token,
        )
        self._sessions.create_session(session)

        access_token = self._mint_jwt(
            jti, session_id, user_info.email, user_info.name, user_info.sg_user_id
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._expire_seconds,
            "refresh_token": None,
            "user": {
                "id": user_info.sg_user_id,
                "email": user_info.email,
                "name": user_info.name,
                "shotgrid_user_id": user_info.sg_user_id,
            },
        }

    # ── Token refresh ─────────────────────────────────────────────────── #

    def refresh_access_token(self, expired_jwt: str) -> dict:
        """Rotate DNA JWT + underlying SG tokens.

        SG access_token lifetime = 3600s (1 hour, site-configurable, returned
        in expires_in on every auth response). We refresh proactively 120s
        before expiry via ShotGridAuthClient.should_refresh().

        DNA JWT lifetime = 480 min (8 hours, configurable via JWT_EXPIRE_MINUTES).
        Over a full DNA JWT lifetime, the SG token is refreshed ~8 times.
        """
        claims = self._decode_jwt(expired_jwt, allow_expired=True)
        now = int(time.time())
        exp = claims.get("exp", 0)

        if now > (exp + self._REFRESH_GRACE_SECONDS):
            raise ValueError("Token expired too long ago. Please log in again.")

        old_jti = claims.get("jti")
        session_id = claims.get("session_id")

        if old_jti and self._sessions.is_token_revoked(old_jti):
            raise ValueError("Token has been revoked. Please log in again.")

        session = self._sessions.get_session(session_id)
        if session is None:
            raise ValueError("Session not found or expired. Please log in again.")

        if not session.refresh_token:
            raise ValueError(
                "No ShotGrid refresh token in session. Please log in again."
            )

        try:
            new_sg = self._get_sg_auth().refresh_tokens(session.refresh_token)
        except ValueError as exc:
            self._sessions.delete_session(session_id)
            raise ValueError(f"ShotGrid token refresh failed: {exc}. Please log in again.")

        new_jti = str(uuid.uuid4())
        session.sg_token = new_sg.access_token
        session.refresh_token = new_sg.refresh_token
        session.jti = new_jti
        self._sessions.update_session(session)

        # Revoke old jti in blocklist
        if old_jti:
            remaining = max(0, exp - int(time.time()))
            self._sessions.revoke_token(old_jti, remaining + self._REFRESH_GRACE_SECONDS)

        # Release stale pool slot (new SG token → new connection on next request)
        _release_from_pool(session_id)

        access_token = self._mint_jwt(
            new_jti, session_id, session.email, session.name, session.sg_user_id
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._expire_seconds,
            "refresh_token": None,
            "user": {
                "id": session.sg_user_id,
                "email": session.email,
                "name": session.name,
                "shotgrid_user_id": session.sg_user_id,
            },
        }

    # ── Logout / revocation ───────────────────────────────────────────── #

    def revoke_token(self, token: str) -> None:
        """Revoke JWT → add to Redis blocklist + delete session + release pool."""
        try:
            claims = self._decode_jwt(token, allow_expired=True)
        except ValueError:
            return
        jti = claims.get("jti")
        session_id = claims.get("session_id")
        exp = claims.get("exp", 0)
        if jti:
            remaining = max(0, exp - int(time.time()))
            self._sessions.revoke_token(jti, remaining + self._REFRESH_GRACE_SECONDS)
        if session_id:
            self._sessions.delete_session(session_id)
            _release_from_pool(session_id)

    # ── Session retrieval (prodtrack dependency) ──────────────────────── #

    def get_session_for_request(self, token: str) -> UserSession:
        """Validate JWT + blocklist check → return Redis session with sg_token.

        This is called by get_user_scoped_prodtrack_provider() on every request.
        The sg_token from the session is passed to ShotGridConnectionPool.get()
        which returns a pooled Shotgun connection (no TCP handshake per request).

        ShotGrid enforces the user's native permissions on every .find() call
        made through the connection — no extra filtering needed in app code.
        """
        claims = self.validate_token(token)
        session_id = claims.get("session_id")
        if not session_id:
            raise ValueError("Token is missing 'session_id' claim.")
        session = self._sessions.get_session(session_id)
        if session is None:
            raise ValueError("Session has expired. Please log in again.")
        return session

    # ── Internal ──────────────────────────────────────────────────────── #

    def _mint_jwt(self, jti, session_id, email, name, sg_user_id) -> str:
        """Mint a signed DNA JWT. No SG token inside — server-side only."""
        now = int(time.time())
        payload = {
            "jti": jti,
            "sub": str(sg_user_id),
            "session_id": session_id,
            "email": email,
            "name": name or email,
            "iat": now,
            "exp": now + self._expire_seconds,
        }
        return pyjwt.encode(payload, self._secret, algorithm=self._algorithm)

    def _decode_jwt(self, token: str, allow_expired: bool = False) -> dict:
        options = {"verify_exp": not allow_expired}
        try:
            return pyjwt.decode(
                token, self._secret, algorithms=[self._algorithm], options=options
            )
        except pyjwt.ExpiredSignatureError:
            raise ValueError(
                "Token has expired. Use POST /auth/refresh or log in again."
            )
        except pyjwt.InvalidTokenError as exc:
            raise ValueError(f"Invalid authentication token: {exc}")


# ── Lazy singletons ───────────────────────────────────────────────────────────

def _lazy_session_store():
    from dna.auth.session_store import get_session_store
    return get_session_store()

def _lazy_sg_auth_client():
    from dna.auth.shotgrid_auth_client import get_sg_auth_client
    return get_sg_auth_client()

def _release_from_pool(session_id: str) -> None:
    try:
        from dna.auth.connection_pool import get_connection_pool
        get_connection_pool().release(session_id)
    except Exception:
        pass
