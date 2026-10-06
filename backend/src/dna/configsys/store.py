"""Persistence for runtime config overrides: an append-only revision history.

Every change an administrator makes is a new *revision* holding the complete set of
overrides after the change, who made it, when, and which settings it touched.
Because each revision is a full snapshot, rollback is simply "append a copy of an
older revision", so history is never rewritten and every action stays auditable.

Overrides never contain a plaintext secret (the service refuses one), so revisions
are safe to store and to show.  Several backend instances share one store; each
polls it and reloads when a newer revision appears.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class Revision:
    version: int
    overrides: dict[str, Any]
    actor: str
    at: float
    note: str = ""
    changed: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        """A revision without its (possibly large) snapshot, for listings."""
        return {
            "version": self.version,
            "actor": self.actor,
            "at": self.at,
            "note": self.note,
            "changed": list(self.changed),
        }

    def to_doc(self) -> dict[str, Any]:
        return {
            "_id": self.version,
            "version": self.version,
            "overrides": copy.deepcopy(self.overrides),
            "actor": self.actor,
            "at": self.at,
            "note": self.note,
            "changed": list(self.changed),
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Revision":
        return cls(
            version=doc["version"],
            overrides=copy.deepcopy(doc.get("overrides") or {}),
            actor=doc.get("actor", ""),
            at=doc.get("at", 0.0),
            note=doc.get("note", ""),
            changed=tuple(doc.get("changed") or ()),
        )


class OverrideStore(Protocol):
    def latest(self) -> Optional[Revision]: ...

    def get(self, version: int) -> Optional[Revision]: ...

    def history(self, limit: int = 50) -> list[Revision]:
        """Newest first."""
        ...

    def append(
        self,
        overrides: dict[str, Any],
        *,
        actor: str,
        note: str = "",
        changed: tuple[str, ...] = (),
        expected_version: Optional[int] = None,
    ) -> Revision:
        """Add a revision.  With ``expected_version``, fail (``StaleRevision``) if
        someone else saved first, so two admins cannot silently overwrite each other."""
        ...


class StaleRevision(Exception):
    """The store moved on since the caller read it; reload and retry."""


@dataclass
class MemoryOverrideStore:
    """In-process store: development and tests (state is lost on restart)."""

    _revisions: list[Revision] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def latest(self) -> Optional[Revision]:
        with self._lock:
            return self._revisions[-1] if self._revisions else None

    def get(self, version: int) -> Optional[Revision]:
        with self._lock:
            return next((r for r in self._revisions if r.version == version), None)

    def history(self, limit: int = 50) -> list[Revision]:
        with self._lock:
            return list(reversed(self._revisions))[:limit]

    def append(self, overrides, *, actor, note="", changed=(), expected_version=None) -> Revision:
        with self._lock:
            current = self._revisions[-1].version if self._revisions else 0
            if expected_version is not None and expected_version != current:
                raise StaleRevision(f"expected revision {expected_version}, store is at {current}")
            revision = Revision(
                current + 1, copy.deepcopy(overrides), actor, time.time(), note, tuple(changed)
            )
            self._revisions.append(revision)
            return revision


class MongoOverrideStore:
    """MongoDB-backed store shared by every backend instance.

    ``_id`` is the version number, so Mongo's unique ``_id`` makes two concurrent
    appends race safely: exactly one wins, the other gets ``StaleRevision``.
    """

    def __init__(self, collection: Any = None, *, url: str = "", db_name: str = "dna") -> None:
        if collection is None:
            from pymongo import MongoClient

            collection = MongoClient(url, serverSelectionTimeoutMS=5000)[db_name]["config_revisions"]
        self._c = collection

    def _latest_doc(self) -> Optional[dict]:
        docs = list(self._c.find({}, sort=[("version", -1)], limit=1))
        return docs[0] if docs else None

    def latest(self) -> Optional[Revision]:
        doc = self._latest_doc()
        return Revision.from_doc(doc) if doc else None

    def get(self, version: int) -> Optional[Revision]:
        doc = self._c.find_one({"_id": version})
        return Revision.from_doc(doc) if doc else None

    def history(self, limit: int = 50) -> list[Revision]:
        return [Revision.from_doc(d) for d in self._c.find({}, sort=[("version", -1)], limit=limit)]

    def append(self, overrides, *, actor, note="", changed=(), expected_version=None) -> Revision:
        from pymongo.errors import DuplicateKeyError

        latest = self._latest_doc()
        current = latest["version"] if latest else 0
        if expected_version is not None and expected_version != current:
            raise StaleRevision(f"expected revision {expected_version}, store is at {current}")
        revision = Revision(current + 1, copy.deepcopy(overrides), actor, time.time(), note, tuple(changed))
        try:
            self._c.insert_one(revision.to_doc())
        except DuplicateKeyError as exc:
            raise StaleRevision("another administrator saved at the same moment") from exc
        return revision
