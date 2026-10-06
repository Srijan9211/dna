"""Connector profiles, the mapper, and their integration with config."""

from unittest import mock

import pytest

from dna import configsys
from dna.configsys import ConfigError
from dna.models.entity import ENTITY_MODELS
from dna.prodtrack_providers import shotgrid as sg_module
from dna.prodtrack_providers.mapper import ProfileMapper
from dna.prodtrack_providers.mock_provider import MockProdtrackProvider
from dna.prodtrack_providers.profile import (
    KINDS,
    ROLES,
    ConnectorProfile,
    has_profile,
    load_profile,
)
from dna.prodtrack_providers.shotgrid import ShotgridAdapter, ShotgridProvider


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    for key in [k for k in __import__("os").environ if k.startswith("DNA__")]:
        monkeypatch.delenv(key)
    monkeypatch.delenv("DNA_CONFIG_DIR", raising=False)
    configsys.reset_config()
    yield
    configsys.reset_config()


def base(**overrides):
    data = {
        "connector": "x",
        "entities": {"version": {"source": "V"}, "shot": {"source": "S"}},
    }
    data.update(overrides)
    return data


# ── vocabulary stays in step with the models ─────────────────────────────────


def test_profile_kinds_match_the_entity_models():
    assert set(KINDS) == set(ENTITY_MODELS)


def test_every_role_kind_in_the_shipped_profiles_is_bound():
    for name in ("shotgrid", "mock"):
        profile = load_profile(name)
        for role, kinds in profile.roles.items():
            assert role in ROLES
            assert all(k in profile.entities for k in kinds)


# ── schema validation ────────────────────────────────────────────────────────


def test_unknown_entity_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown entity kind"):
        ConnectorProfile.model_validate(base(entities={"sequence": {"source": "Seq"}}))


def test_role_must_reference_bound_kinds():
    with pytest.raises(ValueError, match="no binding"):
        ConnectorProfile.model_validate(base(roles={"reviewable": ["playlist"]}))


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError, match="unknown role"):
        ConnectorProfile.model_validate(base(roles={"boss": ["version"]}))


def test_kinds_sharing_a_tracker_entity_need_where_filters():
    shared = {"shot": {"source": "folder"}, "asset": {"source": "folder"}}
    with pytest.raises(ValueError, match="share the tracker entity"):
        ConnectorProfile.model_validate(base(entities=shared))
    ok = {
        "shot": {"source": "folder", "where": {"folderType": ["Shot"]}},
        "asset": {"source": "folder", "where": {"folderType": ["Asset"]}},
    }
    profile = ConnectorProfile.model_validate(base(entities=ok))
    with pytest.raises(ValueError, match="several DNA kinds"):
        ProfileMapper(profile).kind_for_source("folder")


def test_typos_in_a_profile_are_errors():
    with pytest.raises(ValueError):
        ConnectorProfile.model_validate(base(capabilites={}))
    with pytest.raises(ValueError):
        ConnectorProfile.model_validate(base(entities={"version": {"sourse": "V"}}))


def test_contract_version_is_enforced():
    with pytest.raises(ValueError):
        ConnectorProfile.model_validate(base(contract="dna.prodtrack/9"))


# ── loading and overriding ───────────────────────────────────────────────────


def test_packaged_profiles_exist_and_load():
    assert has_profile("shotgrid") and has_profile("mock")
    assert not has_profile("nonesuch")
    assert load_profile("shotgrid").display_name == "ShotGrid"


def test_missing_profile_has_a_helpful_error():
    with pytest.raises(ValueError, match="profile_path"):
        load_profile("nonesuch")


def test_override_merges_and_null_removes_a_key():
    profile = load_profile(
        "shotgrid",
        overrides={
            "entities": {
                "version": {"fields": {"sg_status_list": None, "sg_review": "status"}}
            },
            "labels": {"playlist": "Review List"},
        },
    )
    fields = profile.entities["version"].fields
    assert "sg_status_list" not in fields and fields["sg_review"] == "status"
    assert fields["code"] == "name"  # the rest of the base survives
    assert profile.labels["playlist"] == "Review List"
    assert profile.labels["shot"] == "Shot"


def test_invalid_override_error_names_the_problem():
    with pytest.raises(ValueError, match="Invalid connector profile for 'shotgrid'"):
        load_profile("shotgrid", overrides={"capabilities": {"playlists": "banana"}})


def test_a_whole_profile_can_come_from_a_file(tmp_path):
    path = tmp_path / "ftrack.yaml"
    path.write_text(
        "connector: ftrack\ncapabilities: {note_attachments: false}\n"
        "entities:\n  version: {source: AssetVersion}\n",
        encoding="utf-8",
    )
    profile = load_profile("ftrack", path=str(path))
    assert profile.entities["version"].source == "AssetVersion"
    assert profile.capabilities.note_attachments is False


# ── mapper ───────────────────────────────────────────────────────────────────


def test_mapper_translates_in_both_directions():
    m = ProfileMapper(load_profile("shotgrid"))
    assert m.source("version") == "Version"
    assert m.kind_for_source("HumanUser") == "user"
    assert m.tracker_field("version", "status") == "sg_status_list"
    assert m.tracker_field("version", "task") == "sg_task"  # a link field
    assert m.tracker_field_or_none("version", "nonexistent") is None
    assert m.read_fields("playlist") == [
        "id", "code", "description", "project", "created_at", "updated_at", "versions",
    ]
    assert m.to_tracker_filters(
        "version", [{"field": "status", "operator": "is", "value": "rev"}]
    ) == [["sg_status_list", "is", "rev"]]
    with pytest.raises(ValueError, match="Unknown entity type: Bogus"):
        m.kind_for_source("Bogus")


