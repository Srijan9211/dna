"""How the configuration behaves when things go wrong.

The happy path (apply, record, roll back) is covered in test_config_store.py.  This file
is about the failures a production deployment meets: the shared store goes away, a
stored revision stops validating after an upgrade, a config file is edited badly, a
secret is rotated, two administrators race.
"""

import threading
import time

import pytest

from dna import configsys
from dna.configsys import ConfigError, StaleRevision, StoreUnavailable, migrations
from dna.configsys import store as store_module
from dna.configsys.store import MemoryOverrideStore, MongoOverrideStore


class FakeMongoCollection:
    """Just enough pymongo: unique _id, sorted find with projection, find_one."""

    def __init__(self):
        self.docs = {}

    def find(self, query, sort=None, limit=0, projection=None):
        docs = sorted(self.docs.values(), key=lambda d: d["version"], reverse=True)
        docs = docs[:limit] if limit else docs
        if projection:
            docs = [{k: v for k, v in d.items() if projection.get(k, 1)} for d in docs]
        return iter(docs)

    def find_one(self, query):
        return self.docs.get(query["_id"])

    def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError

        if doc["_id"] in self.docs:
            raise DuplicateKeyError("dup")
        self.docs[doc["_id"]] = doc


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    configsys.reset_config()
    yield
    configsys.reset_config()


@pytest.fixture
def shared(monkeypatch):
    """Persistence on, with an in-memory store standing in for the shared MongoDB."""
    store = MemoryOverrideStore()
    monkeypatch.setenv("DNA_CONFIG_PERSIST", "true")
    monkeypatch.setattr(configsys, "_get_store", lambda service: store)
    return store


def publish_on(value=True):
    return {"features": {"transcript_publish": value}}


# ── a stored revision that no longer validates ───────────────────────────────


def test_an_unusable_newest_revision_falls_back_to_the_last_good_one(shared):
    shared.append(publish_on(True), actor="a", note="good")
    shared.append(
        {**publish_on(True), "features": {"transcript_publish": "banana"}}, actor="b"
    )
    cfg = configsys.get_config()
    assert cfg.get("features").transcript_publish is True  # revision 1 still applies
    status = configsys.config_status()
    assert status["ok"] is False
    assert "revision 2 cannot be applied" in status["problems"][0]
    assert "running on the last valid revision, 1" in status["problems"][0]
    assert (status["running_revision"], status["latest_revision"]) == (1, 2)


def test_when_no_revision_is_usable_it_runs_on_files_and_environment(shared):
    shared.append({"features": {"transcript_publish": "banana"}}, actor="a")
    assert configsys.get_config().get("features").transcript_publish is False
    status = configsys.config_status()
    assert (
        status["ok"] is False
        and "no earlier revision is usable" in status["problems"][0]
    )


def test_saving_a_good_revision_clears_the_problem(shared):
    shared.append(publish_on(True), actor="a")
    shared.append({"features": {"transcript_publish": "banana"}}, actor="b")
    configsys.get_config()
    assert configsys.config_status()["ok"] is False

    configsys.apply_overrides(publish_on(False), actor="admin", note="repair")
    status = configsys.config_status()
    assert (
        status["ok"] is True
        and status["running_revision"] == status["latest_revision"] == 3
    )


def test_a_newer_revision_is_not_retried_every_poll(shared, caplog):
    shared.append({"features": {"transcript_publish": "banana"}}, actor="a")
    configsys.get_config()
    caplog.clear()
    for _ in range(3):
        configsys._checked_at = 0.0
        configsys.get_config()
    assert "cannot be applied" not in caplog.text  # judged once, not on every poll


# ── schema versions and migrations ───────────────────────────────────────────


def test_a_revision_from_a_newer_dna_is_refused_not_misread(shared):
    shared.append(publish_on(True), actor="a")
    newer = shared.append(publish_on(False), actor="b")
    object.__setattr__(newer, "schema", store_module.CURRENT_SCHEMA + 1)
    configsys.get_config()
    status = configsys.config_status()
    assert status["ok"] is False and "newer DNA" in status["problems"][0]
    assert configsys.get_config().get("features").transcript_publish is True


