"""Profile field names go into the tracker's queries, so they must be plain names."""

import pytest

from dna import configsys
from dna.configsys import ConfigError, ConfigService, default_registry
from dna.prodtrack_providers.ayon import build_selection

INJECTION = 'name } } mutation { deleteProject(name:"x") { id'


def candidate(entity, key, value):
    return {
        "prodtrack": {
            "connector": "ayon",
            "profile": {"entities": {"shot": {entity: {key: value}}}},
        }
    }


def load(runtime):
    return ConfigService(default_registry, environ={}, runtime_overrides=runtime).load()


@pytest.mark.parametrize(
    "bad",
    [
        INJECTION,
        "name }",
        "a b",
        "a-b",
        "a;b",
        "",
        ".leading",
        "trailing.",
        "a..b",
        "1starts_with_digit",
        "a\nb",
        'a"b',
    ],
)
def test_a_field_name_that_could_alter_a_query_is_refused(bad):
    with pytest.raises(ConfigError, match="not a plain field name"):
        load(candidate("fields", bad, "description"))


def test_every_name_in_a_binding_is_checked():
    for entity, key, value in (
        ("fields", "attrib.description", INJECTION),  # the DNA-side name
        ("links", INJECTION, "x"),
        ("where", INJECTION, ["Shot"]),
    ):
        with pytest.raises(ConfigError, match="not a plain field name"):
            load(candidate(entity, key, value))


def test_the_entity_type_is_checked():
    with pytest.raises(ConfigError, match="not a plain field name"):
        load(
            {
                "prodtrack": {
                    "connector": "ayon",
                    "profile": {"entities": {"shot": {"source": "folder { x"}}},
                }
            }
        )


@pytest.mark.parametrize(
    "good",
    [
        "name",
        "attrib.description",
        "attrib.vendorNotes",
        "sg_status_list",
        "entity.Shot.id",
        "project.Project.name",
        "createdAt",
        "_private",
    ],
)
def test_ordinary_field_names_still_work(good):
    svc = load(candidate("fields", good, "description"))
    assert good in svc.get("prodtrack").profile["entities"]["shot"]["fields"]


def test_every_shipped_profile_passes_the_check():
    from dna.prodtrack_providers.profile import has_profile, load_profile

    for connector in ("shotgrid", "ayon", "mock"):
        assert has_profile(connector)
        load_profile(connector)  # raises if a shipped name is not plain


def test_a_refused_name_never_reaches_a_query():
    """The selection builder trusts its input; validation is what keeps it safe."""
    assert "mutation" in build_selection(
        ["name", INJECTION]
    )  # the hazard being guarded
    with pytest.raises(ConfigError):
        load(candidate("fields", INJECTION, "description"))


def test_the_check_applies_to_a_runtime_change_too():
    configsys.reset_config()
    with pytest.raises(ConfigError):
        configsys.preview_overrides(candidate("fields", INJECTION, "description"))
    configsys.reset_config()
