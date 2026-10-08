"""Behaviour of the config service: layering, provenance, secrets, validation."""

import pytest

from dna.configsys import ConfigError, ConfigService, default_registry


def make(environ=None, config_dir=None, runtime=None):
    return ConfigService(
        default_registry,
        environ=environ or {},
        config_dir=config_dir,
        runtime_overrides=runtime,
    ).load()


# ── defaults match what the code reads today ─────────────────────────────────


def test_defaults_match_existing_behaviour():
    cfg = make()
    assert cfg.get("server").log_level == "INFO"
    assert cfg.get("server").attachment_store_dir == "/tmp/dna_attachments"
    assert cfg.get("server").cors_allowed_origins == []
    assert cfg.get("auth").provider == "none"
    assert cfg.get("auth").jwt.issuer == "dna-backend"
    assert cfg.get("auth").jwt.audience == "dna-api"
    assert cfg.get("auth").jwt.access_token_minutes == 15
    assert cfg.get("auth").session.ttl_seconds == 28800
    assert cfg.get("auth").session.max_lifetime_seconds == 43200
    assert cfg.get("auth").refresh_cookie.samesite == "strict"
    assert cfg.get("auth").refresh_cookie.secure is True
    assert cfg.get("storage").mongodb_url == "mongodb://localhost:27017"
    assert cfg.get("storage").mongodb_db == "dna"
    assert cfg.get("prodtrack").connector == "shotgrid"
    assert cfg.get("prodtrack").startup_check_timeout == 10.0
    assert cfg.get("prodtrack").shotgrid.transcript_entity == "CustomEntity01"
    assert cfg.get("prodtrack").shotgrid.site_type == "cloud"
    assert cfg.get("llm").provider == "openai"
    assert cfg.get("transcription").provider == "vexa"
    assert cfg.get("transcription").vexa.api_url == "https://api.cloud.vexa.ai"
    assert cfg.get("features").transcript_publish is False
    assert cfg.get("features").testing_enabled is False


# ── environment ──────────────────────────────────────────────────────────────


def test_legacy_env_aliases_are_honoured():
    cfg = make(
        {
            "SHOTGRID_URL": "https://studio.shotgrid.autodesk.com",
            "PRODTRACK_PROVIDER": "AYON",
            "SESSION_TTL_SECONDS": "60",
            "DISABLE_DOCS": "true",
            "CORS_ALLOWED_ORIGINS": "http://a.test, http://b.test",
        }
    )
    assert cfg.get("prodtrack").shotgrid.url == "https://studio.shotgrid.autodesk.com"
    assert cfg.get("prodtrack").connector == "ayon"  # normalised
    assert cfg.get("auth").session.ttl_seconds == 60
    assert cfg.get("server").disable_docs is True
    assert cfg.get("server").cors_allowed_origins == ["http://a.test", "http://b.test"]


def test_uniform_env_form_wins_over_legacy_alias():
    cfg = make(
        {"LOG_LEVEL": "debug", "DNA__SERVER__LOG_LEVEL": "warning"},
    )
    assert cfg.get("server").log_level == "WARNING"
    assert cfg.provenance()["server.log_level"] == "env:DNA__SERVER__LOG_LEVEL"


def test_empty_env_value_is_treated_as_unset():
    cfg = make({"MONGODB_DB": "", "SESSION_TTL_SECONDS": ""})
    assert cfg.get("storage").mongodb_db == "dna"
    assert cfg.get("auth").session.ttl_seconds == 28800


def test_llm_profiles_are_built_from_legacy_env():
    cfg = make(
        {
            "LLM_PROVIDER": "Gemini",
            "GEMINI_API_KEY": "k",
            "GEMINI_MODEL": "gemini-2.5-flash",
            "GEMINI_TIMEOUT": "45",
            "CUSTOM_LLM_URL": "http://llm.local/v1",
        }
    )
    llm = cfg.get("llm")
    assert llm.provider == "gemini"
    assert llm.profiles["gemini"].model == "gemini-2.5-flash"
    assert llm.profiles["gemini"].timeout == 45.0
    assert llm.profiles["custom"].url == "http://llm.local/v1"
    assert "openai" not in llm.profiles  # nothing set -> provider defaults apply


# ── files + precedence + provenance ──────────────────────────────────────────