def test_an_old_revision_is_migrated_when_it_is_read(shared, monkeypatch):
    """A later DNA renames features.publish_transcripts -> features.transcript_publish."""
    old = shared.append({"features": {"publish_transcripts": True}}, actor="a")
    assert old.schema == 1

    def rename(overrides):
        features = dict(overrides.get("features", {}))
        if "publish_transcripts" in features:
            features["transcript_publish"] = features.pop("publish_transcripts")
        return {**overrides, "features": features}

    monkeypatch.setattr(store_module, "CURRENT_SCHEMA", 2)
    monkeypatch.setitem(migrations.MIGRATIONS, 1, rename)
    assert configsys.get_config().get("features").transcript_publish is True
    assert configsys.config_status()["ok"] is True


def test_a_missing_migration_is_reported_not_guessed(shared, monkeypatch):
    shared.append(publish_on(True), actor="a")
    monkeypatch.setattr(store_module, "CURRENT_SCHEMA", 3)
    configsys.get_config()
    assert "no migration from schema 1" in configsys.config_status()["problems"][0]


def test_a_failing_migration_is_reported(shared, monkeypatch):
    shared.append(publish_on(True), actor="a")
    monkeypatch.setattr(store_module, "CURRENT_SCHEMA", 2)
    monkeypatch.setitem(migrations.MIGRATIONS, 1, lambda o: 1 / 0)
    configsys.get_config()
    assert "migration from schema 1 failed" in configsys.config_status()["problems"][0]


def test_rollback_migrates_the_revision_it_restores(shared, monkeypatch):
    shared.append({"features": {"publish_transcripts": True}}, actor="a")
    monkeypatch.setattr(store_module, "CURRENT_SCHEMA", 2)
    monkeypatch.setitem(
        migrations.MIGRATIONS,
        1,
        lambda o: {
            "features": {"transcript_publish": o["features"]["publish_transcripts"]}
        },
    )
    configsys.get_config()
    configsys.apply_overrides(publish_on(False), actor="b")
    configsys.rollback(1, actor="c")
    assert configsys.get_config().get("features").transcript_publish is True


# ── the shared store goes away ───────────────────────────────────────────────


class FlakyStore(MemoryOverrideStore):
    """A store that can be switched off, optionally with a slow, hanging failure."""

    def __init__(self):
        super().__init__()
        self.down = False
        self.delay = 0.0
        self.calls = 0

    def latest(self):
        self.calls += 1
        if self.down:
            time.sleep(self.delay)
            raise StoreUnavailable("mongo is down")
        return super().latest()


@pytest.fixture
def flaky(monkeypatch):
    store = FlakyStore()
    monkeypatch.setenv("DNA_CONFIG_PERSIST", "true")
    monkeypatch.setenv("DNA_CONFIG_REFRESH_SECONDS", "0.05")
    monkeypatch.setattr(configsys, "_get_store", lambda service: store)
    return store


def test_an_unreachable_store_keeps_the_current_config_and_says_so(flaky):
    flaky.append(publish_on(True), actor="a")
    assert configsys.get_config().get("features").transcript_publish is True
    flaky.down = True
    configsys._checked_at = 0.0
    assert configsys.get_config().get("features").transcript_publish is True
    status = configsys.config_status()
    assert status["ok"] is False and "unreachable" in status["problems"][0]


def test_recovery_is_noticed_and_picks_up_what_was_missed(flaky):
    flaky.append(publish_on(True), actor="a")
    configsys.get_config()
    flaky.down = True
    configsys._checked_at = 0.0
    configsys.get_config()
    flaky.down = False
    flaky.append(
        {"features": {"transcript_publish": True}, "llm": {"provider": "gemini"}},
        actor="b",
    )
    configsys._checked_at = 0.0
    assert configsys.get_config().get("llm").provider == "gemini"
    assert configsys.config_status()["ok"] is True


def test_a_slow_dead_store_never_makes_get_config_wait(flaky, monkeypatch):
    """The failure that used to freeze request handling: every refresh blocked the
    caller for the store's full timeout.  Refresh now happens off the request path."""
    monkeypatch.setattr(configsys, "BACKGROUND_REFRESH", True)
    flaky.append(publish_on(True), actor="a")
    configsys.get_config()
    flaky.down, flaky.delay = True, 0.4
    slowest = 0.0
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        started = time.perf_counter()
        configsys.get_config().get("features")
        slowest = max(slowest, time.perf_counter() - started)
        time.sleep(0.01)
    assert slowest < 0.1, f"get_config() waited {slowest:.2f}s on a dead store"
    assert flaky.calls >= 2  # it did keep trying, in the background


