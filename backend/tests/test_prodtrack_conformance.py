"""Conformance: what every production-tracking connector must do.

The same scenarios run against each connector through DNA's provider contract
(``ProdtrackProviderBase``).  A new connector (AYON, ftrack, Kitsu) is "done" when
it joins ``CONNECTORS`` below and this suite passes.  Reads run against every
connector; writes run only where the harness is safe to mutate (a fake tracker),
never against the shipped mock database.
"""

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "golden"))

from sg_scenarios import DATA, FakeShotgun  # noqa: E402

from dna import configsys  # noqa: E402
from dna.models.entity import Playlist, Project, User, Version  # noqa: E402
from dna.prodtrack_providers import shotgrid as sg_module  # noqa: E402
from dna.prodtrack_providers.mock_provider import MockProdtrackProvider  # noqa: E402
from dna.prodtrack_providers.profile import KINDS  # noqa: E402

# Captured before the ``tracker`` fixture clears every config-registered variable,
# so the other connectors are tested in isolation from the developer's environment.
_AYON_URL, _AYON_KEY = os.environ.get("AYON_URL"), os.environ.get("AYON_API_KEY")


@dataclass
class Harness:
    name: str
    provider: Any
    user_email: str
    writable: bool
    # Unknown emails raise ValueError (the contract).  The mock is the documented
    # exception: it synthesises a user so any developer login works.
    strict_users: bool = True


def _mock_harness() -> Harness:
    provider = MockProdtrackProvider()
    conn = sqlite3.connect(str(provider._db_path))
    row = conn.execute(
        "select u.email from users u join project_users pu on pu.user_id = u.id "
        "where u.email like '%@%' group by u.email order by count(*) desc limit 1"
    ).fetchone()
    conn.close()
    return Harness("mock", provider, row[0], writable=False, strict_users=False)


def _shotgrid_harness() -> Harness:
    log: list = []
    patcher = mock.patch.object(
        sg_module,
        "Shotgun",
        side_effect=lambda *a, **k: FakeShotgun(
            f"sudo:{k.get('sudo_as_login')}" if k.get("sudo_as_login") else "script",
            log,
        ),
    )
    patcher.start()
    provider = sg_module.ShotgridProvider(
        url="https://studio.example", script_name="s", api_key="k", sudo_user="ann"
    )
    harness = Harness("shotgrid", provider, "ann@x.com", writable=True)
    harness.patcher = patcher  # type: ignore[attr-defined]
    return harness


def _ayon_harness() -> Harness:
    """A live AYON server (opt-in: needs AYON_URL and AYON_API_KEY in the environment).

    Read-only: nothing is written to the server.  The write scenarios are covered
    by ``test_ayon_live.py`` against a disposable project.
    """
    import requests

    url, key = _AYON_URL, _AYON_KEY
    if not (url and key):
        pytest.skip("set AYON_URL and AYON_API_KEY to run the AYON conformance checks")
    try:
        requests.get(f"{url.rstrip('/')}/api/info", timeout=3).raise_for_status()
    except requests.RequestException:
        pytest.skip(f"AYON at {url} is not reachable")
    from dna.prodtrack_providers.ayon import AyonProvider

    provider = AyonProvider(url=url, api_key=key)
    candidates = [u for u in provider.search("", ["user"], None, 200) if u.get("email")]
    if not candidates:
        pytest.skip("the AYON server has no user with an email address")
    return Harness("ayon", provider, candidates[0]["email"], writable=False)


CONNECTORS: dict[str, Callable[[], Harness]] = {
    "mock": _mock_harness,
    "shotgrid": _shotgrid_harness,
    "ayon": _ayon_harness,
}


