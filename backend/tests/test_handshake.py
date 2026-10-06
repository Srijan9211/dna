"""The handshake: schema validation, suggestions, the runner, and provider hooks."""

from unittest import mock

import pytest

from dna import configsys
from dna.prodtrack_providers import shotgrid as sg_module
from dna.prodtrack_providers.handshake import (
    Check,
    TrackerSchema,
    check_candidate,
    run_handshake,
    validate_profile,
)
from dna.prodtrack_providers.profile import load_profile
from dna.prodtrack_providers.prodtrack_provider_base import ProdtrackAuthError, ProdtrackProviderBase
from dna.prodtrack_providers.shotgrid import ShotgridProvider


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    configsys.reset_config()
    yield
    configsys.reset_config()


def schema_for(profile, *, drop=()):
    """A schema in which the profile's every mapped field exists."""
    entities = {}
    for binding in profile.entities.values():
        entities.setdefault(binding.source, set()).update(binding.fields, binding.links)
    for source, field in drop:
        entities[source].discard(field)
    return TrackerSchema(
        version="Test 1",
        entities=entities,
        known_entities=set(entities),
        folder_types={"Shot", "Asset", "Sequence"},
    )


# ── validate_profile ─────────────────────────────────────────────────────────


def test_a_matching_schema_passes_every_mapping():
    profile = load_profile("shotgrid")
    checks, suggestions = validate_profile(profile, schema_for(profile))
    assert {c.status for c in checks} == {"ok"} and suggestions == []
    assert {c.name for c in checks} == {f"mapping.{k}" for k in profile.entities}


def test_a_missing_field_fails_and_suggests_the_closest_real_one():
    profile = load_profile("shotgrid")
    schema = schema_for(profile, drop=[("Version", "sg_status_list")])
    schema.entities["Version"].add("sg_status")  # what this site actually calls it
    checks, suggestions = validate_profile(profile, schema)
    failed = [c for c in checks if c.status == "fail"]
    assert [c.name for c in failed] == ["mapping.version"]
    assert "sg_status_list" in failed[0].message
    (s,) = [s for s in suggestions if "sg_status_list" in s.problem]
    assert s.options[0] == "sg_status" and "status" in s.problem
    # the suggestion is a ready-to-apply override that renames the field
    assert s.override == {"entities": {"version": {"fields": {"sg_status_list": None, "sg_status": "status"}}}}


def test_the_suggested_override_actually_fixes_the_profile():
    profile = load_profile("shotgrid")
    schema = schema_for(profile, drop=[("Version", "sg_status_list")])
    schema.entities["Version"].add("sg_status")
    (suggestion,) = [s for s in validate_profile(profile, schema)[1] if s.override]
    fixed = load_profile("shotgrid", overrides=suggestion.override)
    checks, _ = validate_profile(fixed, schema)
    assert all(c.status == "ok" for c in checks if c.name == "mapping.version")


def test_an_entity_the_tracker_lacks_fails_with_a_suggestion():
    profile = load_profile("shotgrid")
    schema = schema_for(profile)
    schema.known_entities.discard("Playlist")
    schema.known_entities.add("Playlists")
    checks, suggestions = validate_profile(profile, schema)
    assert any(c.status == "fail" and "no entity 'Playlist'" in c.message for c in checks)
    assert any(s.options == ["Playlists"] for s in suggestions)


def test_folder_types_are_checked_and_corrected():
    profile = load_profile("ayon", overrides={"entities": {"shot": {"where": {"folderType": ["Shotz", "Sequence"]}}}})
    schema = schema_for(profile)
    checks, suggestions = validate_profile(profile, schema)
    assert any(c.name == "mapping.shot" and c.status == "fail" and "Shotz" in c.message for c in checks)
    (s,) = [s for s in suggestions if "folder type" in s.problem]
    assert s.options == ["Shot"]
    assert s.override == {"entities": {"shot": {"where": {"folderType": ["Shot", "Sequence"]}}}}


def test_an_entity_the_connector_could_not_introspect_is_skipped_not_blamed():
    profile = load_profile("shotgrid")
    schema = TrackerSchema(known_entities={b.source for b in profile.entities.values()})  # no field info
    checks, _ = validate_profile(profile, schema)
    assert {c.status for c in checks} == {"skip"}


