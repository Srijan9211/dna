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

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
from dna.configsys.meta import lookup, meta
from dna.configsys.registry import Registry, default_registry
from dna.configsys.secrets import is_ref, mask, resolve_refs

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


@dataclass
class _State:
    models: dict[str, BaseModel]
    provenance: dict[str, str]
    merged: dict[str, Any]
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)


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
        self._environ: Mapping[str, str] = (
            os.environ if environ is None else environ
        )
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
        return self

    def _build(self, runtime: dict[str, Any]) -> _State:
        layers: list[Layer] = []
        layers.extend(load_file_layers(self._config_dir) if self._config_dir else [])
        layers.append(build_env_layer(self._registry, self._environ))
        if runtime:
            layers.append(Layer("runtime", runtime))
        merged, provenance = merge_layers(layers)

        issues: list[Issue] = []
        ref_errors: list[tuple[str, str]] = []
        merged = resolve_refs(merged, self._environ, ref_errors)
        issues.extend(
            Issue(p.split(".")[0], p, msg, provenance.get(p, ""))
            for p, msg in ref_errors
        )

        known = {s.name for s in self._registry.sections()} | {PROJECTS_KEY}
        for name in merged:
            if name not in known:
                issues.append(
                    Issue(name, name, "unknown config section", provenance.get(name, ""))
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
        return _State(models, provenance, merged, projects)

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
                        Issue(name, f"{PROJECTS_KEY}.{pid}.{name}", "unknown config section")
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
                    model_cls.model_validate(
                        deep_merge(base.model_dump(), patch or {})
                    )
                except ValidationError as exc:
                    issues.extend(
                        self._issues_from(f"{PROJECTS_KEY}.{pid}.{name}", exc, provenance)
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

    def effective(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        """Resolved configuration, secrets masked unless explicitly disabled."""
        return {
            name: _dump(model, mask_secrets) for name, model in self.state.models.items()
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
        new_runtime = deep_merge({}, patch) if replace else deep_merge(self._runtime, patch)
        new_state = self._build(new_runtime)  # raises ConfigError, state untouched

        changed: list[str] = []
        restart: list[str] = []
        for name, model in new_state.models.items():
            after = _dump(model, False)
            for path, value in flatten(after):
                old = _get(before.get(name, {}), path)
                if old != value:
                    dotted = ".".join([name, *path])
                    changed.append(dotted)
                    info = lookup(self._registry.get(name).model, path)
                    if meta(info).get("reload") == "restart":
                        restart.append(dotted)

        if dry_run:
            return ChangeResult(self._version, sorted(changed), sorted(restart))
        self._runtime = new_runtime
        self._state = new_state
        self._version += 1
        return ChangeResult(self._version, sorted(changed), sorted(restart))


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
