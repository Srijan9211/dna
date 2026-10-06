"""Revision history, rollback, import/export, and sharing between instances."""

import time

import pytest

from dna import configsys
from dna.configsys import ConfigError, StaleRevision
from dna.configsys.store import MemoryOverrideStore, MongoOverrideStore, Revision


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    configsys.reset_config()
    yield
    configsys.reset_config()


# ── the stores ───────────────────────────────────────────────────────────────


class FakeMongoCollection:
    """Just enough pymongo: unique _id, sorted find, find_one."""

    def __init__(self):
        self.docs = {}

    def find(self, query, sort=None, limit=0):
        docs = sorted(self.docs.values(), key=lambda d: d["version"], reverse=True)
        return iter(docs[:limit] if limit else docs)

    def find_one(self, query):
        return self.docs.get(query["_id"])

    def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError

        if doc["_id"] in self.docs:
            raise DuplicateKeyError("dup")
        self.docs[doc["_id"]] = doc


@pytest.fixture(params=["memory", "mongo"])
def store(request):
    return MemoryOverrideStore() if request.param == "memory" else MongoOverrideStore(FakeMongoCollection())


def test_versions_increase_and_history_is_newest_first(store):
    assert store.latest() is None and store.history() == []
    a = store.append({"x": 1}, actor="ann", note="first", changed=("a.b",))
    b = store.append({"x": 2}, actor="bob")
    assert (a.version, b.version) == (1, 2)
    assert store.latest().overrides == {"x": 2}
    assert [r.version for r in store.history()] == [2, 1]
    assert store.get(1).actor == "ann" and store.get(1).changed == ("a.b",)
    assert store.get(99) is None
    assert [r.version for r in store.history(limit=1)] == [2]


def test_snapshots_are_isolated_from_later_mutation(store):
    data = {"features": {"transcript_publish": True}}
    store.append(data, actor="ann")
    data["features"]["transcript_publish"] = False
    assert store.latest().overrides == {"features": {"transcript_publish": True}}


def test_a_stale_writer_is_refused(store):
    store.append({"x": 1}, actor="ann")
    with pytest.raises(StaleRevision):
        store.append({"x": 2}, actor="bob", expected_version=0)
    assert store.append({"x": 2}, actor="bob", expected_version=1).version == 2


def test_two_writers_racing_for_the_same_version_cannot_both_win():
    collection = FakeMongoCollection()
    a, b = MongoOverrideStore(collection), MongoOverrideStore(collection)
    a.append({"x": 1}, actor="a")
    # both read version 1, then both try to write version 2
    original = collection.find
    collection.find = lambda *args, **kw: iter([collection.docs[1]])  # b's stale read
    with pytest.raises(StaleRevision):
        a.append({"x": 2}, actor="a")  # wins ... then:
        b.append({"x": 3}, actor="b")  # the same _id already exists
    collection.find = original
    assert collection.docs[2]["actor"] == "a"


def test_revision_round_trips_through_a_document():
    revision = Revision(3, {"a": {"b": 1}}, "ann", 12.5, "note", ("a.b",))
    assert Revision.from_doc(revision.to_doc()) == revision
    assert revision.summary() == {"version": 3, "actor": "ann", "at": 12.5, "note": "note", "changed": ["a.b"]}


# ── applying, recording, rolling back ────────────────────────────────────────


def test_a_change_is_recorded_with_who_when_and_what():
    result = configsys.apply_overrides(
        {"features": {"transcript_publish": True}}, actor="ann@x.com", note="enable publishing"
    )
    assert result.version == 1 and result.changed == ["features.transcript_publish"]
    (rev,) = configsys.history()
    assert (rev.version, rev.actor, rev.note) == (1, "ann@x.com", "enable publishing")
    assert rev.changed == ("features.transcript_publish",) and rev.at <= time.time()
    assert configsys.get_config().get("features").transcript_publish is True
    assert configsys.current_revision() == 1


def test_a_reference_to_an_unset_variable_is_refused_not_saved():
    with pytest.raises(ConfigError, match="AYON_API_KEY"):
        configsys.apply_overrides({"prodtrack": {"ayon": {"api_key": "${env:AYON_API_KEY}"}}}, actor="ann")
    assert configsys.history() == []


def test_an_invalid_change_is_not_recorded():
    with pytest.raises(ConfigError):
        configsys.apply_overrides({"features": {"transcript_publish": "banana"}}, actor="ann")
    assert configsys.history() == [] and configsys.current_revision() == 0


