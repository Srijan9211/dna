"""The configuration CLI: exit codes, output, and the persistence requirement."""

import json

import pytest
import yaml

from dna import configsys
from dna.config_cli import main


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    configsys.reset_config()
    yield
    configsys.reset_config()


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_validate_succeeds_on_a_valid_configuration(capsys):
    code, out, _ = run(capsys, "validate")
    assert code == 0 and "valid" in out


def test_validate_fails_with_every_problem_listed(capsys, monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "0")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "x")
    code, out, err = run(capsys, "validate")
    assert code == 1 and out == ""
    assert "session.ttl_seconds" in err and "access_token_minutes" in err


def test_show_masks_secrets_and_can_show_provenance(capsys, monkeypatch):
    monkeypatch.setenv("SHOTGRID_API_KEY", "sg-secret-value")
    monkeypatch.setenv("SHOTGRID_URL", "https://studio.example")
    code, out, _ = run(capsys, "show", "--section", "prodtrack", "--provenance")
    assert code == 0 and "sg-secret-value" not in out
    data = yaml.safe_load(out)
    assert data["config"]["prodtrack"]["shotgrid"]["api_key"] == "********"
    assert data["provenance"]["prodtrack.shotgrid.url"] == "env:SHOTGRID_URL"
    assert "llm.provider" not in data["provenance"]  # scoped to the section


def test_show_rejects_an_unknown_section(capsys):
    code, _, err = run(capsys, "show", "--section", "nonesuch")
    assert code == 1 and "Unknown section" in err


def test_handshake_prints_a_checklist_and_exits_zero_when_it_passes(capsys, monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "mock")
    code, out, _ = run(capsys, "handshake")
    assert code == 0 and "RESULT: OK" in out and "[ok  ] describe" in out


def test_handshake_emits_json_for_pipelines(capsys, monkeypatch):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "mock")
    code, out, _ = run(capsys, "handshake", "--json")
    assert code == 0 and json.loads(out)["connector"] == "mock"


def test_handshake_exits_nonzero_on_a_failing_check(capsys, monkeypatch):
    class Failing:
        @classmethod
        def for_handshake(cls, cfg, profile):
            raise ValueError("SHOTGRID_URL is required.")

    monkeypatch.setitem(configsys.providers.prodtrack._explicit, "shotgrid", Failing)
    code, out, _ = run(capsys, "handshake")
    assert code == 1 and "[FAIL] connect" in out and "RESULT: FAILED" in out


def test_handshake_reads_a_candidate_file_and_refuses_what_a_save_would_refuse(capsys, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("auth:\n  provider: none\n", encoding="utf-8")
    code, _, err = run(capsys, "handshake", "--candidate", str(bad))
    assert code == 1 and "cannot be changed at runtime" in err


def test_history_rollback_and_import_need_the_shared_store(capsys, tmp_path):
    doc = tmp_path / "x.yaml"
    doc.write_text("format: 1\noverrides: {}\n", encoding="utf-8")
    for argv in (("history",), ("rollback", "1"), ("import", str(doc))):
        code, _, err = run(capsys, *argv)
        assert code == 1 and "DNA_CONFIG_PERSIST" in err


def test_apply_after_a_good_handshake_needs_the_store_too(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("PRODTRACK_PROVIDER", "mock")
    candidate = tmp_path / "c.yaml"
    candidate.write_text("prodtrack:\n  profile:\n    labels: {shot: Plate}\n", encoding="utf-8")
    code, out, err = run(capsys, "handshake", "--candidate", str(candidate), "--apply")
    assert code == 1 and "RESULT: OK" in out and "DNA_CONFIG_PERSIST" in err


def test_the_full_change_workflow_when_persistence_is_on(capsys, monkeypatch, tmp_path):
    from dna.configsys.store import MemoryOverrideStore

    shared = MemoryOverrideStore()
    monkeypatch.setenv("DNA_CONFIG_PERSIST", "true")
    monkeypatch.setenv("PRODTRACK_PROVIDER", "mock")
    monkeypatch.setattr(configsys, "_get_store", lambda service: shared)
    candidate = tmp_path / "c.yaml"
    candidate.write_text("prodtrack:\n  profile:\n    labels: {shot: Plate}\n", encoding="utf-8")

    code, out, _ = run(capsys, "handshake", "--candidate", str(candidate), "--apply")
    assert code == 0 and "Saved as revision 1" in out

    code, out, _ = run(capsys, "history")
    assert code == 0 and "handshake (cli)" in out and "prodtrack.profile.labels.shot" in out

    exported = tmp_path / "export.yaml"
    assert run(capsys, "export", "-o", str(exported))[0] == 0
    assert yaml.safe_load(exported.read_text())["overrides"]["prodtrack"]["profile"]["labels"] == {"shot": "Plate"}

    assert run(capsys, "rollback", "1")[1].startswith("Rolled back to v1 as revision 2")
    assert run(capsys, "import", str(exported))[1].startswith("Imported as revision 3")
