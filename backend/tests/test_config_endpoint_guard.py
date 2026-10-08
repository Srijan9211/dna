"""An administrator must not be able to aim DNA's credentials at a server of their choosing.

A runtime change may point a tracker, LLM or transcription URL only at a host the
operator already trusts.  Without that, pairing a (permitted) reference to the real API
key with a URL the administrator controls would hand the key to them.
"""

import http.server
import threading

import pytest

from dna import configsys
from dna.configsys import ConfigError, ConfigService, default_registry

ENV = {"AYON_API_KEY": "REAL-SERVICE-KEY"}


def make(environ=None, config_dir=None, runtime=None):
    return ConfigService(
        default_registry,
        environ={**ENV, **(environ or {})},
        config_dir=config_dir,
        runtime_overrides=runtime,
    ).load()


def ayon(url):
    return {"prodtrack": {"ayon": {"url": url, "api_key": "${env:AYON_API_KEY}"}}}


def test_a_runtime_change_cannot_point_the_tracker_at_a_new_host():
    with pytest.raises(ConfigError, match="not trusted for runtime changes"):
        make(runtime=ayon("http://attacker.example"))


def test_the_host_the_operator_configured_stays_usable_at_runtime():
    svc = make(
        {"AYON_URL": "https://ayon.studio.example"},
        runtime=ayon(
            "https://ayon.studio.example:8443/other"
        ),  # same host, other port/path
    )
    assert svc.get("prodtrack").ayon.url == "https://ayon.studio.example:8443/other"


def test_a_host_in_a_config_file_is_trusted(tmp_path):
    (tmp_path / "s.yaml").write_text(
        "prodtrack:\n  ayon:\n    url: https://a.example\n"
    )
    svc = make(config_dir=tmp_path, runtime=ayon("https://a.example/x"))
    assert svc.get("prodtrack").ayon.url == "https://a.example/x"
    with pytest.raises(ConfigError):
        make(config_dir=tmp_path, runtime=ayon("https://b.example"))


def test_an_operator_can_allow_more_hosts():
    svc = make(
        {"DNA_CONFIG_ALLOWED_HOSTS": "ayon.a.example, ayon.b.example"},
        runtime=ayon("https://AYON.B.example"),  # case does not matter
    )
    assert svc.get("prodtrack").ayon.url == "https://AYON.B.example"


def test_the_allowlist_cannot_be_widened_at_runtime():
    with pytest.raises(ConfigError):
        make(
            runtime={
                "admin": {"allowed_hosts": ["attacker.example"]},
                **ayon("http://attacker.example"),
            }
        )


def test_other_endpoints_are_guarded_too():
    for patch in (
        {"transcription": {"vexa": {"api_url": "http://attacker.example"}}},
        {"llm": {"profiles": {"openai": {"url": "http://attacker.example"}}}},
        {"prodtrack": {"shotgrid": {"url": "http://attacker.example"}}},
    ):
        with pytest.raises(ConfigError, match="not trusted"):
            make(runtime=patch)


def test_a_built_in_default_endpoint_can_be_restored_at_runtime():
    svc = make(
        {"VEXA_API_URL": "https://vexa.studio.example"},
        runtime={"transcription": {"vexa": {"api_url": "https://api.cloud.vexa.ai"}}},
    )
    assert svc.get("transcription").vexa.api_url == "https://api.cloud.vexa.ai"


def test_values_that_are_not_endpoints_are_unaffected():
    svc = make(
        runtime={
            "llm": {"provider": "gemini"},
            "features": {"transcript_publish": True},
        }
    )
    assert svc.get("llm").provider == "gemini"


def test_a_rollback_or_import_cannot_smuggle_a_host_in(monkeypatch):
    """Both replay stored overrides through the same validation as a new change."""
    configsys.reset_config()
    with pytest.raises(ConfigError, match="not trusted"):
        configsys.import_overrides(
            {"format": 1, "overrides": ayon("http://attacker.example")}, actor="x"
        )


# ── the original exploit, end to end ─────────────────────────────────────────


def test_the_handshake_never_sends_the_service_key_to_a_host_the_admin_chose(
    monkeypatch,
):
    from dna.prodtrack_providers.handshake import check_candidate

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["key"] = self.headers.get("X-Api-Key")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_POST = do_GET

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("AYON_API_KEY", "REAL-SERVICE-KEY")
        monkeypatch.setenv("AYON_URL", "http://real-ayon.internal")
        monkeypatch.setenv("PRODTRACK_PROVIDER", "ayon")
        configsys.reset_config()
        with pytest.raises(ConfigError, match="not trusted"):
            check_candidate(ayon(f"http://127.0.0.1:{server.server_port}"))
        assert "key" not in seen  # nothing was ever sent
    finally:
        server.shutdown()
        configsys.reset_config()
