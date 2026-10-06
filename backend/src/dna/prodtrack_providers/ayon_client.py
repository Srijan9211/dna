"""A small AYON server client: REST + GraphQL, with DNA's error semantics.

Deliberately thin.  It authenticates with a service-user API key and, when asked,
acts as another user through the ``x-as-user`` header, so AYON applies *that* user's
own access (the same model as ShotGrid's ``sudo_as_login``).  Every failure is
translated into the domain errors the rest of DNA already understands, so the API
layer returns 401 / 403 / 404 / 503 without knowing it is talking to AYON.
"""

from __future__ import annotations

from typing import Any, Optional

import requests

from dna.prodtrack_providers.prodtrack_provider_base import (
    ProdtrackAuthError,
    ProdtrackPermissionError,
    ProdtrackUnavailableError,
)

AS_USER_HEADER = "x-as-user"

# GraphQL reports problems in a 200 response body.  These substrings of an error
# message mean "you may not", the rest are treated as the server failing.
_FORBIDDEN_MARKERS = ("forbidden", "permission", "not allowed", "access denied")
_NOT_FOUND_MARKERS = ("not found", "does not exist")


class AyonNotFound(ValueError):
    """The requested entity or project does not exist (surfaces as HTTP 404)."""


class AyonClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        as_user: Optional[str] = None,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
        as_user_header: str = AS_USER_HEADER,
    ):
        self.url = (url or "").rstrip("/")
        if not self.url:
            raise ValueError("AYON_URL is required.")
        if not api_key:
            raise ValueError("AYON_API_KEY is required.")
        self._api_key = api_key
        self.as_user = as_user
        self.timeout = timeout
        self._header_name = as_user_header
        self._session = session or requests.Session()

    # ── identity ──────────────────────────────────────────────────────────

    def acting_as(self, login: Optional[str]) -> "AyonClient":
        """A client sharing this one's connection that acts as ``login``."""
        return AyonClient(
            self.url,
            self._api_key,
            as_user=login,
            timeout=self.timeout,
            session=self._session,
            as_user_header=self._header_name,
        )

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {"X-Api-Key": self._api_key}
        if self.as_user:
            headers[self._header_name] = self.as_user
        headers.update(extra or {})
        return headers

    # ── transport ─────────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict[str, Any]] = None,
        data: Any = None,
        headers: Optional[dict[str, str]] = None,
        stream: bool = False,
    ) -> requests.Response:
        """Perform a request and return the response, raising domain errors."""
        try:
            response = self._session.request(
                method,
                f"{self.url}{path}",
                json=json,
                params=params,
                data=data,
                headers=self._headers(headers),
                timeout=self.timeout,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise ProdtrackUnavailableError(f"AYON is unreachable: {exc}") from exc
        self._raise_for_status(response)
        return response

    def rest(self, method: str, path: str, **kwargs: Any) -> Any:
        """REST call returning the decoded JSON body (``{}`` if empty)."""
        response = self.request(method, path, **kwargs)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProdtrackUnavailableError("AYON returned a non-JSON response") from exc

    def graphql(self, query: str, variables: Optional[dict[str, Any]] = None) -> dict:
        """Run a GraphQL query and return its ``data``."""
        payload = self.rest("POST", "/graphql", json={"query": query, "variables": variables or {}})
        errors = payload.get("errors")
        if errors:
            raise self._graphql_error(errors)
        return payload.get("data") or {}

    def graphql_all(
        self,
        query: str,
        path: tuple[str, ...],
        variables: Optional[dict[str, Any]] = None,
        *,
        page_size: int = 200,
        max_pages: int = 50,
    ) -> list[dict]:
        """Collect every node of a paginated connection.

        ``query`` must declare ``$first: Int`` and ``$after: String``, pass them to
        the connection at ``path`` and select ``pageInfo { hasNextPage endCursor }``.
        """
        nodes: list[dict] = []
        after: Optional[str] = None
        for _ in range(max_pages):
            data = self.graphql(query, {**(variables or {}), "first": page_size, "after": after})
            connection: Any = data
            for key in path:
                connection = (connection or {}).get(key)
            if not connection:
                break
            nodes.extend(edge["node"] for edge in connection.get("edges", []) if edge.get("node"))
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
        return nodes

    # ── errors ────────────────────────────────────────────────────────────

    @staticmethod
    def _detail(response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return (response.text or "")[:200]
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("message") or body)[:300]
        return str(body)[:300]

    def _raise_for_status(self, response: requests.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = self._detail(response)
        if status == 401:
            raise ProdtrackAuthError(
                "AYON rejected the credentials or the impersonated user. Your "
                "access may have been revoked. Please log in again."
            )
        if status == 403:
            raise ProdtrackPermissionError(f"AYON denied access to this resource: {detail}")
        if status == 404:
            raise AyonNotFound(f"Not found in AYON: {detail}")
        # AYON answers a bare 500 when the x-as-user target does not exist, which
        # is indistinguishable from a real outage here.  The provider therefore
        # verifies the user it impersonates before relying on the header.
        raise ProdtrackUnavailableError(f"AYON request failed ({status}): {detail}")

    @staticmethod
    def _graphql_error(errors: list[dict]) -> Exception:
        message = "; ".join(str(e.get("message", e)) for e in errors)
        lowered = message.lower()
        if any(m in lowered for m in _FORBIDDEN_MARKERS):
            return ProdtrackPermissionError(f"AYON denied access: {message}")
        if any(m in lowered for m in _NOT_FOUND_MARKERS):
            return AyonNotFound(f"Not found in AYON: {message}")
        return ProdtrackUnavailableError(f"AYON GraphQL error: {message}")
