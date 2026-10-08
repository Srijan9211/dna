"""The event bus and meeting leases without a database.

These run everywhere (CI has no MongoDB) against small in-memory stand-ins that behave
like the parts of MongoDB the code relies on.  The real-database behaviour (races,
cursors, timing) is checked in test_event_bus.py and test_meeting_ownership.py; this
file keeps the logic honest where those are skipped.
"""

import asyncio
import json

import pytest
from pymongo.errors import DuplicateKeyError

from dna.events import EventPublisher, EventType
from dna.events.bus import MongoEventBus, new_instance_id
from dna.events.lease import MeetingLeases


class Ring:
    """An in-memory capped collection plus the little of the async client we use."""

    def __init__(self, shared=None):
        self.docs = shared if shared is not None else []
        self.next_id = len(self.docs) + 1
        self.fail_inserts = False
        self.fail_find = 0  # the next N find() calls raise
        self.break_cursors = 0  # the next N cursor reads raise (a dropped connection)

    async def insert_one(self, doc):
        if self.fail_inserts:
            raise ConnectionError("down")
        doc = {**doc, "_id": self.next_id}
        self.next_id += 1
        self.docs.append(doc)

        class Result:
            inserted_id = doc["_id"]

        return Result()

    def find(self, query=None, sort=None, limit=0, cursor_type=None):
        if self.fail_find:
            self.fail_find -= 1
            raise ConnectionError("lost")
        floor = ((query or {}).get("_id") or {}).get("$gt")
        docs = [d for d in self.docs if floor is None or d["_id"] > floor]
        if sort:
            docs = list(reversed(self.docs))
        if limit:
            docs = docs[:limit]
        return Cursor(self, docs, tail=cursor_type is not None, floor=floor)


class Cursor:
    def __init__(self, ring, docs, tail, floor):
        self.ring, self._docs, self.tail, self.floor = ring, list(docs), tail, floor
        self.alive = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.ring.break_cursors:
            self.ring.break_cursors -= 1
            raise ConnectionError("connection reset")
        if self._docs:
            doc = self._docs.pop(0)
            self.floor = doc["_id"]
            return doc
        if self.tail:  # a tailable cursor picks up whatever arrived since
            fresh = [
                d for d in self.ring.docs if self.floor is None or d["_id"] > self.floor
            ]
            if fresh:
                self._docs = fresh
                return await self.__anext__()
        raise StopAsyncIteration


class Db:
    def __init__(self, ring):
        self.ring = ring
        self.created = []

    def __getitem__(self, name):
        return self.ring

    async def list_collection_names(self):
        return ["dna_events"] if self.created else []

    async def create_collection(self, name, **options):
        self.created.append((name, options))


class Client:
    def __init__(self, ring):
        self.ring = ring
        self.db = Db(ring)

    def __getitem__(self, name):
        return self.db


def make_bus(ring, instance, poll=0.01):
    return MongoEventBus(
        "x", "db", instance_id=instance, client=Client(ring), poll_seconds=poll
    )


async def wait_for(condition, seconds=2.0):
    end = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > end:
            return False
        await asyncio.sleep(0.005)
    return True


async def test_a_message_published_by_one_bus_reaches_the_other():
    ring = Ring()
    got = []
    a, b = make_bus(ring, "A"), make_bus(ring, "B")

    async def noop(m):
        pass

    async def collect(m):
        got.append(m)

    await a.start(noop)
    await b.start(collect)
    await a.publish({"type": "x", "n": 1})
    assert await wait_for(lambda: got)
    assert got == [{"type": "x", "n": 1}]
    await a.stop()
    await b.stop()


async def test_a_bus_does_not_deliver_its_own_messages_back_to_itself():
    ring = Ring()
    got = []
    a = make_bus(ring, "A")

    async def collect(m):
        got.append(m)

    await a.start(collect)
    await a.publish({"n": 1})
    await asyncio.sleep(0.1)
    assert got == []
    await a.stop()


