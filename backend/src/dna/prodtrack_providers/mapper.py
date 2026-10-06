"""The generic half of a connector: profile-driven mapping.

A transport adapter (ShotGrid REST, AYON GraphQL, ...) knows how to *talk* to a
tracker.  :class:`ProfileMapper` knows what the tracker's fields *mean*, from the
connector profile, so no adapter hard-codes a field name that a studio might have
renamed.  Everything here is transport-neutral.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from dna.prodtrack_providers.profile import ConnectorProfile, EntityBinding

LinkResolver = Callable[[Any], Any]


def get_path(record: Any, path: str) -> Any:
    """Read ``path`` from a raw tracker record.

    A literal key wins (ShotGrid has real keys containing dots, such as
    ``created_by.HumanUser.email``); otherwise a dotted path is walked through
    nested mappings (AYON's ``attrib.description``).
    """
    if not isinstance(record, dict):
        return None
    if path in record:
        return record[path]
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class ProfileMapper:
    def __init__(self, profile: ConnectorProfile):
        self.profile = profile

    # ── kinds <-> tracker entities ────────────────────────────────────────

    def binding(self, kind: str, *, message: str = "Unknown entity type: {kind}") -> EntityBinding:
        found = self.profile.entities.get(kind)
        if found is None:
            raise ValueError(message.format(kind=kind))
        return found

    def has(self, kind: str) -> bool:
        return kind in self.profile.entities

    def source(self, kind: str) -> str:
        """The tracker's own entity type for a DNA kind (``version`` -> ``Version``)."""
        return self.binding(kind).source

    def kind_for_source(self, source: str) -> str:
        """The DNA kind bound to a tracker entity type."""
        matches = [k for k, b in self.profile.entities.items() if b.source == source]
        if not matches:
            raise ValueError(f"Unknown entity type: {source}")
        if len(matches) > 1:
            raise ValueError(
                f"Tracker entity '{source}' maps to several DNA kinds {matches}; "
                "it cannot be resolved without inspecting the record"
            )
        return matches[0]

    @staticmethod
    def kind_of(entity: Any) -> str:
        """DNA kind of a DNA entity instance (``Version`` -> ``version``)."""
        return entity.__class__.__name__.lower()

    # ── fields ────────────────────────────────────────────────────────────

    def tracker_field(self, kind: str, dna_field: str) -> str:
        """Tracker field behind a DNA field, whether plain or a link."""
        found = self.tracker_field_or_none(kind, dna_field)
        if found is None:
            raise ValueError(f"No tracker field for '{kind}.{dna_field}' in the connector profile")
        return found

    def tracker_field_or_none(self, kind: str, dna_field: str) -> Optional[str]:
        binding = self.binding(kind)
        for tracker, dna in binding.fields.items():
            if dna == dna_field:
                return tracker
        for tracker, dna in binding.links.items():
            if dna == dna_field:
                return tracker
        return None

    def read_fields(self, kind: str) -> list[str]:
        """Every tracker field to request for ``kind`` (plain fields, then links)."""
        binding = self.binding(kind)
        return [*binding.fields, *binding.links]

    def dna_to_tracker(self, kind: str, *, message: str = "Unknown entity type: {kind}") -> dict[str, str]:
        binding = self.binding(kind, message=message)
        out = {dna: tracker for tracker, dna in binding.fields.items()}
        out.update({dna: tracker for tracker, dna in binding.links.items()})
        return out

    # ── conversion ────────────────────────────────────────────────────────

    def entity_data(
        self, kind: str, raw: dict[str, Any], resolve_link: LinkResolver
    ) -> dict[str, Any]:
        """Turn a raw tracker record into the keyword data for a DNA model."""
        binding = self.binding(kind, message="No field mapping for entity type: {kind}")
        data: dict[str, Any] = {}
        for tracker, dna in binding.fields.items():
            data[dna] = get_path(raw, tracker)
        for tracker, dna in binding.links.items():
            data[dna] = resolve_link(get_path(raw, tracker))
        return data

    def to_tracker_filters(
        self, kind: str, filters: list[dict[str, Any]]
    ) -> list[list[Any]]:
        """Translate DNA filters (``{field, operator, value}``) to tracker filters."""
        dna_to_tracker = self.dna_to_tracker(kind, message="Unsupported entity type: {kind}")
        out: list[list[Any]] = []
        for f in filters:
            tracker = dna_to_tracker.get(f.get("field"))
            if tracker is None:
                raise ValueError(f"Unknown field '{f.get('field')}' for '{kind}'")
            out.append([tracker, f.get("operator"), f.get("value")])
        return out

    # ── urls ──────────────────────────────────────────────────────────────

    def url(
        self, name: str, *, base: str, kind: str, entity_id: Any, **extra: Any
    ) -> Optional[str]:
        """Render a web-UI URL template, or None if the profile defines none.

        ``extra`` supplies connector-specific variables (AYON's ``{project}``).
        """
        template = getattr(self.profile.urls, name, None)
        base = (base or "").rstrip("/")
        if not template or not base:
            return None
        return template.format(
            base=base, type=self.source(kind), id=entity_id, **extra
        )
