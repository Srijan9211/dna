"""docs/CONFIGURATION_REFERENCE.md is generated; it must match the settings registry."""

import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "generate_config_reference.py"
)


def _generator():
    spec = importlib.util.spec_from_file_location("generate_config_reference", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_reference_matches_the_settings_registry():
    module = _generator()
    current = module.TARGET.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert current == module.render(), (
        "docs/CONFIGURATION_REFERENCE.md is stale; run "
        "`python backend/scripts/generate_config_reference.py`"
    )


def test_every_setting_with_an_environment_variable_is_documented():
    module = _generator()
    text = module.render()
    for name in default_registry_env_names():
        assert f"`{name}`" in text, name


def default_registry_env_names():
    from dna.configsys import default_registry

    return default_registry.known_env_names()
