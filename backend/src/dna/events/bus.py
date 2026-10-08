"""Carrying live events between backend instances.

A browser holds a WebSocket to *one* instance.  With several instances behind a load
balancer, an event raised on instance A (a transcript line, a bot status change) must
also reach the browsers connected to B and C.  The bus does that.

``MongoEventBus`` uses a **capped collection** that every instance reads.  A capped
collection is a fixed-size ring that MongoDB keeps in insertion order, and a tailable
cursor follows new entries as they are added: effectively a small publish/subscribe
channel.  It is used instead of change streams because those need a replica set, and
DNA's MongoDB is a single node by default.  Each instance polls its cursor every
50 ms, so an event reaches other instances' browsers in roughly 50 ms.  (Letting the
server hold the cursor until news arrives looks better but is not: on a standalone
MongoDB only some waiting readers are woken, and delivery slowed to seconds as
instances were added.  This was measured, and is covered by a test.)  Nothing here is
persistent: an event is live, and an instance that was down while it was published does
not replay it (state is read from the database; events only say "something changed").

An instance delivers its own events to its own browsers directly and writes them to the
bus for the others; it skips its own messages when it reads the bus back, so nothing is
delivered twice.  Publishing is best effort and never raises into a request: if the bus
is down, local browsers still get their events and the failure is reported by
``healthy`` (and so by ``/health``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

OnMessage = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_COLLECTION = "dna_events"
DEFAULT_SIZE_BYTES = 8 * 1024 * 1024  # the ring: old events fall off the end
DEFAULT_MAX_DOCS = 10_000
DEFAULT_POLL_SECONDS = 0.05  # how often each instance looks for new events
_MARKER = "__marker__"  # a tailable cursor needs at least one document to attach to
_MAX_BACKOFF_SECONDS = 30.0


def new_instance_id() -> str:
    """A name for this process, unique per start, readable in logs."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class EventBus(Protocol):
    instance_id: str

    @property
    def healthy(self) -> bool: ...

    async def start(self, on_message: OnMessage) -> None:
        """Begin delivering other instances' messages to ``on_message``."""
        ...

    async def publish(self, message: dict[str, Any]) -> None:
        """Send ``message`` to the other instances.  Never raises."""
        ...

    async def stop(self) -> None: ...


class MongoEventBus:
    def __init__(
        self,
        url: str,
        db_name: str,
        *,
        collection: str = DEFAULT_COLLECTION,
        size_bytes: int = DEFAULT_SIZE_BYTES,
        max_docs: int = DEFAULT_MAX_DOCS,
        instance_id: Optional[str] = None,
        client: Any = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.instance_id = instance_id or new_instance_id()
        self._poll = poll_seconds
        self._url, self._db_name = url, db_name
        self._collection_name = collection
        self._size, self._max = size_bytes, max_docs
        self._client = client
        self._collection: Any = None
        self._task: Optional[asyncio.Task] = None
        self._on_message: Optional[OnMessage] = None
        self._healthy = False
        self._stopping = False

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def database(self) -> Any:
        """The database the bus uses, for collections that share its connection."""
        return self._client[self._db_name]

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self, on_message: OnMessage) -> None:
        """Create the ring if needed and start listening.  Raises if MongoDB cannot be
        reached, so a misconfigured deployment says so at startup."""
        from pymongo import AsyncMongoClient

        self._on_message = on_message
        if self._client is None:
            self._client = AsyncMongoClient(
                self._url,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
            )
        db = self._client[self._db_name]
        await self._ensure_collection(db)
        self._collection = db[self._collection_name]
        # Decide where "now" is before returning, not in the background task: an
        # event raised a moment after start() must not be mistaken for an old one.
        start_after = await self._newest_id()
        self._healthy = True
        self._stopping = False
        self._task = asyncio.create_task(self._tail(start_after), name="event-bus-tail")
        logger.info("Event bus started on instance %s", self.instance_id)

    async def _ensure_collection(self, db: Any) -> None:
        from pymongo.errors import CollectionInvalid, OperationFailure

        if self._collection_name in await db.list_collection_names():
            return
        try:
            await db.create_collection(
                self._collection_name, capped=True, size=self._size, max=self._max
            )
        except (CollectionInvalid, OperationFailure) as exc:
            # Another instance created it between our check and our create.
            if self._collection_name not in await db.list_collection_names():
                raise
            logger.debug("Event bus collection already created elsewhere: %s", exc)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._healthy = False

    # ── publishing ────────────────────────────────────────────────────────

    async def publish(self, message: dict[str, Any]) -> None:
        if self._collection is None:
            return
        try:
            await self._collection.insert_one(
                {"i": self.instance_id, "t": time.time(), "m": message}
            )
            self._healthy = True
        except Exception as exc:  # best effort: local clients were already served
            self._healthy = False
            logger.warning("Could not publish to the event bus: %s", exc)

    # ── listening ─────────────────────────────────────────────────────────

    async def _newest_id(self) -> Any:
        """The id of the newest document, adding a marker if the ring is empty (a
        tailable cursor on an empty capped collection closes immediately)."""
        async for doc in self._collection.find({}, sort=[("$natural", -1)], limit=1):
            return doc["_id"]
        result = await self._collection.insert_one(
            {"i": _MARKER, "t": time.time(), "m": None}
        )
        return result.inserted_id

    async def _tail(self, start_after: Any) -> None:
        from pymongo import CursorType

        backoff = 1.0
        last_id: Any = start_after
        while not self._stopping:
            try:
                if last_id is None:
                    last_id = await self._newest_id()  # live events only: none replayed
                # A tailable cursor that we poll ourselves.  The server can hold a
                # cursor open until news arrives ("await"), but on a standalone
                # MongoDB that wake-up does not reach every waiting reader: measured
                # with several instances, delivery took seconds and grew with the
                # number of readers.  Polling costs a trivial getMore every
                # ``poll_seconds`` per instance and delivers in a time that does not
                # depend on how many instances there are.
                cursor = self._collection.find(
                    {"_id": {"$gt": last_id}}, cursor_type=CursorType.TAILABLE
                )
                while cursor.alive and not self._stopping:
                    async for doc in cursor:
                        last_id = doc["_id"]
                        await self._deliver(doc)
                    # A poll that completed means the connection is good: recover the
                    # health flag and the backoff here, not only when the cursor ends.
                    self._healthy = True
                    backoff = 1.0
                    await asyncio.sleep(self._poll)
                self._healthy = True
                backoff = 1.0
                # The cursor ended (the server closed it): reopen from where we were.
                # If the ring overwrote that point, the newest id is the right place.
                if not await self._still_there(last_id):
                    last_id = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._healthy = False
                logger.warning(
                    "Event bus listener lost its connection (%s); retrying in %.0fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(_MAX_BACKOFF_SECONDS, backoff * 2)
                last_id = None  # events missed while away are not replayed

    async def _still_there(self, doc_id: Any) -> bool:
        async for _ in self._collection.find({"_id": doc_id}, limit=1):
            return True
        return False

    async def _deliver(self, doc: dict[str, Any]) -> None:
        if doc.get("i") in (self.instance_id, _MARKER) or self._on_message is None:
            return
        try:
            await self._on_message(doc["m"])
        except Exception:
            logger.exception("Error delivering an event received from the bus")