def test_file_layers_merge_in_name_order_and_env_overrides_files(tmp_path):
    (tmp_path / "00-base.yaml").write_text(
        "server:\n  log_level: debug\n  disable_docs: true\n", encoding="utf-8"
    )
    (tmp_path / "10-site.yaml").write_text(
        "server:\n  log_level: warning\n", encoding="utf-8"
    )
    cfg = make({"DISABLE_DOCS": "false"}, config_dir=tmp_path)
    assert cfg.get("server").log_level == "WARNING"  # later file wins
    assert cfg.get("server").disable_docs is False  # env beats file
    prov = cfg.provenance()
    assert prov["server.log_level"].startswith("file:")
    assert prov["server.log_level"].endswith("10-site.yaml")
    assert prov["server.disable_docs"] == "env:DISABLE_DOCS"
    assert prov["server.attachment_store_dir"] == "default"


def test_config_dir_can_come_from_the_environment(tmp_path):
    (tmp_path / "site.yaml").write_text("storage:\n  mongodb_db: studio\n")
    cfg = make({"DNA_CONFIG_DIR": str(tmp_path)})
    assert cfg.get("storage").mongodb_db == "studio"


# ── secrets ──────────────────────────────────────────────────────────────────


def test_secret_references_resolve_from_env_and_file(tmp_path):
    secret_file = tmp_path / "ayon_key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    (tmp_path / "site.yaml").write_text(
        "prodtrack:\n"
        "  shotgrid:\n"
        "    api_key: ${env:SG_KEY}\n"
        "  ayon:\n"
        f"    api_key: ${{file:{secret_file.as_posix()}}}\n",
        encoding="utf-8",
    )
    cfg = make({"SG_KEY": "env-secret"}, config_dir=tmp_path)
    prodtrack = cfg.get("prodtrack")
    assert prodtrack.shotgrid.api_key == "env-secret"
    assert prodtrack.ayon.api_key == "file-secret"


def test_secret_reference_fallback_and_missing(tmp_path):
    (tmp_path / "site.yaml").write_text(
        "transcription:\n  vexa:\n    api_key: ${env:NOPE:-fallback}\n"
    )
    assert make(config_dir=tmp_path).get("transcription").vexa.api_key == "fallback"

    (tmp_path / "site.yaml").write_text(
        "transcription:\n  vexa:\n    api_key: ${env:NOPE}\n"
    )
    with pytest.raises(ConfigError) as err:
        make(config_dir=tmp_path)
    assert "NOPE" in str(err.value)


def test_effective_masks_secrets_but_get_returns_real_value():
    cfg = make({"SHOTGRID_API_KEY": "s3cret", "JWT_SECRET_KEY": "jwt-s3cret"})
    effective = cfg.effective()
    assert effective["prodtrack"]["shotgrid"]["api_key"] == "********"
    assert effective["auth"]["jwt"]["secret_key"] == "********"
    assert "s3cret" not in repr(effective)
    assert cfg.get("prodtrack").shotgrid.api_key == "s3cret"
    assert (
        cfg.effective(mask_secrets=False)["prodtrack"]["shotgrid"]["api_key"]
        == "s3cret"
    )


def test_unset_secret_is_none_not_a_mask():
    assert make().effective()["prodtrack"]["shotgrid"]["api_key"] is None


# ── validation ───────────────────────────────────────────────────────────────


def test_all_problems_are_reported_together_with_their_source():
    with pytest.raises(ConfigError) as err:
        make(
            {
                "SESSION_TTL_SECONDS": "-5",
                "JWT_EXPIRE_MINUTES": "abc",
                "REFRESH_COOKIE_SAMESITE": "none",
                "REFRESH_COOKIE_SECURE": "false",
            }
        )
    paths = {i.path for i in err.value.issues}
    assert "auth.session.ttl_seconds" in paths
    assert "auth.jwt.access_token_minutes" in paths
    assert any("SECURE" in i.message for i in err.value.issues)
    assert any(
        i.source == "SESSION_TTL_SECONDS" or "env" in i.source for i in err.value.issues
    )


