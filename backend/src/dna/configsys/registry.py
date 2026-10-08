"""Section registry.

Each subsystem registers a typed section here.  The registry is the single
list of "what can be configured", from which the JSON Schema, the admin UI,
the documentation and the env-coverage test are all derived.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from dna.configsys.meta import Section, iter_fields, meta

# (path relative to the section, value, env var name) for provenance.
LegacyEnvHook = Callable[
    [Mapping[str, str]], Iterable[tuple[tuple[str, ...], Any, str]]
]


@dataclass(frozen=True)
class SectionSpec:
    name: str
    model: type[Section]
    legacy_env: Optional[LegacyEnvHook] = None
    # Env var names read by ``legacy_env`` (so coverage can be checked).
    legacy_env_names: tuple[str, ...] = ()
    # False for sections that carry bootstrap or security settings (auth, storage,
    # server, admin).  They come from files and the environment only, never from
    # the runtime API, so a compromised admin session cannot turn authentication
    # off or point DNA at another database.
    runtime_overridable: bool = True


@dataclass
class Registry:
    _specs: dict[str, SectionSpec] = field(default_factory=dict)

    def register(
        self,
        name: str,
        model: type[Section],
        *,
        legacy_env: Optional[LegacyEnvHook] = None,
        legacy_env_names: Iterable[str] = (),
        runtime_overridable: bool = True,
    ) -> SectionSpec:
        if name in self._specs:
            raise ValueError(f"Config section '{name}' is already registered")
        if name == "projects":
            raise ValueError("'projects' is reserved for per-project overrides")
        spec = SectionSpec(
            name, model, legacy_env, tuple(legacy_env_names), runtime_overridable
        )
        self._specs[name] = spec
        return spec

    def get(self, name: str) -> SectionSpec:
        return self._specs[name]

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def sections(self) -> list[SectionSpec]:
        return list(self._specs.values())

    def sorted_env_names(self) -> tuple[str, ...]:
        """``known_env_names`` as a sorted tuple, computed once per registration set
        (``get_config()`` needs it on every call, and it never changes at runtime)."""
        cached = getattr(self, "_env_names_cache", None)
        if cached is None or cached[0] != len(self._specs):
            cached = (len(self._specs), tuple(sorted(self.known_env_names())))
            self._env_names_cache = cached
        return cached[1]

    def known_env_names(self) -> set[str]:
        """Every environment variable this registry reads."""
        names: set[str] = set()
        for spec in self._specs.values():
            for _, info in iter_fields(spec.model):
                env = meta(info).get("env")
                if env:
                    names.add(env)
            names.update(spec.legacy_env_names)
        return names

    def schema(self) -> dict[str, Any]:
        return {s.name: s.model.model_json_schema() for s in self._specs.values()}


default_registry = Registry()
