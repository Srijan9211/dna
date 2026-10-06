"""ShotGrid production tracking provider implementation.

Authentication
--------------
Every connection authenticates with the script account
(``SHOTGRID_SCRIPT_NAME`` + ``SHOTGRID_API_KEY``).  How the connection is
scoped depends on whether a user is being impersonated:

**User-scoped mode** (``sudo_user`` provided — all authenticated requests):
    Opens ``Shotgun(url, script_name, api_key, sudo_as_login=<login>)``.
    ShotGrid runs the query as that user and enforces their own permission
    group natively, so no filtering is required in application code.  The
    login name is resolved server-side from the session store; the user's
    password is verified once at login and never stored.

**Script mode** (``sudo_user`` is None — background jobs / dev):
    The bare script account, which has full site permissions.  Never use
    this for user-facing requests — see Issue #55.

All queries must go through the ``self._sg`` property rather than
``self.sg`` directly, so that an active ``sudo()`` context is honoured.
"""

import contextlib
from datetime import date
from typing import Any, Optional, cast

from shotgun_api3 import Fault as SGFault
from shotgun_api3 import Shotgun

try:  # AuthenticationFault is absent from very old shotgun_api3 releases.
    from shotgun_api3 import AuthenticationFault
except ImportError:  # pragma: no cover - depends on installed library version
    AuthenticationFault = None

from dna.configsys import get_config
from dna.configsys.meta import Section
from dna.models.entity import (
    ENTITY_MODELS,
    EntityBase,
    Playlist,
    Project,
    User,
    Version,
)
from dna.prodtrack_providers.mapper import ProfileMapper
from dna.prodtrack_providers.profile import ConnectorProfile, load_profile
from dna.prodtrack_providers.prodtrack_provider_base import (
    ProdtrackAuthError,
    ProdtrackPermissionError,
    ProdtrackProviderBase,
    ProdtrackUnavailableError,
    UserNotFoundError,
)

class _NoteAdapter(Section):
    """ShotGrid Note field names that are not DNA fields."""

    to_field: str = "addressings_to"
    cc_field: str = "addressings_cc"
    attachments_field: str = "attachments"
    author_email_field: str = "created_by.HumanUser.email"
    created_at_field: str = "created_at"


class _TranscriptAdapter(Section):
    """Fields of the custom entity that stores published transcripts."""

    code: str = "code"
    project: str = "project"
    playlist: str = "sg_playlist"
    version: str = "sg_version_in_review"
    meeting_id: str = "sg_meeting_id"
    meeting_date: str = "sg_meeting_date"
    platform: str = "sg_platform"
    body: str = "sg_transcript_body"


class ShotgridAdapter(Section):
    """The ``adapter`` block of the ShotGrid profile: names that only mean
    something to the ShotGrid API.  Defaults are ShotGrid's standard schema."""

    status_field: str = "sg_status_list"
    project_members_field: str = "users"
    note: _NoteAdapter = _NoteAdapter()
    transcript: _TranscriptAdapter = _TranscriptAdapter()


def _shotgrid_profile() -> ConnectorProfile:
    """The ShotGrid profile in effect: the configured one when ShotGrid is the
    active connector (so studio overrides apply), else the packaged default."""
    cfg = get_config().get("prodtrack")
    if cfg.connector == "shotgrid":
        return load_profile(
            "shotgrid", path=cfg.profile_path, overrides=cfg.profile
        )
    return load_profile("shotgrid")


# The packaged default mapping, kept for code that imported it.  Providers use
# their own (possibly studio-overridden) profile, not this constant.
FIELD_MAPPING = load_profile("shotgrid").field_mapping()


# ── ShotGrid fault translation ────────────────────────────────────────────────
#
# shotgun_api3 signals every server-side problem as a Fault carrying a prose
# message — a permission denial and an outage arrive as the same exception type.
# Left unhandled these surface as HTTP 500, so a user querying a project they
# cannot see is told "internal server error" instead of "forbidden".
#
# Classification lives here, next to the ShotGrid-specific knowledge it needs.
# main.py registers a single exception handler that calls this and maps the
# result onto a status code, so no call site has to wrap anything.

# The markers live in the connector profile (``errors.permission_markers``) so a
# studio on a localised or customised ShotGrid can extend them.
_PERMISSION_FAULT_MARKERS = tuple(load_profile("shotgrid").errors.permission_markers)