def test_unknown_section_and_unknown_key_are_rejected(tmp_path):
    (tmp_path / "site.yaml").write_text(
        "bogus:\n  a: 1\nserver:\n  log_levl: debug\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as err:
        make(config_dir=tmp_path)
    text = str(err.value)
    assert "unknown config section" in text
    assert "log_levl" in text


def test_site_type_other_than_onprem_means_cloud():
    assert (
        make({"SG_SITE_TYPE": "Cloud"}).get("prodtrack").shotgrid.site_type == "cloud"
    )
    assert (
        make({"SG_SITE_TYPE": "OnPrem"}).get("prodtrack").shotgrid.site_type == "onprem"
    )
    assert (
        make({"SG_SITE_TYPE": "whatever"}).get("prodtrack").shotgrid.site_type
        == "cloud"
    )


# ── runtime overrides ────────────────────────────────────────────────────────


def test_apply_overrides_reports_changes_and_restart_flags():
    cfg = make()
    result = cfg.apply_overrides(
        {
            "features": {"transcript_publish": True},
            "prodtrack": {"connector": "mock"},
        }
    )
    assert cfg.get("features").transcript_publish is True
    assert "features.transcript_publish" in result.changed
    assert "prodtrack.connector" in result.changed
    assert result.restart_required == ["prodtrack.connector"]  # hot vs restart
    assert result.version == cfg.version == 2
    assert cfg.provenance()["features.transcript_publish"] == "runtime"


def test_bootstrap_sections_cannot_be_changed_at_runtime():
    cfg = make()
    for section, patch in (
        ("auth", {"auth": {"provider": "none"}}),
        ("storage", {"storage": {"mongodb_db": "elsewhere"}}),
        ("server", {"server": {"log_level": "debug"}}),
        ("admin", {"admin": {"admins": ["me@x.com"]}}),
    ):
        with pytest.raises(
            ConfigError, match=f"'{section}' section cannot be changed at runtime"
        ):
            cfg.apply_overrides(patch)
    assert cfg.get("auth").provider == "none" and cfg.version == 1  # nothing applied
    # ... but a trusted caller (bootstrap, tests) may:
    cfg.apply_overrides({"server": {"log_level": "debug"}}, allow_locked=True)
    assert cfg.get("server").log_level == "DEBUG"


def test_dry_run_reports_without_applying():
    cfg = make()
    result = cfg.apply_overrides(
        {"features": {"transcript_publish": True}}, dry_run=True
    )
    assert result.changed == ["features.transcript_publish"]
    assert cfg.get("features").transcript_publish is False and cfg.version == 1
    with pytest.raises(ConfigError):  # a dry run validates just as hard
        cfg.apply_overrides(
            {"features": {"transcript_publish": "banana"}}, dry_run=True
        )


def test_replace_swaps_the_whole_override_set():
    cfg = make()
    cfg.apply_overrides({"features": {"transcript_publish": True}})
    cfg.apply_overrides({"llm": {"provider": "gemini"}}, replace=True)
    assert cfg.get("llm").provider == "gemini"
    assert cfg.get("features").transcript_publish is False  # the earlier change is gone


def test_failed_override_leaves_configuration_untouched():
    cfg = make()
    with pytest.raises(ConfigError):
        cfg.apply_overrides({"auth": {"session": {"ttl_seconds": 0}}})
    assert cfg.get("auth").session.ttl_seconds == 28800
    assert cfg.version == 1


def test_plaintext_secret_override_is_refused_but_reference_accepted():
    cfg = make({"VEXA": "x", "VEXA_API_KEY": "from-env"})
    with pytest.raises(ConfigError) as err:
        cfg.apply_overrides({"transcription": {"vexa": {"api_key": "plaintext"}}})
    assert "reference" in str(err.value)

    cfg.apply_overrides({"transcription": {"vexa": {"api_key": "${env:VEXA_API_KEY}"}}})
    assert cfg.get("transcription").vexa.api_key == "from-env"


# ── per-project scope ────────────────────────────────────────────────────────


def test_project_override_applies_only_to_that_project(tmp_path):
    (tmp_path / "site.yaml").write_text(
        "projects:\n"
        "  '42':\n"
        "    features:\n"
        "      transcript_publish: true\n"
        "    prodtrack:\n"
        "      shotgrid:\n"
        "        transcript_entity: CustomEntity05\n",
        encoding="utf-8",
    )
    cfg = make(config_dir=tmp_path)
    assert cfg.get("features").transcript_publish is False
    assert cfg.get_for_project("features", "42").transcript_publish is True
    assert cfg.get_for_project("features", "7").transcript_publish is False
    assert (
        cfg.get_for_project("prodtrack", "42").shotgrid.transcript_entity
        == "CustomEntity05"
    )


def test_project_override_of_a_site_only_setting_is_rejected(tmp_path):
    (tmp_path / "site.yaml").write_text(
        "projects:\n  '42':\n    storage:\n      mongodb_db: other\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as err:
        make(config_dir=tmp_path)
    assert "cannot be overridden per project" in str(err.value)


def test_schema_covers_every_section_and_flags_secrets():
    schema = make().schema()
    assert set(schema) >= {
        "server",
        "auth",
        "storage",
        "prodtrack",
        "llm",
        "transcription",
        "features",
        "content",
    }
    api_key = schema["transcription"]["$defs"]["VexaConfig"]["properties"]["api_key"]
    assert api_key["secret"] is True and api_key["reload"] == "hot"


# ── runtime references cannot read arbitrary server secrets ──────────────────


def _runtime_change(env, patch, config_dir=None):
    svc = make(env, config_dir=config_dir)
    svc.apply_overrides(patch)
    return svc


def test_runtime_cannot_point_a_plain_setting_at_an_environment_variable():
    with pytest.raises(ConfigError, match="only allowed on secret settings"):
        _runtime_change(
            {"TOP_SECRET": "x"}, {"prodtrack": {"ayon": {"url": "${env:TOP_SECRET}"}}}
        )


def test_runtime_cannot_reference_an_undeclared_variable_even_on_a_secret_setting():
    with pytest.raises(ConfigError, match="cannot be referenced from runtime"):
        _runtime_change(
            {"JWT_SECRET": "x"},
            {"prodtrack": {"ayon": {"api_key": "${env:JWT_SECRET}"}}},
        )


def test_runtime_cannot_read_files():
    with pytest.raises(ConfigError, match="file references cannot be set at runtime"):
        _runtime_change({}, {"prodtrack": {"ayon": {"api_key": "${file:/etc/passwd}"}}})


def test_runtime_may_reference_the_variable_a_secret_setting_documents():
    svc = _runtime_change(
        {"AYON_API_KEY": "key-value"},
        {"prodtrack": {"ayon": {"api_key": "${env:AYON_API_KEY}"}}},
    )
    assert svc.get("prodtrack").ayon.api_key == "key-value"


def test_runtime_may_reuse_a_reference_the_operator_wrote_in_the_files(tmp_path):
    (tmp_path / "site.yaml").write_text(
        "prodtrack:\n  ayon:\n    api_key: ${env:STUDIO_AYON_KEY}\n"
    )
    svc = _runtime_change(
        {"STUDIO_AYON_KEY": "k"},
        {"llm": {"profiles": {"openai": {"api_key": "${env:STUDIO_AYON_KEY}"}}}},
        config_dir=tmp_path,
    )
    assert svc.get("llm").profiles["openai"].api_key == "k"


def test_a_rejected_reference_in_a_dry_run_changes_nothing():
    svc = make({"TOP_SECRET": "x"})
    with pytest.raises(ConfigError):
        svc.apply_overrides(
            {"prodtrack": {"ayon": {"url": "${env:TOP_SECRET}"}}}, dry_run=True
        )
    assert svc.get("prodtrack").ayon.url is None or "x" not in str(
        svc.get("prodtrack").ayon.url
    )


# ── tracker URLs ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://ayon.test",
        "gopher://ayon.test",
        "//ayon.test",
        "ayon.test:5000",
        "http://user:pass@ayon.test",
        "http://",
    ],
)
@pytest.mark.parametrize("tracker", ["ayon", "shotgrid"])
def test_a_tracker_url_must_be_a_plain_http_url(tracker, url):
    with pytest.raises(ConfigError):
        make({}, runtime={"prodtrack": {tracker: {"url": url}}})