def test_rollback_reapplies_an_old_revision_as_a_new_one():
    configsys.apply_overrides({"features": {"transcript_publish": True}}, actor="ann")
    configsys.apply_overrides({"llm": {"provider": "gemini"}}, actor="ann")
    cfg = configsys.get_config()
    assert cfg.get("llm").provider == "gemini" and cfg.get("features").transcript_publish is True

    result = configsys.rollback(1, actor="bob")
    assert result.version == 3  # history is never rewritten
    cfg = configsys.get_config()
    assert cfg.get("llm").provider == "openai"  # the second change is undone
    assert cfg.get("features").transcript_publish is True  # the first remains
    assert [(r.version, r.actor) for r in configsys.history()] == [(3, "bob"), (2, "ann"), (1, "ann")]
    assert configsys.history()[0].note == "rollback to revision 1"
    with pytest.raises(ValueError, match="No config revision 99"):
        configsys.rollback(99)


def test_an_expected_version_stops_a_stale_overwrite():
    configsys.apply_overrides({"llm": {"provider": "gemini"}}, actor="ann")
    with pytest.raises(StaleRevision):
        configsys.apply_overrides({"llm": {"provider": "anthropic"}}, actor="bob", expected_version=0)
    assert configsys.get_config().get("llm").provider == "gemini"
    configsys.apply_overrides({"llm": {"provider": "anthropic"}}, actor="bob", expected_version=1)


def test_export_and_import_move_overrides_between_environments():
    configsys.apply_overrides({"features": {"transcript_publish": True}}, actor="ann")
    exported = configsys.export_overrides()
    assert exported == {"format": 1, "revision": 1, "overrides": {"features": {"transcript_publish": True}}}

    configsys.reset_config()  # a fresh environment
    result = configsys.import_overrides(exported, actor="ci")
    assert result.changed == ["features.transcript_publish"]
    assert configsys.history()[0].note == "import"
    with pytest.raises(ValueError, match="Not a DNA config export"):
        configsys.import_overrides({"overrides": {}}, actor="ci")


def test_import_is_validated_like_any_change():
    with pytest.raises(ConfigError, match="cannot be changed at runtime"):
        configsys.import_overrides({"format": 1, "overrides": {"auth": {"provider": "none"}}}, actor="x")


def test_secrets_never_enter_the_history(monkeypatch):
    monkeypatch.setenv("AYON_API_KEY", "the-real-key")
    with pytest.raises(ConfigError):
        configsys.apply_overrides({"prodtrack": {"ayon": {"api_key": "plaintext-key"}}}, actor="ann")
    configsys.apply_overrides({"prodtrack": {"ayon": {"api_key": "${env:AYON_API_KEY}"}}}, actor="ann")
    assert "plaintext-key" not in repr(configsys.history())
    assert "the-real-key" not in repr(configsys.history())  # the reference is stored, not its value
    assert configsys.history()[0].overrides["prodtrack"]["ayon"]["api_key"] == "${env:AYON_API_KEY}"


# ── instances sharing one store ──────────────────────────────────────────────


def test_a_second_instance_picks_up_a_change_made_by_the_first(monkeypatch):
    """Two processes, one MongoDB: a change on A reaches B within refresh_seconds."""
    from dna.configsys import store as store_module

    shared = MemoryOverrideStore()
    monkeypatch.setenv("DNA_CONFIG_PERSIST", "true")
    monkeypatch.setattr(configsys, "_get_store", lambda service: shared)

    # instance A saves
    configsys.get_config()
    configsys.apply_overrides({"features": {"transcript_publish": True}}, actor="A")

    # instance B starts later against the same store, with its own state
    configsys.reset_config()
    assert configsys.get_config().get("features").transcript_publish is True
    assert configsys.current_revision() == 1

    # A saves again; B sees it once its refresh interval has passed
    shared.append({"features": {"transcript_publish": True}, "llm": {"provider": "gemini"}}, actor="A")
    assert configsys.get_config().get("llm").provider == "openai"  # not yet polled
    configsys._checked_at = 0.0  # the interval elapses
    assert configsys.get_config().get("llm").provider == "gemini"
    assert configsys.current_revision() == 2


def test_a_revision_that_no_longer_validates_does_not_take_the_service_down(monkeypatch, caplog):
    shared = MemoryOverrideStore()
    shared.append({"features": {"transcript_publish": "banana"}}, actor="old")  # bad on arrival
    monkeypatch.setenv("DNA_CONFIG_PERSIST", "true")
    monkeypatch.setattr(configsys, "_get_store", lambda service: shared)
    cfg = configsys.get_config()  # must not raise
    assert cfg.get("features").transcript_publish is False  # files + env only
    assert "could not be applied" in caplog.text


def test_an_unreachable_store_keeps_the_current_config(monkeypatch, caplog):
    class Down:
        def latest(self):
            raise ConnectionError("mongo is down")

    monkeypatch.setenv("DNA_CONFIG_PERSIST", "true")
    monkeypatch.setattr(configsys, "_get_store", lambda service: Down())
    assert configsys.get_config().get("llm").provider == "openai"
    assert "store unavailable" in caplog.text
