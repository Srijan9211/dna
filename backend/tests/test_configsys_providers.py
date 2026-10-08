"""Provider registries and the process-wide config accessor."""

from importlib import metadata

import pytest

from dna import configsys
from dna.auth_providers.auth_provider_base import get_auth_provider
from dna.configsys import ConfigError, providers
from dna.configsys.providers import ProviderRegistry
from dna.llm_providers.llm_provider_base import get_llm_provider
from dna.prodtrack_providers.mock_provider import MockProdtrackProvider
from dna.prodtrack_providers.prodtrack_provider_base import get_prodtrack_provider


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    """Isolate every test from the process env and from earlier overrides."""
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DNA_CONFIG_DIR", raising=False)
    configsys.reset_config()
    yield
    configsys.reset_config()


# ── registry mechanics ───────────────────────────────────────────────────────


def test_builtin_resolves_lazily_to_the_real_class():
    assert providers.prodtrack.resolve("mock") is MockProdtrackProvider


def test_unknown_name_error_wording_per_subsystem():
    with pytest.raises(ValueError, match="Unknown storage provider: nope"):
        providers.storage.resolve("nope")
    with pytest.raises(ValueError, match="Unknown LLM provider: nope"):
        providers.llm.resolve("nope")
    with pytest.raises(ValueError, match="Unknown production tracking provider: nope"):
        providers.prodtrack.resolve("nope")


def test_explicit_registration_and_override_of_a_builtin():
    reg = ProviderRegistry(
        group="dna.test", label="test provider", builtins={"a": "builtins:dict"}
    )
    assert reg.resolve("a") is dict
    reg.register("a", list)  # explicit callable wins over the built-in
    assert reg.resolve("a") is list
    reg.register("b", "builtins:set")
    assert reg.resolve("b") is set
    assert reg.names() == ["a", "b"]


def test_entry_point_providers_are_discovered(monkeypatch):
    class FakeEntryPoint:
        name = "ftrack"

        def load(self):
            return dict

    monkeypatch.setattr(
        metadata,
        "entry_points",
        lambda group=None: [FakeEntryPoint()] if group == "dna.test" else [],
    )
    reg = ProviderRegistry(group="dna.test", label="test provider")
    assert reg.resolve("ftrack") is dict
    assert "ftrack" in reg.names()
    with pytest.raises(ValueError, match="Unknown test provider: kitsu"):
        reg.resolve("kitsu")


def test_class_is_resolved_on_every_call_so_patching_works(monkeypatch):
    import dna.prodtrack_providers.mock_provider as module

    class Replacement:
        pass

    monkeypatch.setattr(module, "MockProdtrackProvider", Replacement)
    assert providers.prodtrack.resolve("mock") is Replacement


# ── factories use the registries ─────────────────────────────────────────────


def test_factory_builds_a_custom_registered_provider(monkeypatch):
    class StubLlm:
        LLM_PROVIDER_NAME = "STUB"

    monkeypatch.setitem(providers.llm._explicit, "stub", StubLlm)
    monkeypatch.setenv("LLM_PROVIDER", "Stub")
    assert isinstance(get_llm_provider(), StubLlm)


def test_prodtrack_factory_ignores_identity_for_connectors_without_impersonation(
    monkeypatch,
):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "mock")
    provider = get_prodtrack_provider(sudo_login="someone", session_id="s1")
    assert isinstance(provider, MockProdtrackProvider)


def test_auth_factory_unknown_provider_keeps_its_message(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "kerberos")
    with pytest.raises(ValueError) as err:
        get_auth_provider()
    assert (
        str(err.value)
        == "Unknown auth provider: 'kerberos'. Valid values: none, google, shotgrid, ayon."
    )


# ── get_config follows the environment ───────────────────────────────────────


def test_get_config_reloads_when_the_environment_changes(monkeypatch):
    assert configsys.get_config().get("server").log_level == "INFO"
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert configsys.get_config().get("server").log_level == "DEBUG"
    monkeypatch.delenv("LOG_LEVEL")
    assert configsys.get_config().get("server").log_level == "INFO"


def test_get_config_is_cached_while_the_environment_is_unchanged():
    assert configsys.get_config() is configsys.get_config()


def test_runtime_overrides_survive_an_environment_reload(monkeypatch):
    configsys.apply_overrides({"features": {"transcript_publish": True}})
    monkeypatch.setenv("LOG_LEVEL", "warning")  # forces a reload
    cfg = configsys.get_config()
    assert cfg.get("server").log_level == "WARNING"
    assert cfg.get("features").transcript_publish is True


def test_invalid_environment_fails_loudly_with_every_problem(monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "0")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "x")
    with pytest.raises(ConfigError) as err:
        configsys.get_config()
    assert len(err.value.issues) >= 2
    assert isinstance(err.value, ValueError)  # callers catching ValueError still work


def test_per_project_transcript_entity_is_used_for_publishing(monkeypatch, tmp_path):
    (tmp_path / "site.yaml").write_text(
        "projects:\n  '9':\n    prodtrack:\n      shotgrid:\n"
        "        transcript_entity: CustomEntity07\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DNA_CONFIG_DIR", str(tmp_path))
    from dna.prodtrack_providers.shotgrid import _transcript_entity_type

    assert _transcript_entity_type(9) == "CustomEntity07"
    assert _transcript_entity_type(10) == "CustomEntity01"
    assert _transcript_entity_type() == "CustomEntity01"


def test_llm_profile_from_yaml_reaches_the_provider(monkeypatch, tmp_path):
    (tmp_path / "site.yaml").write_text(
        "llm:\n  provider: openai\n  profiles:\n    openai:\n"
        "      model: gpt-test\n      timeout: 12\n      api_key: sk-from-yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DNA_CONFIG_DIR", str(tmp_path))
    provider = get_llm_provider()
    assert provider.model == "gpt-test"
    assert provider.timeout == 12.0
    assert provider.api_key == "sk-from-yaml"
