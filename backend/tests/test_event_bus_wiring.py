"""How the application starts (or declines to start) the event bus, and what /health says."""

import os
import uuid
from unittest.mock import MagicMock

import main
import pytest
from fastapi.testclient import TestClient

from dna import configsys
from dna.events import get_event_publisher, reset_event_publisher

URL = os.environ.get("DNA_TEST_MONGODB_URL", "mongodb://localhost:27017")


def _reachable() -> bool:
    try:
        from pymongo import MongoClient

        MongoClient(URL, serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for name in ("DNA_EVENT_BUS", "MONGODB_URL", "MONGODB_DB", "STORAGE_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    configsys.reset_config()
    reset_event_publisher()
    main._event_bus_failed = False
    yield
    main._event_bus_failed = False
    reset_event_publisher()
    configsys.reset_config()


class FakeService:
    def __init__(self):
        self.leases = None

    def enable_leases(self, leases):
        self.leases = leases


async def test_by_default_there_is_no_bus_and_nothing_changes():
    service = FakeService()
    await main._start_event_bus(service)
    assert get_event_publisher().bus is None
    assert service.leases is None and main._event_bus_failed is False


async def test_a_bus_that_cannot_start_does_not_stop_the_app_but_is_reported(
    monkeypatch, caplog
):
    monkeypatch.setenv("DNA_EVENT_BUS", "mongodb")
    monkeypatch.setenv("MONGODB_URL", "mongodb://127.0.0.1:1")
    configsys.reset_config()
    service = FakeService()
    await main._start_event_bus(service)  # must not raise
    assert main._event_bus_failed is True
    assert get_event_publisher().bus is None
    assert service.leases is None  # without a bus, no instance may claim meetings
    assert "event bus could not start" in caplog.text


def test_health_goes_degraded_when_the_configured_bus_is_down(monkeypatch):
    monkeypatch.setattr(main, "_event_bus_failed", True)
    response = TestClient(main.app).get("/health")
    assert response.status_code == 503 and response.json()["events"] is False


def test_health_says_nothing_about_events_when_there_is_no_bus():
    assert "events" not in TestClient(main.app).get("/health").json()


def test_the_bus_needs_mongodb_storage(monkeypatch):
    monkeypatch.setenv("DNA_EVENT_BUS", "mongodb")
    monkeypatch.setenv("STORAGE_PROVIDER", "other")
    configsys.reset_config()
    import asyncio

    service = FakeService()
    asyncio.run(main._start_event_bus(service))
    assert main._event_bus_failed is True


@pytest.mark.skipif(not _reachable(), reason=f"no MongoDB at {URL}")
async def test_with_the_bus_on_instances_share_events_and_meetings(monkeypatch):
    from pymongo import MongoClient

    db = f"dna_test_{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("DNA_EVENT_BUS", "mongodb")
    monkeypatch.setenv("MONGODB_URL", URL)
    monkeypatch.setenv("MONGODB_DB", db)
    configsys.reset_config()
    try:
        service = FakeService()
        await main._start_event_bus(service)
        publisher = get_event_publisher()
        assert main._event_bus_failed is False
        assert publisher.bus is not None and publisher.bus.healthy
        assert service.leases is not None
        assert service.leases.instance_id == publisher.bus.instance_id
        assert TestClient(main.app).get("/health").json()["events"] is True
        await publisher.close()
    finally:
        MongoClient(URL).drop_database(db)


def test_every_browser_message_goes_through_the_publisher():
    """If something broadcast straight to the local WebSocket manager, browsers on other
    instances would never see it.  Only the publisher itself may touch the manager."""
    import pathlib
    import re

    root = pathlib.Path(main.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "event_publisher.py" or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"ws_manager\.broadcast\(", text):
            offenders.append(
                f"{path.relative_to(root)}:{text[: match.start()].count(chr(10)) + 1}"
            )
    assert (
        not offenders
    ), f"broadcast around the publisher (and so the bus): {offenders}"
