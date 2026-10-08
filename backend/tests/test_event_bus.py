"""Live events across backend instances, against a real MongoDB.

Each ``EventPublisher`` below stands for one backend instance with its own browsers (fake
WebSockets).  What matters is what a browser sees: every event exactly once, in order,
no matter which instance raised it.

Uses a throwaway database on ``DNA_TEST_MONGODB_URL`` (default
``mongodb://localhost:27017``); skipped when no server answers.
"""

import asyncio
import json
import os
import uuid

import pytest

from dna.events import EventPublisher, EventType
from dna.events.bus import MongoEventBus

URL = os.environ.get("DNA_TEST_MONGODB_URL", "mongodb://localhost:27017")


def _reachable() -> bool:
    try:
        from pymongo import MongoClient

        MongoClient(URL, serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason=f"no MongoDB at {URL}")


class Browser:
    """A WebSocket client: records what the server sends it."""

    def __init__(self):
        self.messages: list[dict] = []

    async def accept(self):
        pass

    async def send_text(self, text):
        self.messages.append(json.loads(text))


@pytest.fixture
def db_name():
    from pymongo import MongoClient

    name = f"dna_test_{uuid.uuid4().hex[:10]}"
    yield name
    MongoClient(URL).drop_database(name)


async def instance(db_name, name) -> tuple[EventPublisher, Browser]:
    """One backend instance with one connected browser."""
    publisher = EventPublisher()
    await publisher.attach_bus(MongoEventBus(URL, db_name, instance_id=name))
    browser = Browser()
    await publisher.ws_manager.connect(browser)
    return publisher, browser


