"""AYON login: the client, the provider's session lifecycle, and the HTTP endpoints.

A live login needs a real password, so these run against a scripted AYON.  They pin
the security properties DNA promises for ShotGrid and must keep for AYON: no stored
credential, immediate revocation, a deactivated account loses access on refresh, and
a failed or missing login name never degrades to service-account access.
"""

import json
import time
from unittest import mock

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

from dna import configsys
from dna.auth.ayon_auth_client import AyonAuthClient, AyonUnavailable
from dna.auth.errors import AuthBackendUnavailable
from dna.auth.session_store import AyonCredentials, UserSession
from dna.auth_providers.ayon_login import AyonLoginProvider
from dna.auth_providers.shotgrid_sso import (
    REFRESH_COOKIE_NAME,
    SessionAuthProvider,
    ShotGridSSOProvider,
)
from tests.test_auth_prod import _make_store

SECRET = "k" * 40
USERS = {
    "bob": {
        "name": "bob",
        "active": True,
        "attrib": {"fullName": "Bob Sup", "email": "bob@x.com"},
    },
    "noemail": {
        "name": "noemail",
        "active": True,
        "attrib": {"fullName": None, "email": None},
    },
    "gone": {
        "name": "gone",
        "active": False,
        "attrib": {"fullName": "Gone", "email": "gone@x.com"},
    },
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    monkeypatch.setenv("AYON_URL", "http://ayon.test")
    monkeypatch.setenv("AYON_API_KEY", "service-key")
    configsys.reset_config()
    yield
    configsys.reset_config()


# ── a scripted AYON HTTP surface ─────────────────────────────────────────────


class FakeHttp:
    """Answers AYON's login, logout and users GraphQL; records every call."""

    def __init__(self, passwords=None, users=None):
        self.passwords = passwords or {
            "bob": "right",
            "noemail": "right",
            "gone": "right",
        }
        self.users = USERS if users is None else users
        self.calls: list[dict] = []
        self.login_status = None  # force a status
        self.graphql_status = 200

    def request(self, method, url, timeout=None, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        path = url.split("ayon.test", 1)[1]
        response = mock.Mock()
        response.status_code = 200
        if path == "/api/auth/login":
            body = kw["json"]
            if self.login_status:
                response.status_code = self.login_status
                response.json.return_value = {}
            elif self.passwords.get(body["name"]) == body["password"]:
                response.json.return_value = {"token": "ayon-session-token", "user": {}}
            else:
                response.status_code = 401
                response.json.return_value = {"detail": "Invalid"}
        elif path == "/api/auth/logout":
            response.json.return_value = {"detail": "Logged out"}
        elif path == "/graphql":
            response.status_code = self.graphql_status
            variables = kw["json"]["variables"]["v"]
            if "users(names:" in kw["json"]["query"]:
                nodes = [self.users[n] for n in variables if n in self.users]
            else:
                nodes = [
                    u for u in self.users.values() if u["attrib"]["email"] in variables
                ]
            response.json.return_value = {
                "data": {"users": {"edges": [{"node": n} for n in nodes]}}
            }
        return response


def client(http=None) -> AyonAuthClient:
    return AyonAuthClient(session=http or FakeHttp())


# ── the auth client ──────────────────────────────────────────────────────────


def test_login_verifies_the_password_then_revokes_ayons_session():
    http = FakeHttp()
    assert client(http).login_user("bob", "right") == "bob"
    login, logout = [c for c in http.calls if "/api/auth/" in c["url"]]
    assert login["json"] == {"name": "bob", "password": "right"}
    assert logout["url"].endswith("/api/auth/logout")
    assert logout["headers"] == {"Authorization": "Bearer ayon-session-token"}


def test_an_email_address_is_resolved_to_the_ayon_user_name():
    http = FakeHttp()
    assert client(http).login_user("bob@x.com", "right") == "bob"
    login = next(c for c in http.calls if c["url"].endswith("/api/auth/login"))
    assert login["json"]["name"] == "bob"
    with pytest.raises(ValueError, match="Invalid username or password"):
        client(http).login_user("nobody@x.com", "right")


@pytest.mark.parametrize(
    "username, password",
    [("bob", "wrong"), ("bob", ""), ("", "right"), ("ghost", "right")],
)
def test_bad_credentials_are_rejected_without_saying_which_part_was_wrong(
    username, password
):
    with pytest.raises(ValueError, match="Invalid username or password") as err:
        client().login_user(username, password)
    assert not isinstance(err.value, AuthBackendUnavailable)


def test_empty_credentials_never_reach_ayon():
    http = FakeHttp()
    with pytest.raises(ValueError):
        client(http).login_user("bob", "")
    assert not [c for c in http.calls if c["url"].endswith("/api/auth/login")]


def test_a_login_that_returns_no_token_is_not_a_success():
    http = FakeHttp()
    http.passwords = {"bob": "right"}
    original = http.request

    def no_token(method, url, **kw):
        response = original(method, url, **kw)
        if url.endswith("/api/auth/login") and response.status_code == 200:
            response.json.return_value = {"redirectUrl": "https://sso.example"}
        return response

    http.request = no_token
    with pytest.raises(ValueError, match="did not sign this account in"):
        client(http).login_user("bob", "right")


def test_an_ayon_outage_is_unavailable_not_a_wrong_password():
    http = FakeHttp()
    http.login_status = 503
    with pytest.raises(AyonUnavailable):
        client(http).login_user("bob", "right")
    session = mock.Mock()
    session.request.side_effect = requests.ConnectionError("refused")
    with pytest.raises(AyonUnavailable):
        AyonAuthClient(session=session).login_user("bob", "right")
    assert issubclass(AyonUnavailable, AuthBackendUnavailable)


def test_a_rejected_service_key_is_an_operator_problem_not_a_login_failure():
    http = FakeHttp()
    http.graphql_status = 401
    with pytest.raises(AyonUnavailable, match="service key"):
        client(http).get_user_info("bob")


def test_user_info_requires_an_active_account_with_an_email():
    info = client().get_user_info("bob")
    assert (info.login, info.email, info.name) == ("bob", "bob@x.com", "Bob Sup")
    with pytest.raises(ValueError, match="no email address"):
        client().get_user_info("noemail")
    with pytest.raises(ValueError, match="not active"):
        client().get_user_info("gone")
    with pytest.raises(ValueError, match="not active"):
        client().get_user_info("missing")
    with pytest.raises(ValueError, match="no longer active"):
        client().confirm_user_active("gone")
    client().confirm_user_active("bob")


# ── the provider ─────────────────────────────────────────────────────────────


def make_provider(http=None):
    store = _make_store()
    return AyonLoginProvider(session_store=store, ayon_auth_client=client(http)), store


def test_it_is_a_session_provider_alongside_shotgrid():
    provider, _ = make_provider()
    assert isinstance(provider, SessionAuthProvider)
    assert not isinstance(provider, ShotGridSSOProvider)
    assert provider.get_login_info() == {"mode": "pat", "tracker": "ayon"}


def test_login_opens_a_session_that_holds_a_login_name_and_no_credential():
    provider, store = make_provider()
    result = provider.login("bob", "right")
    assert result.refresh_cookie and result.body["token_type"] == "Bearer"
    assert result.body["user"] == {"id": "bob", "email": "bob@x.com", "name": "Bob Sup"}

    claims, session = provider.authenticate(result.body["access_token"])
    assert claims["sub"] == "bob" and claims["email"] == "bob@x.com"
    assert session.auth_provider == "ayon_login"
    assert session.ayon == AyonCredentials(username="bob") and session.shotgrid is None
    assert (session.tracker_login, session.tracker_user_id) == ("bob", "bob")

    stored = json.dumps(store._sessions.docs, default=str)
    for secret in (
        "right",
        "ayon-session-token",
        "service-key",
        result.refresh_cookie.split(".", 1)[-1],
    ):
        assert secret not in stored, f"{secret!r} must never be stored"


def test_the_access_token_carries_no_credential():
    provider, _ = make_provider()
    token = provider.login("bob", "right").body["access_token"]
    import jwt

    payload = jwt.decode(token, options={"verify_signature": False})
    assert set(payload) == {
        "jti",
        "sub",
        "session_id",
        "email",
        "name",
        "iat",
        "exp",
        "iss",
        "aud",
    }


def test_a_failed_login_opens_no_session():
    provider, store = make_provider()
    with pytest.raises(ValueError):
        provider.login("bob", "wrong")
    with pytest.raises(ValueError, match="no email address"):
        provider.login("noemail", "right")
    assert store._sessions.docs == {}


def test_refresh_rotates_and_rechecks_the_account():
    provider, store = make_provider()
    first = provider.login("bob", "right")
    second = provider.refresh_session(first.refresh_cookie)
    assert second.refresh_cookie and second.refresh_cookie != first.refresh_cookie
    assert provider.authenticate(second.body["access_token"])[1].tracker_login == "bob"


def test_a_deactivated_account_loses_the_session_at_the_next_refresh():
    http = FakeHttp()
    provider, store = make_provider(http)
    first = provider.login("bob", "right")
    http.users = {**USERS, "bob": {**USERS["bob"], "active": False}}
    with pytest.raises(ValueError, match="no longer active. Please log in again"):
        provider.refresh_session(first.refresh_cookie)
    with pytest.raises(ValueError, match="session has ended"):
        provider.authenticate(first.body["access_token"])  # every token for it is dead
    assert store._sessions.docs == {}


def test_an_ayon_outage_during_refresh_keeps_the_session():
    http = FakeHttp()
    provider, _ = make_provider(http)
    first = provider.login("bob", "right")
    http.graphql_status = 503
    second = provider.refresh_session(first.refresh_cookie)  # rechecked next time
    assert provider.authenticate(second.body["access_token"])


def test_a_session_without_a_login_name_cannot_be_renewed():
    provider, store = make_provider()
    first = provider.login("bob", "right")
    session = next(iter(store._sessions.docs.values()))
    session["ayon"] = {"username": ""}
    with pytest.raises(ValueError, match="cannot be renewed"):
        provider.refresh_session(first.refresh_cookie)


def test_a_replayed_refresh_cookie_revokes_the_session():
    provider, store = make_provider()
    first = provider.login("bob", "right")
    provider.refresh_session(first.refresh_cookie)
    session = next(iter(store._sessions.docs.values()))
    session["dna_refresh_rotated_at"] = time.time() - 3600  # outside the grace window
    with pytest.raises(ValueError, match="used from another location"):
        provider.refresh_session(first.refresh_cookie)
    assert store._sessions.docs == {}


def test_logout_and_logout_everywhere_end_the_session_immediately():
    provider, store = make_provider()
    a, b = provider.login("bob", "right"), provider.login("bob", "right")
    provider.logout(a.body["access_token"], None)
    with pytest.raises(ValueError):
        provider.authenticate(a.body["access_token"])
    assert provider.logout_all(b.body["access_token"]) == 1
    with pytest.raises(ValueError):
        provider.authenticate(b.body["access_token"])


def test_a_short_jwt_secret_is_refused(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "short")
    configsys.reset_config()
    with pytest.raises(ValueError, match="at least 32"):
        AyonLoginProvider(session_store=_make_store(), ayon_auth_client=client())


def test_selected_by_auth_provider_ayon(monkeypatch):
    from dna.auth_providers.auth_provider_base import get_auth_provider

    monkeypatch.setenv("AUTH_PROVIDER", "ayon")
    with mock.patch(
        "dna.auth_providers.shotgrid_sso._lazy_session_store",
        return_value=_make_store(),
    ):
        assert isinstance(get_auth_provider(), AyonLoginProvider)


# ── HTTP endpoints ───────────────────────────────────────────────────────────


@pytest.fixture
def api(monkeypatch):
    import main

    monkeypatch.setenv("AUTH_PROVIDER", "ayon")
    configsys.reset_config()
    http = FakeHttp()
    provider, _ = make_provider(http)
    main.app.dependency_overrides[main.get_auth_provider_cached] = lambda: provider
    yield TestClient(main.app), provider, http
    main.app.dependency_overrides.pop(main.get_auth_provider_cached, None)


def test_login_endpoint_sets_the_refresh_cookie_and_returns_a_token(api):
    client_, _, _ = api
    response = client_.post(
        "/auth/login", json={"username": "bob", "password": "right"}
    )
    assert response.status_code == 200
    assert response.json()["user"]["id"] == "bob"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{REFRESH_COOKIE_NAME}=") and "HttpOnly" in cookie


def test_login_info_endpoint_reports_the_mode(api):
    client_, _, _ = api
    assert client_.get("/auth/login").json() == {"mode": "pat", "tracker": "ayon"}


def test_wrong_password_is_401_and_an_outage_is_503_without_leaking_details(api):
    client_, _, http = api
    assert (
        client_.post(
            "/auth/login", json={"username": "bob", "password": "no"}
        ).status_code
        == 401
    )
    http.login_status = 503
    response = client_.post(
        "/auth/login", json={"username": "bob", "password": "right"}
    )
    assert response.status_code == 503
    assert (
        response.json()["detail"]
        == "AYON is unavailable right now. Please try again shortly."
    )
    assert "ayon.test" not in response.text


def test_me_endpoint_reports_the_tracker_identity(api):
    client_, _, _ = api
    token = client_.post(
        "/auth/login", json={"username": "bob", "password": "right"}
    ).json()["access_token"]
    me = client_.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me == {
        "email": "bob@x.com",
        "name": "Bob Sup",
        "tracker_user_id": "bob",
    }  # no shotgrid_user_id


def test_requests_are_scoped_to_the_signed_in_ayon_user(api):
    import main

    _, provider, _ = api
    token = provider.login("bob", "right").body["access_token"]
    session = provider.authenticate(token)[1]
    with mock.patch.object(main, "get_prodtrack_provider") as factory:
        main.get_user_scoped_prodtrack_provider(
            main.AuthContext(email="bob@x.com", session=session), provider
        )
    factory.assert_called_once_with(sudo_login="bob", session_id=session.session_id)


def test_a_session_without_a_login_name_fails_closed(api):
    import main

    _, provider, _ = api
    blank = UserSession(
        session_id="s",
        jti="j",
        email="bob@x.com",
        name="Bob",
        auth_provider="ayon_login",
        ayon=AyonCredentials(username=""),
    )
    with mock.patch.object(main, "get_prodtrack_provider") as factory:
        with pytest.raises(HTTPException) as err:
            main.get_user_scoped_prodtrack_provider(
                main.AuthContext(email="bob@x.com", session=blank), provider
            )
    assert err.value.status_code == 401
    assert (
        err.value.detail
        == "Session is missing the AYON login name. Please log in again."
    )
    factory.assert_not_called()  # never degraded to the service account


def test_cors_credentials_are_enabled_for_ayon_login_too(monkeypatch):
    from dna.cors_settings import get_cors_middleware_kwargs

    monkeypatch.setenv("AUTH_PROVIDER", "ayon")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://dna.example")
    configsys.reset_config()
    kwargs = get_cors_middleware_kwargs()
    assert kwargs["allow_credentials"] is True and kwargs["allow_origins"] == [
        "https://dna.example"
    ]


# ── login throttling ─────────────────────────────────────────────────────────


@pytest.fixture
def throttled(api):
    from dna.auth.throttle import reset_login_throttle

    reset_login_throttle()
    yield api[0]
    reset_login_throttle()


def attempt(client, password):
    return client.post("/auth/login", json={"username": "bob", "password": password})


def test_the_route_locks_out_after_repeated_failures_and_says_when_to_retry(throttled):
    for _ in range(10):
        assert attempt(throttled, "wrong").status_code == 401
    locked = attempt(throttled, "wrong")
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    # even the right password waits: otherwise the lock would not stop guessing
    assert attempt(throttled, "right").status_code == 429


def test_a_correct_login_is_never_throttled_for_a_user_who_has_not_failed(throttled):
    assert attempt(throttled, "right").status_code == 200


def test_the_throttle_can_be_turned_off(throttled, monkeypatch):
    configsys.get_config().apply_overrides(
        {"auth": {"login_throttle": {"enabled": False}}}, allow_locked=True
    )
    for _ in range(12):
        assert attempt(throttled, "wrong").status_code == 401
