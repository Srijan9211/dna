"""The prodtrack handshake: does this configuration actually work with this tracker?

A studio configures DNA for its tracker by supplying connection settings and, where
its tracker differs from the defaults, a connector profile.  The handshake turns
"I hope that's right" into a checked report, without changing anything:

1. **describe**   load and validate the profile; list what the connector supports
2. **connect**    reach the tracker and authenticate (and, where supported, prove the
                  impersonation mechanism works)
3. **discover**   read the tracker's real schema (entities, fields, folder types ...)
4. **validate**   check every mapping in the profile against that schema
5. **suggest**    for each mismatch, offer the closest names the tracker does have
6. **smoke**      optionally walk projects, playlists and versions, read-only

The result is a structured report (a checklist an admin UI renders, or a CLI prints).
Nothing here writes to the tracker; applying a configuration is a separate, explicit
step taken only when the report has no failures.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional

from dna.prodtrack_providers.profile import ConnectorProfile

logger = logging.getLogger(__name__)

Status = Literal["ok", "warn", "fail", "skip"]
_SEVERITY = {"ok": 0, "skip": 0, "warn": 1, "fail": 2}


@dataclass
class TrackerSchema:
    """What a tracker says it has.  Transport-neutral, filled in by the adapter."""

    version: str = ""
    # Tracker entity type -> the field names it has.  Only entities the profile
    # uses need appear.  Dotted names (``attrib.description``) are allowed.
    entities: dict[str, set[str]] = field(default_factory=dict)
    # Every entity type the tracker has, even those not listed above.
    known_entities: set[str] = field(default_factory=set)
    folder_types: set[str] = field(default_factory=set)
    task_types: set[str] = field(default_factory=set)
    statuses: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # caveats worth showing the admin

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "entities": sorted(self.known_entities or self.entities),
            "folder_types": sorted(self.folder_types),
            "task_types": sorted(self.task_types),
            "statuses": self.statuses,
            "notes": self.notes,
        }


@dataclass
class Check:
    name: str
    status: Status
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Suggestion:
    """A concrete fix for a mismatch, as a profile override the admin can accept."""

    path: str  # where in the profile, e.g. entities.version.fields
    problem: str
    options: list[str]
    override: Optional[dict[str, Any]] = None


@dataclass
class HandshakeReport:
    connector: str
    contract: str = ""
    checks: list[Check] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)
    schema: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """No check failed.  Warnings do not block applying a configuration."""
        return all(c.status != "fail" for c in self.checks)

    def add(self, name: str, status: Status, message: str, **details: Any) -> Check:
        check = Check(name, status, message, details)
        self.checks.append(check)
        return check

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "contract": self.contract,
            "ok": self.ok,
            "checks": [c.__dict__ for c in self.checks],
            "suggestions": [s.__dict__ for s in self.suggestions],
            "capabilities": self.capabilities,
            "schema": self.schema,
        }


# ── validating a profile against a schema ────────────────────────────────────


def _close(name: str, candidates: Iterable[str], n: int = 3) -> list[str]:
    return difflib.get_close_matches(name, sorted(candidates), n=n, cutoff=0.55)


def validate_profile(profile: ConnectorProfile, schema: TrackerSchema) -> tuple[list[Check], list[Suggestion]]:
    """Check every entity binding in ``profile`` against what the tracker has.

    A mapping to a field the tracker does not have fails: DNA would request it and
    the tracker would reject the query.  Where the tracker did not report an entity
    at all the check is skipped (a connector that cannot introspect is not blamed).
    """
    checks: list[Check] = []
    suggestions: list[Suggestion] = []

    for kind, binding in profile.entities.items():
        label = f"mapping.{kind}"
        source = binding.source
        if schema.known_entities and source not in schema.known_entities:
            options = _close(source, schema.known_entities)
            checks.append(Check(label, "fail", f"{kind}: the tracker has no entity '{source}'", {"source": source, "closest": options}))
            if options:
                suggestions.append(Suggestion(f"entities.{kind}.source", f"no entity '{source}'", options,
                                              {"entities": {kind: {"source": options[0]}}}))
            continue
        available = schema.entities.get(source)
        if available is None:
            checks.append(Check(label, "skip", f"{kind}: fields of '{source}' not reported by the tracker"))
            continue

        problems: list[str] = []
        for group, names in (("fields", binding.fields), ("links", binding.links)):
            for tracker_field in names:
                if tracker_field in available:
                    continue
                options = _close(tracker_field, available)
                problems.append(f"{group[:-1]} '{tracker_field}'")
                dna_name = names[tracker_field]
                override = None
                if options:
                    override = {"entities": {kind: {group: {tracker_field: None, options[0]: dna_name}}}}
                suggestions.append(Suggestion(
                    f"entities.{kind}.{group}", f"'{source}' has no field '{tracker_field}' (maps to DNA '{dna_name}')",
                    options, override,
                ))
        wanted_types = binding.where.get("folderType") or []
        if wanted_types and schema.folder_types:
            missing = [t for t in wanted_types if t not in schema.folder_types]
            for folder_type in missing:
                options = _close(folder_type, schema.folder_types)
                problems.append(f"folder type '{folder_type}'")
                override = None
                if options:  # a list is replaced whole, so send the corrected list
                    fixed = [t if t in schema.folder_types else options[0] for t in wanted_types]
                    override = {"entities": {kind: {"where": {"folderType": fixed}}}}
                suggestions.append(Suggestion(f"entities.{kind}.where.folderType",
                                              f"no folder type '{folder_type}'",
                                              options or sorted(schema.folder_types), override))
        if problems:
            checks.append(Check(label, "fail", f"{kind}: not found in the tracker: {', '.join(problems)}", {"problems": problems}))
        else:
            checks.append(Check(label, "ok", f"{kind}: '{source}' and its mapped fields exist"))
    return checks, suggestions


# ── the runner ───────────────────────────────────────────────────────────────

ProviderFactory = Callable[[ConnectorProfile], Any]


def run_handshake(
    connector: str,
    profile: ConnectorProfile,
    make_provider: ProviderFactory,
    *,
    smoke_email: Optional[str] = None,
) -> HandshakeReport:
    """Run the handshake for one connector.

    Args:
        profile: the (candidate) profile to check.
        make_provider: builds a provider for ``profile`` from the (candidate)
            connection settings.  Called once; raising means the settings are
            incomplete and is reported, not propagated.
        smoke_email: when given, also walks that user's projects, playlists and
            versions (read-only).
    """
    report = HandshakeReport(connector=connector, contract=profile.contract)
    report.add("describe", "ok", f"{profile.display_name or connector} profile is valid",
               roles=profile.roles, labels=profile.labels)
    report.capabilities = profile.capabilities.model_dump()

    try:
        provider = make_provider(profile)
    except Exception as exc:  # incomplete connection settings
        report.add("connect", "fail", f"cannot build the connector: {_clean(exc)}")
        return report

    # connect
    try:
        info = provider.check_connection()
    except NotImplementedError:
        report.add("connect", "skip", "this connector cannot test its connection")
    except Exception as exc:
        report.add("connect", "fail", f"cannot reach or authenticate with the tracker: {_clean(exc)}")
        return report
    else:
        report.add("connect", "ok", "connected and authenticated", **(info or {}))

    # discover
    schema: Optional[TrackerSchema] = None
    try:
        schema = provider.discover_schema()
    except NotImplementedError:
        report.add("discover", "skip", "this connector cannot read its tracker's schema")
    except Exception as exc:
        report.add("discover", "fail", f"could not read the tracker's schema: {_clean(exc)}")
    else:
        report.schema = schema.summary()
        report.add("discover", "ok", f"read the schema of {schema.version or 'the tracker'}")

    # validate + suggest
    if schema is not None:
        checks, suggestions = validate_profile(profile, schema)
        report.checks.extend(checks)
        report.suggestions.extend(suggestions)
        for issue in provider.adapter_issues(schema):
            report.checks.append(issue)
        for note in schema.notes:
            report.add("note", "warn", note)

    # smoke
    if smoke_email:
        report.checks.append(_smoke(provider, smoke_email))
    return report


def _smoke(provider: Any, email: str) -> Check:
    """Read-only walk: user -> projects -> playlists -> versions."""
    try:
        provider.get_user_by_email(email)
        projects = provider.get_projects_for_user(email)
        playlists = versions = 0
        for project in projects:
            for playlist in provider.get_playlists_for_project(project.id):
                playlists += 1
                versions += len(provider.get_versions_for_playlist(playlist.id))
                if versions:  # one populated playlist proves the whole path
                    break
            if versions:
                break
    except Exception as exc:
        return Check("smoke", "fail", f"the read path failed for {email}: {_clean(exc)}")
    if not projects:
        return Check("smoke", "warn", f"{email} has no projects")
    if not versions:
        return Check("smoke", "warn", f"{email} sees {len(projects)} project(s) but no playlist with versions yet",
                     {"projects": len(projects), "playlists": playlists})
    return Check("smoke", "ok", f"read {versions} version(s) through the full path",
                 {"projects": len(projects), "playlists": playlists, "versions": versions})


def _clean(exc: Exception) -> str:
    """An error message safe to show an admin: one line, bounded, and never a secret
    (clients raise domain errors that name the problem, not the credential)."""
    return " ".join(str(exc).split())[:300] or type(exc).__name__


def check_candidate(
    candidate: Optional[dict[str, Any]] = None, *, smoke_email: Optional[str] = None
) -> HandshakeReport:
    """Handshake the configuration as it would be with ``candidate`` applied.

    The candidate is held to the same rules as a real change (secrets only as
    references, locked sections refused) and is *not* applied.  Used by both the
    HTTP API and the CLI so they cannot disagree.

    Raises:
        ConfigError: the candidate is not a valid configuration.
        ValueError: the connector is unknown or has no valid profile.
    """
    from dna import configsys
    from dna.configsys import ConfigService, providers
    from dna.configsys.layers import deep_merge
    from dna.prodtrack_providers.profile import load_profile

    candidate = candidate or {}
    service = configsys.get_config()
    service.apply_overrides(candidate, dry_run=True)
    merged = ConfigService(
        runtime_overrides=deep_merge(service.runtime_overrides, candidate)
    ).load()
    pt = merged.get("prodtrack")
    profile = load_profile(pt.connector, path=pt.profile_path, overrides=pt.profile)
    provider_cls = providers.prodtrack.resolve(pt.connector)
    return run_handshake(
        pt.connector, profile, lambda p: provider_cls.for_handshake(pt, p), smoke_email=smoke_email
    )