async def test_messages_come_out_in_the_order_they_went_in():
    ring = Ring()
    got = []
    a, b = make_bus(ring, "A"), make_bus(ring, "B")

    async def noop(m):
        pass

    async def collect(m):
        got.append(m["n"])

    await a.start(noop)
    await b.start(collect)
    for n in range(25):
        await a.publish({"n": n})
    assert await wait_for(lambda: len(got) == 25)
    assert got == list(range(25))
    await a.stop()
    await b.stop()


async def test_a_message_published_just_after_start_is_not_missed():
    """Where 'now' is is fixed before start() returns, so there is no gap to fall in."""
    ring = Ring()
    got = []
    a, b = make_bus(ring, "A"), make_bus(ring, "B")

    async def noop(m):
        pass

    async def collect(m):
        got.append(m)

    await a.start(noop)
    await b.start(collect)
    await a.publish({"first": True})  # immediately, before B's task has had a turn
    assert await wait_for(lambda: got)
    await a.stop()
    await b.stop()


async def test_history_before_start_is_not_replayed():
    ring = Ring()
    got = []
    old = make_bus(ring, "A")

    async def noop(m):
        pass

    await old.start(noop)
    await old.publish({"old": True})
    late = make_bus(ring, "B")

    async def collect(m):
        got.append(m)

    await late.start(collect)
    await asyncio.sleep(0.1)
    assert got == []
    await old.stop()
    await late.stop()


async def test_the_ring_is_created_capped_when_it_does_not_exist():
    ring = Ring()
    bus = make_bus(ring, "A")

    async def noop(m):
        pass

    await bus.start(noop)
    ((name, options),) = bus._client.db.created
    assert name == "dna_events" and options["capped"] is True
    assert options["size"] > 0 and options["max"] > 0
    await bus.stop()


async def test_a_failing_handler_does_not_stop_later_messages():
    ring = Ring()
    got = []
    a, b = make_bus(ring, "A"), make_bus(ring, "B")

    async def noop(m):
        pass

    async def flaky(m):
        got.append(m["n"])
        if m["n"] == 1:
            raise RuntimeError("handler bug")

    await a.start(noop)
    await b.start(flaky)
    for n in (1, 2, 3):
        await a.publish({"n": n})
    assert await wait_for(lambda: got == [1, 2, 3])
    await a.stop()
    await b.stop()


async def test_publishing_to_a_dead_bus_does_not_raise_and_marks_it_unhealthy():
    ring = Ring()
    bus = make_bus(ring, "A")

    async def noop(m):
        pass

    await bus.start(noop)
    assert bus.healthy
    ring.fail_inserts = True
    await bus.publish({"n": 1})  # must not raise
    assert bus.healthy is False
    ring.fail_inserts = False
    await bus.publish({"n": 2})
    assert bus.healthy is True
    await bus.stop()


async def test_the_listener_recovers_after_losing_its_connection(monkeypatch):
    real_sleep = asyncio.sleep

    async def quick(seconds):  # do not really wait out the backoff
        await real_sleep(0.001)

    ring = Ring()
    got = []
    a, b = make_bus(ring, "A"), make_bus(ring, "B")

    async def noop(m):
        pass

    async def collect(m):
        got.append(m)

    await a.start(noop)
    await b.start(collect)
    monkeypatch.setattr("dna.events.bus.asyncio.sleep", quick)
    ring.break_cursors = 1  # the open cursor's connection drops...
    ring.fail_find = 2  # ...and the first two attempts to reopen fail too
    assert await wait_for(lambda: ring.break_cursors == 0 and ring.fail_find == 0)
    assert await wait_for(lambda: b.healthy)  # it found its way back
    await a.publish({"after": "outage"})
    assert await wait_for(lambda: got)
    await a.stop()
    await b.stop()


async def test_stop_is_clean_and_idempotent():
    ring = Ring()
    bus = make_bus(ring, "A")

    async def noop(m):
        pass

    await bus.start(noop)
    await bus.stop()
    await bus.stop()
    assert bus.healthy is False
    await bus.publish({"n": 1})  # a stopped bus is simply quiet


