"""Live AYON write checks (opt-in, against a disposable project).

These create real comments, a list, an attachment and a status change on an AYON
server, then remove everything they created.  They run only when ALL of these are
set, so a normal test run never writes to anyone's tracker:

    AYON_URL, AYON_API_KEY      the server and a service-user key
    DNA_AYON_LIVE_WRITE=1       explicit consent to write
    DNA_AYON_TEST_PROJECT       a project whose first list has versions (e.g. dna_test)
    DNA_AYON_TEST_USER          a user to act as (e.g. the project's owner)

Only entities carrying a marker string are touched, and cleanup runs even when an
assertion fails.
"""

import os
from datetime import date

import pytest

from dna import configsys
from dna.prodtrack_providers.ayon import AyonProvider, split_id

_ENV = {
    k: os.environ.get(k)
    for k in (
        "AYON_URL",
        "AYON_API_KEY",
        "DNA_AYON_LIVE_WRITE",
        "DNA_AYON_TEST_PROJECT",
        "DNA_AYON_TEST_USER",
    )
}

pytestmark = pytest.mark.skipif(
    not (all(_ENV.values()) and _ENV["DNA_AYON_LIVE_WRITE"] == "1"),
    reason="live AYON writes are opt-in (see the module docstring)",
)

MARK = "DNA live test - safe to delete"


@pytest.fixture
def live(monkeypatch):
    configsys.reset_config()
    provider = AyonProvider(
        url=_ENV["AYON_URL"],
        api_key=_ENV["AYON_API_KEY"],
        sudo_user=_ENV["DNA_AYON_TEST_USER"],
    )
    created: list[tuple[str, str, str]] = []  # (kind, project, id)
    yield provider, created
    for kind, project, entity_id in reversed(created):
        path = f"/api/projects/{project}/{'lists' if kind == 'list' else 'activities'}/{entity_id}"
        try:
            provider._c.rest("DELETE", path)
        except Exception:  # best effort; the marker makes leftovers easy to find
            pass


def _target(provider):
    project = _ENV["DNA_AYON_TEST_PROJECT"]
    playlists = provider.get_playlists_for_project(project)
    for playlist in playlists:
        versions = provider.get_versions_for_playlist(playlist.id)
        if versions:
            return project, playlists[0], versions[0]
    pytest.skip(f"project {project} has no playlist with versions")


def test_note_lifecycle_with_attribution_attachment_and_status(live, tmp_path):
    provider, created = live
    _, _, version = _target(provider)
    original_status = version.status

    email = provider.get_entity("user", _ENV["DNA_AYON_TEST_USER"]).email
    note_id = provider.publish_note(
        version.id,
        MARK,
        "DNA live test",
        [_ENV["DNA_AYON_TEST_USER"]],
        [],
        [],
        author_email=email,
    )
    created.append(("activity", *split_id(note_id)))
    assert (
        provider.publish_note(
            version.id,
            MARK,
            "DNA live test",
            [_ENV["DNA_AYON_TEST_USER"]],
            [],
            [],
            author_email=email,
        )
        == note_id
    )  # idempotent

    notes = provider.get_entity("version", version.id).notes
    mine = next(n for n in notes if n.id == note_id)
    assert (
        mine.subject == "DNA live test"
        and mine.author.login == _ENV["DNA_AYON_TEST_USER"]
    )

    attachment = tmp_path / "a.txt"
    attachment.write_text("live")
    assert provider.attach_file_to_note(note_id, str(attachment), "live.txt") is True

    try:
        assert provider.update_version_status(version.id, "Approved") is True
        assert provider.get_entity("version", version.id).status == "Approved"
    finally:
        provider.update_version_status(version.id, original_status)
    assert provider.get_entity("version", version.id).status == original_status


def test_playlist_lifecycle(live):
    provider, created = live
    project, _, version = _target(provider)
    playlist = provider.create_playlist(project, f"{MARK} list")
    created.append(("list", *split_id(playlist.id)))
    assert provider.add_version_to_playlist(playlist.id, version.id) is True
    assert (
        provider.add_version_to_playlist(playlist.id, version.id) is True
    )  # idempotent
    assert [v.id for v in provider.get_versions_for_playlist(playlist.id)] == [
        version.id
    ]


def test_transcript_publish_and_update(live):
    provider, created = live
    project, playlist, version = _target(provider)
    entity_id = provider.publish_transcript(
        project_id=project,
        playlist_id=playlist.id,
        version_id=version.id,
        meeting_id="live-1",
        meeting_date=date(2026, 10, 6),
        platform="meet",
        body=MARK,
    )
    created.append(("activity", *split_id(entity_id)))
    assert (
        provider.update_transcript(
            entity_type=provider.transcript_entity_type(project),
            entity_id=entity_id,
            body=f"{MARK} (edited)",
            meeting_date=date(2026, 10, 6),
        )
        is True
    )
