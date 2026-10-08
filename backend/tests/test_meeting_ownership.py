"""Exactly one backend instance handles each meeting, and a meeting survives its owner.

Leases are tested against a real MongoDB (race conditions are the point); the service
logic that uses them is tested against a stand-in so each case is deterministic.
"""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from dna.events.lease import MeetingLeases
from dna.transcription_service import TranscriptionService

URL = os.environ.get("DNA_TEST_MONGODB_URL", "mongodb://localhost:27017")


def _reachable() -> bool:
    try:
        from pymongo import MongoClient

        MongoClient(URL, serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except Exception:
        return False


needs_mongo = pytest.mark.skipif(not _reachable(), reason=f"no MongoDB at {URL}")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


# ── the lease itself, on a real MongoDB ──────────────────────────────────────


@pytest.fixture
def leases_for():
    from pymongo import AsyncMongoClient, MongoClient

    name = f"dna_test_{uuid.uuid4().hex[:10]}"
    clients = []

    def make(instance, clock=None, ttl=60.0):
        client = AsyncMongoClient(URL)
        clients.append(client)
        return MeetingLeases(
            client[name]["leases"], instance, ttl_seconds=ttl, clock=clock or Clock()
        )

    yield make
    MongoClient(URL).drop_database(name)


@needs_mongo
async def test_the_first_instance_to_ask_gets_the_meeting(leases_for):
    a, b = leases_for("A"), leases_for("B")
    assert await a.acquire("google_meet:abc") is True
    assert await b.acquire("google_meet:abc") is False
    assert await a.owner("google_meet:abc") == "A"


@needs_mongo
async def test_the_owner_can_ask_again_and_keeps_it(leases_for):
    a = leases_for("A")
    assert await a.acquire("m") and await a.acquire("m")


@needs_mongo
async def test_many_instances_racing_for_one_meeting_exactly_one_wins(leases_for):
    contenders = [leases_for(f"I{i}") for i in range(12)]
    results = await asyncio.gather(*(c.acquire("race") for c in contenders))
    assert results.count(True) == 1
    winner = contenders[results.index(True)].instance_id
    assert await contenders[0].owner("race") == winner


@needs_mongo
async def test_a_lease_that_is_not_renewed_passes_to_another_instance(leases_for):
    clock = Clock()
    a, b = leases_for("A", clock, ttl=60), leases_for("B", clock, ttl=60)
    assert await a.acquire("m")
    clock.now += 30
    assert await b.acquire("m") is False  # still A's
    clock.now += 31  # A has gone quiet for longer than the lease
    assert await b.acquire("m") is True  # B takes over
    assert await a.renew("m") is False  # A learns it lost the meeting
    assert await a.owner("m") == "B"


@needs_mongo
async def test_renewing_keeps_a_meeting_past_the_original_expiry(leases_for):
    clock = Clock()
    a, b = leases_for("A", clock, ttl=60), leases_for("B", clock, ttl=60)
    await a.acquire("m")
    for _ in range(5):
        clock.now += 40
        assert await a.renew("m") is True
        assert await b.acquire("m") is False


@needs_mongo
async def test_a_released_meeting_can_be_taken_at_once(leases_for):
    a, b = leases_for("A"), leases_for("B")
    await a.acquire("m")
    await a.release("m")
    assert await b.acquire("m") is True


@needs_mongo
async def test_only_the_owner_can_release(leases_for):
    a, b = leases_for("A"), leases_for("B")
    await a.acquire("m")
    await b.release("m")  # not B's to give up
    assert await a.owner("m") == "A"


@needs_mongo
async def test_a_stranger_cannot_renew(leases_for):
    a, b = leases_for("A"), leases_for("B")
    await a.acquire("m")
    assert await b.renew("m") is False
    assert await b.renew("never-existed") is False


@needs_mongo
async def test_an_expired_lease_reports_no_owner(leases_for):
    clock = Clock()
    a = leases_for("A", clock, ttl=10)
    await a.acquire("m")
    clock.now += 11
    assert await a.owner("m") is None


# ── the service's use of leases ──────────────────────────────────────────────


class FakeLeases:
    """A stand-in for MeetingLeases: who holds what is set by the test."""

    def __init__(self, held_elsewhere=()):
        self.mine = set()
        self.elsewhere = set(held_elsewhere)
        self.released = []
        self.renew_ok = True

    async def acquire(self, key):
        if key in self.elsewhere:
            return False
        self.mine.add(key)
        return True

    async def renew(self, key):
        return self.renew_ok and key in self.mine

    async def release(self, key):
        self.mine.discard(key)
        self.released.append(key)


@pytest.fixture
def service():
    provider = MagicMock()
    provider.subscribe_to_meeting = AsyncMock()
    provider.unsubscribe_from_meeting = AsyncMock()
    provider.get_active_bots = AsyncMock(return_value=[])
    provider.register_meeting_id_mapping = MagicMock()
    provider.close = AsyncMock()
    storage = MagicMock()
    storage.get_playlist_metadata = AsyncMock(return_value=None)
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    publisher.broadcast = AsyncMock()
    svc = TranscriptionService(provider, storage, publisher)
    return svc


def bot(meeting, status="active"):
    return {"platform": "google_meet", "native_meeting_id": meeting, "status": status}


def metadata():
    m = MagicMock()
    m.playlist_id = "p1"
    m.vexa_meeting_id = 7
    return m


async def test_a_single_instance_handles_every_meeting_as_before(service):
    await service.subscribe_to_meeting("google_meet", "abc", "p1")
    service.transcription_provider.subscribe_to_meeting.assert_awaited_once()
    assert "google_meet:abc" in service._subscribed_meetings


async def test_an_instance_leaves_a_meeting_another_instance_holds(service):
    service.enable_leases(FakeLeases(held_elsewhere={"google_meet:abc"}))
    await service.subscribe_to_meeting("google_meet", "abc", "p1")
    service.transcription_provider.subscribe_to_meeting.assert_not_awaited()
    assert "google_meet:abc" not in service._subscribed_meetings


async def test_an_instance_takes_a_free_meeting_and_holds_the_lease(service):
    leases = FakeLeases()
    service.enable_leases(leases)
    await service.subscribe_to_meeting("google_meet", "abc", "p1")
    service.transcription_provider.subscribe_to_meeting.assert_awaited_once()
    assert "google_meet:abc" in leases.mine


async def test_a_lease_that_cannot_be_checked_is_not_assumed(service):
    class Broken(FakeLeases):
        async def acquire(self, key):
            raise ConnectionError("mongo is down")

    service.enable_leases(Broken())
    await service.subscribe_to_meeting("google_meet", "abc", "p1")
    service.transcription_provider.subscribe_to_meeting.assert_not_awaited()


async def test_reconcile_adopts_a_meeting_whose_owner_has_gone(service):
    """Instance A died; its lease expired; this instance picks the meeting up."""
    service.enable_leases(FakeLeases())
    service.transcription_provider.get_active_bots.return_value = [bot("abc")]
    service.storage_provider.get_playlist_metadata_by_meeting_id = AsyncMock(
        return_value=metadata()
    )
    await service.reconcile()
    service.transcription_provider.subscribe_to_meeting.assert_awaited_once()
    assert "google_meet:abc" in service._subscribed_meetings


async def test_reconcile_leaves_a_meeting_that_is_still_owned_elsewhere(service):
    service.enable_leases(FakeLeases(held_elsewhere={"google_meet:abc"}))
    service.transcription_provider.get_active_bots.return_value = [bot("abc")]
    service.storage_provider.get_playlist_metadata_by_meeting_id = AsyncMock(
        return_value=metadata()
    )
    await service.reconcile()
    service.transcription_provider.subscribe_to_meeting.assert_not_awaited()


async def test_reconcile_does_not_resubscribe_to_what_it_already_handles(service):
    leases = FakeLeases()
    service.enable_leases(leases)
    await service.subscribe_to_meeting("google_meet", "abc", "p1")
    service.transcription_provider.subscribe_to_meeting.reset_mock()
    service.transcription_provider.get_active_bots.return_value = [bot("abc")]
    await service.reconcile()
    service.transcription_provider.subscribe_to_meeting.assert_not_awaited()


async def test_reconcile_stops_handling_a_meeting_whose_lease_was_lost(service):
    leases = FakeLeases()
    service.enable_leases(leases)
    await service.subscribe_to_meeting("google_meet", "abc", "p1")
    leases.renew_ok = False  # another instance took it while we were stalled
    await service.reconcile()
    assert "google_meet:abc" not in service._subscribed_meetings
    service.transcription_provider.unsubscribe_from_meeting.assert_awaited_once_with(
        platform="google_meet", meeting_id="abc"
    )


async def test_a_failed_renewal_is_retried_not_treated_as_a_loss(service):
    class Flaky(FakeLeases):
        async def renew(self, key):
            raise ConnectionError("blip")

    service.enable_leases(Flaky())
    await service.subscribe_to_meeting("google_meet", "abc", "p1")
    await service.reconcile()
    assert "google_meet:abc" in service._subscribed_meetings  # still ours


async def test_a_finished_meeting_releases_its_lease(service):
    leases = FakeLeases()
    service.enable_leases(leases)
    await service.subscribe_to_meeting("google_meet", "abc", "p1")
    await service.on_transcription_completed(
        {"platform": "google_meet", "meeting_id": "abc"}
    )
    assert leases.released == ["google_meet:abc"]


async def test_shutting_down_hands_meetings_back_for_immediate_takeover(service):
    leases = FakeLeases()
    service.enable_leases(leases)
    await service.subscribe_to_meeting("google_meet", "abc", "p1")
    await service.subscribe_to_meeting("google_meet", "def", "p2")
    await service.close()
    assert sorted(leases.released) == ["google_meet:abc", "google_meet:def"]


async def test_the_background_loop_runs_and_stops_cleanly(service):
    leases = FakeLeases()
    service.enable_leases(leases)
    service.reconcile_seconds = 0.02
    calls = []
    real = service.reconcile

    async def counting():
        calls.append(1)
        await real()

    service.reconcile = counting
    await service.start_reconciling()
    await asyncio.sleep(0.15)
    await service.close()
    assert len(calls) >= 3 and service._reconcile_task is None


async def test_without_leases_there_is_no_background_loop(service):
    await service.start_reconciling()
    assert service._reconcile_task is None


async def test_transcripts_go_out_through_the_publisher_so_other_instances_get_them(
    service,
):
    """The browser-facing broadcast must go via the publisher (and so the bus), not
    straight to this instance's WebSocket clients."""
    meta = MagicMock()
    meta.in_review = "v1"
    meta.transcription_paused = False
    meta.transcription_resumed_at = None
    service.storage_provider.get_playlist_metadata = AsyncMock(return_value=meta)
    service._meeting_to_playlist["google_meet:abc"] = "p1"
    await service.on_transcription_updated(
        {
            "platform": "google_meet",
            "meeting_id": "abc",
            "speaker": "Ann",
            "confirmed": [],
            "pending": [],
            "ts": 1.0,
        }
    )
    service.event_publisher.broadcast.assert_awaited_once()
    assert service.event_publisher.broadcast.await_args.args[0]["type"] == "transcript"


async def test_no_lease_is_taken_for_a_meeting_that_has_finished(service):
    """Reconciling runs every few seconds on every instance: it must not litter the
    lease table with finished meetings."""
    leases = FakeLeases()
    service.enable_leases(leases)
    service.transcription_provider.get_active_bots.return_value = [
        bot("done1", "completed"),
        bot("done2", "failed"),
        bot("done3", "stopped"),
    ]
    service.storage_provider.get_playlist_metadata_by_meeting_id = AsyncMock(
        return_value=metadata()
    )
    await service.reconcile()
    assert leases.mine == set()


async def test_no_lease_is_taken_for_a_meeting_with_no_playlist(service):
    leases = FakeLeases()
    service.enable_leases(leases)
    service.transcription_provider.get_active_bots.return_value = [bot("orphan")]
    service.storage_provider.get_playlist_metadata_by_meeting_id = AsyncMock(
        return_value=None
    )
    await service.reconcile()
    assert leases.mine == set()
