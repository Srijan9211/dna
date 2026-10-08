"""The /config admin API: access rules, changes, history, and the handshake endpoint."""

import main
import pytest
from fastapi.testclient import TestClient

from dna import configsys

ADMIN, USER = "admin@x.com", "artist@x.com"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    configsys.reset_config()
    yield
    main.app.dependency_overrides.pop(main.get_auth_context, None)
    configsys.reset_config()


def call(client, method, path):
    """Call a route with the body its method needs (GET takes none)."""
    body = {} if method in ("post", "put") else None
    return client.request(method.upper(), path, json=body)


def as_user(email):
    main.app.dependency_overrides[main.get_auth_context] = lambda: main.AuthContext(
        email=email
    )


@pytest.fixture
def api(monkeypatch):
    """Admin API on, authentication on, ADMIN allowed, signed in as ADMIN."""
    monkeypatch.setenv("DNA_CONFIG_ADMIN_ENABLED", "true")
    monkeypatch.setenv("DNA_CONFIG_ADMINS", f"{ADMIN}, other@x.com")
    monkeypatch.setenv("AUTH_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client")
    configsys.reset_config()
    as_user(ADMIN)
    return TestClient(main.app)


# ── who may administer ───────────────────────────────────────────────────────

ADMIN_ROUTES = [
    ("get", "/config/schema"),
    ("get", "/config/effective"),
    ("get", "/config/history"),
    ("get", "/config/export"),
    ("post", "/config/validate"),
    ("put", "/config/features"),
    ("post", "/config/rollback/1"),
    ("post", "/config/import"),
    ("post", "/config/prodtrack/handshake"),
]


@pytest.mark.parametrize("method, path", ADMIN_ROUTES)
def test_the_admin_api_does_not_exist_until_enabled(method, path):
    response = call(TestClient(main.app), method, path)
    assert response.status_code == 404  # not even a hint that it is there


@pytest.mark.parametrize("method, path", ADMIN_ROUTES)
def test_without_authentication_nobody_can_administer(method, path, monkeypatch):
    monkeypatch.setenv("DNA_CONFIG_ADMIN_ENABLED", "true")
    configsys.reset_config()
    response = call(TestClient(main.app), method, path)
    assert response.status_code == 403
    assert "authentication is enabled" in response.json()["detail"]


def test_development_can_opt_in_to_unauthenticated_administration(monkeypatch):
    monkeypatch.setenv("DNA_CONFIG_ADMIN_ENABLED", "true")
    monkeypatch.setenv("DNA_CONFIG_ALLOW_UNAUTHENTICATED", "true")
    configsys.reset_config()
    assert TestClient(main.app).get("/config/schema").status_code == 200


def test_the_unauthenticated_opt_in_is_ignored_once_authentication_is_on(
    monkeypatch, api
):
    monkeypatch.setenv("DNA_CONFIG_ALLOW_UNAUTHENTICATED", "true")
    as_user(USER)  # authenticated, but not on the admin list
    assert api.get("/config/schema").status_code == 403


@pytest.mark.parametrize("method, path", ADMIN_ROUTES)
def test_only_listed_admins_may_use_it(method, path, api):
    as_user(USER)
    assert call(api, method, path).status_code == 403


def test_admin_emails_match_case_insensitively(api):
    as_user("ADMIN@X.COM")
    assert api.get("/config/schema").status_code == 200


# ── public ───────────────────────────────────────────────────────────────────


def test_public_config_is_open_and_contains_nothing_secret(monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "ayon")
    monkeypatch.setenv("AYON_API_KEY", "super-secret-key")
    monkeypatch.setenv("AYON_URL", "http://internal-ayon:5000")
    monkeypatch.setenv("JWT_SECRET_KEY", "j" * 40)
    configsys.reset_config()
    response = TestClient(main.app).get("/config/public")  # no credentials
    assert response.status_code == 200
    body = response.json()
    assert body["auth"] == {"provider": "none"}
    assert (
        body["prodtrack"]["connector"] == "ayon"
        and body["prodtrack"]["labels"]["playlist"] == "List"
    )
    assert body["prodtrack"]["capabilities"]["note_attachments"] is True
    assert body["prodtrack"]["roles"]["reviewable"] == ["version"]
    for secret in ("super-secret-key", "internal-ayon", "j" * 40):
        assert secret not in response.text