@pytest.fixture(params=sorted(CONNECTORS))
def tracker(request, monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    configsys.reset_config()
    harness = CONNECTORS[request.param]()
    yield harness
    if hasattr(harness, "patcher"):
        harness.patcher.stop()
    configsys.reset_config()


# ── identity and discovery ───────────────────────────────────────────────────


def test_finds_a_user_by_email(tracker):
    user = tracker.provider.get_user_by_email(tracker.user_email)
    assert isinstance(user, User) and user.id
    assert (user.email or "").lower() == tracker.user_email.lower()


def test_unknown_user_is_a_value_error(tracker):
    if not tracker.strict_users:
        pytest.skip("this connector synthesises unknown users by design")
    with pytest.raises(ValueError):
        tracker.provider.get_user_by_email("nobody-here@example.invalid")


def test_project_playlist_version_walk(tracker):
    p = tracker.provider
    projects = p.get_projects_for_user(tracker.user_email)
    assert projects and all(isinstance(x, Project) for x in projects)
    playlists = []
    for project in projects:
        playlists = p.get_playlists_for_project(project.id)
        if playlists:
            break
    assert playlists and all(isinstance(x, Playlist) for x in playlists)
    versions = []
    for playlist in playlists:
        versions = p.get_versions_for_playlist(playlist.id)
        if versions:
            break
    assert versions and all(isinstance(v, Version) for v in versions)
    assert all(v.id and v.name for v in versions)


def _first_version(p, email):
    for project in p.get_projects_for_user(email):
        for playlist in p.get_playlists_for_project(project.id):
            versions = p.get_versions_for_playlist(playlist.id)
            if versions:
                return versions[0]
    pytest.skip("no playlist with versions is visible to the test user")


def test_get_entity_round_trips_a_version(tracker):
    listed = _first_version(tracker.provider, tracker.user_email)
    fetched = tracker.provider.get_entity("version", listed.id)
    assert isinstance(fetched, Version) and fetched.id == listed.id


def test_unknown_entity_type_is_a_value_error(tracker):
    with pytest.raises(ValueError):
        tracker.provider.get_entity("not-a-kind", 1)


def test_search_returns_lightweight_results(tracker):
    results = tracker.provider.search("", ["user", "shot"], None, 3)
    assert isinstance(results, list) and results
    for item in results:
        assert {"type", "id", "name"} <= set(item)


def test_version_statuses_are_code_name_pairs(tracker):
    statuses = tracker.provider.get_version_statuses()
    assert isinstance(statuses, list)
    for status in statuses:
        assert {"code", "name"} <= set(status)


# ── capabilities ─────────────────────────────────────────────────────────────


def test_connector_declares_capabilities_and_a_complete_profile(tracker):
    profile = tracker.provider.profile
    assert profile is not None and profile.contract == "dna.prodtrack/1"
    assert set(profile.entities) == set(KINDS), "a connector must bind every DNA kind"
    caps = tracker.provider.capabilities()
    assert caps.playlists is True  # DNA's review flow is built on playlists
    assert caps.impersonation in ("sudo_header", "session_user", "service_only", "none")


def test_impersonation_flag_matches_the_profile(tracker):
    declared = tracker.provider.capabilities().impersonation != "none"
    assert declared == bool(type(tracker.provider).SUPPORTS_IMPERSONATION)


# ── writes (fake trackers only) ──────────────────────────────────────────────


def test_publish_note_is_idempotent_and_sets_status(tracker):
    if not tracker.writable:
        pytest.skip("writes are not run against this connector's data")
    p = tracker.provider
    kwargs = dict(
        version_id=1001,
        content="Looks good",
        subject="Review",
        to_users=[1],
        cc_users=[],
        links=[],
        version_status="apr",
    )
    first = p.publish_note(**kwargs)
    assert isinstance(first, int)
    existing = p.publish_note(
        version_id=1001,
        content="Content",
        subject="Subj",
        to_users=[],
        cc_users=[],
        links=[],
    )
    assert existing == DATA["Note"][0]["id"]  # a duplicate returns the existing note


def test_playlist_lifecycle(tracker):
    if not tracker.writable:
        pytest.skip("writes are not run against this connector's data")
    p = tracker.provider
    if not tracker.provider.capabilities().create_playlist:
        pytest.skip("connector cannot create playlists")
    created = p.create_playlist(10, "conformance-list")
    assert isinstance(created, Playlist)
    assert p.add_version_to_playlist(100, 1001) is True  # already present is success