def classify_sg_fault(exc: Exception) -> Exception:
    """Map a ShotGrid ``Fault`` onto the matching DNA domain exception.

    Returns (rather than raises) the exception so callers and the FastAPI
    handler can decide what to do with it.

    - ``AuthenticationFault`` → :class:`ProdtrackAuthError` (401): ShotGrid
      rejected the impersonated identity, usually a deactivated account.
    - message naming a permission problem → :class:`ProdtrackPermissionError`
      (403): the account is valid but lacks access to this resource.
    - anything else → :class:`ProdtrackUnavailableError` (503).
    """
    message = str(exc)

    if AuthenticationFault is not None and isinstance(exc, AuthenticationFault):
        return ProdtrackAuthError(
            "ShotGrid rejected the current identity. Your access may have been "
            "revoked or your account deactivated. Please log in again."
        )

    lowered = message.lower()
    markers = _shotgrid_profile().errors.permission_markers or _PERMISSION_FAULT_MARKERS
    if any(marker.lower() in lowered for marker in markers):
        return ProdtrackPermissionError(
            "ShotGrid denied access to this resource for your account."
        )

    return ProdtrackUnavailableError(f"ShotGrid request failed: {message}")


class ShotgridProvider(ProdtrackProviderBase):
    """ShotGrid provider for production tracking operations."""

    SUPPORTS_IMPERSONATION = True

    def __init__(
        self,
        url: Optional[str] = None,
        script_name: Optional[str] = None,
        api_key: Optional[str] = None,
        sudo_user: Optional[str] = None,
        connect: bool = True,
        session_id: Optional[str] = None,
        profile: Optional[ConnectorProfile] = None,
    ):
        """Initialize the ShotGrid connection.

        All connections authenticate with the script account.  When
        ``sudo_user`` is set, the connection additionally passes
        ``sudo_as_login=<sudo_user>`` so ShotGrid applies that user's own
        permission group to every query.  No user credential is ever held by
        this class — the user's password is verified once at login and
        discarded, and only their ShotGrid login name is carried forward.

        Args:
            url:          ShotGrid server URL. Defaults to SHOTGRID_URL.
            script_name:  API script name. Defaults to SHOTGRID_SCRIPT_NAME.
            api_key:      API key. Defaults to SHOTGRID_API_KEY.
            sudo_user:    ShotGrid login name to impersonate via sudo_as_login.
                          Resolved server-side from the session store.
            connect:      Whether to connect immediately.
            session_id:   The user's DNA session ID. Diagnostics only.
            profile:      Connector profile (field mappings, names, capabilities).
                          Defaults to the configured ShotGrid profile.
        """
        super().__init__()

        self.profile: ConnectorProfile = profile or _shotgrid_profile()
        self.mapper = ProfileMapper(self.profile)
        self.adapter = ShotgridAdapter.model_validate(self.profile.adapter)

        sg_cfg = get_config().get("prodtrack").shotgrid
        self.url: str = (url or sg_cfg.url or "").rstrip("/")
        self._sudo_connection: Optional[Shotgun] = None

        if not self.url:
            raise ValueError("SHOTGRID_URL is required.")

        self.session_id = session_id
        self.script_name = script_name or sg_cfg.script_name
        self.api_key = api_key or sg_cfg.api_key
        self.sudo_user = sudo_user or sg_cfg.sudo_user

        if not all([self.script_name, self.api_key]):
            raise ValueError(
                "ShotGrid script credentials not provided. Set SHOTGRID_SCRIPT_NAME "
                "and SHOTGRID_API_KEY."
            )

        self.sg: Optional[Shotgun] = None
        if connect:
            self.connect()

    def connect(self, sudo_user: Optional[str] = None) -> None:
        """Connect using script credentials."""
        self.sg = Shotgun(
            self.url,
            self.script_name,
            self.api_key,
            sudo_as_login=sudo_user or self.sudo_user,
        )

    def set_sudo_user(self, sudo_user: str) -> None:
        """Set sudo user and re-connect (script-auth only)."""
        self.sudo_user = sudo_user
        self.connect()

    @contextlib.contextmanager
    def sudo(self, user_login: str):
        """Context manager to perform actions as a specific user.

        Opens a temporary script connection carrying
        ``sudo_as_login=<user_login>`` and routes every ``self._sg`` call
        through it for the duration of the block, so ShotGrid attributes the
        action to that user and enforces their permission group.  The previous
        connection is restored on exit, including on exception.
        """
        original = self._sudo_connection
        try:
            self._sudo_connection = Shotgun(
                self.url,
                self.script_name,
                self.api_key,
                sudo_as_login=user_login,
            )
            yield
        finally:
            self._sudo_connection = original

    @property
    def _sg(self) -> Optional[Shotgun]:
        """Return the active ShotGrid connection (sudo override or main).

        Always use this rather than ``self.sg`` so that an open ``sudo()``
        context is honoured — reaching for ``self.sg`` directly runs the query
        as the bare script account and silently ignores the impersonation.

        ``Fault`` exceptions raised through this connection propagate to the
        FastAPI handler registered in main.py, which uses
        :func:`classify_sg_fault` to return 403/401/503 as appropriate.
        """
        return self._sudo_connection or self.sg

    # ── Entity conversion ─────────────────────────────────────────────── #

    def _convert_sg_entity_to_dna_entity(
        self,
        sg_entity: dict,
        entity_mapping: Optional[dict] = None,
        entity_type: Optional[str] = None,
        resolve_links: bool = True,
    ) -> EntityBase:
        resolve = (
            self._resolve_linked_field if resolve_links else self._convert_shallow_link
        )
        if entity_mapping is None:
            entity_data = self.mapper.entity_data(entity_type, sg_entity, resolve)
        else:  # an explicit legacy-shaped mapping dict
            entity_data = {
                dna: sg_entity.get(sg)
                for sg, dna in entity_mapping["fields"].items()
            }
            for sg, dna in entity_mapping.get("linked_fields", {}).items():
                entity_data[dna] = resolve(sg_entity.get(sg))
        return ENTITY_MODELS[entity_type](**entity_data)

    def _convert_shallow_link(self, data):
        if data is None:
            return None
        if isinstance(data, dict):
            return self._create_shallow_entity(data)
        elif isinstance(data, list):
            return [self._create_shallow_entity(item) for item in data if item]
        return None

    def _create_shallow_entity(self, sg_link: dict) -> EntityBase:
        sg_type = sg_link.get("type")
        entity_id = sg_link.get("id")
        name = sg_link.get("name")
        dna_type = self.mapper.kind_for_source(sg_type)
        model_class = ENTITY_MODELS[dna_type]
        if dna_type == "playlist":
            return model_class(id=entity_id, code=name)
        return model_class(id=entity_id, name=name)

    def _resolve_linked_field(self, data):
        if isinstance(data, dict):
            dna_type = self.mapper.kind_for_source(data["type"])
            return self.get_entity(dna_type, data["id"], resolve_links=False)
        elif isinstance(data, list):
            return [
                self.get_entity(
                    self.mapper.kind_for_source(item["type"]),
                    item["id"],
                    resolve_links=False,
                )
                for item in data
            ]
        return None

    def _sg_type_of(self, entity: EntityBase) -> str:
        """ShotGrid entity type for a DNA entity (its class name if unbound)."""
        kind = self.mapper.kind_of(entity)
        return self.mapper.source(kind) if self.mapper.has(kind) else kind.capitalize()

    def _convert_entities_to_sg_links(self, entities):
        if isinstance(entities, EntityBase):
            return {"type": self._sg_type_of(entities), "id": entities.id}
        elif isinstance(entities, list):
            return [
                {"type": self._sg_type_of(e), "id": e.id}
                for e in entities
                if isinstance(e, EntityBase)
            ]
        return None

    def _project_link(self, project_id: int) -> dict:
        return {"type": self.mapper.source("project"), "id": project_id}

    # ── CRUD ──────────────────────────────────────────────────────────── #

    def get_entity(
        self, entity_type: str, entity_id: int, resolve_links: bool = True
    ) -> EntityBase:
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")
        binding = self.mapper.binding(entity_type)
        sg_entity = self._sg.find_one(
            binding.source,
            filters=[["id", "is", entity_id]],
            fields=list(set(self.mapper.read_fields(entity_type))),
        )
        if not sg_entity:
            raise ValueError(f"Entity not found: {entity_type} {entity_id}")
        return self._convert_sg_entity_to_dna_entity(
            sg_entity, None, entity_type, resolve_links=resolve_links
        )

    def add_entity(self, entity_type: str, entity: EntityBase) -> EntityBase:
        binding = self.mapper.binding(entity_type)
        sg_entity_data = {}
        for sg_field_name, dna_field_name in binding.fields.items():
            if sg_field_name == "id":
                continue
            value = entity.model_dump().get(dna_field_name)
            if value is not None:
                sg_entity_data[sg_field_name] = value
        linked_fields_to_preserve = {}
        for sg_field_name, dna_field_name in binding.links.items():
            linked_entities = getattr(entity, dna_field_name, None)
            if linked_entities is None:
                continue
            linked_fields_to_preserve[dna_field_name] = linked_entities
            sg_linked = self._convert_entities_to_sg_links(linked_entities)
            if sg_linked:
                sg_entity_data[sg_field_name] = sg_linked
        result = self._sg.create(binding.source, sg_entity_data)
        created_entity = self._convert_sg_entity_to_dna_entity(
            result, None, entity_type, resolve_links=False
        )
        for dna_field_name, linked_entities in linked_fields_to_preserve.items():
            setattr(created_entity, dna_field_name, linked_entities)
        return created_entity

    def find(
        self, entity_type: str, filters: list[dict[str, Any]], limit: int = 0
    ) -> list[EntityBase]:
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")
        binding = self.mapper.binding(
            entity_type, message="Unsupported entity type: {kind}"
        )
        sg_filters = self.mapper.to_tracker_filters(entity_type, filters)
        sg_results = self._sg.find(
            binding.source,
            filters=sg_filters,
            fields=self.mapper.read_fields(entity_type),
            limit=limit,
        )
        return [
            self._convert_sg_entity_to_dna_entity(r, None, entity_type)
            for r in sg_results
        ]

    def search(
        self,
        query: str,
        entity_types: list[str],
        project_id: int | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not self.sg:
            raise ValueError("Not connected to ShotGrid")
        results = []
        for entity_type in entity_types:
            binding = self.mapper.binding(
                entity_type, message="Unsupported entity type: {kind}"
            )
            name_sg_field = self.mapper.tracker_field_or_none(entity_type, "name")
            if name_sg_field is None:
                continue
            sg_fields = ["id", name_sg_field]
            email_field = self.mapper.tracker_field_or_none(entity_type, "email")
            desc_field = self.mapper.tracker_field_or_none(entity_type, "description")
            project_field = self.mapper.tracker_field_or_none(entity_type, "project")
            if entity_type == "user":
                sg_fields.append(email_field or "email")
            else:
                if desc_field:
                    sg_fields.append(desc_field)
                if project_field:
                    sg_fields.append(project_field)
            q = (query or "").strip()
            sg_filters: list = []
            if q:
                sg_filters.append([name_sg_field, "contains", q])
            if entity_type != "user" and project_id is not None:
                sg_filters.append(
                    [
                        project_field or "project",
                        "is",
                        self._project_link(project_id),
                    ]
                )
            sg_results = self._sg.find(
                binding.source, filters=sg_filters, fields=sg_fields, limit=limit
            )
            model_class = ENTITY_MODELS.get(entity_type)
            dna_type = model_class.__name__ if model_class else entity_type.capitalize()
            for sg_entity in sg_results:
                result = {
                    "type": dna_type,
                    "id": sg_entity.get("id"),
                    "name": sg_entity.get(name_sg_field),
                }
                if entity_type == "user":
                    result["email"] = sg_entity.get(email_field or "email")
                else:
                    if (desc_field or "description") in sg_entity:
                        result["description"] = sg_entity.get(
                            desc_field or "description"
                        )
                    project_data = sg_entity.get(project_field or "project")
                    if project_data:
                        result["project"] = {
                            "type": project_data.get("type"),
                            "id": project_data.get("id"),
                        }
                results.append(result)
        return results

    def get_user_by_email(self, user_email: str) -> User:
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")
        sg_user = self._sg.find_one(
            self.mapper.source("user"),
            filters=[[self.mapper.tracker_field("user", "email"), "is", user_email]],
            fields=self.mapper.read_fields("user"),
        )
        if not sg_user:
            raise ValueError(f"User not found: {user_email}")
        return self._convert_sg_entity_to_dna_entity(
            sg_user, None, "user", resolve_links=False
        )

    def get_projects_for_user(self, user_email: str) -> list[Project]:
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")
        m = self.mapper
        email_field = m.tracker_field("user", "email")
        user = self._sg.find_one(
            m.source("user"),
            filters=[[email_field, "is", user_email]],
            fields=[
                m.tracker_field("user", "id"),
                email_field,
                m.tracker_field("user", "name"),
            ],
        )
        if not user:
            raise ValueError(f"User not found: {user_email}")
        sg_projects = self._sg.find(
            m.source("project"),
            filters=[[self.adapter.project_members_field, "is", user]],
            fields=m.read_fields("project"),
        )
        return [
            self._convert_sg_entity_to_dna_entity(
                p, None, "project", resolve_links=False
            )
            for p in sg_projects
        ]

    def get_playlists_for_project(self, project_id: int) -> list[Playlist]:
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")
        m = self.mapper
        sg_playlists = self._sg.find(
            m.source("playlist"),
            filters=[
                [
                    m.tracker_field("playlist", "project"),
                    "is",
                    self._project_link(project_id),
                ]
            ],
            fields=list(m.binding("playlist").fields),
        )
        return [
            self._convert_sg_entity_to_dna_entity(
                p, None, "playlist", resolve_links=False
            )
            for p in sg_playlists
        ]

    def create_playlist(self, project_id: int, name: str) -> Playlist:
        """Create a new playlist in ShotGrid.

        Args:
            project_id: The ID of the project the playlist belongs to
            name: The playlist name/code

        Returns:
            The created Playlist entity
        """
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")
        m = self.mapper
        sg_playlist = self._sg.create(
            m.source("playlist"),
            {
                m.tracker_field("playlist", "code"): name,
                m.tracker_field("playlist", "project"): self._project_link(project_id),
            },
        )
        return self._convert_sg_entity_to_dna_entity(
            sg_playlist, None, "playlist", resolve_links=False
        )

    def get_versions_for_playlist(self, playlist_id: int) -> list[Version]:
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")
        m = self.mapper
        versions_field = m.tracker_field("playlist", "versions")
        sg_playlist = self._sg.find_one(
            m.source("playlist"),
            filters=[["id", "is", playlist_id]],
            fields=[versions_field],
        )
        if not sg_playlist or not sg_playlist.get(versions_field):
            return []
        version_ids = [v["id"] for v in sg_playlist[versions_field]]
        sg_versions = self._sg.find(
            m.source("version"),
            filters=[["id", "in", version_ids]],
            fields=m.read_fields("version"),
        )
        task_field = m.tracker_field("version", "task")
        task_ids = list(
            {
                v[task_field]["id"]
                for v in sg_versions
                if v.get(task_field) and v[task_field].get("id")
            }
        )
        tasks_by_id: dict[int, dict] = {}
        if task_ids:
            task_fields = list(m.binding("task").fields)
            for sg_task in self._sg.find(
                m.source("task"), filters=[["id", "in", task_ids]], fields=task_fields
            ):
                tasks_by_id[sg_task["id"]] = sg_task

        note_links_field = m.tracker_field("note", "note_links")
        author_field = m.tracker_field("note", "author")
        author_email_field = self.adapter.note.author_email_field
        sg_notes = self._sg.find(
            m.source("note"),
            filters=[[note_links_field, "is", {"type": m.source("playlist"), "id": playlist_id}]],
            fields=[
                m.tracker_field("note", "id"),
                m.tracker_field("note", "subject"),
                m.tracker_field("note", "content"),
                note_links_field,
                author_field,
                author_email_field,
                self.adapter.note.created_at_field,
            ],
        )
        notes_by_version_id: dict[int, list] = {}
        version_source = m.source("version")
        for sg_note in sg_notes:
            dna_note = self._convert_sg_entity_to_dna_entity(
                sg_note, None, "note", resolve_links=False
            )
            if (
                sg_note.get(author_field)
                and sg_note[author_field].get("type") == m.source("user")
            ):
                email = sg_note.get(author_email_field)
                if email and dna_note.author:
                    dna_note.author.email = email
            links = sg_note.get(note_links_field, [])
            linked_vids = (
                [l["id"] for l in links if l["type"] == version_source]
                if isinstance(links, list)
                else (
                    [links["id"]]
                    if isinstance(links, dict) and links["type"] == version_source
                    else []
                )
            )
            for vid in linked_vids:
                if vid in version_ids:
                    notes_by_version_id.setdefault(vid, []).append(dna_note)

        versions = []
        for sg_version in sg_versions:
            version = self._convert_sg_entity_to_dna_entity(
                sg_version, None, "version", resolve_links=False
            )
            if sg_version.get(task_field) and sg_version[task_field].get("id"):
                task_id = sg_version[task_field]["id"]
                if task_id in tasks_by_id:
                    version.task = self._convert_sg_entity_to_dna_entity(
                        tasks_by_id[task_id], None, "task", resolve_links=False
                    )
            if version.id in notes_by_version_id:
                version.notes = notes_by_version_id[version.id]

            version.prodtrack_detail_url = m.url(
                "version_detail", base=self.url, kind="version", entity_id=version.id
            )
            if version.entity and m.has(m.kind_of(version.entity)):
                version.prodtrack_entity_detail_url = m.url(
                    "entity_detail",
                    base=self.url,
                    kind=m.kind_of(version.entity),
                    entity_id=version.entity.id,
                )

            versions.append(version)
        return versions

    def add_version_to_playlist(self, playlist_id: int, version_id: int) -> bool:
        """Add an existing version to a ShotGrid playlist.

        Args:
            playlist_id: The ID of the playlist
            version_id: The ID of the version to add

        Returns:
            True on success (including when the version was already present)
        """
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")
        m = self.mapper
        versions_field = m.tracker_field("playlist", "versions")
        sg_playlist = self._sg.find_one(
            m.source("playlist"),
            filters=[["id", "is", playlist_id]],
            fields=[versions_field],
        )
        if not sg_playlist:
            raise ValueError(f"Playlist {playlist_id} not found")

        versions = sg_playlist.get(versions_field) or []
        if any(v.get("id") == version_id for v in versions):
            return True

        versions.append({"type": m.source("version"), "id": version_id})
        self._sg.update(m.source("playlist"), playlist_id, {versions_field: versions})
        return True

    def get_version_statuses(
        self, project_id: int | None = None
    ) -> list[dict[str, str]]:
        if not self.sg:
            raise ValueError("Not connected to ShotGrid")
        project_entity = self._project_link(project_id) if project_id else None
        status_field = self.adapter.status_field
        schema = self._sg.schema_field_read(
            self.mapper.source("version"), status_field, project_entity
        )
        if not schema or status_field not in schema:
            return []
        props = schema[status_field].get("properties", {})
        valid_values = props.get("valid_values", {}).get("value", [])
        display_values = props.get("display_values", {}).get("value", {})
        return [{"code": c, "name": display_values.get(c, c)} for c in valid_values]

    def update_version_status(self, version_id: int, status: str) -> bool:
        if not self._sg:
            return False
        try:
            self._sg.update(
                self.mapper.source("version"),
                version_id,
                {self.adapter.status_field: status},
            )
            return True
        except Exception:
            return False

    def update_note(
        self,
        note_id: int,
        content: str,
        subject: Optional[str] = None,
        version_id: Optional[int] = None,
        version_status: Optional[str] = None,
    ) -> bool:
        if not self._sg:
            return False
        m = self.mapper
        data = {m.tracker_field("note", "content"): content}
        if subject:
            data[m.tracker_field("note", "subject")] = subject
        try:
            self._sg.update(m.source("note"), note_id, data)
            if version_status and version_id:
                self._sg.update(
                    m.source("version"),
                    version_id,
                    {self.adapter.status_field: version_status},
                )
            return True
        except Exception as e:
            print(f"Error updating note {note_id}: {e}")
            return False

    def publish_note(
        self,
        version_id: int,
        content: str,
        subject: str,
        to_users: list[int],
        cc_users: list[int],
        links: list[EntityBase],
        author_email: Optional[str] = None,
        version_status: Optional[str] = None,
    ) -> int:
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")
        m = self.mapper
        version_source = m.source("version")
        status_field = self.adapter.status_field
        version_project_field = m.tracker_field("version", "project")
        version_data = self._sg.find_one(
            version_source,
            filters=[["id", "is", version_id]],
            fields=[version_project_field],
        )
        if not version_data:
            raise ValueError(f"Version {version_id} not found")
        project = version_data.get(version_project_field)
        if not project:
            raise ValueError(f"Version {version_id} has no project assigned")

        project_field = m.tracker_field("note", "project")
        subject_field = m.tracker_field("note", "subject")
        content_field = m.tracker_field("note", "content")
        links_field = m.tracker_field("note", "note_links")
        version_link = {"type": version_source, "id": version_id}

        # Duplicate check
        existing = self._sg.find_one(
            m.source("note"),
            filters=[
                [project_field, "is", project],
                [links_field, "is", version_link],
                [subject_field, "is", subject],
                [content_field, "is", content],
            ],
            fields=[m.tracker_field("note", "id")],
        )
        if existing:
            if version_status:
                self._sg.update(version_source, version_id, {status_field: version_status})
            return existing["id"]

        note_links = [version_link]
        if links:
            extra = self._convert_entities_to_sg_links(links)
            if isinstance(extra, dict):
                note_links.append(extra)
            elif isinstance(extra, list):
                note_links.extend(extra)

        user_source = m.source("user")
        note_data = {
            project_field: project,
            subject_field: subject,
            content_field: content,
            links_field: note_links,
            self.adapter.note.to_field: [
                {"type": user_source, "id": uid} for uid in to_users
            ],
            self.adapter.note.cc_field: [
                {"type": user_source, "id": uid} for uid in cc_users
            ],
        }

        # Attribute the note to its actual author, which is not necessarily the
        # user making the request — a supervisor may publish a playlist
        # containing notes written by several artists.  Opening a nested sudo
        # for the author makes ShotGrid record the correct one.  With no
        # author_email, the note is created on the current connection, which is
        # already impersonating the requesting user.
        author_login = None
        if author_email:
            try:
                author_user = self.get_user_by_email(author_email)
                author_login = author_user.login if author_user else None
            except ValueError as e:
                raise UserNotFoundError(
                    f"Author not found in ShotGrid: {author_email}"
                ) from e
        if author_login:
            with self.sudo(author_login):
                result = self._sg.create(m.source("note"), note_data)
        else:
            result = self._sg.create(m.source("note"), note_data)

        if version_status:
            self._sg.update(version_source, version_id, {status_field: version_status})
        return result["id"]

    def attach_file_to_note(
        self, note_id: int, file_path: str, display_name: str
    ) -> bool:
        if not self._sg:
            return False
        try:
            self._sg.upload(
                self.mapper.source("note"),
                note_id,
                file_path,
                field_name=self.adapter.note.attachments_field,
                display_name=display_name,
            )
            return True
        except Exception:
            return False

    def transcript_entity_type(self, project_id: Any = None) -> str:
        return _transcript_entity_type(project_id)

    # ── handshake ─────────────────────────────────────────────────────── #

    @classmethod
    def for_handshake(cls, prodtrack_config: Any, profile: ConnectorProfile) -> "ShotgridProvider":
        sg = prodtrack_config.shotgrid
        return cls(
            url=sg.url,
            script_name=sg.script_name,
            api_key=sg.api_key,
            sudo_user=sg.sudo_user,
            profile=profile,
        )

    def check_connection(self) -> dict[str, Any]:
        if not self.sg:
            raise ValueError("Not connected to ShotGrid")
        # A real query proves the script name and key, which info() alone does not.
        self.sg.find_one(self.mapper.source("user"), [], ["id"])
        info: dict[str, Any] = {"site": self.url}
        try:
            version = self.sg.info().get("version")
            if version:
                info["server_version"] = ".".join(str(v) for v in version)
        except Exception:  # info() is advisory
            pass
        return info

    def discover_schema(self) -> "TrackerSchema":
        from dna.prodtrack_providers.handshake import TrackerSchema

        if not self.sg:
            raise ValueError("Not connected to ShotGrid")
        known = set(self.sg.schema_entity_read())
        sources = {b.source for b in self.profile.entities.values()}
        sources.add(self.transcript_entity_type())
        entities = {
            source: set(self.sg.schema_field_read(source))
            for source in sorted(sources)
            if source in known
        }
        try:
            statuses = [s["code"] for s in self.get_version_statuses()]
        except Exception:
            statuses = []
        return TrackerSchema(
            version="ShotGrid", entities=entities, known_entities=known, statuses=statuses
        )

    def adapter_issues(self, schema: "TrackerSchema") -> "list[Check]":
        from dna.prodtrack_providers.handshake import Check

        checks: list[Check] = []
        version_source = self.mapper.source("version")
        version_fields = schema.entities.get(version_source)
        if version_fields is not None:
            ok = self.adapter.status_field in version_fields
            checks.append(Check(
                "adapter.status_field",
                "ok" if ok else "fail",
                f"status field '{self.adapter.status_field}' "
                + ("exists on " if ok else "is missing from ") + version_source,
            ))
        note_source = self.mapper.source("note")
        note_fields = schema.entities.get(note_source)
        if note_fields is not None:
            wanted = [self.adapter.note.to_field, self.adapter.note.cc_field, self.adapter.note.attachments_field]
            missing = [f for f in wanted if f not in note_fields]
            checks.append(Check(
                "adapter.note",
                "fail" if missing else "ok",
                f"note fields missing from {note_source}: {missing}" if missing else f"note recipient and attachment fields exist on {note_source}",
            ))
        transcript_entity = self.transcript_entity_type()
        if schema.known_entities and transcript_entity not in schema.known_entities:
            checks.append(Check(
                "adapter.transcript", "warn",
                f"transcript entity '{transcript_entity}' does not exist; transcript publishing will fail until it is enabled",
            ))
        elif transcript_entity in schema.entities:
            fields = schema.entities[transcript_entity]
            t = self.adapter.transcript
            missing = [f for f in (t.code, t.project, t.playlist, t.version, t.meeting_id, t.meeting_date, t.platform, t.body) if f not in fields]
            checks.append(Check(
                "adapter.transcript",
                "warn" if missing else "ok",
                f"{transcript_entity} lacks fields {missing}" if missing else f"{transcript_entity} has every transcript field",
            ))
        return checks

    def publish_transcript(
        self,
        *,
        project_id: int,
        playlist_id: int,
        version_id: int,
        meeting_id: str,
        meeting_date: date,
        platform: str,
        body: str,
    ) -> int:
        """Create a transcript row in the configured SG custom entity."""
        if not self._sg:
            raise ValueError("Not connected to ShotGrid")

        entity_type = _transcript_entity_type(project_id)
        f = self.adapter.transcript
        m = self.mapper
        # Human-readable code so the row is identifiable on the SG entity page.
        code = f"transcript-{version_id}-{meeting_date.isoformat()}"
        payload: dict[str, Any] = {
            f.code: code,
            f.project: self._project_link(project_id),
            f.playlist: {"type": m.source("playlist"), "id": playlist_id},
            f.version: {"type": m.source("version"), "id": version_id},
            f.meeting_id: meeting_id,
            f.meeting_date: meeting_date.isoformat(),
            f.platform: platform,
            f.body: body,
        }
        result = self._sg.create(entity_type, payload)
        return result["id"]

    def update_transcript(
        self,
        *,
        entity_type: str,
        entity_id: int,
        body: str,
        meeting_date: date,
    ) -> bool:
        """Patch body + date on an existing transcript; other fields untouched."""
        if not self._sg:
            return False
        f = self.adapter.transcript
        try:
            self._sg.update(
                entity_type,
                entity_id,
                {
                    f.body: body,
                    f.meeting_date: meeting_date.isoformat(),
                },
            )
            return True
        except Exception:
            return False

def _get_dna_entity_type(sg_entity_type: str) -> str:
    for entity_type, entity_data in FIELD_MAPPING.items():
        if entity_data["entity_id"] == sg_entity_type:
            return entity_type
    raise ValueError(f"Unknown entity type: {sg_entity_type}")


def _transcript_entity_type(project_id: Optional[int] = None) -> str:
    """Custom-entity slot that stores transcripts.

    Set site-wide (``SHOTGRID_TRANSCRIPT_ENTITY``) and optionally overridden per
    project, since studios enable a different slot per ShotGrid project.
    """
    cfg = get_config()
    if project_id is not None:
        return cfg.get_for_project(
            "prodtrack", str(project_id)
        ).shotgrid.transcript_entity
    return cfg.get("prodtrack").shotgrid.transcript_entity
