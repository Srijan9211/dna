"""Setting metadata and helpers for walking section models.

Every configurable value is a pydantic field declared with :func:`setting`,
which attaches the metadata the config service needs: the legacy environment
variable that feeds it, whether it is a secret, whether a change applies live
or needs a restart, and which scopes (site / project / user) may override it.
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Mapping
from typing import Any, Literal, Optional, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field
from pydantic.fields import FieldInfo

Reload = Literal["hot", "restart"]
SCOPES = ("site", "project", "user")


class Section(BaseModel):
    """Base class for every config section and nested group.

    ``extra="forbid"`` turns a typo in a studio YAML file into a startup error
    instead of a silently ignored key.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


def setting(
    default: Any = ...,
    *,
    env: Optional[str] = None,
    secret: bool = False,
    endpoint: bool = False,
    reload: Reload = "hot",
    scope: tuple[str, ...] = ("site",),
    description: str = "",
    default_factory: Any = None,
    **kwargs: Any,
) -> Any:
    """Declare a configurable field.

    Args:
        env: Legacy environment variable that feeds this field.  Existing
            deployments keep working because the alias is always honoured.
        secret: Masked in every API response; overrides must be references
            (``${env:NAME}`` / ``${file:PATH}``), never plaintext.
        endpoint: A URL DNA sends credentials to.  A change made at runtime may only
            point it at a host the operator already trusts, so an administrator cannot
            aim a real API key at a server of their choosing.
        reload: ``"hot"`` applies without a restart; ``"restart"`` is flagged
            so an operator knows the change is not live yet.
        scope: Layers allowed to override the field beyond the site config.
    """
    bad = [s for s in scope if s not in SCOPES]
    if bad:
        raise ValueError(f"Unknown scope(s) {bad}; expected a subset of {SCOPES}")
    extra = {
        "env": env,
        "secret": secret,
        "endpoint": endpoint,
        "reload": reload,
        "scope": list(scope),
    }
    if default_factory is not None:
        return Field(
            default_factory=default_factory,
            description=description,
            json_schema_extra=extra,
            **kwargs,
        )
    return Field(default, description=description, json_schema_extra=extra, **kwargs)


def meta(info: Optional[FieldInfo]) -> dict[str, Any]:
    """Return the metadata dict attached by :func:`setting` (empty if none)."""
    if info is None:
        return {}
    extra = info.json_schema_extra
    return extra if isinstance(extra, dict) else {}


def _model_type(annotation: Any) -> tuple[Optional[type[BaseModel]], bool]:
    """Find the nested model in an annotation.

    Returns ``(model, is_mapping)``; ``is_mapping`` is True for
    ``dict[str, Model]`` where the next path segment is a free-form key.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, False
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (dict, Mapping) and len(args) == 2:
        inner, _ = _model_type(args[1])
        if inner is not None:
            return inner, True
    if origin is Union or origin is types.UnionType:
        for arg in args:
            if arg is type(None):
                continue
            found = _model_type(arg)
            if found[0] is not None:
                return found
    return None, False


def iter_fields(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], FieldInfo]]:
    """Yield ``(path, field)`` for every field, descending into nested groups.

    Mappings of models (``dict[str, Model]``) are not descended: their keys are
    data, so they cannot carry a fixed environment alias.
    """
    for name, info in model.model_fields.items():
        path = (*prefix, name)
        yield path, info
        nested, is_mapping = _model_type(info.annotation)
        if nested is not None and not is_mapping:
            yield from iter_fields(nested, path)


def lookup(model: type[BaseModel], path: tuple[str, ...]) -> Optional[FieldInfo]:
    """Resolve the field governing ``path`` (skipping free-form mapping keys).

    A path that runs past a leaf (e.g. into a plain ``dict`` value) resolves to
    that leaf's field, so metadata such as ``secret`` still applies.
    """
    current: Optional[type[BaseModel]] = model
    info: Optional[FieldInfo] = None
    i = 0
    while i < len(path):
        if current is None:
            return info
        info = current.model_fields.get(path[i])
        if info is None:
            return None
        i += 1
        current, is_mapping = _model_type(info.annotation)
        if is_mapping:
            i += 1
    return info