# ── the runner ───────────────────────────────────────────────────────────────


class StubProvider(ProdtrackProviderBase):
    def __init__(self, *, connect=None, schema=None, adapter=(), smoke=True):
        self._connect, self._schema, self._adapter, self._smoke = connect, schema, adapter, smoke

    def check_connection(self):
        if isinstance(self._connect, Exception):
            raise self._connect
        if self._connect is None:
            raise NotImplementedError
        return self._connect

    def discover_schema(self):
        if isinstance(self._schema, Exception):
            raise self._schema
        if self._schema is None:
            raise NotImplementedError
        return self._schema

    def adapter_issues(self, schema):
        return list(self._adapter)

    def get_user_by_email(self, email):
        if not self._smoke:
            raise ValueError("User not found")

    def get_projects_for_user(self, email):
        return [mock.Mock(id="p")]

    def get_playlists_for_project(self, project_id):
        return [mock.Mock(id="pl")]

    def get_versions_for_playlist(self, playlist_id):
        return [object(), object()]


PROFILE = load_profile("shotgrid")


def run(provider=None, **kw):
    return run_handshake("shotgrid", PROFILE, lambda p: provider, **kw)


def statuses(report):
    return {c.name: c.status for c in report.checks}


def test_a_healthy_connector_reports_ok_and_what_it_supports():
    report = run(StubProvider(connect={"server_version": "9.9"}, schema=schema_for(PROFILE)), smoke_email="a@x.com")
    assert report.ok and report.contract == "dna.prodtrack/1"
    assert report.capabilities["impersonation"] == "sudo_header"
    s = statuses(report)
    assert s["describe"] == s["connect"] == s["discover"] == s["smoke"] == "ok"
    assert next(c for c in report.checks if c.name == "connect").details == {"server_version": "9.9"}
    assert "2 version(s)" in next(c for c in report.checks if c.name == "smoke").message
    assert report.schema["version"] == "Test 1"


def test_incomplete_connection_settings_fail_the_connect_step_without_raising():
    def broken(profile):
        raise ValueError("SHOTGRID_URL is required.")

    report = run_handshake("shotgrid", PROFILE, broken)
    assert not report.ok and statuses(report) == {"describe": "ok", "connect": "fail"}
    assert "SHOTGRID_URL is required" in report.checks[-1].message


def test_a_connection_failure_stops_the_run_and_is_reported():
    report = run(StubProvider(connect=ProdtrackAuthError("AYON rejected the credentials")))
    assert not report.ok and "discover" not in statuses(report)
    assert "rejected the credentials" in next(c for c in report.checks if c.name == "connect").message


def test_a_connector_that_cannot_introspect_is_skipped_not_failed():
    report = run(StubProvider())
    assert report.ok and statuses(report)["connect"] == statuses(report)["discover"] == "skip"


def test_a_schema_failure_is_reported_but_does_not_hide_the_connection_result():
    report = run(StubProvider(connect={}, schema=RuntimeError("boom")))
    assert not report.ok and statuses(report)["connect"] == "ok" and statuses(report)["discover"] == "fail"


def test_adapter_issues_and_schema_notes_join_the_report():
    schema = schema_for(PROFILE)
    schema.notes = ["no projects visible"]
    report = run(StubProvider(connect={}, schema=schema, adapter=[Check("adapter.x", "fail", "bad name")]))
    assert not report.ok
    assert statuses(report)["adapter.x"] == "fail" and statuses(report)["note"] == "warn"


def test_warnings_do_not_block_but_failures_do():
    assert run(StubProvider(connect={}, schema=schema_for(PROFILE), adapter=[Check("a", "warn", "careful")])).ok
    assert not run(StubProvider(connect={}, schema=schema_for(PROFILE), adapter=[Check("a", "fail", "no")])).ok


def test_smoke_failure_and_empty_results():
    report = run(StubProvider(connect={}, smoke=False), smoke_email="ghost@x.com")
    assert not report.ok and "User not found" in next(c for c in report.checks if c.name == "smoke").message

    class Empty(StubProvider):
        def get_playlists_for_project(self, project_id):
            return []

    report = run(Empty(connect={}), smoke_email="a@x.com")
    assert report.ok and statuses(report)["smoke"] == "warn"  # reachable, just nothing to review yet


