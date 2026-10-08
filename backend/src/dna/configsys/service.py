"""The config service: load, validate, resolve, report.

Lifecycle
---------
``load()`` builds every layer, resolves secret references, validates *all*
sections together and raises one :class:`ConfigError` listing every problem
(rather than failing on the first).  A successful load is atomic: either the
whole new configuration takes effect or the previous one stays.

``apply_overrides()`` is the same pipeline with a patch added to the runtime
layer, so what an admin saves is validated exactly like what a file contains.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from dna.configsys.layers import (
    PROJECTS_KEY,
    Layer,
    build_env_layer,
    deep_merge,
    flatten,
    load_file_layers,
    merge_layers,
)
from dna.configsys.meta import iter_fields, lookup, meta
from dna.configsys.registry import Registry, default_registry
from dna.configsys.secrets import is_ref, mask, ref_target, resolve_refs

logger = logging.getLogger(__name__)

CONFIG_DIR_ENV = "DNA_CONFIG_DIR"


@dataclass(frozen=True)
class Issue:
    section: str
    path: str
    message: str
    source: str = ""

    def __str__(self) -> str:
        where = f" (from {self.source})" if self.source else ""
        return f"{self.path or self.section}: {self.message}{where}"


class ConfigError(ValueError):
    """Raised when configuration is invalid.  Carries every issue found."""

    def __init__(self, issues: list[Issue]):
        self.issues = issues
        lines = "\n".join(f"  - {i}" for i in issues)
        super().__init__(f"Invalid configuration ({len(issues)} problem(s)):\n{lines}")


@dataclass(frozen=True)
class ChangeResult:
    version: int
    changed: list[str]
    restart_required: list[str]
    # What each changed setting was and became ({"path", "before", "after"}); secrets
    # appear masked.  The audit trail keeps it, so a reviewer sees the change itself.
    details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _State:
    models: dict[str, BaseModel]
    provenance: dict[str, str]
    merged: dict[str, Any]
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    watched: list[str] = field(default_factory=list)


class ConfigService:
    def __init__(
        self,
        registry: Registry = default_registry,
        *,
        environ: Optional[Mapping[str, str]] = None,
        config_dir: Optional[Path] = None,
        runtime_overrides: Optional[dict[str, Any]] = None,
    ):
        self._registry = registry
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        if config_dir is None and self._environ.get(CONFIG_DIR_ENV):
            config_dir = Path(self._environ[CONFIG_DIR_ENV])
        self._config_dir = config_dir
        self._runtime: dict[str, Any] = dict(runtime_overrides or {})
        self._state: Optional[_State] = None
        self._version = 0

    # ── loading ───────────────────────────────────────────────────────────

    def load(self) -> "ConfigService":
        self._state = self._build(self._runtime)
        self._version += 1
        for message in self._state.warnings:
            logger.warning("Configuration: %s", message)
        return self

    @property
    def config_dir(self) -> Optional[Path]:
        return self._config_dir

    def watched_files(self) -> list[str]:
        """Paths whose modification should trigger a reload."""
        return list(self.state.watched)

    @property
    def warnings(self) -> list[str]:
        """Things that are valid but unwise (for example a plain secret in a file)."""
        return list(self.state.warnings)

    def _build(self, runtime: dict[str, Any]) -> _State:
        layers: list[Layer] = []
        layers.extend(load_file_layers(self._config_dir) if self._config_dir else [])
        layers.append(build_env_layer(self._registry, self._environ))
        if runtime:
            layers.append(Layer("runtime", runtime))
        merged, provenance = merge_layers(layers)

        issues: list[Issue] = []
        issues.extend(self._check_runtime_references(layers, runtime))
        issues.extend(self._check_runtime_endpoints(layers, runtime))
        ref_errors: list[tuple[str, str]] = []
        raw_merged = merged  # before secret references are resolved
        merged = resolve_refs(merged, self._environ, ref_errors)
        issues.extend(
            Issue(p.split(".")[0], p, msg, provenance.get(p, ""))
            for p, msg in ref_errors
        )

        known = {s.name for s in self._registry.sections()} | {PROJECTS_KEY}
        for name in merged:
            if name not in known:
                issues.append(
                    Issue(
                        name, name, "unknown config section", provenance.get(name, "")
                    )
                )

        models: dict[str, BaseModel] = {}
        for spec in self._registry.sections():
            raw = merged.get(spec.name, {})
            if not isinstance(raw, dict):
                issues.append(Issue(spec.name, spec.name, "must be a mapping"))
                continue
            try:
                models[spec.name] = spec.model.model_validate(raw)
            except ValidationError as exc:
                issues.extend(self._issues_from(spec.name, exc, provenance))

        projects = merged.get(PROJECTS_KEY, {}) or {}
        issues.extend(self._check_projects(projects, models, provenance))

        if issues:
            raise ConfigError(issues)
        return _State(
            models,
            provenance,
            merged,
            projects,
            self._plain_secret_warnings(layers) + self._shadow_warnings(layers),
            self._watched_files(layers, raw_merged),
        )

    def _trusted_hosts(self, layers: list[Layer]) -> set[str]:
        """Hosts a runtime change may point an endpoint at: those the operator put in
        the files or environment, each endpoint's built-in default, and
        ``admin.allowed_hosts``."""
        trusted: set[str] = set()
        trusted_layers = [layer for layer in layers if layer.name != "runtime"]
        for layer in trusted_layers:
            for path, value in _walk_strings(layer.data):
                if path[0] in self._registry:
                    info = lookup(self._registry.get(path[0]).model, path[1:])
                    if info is not None and meta(info).get("endpoint"):
                        trusted.add(_host(value))
        for spec in self._registry.sections():
            for _, info in iter_fields(spec.model):
                if meta(info).get("endpoint") and isinstance(info.default, str):
                    trusted.add(_host(info.default))
        lower, _ = merge_layers(trusted_layers)
        extra = (lower.get("admin") or {}).get("allowed_hosts") or []
        if isinstance(extra, str):
            extra = extra.split(",")
        trusted.update(h.strip().lower() for h in extra if str(h).strip())
        trusted.discard("")
        return trusted

    def _check_runtime_endpoints(
        self, layers: list[Layer], runtime: dict[str, Any]
    ) -> list[Issue]:
        """Keep an administrator from aiming credentials at a server of their choosing.

        An endpoint (a tracker, LLM or transcription URL) is where DNA sends its API
        key.  If a runtime change could set it to any host, pairing it with the real
        key (a permitted reference) would hand that key to whoever runs the host.  So
        at runtime an endpoint may only use a host already trusted (see
        :meth:`_trusted_hosts`); anything else is the operator's decision, made in the
        files, the environment or ``admin.allowed_hosts``.
        """
        if not runtime:
            return []
        trusted: Optional[set[str]] = None
        issues: list[Issue] = []
        for path, value in _walk_strings(runtime):
            if path[0] == PROJECTS_KEY or path[0] not in self._registry:
                continue
            info = lookup(self._registry.get(path[0]).model, path[1:])
            if info is None or not meta(info).get("endpoint") or not value.strip():
                continue
            if trusted is None:
                trusted = self._trusted_hosts(layers)
            host = _host(value)
            if host and host not in trusted:
                issues.append(
                    Issue(
                        path[0],
                        ".".join(path),
                        f"host '{host}' is not trusted for runtime changes; an "
                        "operator must add it to the config files or "
                        "admin.allowed_hosts (DNA_CONFIG_ALLOWED_HOSTS)",
                        "runtime",
                    )
                )
        return issues

    def _shadow_warnings(self, layers: list[Layer]) -> list[str]:
        """An override saved on the admin page outranks the files and environment, so
        editing those has no effect.  Say so, rather than leaving it a mystery."""
        runtime = next((lyr for lyr in layers if lyr.name == "runtime"), None)
        if runtime is None:
            return []
        lower, provenance = merge_layers(
            [lyr for lyr in layers if lyr.name != "runtime"]
        )
        out: list[str] = []
        for path, value in flatten(runtime.data):
            if path[0] == PROJECTS_KEY:
                continue
            before = _get(lower, path)
            if before is None or _same(before, value):
                continue
            dotted = ".".join(path)
            out.append(
                f"{dotted} is set on the admin page and overrides "
                f"{provenance.get(dotted, 'the configured value')}; changing that "
                "source has no effect until the override is removed."
            )
        return out

    def _watched_files(
        self, layers: list[Layer], raw_merged: dict[str, Any]
    ) -> list[str]:
        """Files whose change should reload the configuration: the YAML files and any
        file a secret reference reads (so a rotated mounted secret is noticed)."""
        files = [lyr.detail for lyr in layers if lyr.name == "file" and lyr.detail]
        for _, value in _walk_strings(raw_merged):
            target = ref_target(value) if is_ref(value) else None
            if target is not None and target[0] == "file":
                files.append(target[1])
        return sorted(set(files))

    def _plain_secret_warnings(self, layers: list[Layer]) -> list[str]:
        """A secret written into a config file sits in version control and backups.
        It is allowed (an operator may have no alternative) but worth saying out loud.
        """
        out: list[str] = []
        for layer in layers:
            if layer.name != "file":
                continue
            for path, value in _walk_strings(layer.data):
                if not value or is_ref(value) or path[0] not in self._registry:
                    continue
                info = lookup(self._registry.get(path[0]).model, path[1:])
                if info is not None and meta(info).get("secret"):
                    out.append(
                        f"{layer.detail}: {'.'.join(path)} holds a plain secret; "
                        "use ${env:NAME} or ${file:PATH} instead."
                    )
        return out

    def _check_runtime_references(
        self, layers: list[Layer], runtime: dict[str, Any]
    ) -> list[Issue]:
        """Keep runtime changes from reading arbitrary server secrets.

        The runtime layer is writable through the admin API.  If it could carry any
        ``${env:NAME}`` or ``${file:PATH}``, an administrator could point a plain
        setting (or a tracker URL they control) at ``${env:JWT_SECRET}`` or a file
        and read the value back.  So a reference there is accepted only on a secret
        setting, and only for something the operator already sanctioned: an
        environment variable that a secret setting is documented to read, or a
        reference the operator wrote in the config files.
        """
        if not runtime:
            return []
        env_names: set[str] = set()
        file_refs: set[str] = set()
        for spec in self._registry.sections():
            for _, info in iter_fields(spec.model):
                m = meta(info)
                if m.get("secret") and m.get("env"):
                    env_names.add(m["env"])
            # Names a section computes (one API key per LLM profile): kept when
            # they feed a secret setting.
            if spec.legacy_env is not None and spec.legacy_env_names:
                probe = {name: "x" for name in spec.legacy_env_names}
                for rel, _, name in spec.legacy_env(probe):
                    if meta(lookup(spec.model, tuple(rel))).get("secret"):
                        env_names.add(name)
        for layer in layers:
            if layer.name != "file":
                continue
            for _, value in _walk_strings(layer.data):
                target = ref_target(value) if is_ref(value) else None
                if target is None:
                    continue
                if target[0] == "env":
                    env_names.add(target[1])
                else:
                    file_refs.add(value)

        issues: list[Issue] = []
        for path, value in _walk_strings(runtime):
            target = ref_target(value) if is_ref(value) else None
            if target is None:
                continue
            dotted = ".".join(path)
            section = path[0]
            field_path = path[1:]
            if section == PROJECTS_KEY:
                section, field_path = (path[2], path[3:]) if len(path) > 3 else ("", ())
            info = (
                lookup(self._registry.get(section).model, field_path)
                if section in self._registry and field_path
                else None
            )
            kind, name = target
            if not (info is not None and meta(info).get("secret")):
                msg = "references are only allowed on secret settings"
            elif kind == "env" and name not in env_names:
                msg = (
                    f"environment variable {name} cannot be referenced from runtime "
                    "configuration; use one the operator has configured for secrets"
                )
            elif kind == "file" and value not in file_refs:
                msg = "file references cannot be set at runtime; define them in the config files"
            else:
                continue
            issues.append(Issue(path[0], dotted, msg, "runtime"))
        return issues

    @staticmethod
    def _issues_from(
        section: str, exc: ValidationError, provenance: Mapping[str, str]
    ) -> list[Issue]:
        out: list[Issue] = []
        for err in exc.errors():
            loc = ".".join([section, *[str(p) for p in err["loc"]]])
            out.append(Issue(section, loc, err["msg"], provenance.get(loc, "")))
        return out

    def _check_projects(
        self,
        projects: Any,
        models: dict[str, BaseModel],
        provenance: Mapping[str, str],
    ) -> list[Issue]:
        issues: list[Issue] = []
        if not isinstance(projects, dict):
            return [Issue(PROJECTS_KEY, PROJECTS_KEY, "must be a mapping")]
        for pid, sections in projects.items():
            for name, patch in (sections or {}).items():
                if name not in self._registry:
                    issues.append(
                        Issue(
                            name,
                            f"{PROJECTS_KEY}.{pid}.{name}",
                            "unknown config section",
                        )
                    )
                    continue
                model_cls = self._registry.get(name).model
                for path, _ in flatten(patch or {}):
                    info = lookup(model_cls, path)
                    dotted = ".".join([PROJECTS_KEY, str(pid), name, *path])
                    if info is None:
                        issues.append(Issue(name, dotted, "unknown setting"))
                    elif "project" not in meta(info).get("scope", ["site"]):
                        issues.append(
                            Issue(name, dotted, "cannot be overridden per project")
                        )
                base = models.get(name)
                if base is None:
                    continue
                try:
                    model_cls.model_validate(deep_merge(base.model_dump(), patch or {}))
                except ValidationError as exc:
                    issues.extend(
                        self._issues_from(
                            f"{PROJECTS_KEY}.{pid}.{name}", exc, provenance
                        )
                    )
        return issues

    # ── reading ───────────────────────────────────────────────────────────

    @property
    def state(self) -> _State:
        if self._state is None:
            raise RuntimeError("ConfigService.load() has not been called")
        return self._state

    @property
    def version(self) -> int:
        return self._version

    @property
    def runtime_overrides(self) -> dict[str, Any]:
        """A copy of the runtime override layer currently applied."""
        return deep_merge({}, self._runtime)

    def get(self, section: str) -> Any:
        """The validated site-level model for ``section``."""
        return self.state.models[section]

    def get_for_project(self, section: str, project_id: str) -> Any:
        """``section`` with the project's overrides applied."""
        base = self.state.models[section]
        patch = (self.state.projects.get(str(project_id)) or {}).get(section)
        if not patch:
            return base
        model_cls = self._registry.get(section).model
        return model_cls.model_validate(deep_merge(base.model_dump(), patch))

    def project_overrides(self) -> list[dict[str, Any]]:
        """Every per-project override now in force, with where it came from.

        ``source`` is ``runtime`` for one set through the admin page, API or CLI, or
        ``file:<path>`` for one in a config file (those can only be changed there).
        """
        out: list[dict[str, Any]] = []
        for key, value in flatten(self.state.projects):
            if len(key) < 3:
                continue
            dotted = ".".join([PROJECTS_KEY, *key])
            out.append(
                {
                    "project": key[0],
                    "section": key[1],
                    "path": ".".join(key[2:]),
                    "value": value,
                    "source": self.state.provenance.get(dotted, "runtime"),
                }
            )
        return sorted(out, key=lambda o: (o["project"], o["section"], o["path"]))

    def project_settings(self) -> list[dict[str, Any]]:
        """The settings a project may override, with the site-wide value each has."""
        out: list[dict[str, Any]] = []
        for spec in self._registry.sections():
            model = self.state.models.get(spec.name)
            if model is None:
                continue
            connector = getattr(model, "connector", None)
            for path, info in iter_fields(spec.model):
                m = meta(info)
                if "project" not in (m.get("scope") or []):
                    continue
                if (
                    spec.name == "prodtrack"
                    and path[0] in _CONNECTOR_GROUPS
                    and path[0] != connector
                ):
                    continue  # another tracker's setting: it cannot apply here
                out.append(
                    {
                        "section": spec.name,
                        "path": ".".join(path),
                        "description": info.description or "",
                        "type": _type_name(info.annotation),
                        "site_value": _get(_dump(model, True), path),
                        "restart": m.get("reload") == "restart",
                        "secret": bool(m.get("secret")),
                    }
                )
        return out

    def effective(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        """Resolved configuration, secrets masked unless explicitly disabled."""
        return {
            name: _dump(model, mask_secrets)
            for name, model in self.state.models.items()
        }

    def provenance(self) -> dict[str, str]:
        """Source of every effective leaf value (``default`` if none supplied)."""
        out: dict[str, str] = {}
        for name, model in self.state.models.items():
            for path, _ in flatten(_dump(model, True)):
                dotted = ".".join([name, *path])
                out[dotted] = self.state.provenance.get(dotted, "default")
        return out

    def schema(self) -> dict[str, Any]:
        return self._registry.schema()

    # ── changing ──────────────────────────────────────────────────────────

    def apply_overrides(
        self,
        patch: Mapping[str, Any],
        *,
        replace: bool = False,
        dry_run: bool = False,
        allow_locked: bool = False,
    ) -> ChangeResult:
        """Validate and apply a runtime override patch atomically.

        Args:
            replace: ``patch`` is the complete new set of overrides (rollback,
                import) instead of a change merged onto the current ones.
            dry_run: validate and report what would change without applying it.
            allow_locked: permit sections marked not runtime-overridable.  Only
                trusted callers (tests, bootstrap) set this; the HTTP API never does.

        Secret values must be references (``${env:...}`` / ``${file:...}``);
        plaintext secrets are refused so they can never reach the override store.
        """
        issues: list[Issue] = []
        for path, value in flatten(patch):
            if path[0] == PROJECTS_KEY:
                # projects.<id>.<section>.<setting>: a project override of a secret is
                # held to the same rule (the settings that allow it are checked below).
                if len(path) > 3 and path[2] in self._registry:
                    info = lookup(self._registry.get(path[2]).model, path[3:])
                    if (
                        info is not None
                        and meta(info).get("secret")
                        and not is_ref(value)
                    ):
                        issues.append(
                            Issue(
                                path[2],
                                ".".join(path),
                                "secret values must be a reference like "
                                "${env:NAME} or ${file:PATH}, not plaintext",
                            )
                        )
                continue
            if path[0] not in self._registry:
                continue  # reported as an unknown section by the build below
            spec = self._registry.get(path[0])
            if not spec.runtime_overridable and not allow_locked:
                issues.append(
                    Issue(
                        path[0],
                        ".".join(path),
                        f"the '{path[0]}' section cannot be changed at runtime; set it "
                        "in the config files or the environment",
                    )
                )
                continue
            info = lookup(spec.model, path[1:])
            if info is not None and meta(info).get("secret") and not is_ref(value):
                issues.append(
                    Issue(
                        path[0],
                        ".".join(path),
                        "secret values must be a reference like ${env:NAME} "
                        "or ${file:PATH}, not plaintext",
                    )
                )
        if issues:
            raise ConfigError(issues)

        before = {n: _dump(m, False) for n, m in self.state.models.items()}
        new_runtime = (
            deep_merge({}, patch) if replace else deep_merge(self._runtime, patch)
        )
        new_state = self._build(new_runtime)  # raises ConfigError, state untouched

        changed: list[str] = []
        restart: list[str] = []
        details: list[dict[str, Any]] = []
        before_shown = {n: _dump(m, True) for n, m in self.state.models.items()}
        for name, model in new_state.models.items():
            after = _dump(model, False)
            after_shown = _dump(model, True)
            for path, value in flatten(after):
                old = _get(before.get(name, {}), path)
                if old != value:
                    dotted = ".".join([name, *path])
                    changed.append(dotted)
                    info = lookup(self._registry.get(name).model, path)
                    if meta(info).get("reload") == "restart":
                        restart.append(dotted)
                    if meta(info).get("secret"):
                        # show the reference that was set, never the secret behind it
                        was = _get(self._runtime, (name, *path))
                        now = _get(new_runtime, (name, *path))
                    else:
                        was = _get(before_shown.get(name, {}), path)
                        now = _get(after_shown, path)
                    details.append(
                        {"path": dotted, "before": _shown(was), "after": _shown(now)}
                    )

        old_projects = {k: v for k, v in flatten(self.state.projects)}
        new_projects = {k: v for k, v in flatten(new_state.projects)}
        for key in sorted(set(old_projects) | set(new_projects)):
            if old_projects.get(key) == new_projects.get(key):
                continue
            changed.append(".".join([PROJECTS_KEY, *key]))
            details.append(
                {
                    "path": ".".join([PROJECTS_KEY, *key]),
                    "before": _shown(old_projects.get(key)),
                    "after": _shown(new_projects.get(key)),
                }
            )
            if len(key) > 2 and key[1] in self._registry:
                info = lookup(self._registry.get(key[1]).model, key[2:])
                if meta(info).get("reload") == "restart":
                    restart.append(".".join([PROJECTS_KEY, *key]))
        details.sort(key=lambda d: d["path"])
        if dry_run:
            return ChangeResult(
                self._version, sorted(changed), sorted(restart), details
            )
        self._runtime = new_runtime
        self._state = new_state
        self._version += 1
        return ChangeResult(self._version, sorted(changed), sorted(restart), details)


# The per-tracker groups inside the prodtrack section (``prodtrack.shotgrid.*``).
_CONNECTOR_GROUPS = frozenset({"shotgrid", "ayon", "mock"})


def _host(value: str) -> str:
    """The lower-cased host name of a URL, or '' when it has none."""
    try:
        return (urlsplit(str(value).strip()).hostname or "").lower()
    except ValueError:
        return ""


def _same(a: Any, b: Any) -> bool:
    """Loose equality across the string form the environment gives and the typed
    value a runtime change carries (``"true"`` and ``True``)."""
    return str(a).strip().lower() == str(b).strip().lower()


def _shown(value: Any, limit: int = 200) -> Any:
    """A value as it is kept in the audit trail: short, and never a secret."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _type_name(annotation: Any) -> str:
    """A UI-friendly name for a setting's type: boolean, integer, number or string."""
    text = str(annotation)
    for needle, name in (("bool", "boolean"), ("int", "integer"), ("float", "number")):
        if needle in text:
            return name
    return "string"


def _get(data: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _dump(model: BaseModel, mask_secrets: bool) -> dict[str, Any]:
    """Dump a model to plain data, masking secret fields when asked."""
    out: dict[str, Any] = {}
    for name, info in type(model).model_fields.items():
        out[name] = _dump_value(getattr(model, name), info, mask_secrets)
    return out


def _dump_value(value: Any, info: Any, mask_secrets: bool) -> Any:
    if mask_secrets and meta(info).get("secret"):
        return mask(value)
    return _plain(value, mask_secrets)


def _plain(value: Any, mask_secrets: bool) -> Any:
    if isinstance(value, BaseModel):
        return _dump(value, mask_secrets)
    if isinstance(value, dict):
        return {k: _plain(v, mask_secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v, mask_secrets) for v in value]
    return value


def _walk_strings(
    data: Any, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], str]]:
    """Every string leaf of a nested mapping/list, with its path."""
    if isinstance(data, Mapping):
        for key, value in data.items():
            yield from _walk_strings(value, (*path, str(key)))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            yield from _walk_strings(value, (*path, str(i)))
    elif isinstance(data, str):
        yield path, data