def test_mapper_renders_urls_from_templates():
    m = ProfileMapper(load_profile("shotgrid"))
    url = m.url("version_detail", base="https://s.example/", kind="version", entity_id=7)
    assert url == "https://s.example/detail/Version/7"
    assert m.url("version_detail", base="", kind="version", entity_id=7) is None
    mock_mapper = ProfileMapper(load_profile("mock"))
    assert mock_mapper.url("version_detail", base="https://x", kind="version", entity_id=1) is None


# ── capabilities ─────────────────────────────────────────────────────────────


def test_capabilities_come_from_the_profile():
    caps = MockProdtrackProvider().capabilities()
    assert caps.note_attachments is False and caps.impersonation == "none"
    with mock.patch.object(sg_module, "Shotgun"):
        provider = ShotgridProvider(url="https://s", script_name="a", api_key="k")
    assert provider.capabilities().impersonation == "sudo_header"
    assert "tracker_entity" in provider.capabilities().transcript_storage


# ── config integration ───────────────────────────────────────────────────────


def test_config_validates_the_profile_at_startup(monkeypatch):
    monkeypatch.setenv("DNA__PRODTRACK__PROFILE__CAPABILITIES__PLAYLISTS", "banana")
    with pytest.raises(ConfigError) as err:
        configsys.get_config()
    assert "Invalid connector profile" in str(err.value)


def test_profile_overrides_without_any_profile_to_apply_to_are_refused(monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "kitsu")
    monkeypatch.setenv("DNA__PRODTRACK__PROFILE__LABELS__SHOT", "Shot")
    with pytest.raises(ConfigError, match="no profile to apply them to"):
        configsys.get_config()


def test_a_connector_with_no_profile_still_loads_without_overrides(monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "kitsu")
    assert configsys.get_config().get("prodtrack").connector == "kitsu"


def test_studio_renames_the_status_field_and_dna_follows_with_no_code_change(
    monkeypatch, tmp_path
):
    """A studio whose ShotGrid calls the status field ``sg_review_status`` needs
    only a config file: queries, writes and the schema read all follow."""
    (tmp_path / "site.yaml").write_text(
        "prodtrack:\n  profile:\n    entities:\n      version:\n"
        "        fields: {sg_status_list: null, sg_review_status: status}\n"
        "    adapter:\n      status_field: sg_review_status\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DNA_CONFIG_DIR", str(tmp_path))
    fake = mock.MagicMock()
    fake.find.return_value = [
        {"id": 1, "code": "v1", "sg_review_status": "apr", "project": None}
    ]
    with mock.patch.object(sg_module, "Shotgun", return_value=fake):
        provider = ShotgridProvider(url="https://s", script_name="a", api_key="k")
        provider.update_version_status(1, "apr")
        found = provider.find("version", [{"field": "status", "operator": "is", "value": "apr"}])

    fake.update.assert_called_once_with("Version", 1, {"sg_review_status": "apr"})
    query = fake.find.call_args
    assert query.kwargs["filters"] == [["sg_review_status", "is", "apr"]]
    assert "sg_review_status" in query.kwargs["fields"]
    assert "sg_status_list" not in query.kwargs["fields"]
    assert found[0].status == "apr"  # converted through the renamed field


def test_a_list_cannot_be_set_from_a_plain_environment_string(monkeypatch):
    # Env values are strings; a list-valued profile setting belongs in YAML.  It
    # is rejected loudly rather than silently ignored.
    monkeypatch.setenv(
        "DNA__PRODTRACK__PROFILE__ERRORS__PERMISSION_MARKERS", '["verboten"]'
    )
    with pytest.raises(ConfigError):
        configsys.get_config()


def test_studio_can_set_its_own_permission_markers_in_yaml(monkeypatch, tmp_path):
    from dna.prodtrack_providers.prodtrack_provider_base import (
        ProdtrackPermissionError,
        ProdtrackUnavailableError,
    )

    (tmp_path / "site.yaml").write_text(
        "prodtrack:\n  profile:\n    errors:\n"
        "      permission_markers: [verboten, 'kein zugriff']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DNA_CONFIG_DIR", str(tmp_path))
    # A localised server message the built-in English markers would miss:
    assert isinstance(
        sg_module.classify_sg_fault(Exception("Kein Zugriff auf diese Ressource")),
        ProdtrackPermissionError,
    )
    assert isinstance(
        sg_module.classify_sg_fault(Exception("connection reset by peer")),
        ProdtrackUnavailableError,
    )


def test_adapter_block_is_validated_by_the_adapter_not_the_core_profile():
    with pytest.raises(ValueError):
        ShotgridAdapter.model_validate({"status_feild": "x"})
    adapter = ShotgridAdapter.model_validate(load_profile("shotgrid").adapter)
    assert adapter.status_field == "sg_status_list"
    assert adapter.note.to_field == "addressings_to"
    assert adapter.transcript.body == "sg_transcript_body"


def test_defaults_in_the_adapter_model_match_the_shipped_profile():
    shipped = load_profile("shotgrid").adapter
    assert ShotgridAdapter.model_validate(shipped) == ShotgridAdapter()