def test_error_messages_are_one_bounded_line():
    report = run(StubProvider(connect=RuntimeError("line one\n" + "x" * 1000)))
    message = next(c for c in report.checks if c.name == "connect").message
    assert "\n" not in message and len(message) < 400


def test_the_report_serialises_for_the_api():
    data = run(StubProvider(connect={"a": 1}, schema=schema_for(PROFILE))).to_dict()
    assert data["ok"] is True and data["connector"] == "shotgrid"
    assert {"name", "status", "message", "details"} <= set(data["checks"][0])


# ── ShotGrid hooks ───────────────────────────────────────────────────────────


class FakeSg:
    """schema_entity_read / schema_field_read / find_one / info over a dict."""

    def __init__(self, schema):
        self.schema = schema
        self.queries = []

    def schema_entity_read(self):
        return {name: {} for name in self.schema}

    def schema_field_read(self, entity, field=None, project=None):
        return {f: {} for f in self.schema[entity]}

    def find_one(self, *a, **k):
        self.queries.append(("find_one", a))
        return {"id": 1}

    def info(self):
        return {"version": [8, 4, 1]}


def sg_schema(profile=PROFILE, **removed):
    schema = {b.source: set(b.fields) | set(b.links) for b in profile.entities.values()}
    schema["Version"] |= {"sg_status_list"}
    schema["Note"] |= {"addressings_to", "addressings_cc", "attachments"}
    schema["CustomEntity01"] = {"code", "project", "sg_playlist", "sg_version_in_review", "sg_meeting_id",
                                "sg_meeting_date", "sg_platform", "sg_transcript_body"}
    for key, field in removed.items():
        schema[key.replace("__", "")].discard(field)
    return schema


def sg_provider(schema, profile=None):
    with mock.patch.object(sg_module, "Shotgun", return_value=FakeSg(schema)):
        return ShotgridProvider(url="https://s.example", script_name="a", api_key="k", profile=profile)


def test_shotgrid_connection_check_runs_a_real_query():
    provider = sg_provider(sg_schema())
    info = provider.check_connection()
    assert info == {"site": "https://s.example", "server_version": "8.4.1"}
    assert provider.sg.queries[0][0] == "find_one"  # info() alone would not prove the key


def test_shotgrid_discovery_reads_the_schema_of_the_entities_it_uses():
    schema = sg_provider(sg_schema()).discover_schema()
    assert "Version" in schema.entities and "sg_status_list" in schema.entities["Version"]
    assert "CustomEntity01" in schema.entities  # the transcript slot is included
    assert {"Shot", "Playlist"} <= schema.known_entities


def test_shotgrid_end_to_end_handshake_with_a_renamed_status_field():
    """The studio's ShotGrid calls it sg_review_status; the default profile is wrong
    and the handshake says exactly how to fix it."""
    schema = sg_schema()
    schema["Version"].discard("sg_status_list")
    schema["Version"].add("sg_review_status")
    schema["Task"].add("sg_review_status")
    provider = sg_provider(schema)
    report = run_handshake("shotgrid", PROFILE, lambda p: provider)
    assert not report.ok
    failing = {c.name for c in report.checks if c.status == "fail"}
    assert {"mapping.version", "adapter.status_field"} <= failing
    fix = next(s for s in report.suggestions if "sg_status_list" in s.problem and "Version" in s.problem)
    assert fix.options[0] == "sg_review_status"


def test_shotgrid_adapter_checks_recipient_and_transcript_fields():
    schema = sg_schema()
    schema["Note"].discard("addressings_cc")
    del schema["CustomEntity01"]
    provider = sg_provider(schema)
    found = {c.name: c for c in provider.adapter_issues(provider.discover_schema())}
    assert found["adapter.note"].status == "fail" and "addressings_cc" in found["adapter.note"].message
    assert found["adapter.transcript"].status == "warn"  # transcripts off is not fatal

    partial = sg_schema()
    partial["CustomEntity01"].discard("sg_platform")
    provider = sg_provider(partial)
    found = {c.name: c for c in provider.adapter_issues(provider.discover_schema())}
    assert found["adapter.transcript"].status == "warn" and "sg_platform" in found["adapter.transcript"].message


# ── AYON hooks ───────────────────────────────────────────────────────────────


