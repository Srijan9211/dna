"""The AYON connector against a scripted AYON server (no network).

Payload shapes mirror what AYON 1.16 returns (captured from a live server), so these
tests pin the conversion logic.  The live-server checks are in
``test_prodtrack_conformance.py`` and ``test_ayon_live.py``.
"""

import re
from datetime import date
from typing import Any, Optional
from unittest import mock

import pytest
import requests

from dna import configsys
from dna.models.entity import Asset, Playlist, Project, Shot, User, Version
from dna.prodtrack_providers import ayon as ayon_module
from dna.prodtrack_providers.ayon import (
    AyonAdapter,
    AyonProvider,
    build_selection,
    make_id,
    split_id,
)
from dna.prodtrack_providers.ayon_client import AyonClient, AyonNotFound
from dna.prodtrack_providers.prodtrack_provider_base import (
    ProdtrackAuthError,
    ProdtrackPermissionError,
    ProdtrackUnavailableError,
    UserNotFoundError,
)

P = "demo"
USERS = {
    "ann": {
        "name": "ann",
        "active": True,
        "attrib": {"fullName": "Ann Artist", "email": "ann@x.com"},
    },
    "bob": {
        "name": "bob",
        "active": True,
        "attrib": {"fullName": "Bob Sup", "email": "bob@x.com"},
    },
    "svc": {"name": "svc", "active": True, "attrib": {"fullName": None, "email": None}},
}


def _folder(fid, name, ftype, desc=None):
    return {
        "id": fid,
        "name": name,
        "label": None,
        "folderType": ftype,
        "path": f"/{name}",
        "attrib": {"description": desc},
    }


def _comment(aid, body, author="ann", at="2026-10-06T10:00:00+00:00"):
    return {"activityId": aid, "body": body, "createdAt": at, "author": USERS[author]}


def _version(vid, folder, product, task, *, status="Pending review", comments=()):
    return {
        "id": vid,
        "name": "v001",
        "version": 1,
        "status": status,
        "author": "ann",
        "createdAt": "2026-10-06T09:00:00+00:00",
        "updatedAt": "2026-10-06T09:30:00+00:00",
        "thumbnailId": "thumb-" + vid,
        "hasReviewables": True,
        "attrib": {"description": f"desc of {vid}"},
        "product": {
            "id": "p-" + vid,
            "name": product,
            "productType": "render",
            "folder": folder,
        },
        "task": task,
        "activities": {"edges": [{"node": c} for c in comments]},
    }


