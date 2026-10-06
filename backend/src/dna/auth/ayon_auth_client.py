"""AYON authentication client.

Verifies a user's password with AYON and resolves who they are.  DNA keeps no AYON
credential: the password is checked once and dropped, and the session token AYON
issues for the check is revoked immediately, so nothing renewable is left behind.
After login every request reaches AYON with DNA's service key and ``x-as-user``.

Identity checks use the service key, so the user's own token is never needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import requests

from dna.auth.errors import AuthBackendUnavailable
from dna.configsys import get_config

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 15.0


class AyonUnavailable(AuthBackendUnavailable):
    """AYON could not be reached or failed to answer. Transient: retry."""


@dataclass
class AyonUserInfo:
    login: str
    email: str
    name: str


class AyonAuthClient:
    def __init__(
        self,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        cfg = get_config().get("prodtrack").ayon
        self.url = (url or cfg.url or "").rstrip("/")
        if not self.url:
            raise ValueError("AYON_URL is required.")
        self._api_key = api_key or cfg.api_key
        self._timeout = cfg.timeout or _REQUEST_TIMEOUT_SECONDS
        self._http = session or requests.Session()

    # ── transport ─────────────────────────────────────────────────────────

    def _call(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            return self._http.request(
                method, f"{self.url}{path}", timeout=self._timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise AyonUnavailable(f"AYON is unreachable: {exc}") from exc

    def _service_graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise ValueError(
                "AYON_API_KEY is required to resolve user identity. Set it in your "
                "environment."
            )
        response = self._call(
            "POST",
            "/graphql",
            json={"query": query, "variables": variables},
            headers={"X-Api-Key": self._api_key},
        )
        if response.status_code >= 500:
            raise AyonUnavailable(f"AYON answered {response.status_code}")
        if response.status_code in (401, 403):
            # The *service* key is wrong: an operator problem, not the user's.
            raise AyonUnavailable("DNA's AYON service key was rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AyonUnavailable("AYON returned a non-JSON response") from exc
        if payload.get("errors"):
            raise AyonUnavailable(f"AYON GraphQL error: {payload['errors']}")
        return payload.get("data") or {}

    # ── identity ──────────────────────────────────────────────────────────

    def _find_user(self, *, name: Optional[str] = None, email: Optional[str] = None) -> Optional[dict]:
        if name is not None:
            query, variables = (
                "query($v: [String!]) { users(names: $v) { edges { node { name active attrib { fullName email } } } } }",
                {"v": [name]},
            )
        else:
            query, variables = (
                "query($v: [String!]) { users(emails: $v) { edges { node { name active attrib { fullName email } } } } }",
                {"v": [email]},
            )
        edges = self._service_graphql(query, variables).get("users", {}).get("edges", [])
        return edges[0]["node"] if edges else None

    def resolve_login(self, identifier: str) -> str:
        """The AYON user name for what was typed: a name, or an email address."""
        identifier = (identifier or "").strip()
        if "@" in identifier:
            node = self._find_user(email=identifier)
            if node is None:
                raise ValueError("Invalid username or password.")
            return node["name"]
        return identifier

    def login_user(self, username: str, password: str) -> str:
        """Verify the credentials with AYON; return the AYON user name.

        The session AYON opens for the check is revoked straight away.

        Raises:
            ValueError: AYON rejected the credentials.
            AyonUnavailable: AYON did not answer.
        """
        login = self.resolve_login(username)
        if not login or not password:
            raise ValueError("Invalid username or password.")
        response = self._call("POST", "/api/auth/login", json={"name": login, "password": password})
        if response.status_code in (400, 401, 403, 404):
            raise ValueError("Invalid username or password.")
        if response.status_code >= 400:
            raise AyonUnavailable(f"AYON login failed with status {response.status_code}")
        try:
            token = (response.json() or {}).get("token")
        except ValueError:
            token = None
        if not token:
            # 200 without a token: AYON asked for something we cannot do (SSO
            # redirect, password reset).  Treat it as a refusal, not a success.
            raise ValueError("AYON did not sign this account in with a password.")
        try:
            self._call("POST", "/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
        except AyonUnavailable:
            logger.warning("Could not revoke AYON's verification session; it will expire on its own.")
        return login

    def get_user_info(self, login: str) -> AyonUserInfo:
        """Resolve an active AYON user.  DNA identifies people by email, so one is required."""
        node = self._find_user(name=login)
        if node is None or not node.get("active", True):
            raise ValueError("This AYON account is not active.")
        attrib = node.get("attrib") or {}
        email = (attrib.get("email") or "").strip()
        if not email:
            raise ValueError(
                "Your AYON account has no email address, which DNA needs to identify "
                "you. Ask an administrator to add one."
            )
        return AyonUserInfo(login=node["name"], email=email, name=attrib.get("fullName") or node["name"])

    def confirm_user_active(self, login: str) -> None:
        """Raise ``ValueError`` if the account may no longer sign in."""
        node = self._find_user(name=login)
        if node is None or not node.get("active", True):
            raise ValueError("This AYON account is no longer active.")
