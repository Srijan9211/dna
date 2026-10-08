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
    ("get", "/config/projects"),
    ("put", "/config/projects/p1"),
    ("delete", "/config/projects/p1"),
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


# ── per-project overrides ────────────────────────────────────────────────────

PUBLISH = {"values": {"features": {"transcript_publish": True}}}


def test_the_projects_endpoint_lists_what_a_project_may_override(api):
    body = api.get("/config/projects").json()
    paths = {(x["section"], x["path"]) for x in body["settings"]}
    assert ("features", "transcript_publish") in paths
    assert ("prodtrack", "shotgrid.transcript_entity") in paths
    # nothing a project must not touch
    assert not any(
        x["section"] in ("auth", "server", "storage") for x in body["settings"]
    )
    flag = next(x for x in body["settings"] if x["path"] == "transcript_publish")
    assert flag["type"] == "boolean" and flag["site_value"] is False
    assert body["overrides"] == []


def test_setting_a_project_override_takes_effect_for_that_project_only(api):
    response = api.put("/config/projects/proj_a", json=PUBLISH)
    assert response.status_code == 200
    assert response.json()["changed"] == ["projects.proj_a.features.transcript_publish"]

    overrides = api.get("/config/projects").json()["overrides"]
    assert overrides == [
        {
            "project": "proj_a",
            "section": "features",
            "path": "transcript_publish",
            "value": True,
            "source": "runtime",
        }
    ]
    assert api.get("/config/public?project_id=proj_a").json()["features"] == {
        "transcript_publish": True
    }
    assert api.get("/config/public?project_id=proj_b").json()["features"] == {
        "transcript_publish": False
    }
    assert api.get("/config/public").json()["features"] == {"transcript_publish": False}


def test_a_project_change_is_recorded_in_the_history(api):
    api.put("/config/projects/proj_a", json={**PUBLISH, "note": "pilot show"})
    latest = api.get("/config/history").json()[0]
    assert latest["note"] == "pilot show"
    assert latest["changed"] == ["projects.proj_a.features.transcript_publish"]
    assert latest["actor"] == ADMIN


def test_a_setting_that_is_not_project_scoped_is_refused(api):
    response = api.put(
        "/config/projects/proj_a",
        json={"values": {"features": {"testing_enabled": True}}},
    )
    assert response.status_code == 422
    assert "cannot be overridden per project" in response.text


def test_locked_and_unknown_sections_and_settings_are_refused(api):
    for values in (
        {"auth": {"provider": "none"}},
        {"nonsense": {"x": 1}},
        {"features": {"no_such_setting": 1}},
    ):
        assert api.put("/config/projects/p", json={"values": values}).status_code == 422
    assert api.get("/config/projects").json()["overrides"] == []  # nothing was saved


def test_a_project_value_of_the_wrong_type_is_refused(api):
    response = api.put(
        "/config/projects/p",
        json={"values": {"features": {"transcript_publish": "maybe"}}},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("bad", ["a b", "a/b", "x" * 200, "a;b", "$(x)"])
def test_a_project_id_must_look_like_an_id(api, bad):
    assert api.put(f"/config/projects/{bad}", json=PUBLISH).status_code in (404, 422)
    assert api.get(f"/config/public?project_id={bad}").status_code == 422


def test_removing_an_override_restores_the_site_value(api):
    api.put("/config/projects/proj_a", json=PUBLISH)
    removed = api.delete(
        "/config/projects/proj_a?section=features&path=transcript_publish"
    )
    assert removed.status_code == 200
    assert removed.json()["changed"] == ["projects.proj_a.features.transcript_publish"]
    assert api.get("/config/projects").json()["overrides"] == []
    assert api.get("/config/public?project_id=proj_a").json()["features"] == {
        "transcript_publish": False
    }
    # and nothing is left behind in the stored overrides
    assert "projects" not in api.get("/config/export").json()["overrides"]


def test_removing_a_whole_project_removes_all_its_overrides(api):
    api.put(
        "/config/projects/proj_a",
        json={
            "values": {
                "features": {"transcript_publish": True},
                "prodtrack": {"shotgrid": {"transcript_entity": "CustomEntity05"}},
            }
        },
    )
    assert api.delete("/config/projects/proj_a").status_code == 200
    assert api.get("/config/projects").json()["overrides"] == []


def test_removing_something_that_is_not_there_is_404(api):
    assert api.delete("/config/projects/nobody").status_code == 404
    api.put("/config/projects/proj_a", json=PUBLISH)
    assert (
        api.delete(
            "/config/projects/proj_a?section=features&path=testing_enabled"
        ).status_code
        == 404
    )
    assert (
        api.delete("/config/projects/proj_a?path=transcript_publish").status_code == 422
    )


def test_an_override_in_a_config_file_cannot_be_removed_through_the_api(
    api, tmp_path, monkeypatch
):
    (tmp_path / "site.yaml").write_text(
        "projects:\n  proj_f:\n    features:\n      transcript_publish: true\n"
    )
    monkeypatch.setenv("DNA_CONFIG_DIR", str(tmp_path))
    configsys.reset_config()
    overrides = api.get("/config/projects").json()["overrides"]
    assert len(overrides) == 1 and overrides[0]["source"].startswith("file")
    response = api.delete("/config/projects/proj_f")
    assert response.status_code == 409 and "configuration file" in response.text


def test_a_stale_project_change_is_a_conflict(api):
    api.put("/config/projects/p1", json=PUBLISH)
    stale = api.put("/config/projects/p2", json={**PUBLISH, "expected_version": 0})
    assert stale.status_code == 409


def test_a_rollback_undoes_a_project_override(api):
    api.put("/config/projects/proj_a", json=PUBLISH)  # revision 1
    api.put(
        "/config/features", json={"values": {"testing_enabled": False}}
    )  # revision 2
    api.post("/config/rollback/1")
    assert api.get("/config/public?project_id=proj_a").json()["features"] == {
        "transcript_publish": True
    }


# ── the settings that follow a project's override ────────────────────────────


def test_transcript_publishing_follows_the_project_override(api):
    import main

    assert main._transcript_publish_possible() is False
    api.put("/config/projects/proj_a", json=PUBLISH)
    assert main._transcript_publish_possible() is True
    assert main._transcript_publish_enabled("proj_a") is True
    assert main._transcript_publish_enabled("proj_b") is False
    assert main._transcript_publish_enabled() is False  # the site-wide value


def test_a_project_can_switch_publishing_off_when_it_is_on_site_wide(api):
    import main

    api.put("/config/features", json={"values": {"transcript_publish": True}})
    api.put(
        "/config/projects/quiet",
        json={"values": {"features": {"transcript_publish": False}}},
    )
    assert main._transcript_publish_enabled("quiet") is False
    assert main._transcript_publish_enabled("loud") is True


def test_only_settings_that_apply_to_the_active_tracker_are_offered(api, monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "ayon")
    configsys.reset_config()
    paths = {
        (x["section"], x["path"])
        for x in api.get("/config/projects").json()["settings"]
    }
    assert ("features", "transcript_publish") in paths
    assert ("prodtrack", "shotgrid.transcript_entity") not in paths  # ShotGrid-only