class ScriptedAyon:
    """Stands in for AyonClient; answers from canned data and records every call."""

    def __init__(self, *, as_user=None, log=None, state=None, url="http://ayon.test"):
        self.url = url
        self.as_user = as_user
        self.log: list[dict[str, Any]] = log if log is not None else []
        self.state = state if state is not None else self._default_state()
        self.queries: list[str] = []

    @staticmethod
    def _default_state() -> dict[str, Any]:
        sh = _folder("f1", "sh010", "Shot", "Opening")
        asset = _folder("f2", "hero", "Asset")
        task = {
            "id": "t1",
            "name": "lighting",
            "taskType": "Lighting",
            "status": "In progress",
        }
        return {
            "projects": {
                P: {"name": P, "label": "Demo", "active": True},
                "other": {"name": "other", "label": "Other", "active": True},
            },
            "access": {"ann": [P], "bob": [P, "other"]},
            "versions": {
                "v1": _version(
                    "v1",
                    sh,
                    "renderLighting",
                    task,
                    comments=[
                        _comment(
                            "c2",
                            "**Second**\n\nlater note",
                            "bob",
                            "2026-10-06T12:00:00+00:00",
                        ),
                        _comment(
                            "c1", "first plain note", "ann", "2026-10-06T11:00:00+00:00"
                        ),
                    ],
                ),
                "v2": _version("v2", asset, "modelMain", None),
            },
            "lists": {
                "L1": {
                    "id": "L1",
                    "label": "Dailies",
                    "entityType": "version",
                    "createdAt": "2026-10-01T00:00:00+00:00",
                    "updatedAt": "2026-10-02T00:00:00+00:00",
                    "items": [("v1", 1), ("v2", 0)],
                }
            },
            "statuses": [
                {"name": "Pending review", "scope": ["version", "task"]},
                {"name": "Approved", "scope": ["version"]},
                {"name": "In progress", "scope": ["task"]},
            ],
            "reviewables": {"v1": [{"fileId": "file-v1", "label": "main"}], "v2": []},
            "next": 0,
        }

    def acting_as(self, login):
        child = ScriptedAyon(
            as_user=login, log=self.log, state=self.state, url=self.url
        )
        child.queries = self.queries
        return child

    # ── GraphQL ───────────────────────────────────────────────────────────

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        assert query.count("{") == query.count("}"), f"unbalanced braces: {query}"
        assert query.count("(") == query.count(")"), f"unbalanced parens: {query}"
        self.queries.append(query)
        self.log.append({"op": "graphql", "as": self.as_user, "vars": variables or {}})
        v, s = variables or {}, self.state
        edges = lambda nodes: {"edges": [{"node": n} for n in nodes]}  # noqa: E731

        if "users(names:" in query:
            return {"users": edges([USERS[n] for n in v["names"] if n in USERS])}
        if "users(emails:" in query:
            return {
                "users": edges(
                    [u for u in USERS.values() if u["attrib"]["email"] in v["e"]]
                )
            }
        if "users(first:" in query:
            return {"users": edges(list(USERS.values()))}
        if re.search(r"query \{ projects", query):
            allowed = s["access"].get(self.as_user) if self.as_user else None
            names = allowed if allowed is not None else list(s["projects"])
            return {"projects": edges([s["projects"][n] for n in names])}
        if "project(name: $p) { name label active }" in query:
            node = s["projects"].get(v["p"])
            return {"project": node}
        if "entityLists(first:" in query:
            return {
                "project": {
                    "entityLists": {
                        **edges(
                            [
                                {
                                    k: l[k]
                                    for k in (
                                        "id",
                                        "label",
                                        "entityType",
                                        "createdAt",
                                        "updatedAt",
                                    )
                                }
                                for l in s["lists"].values()
                            ]
                        ),
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }
        if "entityLists(ids:" in query:
            found = [s["lists"][i] for i in v["ids"] if i in s["lists"]]
            return {
                "project": {
                    "entityLists": edges(
                        [
                            {
                                "id": l["id"],
                                "items": {
                                    "edges": [
                                        {"entityId": e, "position": p}
                                        for e, p in l["items"]
                                    ]
                                },
                            }
                            for l in found
                        ]
                    )
                }
            }
        if "versions(ids:" in query and "product" in query:
            return {
                "project": {
                    "versions": {
                        **edges(
                            [s["versions"][i] for i in v["ids"] if i in s["versions"]]
                        ),
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }
        if "versions(ids:" in query:  # the duplicate-comment check
            node = s["versions"].get(v["ids"][0], {})
            return {
                "project": {
                    "versions": edges(
                        [{"activities": node.get("activities", {"edges": []})}]
                    )
                }
            }
        if "versions(search:" in query:
            return {"project": {"versions": edges(list(s["versions"].values()))}}
        if "folders(search:" in query:
            folders = [x["product"]["folder"] for x in s["versions"].values()]
            return {"project": {"folders": edges(folders)}}
        if "folders(ids:" in query:
            hit = [
                x["product"]["folder"]
                for x in s["versions"].values()
                if x["product"]["folder"]["id"] in v["ids"]
            ]
            return {"project": {"folders": edges(hit)}}
        if "tasks(first:" in query:
            ver = s["versions"]["v1"]
            return {
                "project": {
                    "tasks": edges(
                        [{**ver["task"], "folder": ver["product"]["folder"]}]
                    )
                }
            }
        if "tasks(ids:" in query:
            ver = s["versions"]["v1"]
            return {
                "project": {
                    "tasks": edges(
                        [{**ver["task"], "folder": ver["product"]["folder"]}]
                    )
                }
            }
        raise AssertionError(f"unscripted query: {query}")

    def graphql_all(self, query, path, variables=None, **_):
        data = self.graphql(query, {**(variables or {}), "first": 200, "after": None})
        node: Any = data
        for key in path:
            node = node[key]
        return [e["node"] for e in node["edges"]]

    # ── REST ──────────────────────────────────────────────────────────────

    def rest(self, method: str, path: str, **kwargs: Any) -> Any:
        self.log.append(
            {"op": "rest", "as": self.as_user, "method": method, "path": path, **kwargs}
        )
        s = self.state
        if method == "GET" and (
            m := re.fullmatch(rf"/api/projects/{P}/versions/(\w+)/reviewables", path)
        ):
            return {"reviewables": s["reviewables"].get(m.group(1), [])}
        if method == "GET" and re.fullmatch(r"/api/projects/\w+", path):
            return {"statuses": s["statuses"]}
        s["next"] += 1
        return {"id": f"new{s['next']}"}


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    configsys.reset_config()
    ayon_module._verified.clear()
    yield
    configsys.reset_config()


def provider(sudo_user=None, **kw):
    fake = ScriptedAyon()
    return AyonProvider(client=fake, sudo_user=sudo_user, **kw), fake


# ── ids and helpers ──────────────────────────────────────────────────────────


def test_ids_round_trip_and_reject_foreign_ids():
    assert split_id(make_id("demo", "abc")) == ("demo", "abc")
    for bad in (123, "no-colon", ":x", "x:", None):
        with pytest.raises(ValueError, match="Invalid AYON id"):
            split_id(bad)


def test_selection_builder_nests_dotted_paths():
    assert (
        build_selection(["id", "attrib.a", "attrib.b", "name"])
        == "id attrib { a b } name"
    )


def test_adapter_defaults_match_the_shipped_profile():
    from dna.prodtrack_providers.profile import load_profile

    assert AyonAdapter.model_validate(load_profile("ayon").adapter) == AyonAdapter()


# ── reading ──────────────────────────────────────────────────────────────────


def test_user_lookup_by_email():
    p, _ = provider()
    user = p.get_user_by_email("ann@x.com")
    assert isinstance(user, User) and (user.id, user.name, user.login) == (
        "ann",
        "Ann Artist",
        "ann",
    )
    with pytest.raises(ValueError, match="User not found: no@x.com"):
        p.get_user_by_email("no@x.com")


def test_projects_are_listed_as_the_user_so_ayon_applies_their_access():
    p, fake = provider()
    assert [x.id for x in p.get_projects_for_user("ann@x.com")] == [P]
    assert [x.id for x in p.get_projects_for_user("bob@x.com")] == [P, "other"]
    projects = p.get_projects_for_user("ann@x.com")
    assert isinstance(projects[0], Project) and projects[0].name == "Demo"
    assert [c["as"] for c in fake.log if c["op"] == "graphql"][-1] == "ann"


def test_playlists_are_entity_lists_of_versions():
    p, _ = provider()
    (playlist,) = p.get_playlists_for_project(P)
    assert isinstance(playlist, Playlist)
    assert (playlist.id, playlist.code) == (f"{P}:L1", "Dailies")
    assert playlist.project == {"type": "Project", "id": P, "name": P}
    with pytest.raises(ValueError, match="expected the project name"):
        p.get_playlists_for_project(123)


def test_versions_follow_the_list_order_and_are_fully_converted():
    p, _ = provider()
    versions = p.get_versions_for_playlist(f"{P}:L1")
    assert [v.id for v in versions] == [
        f"{P}:v2",
        f"{P}:v1",
    ]  # by item position, not id

    v1 = versions[1]
    assert isinstance(v1, Version)
    assert v1.name == "sh010_renderLighting_v001"
    assert (v1.status, v1.description) == ("Pending review", "desc of v1")
    assert v1.user == {"type": "User", "id": "ann", "name": "Ann Artist"}
    assert isinstance(v1.entity, Shot) and (v1.entity.id, v1.entity.name) == (
        f"{P}:f1",
        "sh010",
    )
    assert v1.entity.description == "Opening"
    assert v1.task.name == "lighting" and v1.task.status == "In progress"
    assert v1.task.pipeline_step == {
        "type": "TaskType",
        "id": "Lighting",
        "name": "Lighting",
    }
    assert v1.movie_path == f"http://ayon.test/api/projects/{P}/files/file-v1/payload"
    assert v1.thumbnail == f"http://ayon.test/api/projects/{P}/versions/v1/thumbnail"
    assert v1.created_at == "2026-10-06T09:00:00+00:00"
    assert v1.prodtrack_detail_url == "http://ayon.test/projects/demo/products"

    v2 = versions[0]
    assert isinstance(v2.entity, Asset) and v2.task is None and v2.movie_path is None


def test_notes_are_oldest_first_with_subjects_and_authors():
    p, _ = provider()
    notes = p.get_versions_for_playlist(f"{P}:L1")[1].notes
    assert [n.id for n in notes] == [
        f"{P}:c1",
        f"{P}:c2",
    ]  # chronological, not as returned
    assert (notes[0].subject, notes[0].content) == (None, "first plain note")
    assert (notes[1].subject, notes[1].content) == ("Second", "later note")
    assert notes[1].author.login == "bob" and notes[1].author.email == "bob@x.com"
    assert notes[1].note_links[0].id == f"{P}:v1"


def test_an_unlisted_folder_type_falls_back_to_the_configured_subject_kind():
    p, fake = provider()
    fake.state["versions"]["v2"]["product"]["folder"]["folderType"] = "Library"
    assert isinstance(p.get_versions_for_playlist(f"{P}:L1")[0].entity, Shot)


def test_studio_profile_changes_which_folder_types_are_assets(monkeypatch, tmp_path):
    (tmp_path / "site.yaml").write_text(
        "prodtrack:\n  connector: ayon\n  profile:\n    entities:\n"
        "      shot: {where: {folderType: [Shot, Sequence]}}\n"
        "      asset: {where: {folderType: [Library]}}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DNA_CONFIG_DIR", str(tmp_path))
    p, fake = provider()
    fake.state["versions"]["v2"]["product"]["folder"]["folderType"] = "Library"
    assert isinstance(p.get_versions_for_playlist(f"{P}:L1")[0].entity, Asset)


def test_get_entity_for_every_kind():
    p, _ = provider()
    assert p.get_entity("project", P).name == "Demo"
    assert p.get_entity("user", "bob").email == "bob@x.com"
    assert p.get_entity("version", f"{P}:v1").name == "sh010_renderLighting_v001"
    assert isinstance(p.get_entity("shot", f"{P}:f1"), Shot)
    assert p.get_entity("task", f"{P}:t1").entity.name == "sh010"
    playlist = p.get_entity("playlist", f"{P}:L1")
    assert [v.id for v in playlist.versions] == [f"{P}:v2", f"{P}:v1"]


def test_get_entity_errors():
    p, _ = provider()
    with pytest.raises(ValueError, match="Unknown entity type"):
        p.get_entity("nope", "x")
    with pytest.raises(ValueError, match="Entity not found"):
        p.get_entity("asset", f"{P}:f1")  # a Shot folder is not an asset
    with pytest.raises(ValueError, match="Entity not found"):
        p.get_entity("version", f"{P}:missing")
    with pytest.raises(ValueError, match="Invalid AYON id"):
        p.get_entity("version", 5)


def test_statuses_come_from_project_scope_and_union_across_projects():
    p, _ = provider()
    expected = [
        {"code": "Pending review", "name": "Pending review"},
        {"code": "Approved", "name": "Approved"},
    ]
    assert p.get_version_statuses(P) == expected
    assert p.get_version_statuses() == expected  # same names across projects collapse


def test_search():
    p, _ = provider()
    assert p.search("ann", ["user"]) == [
        {"type": "User", "id": "ann", "name": "Ann Artist", "email": "ann@x.com"}
    ]
    shots = p.search("sh", ["shot"], P)
    assert shots[0]["type"] == "Shot" and shots[0]["id"] == f"{P}:f1"
    assert p.search("", ["version"], P)[0]["name"] == "sh010_renderLighting_v001"
    with pytest.raises(ValueError, match="Unsupported entity type"):
        p.search("x", ["note"])


def test_search_covers_every_type_the_ui_asks_for():
    """The mention and link pickers search user/shot/asset/version/task together; one
    unsupported type fails the whole request (400), so each must be answered."""
    p, _ = provider()
    got = p.search("", ["user", "shot", "asset", "version", "task"], P)
    assert {r["type"] for r in got} >= {"User", "Shot", "Version", "Task"}
    task = next(r for r in got if r["type"] == "Task")
    assert task["id"].startswith(f"{P}:") and task["project"] == {
        "type": "Project",
        "id": P,
    }
    assert p.search("zzz-no-match", ["task"], P) == []
    assert all(r["type"] == "Playlist" for r in p.search("", ["playlist"], P))


def test_free_form_find_is_refused_with_guidance():
    p, _ = provider()
    with pytest.raises(ValueError, match="not supported by the AYON connector"):
        p.find("version", [])


# ── identity ─────────────────────────────────────────────────────────────────


def test_calls_act_as_the_signed_in_user():
    p, fake = provider(sudo_user="bob")
    fake.log.clear()
    p.get_playlists_for_project(P)
    calls = [
        c for c in fake.log if c["op"] == "graphql" and "ids" not in c["vars"] or True
    ]
    user_checks = [c for c in calls if "names" in c["vars"]]
    work = [c for c in calls if "names" not in c["vars"]]
    assert [c["as"] for c in user_checks] == [
        None
    ]  # the check itself uses the service identity
    assert work and all(c["as"] == "bob" for c in work)  # everything else acts as bob


def test_an_unknown_or_deactivated_user_fails_closed_as_an_auth_error():
    p, _ = provider(sudo_user="ghost")
    with pytest.raises(ProdtrackAuthError, match="does not know the user 'ghost'"):
        p.get_playlists_for_project(P)


def test_verified_users_are_cached_briefly():
    p, fake = provider(sudo_user="bob")
    p.get_playlists_for_project(P)
    first = len([q for q in fake.queries if "users(names:" in q])
    p2 = AyonProvider(client=fake, sudo_user="bob")
    p2.get_playlists_for_project(P)
    assert len([q for q in fake.queries if "users(names:" in q]) == first  # no re-check


# ── writing ──────────────────────────────────────────────────────────────────


def test_publish_note_posts_a_comment_with_subject_and_mentions():
    p, fake = provider(sudo_user="ann")
    note_id = p.publish_note(f"{P}:v1", "Looks good", "Review", ["bob"], ["ann"], [])
    assert note_id == f"{P}:new1"
    post = next(c for c in fake.log if c["op"] == "rest" and c["method"] == "POST")
    assert post["path"] == f"/api/projects/{P}/versions/v1/activities"
    assert post["json"]["activityType"] == "comment"
    assert (
        post["json"]["body"]
        == "**Review**\n\nLooks good\n\n[@bob](user:bob) [@ann](user:ann)"
    )
    assert post["as"] == "ann"


def test_publish_note_is_idempotent_on_identical_body():
    p, fake = provider()
    p.publish_note(f"{P}:v1", "later note", "Second", [], [], [])
    assert not [c for c in fake.log if c["op"] == "rest" and c["method"] == "POST"]
    assert p.publish_note(f"{P}:v1", "later note", "Second", [], [], []) == f"{P}:c2"


def test_publish_note_as_its_author_and_with_status():
    p, fake = provider(sudo_user="ann")
    p.publish_note(
        f"{P}:v1",
        "x",
        "y",
        [],
        [],
        [],
        author_email="bob@x.com",
        version_status="Approved",
    )
    post = next(c for c in fake.log if c["op"] == "rest" and c["method"] == "POST")
    patch = next(c for c in fake.log if c["op"] == "rest" and c["method"] == "PATCH")
    assert post["as"] == "bob"  # attributed to the real author, not the requester
    assert patch["path"] == f"/api/projects/{P}/versions/v1" and patch["json"] == {
        "status": "Approved"
    }
    assert patch["as"] == "ann"


def test_publish_note_for_an_unknown_author():
    p, _ = provider()
    with pytest.raises(
        UserNotFoundError, match="Author not found in AYON: ghost@x.com"
    ):
        p.publish_note(f"{P}:v1", "x", "y", [], [], [], author_email="ghost@x.com")


def test_update_version_status_reports_failure_instead_of_raising():
    p, fake = provider()
    assert p.update_version_status(f"{P}:v1", "Approved") is True
    assert p.update_version_status("not-an-ayon-id", "Approved") is False


def test_attachment_uploads_then_appends_to_the_comment(tmp_path):
    p, fake = provider()
    f = tmp_path / "a.txt"
    f.write_text("hi")
    assert p.attach_file_to_note(f"{P}:c1", str(f), "notes.txt") is True
    upload, patch = [c for c in fake.log if c["op"] == "rest"]
    assert upload["path"] == f"/api/projects/{P}/files" and upload["data"] == b"hi"
    assert (
        upload["headers"]["x-file-name"] == "notes.txt"
        and upload["headers"]["content-type"] == "text/plain"
    )
    assert upload["headers"]["X-Activity-ID"] == "c1"
    assert patch["json"] == {"files": ["new1"], "appendFiles": True}
    assert p.attach_file_to_note("bad", str(f), "x") is False


def test_playlist_lifecycle():
    p, fake = provider()
    created = p.create_playlist(P, "New list")
    assert created.id == f"{P}:new1" and created.code == "New list"
    body = next(c for c in fake.log if c["op"] == "rest")["json"]
    assert body == {
        "label": "New list",
        "entityType": "version",
        "entityListType": "generic",
        "items": [],
    }
    assert (
        p.add_version_to_playlist(f"{P}:L1", f"{P}:v1") is True
    )  # already in the list
    assert not [
        c
        for c in fake.log
        if c["op"] == "rest" and c.get("path", "").endswith("/items")
    ]
    fake.state["versions"]["v3"] = fake.state["versions"]["v2"]
    p.add_version_to_playlist(f"{P}:L1", f"{P}:v3")
    add = [
        c
        for c in fake.log
        if c["op"] == "rest" and c.get("path", "").endswith("/items")
    ][0]
    assert add["json"] == {"entityId": "v3"}
    with pytest.raises(ValueError, match="own project"):
        p.add_version_to_playlist(f"{P}:L1", "other:v1")


def test_transcript_is_a_comment_with_the_text_attached_and_is_replaceable():
    p, fake = provider()
    assert p.transcript_entity_type(P) == "activity"
    tid = p.publish_transcript(
        project_id=P,
        playlist_id=f"{P}:L1",
        version_id=f"{P}:v1",
        meeting_id="m-1",
        meeting_date=date(2026, 10, 6),
        platform="meet",
        body="hello world",
    )
    assert tid == f"{P}:new1"
    ops = [c for c in fake.log if c["op"] == "rest"]
    assert ops[0]["json"]["body"] == "Meeting transcript: meet m-1, 2026-10-06"
    assert (
        ops[1]["data"] == b"hello world"
        and ops[1]["headers"]["x-file-name"] == "transcript-2026-10-06.txt"
    )
    assert ops[2]["json"] == {"files": ["new2"], "appendFiles": True}

    fake.log.clear()
    assert p.update_transcript(
        entity_type="activity",
        entity_id=tid,
        body="edited",
        meeting_date=date(2026, 10, 7),
    )
    replace = [c for c in fake.log if c["op"] == "rest"][-1]
    assert replace["json"]["appendFiles"] is False  # replaces the old attachment


def test_creating_a_note_through_add_entity_needs_a_version_link():
    from dna.models.entity import Note

    p, _ = provider()
    with pytest.raises(ValueError, match="linked to a version"):
        p.add_entity("note", Note(id=0, subject="s", content="c"))
    note = Note(id=0, subject="s", content="c", note_links=[Version(id=f"{P}:v1")])
    assert p.add_entity("note", note).id == f"{P}:new1"
    with pytest.raises(ValueError, match="not supported by AYON"):
        p.add_entity("shot", Shot(id=1))


# ── capabilities and wiring ──────────────────────────────────────────────────


def test_capabilities_and_registry():
    p, _ = provider()
    caps = p.capabilities()
    assert caps.impersonation == "sudo_header" and caps.note_addressing is False
    assert caps.note_mentions is True and "note_attachment" in caps.transcript_storage
    from dna.configsys import providers

    assert providers.prodtrack.resolve("ayon") is AyonProvider


def test_every_query_the_provider_builds_is_well_formed():
    p, fake = provider(sudo_user="ann")
    versions = p.get_versions_for_playlist(f"{P}:L1")
    p.get_entity("shot", f"{P}:f1"), p.get_entity("task", f"{P}:t1"), p.get_entity(
        "playlist", f"{P}:L1"
    )
    p.search("x", ["user", "shot", "version"], P)
    p.get_projects_for_user("ann@x.com")
    p.publish_note(versions[1].id, "c", "s", [], [], [])
    assert len(fake.queries) >= 10  # the fake asserts balance on every one


# ── the HTTP client ──────────────────────────────────────────────────────────


def _client(status=200, body=None, **kw):
    session = mock.Mock()
    response = mock.Mock(status_code=status, content=b"{}" if body is None else b"x")
    response.json.return_value = {} if body is None else body
    response.text = str(body)
    session.request.return_value = response
    return AyonClient("http://ayon.test/", "key", session=session, **kw), session


def test_client_sends_the_key_and_the_impersonation_header():
    client, session = _client(as_user="ann")
    client.rest("GET", "/api/users/me")
    sent = session.request.call_args.kwargs["headers"]
    assert sent["X-Api-Key"] == "key" and sent["x-as-user"] == "ann"
    assert client.url == "http://ayon.test"
    assert "x-as-user" not in _client()[0]._headers()


def test_client_translates_http_failures_into_domain_errors():
    for status, expected in [
        (401, ProdtrackAuthError),
        (403, ProdtrackPermissionError),
        (404, AyonNotFound),
        (500, ProdtrackUnavailableError),
        (503, ProdtrackUnavailableError),
    ]:
        client, _ = _client(status, {"detail": "boom"})
        with pytest.raises(expected):
            client.rest("GET", "/x")
    assert issubclass(AyonNotFound, ValueError)  # the API layer turns it into 404


def test_client_translates_connection_failures():
    client, session = _client()
    session.request.side_effect = requests.ConnectionError("refused")
    with pytest.raises(ProdtrackUnavailableError, match="unreachable"):
        client.rest("GET", "/x")


def test_client_translates_graphql_errors():
    client, _ = _client(200, {"errors": [{"message": "Forbidden: no access"}]})
    with pytest.raises(ProdtrackPermissionError):
        client.graphql("query { x }")
    client, _ = _client(200, {"errors": [{"message": "Project not found"}]})
    with pytest.raises(AyonNotFound):
        client.graphql("query { x }")
    client, _ = _client(200, {"errors": [{"message": "Syntax Error"}]})
    with pytest.raises(ProdtrackUnavailableError):
        client.graphql("query { x }")


def test_client_pagination_follows_cursors():
    pages = [
        {
            "data": {
                "c": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "A"},
                    "edges": [{"node": 1}, {"node": 2}],
                }
            }
        },
        {
            "data": {
                "c": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "edges": [{"node": 3}],
                }
            }
        },
    ]
    client, session = _client()
    session.request.return_value.content = b"x"
    session.request.return_value.json.side_effect = pages
    assert client.graphql_all("query", ("c",)) == [1, 2, 3]
    sent = [
        c.kwargs["json"]["variables"]["after"] for c in session.request.call_args_list
    ]
    assert sent == [None, "A"]


def test_client_requires_url_and_key():
    with pytest.raises(ValueError, match="AYON_URL"):
        AyonClient("", "k")
    with pytest.raises(ValueError, match="AYON_API_KEY"):
        AyonClient("http://x", "")


# ── caching and concurrency ──────────────────────────────────────────────────


def _reviewable_calls(fake):
    return [
        c for c in fake.log if c["op"] == "rest" and c["path"].endswith("/reviewables")
    ]


def test_reviewables_are_cached_briefly_for_the_same_user():
    p, fake = provider()
    p.get_versions_for_playlist(f"{P}:L1")
    first = len(_reviewable_calls(fake))
    p.get_versions_for_playlist(f"{P}:L1")
    assert first > 0 and len(_reviewable_calls(fake)) == first  # second read: no calls


def test_one_users_cached_reviewables_are_never_served_to_another():
    """Reviewables are fetched with the acting user's access; a cache shared across
    users would let someone see media metadata they may not."""
    p, fake = provider(sudo_user="ann")
    p.get_versions_for_playlist(f"{P}:L1")
    after_ann = len(_reviewable_calls(fake))
    q = AyonProvider(client=fake, sudo_user="bob")
    q.get_versions_for_playlist(f"{P}:L1")
    assert len(_reviewable_calls(fake)) > after_ann  # bob had to ask for himself


def test_cached_author_objects_cannot_be_changed_through_a_result():
    p, _ = provider()
    first = p.get_versions_for_playlist(f"{P}:L1")
    assert first[1].user["name"] == "Ann Artist"
    first[1].user["name"] = "tampered"
    again = p.get_versions_for_playlist(f"{P}:L1")
    assert again[1].user["name"] == "Ann Artist"


def test_search_results_keep_the_order_the_types_were_asked_for():
    p, _ = provider()
    got = p.search("", ["version", "user", "shot"], P)
    types = [r["type"] for r in got]
    assert types.index("Version") < types.index("User") < types.index("Shot")


def test_an_unsupported_type_fails_before_any_search_runs():
    p, fake = provider()
    with pytest.raises(ValueError, match="Unsupported entity type"):
        p.search("x", ["user", "note"])
    assert not fake.queries  # nothing was sent to the tracker