def test_public_config_exposes_the_google_client_id_the_browser_needs(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com")
    configsys.reset_config()
    assert TestClient(main.app).get("/config/public").json()["auth"] == {
        "provider": "google",
        "google_client_id": "abc.apps.googleusercontent.com",
    }


def test_public_config_follows_studio_vocabulary_overrides(api):
    api.put(
        "/config/prodtrack", json={"values": {"profile": {"labels": {"shot": "Plate"}}}}
    )
    assert api.get("/config/public").json()["prodtrack"]["labels"]["shot"] == "Plate"


# ── reading ──────────────────────────────────────────────────────────────────


def test_effective_config_masks_secrets_and_shows_provenance(api, monkeypatch):
    monkeypatch.setenv("SHOTGRID_API_KEY", "sg-secret")
    body = api.get("/config/effective").json()
    assert body["config"]["prodtrack"]["shotgrid"]["api_key"] == "********"
    assert "sg-secret" not in api.get("/config/effective").text
    assert body["provenance"]["prodtrack.shotgrid.api_key"] == "env:SHOTGRID_API_KEY"
    assert body["provenance"]["auth.provider"] == "env:AUTH_PROVIDER"
    assert body["revision"] == 0


def test_schema_describes_every_section_and_flags_secrets(api):
    schema = api.get("/config/schema").json()
    assert {"prodtrack", "llm", "admin"} <= set(schema)
    ayon = schema["prodtrack"]["$defs"]["AyonConnection"]["properties"]["api_key"]
    assert ayon["secret"] is True


# ── changing ─────────────────────────────────────────────────────────────────


def test_validate_reports_without_applying(api):
    ok = api.post(
        "/config/validate", json={"patch": {"features": {"transcript_publish": True}}}
    ).json()
    assert ok == {
        "valid": True,
        "issues": [],
        "changed": ["features.transcript_publish"],
        "restart_required": [],
    }
    bad = api.post(
        "/config/validate",
        json={"patch": {"features": {"transcript_publish": "banana"}}},
    ).json()
    assert (
        bad["valid"] is False
        and bad["issues"][0]["path"] == "features.transcript_publish"
    )
    assert (
        api.get("/config/effective").json()["config"]["features"]["transcript_publish"]
        is False
    )


def test_changing_a_section_takes_effect_and_is_recorded_against_the_admin(api):
    response = api.put(
        "/config/features",
        json={"values": {"transcript_publish": True}, "note": "go live"},
    )
    assert response.status_code == 200 and response.json()["revision"] == 1
    assert (
        api.get("/config/effective").json()["config"]["features"]["transcript_publish"]
        is True
    )
    (rev,) = api.get("/config/history").json()
    assert (rev["actor"], rev["note"], rev["changed"]) == (
        ADMIN,
        "go live",
        ["features.transcript_publish"],
    )


def test_invalid_values_are_a_422_with_every_problem(api):
    response = api.put(
        "/config/llm", json={"values": {"profiles": {"openai": {"timeout": -1}}}}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert (
        detail["message"] == "Invalid configuration"
        and "timeout" in detail["issues"][0]["path"]
    )
    assert api.get("/config/history").json() == []  # nothing recorded


@pytest.mark.parametrize("section", ["auth", "storage", "server", "admin"])
def test_bootstrap_sections_cannot_be_changed_through_the_api(section, api):
    body = {
        "values": {
            "provider": "none",
            "mongodb_db": "x",
            "log_level": "debug",
            "admins": ["me@x.com"],
        }
    }
    values = {
        "auth": {"provider": "none"},
        "storage": {"mongodb_db": "x"},
        "server": {"log_level": "debug"},
        "admin": {"admins": ["me@x.com"]},
    }[section]
    response = api.put(f"/config/{section}", json={"values": values})
    assert response.status_code == 422
    assert "cannot be changed at runtime" in response.text
    assert (
        api.get("/config/effective").json()["config"]["auth"]["provider"] == "google"
    )  # untouched


def test_a_plaintext_secret_is_refused_but_a_reference_is_accepted(api, monkeypatch):
    monkeypatch.setenv("MY_KEY", "key-from-env")
    monkeypatch.setenv("VEXA_API_KEY", "key-from-env")
    refused = api.put(
        "/config/transcription", json={"values": {"vexa": {"api_key": "plaintext"}}}
    )
    assert refused.status_code == 422 and "reference" in refused.text
    assert "plaintext" not in api.get("/config/history").text
    arbitrary = api.put(
        "/config/transcription", json={"values": {"vexa": {"api_key": "${env:MY_KEY}"}}}
    )
    assert arbitrary.status_code == 422  # not a variable a secret setting reads
    accepted = api.put(
        "/config/transcription",
        json={"values": {"vexa": {"api_key": "${env:VEXA_API_KEY}"}}},
    )
    assert accepted.status_code == 200
    assert (
        "key-from-env" not in api.get("/config/effective").text
    )  # masked, never echoed


def test_an_unknown_section_is_a_404(api):
    assert api.put("/config/nonesuch", json={"values": {}}).status_code == 404


def test_a_stale_save_is_a_409_not_a_silent_overwrite(api):
    api.put("/config/llm", json={"values": {"provider": "gemini"}})
    stale = api.put(
        "/config/llm", json={"values": {"provider": "anthropic"}, "expected_version": 0}
    )
    assert stale.status_code == 409
    assert api.get("/config/effective").json()["config"]["llm"]["provider"] == "gemini"
    assert (
        api.put(
            "/config/llm",
            json={"values": {"provider": "anthropic"}, "expected_version": 1},
        ).status_code
        == 200
    )


# ── history, rollback, export/import ─────────────────────────────────────────


def test_rollback_restores_an_earlier_state_as_a_new_revision(api):
    api.put("/config/features", json={"values": {"transcript_publish": True}})
    api.put("/config/llm", json={"values": {"provider": "gemini"}})
    response = api.post("/config/rollback/1")
    assert response.status_code == 200 and response.json()["revision"] == 3
    config = api.get("/config/effective").json()["config"]
    assert (
        config["llm"]["provider"] == "openai"
        and config["features"]["transcript_publish"] is True
    )
    assert [r["version"] for r in api.get("/config/history").json()] == [3, 2, 1]
    assert api.post("/config/rollback/99").status_code == 404


def test_export_then_import_reproduces_the_overrides(api):
    api.put("/config/features", json={"values": {"transcript_publish": True}})
    document = api.get("/config/export").json()
    assert document["overrides"] == {"features": {"transcript_publish": True}}
    api.put("/config/llm", json={"values": {"provider": "gemini"}})
    assert api.post("/config/import", json={"document": document}).status_code == 200
    config = api.get("/config/effective").json()["config"]
    assert (
        config["llm"]["provider"] == "openai"
        and config["features"]["transcript_publish"] is True
    )
    assert api.post("/config/import", json={"document": {"nope": 1}}).status_code == 400
    locked = {"format": 1, "overrides": {"auth": {"provider": "none"}}}
    assert api.post("/config/import", json={"document": locked}).status_code == 422


# ── handshake ────────────────────────────────────────────────────────────────


def test_handshake_checks_the_current_configuration(api, monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "mock")
    configsys.reset_config()
    report = api.post("/config/prodtrack/handshake", json={}).json()
    assert (
        report["ok"] is True
        and report["connector"] == "mock"
        and report["applied"] is False
    )
    assert {c["name"] for c in report["checks"]} >= {"describe", "connect", "discover"}


def test_handshake_tries_a_candidate_without_applying_it(api, monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "mock")
    configsys.reset_config()
    body = {"candidate": {"prodtrack": {"profile": {"labels": {"shot": "Plate"}}}}}
    assert api.post("/config/prodtrack/handshake", json=body).json()["applied"] is False
    assert (
        api.get("/config/public").json()["prodtrack"]["labels"]["shot"] == "Shot"
    )  # unchanged


def test_handshake_applies_a_candidate_only_when_every_check_passes(api, monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "mock")
    configsys.reset_config()
    body = {
        "candidate": {"prodtrack": {"profile": {"labels": {"shot": "Plate"}}}},
        "apply": True,
        "note": "relabel",
    }
    report = api.post("/config/prodtrack/handshake", json=body).json()
    assert report["applied"] is True and report["revision"] == 1
    assert api.get("/config/public").json()["prodtrack"]["labels"]["shot"] == "Plate"
    assert api.get("/config/history").json()[0]["note"] == "relabel"


def test_a_failing_handshake_is_never_saved(api, monkeypatch):
    class Failing:
        @classmethod
        def for_handshake(cls, cfg, profile):
            raise ValueError("SHOTGRID_URL is required.")

    monkeypatch.setitem(configsys.providers.prodtrack._explicit, "shotgrid", Failing)
    body = {
        "candidate": {"prodtrack": {"profile": {"labels": {"shot": "Plate"}}}},
        "apply": True,
    }
    response = api.post("/config/prodtrack/handshake", json=body)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["message"] == "The handshake has failing checks; nothing was saved."
    assert detail["report"]["ok"] is False and detail["report"]["applied"] is False
    assert api.get("/config/history").json() == []


def test_handshake_rejects_what_a_real_change_would_reject(api):
    for candidate in (
        {"auth": {"provider": "none"}},
        {"prodtrack": {"ayon": {"api_key": "plaintext"}}},
    ):
        response = api.post(
            "/config/prodtrack/handshake", json={"candidate": candidate}
        )
        assert response.status_code == 422


def test_handshake_with_an_unknown_connector_is_a_clear_422(api):
    response = api.post(
        "/config/prodtrack/handshake",
        json={"candidate": {"prodtrack": {"connector": "nonesuch"}}},
    )
    assert response.status_code == 422 and "nonesuch" in response.text


# ── frontend settings and the admin-page access check ────────────────────────


def test_public_config_carries_what_used_to_be_build_time_frontend_settings(
    monkeypatch,
):
    monkeypatch.setenv("DNA_FEATURE_AI", "false")
    monkeypatch.setenv("DNA_FEATURE_TRANSCRIPTION", "true")
    monkeypatch.setenv("DNA_WS_URL", "wss://dna.example/ws")
    monkeypatch.setenv("DNA_PRODTRACK_TAB_SYNC_EXTENSION_ID", "abcdef")
    monkeypatch.setenv("DNA_DEV_MODE", "true")
    configsys.reset_config()
    frontend = TestClient(main.app).get("/config/public").json()["frontend"]
    assert frontend == {
        "features": {
            "transcription": True,
            "in_review": None,
            "ai": False,
        },  # None = the app's own default
        "ws_url": "wss://dna.example/ws",
        "prodtrack_tab_sync_extension_id": "abcdef",
        "dev_mode": True,
    }


def test_public_config_defaults_leave_every_frontend_choice_to_the_app():
    frontend = TestClient(main.app).get("/config/public").json()["frontend"]
    assert frontend["features"] == {
        "transcription": None,
        "in_review": None,
        "ai": None,
    }
    assert frontend["dev_mode"] is False and frontend["ws_url"] is None


def test_frontend_settings_can_be_changed_live_through_the_admin_api(api):
    api.put("/config/frontend", json={"values": {"feature_ai": False}})
    assert api.get("/config/public").json()["frontend"]["features"]["ai"] is False


def test_access_tells_the_ui_who_may_see_the_admin_pages(api):
    assert api.get("/config/access").json() == {"admin": True}
    as_user(USER)
    assert api.get("/config/access").json() == {
        "admin": False
    }  # not an error: just no admin pages


def test_access_never_reveals_that_the_admin_api_exists(monkeypatch):
    # off by default: indistinguishable from a deployment without the feature
    assert TestClient(main.app).get("/config/access").json() == {"admin": False}


def test_effective_lists_the_locked_sections_so_the_ui_can_show_a_lock(api):
    body = api.get("/config/effective").json()
    assert sorted(body["locked_sections"]) == ["admin", "auth", "server", "storage"]
    assert "prodtrack" not in body["locked_sections"]


# ── identity: tracker logins, not tracker-supplied emails ────────────────────


class _Session:
    def __init__(self, login):
        self.tracker_login = login


def as_tracker_user(login, email):
    main.app.dependency_overrides[main.get_auth_context] = lambda: main.AuthContext(
        email=email, session=_Session(login)
    )


def test_a_tracker_user_is_matched_by_login_not_by_email(api, monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "ayon")
    monkeypatch.setenv("DNA_CONFIG_ADMINS", "alice")
    configsys.reset_config()
    as_tracker_user("alice", "someone-else@x.com")
    assert api.get("/config/effective").status_code == 200


def test_a_tracker_user_cannot_become_admin_by_setting_their_email(api, monkeypatch):
    """The email on a tracker account is a profile field the user may edit."""
    monkeypatch.setenv("AUTH_PROVIDER", "ayon")
    monkeypatch.setenv("DNA_CONFIG_ADMINS", f"{ADMIN}")
    configsys.reset_config()
    as_tracker_user("mallory", ADMIN)
    assert api.get("/config/effective").status_code == 403


def test_google_users_are_still_matched_by_their_verified_email(api):
    as_user(ADMIN)
    assert api.get("/config/effective").status_code == 200