def test_a_tracker_url_is_tidied():
    svc = make({}, runtime={"prodtrack": {"ayon": {"url": " http://ayon.test:5000/ "}}})
    assert svc.get("prodtrack").ayon.url == "http://ayon.test:5000"


def test_runtime_may_reference_the_documented_per_profile_llm_keys():
    svc = _runtime_change(
        {"OPENAI_API_KEY": "sk-from-env"},
        {"llm": {"profiles": {"openai": {"api_key": "${env:OPENAI_API_KEY}"}}}},
    )
    assert svc.get("llm").profiles["openai"].api_key == "sk-from-env"


# ── warnings ─────────────────────────────────────────────────────────────────


def test_a_plain_secret_in_a_file_is_allowed_but_warned_about(tmp_path):
    (tmp_path / "site.yaml").write_text(
        "prodtrack:\n  ayon:\n    api_key: plainvalue\n"
    )
    svc = make({}, config_dir=tmp_path)
    assert svc.get("prodtrack").ayon.api_key == "plainvalue"
    assert any("prodtrack.ayon.api_key" in w for w in svc.warnings)
    assert "plainvalue" not in " ".join(svc.warnings)  # never echoes the value


def test_a_reference_in_a_file_is_not_warned_about(tmp_path):
    (tmp_path / "site.yaml").write_text(
        "prodtrack:\n  ayon:\n    api_key: ${env:AYON_API_KEY}\n"
    )
    assert make({"AYON_API_KEY": "k"}, config_dir=tmp_path).warnings == []
