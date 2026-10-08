"""Who is listening to a meeting.

DNA subscribes to a transcription service for each meeting that is in progress, and
turns what it hears into stored transcript segments and live events.  With several
instances, *one* of them must do that for a given meeting; if every instance
subscribed, each transcript line would be processed (and shown to browsers) once per
instance.

A **lease** is a record that says "instance X is handling meeting M until time T".  An
instance may take a meeting only when nobody holds a live lease on it, and keeps it by
renewing before T.  If the owner dies, T passes, and another instance takes over, so a
meeting survives the loss of the instance that started it.

Leases live in MongoDB with the meeting as ``_id``, so two instances racing to take the
same meeting cannot both succeed: the database rejects the second insert.  Expiry uses
each instance's own clock, so instances should run NTP (a second or two of skew is
harmless next to a lease of tens of seconds).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "dna_meeting_leases"
DEFAULT_TTL_SECONDS = 60.0


class MeetingLeases:
    def __init__(
        self,
        collection: Any,
        instance_id: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock=time.time,
    ) -> None:
        self._c = collection
        self.instance_id = instance_id
        self.ttl = ttl_seconds
        self._clock = clock

    async def acquire(self, meeting_key: str) -> bool:
        """Take (or keep) the lease.  False when another instance holds a live one."""
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError

        now = self._clock()
        try:
            await self._c.find_one_and_update(
                {
                    "_id": meeting_key,
                    "$or": [{"owner": self.instance_id}, {"expires": {"$lt": now}}],
                },
                {
                    "$set": {"owner": self.instance_id, "expires": now + self.ttl},
                    "$setOnInsert": {"since": now},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return True
        except DuplicateKeyError:
            # No document matched our condition (someone else holds a live lease), so
            # the upsert tried to create one and the database refused: it is taken.
            return False

    async def renew(self, meeting_key: str) -> bool:
        """Extend a lease we hold.  False if we no longer hold it (it expired and
        another instance took the meeting): the caller must stop handling it."""
        result = await self._c.update_one(
            {"_id": meeting_key, "owner": self.instance_id},
            {"$set": {"expires": self._clock() + self.ttl}},
        )
        return result.matched_count == 1

    async def release(self, meeting_key: str) -> None:
        """Give the meeting up (it ended, or this instance is shutting down)."""
        await self._c.delete_one({"_id": meeting_key, "owner": self.instance_id})

    async def owner(self, meeting_key: str) -> Optional[str]:
        doc = await self._c.find_one({"_id": meeting_key})
        if doc is None or doc.get("expires", 0) < self._clock():
            return None
        return doc.get("owner")
