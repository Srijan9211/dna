"""Upgrading stored overrides when a setting is renamed or reshaped.

Runtime overrides are kept in a shared store and outlive the DNA version that wrote
them.  If a later version renames ``llm.provider`` to ``llm.default``, every stored
revision would stop validating and (without this) take the administrators' changes
with it.  So each revision records the schema it was written under, and a migration
per step upgrades it as it is read:

    CURRENT_SCHEMA = 2                       # in store.py
    MIGRATIONS[1] = _rename_llm_provider     # schema 1 -> 2

A migration is a pure function from the old overrides to the new ones.  A revision
written by a *newer* DNA than this one is refused rather than guessed at, so a rolling
deploy or a downgrade never silently misreads it.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from dna.configsys import store

Migration = Callable[[dict[str, Any]], dict[str, Any]]

# MIGRATIONS[n] upgrades overrides written under schema n to schema n + 1.
MIGRATIONS: dict[int, Migration] = {}


class MigrationError(Exception):
    """The overrides cannot be brought up to this version's schema."""


def migrate(overrides: dict[str, Any], from_schema: int) -> dict[str, Any]:
    """``overrides`` upgraded from ``from_schema`` to the current schema."""
    current = store.CURRENT_SCHEMA
    if from_schema > current:
        raise MigrationError(
            f"written by a newer DNA (schema {from_schema}); this version "
            f"understands up to schema {current}"
        )
    result = copy.deepcopy(overrides)
    for version in range(from_schema, current):
        step = MIGRATIONS.get(version)
        if step is None:
            raise MigrationError(f"no migration from schema {version} to {version + 1}")
        try:
            result = step(result)
        except Exception as exc:
            raise MigrationError(
                f"migration from schema {version} failed: {exc}"
            ) from exc
    return result
