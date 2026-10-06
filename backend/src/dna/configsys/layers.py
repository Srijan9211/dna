"""Config layers: files, environment, merging and provenance.

Layers are merged lowest to highest precedence.  Every leaf value remembers
which layer supplied it, so an operator can ask "where did this come from".
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dna.configsys.meta import iter_fields, meta
from dna.configsys.registry import Registry

Path_ = tuple[str, ...]

# Reserved top-level key holding per-project overrides:
#   projects: {<project_id>: {<section>: {...}}}
PROJECTS_KEY = "projects"


@dataclass
class Layer:
    """One source of configuration."""

    name: str  # "file", "env", "runtime" ...
    data: dict[str, Any]
    detail: str = ""  # default provenance detail (e.g. the file path)
    details: dict[str, str] = field(default_factory=dict)  # per-leaf override

    def source_of(self, dotted: str) -> str:
        detail = self.details.get(dotted, self.detail)
        return f"{self.name}:{detail}" if detail else self.name


def flatten(data: Mapping[str, Any], prefix: Path_ = ()) -> Iterator[tuple[Path_, Any]]:
    """Yield ``(path, value)`` for every leaf.  Empty dicts count as leaves."""
    for key, value in data.items():
        path = (*prefix, str(key))
        if isinstance(value, Mapping) and value:
            yield from flatten(value, path)
        else:
            yield path, value


def set_path(data: dict[str, Any], path: Path_, value: Any) -> None:
    """Set ``value`` at ``path``, replacing any non-dict in the way."""
    node = data
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Return a new dict with ``patch`` merged over ``base`` (patch wins)."""
    merged = copy.deepcopy(base)
    for path, value in flatten(patch):
        set_path(merged, path, copy.deepcopy(value))
    return merged


def merge_layers(layers: list[Layer]) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge layers in order; return the merged dict and a provenance map."""
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for layer in layers:
        for path, value in flatten(layer.data):
            set_path(merged, path, copy.deepcopy(value))
            dotted = ".".join(path)
            provenance[dotted] = layer.source_of(dotted)
    return merged, provenance


def load_file_layers(config_dir: Path) -> list[Layer]:
    """Load every ``*.yaml`` / ``*.yml`` in ``config_dir``, sorted by name.

    Later files override earlier ones, so ``00-base.yaml`` then ``10-site.yaml``
    behaves as expected.  A file's top level maps section names to settings.
    """
    layers: list[Layer] = []
    if not config_dir or not config_dir.is_dir():
        return layers
    files = sorted(
        p for p in config_dir.iterdir() if p.suffix.lower() in (".yaml", ".yml")
    )
    for path in files:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path}: top level must be a mapping of sections")
        layers.append(Layer("file", loaded, detail=str(path)))
    return layers


def build_env_layer(registry: Registry, environ: Mapping[str, str]) -> Layer:
    """Collect configuration from the environment.

    Three forms are honoured, lowest to highest precedence:
      1. field aliases declared with ``setting(env=...)`` (``SHOTGRID_URL``)
      2. a section's ``legacy_env`` hook (computed names, e.g. per-LLM keys)
      3. the uniform ``DNA__SECTION__KEY`` form (``DNA__SERVER__LOG_LEVEL``)

    An empty value is treated as unset, matching how the existing code reads
    most variables (``os.getenv(X) or default``).
    """
    data: dict[str, Any] = {}
    details: dict[str, str] = {}

    def put(path: Path_, value: Any, source: str) -> None:
        set_path(data, path, value)
        details[".".join(path)] = source

    for spec in registry.sections():
        for rel, info in iter_fields(spec.model):
            name = meta(info).get("env")
            if name and environ.get(name, "") != "":
                put((spec.name, *rel), environ[name], name)
        if spec.legacy_env is not None:
            for rel, value, name in spec.legacy_env(environ):
                put((spec.name, *rel), value, name)

    for name, value in environ.items():
        if not name.startswith("DNA__") or value == "":
            continue
        parts = tuple(p.lower() for p in name[len("DNA__") :].split("__") if p)
        if len(parts) >= 2:
            put(parts, value, name)

    return Layer("env", data, details=details)
