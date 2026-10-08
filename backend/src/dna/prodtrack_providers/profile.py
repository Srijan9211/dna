"""Connector profiles: everything tracker-specific, as data.

A profile tells DNA how one production-tracking system maps onto DNA's concepts.
DNA ships ``profiles/shotgrid.yaml`` and ``profiles/mock.yaml``; a studio can
override any part of a profile (``prodtrack.profile`` in config) or supply a whole
new one for a tracker (``prodtrack.profile_path``).

DNA's own vocabulary is fixed and small.  A *kind* is a DNA entity type; a *role*
groups kinds by what they are for in a review:

    project     the container everything belongs to
    subject     what the work is about (ShotGrid Shot/Asset, AYON folder, ...)
    task        a unit of work on a subject
    reviewable  the thing being reviewed (ShotGrid Version, AYON version, ...)
    collection  an ordered set of reviewables (Playlist, entity list, ...)
    person      a user
    note        feedback attached to a reviewable

The profile binds each kind to a tracker entity (``source``) and names the
tracker fields behind each DNA field.  Names only meaningful to one transport
(``sg_status_list``, ``note_links`` ...) live in the free-form ``adapter`` block,
whose schema the transport adapter owns.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import ValidationError, field_validator, model_validator

from dna.configsys.layers import deep_merge
from dna.configsys.meta import Section

CONTRACT = "dna.prodtrack/1"

# DNA entity kinds (keys of ``dna.models.entity.ENTITY_MODELS``; a test keeps
# the two in sync).  Listed here so this module needs no model imports.
KINDS = ("project", "shot", "asset", "note", "task", "version", "playlist", "user")
ROLES = ("project", "subject", "task", "reviewable", "collection", "person", "note")

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

TranscriptStrategy = Literal["tracker_entity", "note_attachment", "dna_only"]
Impersonation = Literal["sudo_header", "session_user", "service_only", "none"]


class Capabilities(Section):
    """What the connector can do.  DNA disables a feature rather than failing."""

    playlists: bool = True
    create_playlist: bool = True
    add_to_playlist: bool = True
    notes: bool = True
    note_attachments: bool = True
    note_addressing: bool = True  # to / cc recipients
    note_mentions: bool = False
    version_status_write: bool = True
    author_attribution: bool = True  # a note can be created as another user
    transcript_storage: list[TranscriptStrategy] = ["dna_only"]
    impersonation: Impersonation = "none"


# Field and entity names are put into the tracker's queries (a GraphQL selection, a
# ShotGrid field list), so they must be plain dotted identifiers: nothing a profile
# override can contain may change the shape of a query.
_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _check_paths(kind: str, names: Any) -> None:
    for name in names:
        if not isinstance(name, str) or not _PATH.match(name):
            raise ValueError(
                f"{kind} '{name}' is not a plain field name "
                "(letters, digits and underscores, joined by dots)"
            )


class EntityBinding(Section):
    """One DNA kind bound to a tracker entity."""

    source: str  # tracker entity type: "Shot", "Version", "folder", ...
    fields: dict[str, str] = {}  # tracker field -> DNA field
    links: dict[str, str] = {}  # tracker link field -> DNA field
    # Fixed restriction on ``source`` (AYON: {"folderType": ["Shot"]}) so several
    # kinds can share one tracker entity.
    where: dict[str, Any] = {}

    @model_validator(mode="after")
    def _names_are_plain(self) -> "EntityBinding":
        _check_paths("entity type", [self.source])
        _check_paths("field", self.fields)
        _check_paths("DNA field", self.fields.values())
        _check_paths("link field", self.links)
        _check_paths("DNA field", self.links.values())
        _check_paths("filter field", self.where)
        return self


class Urls(Section):
    """Web-UI links.  Variables: ``{base}`` (tracker URL), ``{id}``, ``{type}``
    (the tracker's own entity type name), plus any a connector supplies (AYON
    adds ``{project}``)."""

    version_detail: Optional[str] = None
    entity_detail: Optional[str] = None


class ErrorRules(Section):
    """Lower-case substrings of a tracker error message that mean "forbidden"."""

    permission_markers: list[str] = []


class ConnectorProfile(Section):
    contract: Literal["dna.prodtrack/1"] = CONTRACT
    connector: str
    display_name: str = ""
    # Tracker vocabulary shown to users, by DNA kind ("collection": "Playlist").
    labels: dict[str, str] = {}
    capabilities: Capabilities = Capabilities()
    entities: dict[str, EntityBinding] = {}
    roles: dict[str, list[str]] = {}
    urls: Urls = Urls()
    errors: ErrorRules = ErrorRules()
    adapter: dict[str, Any] = {}

    @field_validator("entities")
    @classmethod
    def _known_kinds(cls, v: dict[str, EntityBinding]) -> dict[str, EntityBinding]:
        unknown = sorted(set(v) - set(KINDS))
        if unknown:
            raise ValueError(
                f"unknown entity kind(s) {unknown}; valid kinds: {list(KINDS)}"
            )
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "ConnectorProfile":
        for role, kinds in self.roles.items():
            if role not in ROLES:
                raise ValueError(f"unknown role '{role}'; valid roles: {list(ROLES)}")
            missing = [k for k in kinds if k not in self.entities]
            if missing:
                raise ValueError(
                    f"role '{role}' lists kinds with no binding: {missing}"
                )
        by_source: dict[str, list[str]] = {}
        for kind, binding in self.entities.items():
            by_source.setdefault(binding.source, []).append(kind)
        for source, kinds in by_source.items():
            if len(kinds) > 1 and not all(self.entities[k].where for k in kinds):
                raise ValueError(
                    f"kinds {kinds} share the tracker entity '{source}'; each needs a "
                    "'where' filter so they can be told apart"
                )
        return self

    # ── helpers ───────────────────────────────────────────────────────────

    def field_mapping(self) -> dict[str, dict[str, Any]]:
        """The legacy ``FIELD_MAPPING`` shape, for code that still expects it."""
        return {
            kind: {
                "entity_id": b.source,
                "fields": dict(b.fields),
                "linked_fields": dict(b.links),
            }
            for kind, b in self.entities.items()
        }

    def role_of(self, kind: str) -> Optional[str]:
        for role, kinds in self.roles.items():
            if kind in kinds:
                return role
        return None


# ── loading ──────────────────────────────────────────────────────────────────


def packaged_profile_path(connector: str) -> Path:
    return PROFILES_DIR / f"{connector}.yaml"


def has_profile(connector: str, path: Optional[str] = None) -> bool:
    return bool(path) or packaged_profile_path(connector).is_file()


def load_profile(
    connector: str,
    *,
    path: Optional[str] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> ConnectorProfile:
    """Load and validate a connector profile.

    The base is ``path`` if given, else the profile DNA ships for ``connector``;
    ``overrides`` (the studio's ``prodtrack.profile``) is deep-merged on top.

    Raises:
        ValueError: no profile exists, or it does not validate.
    """
    source = Path(path).expanduser() if path else packaged_profile_path(connector)
    if not source.is_file():
        raise ValueError(
            f"No connector profile for '{connector}' (looked for {source}). Set "
            "prodtrack.profile_path, or install a connector that ships one."
        )
    key = json.dumps(overrides or {}, sort_keys=True, default=str)
    return _load_cached(connector, str(source), source.stat().st_mtime_ns, key)


def _prune_nulls(node: Any) -> Any:
    """Drop mapping keys whose value is null.

    An override sets a key to ``null`` to *remove* it from the base profile, which
    plain merging cannot express (renaming a field means deleting the old name).
    """
    if isinstance(node, dict):
        return {k: _prune_nulls(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_prune_nulls(v) for v in node]
    return node


@lru_cache(maxsize=32)
def _load_cached(
    connector: str, source: str, mtime_ns: int, overrides_json: str
) -> ConnectorProfile:
    data = yaml.safe_load(Path(source).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{source}: a connector profile must be a mapping")
    merged = _prune_nulls(deep_merge(data, json.loads(overrides_json)))
    merged.setdefault("connector", connector)
    try:
        return ConnectorProfile.model_validate(merged)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(
            f"Invalid connector profile for '{connector}': {problems}"
        ) from exc
