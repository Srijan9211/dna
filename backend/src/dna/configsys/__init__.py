"""DNA configuration system.

``get_config()`` returns the process-wide :class:`ConfigService`, loaded from
defaults, ``DNA_CONFIG_DIR`` YAML files, the environment and (when enabled) the
runtime overrides saved by administrators.  Importing this package registers every
section (see :mod:`dna.configsys.sections`).

The service follows its inputs: if a variable it reads changes (tests patching
``os.environ``, an operator exporting a new value) the next ``get_config()`` call
reloads, and when overrides are persisted (``admin.persist``) every instance polls
the shared store and picks up changes made on any other.

Runtime changes go through :func:`apply_overrides`, which validates, records a
revision (who, when, what), and applies.  :func:`rollback` re-applies an older
revision as a new one, so history is never rewritten.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Mapping, Optional

from dna.configsys import sections  # noqa: F401  (registers the sections)
from dna.configsys.layers import deep_merge
from dna.configsys.meta import Section, setting
from dna.configsys.registry import Registry, default_registry
from dna.configsys.service import (
    CONFIG_DIR_ENV,
    ChangeResult,
    ConfigError,
    ConfigService,
    Issue,
)
from dna.configsys.store import (
    MemoryOverrideStore,
    MongoOverrideStore,
    OverrideStore,
    Revision,
    StaleRevision,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ChangeResult",
    "ConfigError",
    "ConfigService",
    "Issue",
    "Registry",
    "Revision",
    "Section",
    "StaleRevision",
    "apply_overrides",
    "current_revision",
    "default_registry",
    "export_overrides",
    "get_config",
    "history",
    "import_overrides",
    "preview_overrides",
    "reset_config",
    "rollback",
    "setting",
]

_lock = threading.RLock()  # apply_overrides() calls get_config() while holding it
_service: Optional[ConfigService] = None
_fingerprint: Optional[tuple] = None
_runtime: dict[str, Any] = {}
_store: Optional[OverrideStore] = None
_applied_version = 0  # the store revision currently reflected in ``_runtime``
_checked_at = 0.0


def _env_fingerprint() -> tuple:
    """Everything in the environment the service reads, as a comparable value."""
    env = os.environ
    names = sorted(default_registry.known_env_names())
    return (
        env.get(CONFIG_DIR_ENV),
        tuple(env.get(n) for n in names),
        tuple(sorted((k, v) for k, v in env.items() if k.startswith("DNA__"))),
    )


def _get_store(service: ConfigService) -> OverrideStore:
    """The override store: shared MongoDB when persistence is on, else in-process."""
    global _store
    if _store is None:
        if service.get("admin").persist:
            storage = service.get("storage")
            _store = MongoOverrideStore(url=storage.mongodb_url, db_name=storage.mongodb_db)
        else:
            _store = MemoryOverrideStore()
    return _store


def _sync_from_store(service: ConfigService) -> ConfigService:
    """Adopt a newer persisted revision, if any.  Never raises: a store that is
    down or holds a revision that no longer validates must not take DNA down."""
    global _runtime, _applied_version, _checked_at
    _checked_at = time.monotonic()
    if not service.get("admin").persist:
        return service
    try:
        latest = _get_store(service).latest()
        if latest is None or latest.version == _applied_version:
            return service
        loaded = ConfigService(runtime_overrides=latest.overrides).load()
    except ConfigError as exc:
        logger.error(
            "Config revision could not be applied; continuing with the files and "
            "environment only. Fix it and save a new revision, or roll back:\n%s", exc,
        )
        return service
    except Exception as exc:  # store unreachable
        logger.error("Config override store unavailable; keeping current config: %s", exc)
        return service
    _runtime, _applied_version = latest.overrides, latest.version
    return loaded


def get_config() -> ConfigService:
    """The process-wide service, (re)loaded when its inputs changed."""
    global _service, _fingerprint
    current = _env_fingerprint()
    if _service is not None and current == _fingerprint:
        due = _service.get("admin").persist and (
            time.monotonic() - _checked_at >= _service.get("admin").refresh_seconds
        )
        if not due:
            return _service
    with _lock:
        if _service is None or current != _fingerprint:
            service = ConfigService(runtime_overrides=_runtime).load()
            _fingerprint = current
        else:
            service = _service
        _service = _sync_from_store(service)
        return _service


# ── changing configuration at runtime ────────────────────────────────────────


def preview_overrides(patch: Mapping[str, Any], *, replace: bool = False) -> ChangeResult:
    """What ``patch`` would change, validated exactly as a real change, applied to nothing."""
    return get_config().apply_overrides(patch, replace=replace, dry_run=True)


def apply_overrides(
    patch: Mapping[str, Any],
    *,
    actor: str = "system",
    note: str = "",
    replace: bool = False,
    expected_version: Optional[int] = None,
) -> ChangeResult:
    """Validate, record and apply a runtime change.

    The change is validated first, saved as a revision, and only then applied, so a
    failed save never leaves this process ahead of the store.

    Args:
        expected_version: the revision the caller based the change on; if someone
            else has saved since, :class:`StaleRevision` is raised instead of
            silently overwriting their work.
    """
    global _runtime, _applied_version
    with _lock:
        service = get_config()
        preview = service.apply_overrides(patch, replace=replace, dry_run=True)
        snapshot = deep_merge({}, patch) if replace else deep_merge(service.runtime_overrides, patch)
        revision = _get_store(service).append(
            snapshot,
            actor=actor,
            note=note,
            changed=tuple(preview.changed),
            expected_version=_applied_version if expected_version is None else expected_version,
        )
        result = service.apply_overrides(patch, replace=replace)
        _runtime, _applied_version = service.runtime_overrides, revision.version
        return ChangeResult(revision.version, result.changed, result.restart_required)


def current_revision() -> int:
    """The store revision this process is running (0 before any runtime change)."""
    get_config()
    return _applied_version


def history(limit: int = 50) -> list[Revision]:
    """Recorded changes, newest first."""
    return _get_store(get_config()).history(limit)


def rollback(version: int, *, actor: str = "system") -> ChangeResult:
    """Re-apply the overrides of revision ``version`` as a new revision."""
    store = _get_store(get_config())
    target = store.get(version)
    if target is None:
        raise ValueError(f"No config revision {version}")
    return apply_overrides(
        target.overrides, actor=actor, note=f"rollback to revision {version}", replace=True
    )


def export_overrides() -> dict[str, Any]:
    """The current runtime overrides as a portable document (references, never secrets)."""
    service = get_config()
    return {"format": 1, "revision": _applied_version, "overrides": service.runtime_overrides}


def import_overrides(document: Mapping[str, Any], *, actor: str = "system") -> ChangeResult:
    """Replace the runtime overrides with an exported document."""
    if document.get("format") != 1 or not isinstance(document.get("overrides"), dict):
        raise ValueError("Not a DNA config export (expected format 1 with an 'overrides' mapping)")
    return apply_overrides(
        document["overrides"], actor=actor, note="import", replace=True
    )


def reset_config() -> None:
    """Drop the cached service, overrides and store (tests)."""
    global _service, _fingerprint, _runtime, _store, _applied_version, _checked_at
    with _lock:
        _service, _fingerprint, _runtime, _store = None, None, {}, None
        _applied_version, _checked_at = 0, 0.0
