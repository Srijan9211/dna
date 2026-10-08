"""DNA configuration system.

``get_config()`` returns the process-wide :class:`ConfigService`, loaded from
defaults, ``DNA_CONFIG_DIR`` YAML files, the environment and (when enabled) the
runtime overrides saved by administrators.  Importing this package registers every
section (see :mod:`dna.configsys.sections`).

``get_config()`` only ever reads memory, so it is safe to call from anywhere,
including the event loop.  The service follows its inputs: if a config file, a
secret file or a variable it reads changes, it reloads (a change that does not
validate is refused and the running configuration is kept).  When overrides are
persisted (``admin.persist``) a background thread polls the shared store and adopts
changes made on any other instance; if the store is unreachable it backs off and keeps
the configuration it has.  :func:`config_status` reports whether DNA is running on what
was intended, or on a fallback.

Runtime changes go through :func:`apply_overrides`, which validates, records a
revision (who, when, what, before and after), and applies.  :func:`rollback`
re-applies an older revision as a new one, so history is never rewritten.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from dna.configsys import sections  # noqa: F401  (registers the sections)
from dna.configsys.layers import deep_merge
from dna.configsys.meta import Section, setting
from dna.configsys.migrations import MigrationError, migrate
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
    StoreUnavailable,
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
    "StoreUnavailable",
    "apply_overrides",
    "config_status",
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
# The newest store revision this process has looked at.  A save is based on it, so a
# process that is behind gets a conflict instead of overwriting someone's change.
_applied_version = 0
# The revision actually in effect.  It differs from the above when the newest one could
# not be used and an older valid one was adopted instead.
_running_revision = 0
_checked_at = 0.0

# Why DNA is not running on exactly what was intended (cleared when it is).
_input_problem: Optional[str] = None  # a config file or the environment is invalid
_store_problem: Optional[str] = None  # the shared store cannot be reached
_revision_problem: Optional[str] = None  # a stored revision cannot be used
_store_failures = 0

# ``get_config()`` runs many times per request, and reading ~100 environment
# variables each time is real CPU (notably on Windows).  A running server's
# environment does not change, so inputs are re-checked at most this often; 0 means
# every call (the test suite does that, because tests change the environment on
# purpose).
ENV_RECHECK_SECONDS = 5.0
_env_checked_at = 0.0

# True: a background thread keeps the shared store in sync, so no caller ever waits on
# the network.  False: ``get_config()`` syncs inline when due (used by tests).
BACKGROUND_REFRESH = True
_refresher: Optional[threading.Thread] = None
_stop = threading.Event()

_MAX_BACKOFF_SECONDS = 60.0
_FALLBACK_SEARCH = 50  # how many older revisions to try when the newest is unusable


# ── noticing that inputs changed ─────────────────────────────────────────────


def _env_fingerprint() -> tuple:
    """Everything in the environment the service reads, as a comparable value."""
    env = os.environ
    names = default_registry.sorted_env_names()
    return (
        env.get(CONFIG_DIR_ENV),
        tuple(env.get(n) for n in names),
        tuple(sorted((k, v) for k, v in env.items() if k.startswith("DNA__"))),
    )


def _files_fingerprint(service: Optional[ConfigService]) -> tuple:
    """Modification state of the config files and of any file a secret reads, so an
    edited YAML file or a rotated mounted secret is picked up without a restart."""
    paths: set[str] = set(service.watched_files()) if service is not None else set()
    config_dir = service.config_dir if service is not None else None
    if config_dir is None and os.environ.get(CONFIG_DIR_ENV):
        config_dir = Path(os.environ[CONFIG_DIR_ENV])
    if config_dir is not None and config_dir.is_dir():
        paths.update(
            str(p)
            for p in config_dir.iterdir()
            if p.suffix.lower() in (".yaml", ".yml")
        )
    state = []
    for path in sorted(paths):
        try:
            stat = os.stat(path)
            state.append((path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            state.append((path, None, None))
    return tuple(state)


def _get_store(service: ConfigService) -> OverrideStore:
    """The override store: shared MongoDB when persistence is on, else in-process."""
    global _store
    if _store is None:
        if service.get("admin").persist:
            storage = service.get("storage")
            _store = MongoOverrideStore(
                url=storage.mongodb_url, db_name=storage.mongodb_db
            )
        else:
            _store = MemoryOverrideStore()
    return _store


# ── loading ──────────────────────────────────────────────────────────────────


def _reload_inputs(current: tuple) -> None:
    """(Re)build the service because a file or variable changed.

    At startup an invalid configuration is fatal and says why.  Once running, a bad
    edit is refused: the configuration in effect stays, the problem is reported by
    :func:`config_status`, and the next edit gets a fresh attempt.
    """
    global _service, _fingerprint, _input_problem
    first = _service is None
    try:
        service = ConfigService(runtime_overrides=_runtime).load()
    except ConfigError as exc:
        if first:
            raise
        _fingerprint = current  # do not retry until something changes again
        _input_problem = (
            "the configuration files or environment changed but are invalid; "
            "still running on the previous configuration: " + str(exc).splitlines()[0]
        )
        logger.error(
            "Configuration changed on disk but is invalid; keeping the previous "
            "configuration:\n%s",
            exc,
        )
        return
    _service, _fingerprint, _input_problem = service, current, None
    if first:
        _refresh_once()  # adopt the shared revision before serving anything


def get_config() -> ConfigService:
    """The process-wide service.  Reads memory only; never waits on the network."""
    global _env_checked_at
    service = _service
    now = time.monotonic()
    if (
        service is None
        or ENV_RECHECK_SECONDS <= 0
        or now - _env_checked_at >= ENV_RECHECK_SECONDS
    ):
        current = (_env_fingerprint(), _files_fingerprint(service))
        _env_checked_at = now
        if service is None or current != _fingerprint:
            with _lock:
                if _service is None or current != _fingerprint:
                    _reload_inputs(current)
        service = _service
    assert service is not None
    if service.get("admin").persist:
        _ensure_refresh(service)
    return _service or service  # a refresh may just have swapped it


# ── the shared store ─────────────────────────────────────────────────────────


def _ensure_refresh(service: ConfigService) -> None:
    global _refresher
    if BACKGROUND_REFRESH:
        if _refresher is None or not _refresher.is_alive():
            with _lock:
                if _refresher is None or not _refresher.is_alive():
                    _refresher = threading.Thread(
                        target=_refresh_loop,
                        args=(_stop,),
                        name="config-refresh",
                        daemon=True,
                    )
                    _refresher.start()
    elif time.monotonic() - _checked_at >= service.get("admin").refresh_seconds:
        _refresh_once()


def _refresh_loop(stop: threading.Event) -> None:
    failures = 0
    while not stop.is_set():
        try:
            ok = _refresh_once()
        except Exception:  # never let the thread die
            logger.exception("Config refresh failed unexpectedly")
            ok = False
        failures = 0 if ok else failures + 1
        service = _service
        base = service.get("admin").refresh_seconds if service is not None else 5.0
        delay = (
            min(_MAX_BACKOFF_SECONDS, base * (2 ** min(failures, 5)))
            if failures
            else base
        )
        stop.wait(delay)


def _refresh_once() -> bool:
    """Look at the shared store and adopt a newer revision.  False if it is unreachable.

    Never raises: a store that is down, or a revision that no longer validates, must
    not take DNA down.  Both are reported through :func:`config_status` instead.
    """
    global _checked_at, _store_problem, _store_failures
    _checked_at = time.monotonic()
    service = _service
    if service is None or not service.get("admin").persist:
        return True
    try:
        store = _get_store(service)
        latest = store.latest()
    except Exception as exc:
        _store_failures += 1
        _store_problem = (
            "the shared configuration store is unreachable; running on revision "
            f"{_running_revision}"
        )
        # Say it when it starts and then now and again, not on every retry.
        if _store_failures == 1 or _store_failures % 20 == 0:
            logger.error(
                "Config override store unavailable (%d consecutive failure(s)); "
                "keeping the current configuration: %s",
                _store_failures,
                exc,
            )
        return False
    if _store_failures:
        logger.info("Config override store is reachable again.")
    _store_failures, _store_problem = 0, None
    if latest is None or latest.version == _applied_version:
        return True
    _adopt(store, latest)
    return True


def _adopt(store: OverrideStore, latest: Revision) -> None:
    """Make ``latest`` the running configuration, or the newest older one that works.

    A revision can stop being usable after an upgrade (a renamed setting, a stricter
    rule).  Dropping every runtime change because of that would silently undo the
    administrators' work, so the newest revision that still validates is used instead
    and the situation is reported until someone saves a good one.
    """
    global _service, _runtime, _applied_version, _running_revision, _revision_problem
    versions = [latest.version]
    try:
        versions += [
            r.version
            for r in store.history(_FALLBACK_SEARCH)
            if r.version < latest.version
        ]
    except StoreUnavailable:
        pass
    first_error = ""
    for version in versions:
        revision = latest if version == latest.version else store.get(version)
        if revision is None:
            continue
        try:
            overrides = migrate(revision.overrides, revision.schema)
            loaded = ConfigService(runtime_overrides=overrides).load()
        except (ConfigError, MigrationError) as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            first_error = first_error or reason
            logger.error("Config revision %d cannot be applied: %s", version, exc)
            continue
        with _lock:
            _service, _runtime = loaded, overrides
            _applied_version, _running_revision = latest.version, revision.version
            if revision.version == latest.version:
                _revision_problem = None
            else:
                _revision_problem = (
                    f"revision {latest.version} cannot be applied ({first_error}); "
                    f"running on the last valid revision, {revision.version}. "
                    "Save a corrected revision or roll back."
                )
        return
    with _lock:
        _applied_version = latest.version
        _revision_problem = (
            f"revision {latest.version} cannot be applied ({first_error}) and no "
            "earlier revision is usable; running on the files and environment only."
        )


def config_status() -> dict[str, Any]:
    """Whether DNA is running on the configuration that was intended.

    ``ok`` is false when a bad file edit was refused, the shared store is unreachable,
    or a stored revision could not be used.  Each is described in ``problems``.
    """
    service = get_config()
    problems = [p for p in (_input_problem, _store_problem, _revision_problem) if p]
    return {
        "ok": not problems,
        "problems": problems,
        "persist": service.get("admin").persist,
        "running_revision": _running_revision,
        "latest_revision": _applied_version,
        "warnings": service.warnings,
    }


# ── changing configuration at runtime ────────────────────────────────────────


def preview_overrides(
    patch: Mapping[str, Any], *, replace: bool = False
) -> ChangeResult:
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

    Raises:
        ConfigError: the change is not valid; nothing was saved.
        StaleRevision: someone saved first.
        StoreUnavailable: the shared store could not be reached; nothing was saved.
    """
    global _runtime, _applied_version, _running_revision, _revision_problem
    with _lock:
        service = get_config()
        preview = service.apply_overrides(patch, replace=replace, dry_run=True)
        snapshot = (
            deep_merge({}, patch)
            if replace
            else deep_merge(service.runtime_overrides, patch)
        )
        revision = _get_store(service).append(
            snapshot,
            actor=actor,
            note=note,
            changed=tuple(preview.changed),
            details=tuple(preview.details),
            expected_version=(
                _applied_version if expected_version is None else expected_version
            ),
        )
        result = service.apply_overrides(patch, replace=replace)
        _runtime, _applied_version = service.runtime_overrides, revision.version
        _running_revision, _revision_problem = revision.version, None
        return ChangeResult(
            revision.version, result.changed, result.restart_required, result.details
        )