async def test_publishing_before_start_is_a_no_op():
    await make_bus(Ring(), "A").publish({"n": 1})


def test_instance_ids_are_unique_per_start():
    assert new_instance_id() != new_instance_id()


# ── the publisher around the bus ─────────────────────────────────────────────


class Browser:
    def __init__(self):
        self.messages = []

    async def accept(self):
        pass

    async def send_text(self, text):
        self.messages.append(json.loads(text))


async def test_two_publishers_share_events_through_one_ring():
    ring = Ring()
    a, b = EventPublisher(), EventPublisher()
    browser_a, browser_b = Browser(), Browser()
    await a.attach_bus(make_bus(ring, "A"))
    await b.attach_bus(make_bus(ring, "B"))
    await a.ws_manager.connect(browser_a)
    await b.ws_manager.connect(browser_b)

    await a.publish(EventType.BOT_STATUS_CHANGED, {"status": "active"})
    assert await wait_for(lambda: browser_b.messages)
    await asyncio.sleep(0.05)
    assert (
        browser_a.messages
        == browser_b.messages
        == [{"type": "bot.status_changed", "payload": {"status": "active"}}]
    )
    await a.close()
    await b.close()


async def test_a_bus_that_fails_to_start_is_not_left_attached():
    class Broken:
        instance_id = "Z"
        healthy = False

        async def start(self, on_message):
            raise ConnectionError("no mongo")

    publisher = EventPublisher()
    with pytest.raises(ConnectionError):
        await publisher.attach_bus(Broken())
    assert publisher.bus is None
    await publisher.broadcast({"type": "x"})  # still works locally


async def test_without_a_bus_events_stay_local():
    publisher = EventPublisher()
    browser = Browser()
    await publisher.ws_manager.connect(browser)
    await publisher.broadcast({"type": "x"})
    assert browser.messages == [{"type": "x"}]


# ── leases, against a stand-in collection ────────────────────────────────────


class LeaseTable:
    """Behaves like the lease collection for exactly the operations the code sends."""

    def __init__(self):
        self.docs = {}

    async def find_one_and_update(self, flt, update, upsert, return_document):
        doc = self.docs.get(flt["_id"])
        if doc is not None:
            mine_or_expired = any(
                (("owner" in c) and doc["owner"] == c["owner"])
                or (("expires" in c) and doc["expires"] < c["expires"]["$lt"])
                for c in flt["$or"]
            )
            if not mine_or_expired:
                raise DuplicateKeyError("held by someone else")
            doc.update(update["$set"])
            return doc
        self.docs[flt["_id"]] = {
            "_id": flt["_id"],
            **update["$set"],
            **update["$setOnInsert"],
        }
        return self.docs[flt["_id"]]

    async def update_one(self, flt, update):
        doc = self.docs.get(flt["_id"])
        hit = doc is not None and doc["owner"] == flt["owner"]
        if hit:
            doc.update(update["$set"])

        class R:
            matched_count = 1 if hit else 0

        return R()

    async def delete_one(self, flt):
        doc = self.docs.get(flt["_id"])
        if doc is not None and doc["owner"] == flt["owner"]:
            del self.docs[flt["_id"]]

    async def find_one(self, flt):
        return self.docs.get(flt["_id"])


def lease(table, name, clock):
    return MeetingLeases(table, name, ttl_seconds=60, clock=lambda: clock[0])


async def test_lease_rules_hold_without_a_database():
    table, clock = LeaseTable(), [1000.0]
    a, b = lease(table, "A", clock), lease(table, "B", clock)
    assert await a.acquire("m") and not await b.acquire("m")
    assert await a.acquire("m")  # the owner may ask again
    assert await a.owner("m") == "A"
    clock[0] += 61  # A went quiet for longer than the lease
    assert await a.owner("m") is None
    assert await b.acquire("m")
    assert not await a.renew("m")  # A finds out it lost the meeting
    assert await b.renew("m")
    await a.release("m")  # not A's to release
    assert await b.owner("m") == "B"
    await b.release("m")
    assert await a.acquire("m")
