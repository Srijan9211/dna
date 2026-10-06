"""The config system is the only reader of the environment.

Two rules, enforced here so they cannot silently erode:

1. No module outside ``dna/configsys`` reads ``os.environ`` / ``os.getenv``
   directly.  Everything is declared in the registry, which is what makes it
   visible to the schema, the admin UI, validation and provenance.
2. Every environment variable the registry exposes is unique and documented.

If you need a new setting, add it to ``dna/configsys/sections.py`` (with its
default) and read it through ``get_config()``.
"""

import re
from pathlib import Path

from dna.configsys import default_registry
from dna.configsys.meta import iter_fields, meta

SRC = Path(__file__).resolve().parents[1] / "src"

_ENV_ACCESS = re.compile(r"\bos\.(?:getenv|environ)\b")

# Files allowed to touch the environment directly, with the reason.  Keep empty
# unless there is a real need (bootstrap code that must run before config).
ALLOWED: dict[str, str] = {}


def _violations() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("dna/configsys/") or rel in ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _ENV_ACCESS.search(code):
                found.setdefault(rel, []).append(number)
    return found


def test_nothing_outside_configsys_reads_the_environment():
    violations = _violations()
    assert not violations, (
        "These modules read the environment directly. Register the setting in "
        "dna/configsys/sections.py and use get_config() instead: "
        f"{violations}"
    )


def test_the_scan_pattern_still_detects_environment_reads():
    # Guards against the regex silently matching nothing and the ratchet above
    # passing vacuously.
    for snippet in (
        'os.getenv("X")',
        "os.environ.get('X')",
        'os.environ["X"]',
        "value = os.getenv(name)",
    ):
        assert _ENV_ACCESS.search(snippet), snippet
    assert not _ENV_ACCESS.search("environment = {}")


def test_registry_has_no_duplicate_env_aliases():
    seen: dict[str, str] = {}
    for spec in default_registry.sections():
        for path, info in iter_fields(spec.model):
            env = meta(info).get("env")
            if not env:
                continue
            here = f"{spec.name}.{'.'.join(path)}"
            assert env not in seen, f"{env} maps to both {seen[env]} and {here}"
            seen[env] = here


def test_every_registered_alias_is_known_to_the_registry():
    known = default_registry.known_env_names()
    # Spot-check the aliases existing deployments rely on.
    for name in (
        "SHOTGRID_URL",
        "SHOTGRID_SCRIPT_NAME",
        "SHOTGRID_API_KEY",
        "AUTH_PROVIDER",
        "PRODTRACK_PROVIDER",
        "LLM_PROVIDER",
        "OPENAI_API_KEY",
        "GEMINI_MODEL",
        "ANTHROPIC_TIMEOUT",
        "CUSTOM_LLM_URL",
        "MONGODB_URL",
        "VEXA_API_KEY",
        "JWT_SECRET_KEY",
        "CORS_ALLOWED_ORIGINS",
        "DNA_ENABLE_TRANSCRIPT_PUBLISH",
    ):
        assert name in known, name


def test_section_defaults_match_the_constants_the_code_still_uses():
    from dna.auth_providers.shotgrid_sso import (
        DEFAULT_ACCESS_TOKEN_MINUTES,
        DEFAULT_JWT_AUDIENCE,
        DEFAULT_JWT_ISSUER,
        DEFAULT_SESSION_MAX_LIFETIME_SECONDS,
    )
    from dna.configsys.sections import AuthSection

    auth = AuthSection()
    assert auth.jwt.issuer == DEFAULT_JWT_ISSUER
    assert auth.jwt.audience == DEFAULT_JWT_AUDIENCE
    assert auth.jwt.access_token_minutes == DEFAULT_ACCESS_TOKEN_MINUTES
    assert auth.session.max_lifetime_seconds == DEFAULT_SESSION_MAX_LIFETIME_SECONDS