def test_failed_refreshes_back_off_instead_of_hammering_the_store(flaky, monkeypatch):
    monkeypatch.setattr(configsys, "BACKGROUND_REFRESH", True)
    flaky.append(publish_on(True), actor="a")
    configsys.get_config()
    flaky.down = True
    before = flaky.calls
    time.sleep(1.0)
    configsys.get_config()
    # 1s at a 50 ms interval would be ~20 tries; backing off keeps it to a handful
    assert flaky.calls - before <= 8


def test_the_background_thread_picks_up_a_change_without_any_request(
    flaky, monkeypatch
):
    monkeypatch.setattr(configsys, "BACKGROUND_REFRESH", True)
    configsys.get_config()  # starts the thread
    flaky.append({"llm": {"provider": "gemini"}}, actor="other instance")
    deadline = time.monotonic() + 3
    while (
        time.monotonic() < deadline
        and configsys._service.get("llm").provider != "gemini"
    ):
        time.sleep(0.02)
    assert configsys._service.get("llm").provider == "gemini"


def test_reset_stops_the_background_thread(flaky, monkeypatch):
    monkeypatch.setattr(configsys, "BACKGROUND_REFRESH", True)
    configsys.get_config()
    thread = configsys._refresher
    assert thread.is_alive()
    configsys.reset_config()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_a_save_while_the_store_is_down_fails_cleanly_and_changes_nothing(monkeypatch):
    class Dead(MemoryOverrideStore):
        def append(self, *a, **k):
            raise StoreUnavailable("mongo is down")

    monkeypatch.setenv("DNA_CONFIG_PERSIST", "true")
    monkeypatch.setattr(configsys, "_get_store", lambda service: Dead())
    with pytest.raises(StoreUnavailable):
        configsys.apply_overrides(publish_on(True), actor="a")
    assert configsys.get_config().get("features").transcript_publish is False


def test_driver_errors_are_reported_as_one_error_type():
    class Broken(FakeMongoCollection):
        def find(self, *a, **k):
            raise OSError("socket closed")

    with pytest.raises(StoreUnavailable, match="socket closed"):
        MongoOverrideStore(Broken()).latest()


# ── two administrators ───────────────────────────────────────────────────────


def test_two_simultaneous_saves_one_wins_and_the_other_is_told(shared):
    configsys.get_config()
    results, errors = [], []

    def save(name):
        try:
            results.append(
                configsys.apply_overrides(
                    {"llm": {"provider": name}}, actor=name, expected_version=0
                )
            )
        except StaleRevision as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(n,)) for n in ("gemini", "openai")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1 and len(errors) == 1
    assert shared.latest().version == 1


# ── files and secrets that change while running ──────────────────────────────


@pytest.fixture
def conf_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DNA_CONFIG_DIR", str(tmp_path))
    return tmp_path


def write(path, text):
    path.write_text(text)
    stat = path.stat()  # make the change visible even on a coarse clock
    import os

    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000))


def test_an_edited_config_file_is_picked_up_without_a_restart(conf_dir):
    f = conf_dir / "site.yaml"
    f.write_text("features:\n  transcript_publish: false\n")
    assert configsys.get_config().get("features").transcript_publish is False
    write(f, "features:\n  transcript_publish: true\n")
    assert configsys.get_config().get("features").transcript_publish is True


def test_a_new_config_file_is_picked_up(conf_dir):
    configsys.get_config()
    (conf_dir / "10-more.yaml").write_text("llm:\n  provider: gemini\n")
    assert configsys.get_config().get("llm").provider == "gemini"


def test_a_bad_edit_is_refused_and_the_running_config_is_kept(conf_dir, caplog):
    f = conf_dir / "site.yaml"
    f.write_text("llm:\n  provider: gemini\n")
    configsys.get_config()
    write(f, "llm:\n  provder: gemini\n")  # a typo
    cfg = configsys.get_config()  # must not raise or take the service down
    assert cfg.get("llm").provider == "gemini"
    status = configsys.config_status()
    assert (
        status["ok"] is False
        and "still running on the previous configuration" in status["problems"][0]
    )
    assert "invalid; keeping the previous" in caplog.text


