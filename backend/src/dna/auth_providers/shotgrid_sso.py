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

from dna.auth.session_store import SessionStore, ShotGridCredentials, UserSession
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

        # Create session — SG tokens stored server-side only
        session_id = str(uuid.uuid4())
        jti = str(uuid.uuid4())
        session = UserSession(
            session_id=session_id,
            jti=jti,
            email=user_info.email,
            name=user_info.name,
            auth_provider="shotgrid_sso",
            shotgrid=ShotGridCredentials(
                user_id=user_info.sg_user_id,
                access_token=sg_token_set.access_token,   # NEVER sent to client
                refresh_token=sg_token_set.refresh_token,
            ),
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
            auth_provider="shotgrid_pat",
            shotgrid=ShotGridCredentials(
                user_id=user_info.sg_user_id,
                access_token=username,              # username used as SG login key
                refresh_token=sg_token_set.refresh_token,
                password=password,                  # ← stored server-side, never sent to client
            ),
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
        """Return available auth modes so the frontend renders the correct login UI.

        Always returns the ``modes`` dict describing all available options.
        Also includes legacy ``mode`` / ``redirect_url`` fields for backward compat.

        Response shape::

            {
              "modes": {
                "shotgrid_pat": {"enabled": true},
                "shotgrid_sso": {"enabled": true, "redirect_url": "https://..."},
                "google":       {"enabled": true}
              },
              // legacy fields (kept for backward compat):
              "mode": "sso" | "pat",
              "redirect_url": "https://..."   // only when sso enabled
            }
        """
        modes: dict = {
            "shotgrid_pat": {"enabled": True},
            "shotgrid_sso": {"enabled": False},
            "google": {"enabled": False},
        }

        # ── ShotGrid APS SSO ────────────────────────────────────────────── #
        client_id = os.getenv("SHOTGRID_CLIENT_ID", "").strip()
        if client_id:
            redirect_url = self._build_sg_oauth2_redirect(client_id)
            modes["shotgrid_sso"] = {"enabled": True, "redirect_url": redirect_url}

        # ── Google OAuth2 SSO ───────────────────────────────────────────── #
        google_client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
        if google_client_id:
            modes["google"] = {"enabled": True}

        # ── Legacy mode field (backward compat) ─────────────────────────── #
        if modes["shotgrid_sso"]["enabled"]:
            legacy = {"mode": "sso", "redirect_url": modes["shotgrid_sso"]["redirect_url"]}
        else:
            legacy = {"mode": "pat"}

        return {"modes": modes, **legacy}

    # ── Google OAuth2 login ───────────────────────────────────────────── #

    def handle_google_login(self, google_token: str) -> dict:
        """Exchange a Google OAuth2 access/ID token for a DNA JWT.

        The Google token is validated server-side; a Redis session is created
        (no ShotGrid credentials) and a DNA JWT is minted — identical shape to
        the ShotGrid auth response so the frontend works unchanged.

        Args:
            google_token: Google access token or ID token from the browser.

        Returns:
            DNA auth token response dict with 'access_token'.

        Raises:
            ValueError: Token is invalid, expired, or missing email claim.
        """
        try:
            from dna.auth_providers.google_auth_provider import GoogleAuthProvider
            google_provider = GoogleAuthProvider()
            claims = google_provider.validate_token(google_token)
        except Exception as exc:
            raise ValueError(f"Google token validation failed: {exc}")

        user_email = (claims.get("email") or "").lower().strip()
        user_name = claims.get("name") or user_email

        if not user_email:
            raise ValueError("Google token did not contain an email claim.")

        session_id = str(uuid.uuid4())
        jti = str(uuid.uuid4())
        session = UserSession(
            session_id=session_id,
            jti=jti,
            email=user_email,
            name=user_name,
            auth_provider="google",
            # No shotgrid credentials — Google users access SG via script creds
        )
        self._sessions.create_session(session)

        access_token = self._mint_jwt(jti, session_id, user_email, user_name, 0)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._expire_seconds,
            "refresh_token": None,
            "user": {
                "id": user_email,
                "email": user_email,
                "name": user_name,
                "shotgrid_user_id": 0,
                "auth_mode": "google",
            },
        }

    def _build_sg_oauth2_redirect(self, client_id: str) -> str:
        """Build the Autodesk Platform Services (APS) OAuth2 authorization URL.

        APS 3-legged flow:
          1. User is redirected here → Autodesk login page appears in popup
          2. User authenticates with their Autodesk (ShotGrid cloud) account
          3. Autodesk redirects popup to AUTH_CALLBACK_URL?code=...&state=...
          4. Popup sends code+state to parent tab via window.postMessage
          5. Parent tab calls GET /auth/callback → backend exchanges code for
             APS access_token → resolves ShotGrid user → mints DNA JWT

        Required env vars:
            SHOTGRID_CLIENT_ID      — from aps.autodesk.com app credentials
            SHOTGRID_CLIENT_SECRET  — from aps.autodesk.com app credentials
            AUTH_CALLBACK_URL       — must be registered in the APS app as a
                                      redirect URI (e.g. http://localhost:8080/auth/callback)

        Optional:
            SHOTGRID_OAUTH2_AUTHORIZE_URL  — override authorization endpoint
                                             (default: APS v2 authorize URL)
        """
        import secrets
        from urllib.parse import urlencode

        state = secrets.token_urlsafe(32)
        callback_url = os.getenv("AUTH_CALLBACK_URL", "http://localhost:8080/auth/callback")
        self._sessions.store_oauth_state(state)

        authorize_url = os.getenv(
            "SHOTGRID_OAUTH2_AUTHORIZE_URL",
            "https://developer.api.autodesk.com/authentication/v2/authorize",
        )

        params = urlencode({
            "response_type": "code",
            "client_id": client_id,
            # openid             → OIDC id_token in token response (email, name)
            # user-profile:read  → APS userinfo endpoint
            # data:read          → ShotGrid project data access via APS
            "scope": "openid user-profile:read data:read",
            "redirect_uri": callback_url,
            "state": state,
            # nonce is NOT included — APS v2 /authorize does not support it
        })
        full_url = f"{authorize_url}?{params}"
        print(f"[APS] authorize redirect_uri={callback_url!r}  client_id={client_id[:8]}...")
        return full_url

    # ── APS (Autodesk Platform Services) SSO callback ─────────────────── #

    def handle_aps_sso_callback(
        self, code: str, state: Optional[str] = None
    ) -> dict:
        """Complete the Autodesk Platform Services (APS) OAuth2 SSO flow.

        Steps:
        1. Validate CSRF state against Redis
        2. Exchange authorization code for APS access_token + refresh_token
        3. Fetch user identity from APS /userprofile endpoint (email, name)
        4. Try to exchange APS token for a ShotGrid-native token via
           grant_type=urn:autodesk:params:oauth:grant-type:sso  (SG cloud only)
           — if successful, DNA sessions use the native SG token so ShotGrid's
             own permission model is enforced on every API call.
        5. If SG native exchange is unavailable (not yet GA on all sites),
           fall back: look up the ShotGrid HumanUser by email using script
           credentials and store the APS token in the session.
        6. Mint and return a DNA JWT.

        Args:
            code:  APS authorization_code from the popup callback URL.
            state: CSRF state parameter (validated if present).

        Returns:
            DNA auth token response dict with 'access_token'.
        """
        import requests as _requests

        if state and not self._sessions.consume_oauth_state(state):
            raise ValueError("OAuth2 state is invalid or expired. Please log in again.")

        client_id = os.getenv("SHOTGRID_CLIENT_ID", "").strip()
        client_secret = os.getenv("SHOTGRID_CLIENT_SECRET", "").strip()
        callback_url = os.getenv("AUTH_CALLBACK_URL", "http://localhost:8080/auth/callback")

        if not client_id:
            raise ValueError("SHOTGRID_CLIENT_ID is not configured.")
        if not client_secret:
            raise ValueError(
                "SHOTGRID_CLIENT_SECRET is required for APS SSO. "
                "Add it to your environment from the APS app credentials page."
            )

        # ── Step 1: Exchange authorization code for APS tokens ────────── #
        token_resp = _requests.post(
            "https://developer.api.autodesk.com/authentication/v2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback_url,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if not token_resp.ok:
            raise ValueError(
                f"APS token exchange failed (HTTP {token_resp.status_code}): "
                f"{token_resp.text[:300]}"
            )
        aps_tokens = token_resp.json()
        aps_access_token = aps_tokens.get("access_token")
        aps_refresh_token = aps_tokens.get("refresh_token")

        if not aps_access_token:
            raise ValueError(
                f"APS token response missing 'access_token'. "
                f"Keys received: {list(aps_tokens.keys())}"
            )

        # DEBUG — log full token response so we can see what scopes/fields APS returned
        print(f"[APS] token keys: {list(aps_tokens.keys())}")
        print(f"[APS] access_token payload: {self._decode_jwt_payload(aps_access_token)}")
        _id_tok = aps_tokens.get("id_token")
        if _id_tok:
            print(f"[APS] id_token payload: {self._decode_jwt_payload(_id_tok)}")
        else:
            print("[APS] no id_token in response")

        sg_url = os.getenv("SHOTGRID_URL", "").rstrip("/")
        sg_native_token: Optional[str] = None
        sg_native_refresh: Optional[str] = None
        sg_user_id: int = 0
        user_email: Optional[str] = None
        user_name: str = ""

        # ── Step 2: ShotGrid SSO token exchange (primary path) ───────── #
        # Exchange the APS token for a SG-native Bearer token using SG's
        # custom grant type.  On success we resolve identity via ShotGrid
        # directly — no APS userinfo endpoint needed (bypasses the Hub App
        # audience restriction that causes /userinfo to return 404).
        try:
            sg_exchange = _requests.post(
                f"{sg_url}/api/v1/auth/access_token",
                data={
                    "grant_type": "urn:autodesk:params:oauth:grant-type:sso",
                    "access_token": aps_access_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            print(f"[APS] SG SSO exchange: HTTP {sg_exchange.status_code} — {sg_exchange.text[:200]}")
            if sg_exchange.ok:
                sg_data = sg_exchange.json()
                sg_native_token = sg_data.get("access_token")
                sg_native_refresh = sg_data.get("refresh_token")
                if sg_native_token:
                    user_info = self._get_sg_auth().get_user_info(sg_native_token)
                    user_email = user_info.email
                    user_name = user_info.name or user_email
                    sg_user_id = user_info.sg_user_id or 0
                    print(f"[APS] Identity via SG exchange: {user_email} (sg_id={sg_user_id})")
        except Exception as exc:
            print(f"[APS] SG SSO exchange error: {exc}")

        # ── Step 3: APS identity extraction (fallback) ───────────────── #
        # Used when the SG SSO grant is not available on this SG instance.
        if not user_email:
            id_token_raw = aps_tokens.get("id_token")
            try:
                user_email, user_name = self._extract_aps_identity(
                    aps_access_token, id_token_raw
                )
                print(f"[APS] Identity via APS token: {user_email}")
            except ValueError:
                pass

        # ── Step 4: Last-resort lookup by Autodesk userid in ShotGrid ── #
        # On cloud ShotGrid the HumanUser.login field IS the Autodesk user
        # ID (e.g. 'HWY3KA6E8VTP').  We also try custom Autodesk ID fields
        # that studios sometimes add.
        if not user_email:
            aps_userid = self._decode_jwt_payload(aps_access_token).get("userid", "")
            print(f"[APS] Trying SG lookup by Autodesk userid={aps_userid!r}")
            sg_script = os.getenv("SHOTGRID_SCRIPT_NAME")
            sg_key = os.getenv("SHOTGRID_API_KEY")
            if aps_userid and sg_script and sg_key:
                try:
                    from shotgun_api3 import Shotgun
                    _sg = Shotgun(sg_url, script_name=sg_script, api_key=sg_key)
                    # "login" is tried first — on cloud SG it stores the Autodesk user ID
                    for _field in ("login", "sg_autodesk_id", "sg_account_id", "sg_adsk_id"):
                        try:
                            rec = _sg.find_one(
                                "HumanUser",
                                [[_field, "is", aps_userid]],
                                ["id", "name", "email", "login"],
                            )
                            print(f"[APS] SG field={_field!r} result: {rec}")
                            if rec:
                                user_email = rec.get("email") or ""
                                if not user_email and rec.get("login", "").count("@") > 0:
                                    user_email = rec["login"]
                                user_name = rec.get("name") or user_email
                                sg_user_id = int(rec["id"])
                                print(f"[APS] Found via {_field}: {user_email}")
                                break
                        except Exception as _fe:
                            print(f"[APS] SG field={_field!r} error: {_fe}")
                except Exception as exc:
                    print(f"[APS] SG userid lookup error: {exc}")

        if not user_email:
            aps_userid = self._decode_jwt_payload(aps_access_token).get("userid", "unknown")
            raise ValueError(
                f"Could not resolve user identity for Autodesk userid={aps_userid!r}. "
                "The ShotGrid SSO token exchange failed and no email was found in the "
                "APS token. For full SSO support, create an APS OAuth2 app at "
                "https://aps.autodesk.com/myapps/ (not a Hub Application) and enable "
                "the 'openid' scope."
            )

        # ── Step 5: Look up SG user by email (if not already resolved) ─ #
        if not sg_user_id:
            sg_script = os.getenv("SHOTGRID_SCRIPT_NAME")
            sg_key = os.getenv("SHOTGRID_API_KEY")
            if sg_script and sg_key:
                try:
                    from shotgun_api3 import Shotgun
                    _sg = Shotgun(sg_url, script_name=sg_script, api_key=sg_key)
                    rec = _sg.find_one(
                        "HumanUser",
                        [["email", "is", user_email]],
                        ["id", "name"],
                    )
                    if rec:
                        sg_user_id = int(rec["id"])
                        user_name = rec.get("name") or user_name
                except Exception:
                    pass

        # ── Step 6: Create session + mint DNA JWT ─────────────────────── #
        session_id = str(uuid.uuid4())
        jti = str(uuid.uuid4())
        session = UserSession(
            session_id=session_id,
            jti=jti,
            email=user_email,
            name=user_name,
            auth_provider="shotgrid_sso",
            shotgrid=ShotGridCredentials(
                user_id=sg_user_id,
                access_token=sg_native_token or aps_access_token,
                refresh_token=sg_native_refresh or aps_refresh_token,
            ),
        )
        self._sessions.create_session(session)

        access_token = self._mint_jwt(jti, session_id, user_email, user_name, sg_user_id)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._expire_seconds,
            "refresh_token": None,
            "user": {
                "id": sg_user_id,
                "email": user_email,
                "name": user_name,
                "shotgrid_user_id": sg_user_id,
            },
        }

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
            auth_provider="shotgrid_sso",
            shotgrid=ShotGridCredentials(
                user_id=user_info.sg_user_id,
                access_token=user_info.login or user_info.email,
                refresh_token=sg_token_set.refresh_token,
            ),
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

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        """Decode a JWT payload without signature verification."""
        import base64, json as _json
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return {}
            padding = "=" * (4 - len(parts[1]) % 4)
            payload = base64.urlsafe_b64decode(parts[1] + padding)
            return _json.loads(payload)
        except Exception:
            return {}

    def _extract_aps_identity(
        self,
        access_token: str,
        id_token: Optional[str] = None,
    ) -> tuple:
        """Return (email, name) from APS tokens using the best available method.

        Order of preference:
        1. id_token JWT claims  (present when 'openid' scope was requested)
        2. /authentication/v2/userinfo HTTP call  (requires user-profile:read)
        3. access_token JWT claims  (APS tokens are signed JWTs with identity info)

        Raises ValueError if no email can be determined.
        """
        import requests as _req

        def _claims_to_identity(claims: dict):
            email = (claims.get("email") or claims.get("emailId") or "").lower().strip()
            name = (
                claims.get("name")
                or (
                    f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip()
                )
                or claims.get("preferred_username")
                or email
            )
            return email, name

        # ── A: id_token JWT ───────────────────────────────────────────── #
        if id_token:
            claims = self._decode_jwt_payload(id_token)
            email, name = _claims_to_identity(claims)
            if email:
                return email, name

        # ── B: /authentication/v2/userinfo HTTP call ──────────────────── #
        try:
            resp = _req.get(
                "https://developer.api.autodesk.com/authentication/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if resp.ok:
                email, name = _claims_to_identity(resp.json())
                if email:
                    return email, name
            else:
                print(
                    f"[APS] /userinfo returned HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
        except Exception as exc:
            print(f"[APS] /userinfo request failed: {exc}")

        # ── C: access_token JWT claims ────────────────────────────────── #
        claims = self._decode_jwt_payload(access_token)
        email, name = _claims_to_identity(claims)
        if email:
            return email, name

        raise ValueError(
            "Could not determine user email from APS tokens. "
            "Ensure the 'openid' or 'user-profile:read' scope is granted "
            "in your APS application settings."
        )

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
