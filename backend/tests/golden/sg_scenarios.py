"""Scenario runner for the ShotGrid behavioural golden test.

Runs a fixed set of provider operations against an in-memory fake ShotGrid that
records every call made to it.  The recorded calls and the DNA results are the
"behaviour" of a provider.  ``generate_shotgrid_calls.py`` records them from the
reference implementation; ``test_shotgrid_golden.py`` replays them against the
current one and requires an exact match.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from typing import Any
from unittest import mock

PROJECT = {"type": "Project", "id": 10, "name": "Proj"}
SHOT = {"type": "Shot", "id": 500, "name": "sh010"}
TASK = {"type": "Task", "id": 600, "name": "anim"}

DATA: dict[str, list[dict[str, Any]]] = {
    "HumanUser": [
        {"id": 1, "name": "Ann Artist", "email": "ann@x.com", "login": "ann"},
        {"id": 2, "name": "Bob Sup", "email": "bob@x.com", "login": "bob"},
    ],
    "Project": [PROJECT],
    "Shot": [
        {
            "id": 500,
            "code": "sh010",
            "description": "Opening shot",
            "project": PROJECT,
            "tasks": [TASK],
        }
    ],
    "Asset": [
        {"id": 501, "code": "hero", "description": "Hero", "project": PROJECT, "tasks": []}
    ],
    "Task": [
        {
            "id": 600,
            "sg_status_list": "ip",
            "step": {"type": "Step", "id": 7, "name": "Animation"},
            "content": "anim",
            "project": PROJECT,
            "entity": SHOT,
        }
    ],
    "Version": [
        {
            "id": 1000 + i,
            "code": f"sh010_v00{i}",
            "description": f"take {i}",
            "sg_status_list": "rev",
            "user": {"type": "HumanUser", "id": 1, "name": "Ann Artist"},
            "created_at": "2026-01-0%dT10:00:00Z" % i,
            "updated_at": "2026-01-0%dT11:00:00Z" % i,
            "sg_path_to_movie": f"/movies/v{i}.mov",
            "sg_path_to_frames": f"/frames/v{i}.####.exr",
            "project": PROJECT,
            "image": f"https://thumbs/v{i}.png",
            "entity": SHOT,
            "sg_task": TASK,
            "notes": [],
        }
        for i in (1, 2)
    ],
    "Playlist": [
        {
            "id": 100,
            "code": "dailies",
            "description": "Daily review",
            "project": PROJECT,
            "created_at": "2026-01-03T09:00:00Z",
            "updated_at": "2026-01-03T09:30:00Z",
            "versions": [
                {"type": "Version", "id": 1001, "name": "sh010_v001"},
                {"type": "Version", "id": 1002, "name": "sh010_v002"},
            ],
        }
    ],
    "Note": [
        {
            "id": 900,
            "subject": "Subj",
            "content": "Content",
            "project": PROJECT,
            "note_links": [
                {"type": "Playlist", "id": 100},
                {"type": "Version", "id": 1001},
            ],
            "created_by": {"type": "HumanUser", "id": 1, "name": "Ann Artist"},
            "created_by.HumanUser.email": "ann@x.com",
            "created_at": "2026-01-03T10:00:00Z",
        }
    ],
    "CustomEntity05": [],
}


class FakeShotgun:
    """Records calls and answers from :data:`DATA`."""

    def __init__(self, label: str, log: list[dict[str, Any]]):
        self.label = label
        self._log = log
        self._next = 5000

    def _rec(self, method: str, *args: Any, **kwargs: Any) -> None:
        norm = {
            k: (sorted(v) if k == "fields" and isinstance(v, list) else v)
            for k, v in kwargs.items()
        }
        self._log.append(
            {"conn": self.label, "method": method, "args": list(args), "kwargs": norm}
        )

    @staticmethod
    def _match(rec: dict[str, Any], flt: list[Any]) -> bool:
        field, op, value = flt
        have = rec.get(field)
        if op == "is":
            if isinstance(have, list):
                return any(_same(h, value) for h in have)
            return _same(have, value)
        if op == "in":
            return have in value
        if op == "contains":
            return str(value).lower() in str(have).lower()
        return True

    def find(self, entity_type, filters=None, fields=None, limit=0, **_):
        self._rec("find", entity_type, filters=filters, fields=fields, limit=limit)
        rows = [
            r
            for r in copy.deepcopy(DATA.get(entity_type, []))
            if all(
                self._match(r, f)
                for f in (filters or [])
                if not (entity_type == "Project" and f[0] == "users")
            )
        ]
        if limit:
            rows = rows[:limit]
        return [_project(r, fields) for r in rows]

    def find_one(self, entity_type, filters=None, fields=None, **_):
        self._rec("find_one", entity_type, filters=filters, fields=fields)
        for r in copy.deepcopy(DATA.get(entity_type, [])):
            if all(self._match(r, f) for f in (filters or [])):
                return _project(r, fields)
        return None

    def create(self, entity_type, data, **_):
        self._rec("create", entity_type, data=data)
        self._next += 1
        return {"type": entity_type, "id": self._next, **copy.deepcopy(data)}

    def update(self, entity_type, entity_id, data, **_):
        self._rec("update", entity_type, entity_id, data=data)
        return {"type": entity_type, "id": entity_id}

    def upload(self, entity_type, entity_id, path, **kwargs):
        self._rec("upload", entity_type, entity_id, path, **kwargs)
        return 1

    def schema_field_read(self, entity_type, field_name=None, project_entity=None, **_):
        self._rec(
            "schema_field_read", entity_type, field_name, project_entity=project_entity
        )
        return {
            field_name: {
                "properties": {
                    "valid_values": {"value": ["rev", "apr", "ip"]},
                    "display_values": {
                        "value": {"rev": "Pending Review", "apr": "Approved"}
                    },
                }
            }
        }


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.get("type") == b.get("type") and a.get("id") == b.get("id")
    return a == b


def _project(row: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    if not fields:
        return row
    out = {"type": row.get("type"), "id": row["id"]}
    for f in fields:
        if f in row:
            out[f] = row[f]
    return out


def _dump(value: Any) -> Any:
    if isinstance(value, list):
        return [_dump(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    if hasattr(value, "__to_dict__"):
        return value.__to_dict__()
    return value


def run_scenarios(module: Any) -> dict[str, Any]:
    """Run every scenario against ``module.ShotgridProvider``.

    ``module`` is the shotgrid module under test (legacy or current); its
    ``Shotgun`` is replaced by the recording fake for the duration.
    """
    out: dict[str, Any] = {}
    log: list[dict[str, Any]] = []

    def factory(url, script_name, api_key, sudo_as_login=None, **_):
        return FakeShotgun(f"sudo:{sudo_as_login}" if sudo_as_login else "script", log)

    with mock.patch.object(module, "Shotgun", side_effect=factory), mock.patch.dict(
        "os.environ", {"SHOTGRID_TRANSCRIPT_ENTITY": "CustomEntity05"}
    ):
        provider = module.ShotgridProvider(
            url="https://studio.shotgrid.autodesk.com",
            script_name="script",
            api_key="key",
            sudo_user="ann",
        )

        def run(name: str, fn):
            log.clear()
            try:
                result = _dump(fn())
            except Exception as exc:  # exceptions are behaviour too
                result = {"__raises__": f"{type(exc).__name__}: {exc}"}
            out[name] = {"result": result, "calls": copy.deepcopy(log)}

        run("get_entity_version", lambda: provider.get_entity("version", 1001))
        run(
            "get_entity_version_shallow",
            lambda: provider.get_entity("version", 1001, resolve_links=False),
        )
        run("get_entity_shot", lambda: provider.get_entity("shot", 500))
        run("get_entity_unknown_type", lambda: provider.get_entity("nope", 1))
        run("get_entity_missing", lambda: provider.get_entity("shot", 99999))
        run(
            "find_versions_filtered",
            lambda: provider.find(
                "version",
                [{"field": "status", "operator": "is", "value": "rev"}],
                limit=5,
            ),
        )
        run(
            "find_unknown_field",
            lambda: provider.find(
                "version", [{"field": "bogus", "operator": "is", "value": 1}]
            ),
        )
        run("find_unsupported_type", lambda: provider.find("nope", []))
        run(
            "search_all_types",
            lambda: provider.search("a", ["user", "shot", "asset", "version"], 10, 7),
        )
        run("search_unsupported", lambda: provider.search("a", ["nope"]))
        run("get_user_by_email", lambda: provider.get_user_by_email("ann@x.com"))
        run("get_user_by_email_missing", lambda: provider.get_user_by_email("no@x.com"))
        run("get_projects_for_user", lambda: provider.get_projects_for_user("ann@x.com"))
        run("get_playlists_for_project", lambda: provider.get_playlists_for_project(10))
        run("create_playlist", lambda: provider.create_playlist(10, "new-list"))
        run("get_versions_for_playlist", lambda: provider.get_versions_for_playlist(100))
        run("get_versions_empty_playlist", lambda: provider.get_versions_for_playlist(404))
        run("add_version_to_playlist_new", lambda: provider.add_version_to_playlist(100, 1003))
        run(
            "add_version_to_playlist_present",
            lambda: provider.add_version_to_playlist(100, 1001),
        )
        run("get_version_statuses", lambda: provider.get_version_statuses(10))
        run("get_version_statuses_no_project", lambda: provider.get_version_statuses())
        run("update_version_status", lambda: provider.update_version_status(1001, "apr"))
        run(
            "update_note",
            lambda: provider.update_note(900, "new text", "new subj", 1001, "apr"),
        )
        run(
            "publish_note",
            lambda: provider.publish_note(
                version_id=1001,
                content="Looks good",
                subject="Review",
                to_users=[1],
                cc_users=[2],
                links=[provider.get_entity("shot", 500, resolve_links=False)],
                version_status="apr",
            ),
        )
        run(
            "publish_note_duplicate",
            lambda: provider.publish_note(
                version_id=1001,
                content="Content",
                subject="Subj",
                to_users=[],
                cc_users=[],
                links=[],
                version_status="apr",
            ),
        )
        run(
            "publish_note_as_author",
            lambda: provider.publish_note(
                version_id=1002,
                content="Author note",
                subject="By Bob",
                to_users=[1],
                cc_users=[],
                links=[],
                author_email="bob@x.com",
            ),
        )
        run(
            "publish_note_unknown_author",
            lambda: provider.publish_note(
                version_id=1002,
                content="x",
                subject="y",
                to_users=[],
                cc_users=[],
                links=[],
                author_email="ghost@x.com",
            ),
        )
        run("attach_file", lambda: provider.attach_file_to_note(900, "/tmp/f.txt", "f.txt"))
        run(
            "publish_transcript",
            lambda: provider.publish_transcript(
                project_id=10,
                playlist_id=100,
                version_id=1001,
                meeting_id="m-1",
                meeting_date=date(2026, 1, 3),
                platform="meet",
                body="hello world",
            ),
        )
        run(
            "update_transcript",
            lambda: provider.update_transcript(
                entity_type="CustomEntity05",
                entity_id=5001,
                body="edited",
                meeting_date=date(2026, 1, 4),
            ),
        )
    return json.loads(json.dumps(out, default=str))
