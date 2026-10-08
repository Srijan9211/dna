"""AYON production tracking provider.

How AYON maps onto DNA
----------------------
=====================  =====================================================
DNA                    AYON
=====================  =====================================================
Project                project (id = the project name)
Shot / Asset           folder, told apart by ``folderType`` (profile ``where``)
Task                   task
Version                version, titled from its product and folder
Playlist               entity list of versions
Note                   ``comment`` activity on a version
User                   user (id = the username)
=====================  =====================================================

Ids
---
AYON needs the project name for every lookup, and DNA ids are opaque, so every id
below the project level is ``"<project>:<uuid>"``.  A project's id is its name and a
user's is their username.  See :mod:`dna.models.ids`.

Identity
--------
The connector authenticates with a service-user API key.  When a user is being
acted for (``sudo_user``) every call carries ``x-as-user``, so AYON applies that
user's own project access.  The service user needs the rights to do so; the user is
verified to exist first, because AYON answers a bare 500 for an unknown one.
"""

from __future__ import annotations

import mimetypes
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from dna.configsys import get_config
from dna.configsys.meta import Section
from dna.media_proxy import MediaStream
from dna.models.entity import (
    ENTITY_MODELS,
    Asset,
    EntityBase,
    Note,
    Playlist,
    Project,
    Shot,
    Task,
    User,
    Version,
)
from dna.prodtrack_providers.ayon_client import AyonClient, AyonNotFound
from dna.prodtrack_providers.mapper import ProfileMapper
from dna.prodtrack_providers.profile import ConnectorProfile, load_profile
from dna.prodtrack_providers.prodtrack_provider_base import (
    ProdtrackAuthError,
    ProdtrackProviderBase,
    UserNotFoundError,
)

# ── ids ──────────────────────────────────────────────────────────────────────


def make_id(project: str, entity_uuid: str) -> str:
    """The DNA id of a project-level AYON entity."""
    return f"{project}:{entity_uuid}"


def split_id(value: Any) -> tuple[str, str]:
    """Split a DNA id into ``(project, uuid)``.

    Raises:
        ValueError: ``value`` was not issued by this connector.
    """
    if isinstance(value, str) and ":" in value:
        project, _, entity_uuid = value.partition(":")
        if project and entity_uuid:
            return project, entity_uuid
    raise ValueError(f"Invalid AYON id {value!r}: expected '<project>:<id>'")


# ── adapter config ───────────────────────────────────────────────────────────


class AyonAdapter(Section):
    """The ``adapter`` block of the AYON profile."""

    version_name: str = "{folder}_{product}_{version}"
    unmatched_subject_kind: Literal["shot", "asset"] = "shot"
    list_type: str = "generic"
    comment_activity_type: str = "comment"
    # AYON's editor writes a mention as a Markdown link with a user: target.  If a
    # server does not register it as a mention it still reads as plain "@login".
    mention_template: str = "[@{login}](user:{login})"
    reviewable_label: Optional[str] = None  # prefer this reviewable when several
    max_workers: int = 8


def _ayon_profile() -> ConnectorProfile:
    cfg = get_config().get("prodtrack")
    if cfg.connector == "ayon":
        return load_profile("ayon", path=cfg.profile_path, overrides=cfg.profile)
    return load_profile("ayon")


# ── GraphQL helpers ──────────────────────────────────────────────────────────


def build_selection(paths: Iterable[str]) -> str:
    """Turn dotted paths into a GraphQL selection: ``attrib.x`` -> ``attrib { x }``."""
    tree: dict[str, dict] = {}
    for path in paths:
        node = tree
        for part in path.split("."):
            node = node.setdefault(part, {})

    def render(node: dict) -> str:
        return " ".join(
            key if not child else f"{key} {{ {render(child)} }}"
            for key, child in node.items()
        )

    return render(tree)


_USER_SELECTION = "name active attrib { fullName email }"
_TITLE_PREFIX = re.compile(r"^\*\*(?P<subject>[^\n]+?)\*\*\n\n", re.S)

# How long a verified x-as-user target is trusted before being re-checked.
_VERIFY_TTL_SECONDS = 60.0
_verified: dict[tuple[str, str], float] = {}