async def until(condition, seconds=5.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def settle(seconds=0.4):
    """Long enough for a duplicate or an echo to show up, if there were going to be one."""
    await asyncio.sleep(seconds)


@pytest.fixture
async def stop_all():
    publishers = []
    yield publishers
    for p in publishers:
        await p.close()


async def test_an_event_raised_on_one_instance_reaches_browsers_on_another(
    db_name, stop_all
):
    a, browser_a = await instance(db_name, "A")
    b, browser_b = await instance(db_name, "B")
    stop_all.extend([a, b])

    await a.publish(
        EventType.BOT_STATUS_CHANGED, {"meeting_id": "m1", "status": "active"}
    )

    assert await until(lambda: browser_b.messages)
    assert browser_b.messages == [
        {
            "type": "bot.status_changed",
            "payload": {"meeting_id": "m1", "status": "active"},
        }
    ]


async def test_the_raising_instances_own_browser_gets_it_exactly_once(
    db_name, stop_all
):
    a, browser_a = await instance(db_name, "A")
    b, _ = await instance(db_name, "B")
    stop_all.extend([a, b])
    await a.publish(EventType.BOT_STATUS_CHANGED, {"n": 1})
    await settle()
    assert len(browser_a.messages) == 1  # directly; its own bus echo is ignored


async def test_a_flat_transcript_frame_crosses_instances_unchanged(db_name, stop_all):
    """The transcript envelope is not wrapped in {type, payload}; it must arrive as is."""
    a, _ = await instance(db_name, "A")
    b, browser_b = await instance(db_name, "B")
    stop_all.extend([a, b])
    frame = {
        "type": "transcript",
        "speaker": "Ann",
        "confirmed": [{"segment_id": "s1", "text": "hello"}],
        "pending": [],
        "playlist_id": "dna_test:p1",
        "version_id": "dna_test:v1",
        "ts": 12.5,
    }
    await a.broadcast(frame)
    assert await until(lambda: browser_b.messages)
    assert browser_b.messages == [frame]


async def test_events_arrive_in_the_order_they_were_raised(db_name, stop_all):
    a, _ = await instance(db_name, "A")
    b, browser_b = await instance(db_name, "B")
    stop_all.extend([a, b])
    for i in range(40):
        await a.broadcast({"type": "transcript", "seq": i})
    assert await until(lambda: len(browser_b.messages) == 40)
    assert [m["seq"] for m in browser_b.messages] == list(range(40))


async def test_three_instances_every_browser_sees_every_event_once(db_name, stop_all):
    pubs = []
    browsers = []
    for name in ("A", "B", "C"):
        pub, browser = await instance(db_name, name)
        pubs.append(pub)
        browsers.append(browser)
    stop_all.extend(pubs)

    for i, pub in enumerate(pubs):  # each instance raises two events
        await pub.broadcast({"type": "x", "from": i, "n": 0})
        await pub.broadcast({"type": "x", "from": i, "n": 1})

    assert await until(lambda: all(len(b.messages) == 6 for b in browsers))
    await settle()
    for browser in browsers:
        assert len(browser.messages) == 6  # no duplicates, no echoes
        seen = {(m["from"], m["n"]) for m in browser.messages}
        assert seen == {(f, n) for f in range(3) for n in range(2)}


async def test_a_remote_event_is_not_sent_back_to_the_bus(db_name, stop_all):
    """If instances re-published what they received, one event would loop forever."""
    a, _ = await instance(db_name, "A")
    b, browser_b = await instance(db_name, "B")
    stop_all.extend([a, b])
    await a.broadcast({"type": "x"})
    assert await until(lambda: browser_b.messages)
    await settle()

    from pymongo import MongoClient

    docs = list(
        MongoClient(URL)[db_name]["dna_events"].find({"i": {"$ne": "__marker__"}})
    )
    assert len(docs) == 1 and docs[0]["i"] == "A"


async def test_events_from_before_an_instance_started_are_not_replayed(
    db_name, stop_all
):
    a, _ = await instance(db_name, "A")
    stop_all.append(a)
    await a.broadcast({"type": "old"})
    await settle(0.2)

    b, browser_b = await instance(db_name, "B")  # starts later
    stop_all.append(b)
    await a.broadcast({"type": "new"})
    assert await until(lambda: browser_b.messages)
    await settle()
    assert [m["type"] for m in browser_b.messages] == ["new"]


async def test_remote_events_do_not_run_local_subscribers(db_name, stop_all):
    """The raising instance already ran its subscribers; running them again would
    do the work twice."""
    a, _ = await instance(db_name, "A")
    b, browser_b = await instance(db_name, "B")
    stop_all.extend([a, b])
    ran = []

    async def callback(event_type, payload):
        ran.append(("B", event_type))

    b.subscribe_all(callback)
    await a.publish(EventType.BOT_STATUS_CHANGED, {"n": 1})
    assert await until(lambda: browser_b.messages)
    await settle()
    assert ran == []


async def test_an_instance_that_leaves_stops_receiving_and_the_rest_carry_on(
    db_name, stop_all
):
    a, _ = await instance(db_name, "A")
    b, browser_b = await instance(db_name, "B")
    c, browser_c = await instance(db_name, "C")
    stop_all.extend([a, c])
    await b.close()  # B goes away
    await a.broadcast({"type": "after"})
    assert await until(lambda: browser_c.messages)
    await settle()
    assert [m["type"] for m in browser_c.messages] == ["after"]
    assert browser_b.messages == []


async def test_a_second_instance_starting_at_the_same_time_is_fine(db_name, stop_all):
    """Both try to create the ring on a fresh database."""
    pubs = await asyncio.gather(*(instance(db_name, f"I{i}") for i in range(5)))
    stop_all.extend(p for p, _ in pubs)
    await pubs[0][0].broadcast({"type": "hello"})
    assert await until(lambda: all(b.messages for _, b in pubs[1:]))


async def test_the_ring_is_capped_so_it_cannot_grow_without_bound(db_name, stop_all):
    from pymongo import MongoClient

    a, _ = await instance(db_name, "A")
    stop_all.append(a)
    options = MongoClient(URL)[db_name]["dna_events"].options()
    assert options.get("capped") is True and options.get("size")


async def test_healthy_follows_the_connection(db_name, stop_all):
    a, _ = await instance(db_name, "A")
    stop_all.append(a)
    assert a.bus.healthy is True
    await a.close()
    assert a.bus is None


async def test_a_bus_that_cannot_reach_mongodb_fails_at_startup():
    bad = MongoEventBus("mongodb://127.0.0.1:1", "x", instance_id="Z")
    with pytest.raises(Exception):
        await bad.start(lambda m: asyncio.sleep(0))
    assert bad.healthy is False


async def test_publishing_never_raises_even_when_the_bus_is_down(db_name):
    """Events are best effort: a dead bus must not turn a request into an error."""

    class Down:
        async def insert_one(self, *a, **k):
            raise ConnectionError("mongo is down")

    bus = MongoEventBus(URL, db_name, instance_id="A")
    bus._collection = Down()
    await bus.publish({"type": "x"})  # must not raise
    assert bus.healthy is False


async def test_delivery_stays_fast_as_instances_are_added(db_name, stop_all):
    """Guards a measured problem: waiting on the server for news delivered in seconds
    once several instances were listening, getting slower with each one added."""
    import time

    pubs, browsers = [], []
    for i in range(6):
        pub, browser = await instance(db_name, f"I{i}")
        pubs.append(pub)
        browsers.append(browser)
    stop_all.extend(pubs)

    worst = 0.0
    for n in range(8):
        started = time.perf_counter()
        await pubs[n % 6].broadcast({"type": "x", "n": n})
        # every browser, including the raising instance's own, has seen n + 1 events
        assert await until(
            lambda: all(len(b.messages) >= n + 1 for b in browsers), seconds=3
        )
        worst = max(worst, time.perf_counter() - started)
        await asyncio.sleep(0.1)
    assert worst < 0.5, f"slowest delivery to every browser: {worst:.2f}s"