class FakeAyonClient:
    url = "http://ayon.test"

    def __init__(self, *, as_user=None, honours_header=True, projects=("demo",), others=("bob",)):
        self.as_user, self.honours_header, self.projects, self.others = as_user, honours_header, projects, others

    def acting_as(self, login):
        return FakeAyonClient(as_user=login, honours_header=self.honours_header, projects=self.projects, others=self.others)

    def rest(self, method, path, **kw):
        if path == "/api/info":
            return {"releaseInfo": {"version": "1.16.7"}}
        if path == "/api/users/me":
            return {"name": self.as_user if (self.as_user and self.honours_header) else "svc"}
        if path == "/api/attributes":
            return {"attributes": [{"name": "description", "scope": ["folder", "version"]},
                                   {"name": "fullName", "scope": ["user"]}, {"name": "email", "scope": ["user"]}]}
        if path.startswith("/api/projects/"):
            return {"folderTypes": [{"name": "Shot"}, {"name": "Asset"}], "taskTypes": [{"name": "Lighting"}],
                    "statuses": [{"name": "Approved", "scope": ["version"]}]}
        raise AssertionError(path)

    def graphql(self, query, variables=None):
        if "projects" in query:
            return {"projects": {"edges": [{"node": {"name": p, "active": True}} for p in self.projects]}}
        return {"users": {"edges": [{"node": {"name": n, "active": True}} for n in ("svc", *self.others)]}}


def ayon_provider(**kw):
    from dna.prodtrack_providers.ayon import AyonProvider

    return AyonProvider(client=FakeAyonClient(**kw))


def test_ayon_connection_proves_impersonation():
    facts = ayon_provider().check_connection()
    assert facts == {"server_version": "1.16.7", "service_user": "svc", "impersonation": "verified: acted as 'bob'"}


def test_ayon_connection_fails_when_the_key_cannot_impersonate():
    with pytest.raises(ProdtrackAuthError, match="did not act as 'bob'.*answered as 'svc'"):
        ayon_provider(honours_header=False).check_connection()


def test_ayon_connection_notes_when_there_is_nobody_to_impersonate():
    assert "not tested" in ayon_provider(others=()).check_connection()["impersonation"]


def test_ayon_discovery_merges_static_fields_with_custom_attributes():
    schema = ayon_provider().discover_schema()
    assert schema.version == "AYON 1.16.7"
    assert {"attrib.description", "name", "folderType"} <= schema.entities["folder"]
    assert {"attrib.fullName", "attrib.email"} <= schema.entities["user"]
    assert schema.folder_types == {"Shot", "Asset"} and schema.statuses == ["Approved"]


def test_the_shipped_ayon_profile_validates_against_a_real_shaped_schema():
    provider = ayon_provider()
    report = run_handshake("ayon", provider.profile, lambda p: provider)
    assert report.ok, [c for c in report.checks if c.status == "fail"]


def test_ayon_handshake_catches_a_typo_in_a_custom_attribute():
    profile = load_profile("ayon", overrides={"entities": {"shot": {"fields": {"attrib.descripton": "description"}}}})
    provider = ayon_provider()
    report = run_handshake("ayon", profile, lambda p: provider)
    assert not report.ok
    assert any(s.options and s.options[0] == "attrib.description" for s in report.suggestions)


def test_ayon_warns_when_the_service_user_sees_no_project():
    schema = ayon_provider(projects=()).discover_schema()
    assert schema.folder_types == set() and "no projects" in schema.notes[0].lower()


# ── check_candidate (shared by the API and the CLI) ──────────────────────────


def test_check_candidate_uses_the_candidate_without_applying_it(monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "mock")
    report = check_candidate({"prodtrack": {"profile": {"labels": {"shot": "Plate"}}}})
    assert report.connector == "mock" and report.ok
    assert configsys.get_config().get("prodtrack").profile == {}  # not applied


def test_check_candidate_enforces_the_same_rules_as_a_real_change():
    with pytest.raises(configsys.ConfigError, match="cannot be changed at runtime"):
        check_candidate({"auth": {"provider": "none"}})
    with pytest.raises(configsys.ConfigError, match="reference"):
        check_candidate({"prodtrack": {"ayon": {"api_key": "plaintext"}}})