class AyonProvider(ProdtrackProviderBase):
    """AYON provider for production tracking operations."""

    SUPPORTS_IMPERSONATION = True
    MEDIA_REQUIRES_PROXY = True  # AYON media needs credentials a browser cannot send

    def __init__(
        self,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        sudo_user: Optional[str] = None,
        session_id: Optional[str] = None,
        client: Optional[AyonClient] = None,
        profile: Optional[ConnectorProfile] = None,
    ):
        super().__init__()
        cfg = get_config().get("prodtrack").ayon
        self.profile: ConnectorProfile = profile or _ayon_profile()
        self.mapper = ProfileMapper(self.profile)
        self.adapter = AyonAdapter.model_validate(self.profile.adapter)
        self.session_id = session_id
        self.sudo_user = sudo_user or cfg.sudo_user

        if client is None:
            client = AyonClient(
                url or cfg.url or "",
                api_key or cfg.api_key or "",
                timeout=cfg.timeout,
                as_user_header=cfg.impersonation_header,
            )
        self.url = client.url
        self._service = client
        self._client: Optional[AyonClient] = None

    # ── handshake ─────────────────────────────────────────────────────────

    @classmethod
    def for_handshake(cls, prodtrack_config: Any, profile: ConnectorProfile) -> "AyonProvider":
        ayon = prodtrack_config.ayon
        return cls(
            url=ayon.url, api_key=ayon.api_key, sudo_user=ayon.sudo_user, profile=profile
        )

    def check_connection(self) -> dict[str, Any]:
        info = self._service.rest("GET", "/api/info")
        me = self._service.rest("GET", "/api/users/me")  # proves the API key
        facts: dict[str, Any] = {
            "server_version": (info.get("releaseInfo") or {}).get("version") or info.get("version"),
            "service_user": me.get("name"),
        }
        # Prove the impersonation mechanism: ask who the server thinks we are while
        # acting as someone else.  A key that may not impersonate is ignored, or
        # refused, and every per-user permission in DNA would silently be the
        # service account's.
        target = self.sudo_user or next(
            (u for u in self._users_for_probe() if u != me.get("name")), None
        )
        if target is None:
            facts["impersonation"] = "not tested: the server has no other user"
            return facts
        seen = self._service.acting_as(target).rest("GET", "/api/users/me").get("name")
        if seen != target:
            raise ProdtrackAuthError(
                f"AYON did not act as '{target}' (it answered as '{seen}'). The service "
                "user needs permission to impersonate other users."
            )
        facts["impersonation"] = f"verified: acted as '{target}'"
        return facts

    def _users_for_probe(self) -> list[str]:
        data = self._service.graphql(
            "query { users(first: 20) { edges { node { name active } } } }"
        )
        return [e["node"]["name"] for e in data.get("users", {}).get("edges", []) if e["node"].get("active", True)]

    # Fields AYON's GraphQL schema offers for each entity DNA reads (AYON 1.x).
    _STATIC_FIELDS = {
        "project": {"name", "label", "code", "active"},
        "folder": {"id", "name", "label", "folderType", "path", "status", "tags", "parentId"},
        "task": {"id", "name", "label", "taskType", "status", "tags", "assignees", "folderId"},
        "version": {"id", "name", "version", "status", "tags", "author", "createdAt", "updatedAt", "thumbnailId"},
        "entityList": {"id", "label", "entityType", "createdAt", "updatedAt", "count"},
        "user": {"name", "active"},
        "activity": {"activityId", "body", "createdAt", "updatedAt", "activityType"},
    }
    # AYON attribute scope for each entity above.
    _ATTRIBUTE_SCOPE = {
        "project": "project", "folder": "folder", "task": "task", "version": "version",
        "entityList": "list", "user": "user",
    }

    def discover_schema(self) -> "TrackerSchema":
        from dna.prodtrack_providers.handshake import TrackerSchema

        info = self._service.rest("GET", "/api/info")
        attributes = self._service.rest("GET", "/api/attributes").get("attributes", [])
        entities: dict[str, set[str]] = {}
        for entity, fields in self._STATIC_FIELDS.items():
            scope = self._ATTRIBUTE_SCOPE.get(entity)
            attribs = {f"attrib.{a['name']}" for a in attributes if scope in (a.get("scope") or [])}
            entities[entity] = set(fields) | attribs

        folder_types: set[str] = set()
        task_types: set[str] = set()
        statuses: list[str] = []
        names = self._service_projects()
        for project in names[:5]:
            body = self._service.rest("GET", f"/api/projects/{project}")
            folder_types |= {f["name"] for f in body.get("folderTypes", [])}
            task_types |= {t["name"] for t in body.get("taskTypes", [])}
            for status in body.get("statuses", []):
                if "version" in (status.get("scope") or []) and status["name"] not in statuses:
                    statuses.append(status["name"])
        notes = []
        if not names:
            notes.append(
                "The service user can see no projects, so folder types and statuses "
                "could not be verified. Give it access to a project and re-run."
            )
        return TrackerSchema(
            version=f"AYON {(info.get('releaseInfo') or {}).get('version') or info.get('version', '')}".strip(),
            entities=entities,
            known_entities=set(entities),
            folder_types=folder_types,
            task_types=task_types,
            statuses=statuses,
            notes=notes,
        )

    def _service_projects(self) -> list[str]:
        data = self._service.graphql("query { projects { edges { node { name active } } } }")
        return [e["node"]["name"] for e in data.get("projects", {}).get("edges", []) if e["node"].get("active", True)]

    # ── identity ──────────────────────────────────────────────────────────

    @property
    def _c(self) -> AyonClient:
        """The client calls go through: acting as ``sudo_user`` when set."""
        if self._client is None:
            if self.sudo_user:
                self._verify_user(self.sudo_user)
                self._client = self._service.acting_as(self.sudo_user)
            else:
                self._client = self._service
        return self._client

    def _verify_user(self, login: str) -> None:
        """Fail closed if ``login`` is not an active AYON user."""
        key = (self.url, login)
        if _verified.get(key, 0.0) > time.monotonic():
            return
        users = self._users_by_name([login], client=self._service)
        if login not in users:
            raise ProdtrackAuthError(
                f"AYON does not know the user '{login}'. Your access may have been "
                "revoked or your account deactivated."
            )
        _verified[key] = time.monotonic() + _VERIFY_TTL_SECONDS

    def _author_client(self, author_email: Optional[str]) -> AyonClient:
        """A client acting as the note's real author (or as the current user)."""
        if not author_email:
            return self._c
        try:
            author = self.get_user_by_email(author_email)
        except ValueError as exc:
            raise UserNotFoundError(f"Author not found in AYON: {author_email}") from exc
        return self._service.acting_as(author.login)

    # ── users ─────────────────────────────────────────────────────────────

    def _user_entity(self, node: dict) -> User:
        data = self.mapper.entity_data("user", node, lambda _: None)
        name = node["name"]
        return User(
            id=name,
            name=data.get("name") or name,
            email=data.get("email"),
            login=name,
        )

    def _users_by_name(
        self, names: Iterable[str], *, client: Optional[AyonClient] = None
    ) -> dict[str, User]:
        names = sorted({n for n in names if n})
        if not names:
            return {}
        sel = build_selection(["name", "active", *self.mapper.binding("user").fields])
        data = (client or self._c).graphql(
            f"query($names: [String!]) {{ users(names: $names) {{ edges {{ node {{ {sel} }} }} }} }}",
            {"names": names},
        )
        nodes = [e["node"] for e in data.get("users", {}).get("edges", [])]
        return {n["name"]: self._user_entity(n) for n in nodes if n.get("active", True)}

    def get_user_by_email(self, user_email: str) -> User:
        sel = build_selection(["name", "active", *self.mapper.binding("user").fields])
        data = self._service.graphql(
            f"query($e: [String!]) {{ users(emails: $e) {{ edges {{ node {{ {sel} }} }} }} }}",
            {"e": [user_email]},
        )
        nodes = [e["node"] for e in data.get("users", {}).get("edges", [])]
        nodes = [n for n in nodes if n.get("active", True)]
        if not nodes:
            raise ValueError(f"User not found: {user_email}")
        return self._user_entity(nodes[0])

    # ── projects and playlists ────────────────────────────────────────────

    def _project_ref(self, project: str, label: Optional[str] = None) -> dict[str, Any]:
        return {"type": "Project", "id": project, "name": label or project}

    def get_projects_for_user(self, user_email: str) -> list[Project]:
        user = self.get_user_by_email(user_email)  # ValueError if unknown
        data = self._service.acting_as(user.login).graphql(
            "query { projects { edges { node { name label active } } } }"
        )
        nodes = [e["node"] for e in data.get("projects", {}).get("edges", [])]
        return [
            Project(id=n["name"], name=n.get("label") or n["name"])
            for n in nodes
            if n.get("active", True)
        ]

    def get_playlists_for_project(self, project_id: Any) -> list[Playlist]:
        project = self._project_name(project_id)
        nodes = self._c.graphql_all(
            "query($project: String!, $first: Int, $after: String) {"
            " project(name: $project) { entityLists(first: $first, after: $after) {"
            " pageInfo { hasNextPage endCursor }"
            " edges { node { id label entityType createdAt updatedAt } } } } }",
            ("project", "entityLists"),
            {"project": project},
        )
        return [
            self._playlist_entity(project, n)
            for n in nodes
            if n.get("entityType") == "version"
        ]

    def _playlist_entity(self, project: str, node: dict) -> Playlist:
        data = self.mapper.entity_data("playlist", node, lambda _: None)
        return Playlist(
            id=make_id(project, node["id"]),
            code=data.get("code"),
            description=None,
            project=self._project_ref(project),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            versions=[],
        )

    def create_playlist(self, project_id: Any, name: str) -> Playlist:
        project = self._project_name(project_id)
        created = self._c.rest(
            "POST",
            f"/api/projects/{project}/lists",
            json={
                "label": name,
                "entityType": "version",
                "entityListType": self.adapter.list_type,
                "items": [],
            },
        )
        return Playlist(
            id=make_id(project, created["id"]),
            code=name,
            project=self._project_ref(project),
            versions=[],
        )

    def add_version_to_playlist(self, playlist_id: Any, version_id: Any) -> bool:
        project, list_id = split_id(playlist_id)
        v_project, version_uuid = split_id(version_id)
        if v_project != project:
            raise ValueError("A version can only join a playlist in its own project")
        if version_uuid in self._list_item_ids(project, list_id):
            return True
        self._c.rest(
            "POST",
            f"/api/projects/{project}/lists/{list_id}/items",
            json={"entityId": version_uuid},
        )
        return True

    def _list_item_ids(self, project: str, list_id: str) -> list[str]:
        """Entity ids in a list, in the list's own order."""
        data = self._c.graphql(
            "query($project: String!, $ids: [String!]) { project(name: $project) {"
            " entityLists(ids: $ids) { edges { node { id items { edges {"
            " entityId position } } } } } } }",
            {"project": project, "ids": [list_id]},
        )
        lists = data.get("project", {}).get("entityLists", {}).get("edges", [])
        if not lists:
            raise ValueError(f"Playlist {make_id(project, list_id)} not found")
        edges = lists[0]["node"]["items"]["edges"]
        return [e["entityId"] for e in sorted(edges, key=lambda e: e.get("position") or 0)]

    # ── versions ──────────────────────────────────────────────────────────

    def _folder_selection(self) -> str:
        paths = ["id", "name", "label", "folderType", "path"]
        for kind in ("shot", "asset"):
            if self.mapper.has(kind):
                paths += list(self.mapper.binding(kind).fields)
        return build_selection(paths)

    def _version_selection(self) -> str:
        v_paths = ["id", "name", "version", "status", "author", "thumbnailId",
                   "hasReviewables", *self.mapper.binding("version").fields]
        comment = (
            "activities(activityTypes: [\"%s\"]) { edges { node { activityId body"
            " createdAt author { %s } } } }" % (self.adapter.comment_activity_type, _USER_SELECTION)
        )
        return (
            f"{build_selection(v_paths)} {comment}"
            f" product {{ id name productType folder {{ {self._folder_selection()} }} }}"
            " task { id name taskType status }"
        )

    def _fetch_versions(self, project: str, ids: list[str]) -> list[dict]:
        nodes: list[dict] = []
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            nodes += self._c.graphql_all(
                "query($project: String!, $ids: [String!], $first: Int, $after: String) {"
                " project(name: $project) { versions(ids: $ids, first: $first, after: $after) {"
                " pageInfo { hasNextPage endCursor } edges { node {"
                f" {self._version_selection()} }} }} }} }} }}",
                ("project", "versions"),
                {"project": project, "ids": chunk},
            )
        return nodes

    def _reviewables(self, project: str, version_uuid: str) -> list[dict]:
        try:
            body = self._c.rest("GET", f"/api/projects/{project}/versions/{version_uuid}/reviewables")
        except AyonNotFound:
            return []
        return body.get("reviewables", []) if isinstance(body, dict) else []

    def _pick_reviewable(self, reviewables: list[dict]) -> Optional[dict]:
        if not reviewables:
            return None
        wanted = self.adapter.reviewable_label
        if wanted:
            for item in reviewables:
                if item.get("label") == wanted:
                    return item
        return reviewables[0]

    def _kind_for_folder(self, folder_type: Optional[str]) -> str:
        for kind in ("shot", "asset"):
            if self.mapper.has(kind):
                allowed = self.mapper.binding(kind).where.get("folderType") or []
                if folder_type in allowed:
                    return kind
        return self.adapter.unmatched_subject_kind

    def _subject_entity(self, project: str, folder: dict) -> EntityBase:
        kind = self._kind_for_folder(folder.get("folderType"))
        data = self.mapper.entity_data(kind, folder, lambda _: None)
        return ENTITY_MODELS[kind](
            id=make_id(project, folder["id"]),
            name=data.get("name") or folder.get("name"),
            description=data.get("description"),
            project=self._project_ref(project),
            tasks=[],
        )

    def _task_entity(self, project: str, task: dict, folder: Optional[EntityBase] = None) -> Task:
        data = self.mapper.entity_data("task", task, lambda _: None)
        return Task(
            id=make_id(project, task["id"]),
            name=data.get("name"),
            status=data.get("status"),
            pipeline_step={"type": "TaskType", "id": task.get("taskType"), "name": task.get("taskType")},
            project=self._project_ref(project),
            entity=folder,
        )

    def _title(self, folder: Optional[dict], product: Optional[dict], version: dict) -> str:
        return self.adapter.version_name.format(
            folder=(folder or {}).get("name", ""),
            product=(product or {}).get("name", ""),
            version=version.get("name", ""),
        ).strip("_ ")

    def _version_entity(
        self,
        project: str,
        node: dict,
        *,
        reviewable: Optional[dict],
        users: dict[str, User],
    ) -> Version:
        data = self.mapper.entity_data("version", node, lambda _: None)
        product = node.get("product") or {}
        folder = product.get("folder")
        subject = self._subject_entity(project, folder) if folder else None
        task = self._task_entity(project, node["task"], subject) if node.get("task") else None
        vid = make_id(project, node["id"])
        author = node.get("author")
        author_user = users.get(author) if author else None

        version = Version(
            id=vid,
            name=self._title(folder, product, node),
            description=data.get("description"),
            status=data.get("status"),
            user=(
                {"type": "User", "id": author, "name": author_user.name if author_user else author}
                if author
                else None
            ),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            movie_path=self._media_url(project, reviewable),
            frame_path=None,
            thumbnail=self._thumbnail_url(project, node),
            project=self._project_ref(project),
            entity=subject,
            task=task,
            notes=self._comments(project, vid, node, users_hint=users),
        )
        version.prodtrack_detail_url = self.mapper.url(
            "version_detail", base=self.url, kind="version", entity_id=node["id"], project=project
        )
        if folder:
            version.prodtrack_entity_detail_url = self.mapper.url(
                "entity_detail",
                base=self.url,
                kind=self._kind_for_folder(folder.get("folderType")),
                entity_id=folder["id"],
                project=project,
            )
        return version

    def _media_url(self, project: str, reviewable: Optional[dict]) -> Optional[str]:
        if not reviewable or not reviewable.get("fileId"):
            return None
        # Requires the AYON credentials; DNA's media proxy serves it to browsers.
        return f"{self.url}/api/projects/{project}/files/{reviewable['fileId']}/payload"

    def open_media(self, version_id: Any, kind: str, range_header: Optional[str] = None) -> "MediaStream":
        project, version_uuid = split_id(version_id)
        if kind == "thumbnail":
            path = f"/api/projects/{project}/versions/{version_uuid}/thumbnail"
        elif kind == "movie":
            reviewable = self._pick_reviewable(self._reviewables(project, version_uuid))
            if not reviewable or not reviewable.get("fileId"):
                raise AyonNotFound(f"Version {version_id} has no reviewable media")
            path = f"/api/projects/{project}/files/{reviewable['fileId']}/payload"
        else:
            raise ValueError(f"Unknown media kind '{kind}'")
        response = self._c.request(
            "GET", path, headers={"Range": range_header} if range_header else None, stream=True
        )
        passed = ("content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified")
        headers = {k: v for k, v in response.headers.items() if k.lower() in passed}
        return MediaStream(response.status_code, headers, response.iter_content(64 * 1024), response.close)

    def _thumbnail_url(self, project: str, node: dict) -> Optional[str]:
        if not node.get("thumbnailId"):
            return None
        return f"{self.url}/api/projects/{project}/versions/{node['id']}/thumbnail"

    def _version_authors(self, nodes: list[dict]) -> dict[str, User]:
        names = {n.get("author") for n in nodes if n.get("author")}
        for n in nodes:
            for edge in (n.get("activities") or {}).get("edges", []):
                author = (edge["node"].get("author") or {}).get("name")
                if author:
                    names.add(author)
        return self._users_by_name(names)

    def _build_versions(self, project: str, nodes: list[dict]) -> list[Version]:
        users = self._version_authors(nodes)
        with ThreadPoolExecutor(max_workers=max(1, self.adapter.max_workers)) as pool:
            media = list(pool.map(lambda n: self._reviewables(project, n["id"]), nodes))
        return [
            self._version_entity(
                project, node, reviewable=self._pick_reviewable(items), users=users
            )
            for node, items in zip(nodes, media)
        ]

    def get_versions_for_playlist(self, playlist_id: Any) -> list[Version]:
        project, list_id = split_id(playlist_id)
        ids = self._list_item_ids(project, list_id)
        if not ids:
            return []
        by_id = {n["id"]: n for n in self._fetch_versions(project, ids)}
        ordered = [by_id[i] for i in ids if i in by_id]  # keep the list's order
        return self._build_versions(project, ordered)

    # ── notes ─────────────────────────────────────────────────────────────

    def _comments(
        self, project: str, version_id: str, node: dict, *, users_hint: dict[str, User]
    ) -> list[Note]:
        edges = (node.get("activities") or {}).get("edges", [])
        nodes = sorted((e["node"] for e in edges), key=lambda a: a.get("createdAt") or "")
        notes = []
        for activity in nodes:
            author = activity.get("author") or {}
            login = author.get("name")
            body = activity.get("body") or ""
            subject, content = self._split_body(body)
            notes.append(
                Note(
                    id=make_id(project, activity["activityId"]),
                    subject=subject,
                    content=content,
                    project=self._project_ref(project),
                    note_links=[Version(id=version_id)],
                    author=users_hint.get(login) or (self._user_entity(author) if login else None),
                )
            )
        return notes  # oldest first: the API layer treats later as newer

    @staticmethod
    def _split_body(body: str) -> tuple[Optional[str], str]:
        match = _TITLE_PREFIX.match(body)
        if match:
            return match.group("subject"), body[match.end() :]
        return None, body

    def _compose_body(
        self, subject: str, content: str, to_users: list[Any], cc_users: list[Any]
    ) -> str:
        parts = []
        if subject:
            parts.append(f"**{subject}**")
        if content:
            parts.append(content)
        recipients = [str(u) for u in [*to_users, *cc_users] if u]
        if recipients:
            parts.append(
                " ".join(self.adapter.mention_template.format(login=login) for login in recipients)
            )
        return "\n\n".join(parts)

    def _existing_comment(self, client: AyonClient, project: str, version_uuid: str, body: str) -> Optional[str]:
        data = client.graphql(
            "query($project: String!, $ids: [String!]) { project(name: $project) {"
            " versions(ids: $ids) { edges { node { activities(activityTypes: [\"%s\"]) {"
            " edges { node { activityId body } } } } } } } }" % self.adapter.comment_activity_type,
            {"project": project, "ids": [version_uuid]},
        )
        for v in data.get("project", {}).get("versions", {}).get("edges", []):
            for a in (v["node"].get("activities") or {}).get("edges", []):
                if (a["node"].get("body") or "") == body:
                    return a["node"]["activityId"]
        return None

    def publish_note(
        self,
        version_id: Any,
        content: str,
        subject: str,
        to_users: list[Any],
        cc_users: list[Any],
        links: list[EntityBase],
        author_email: Optional[str] = None,
        version_status: Optional[str] = None,
    ) -> Any:
        project, version_uuid = split_id(version_id)
        body = self._compose_body(subject, content, to_users, cc_users)
        client = self._author_client(author_email)

        existing = self._existing_comment(client, project, version_uuid, body)
        if existing:
            if version_status:
                self.update_version_status(version_id, version_status)
            return make_id(project, existing)

        created = client.rest(
            "POST",
            f"/api/projects/{project}/versions/{version_uuid}/activities",
            json={"activityType": self.adapter.comment_activity_type, "body": body},
        )
        if version_status:
            self.update_version_status(version_id, version_status)
        return make_id(project, created["id"])

    def update_note(
        self,
        note_id: Any,
        content: str,
        subject: Optional[str] = None,
        version_id: Optional[Any] = None,
        version_status: Optional[str] = None,
    ) -> bool:
        try:
            project, activity_id = split_id(note_id)
            body = self._compose_body(subject or "", content, [], [])
            self._c.rest("PATCH", f"/api/projects/{project}/activities/{activity_id}", json={"body": body})
            if version_status and version_id:
                self.update_version_status(version_id, version_status)
            return True
        except Exception:
            return False

    def update_version_status(self, version_id: Any, status: str) -> bool:
        try:
            project, version_uuid = split_id(version_id)
            self._c.rest("PATCH", f"/api/projects/{project}/versions/{version_uuid}", json={"status": status})
            return True
        except Exception:
            return False

    def get_version_statuses(self, project_id: Any = None) -> list[dict[str, str]]:
        projects = [self._project_name(project_id)] if project_id else self._accessible_projects()
        seen: dict[str, dict[str, str]] = {}
        for project in projects:
            body = self._c.rest("GET", f"/api/projects/{project}")
            for status in body.get("statuses", []):
                if "version" in (status.get("scope") or []):
                    seen.setdefault(status["name"], {"code": status["name"], "name": status["name"]})
        return list(seen.values())

    def _accessible_projects(self) -> list[str]:
        data = self._c.graphql("query { projects { edges { node { name active } } } }")
        return [e["node"]["name"] for e in data.get("projects", {}).get("edges", []) if e["node"].get("active", True)]

    # ── attachments and transcripts ───────────────────────────────────────

    def _upload(self, project: str, activity_id: str, name: str, payload: bytes) -> str:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        uploaded = self._c.rest(
            "POST",
            f"/api/projects/{project}/files",
            data=payload,
            headers={"x-file-name": name, "content-type": mime, "X-Activity-ID": activity_id},
        )
        return uploaded["id"]

    def attach_file_to_note(self, note_id: Any, file_path: str, display_name: str) -> bool:
        try:
            project, activity_id = split_id(note_id)
            file_id = self._upload(project, activity_id, display_name, Path(file_path).read_bytes())
            self._c.rest(
                "PATCH",
                f"/api/projects/{project}/activities/{activity_id}",
                json={"files": [file_id], "appendFiles": True},
            )
            return True
        except Exception:
            return False

    def transcript_entity_type(self, project_id: Any = None) -> str:
        return "activity"

    def publish_transcript(
        self,
        *,
        project_id: Any,
        playlist_id: Any,
        version_id: Any,
        meeting_id: str,
        meeting_date: date,
        platform: str,
        body: str,
    ) -> Any:
        """Post the transcript as a comment on the version, with the text attached."""
        project, version_uuid = split_id(version_id)
        header = f"Meeting transcript: {platform} {meeting_id}, {meeting_date.isoformat()}"
        created = self._c.rest(
            "POST",
            f"/api/projects/{project}/versions/{version_uuid}/activities",
            json={"activityType": self.adapter.comment_activity_type, "body": header},
        )
        activity_id = created["id"]
        name = f"transcript-{meeting_date.isoformat()}.txt"
        file_id = self._upload(project, activity_id, name, body.encode("utf-8"))
        self._c.rest(
            "PATCH",
            f"/api/projects/{project}/activities/{activity_id}",
            json={"files": [file_id], "appendFiles": True},
        )
        return make_id(project, activity_id)

    def update_transcript(
        self, *, entity_type: str, entity_id: Any, body: str, meeting_date: date
    ) -> bool:
        """Replace the attached transcript text, leaving the comment otherwise as is."""
        try:
            project, activity_id = split_id(entity_id)
            name = f"transcript-{meeting_date.isoformat()}.txt"
            file_id = self._upload(project, activity_id, name, body.encode("utf-8"))
            self._c.rest(
                "PATCH",
                f"/api/projects/{project}/activities/{activity_id}",
                json={"files": [file_id], "appendFiles": False},
            )
            return True
        except Exception:
            return False

    # ── generic entity access ─────────────────────────────────────────────

    def _project_name(self, project_id: Any) -> str:
        if isinstance(project_id, str) and project_id and ":" not in project_id:
            return project_id
        raise ValueError(f"Invalid AYON project id {project_id!r}: expected the project name")

    def get_entity(self, entity_type: str, entity_id: Any, resolve_links: bool = True) -> EntityBase:
        self.mapper.binding(entity_type)
        if entity_type == "project":
            project = self._project_name(entity_id)
            data = self._c.graphql(
                "query($p: String!) { project(name: $p) { name label active } }", {"p": project}
            )
            node = data.get("project")
            if not node:
                raise ValueError(f"Entity not found: project {entity_id}")
            return Project(id=node["name"], name=node.get("label") or node["name"])
        if entity_type == "user":
            found = self._users_by_name([str(entity_id)])
            if str(entity_id) not in found:
                raise ValueError(f"Entity not found: user {entity_id}")
            return found[str(entity_id)]

        project, uuid = split_id(entity_id)
        if entity_type == "version":
            nodes = self._fetch_versions(project, [uuid])
            if not nodes:
                raise ValueError(f"Entity not found: version {entity_id}")
            return self._build_versions(project, nodes)[0]
        if entity_type in ("shot", "asset"):
            data = self._c.graphql(
                "query($p: String!, $ids: [String!]) { project(name: $p) { folders(ids: $ids) {"
                f" edges {{ node {{ {self._folder_selection()} }} }} }} }} }}",
                {"p": project, "ids": [uuid]},
            )
            edges = data.get("project", {}).get("folders", {}).get("edges", [])
            if not edges or self._kind_for_folder(edges[0]["node"].get("folderType")) != entity_type:
                raise ValueError(f"Entity not found: {entity_type} {entity_id}")
            return self._subject_entity(project, edges[0]["node"])
        if entity_type == "task":
            data = self._c.graphql(
                "query($p: String!, $ids: [String!]) { project(name: $p) { tasks(ids: $ids) {"
                " edges { node { id name taskType status folder { "
                f"{self._folder_selection()} }} }} }} }} }} }}",
                {"p": project, "ids": [uuid]},
            )
            edges = data.get("project", {}).get("tasks", {}).get("edges", [])
            if not edges:
                raise ValueError(f"Entity not found: task {entity_id}")
            node = edges[0]["node"]
            folder = self._subject_entity(project, node["folder"]) if node.get("folder") else None
            return self._task_entity(project, node, folder)
        if entity_type == "playlist":
            lists = [p for p in self.get_playlists_for_project(project) if p.id == entity_id]
            if not lists:
                raise ValueError(f"Entity not found: playlist {entity_id}")
            playlist = lists[0]
            playlist.versions = [
                Version(id=make_id(project, i)) for i in self._list_item_ids(project, uuid)
            ]
            return playlist
        raise ValueError(f"Reading a '{entity_type}' by id is not supported by AYON")

    def add_entity(self, entity_type: str, entity: EntityBase) -> EntityBase:
        if entity_type != "note":
            raise ValueError(f"Creating a '{entity_type}' is not supported by AYON")
        note = entity  # type: ignore[assignment]
        links = getattr(note, "note_links", None) or []
        version = next((link for link in links if isinstance(link, Version)), None)
        if version is None:
            raise ValueError("An AYON note must be linked to a version")
        created_id = self.publish_note(
            version.id, note.content or "", note.subject or "", [], [], []
        )
        note.id = created_id
        return note

    def find(self, entity_type: str, filters: list[dict[str, Any]], limit: int = 0) -> list[EntityBase]:
        raise ValueError(
            "Free-form entity queries are not supported by the AYON connector; use "
            "get_entity, search, or the playlist endpoints."
        )

    def search(
        self,
        query: str,
        entity_types: list[str],
        project_id: Any = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        q = (query or "").strip()
        results: list[dict[str, Any]] = []
        projects = [self._project_name(project_id)] if project_id else None
        for entity_type in entity_types:
            if entity_type == "user":
                results += self._search_users(q, limit)
            elif entity_type in ("shot", "asset"):
                for project in projects or self._accessible_projects():
                    results += self._search_folders(project, entity_type, q, limit)
            elif entity_type == "version":
                for project in projects or self._accessible_projects():
                    results += self._search_versions(project, q, limit)
            elif entity_type == "task":
                for project in projects or self._accessible_projects():
                    results += self._search_tasks(project, q, limit)
            elif entity_type == "playlist":
                for project in projects or self._accessible_projects():
                    results += self._search_playlists(project, q, limit)
            else:
                raise ValueError(f"Unsupported entity type: {entity_type}")
        return results

    def _search_tasks(self, project: str, q: str, limit: int) -> list[dict[str, Any]]:
        # AYON has no name search on tasks, so read a page and filter here.
        data = self._c.graphql(
            "query($p: String!) { project(name: $p) { tasks(first: 500) {"
            " edges { node { id name label taskType folder { name } } } } } }",
            {"p": project},
        )
        out: list[dict[str, Any]] = []
        for edge in data.get("project", {}).get("tasks", {}).get("edges", []):
            node = edge["node"]
            name = node.get("label") or node.get("name") or ""
            if q.lower() not in f"{name} {node.get('taskType') or ''}".lower():
                continue
            out.append(
                {
                    "type": "Task",
                    "id": make_id(project, node["id"]),
                    "name": name,
                    "description": (node.get("folder") or {}).get("name"),
                    "project": {"type": "Project", "id": project},
                }
            )
            if len(out) >= limit:
                break
        return out

    def _search_playlists(self, project: str, q: str, limit: int) -> list[dict[str, Any]]:
        out = []
        for playlist in self.get_playlists_for_project(project):
            if q.lower() in (playlist.code or "").lower():
                out.append(
                    {
                        "type": "Playlist",
                        "id": playlist.id,
                        "name": playlist.code,
                        "description": playlist.description,
                        "project": {"type": "Project", "id": project},
                    }
                )
            if len(out) >= limit:
                break
        return out

    def _search_users(self, q: str, limit: int) -> list[dict[str, Any]]:
        sel = build_selection(["name", "active", *self.mapper.binding("user").fields])
        data = self._c.graphql(f"query {{ users(first: 200) {{ edges {{ node {{ {sel} }} }} }} }}")
        out = []
        for edge in data.get("users", {}).get("edges", []):
            user = self._user_entity(edge["node"])
            if not edge["node"].get("active", True):
                continue
            haystack = f"{user.login} {user.name} {user.email or ''}".lower()
            if q.lower() in haystack:
                out.append({"type": "User", "id": user.id, "name": user.name, "email": user.email})
            if len(out) >= limit:
                break
        return out

    def _search_folders(self, project: str, kind: str, q: str, limit: int) -> list[dict[str, Any]]:
        types = self.mapper.binding(kind).where.get("folderType") or None
        data = self._c.graphql(
            "query($p: String!, $q: String, $t: [String!], $n: Int) { project(name: $p) {"
            " folders(search: $q, folderTypes: $t, first: $n) { edges { node {"
            f" {self._folder_selection()} }} }} }} }} }}",
            {"p": project, "q": q or None, "t": types, "n": limit},
        )
        out = []
        for edge in data.get("project", {}).get("folders", {}).get("edges", []):
            subject = self._subject_entity(project, edge["node"])
            out.append(
                {
                    "type": ENTITY_MODELS[kind].__name__,
                    "id": subject.id,
                    "name": subject.name,
                    "description": subject.description,
                    "project": {"type": "Project", "id": project},
                }
            )
        return out

    def _search_versions(self, project: str, q: str, limit: int) -> list[dict[str, Any]]:
        data = self._c.graphql(
            "query($p: String!, $q: String, $n: Int) { project(name: $p) {"
            " versions(search: $q, first: $n) { edges { node { id name"
            " attrib { description } product { name folder { name } } } } } } }",
            {"p": project, "q": q or None, "n": limit},
        )
        out = []
        for edge in data.get("project", {}).get("versions", {}).get("edges", []):
            node = edge["node"]
            product = node.get("product") or {}
            out.append(
                {
                    "type": "Version",
                    "id": make_id(project, node["id"]),
                    "name": self._title(product.get("folder"), product, node),
                    "description": (node.get("attrib") or {}).get("description"),
                    "project": {"type": "Project", "id": project},
                }
            )
        return out