def current_revision() -> int:
    """The newest store revision this process has seen (0 before any runtime change)."""
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
        migrate(target.overrides, target.schema),
        actor=actor,
        note=f"rollback to revision {version}",
        replace=True,
    )


def export_overrides() -> dict[str, Any]:
    """The current runtime overrides as a portable document (references, never secrets)."""
    service = get_config()
    return {
        "format": 1,
        "revision": _applied_version,
        "overrides": service.runtime_overrides,
    }


def import_overrides(
    document: Mapping[str, Any], *, actor: str = "system"
) -> ChangeResult:
    """Replace the runtime overrides with an exported document."""
    if document.get("format") != 1 or not isinstance(document.get("overrides"), dict):
        raise ValueError(
            "Not a DNA config export (expected format 1 with an 'overrides' mapping)"
        )
    return apply_overrides(
        document["overrides"], actor=actor, note="import", replace=True
    )


def reset_config() -> None:
    """Drop the cached service, overrides and store (tests)."""
    global _service, _fingerprint, _runtime, _store, _applied_version, _checked_at
    global _env_checked_at, _running_revision, _refresher, _stop
    global _input_problem, _store_problem, _revision_problem, _store_failures
    with _lock:
        _stop.set()  # a running refresh thread exits at its next wake-up
        _stop = threading.Event()
        _refresher = None
        _service, _fingerprint, _runtime, _store = None, None, {}, None
        _applied_version = _running_revision = 0
        _checked_at = _env_checked_at = 0.0
        _input_problem = _store_problem = _revision_problem = None
        _store_failures = 0
