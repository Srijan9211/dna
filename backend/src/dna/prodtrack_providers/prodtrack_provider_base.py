from __future__ import annotations

import os
from datetime import date
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from dna.media_proxy import MediaStream
    from dna.models.entity import EntityBase, Playlist, Project, User, Version
    from dna.prodtrack_providers.handshake import Check, TrackerSchema
    from dna.prodtrack_providers.profile import Capabilities, ConnectorProfile


class UserNotFoundError(Exception):
    """Raised when a user is not found in the production tracking system."""

    pass


class ProdtrackPermissionError(Exception):
    """Raised when the production tracker denies access to a resource.

    The caller is authenticated, but their permission group in the tracker does
    not grant access to what they asked for.  Surfaces as HTTP 403.
    """


class ProdtrackAuthError(Exception):
    """Raised when the production tracker rejects the impersonated identity.

    Typically means the user was deactivated or their access was revoked while
    their DNA session was still live.  Surfaces as HTTP 401 so the client
    clears its token and re-authenticates.
    """


class ProdtrackUnavailableError(Exception):
    """Raised when the production tracker errors or cannot be reached.

    Nothing is wrong with the request itself.  Surfaces as HTTP 503.
    """


class ProdtrackProviderBase:
    # True when the connector can act as the signed-in user (ShotGrid's
    # ``sudo_as_login``).  The factory only passes an identity to such providers.
    SUPPORTS_IMPERSONATION = False

    # The connector profile this provider was built from (None for a provider
    # that predates profiles).
    profile: Optional["ConnectorProfile"] = None

    def __init__(self):
        pass

    # True when a version's ``thumbnail`` / ``movie_path`` need credentials a browser
    # cannot send.  DNA then serves them through its signed-URL media proxy.
    MEDIA_REQUIRES_PROXY = False

    def open_media(
        self, version_id: Any, kind: str, range_header: "Optional[str]" = None
    ) -> "MediaStream":
        """Open a version's ``thumbnail`` or ``movie`` for streaming to the browser.

        Only connectors with ``MEDIA_REQUIRES_PROXY`` implement this.  ``range_header``
        is the browser's ``Range`` so video can seek.  Raises ``ValueError`` if the
        version has no such media.
        """
        raise NotImplementedError("This connector's media is served directly.")

    # ── handshake hooks (see dna.prodtrack_providers.handshake) ───────────

    @classmethod
    def for_handshake(
        cls, prodtrack_config: Any, profile: "ConnectorProfile"
    ) -> "ProdtrackProviderBase":
        """Build a provider for a *candidate* configuration, without touching the
        process-wide config.  ``prodtrack_config`` is a ``ProdtrackSection``."""
        return cls()

    def check_connection(self) -> dict[str, Any]:
        """Reach the tracker and authenticate; return facts worth showing an admin
        (server version, the account used).  Raise on failure.  Never write."""
        raise NotImplementedError("This connector cannot test its connection.")

    def discover_schema(self) -> "TrackerSchema":
        """Read the tracker's real schema so the profile can be checked against it."""
        raise NotImplementedError("This connector cannot read its tracker's schema.")

    def adapter_issues(self, schema: "TrackerSchema") -> "list[Check]":
        """Problems in the adapter's own settings (names the core profile does not
        model) given the discovered schema."""
        return []

    def capabilities(self) -> "Capabilities":
        """What this connector can do, from its profile.

        DNA consults this to disable a feature (note attachments, to/cc
        addressing, playlist creation ...) a connector cannot support, rather
        than letting the call fail.  Without a profile every feature is assumed.
        """
        from dna.prodtrack_providers.profile import Capabilities

        return self.profile.capabilities if self.profile is not None else Capabilities()

    @staticmethod
    def build_version_context(version: Version) -> str:
        """Format a Version entity as plain text for LLM prompts."""
        parts: list[str] = []
        if version.name:
            parts.append(f"Version: {version.name}")
        if version.entity:
            entity_type = version.entity.__class__.__name__
            parts.append(f"{entity_type}: {version.entity.name}")
        if version.task:
            if version.task.name:
                parts.append(f"Task: {version.task.name}")
            if version.task.pipeline_step and version.task.pipeline_step.get("name"):
                parts.append(f"Department: {version.task.pipeline_step['name']}")
        if version.status:
            parts.append(f"Status: {version.status}")
        if version.description:
            parts.append(f"Description: {version.description}")
        return "\n".join(parts) if parts else "No version context available."

    def _get_object_type(self, object_type: str) -> type["EntityBase"]:
        """Get the model class from the entity type string."""
        from dna.models.entity import ENTITY_MODELS, EntityBase

        return ENTITY_MODELS.get(object_type, EntityBase)

    def get_entity(
        self, entity_type: str, entity_id: int, resolve_links: bool = True
    ) -> "EntityBase":
        """Get an entity by its ID.

        Args:
            entity_type: The type of entity to fetch
            entity_id: The ID of the entity
            resolve_links: If True, recursively fetch linked entities.
                If False, only include shallow links with id/name.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def add_entity(self, entity_type: str, entity: "EntityBase") -> "EntityBase":
        """Add an entity to the production tracking system."""
        raise NotImplementedError("Subclasses must implement this method.")

    def find(
        self, entity_type: str, filters: list[dict[str, Any]], limit: int = 0
    ) -> list["EntityBase"]:
        """Find entities matching the given filters.

        Args:
            entity_type: The DNA entity type to search for (e.g., 'shot', 'version')
            filters: List of filter conditions in DNA format
            limit: Maximum number of entities to return. Defaults to 0 (no limit).

        Returns:
            List of matching entities
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def search(
        self,
        query: str,
        entity_types: list[str],
        project_id: int | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for entities across multiple entity types.

        Args:
            query: Text to search for (searches name field)
            entity_types: List of entity types to search (e.g., ['user', 'shot', 'asset'])
            project_id: Optional project ID to scope non-user entities
            limit: Maximum results per entity type

        Returns:
            List of lightweight entity representations with type, id, name, and
            type-specific fields (email for users, description for shots/assets/versions)
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def get_user_by_email(self, user_email: str) -> "User":
        """Get a user by their email address.

        Args:
            user_email: The email address of the user

        Returns:
            User entity with name, email, and login

        Raises:
            ValueError: If user is not found
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def get_projects_for_user(self, user_email: str) -> list["Project"]:
        """Get projects accessible by a user.

        Args:
            user_email: The email address of the user

        Returns:
            List of Project entities the user has access to
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def get_playlists_for_project(self, project_id: int) -> list["Playlist"]:
        """Get playlists for a project.

        Args:
            project_id: The ID of the project

        Returns:
            List of Playlist entities for the project
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def create_playlist(self, project_id: int, name: str) -> "Playlist":
        """Create a new playlist in the production tracking system.

        Args:
            project_id: The ID of the project the playlist belongs to
            name: The playlist name/code

        Returns:
            The created Playlist entity
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def get_versions_for_playlist(self, playlist_id: int) -> list["Version"]:
        """Get versions for a playlist.

        Args:
            playlist_id: The ID of the playlist

        Returns:
            List of Version entities in the playlist
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def add_version_to_playlist(self, playlist_id: int, version_id: int) -> bool:
        """Add an existing version to a playlist.

        Args:
            playlist_id: The ID of the playlist
            version_id: The ID of the version to add

        Returns:
            True on success (including when the version was already present)
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def get_version_statuses(
        self, project_id: int | None = None
    ) -> list[dict[str, str]]:
        """Get valid status values for Versions.

        Args:
            project_id: Optional project ID to scope status values

        Returns:
            List of status dicts with 'code' and 'name' keys
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def publish_note(
        self,
        version_id: int,
        content: str,
        subject: str,
        to_users: list[int],
        cc_users: list[int],
        links: list["EntityBase"],
        author_email: str | None = None,
        version_status: str | None = None,
    ) -> int:
        """Publish a note to the production tracking system.

        Args:
            version_id: The ID of the version (or other entity) to link to
            content: Note content
            subject: Note subject
            to_users: List of user IDs to address
            cc_users: List of user IDs to CC
            links: List of additional entities to link
            author_email: Optional email of the author. If provided, the note
                should be created on behalf of this user.
            version_status: Optional status code to set on the version.

        Returns:
            The ID of the created note
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def update_version_status(self, version_id: int, status: str) -> bool:
        """Update the status of a version without publishing a note.

        Args:
            version_id: The ID of the version to update
            status: The status code to set

        Returns:
            True if the update succeeded, False otherwise
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def attach_file_to_note(
        self, note_id: int, file_path: str, display_name: str
    ) -> bool:
        """Upload a local file as an attachment on an existing note.

        Args:
            note_id: The ID of the note to attach the file to
            file_path: Absolute path to the local file
            display_name: Filename to display in the tracking system

        Returns:
            True if upload succeeded, False otherwise
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def transcript_entity_type(self, project_id: Any = None) -> str:
        """Name of the tracker entity a published transcript is stored in.

        DNA records it next to the transcript's id so a later update targets the
        same entity even if the studio moves to a different slot.
        """
        return "transcript"

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
        """Create a transcript row in the production tracking system.

        Returns the entity ID of the newly-created row.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def update_transcript(
        self,
        *,
        entity_type: str,
        entity_id: int,
        body: str,
        meeting_date: date,
    ) -> bool:
        """Update body + meeting_date on an existing transcript entity.

        `entity_type` must come from the caller's bookkeeping (whichever
        custom-entity slot the row was originally created in). Reading the
        current env var here would misfire if studios migrate between slots.

        Only body and meeting_date are touched on purpose; summary and other
        fields are left alone so manual edits on the tracking-system side
        survive a re-publish.
        """
        raise NotImplementedError("Subclasses must implement this method.")


def get_prodtrack_provider(
    sudo_login: Optional[str] = None,
    session_id: Optional[str] = None,
) -> ProdtrackProviderBase:
    """Get the production tracking provider.

    Args:
        sudo_login:  The authenticated user's ShotGrid login name.  When set,
                     the provider connects with the script account and
                     ``sudo_as_login=<sudo_login>``, so ShotGrid enforces that
                     user's own permission group natively.  The caller resolves
                     this from the server-side session store — it is never taken
                     from client input.
        session_id:  The user's DNA session ID.  Carried for diagnostics only.

    Warning:
        Omitting ``sudo_login`` returns a provider bound to the bare script
        account, which has full site permissions.  That path exists only for
        unauthenticated contexts — background jobs and ``AUTH_PROVIDER=none``.
        Authenticated request paths must always supply ``sudo_login``;
        ``get_user_scoped_prodtrack_provider`` in main.py enforces this by
        failing closed with 401 when the session carries no login name.
    """
    from dna.configsys import get_config, providers

    cfg = get_config().get("prodtrack")
    provider_cls = providers.prodtrack.resolve(cfg.connector)

    if cfg.connector == "shotgrid":
        sg = cfg.shotgrid
        if not all([sg.url, sg.script_name, sg.api_key]):
            raise ValueError(
                "ShotGrid credentials not provided. Set SHOTGRID_URL, "
                "SHOTGRID_SCRIPT_NAME, and SHOTGRID_API_KEY, or use "
                "PRODTRACK_PROVIDER=mock for the mock provider."
            )

    if sudo_login and getattr(provider_cls, "SUPPORTS_IMPERSONATION", False):
        return provider_cls(sudo_user=sudo_login, session_id=session_id)

    # Script/service account: unauthenticated contexts only (background jobs,
    # dev mode), or a connector with no impersonation support.
    return provider_cls()
