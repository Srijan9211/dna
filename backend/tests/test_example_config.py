"""config.example/ is what operators copy; it must always load."""

from pathlib import Path

from dna.configsys import ConfigService

EXAMPLE = Path(__file__).resolve().parents[2] / "config.example"


def test_the_example_configuration_loads_without_warnings():
    env = {
        "AYON_API_KEY": "key",
        "JWT_SECRET_KEY": "x" * 40,
        "MONGODB_URL": "mongodb://localhost:27017",
    }
    service = ConfigService(environ=env, config_dir=EXAMPLE).load()
    assert service.get("prodtrack").connector == "ayon"
    assert service.get("admin").persist is True
    assert service.warnings == []
