"""The configuration admin API.

``/config/public`` is open: it is what the frontend reads at boot instead of baked-in
build variables.  Everything else needs a configuration administrator and is off
unless ``admin.enabled`` is set:

    GET  /config/schema                  JSON Schema for every section
    GET  /config/effective               resolved config + where each value came from
    POST /config/validate                dry-run a change
    PUT  /config/{section}               change a section (recorded, revertible)
    GET  /config/history                 who changed what, when
    POST /config/rollback/{version}      re-apply an older revision as a new one
    GET  /config/export                  the runtime overrides as a document
    POST /config/import                  replace the runtime overrides from one
    POST /config/prodtrack/handshake     check a (candidate) tracker configuration
    GET  /config/projects                settings a project may override + current overrides
    PUT  /config/projects/{project_id}   set overrides for one project
    DELETE /config/projects/{project_id} remove one override (or all of a project's)

Safety rules enforced here, not left to the UI:

* A deployment with authentication turned off is not administrable unless the
  operator sets ``admin.allow_unauthenticated`` (development only).
* The ``auth``, ``storage``, ``server`` and ``admin`` sections can never be changed
  through this API.
* Secret values are accepted only as references (``${env:NAME}``) and are masked in
  every response, so neither reaches a log or a browser.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dna import configsys
from dna.configsys import ConfigError, StaleRevision

logger = logging.getLogger(__name__)


class ValidateRequest(BaseModel):
    patch: dict[str, Any]
    replace: bool = False


class SectionUpdate(BaseModel):
    values: dict[str, Any]
    note: str = ""
    expected_version: Optional[int] = Field(
        default=None,
        description="The revision this change was based on; a 409 means someone saved since.",
    )


class HandshakeRequest(BaseModel):
    candidate: dict[str, Any] = Field(
        default_factory=dict,
        description="Overrides to try (e.g. {'prodtrack': {'ayon': {...}, 'profile': {...}}}); "
        "empty checks the configuration as it is now.",
    )
    smoke_email: Optional[str] = Field(
        default=None,
        description="Also walk this user's projects, playlists and versions.",
    )
    apply: bool = Field(
        default=False, description="Save the candidate if the report has no failures."
    )
    note: str = ""


class ProjectUpdate(BaseModel):
    values: dict[str, Any] = Field(
        description="Overrides by section, e.g. {'features': {'transcript_publish': true}}."
    )
    note: str = ""
    expected_version: Optional[int] = None


# A project id is whatever the tracker calls it: an integer, or a name such as an AYON
# project.  Keep it to characters that cannot be mistaken for config syntax.
_PROJECT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _check_project_id(project_id: str) -> str:
    if not _PROJECT_ID.match(project_id):
        raise HTTPException(status_code=422, detail="Invalid project id")
    return project_id


class ImportRequest(BaseModel):
    document: dict[str, Any]


def _issues(exc: ConfigError) -> list[dict[str, str]]:
    return [
        {"section": i.section, "path": i.path, "message": i.message, "source": i.source}
        for i in exc.issues
    ]


def _admin_identity(auth: Any) -> str:
    """Who to check against ``admin.admins``.

    A user signed in through a tracker is identified by their tracker *login*: the
    email DNA shows for them comes from a profile field that may be editable by the
    user, so it must never be what grants administrative rights.  A user with no
    tracker session (Google) is identified by the email Google verified.
    """
    session = getattr(auth, "session", None)
    login = getattr(session, "tracker_login", None) if session is not None else None
    if login:
        return str(login).strip()
    return (getattr(auth, "email", "") or "").strip()


def _remove(tree: dict[str, Any], keys: list[str]) -> bool:
    """Delete the leaf at ``keys`` from a nested mapping, pruning mappings it empties.
    False when nothing was there."""
    if not keys:
        return False
    node: Any = tree
    trail: list[tuple[dict[str, Any], str]] = []
    for key in keys[:-1]:
        if not isinstance(node, dict) or key not in node:
            return False
        trail.append((node, key))
        node = node[key]
    if not isinstance(node, dict) or keys[-1] not in node:
        return False
    del node[keys[-1]]
    for parent, key in reversed(trail):
        if parent[key] == {}:
            del parent[key]
    return True


def create_config_router(auth_dependency: Callable[..., Any]) -> APIRouter:
    """Build the router.  ``auth_dependency`` resolves the caller (an object with an
    ``email``), injected so this module does not import the application."""
    router = APIRouter(prefix="/config", tags=["Config"])

    def admin(auth=Depends(auth_dependency)) -> str:
        cfg = configsys.get_config()
        admin_cfg = cfg.get("admin")
        if not admin_cfg.enabled:
            raise HTTPException(status_code=404, detail="Not found")
        if cfg.get("auth").provider == "none":
            if admin_cfg.allow_unauthenticated:
                return "anonymous"
            raise HTTPException(
                status_code=403,
                detail="Configuration can only be changed when authentication is enabled.",
            )
        allowed = {a.strip().lower() for a in admin_cfg.admins}
        identity = _admin_identity(auth)
        if not identity or identity.lower() not in allowed:
            raise HTTPException(
                status_code=403, detail="You are not a configuration administrator."
            )
        return identity

    Admin = Depends(admin)

    def _conflict(exc: Exception) -> HTTPException:
        return HTTPException(status_code=409, detail=str(exc))

    def _unprocessable(exc: ConfigError) -> HTTPException:
        return HTTPException(
            status_code=422,
            detail={"message": "Invalid configuration", "issues": _issues(exc)},
        )

    # ── public ────────────────────────────────────────────────────────────

    @router.get("/public", summary="Non-secret settings the frontend reads at boot")
    def public(project_id: Optional[str] = None) -> dict[str, Any]:
        """``project_id`` asks for the settings as they apply to that project."""
        cfg = configsys.get_config()
        auth = cfg.get("auth")
        features = (
            cfg.get_for_project("features", _check_project_id(project_id))
            if project_id
            else cfg.get("features")
        )
        body: dict[str, Any] = {
            "config_version": cfg.version,
            "auth": {"provider": auth.provider},
            "features": {"transcript_publish": features.transcript_publish},
        }
        frontend = cfg.get("frontend")
        body["frontend"] = {
            "features": {
                "transcription": frontend.feature_transcription,
                "in_review": frontend.feature_in_review,
                "ai": frontend.feature_ai,
            },
            "ws_url": frontend.ws_url,
            "prodtrack_tab_sync_extension_id": frontend.prodtrack_tab_sync_extension_id,
            "dev_mode": frontend.dev_mode,
        }
        if auth.provider == "google":
            body["auth"]["google_client_id"] = auth.google_client_id
        try:
            from dna.prodtrack_providers.profile import load_profile

            pt = cfg.get("prodtrack")
            profile = load_profile(
                pt.connector, path=pt.profile_path, overrides=pt.profile
            )
            body["prodtrack"] = {
                "connector": pt.connector,
                "display_name": profile.display_name,
                "labels": profile.labels,
                "roles": profile.roles,
                "capabilities": profile.capabilities.model_dump(),
            }
        except ValueError:  # no profile: the frontend falls back to its defaults
            body["prodtrack"] = {"connector": cfg.get("prodtrack").connector}
        return body

    @router.get("/access", summary="Whether the caller may administer configuration")
    def access(auth=Depends(auth_dependency)) -> dict[str, Any]:
        """Lets the UI decide whether to show the admin pages.  Never an error: a
        caller who may not administer simply gets ``admin: false`` (and so does
        everyone when the admin API is off, so its existence is not revealed)."""
        try:
            admin(auth)
        except HTTPException:
            return {"admin": False}
        return {"admin": True}

    # ── read ──────────────────────────────────────────────────────────────

    @router.get("/schema", summary="JSON Schema for every config section")
    def schema(_: str = Admin) -> dict[str, Any]:
        return configsys.get_config().schema()

    @router.get("/effective", summary="Resolved configuration with provenance")
    def effective(_: str = Admin) -> dict[str, Any]:
        cfg = configsys.get_config()
        return {
            "version": cfg.version,
            "revision": configsys.current_revision(),
            "config": cfg.effective(),  # secrets masked
            "provenance": cfg.provenance(),
            "runtime_overrides": cfg.runtime_overrides,
            # Sections that come from files/env only; the UI shows a lock, not an editor.
            "locked_sections": [
                s.name
                for s in configsys.default_registry.sections()
                if not s.runtime_overridable
            ],
        }

    # ── change ────────────────────────────────────────────────────────────

    @router.post("/validate", summary="Dry-run a change without applying it")
    def validate(body: ValidateRequest, _: str = Admin) -> dict[str, Any]:
        try:
            result = configsys.preview_overrides(body.patch, replace=body.replace)
        except ConfigError as exc:
            return {
                "valid": False,
                "issues": _issues(exc),
                "changed": [],
                "restart_required": [],
            }
        return {
            "valid": True,
            "issues": [],
            "changed": result.changed,
            "restart_required": result.restart_required,
        }

    @router.put("/{section}", summary="Change one config section")
    def update_section(
        section: str, body: SectionUpdate, actor: str = Admin
    ) -> dict[str, Any]:
        if section not in configsys.default_registry:
            raise HTTPException(
                status_code=404, detail=f"Unknown config section '{section}'"
            )
        try:
            result = configsys.apply_overrides(
                {section: body.values},
                actor=actor,
                note=body.note,
                expected_version=body.expected_version,
            )
        except ConfigError as exc:
            raise _unprocessable(exc)
        except StaleRevision as exc:
            raise _conflict(exc)
        logger.info(
            "Config section '%s' changed by %s: %s", section, actor, result.changed
        )
        return {
            "revision": result.version,
            "changed": result.changed,
            "restart_required": result.restart_required,
        }

    # ── per-project overrides ─────────────────────────────────────────────

    @router.get(
        "/projects",
        summary="Settings a project may override, and the overrides now set",
    )
    def project_overrides(_: str = Admin) -> dict[str, Any]:
        cfg = configsys.get_config()
        return {
            "revision": configsys.current_revision(),
            "settings": cfg.project_settings(),
            "overrides": cfg.project_overrides(),
        }

    @router.put("/projects/{project_id}", summary="Set overrides for one project")
    def set_project_overrides(
        project_id: str, body: ProjectUpdate, actor: str = Admin
    ) -> dict[str, Any]:
        _check_project_id(project_id)
        try:
            result = configsys.apply_overrides(
                {"projects": {project_id: body.values}},
                actor=actor,
                note=body.note or f"project {project_id}",
                expected_version=body.expected_version,
            )
        except ConfigError as exc:
            raise _unprocessable(exc)
        except StaleRevision as exc:
            raise _conflict(exc)
        logger.info(
            "Project '%s' overrides changed by %s: %s",
            project_id,
            actor,
            result.changed,
        )
        return {
            "revision": result.version,
            "changed": result.changed,
            "restart_required": result.restart_required,
        }

    @router.delete("/projects/{project_id}", summary="Remove a project override")
    def remove_project_override(
        project_id: str,
        section: Optional[str] = None,
        path: Optional[str] = None,
        expected_version: Optional[int] = None,
        actor: str = Admin,
    ) -> dict[str, Any]:
        """Remove one override (``section`` and ``path``), all of a section's, or all of
        the project's.  Only overrides set through the admin API can be removed here;
        one that comes from a config file has to be removed from that file."""
        _check_project_id(project_id)
        if path and not section:
            raise HTTPException(status_code=422, detail="'path' needs a 'section'")
        cfg = configsys.get_config()
        in_files = [
            o
            for o in cfg.project_overrides()
            if o["project"] == project_id
            and o["source"] != "runtime"
            and (section is None or o["section"] == section)
            and (path is None or o["path"] == path or o["path"].startswith(path + "."))
        ]
        runtime = cfg.runtime_overrides
        node = runtime.get("projects", {}).get(project_id)
        if node is None and not in_files:
            raise HTTPException(status_code=404, detail="No such override")
        if not _remove(
            runtime,
            [
                "projects",
                project_id,
                *filter(None, [section]),
                *(path.split(".") if path else []),
            ],
        ):
            if in_files:
                raise HTTPException(
                    status_code=409,
                    detail="That override is set in a configuration file; "
                    "remove it there.",
                )
            raise HTTPException(status_code=404, detail="No such override")
        try:
            result = configsys.apply_overrides(
                runtime,
                actor=actor,
                note=f"remove project {project_id} override",
                replace=True,
                expected_version=expected_version,
            )
        except ConfigError as exc:
            raise _unprocessable(exc)
        except StaleRevision as exc:
            raise _conflict(exc)
        return {
            "revision": result.version,
            "changed": result.changed,
            "restart_required": result.restart_required,
        }

    # ── history ───────────────────────────────────────────────────────────

    @router.get("/history", summary="Recorded configuration changes, newest first")
    def history(limit: int = 50, _: str = Admin) -> list[dict[str, Any]]:
        return [r.summary() for r in configsys.history(max(1, min(limit, 500)))]

    @router.post("/rollback/{version}", summary="Re-apply an older revision")
    def rollback(version: int, actor: str = Admin) -> dict[str, Any]:
        try:
            result = configsys.rollback(version, actor=actor)
        except ValueError as exc:
            if isinstance(exc, ConfigError):
                raise _unprocessable(exc)
            raise HTTPException(status_code=404, detail=str(exc))
        except StaleRevision as exc:
            raise _conflict(exc)
        logger.info("Config rolled back to revision %s by %s", version, actor)
        return {
            "revision": result.version,
            "changed": result.changed,
            "restart_required": result.restart_required,
        }

    @router.get("/export", summary="Export the runtime overrides")
    def export(_: str = Admin) -> dict[str, Any]:
        return configsys.export_overrides()

    @router.post("/import", summary="Replace the runtime overrides from an export")
    def import_(body: ImportRequest, actor: str = Admin) -> dict[str, Any]:
        try:
            result = configsys.import_overrides(body.document, actor=actor)
        except ConfigError as exc:
            raise _unprocessable(exc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except StaleRevision as exc:
            raise _conflict(exc)
        return {
            "revision": result.version,
            "changed": result.changed,
            "restart_required": result.restart_required,
        }

    # ── handshake ─────────────────────────────────────────────────────────

    @router.post(
        "/prodtrack/handshake", summary="Check a (candidate) tracker configuration"
    )
    def handshake(body: HandshakeRequest, actor: str = Admin) -> dict[str, Any]:
        """Connect, discover, validate and (optionally) smoke-test, then optionally save.

        Nothing is written to the tracker.  ``apply`` saves the candidate only when
        the report has no failing check.
        """
        from dna.prodtrack_providers.handshake import check_candidate

        try:
            report = check_candidate(body.candidate, smoke_email=body.smoke_email)
        except ConfigError as exc:
            raise _unprocessable(exc)
        except ValueError as exc:  # unknown connector, missing/invalid profile
            raise HTTPException(status_code=422, detail=str(exc))
        out = report.to_dict()
        out["applied"] = False
        if body.apply:
            if not report.ok:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "The handshake has failing checks; nothing was saved.",
                        "report": out,
                    },
                )
            try:
                result = configsys.apply_overrides(
                    body.candidate, actor=actor, note=body.note or "handshake"
                )
            except ConfigError as exc:
                raise _unprocessable(exc)
            except StaleRevision as exc:
                raise _conflict(exc)
            out.update(
                applied=True,
                revision=result.version,
                restart_required=result.restart_required,
            )
        return out

    return router
