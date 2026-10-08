"""The shared revision store against a real MongoDB.

The unit tests use a fake collection; this checks the behaviour that depends on the real
driver and server: the unique ``_id`` that makes concurrent saves race safely, the
projection that keeps snapshots out of listings, and timeouts when the server is gone.

It uses a throwaway database (dropped afterwards) on ``DNA_TEST_MONGODB_URL``
(default ``mongodb://localhost:27017``) and is skipped when no server answers.
"""

import os
import threading
import time
import uuid

import pytest

from dna import configsys
from dna.configsys import StaleRevision, StoreUnavailable
from dna.configsys.store import MongoOverrideStore

URL = os.environ.get("DNA_TEST_MONGODB_URL", "mongodb://localhost:27017")


def _reachable() -> bool:
    try:
        from pymongo import MongoClient

        MongoClient(URL, serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason=f"no MongoDB at {URL}")


@pytest.fixture
def db_name():
    from pymongo import MongoClient

    name = f"dna_test_{uuid.uuid4().hex[:10]}"
    yield name
    MongoClient(URL).drop_database(name)


@pytest.fixture
def store(db_name):
    return MongoOverrideStore(url=URL, db_name=db_name)


def test_revisions_round_trip_with_their_audit_details(store):
    store.append(
        {"features": {"transcript_publish": True}},
        actor="ann",
        note="turn on",
        changed=("features.transcript_publish",),
        details=(
            {"path": "features.transcript_publish", "before": False, "after": True},
        ),
    )
    latest = store.latest()
    assert (latest.version, latest.actor, latest.note, latest.schema) == (
        1,
        "ann",
        "turn on",
        1,
    )
    assert latest.overrides == {"features": {"transcript_publish": True}}
    assert latest.details[0]["after"] is True


def test_a_listing_leaves_the_snapshots_behind_but_a_get_returns_them(store):
    for i in range(3):
        store.append({"n": {"x": i}}, actor="a")
    assert [r.version for r in store.history(10)] == [3, 2, 1]
    assert all(r.overrides == {} for r in store.history(10))  # not fetched
    assert store.get(2).overrides == {"n": {"x": 1}}  # a rollback still gets them whole


def test_a_stale_save_is_refused(store):
    store.append({"a": 1}, actor="a")
    with pytest.raises(StaleRevision):
        store.append({"a": 2}, actor="b", expected_version=0)
    assert store.latest().version == 1


def test_simultaneous_saves_exactly_one_wins(store):
    """Both admins loaded revision 0 and press save together."""
    outcomes = []
    barrier = threading.Barrier(8)

    def save(i):
        barrier.wait()
        try:
            store.append({"who": i}, actor=f"admin{i}", expected_version=0)
            outcomes.append("saved")
        except StaleRevision:
            outcomes.append("stale")

    threads = [threading.Thread(target=save, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("saved") == 1 and outcomes.count("stale") == 7
    assert [r.version for r in store.history(10)] == [
        1
    ]  # no duplicate revision numbers


def test_two_processes_share_changes_through_the_store(db_name, monkeypatch):
    """The real thing: instance A saves, instance B (a fresh process state) sees it."""
    monkeypatch.setenv("DNA_CONFIG_PERSIST", "true")
    monkeypatch.setenv("MONGODB_URL", URL)
    monkeypatch.setenv("MONGODB_DB", db_name)
    configsys.reset_config()
    configsys.apply_overrides({"llm": {"provider": "gemini"}}, actor="A", note="from A")

    configsys.reset_config()  # instance B starts later with its own memory
    assert configsys.get_config().get("llm").provider == "gemini"
    assert configsys.current_revision() == 1
    configsys.reset_config()


def test_an_unreachable_server_fails_fast_with_one_error_type():
    dead = MongoOverrideStore(url="mongodb://127.0.0.1:1", db_name="x")
    started = time.perf_counter()
    with pytest.raises(StoreUnavailable):
        dead.latest()
    assert time.perf_counter() - started < 8  # bounded by the server-selection timeout