def test_fixing_the_file_recovers(conf_dir):
    f = conf_dir / "site.yaml"
    f.write_text("llm:\n  provider: gemini\n")
    configsys.get_config()
    write(f, "llm:\n  provder: x\n")
    configsys.get_config()
    write(f, "llm:\n  provider: openai\n")
    assert configsys.get_config().get("llm").provider == "openai"
    assert configsys.config_status()["ok"] is True


def test_a_bad_file_does_not_make_every_request_retry_the_load(conf_dir, monkeypatch):
    f = conf_dir / "site.yaml"
    f.write_text("llm:\n  provider: gemini\n")
    configsys.get_config()
    write(f, "llm:\n  provder: x\n")
    loads = []
    real = configsys.ConfigService.load
    monkeypatch.setattr(
        configsys.ConfigService, "load", lambda self: loads.append(1) or real(self)
    )
    for _ in range(5):
        configsys.get_config()
    assert len(loads) == 1


def test_an_invalid_config_at_startup_is_fatal_and_says_why(conf_dir):
    (conf_dir / "site.yaml").write_text("llm:\n  provder: x\n")
    with pytest.raises(ConfigError, match="provder"):
        configsys.get_config()


def test_a_rotated_secret_file_is_picked_up(conf_dir):
    secret = conf_dir / "ayon.key"
    secret.write_text("old-key\n")
    (conf_dir / "site.yaml").write_text(
        f"prodtrack:\n  ayon:\n    api_key: ${{file:{secret}}}\n"
    )
    assert configsys.get_config().get("prodtrack").ayon.api_key == "old-key"
    write(secret, "new-key\n")
    assert configsys.get_config().get("prodtrack").ayon.api_key == "new-key"


# ── what the audit trail keeps ───────────────────────────────────────────────


def test_a_change_records_what_each_setting_was_and_became(shared):
    configsys.apply_overrides(publish_on(True), actor="ann", note="turn on")
    configsys.apply_overrides({"llm": {"provider": "gemini"}}, actor="bob")
    first = next(r for r in configsys.history() if r.version == 1)
    assert first.details == (
        {"path": "features.transcript_publish", "before": False, "after": True},
    )
    summary = first.summary()
    assert summary["details"][0]["after"] is True


def test_secrets_appear_in_the_trail_only_as_references(shared, monkeypatch):
    monkeypatch.setenv("AYON_API_KEY", "super-secret-value")
    configsys.apply_overrides(
        {"prodtrack": {"shotgrid": {"api_key": "${env:AYON_API_KEY}"}}}, actor="ann"
    )
    trail = repr([r.summary() for r in configsys.history()])
    assert "super-secret-value" not in trail
    assert "${env:AYON_API_KEY}" in trail


def test_a_project_change_is_recorded_with_its_values(shared):
    configsys.apply_overrides({"projects": {"p1": publish_on(True)}}, actor="ann")
    (rev,) = configsys.history()
    assert rev.details[0]["path"] == "projects.p1.features.transcript_publish"
    assert rev.details[0]["before"] is None and rev.details[0]["after"] is True


def test_a_long_value_is_shortened_in_the_trail(shared):
    configsys.apply_overrides(
        {"llm": {"profiles": {"x": {"model": "m" * 500}}}}, actor="a"
    )
    (rev,) = configsys.history()
    assert len(rev.details[0]["after"]) <= 201


def test_a_listing_does_not_carry_the_snapshots():
    collection = FakeMongoCollection()
    store = MongoOverrideStore(collection)
    store.append({"x": {"y": 1}}, actor="a")
    (listed,) = store.history()
    assert listed.overrides == {}
    assert store.get(1).overrides == {
        "x": {"y": 1}
    }  # a rollback still reads them whole


# ── an override that hides a file or environment value ───────────────────────


def test_an_admin_override_that_hides_the_environment_is_called_out(
    shared, monkeypatch
):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    configsys.apply_overrides({"llm": {"provider": "gemini"}}, actor="ann")
    warnings = configsys.get_config().warnings
    assert any("llm.provider" in w and "overrides" in w for w in warnings)
    assert any("LLM_PROVIDER" in w for w in warnings)


def test_an_override_that_matches_the_environment_is_not_flagged(shared, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    configsys.apply_overrides({"llm": {"provider": "gemini"}}, actor="ann")
    assert not any("llm.provider" in w for w in configsys.get_config().warnings)


def test_an_override_of_something_nobody_else_set_is_not_flagged(shared):
    configsys.apply_overrides(publish_on(True), actor="ann")
    assert configsys.get_config().warnings == []
