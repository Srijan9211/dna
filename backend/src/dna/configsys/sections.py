"""Registered config sections.

Defaults and environment aliases here mirror, one for one, what the code reads
today with ``os.getenv`` (see ``tests/configsys/test_env_coverage.py``, which
fails if a new ``os.getenv`` name is added without being registered).  Loading
through this module therefore changes no existing behaviour.

New, tracker-neutral keys (e.g. ``prodtrack.ayon``) are added alongside the
legacy ShotGrid ones; they have no legacy alias because nothing set them before.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BeforeValidator, field_validator, model_validator

from dna.configsys.meta import Section, setting
from dna.configsys.registry import default_registry


def _csv(value: Any) -> Any:
    """Accept ``"a, b"`` as well as ``["a", "b"]`` (env vars are strings)."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


CsvList = Annotated[list[str], BeforeValidator(_csv)]


# ── server ───────────────────────────────────────────────────────────────────


class ServerSection(Section):
    log_level: str = setting(
        "INFO", env="LOG_LEVEL", reload="restart", description="Python log level."
    )
    disable_docs: bool = setting(
        False,
        env="DISABLE_DOCS",
        reload="restart",
        description="Hide the OpenAPI docs endpoints.",
    )
    attachment_store_dir: str = setting(
        "/tmp/dna_attachments",
        env="ATTACHMENT_STORE_DIR",
        reload="restart",
        description="Directory for uploaded note attachments.",
    )
    cors_allowed_origins: CsvList = setting(
        default_factory=list,
        env="CORS_ALLOWED_ORIGINS",
        reload="restart",
        description="Origins allowed by CORS (comma separated in the environment).",
    )
    cloud_run_service: Optional[str] = setting(
        None, env="K_SERVICE", reload="restart", description="Set by Cloud Run."
    )
    cloud_run_revision: Optional[str] = setting(
        None, env="K_REVISION", reload="restart", description="Set by Cloud Run."
    )

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


# ── auth ─────────────────────────────────────────────────────────────────────


class JwtConfig(Section):
    secret_key: str = setting(
        "", env="JWT_SECRET_KEY", secret=True, reload="restart", description="HMAC key."
    )
    algorithm: str = setting("HS256", env="JWT_ALGORITHM", reload="restart")
    issuer: str = setting("dna-backend", env="JWT_ISSUER", reload="restart")
    audience: str = setting("dna-api", env="JWT_AUDIENCE", reload="restart")
    access_token_minutes: int = setting(
        15, env="JWT_EXPIRE_MINUTES", reload="restart", ge=1
    )


class SessionConfig(Section):
    ttl_seconds: int = setting(28800, env="SESSION_TTL_SECONDS", reload="restart", ge=1)
    max_lifetime_seconds: int = setting(
        12 * 60 * 60, env="SESSION_MAX_LIFETIME_SECONDS", reload="restart", ge=1
    )


class LoginThrottleConfig(Section):
    enabled: bool = setting(True, reload="restart")
    max_failures: int = setting(
        10,
        ge=1,
        description="Failed password attempts allowed per address and user name "
        "within the window (three times as many per user name alone).",
    )
    window_seconds: float = setting(300.0, gt=0)


class RefreshCookieConfig(Section):
    samesite: str = setting(
        "strict",
        env="REFRESH_COOKIE_SAMESITE",
        reload="restart",
        description="strict | lax | none.  Use none only for cross-site frontends.",
    )
    secure: bool = setting(True, env="REFRESH_COOKIE_SECURE", reload="restart")

    @field_validator("samesite")
    @classmethod
    def _check_samesite(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"strict", "lax", "none"}:
            raise ValueError(
                f"REFRESH_COOKIE_SAMESITE must be strict, lax or none (got '{v}')."
            )
        return v

    @model_validator(mode="after")
    def _none_needs_secure(self) -> "RefreshCookieConfig":
        if self.samesite == "none" and not self.secure:
            raise ValueError(
                "REFRESH_COOKIE_SAMESITE=none requires REFRESH_COOKIE_SECURE=true; "
                "browsers reject the cookie otherwise."
            )
        return self


class AuthSection(Section):
    provider: str = setting(
        "none",
        env="AUTH_PROVIDER",
        reload="restart",
        description="none | google | shotgrid | ayon (more are added by connectors).",
    )
    google_client_id: Optional[str] = setting(None, env="GOOGLE_CLIENT_ID")
    jwt: JwtConfig = setting(default_factory=JwtConfig)
    session: SessionConfig = setting(default_factory=SessionConfig)
    login_throttle: LoginThrottleConfig = setting(default_factory=LoginThrottleConfig)
    refresh_cookie: RefreshCookieConfig = setting(default_factory=RefreshCookieConfig)

    @field_validator("provider")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


# ── storage ──────────────────────────────────────────────────────────────────


class StorageSection(Section):
    provider: str = setting("mongodb", env="STORAGE_PROVIDER", reload="restart")
    mongodb_url: str = setting(
        "mongodb://localhost:27017",
        env="MONGODB_URL",
        secret=True,
        reload="restart",
        description="May embed credentials, so it is treated as a secret.",
    )
    mongodb_db: str = setting("dna", env="MONGODB_DB", reload="restart")


# ── prodtrack ────────────────────────────────────────────────────────────────


def _check_tracker_url(value: Optional[str]) -> Optional[str]:
    """A tracker URL must be http(s) with a host and carry no credentials.

    Credentials embedded in a URL end up in logs and error messages; any other scheme
    (file:, ftp:, gopher: ...) is never a tracker."""
    if value is None or value == "":
        return value
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http:// or https:// URL with a host name")
    if parts.username or parts.password:
        raise ValueError("must not contain a user name or password")
    return value.strip().rstrip("/")


class ShotgridConnection(Section):
    url: Optional[str] = setting(None, env="SHOTGRID_URL", endpoint=True)
    script_name: Optional[str] = setting(None, env="SHOTGRID_SCRIPT_NAME")
    api_key: Optional[str] = setting(None, env="SHOTGRID_API_KEY", secret=True)
    sudo_user: Optional[str] = setting(
        None,
        env="SHOTGRID_SUDO_USER",
        description="Default impersonation for background jobs.",
    )
    site_type: Literal["cloud", "onprem"] = setting("cloud", env="SG_SITE_TYPE")
    transcript_entity: str = setting(
        "CustomEntity01",
        env="SHOTGRID_TRANSCRIPT_ENTITY",
        scope=("site", "project"),
        description="Custom-entity slot that stores published transcripts.",
    )

    @field_validator("site_type", mode="before")
    @classmethod
    def _site_type(cls, v: Any) -> Any:
        # Existing behaviour: anything other than "onprem" means cloud.
        if isinstance(v, str):
            return "onprem" if v.strip().lower() == "onprem" else "cloud"
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: Optional[str]) -> Optional[str]:
        return _check_tracker_url(v)


class AyonConnection(Section):
    url: Optional[str] = setting(None, env="AYON_URL", endpoint=True)
    api_key: Optional[str] = setting(
        None, env="AYON_API_KEY", secret=True, description="Service-user API key."
    )
    impersonation_header: str = setting(
        "x-as-user",
        description="Header used to act as the signed-in user (service users only).",
    )
    sudo_user: Optional[str] = setting(
        None,
        env="AYON_SUDO_USER",
        description="Default user to act as for background jobs; unset means the "
        "service account itself.",
    )
    timeout: float = setting(30.0, env="AYON_TIMEOUT", gt=0, description="Seconds.")

    @field_validator("url")
    @classmethod
    def _url(cls, v: Optional[str]) -> Optional[str]:
        return _check_tracker_url(v)


class MockConnection(Section):
    api_base_url: str = setting("http://localhost:8000", env="API_BASE_URL")


class ProdtrackSection(Section):
    connector: str = setting(
        "shotgrid",
        env="PRODTRACK_PROVIDER",
        reload="restart",
        description="shotgrid | ayon | mock (ftrack and kitsu follow).",
    )
    startup_check_timeout: float = setting(10.0, env="SG_STARTUP_CHECK_TIMEOUT", gt=0)
    shotgrid: ShotgridConnection = setting(default_factory=ShotgridConnection)
    ayon: AyonConnection = setting(default_factory=AyonConnection)
    mock: MockConnection = setting(default_factory=MockConnection)
    profile_path: Optional[str] = setting(
        None,
        reload="restart",
        description="A complete connector profile file, replacing the one DNA ships "
        "for this connector (use for a tracker DNA has no profile for).",
    )
    profile: dict[str, Any] = setting(
        default_factory=dict,
        description="Studio overrides deep-merged over the connector profile: "
        "entity and field mappings, labels, capabilities, URLs.",
    )

    @field_validator("connector")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()

    @model_validator(mode="after")
    def _profile_is_valid(self) -> "ProdtrackSection":
        # Fail at startup, not on the first request.  A connector with no profile
        # at all is allowed (a provider that needs none); one with a broken
        # profile is not.
        from dna.prodtrack_providers.profile import has_profile, load_profile

        if has_profile(self.connector, self.profile_path):
            load_profile(self.connector, path=self.profile_path, overrides=self.profile)
        elif self.profile:
            raise ValueError(
                f"prodtrack.profile has overrides but connector '{self.connector}' "
                "has no profile to apply them to; set prodtrack.profile_path"
            )
        return self


# ── llm ──────────────────────────────────────────────────────────────────────


class LlmProfile(Section):
    """One named LLM endpoint.  Unset values fall back to the provider's own
    defaults (e.g. the default model baked into each provider class)."""

    api_key: Optional[str] = setting(None, secret=True)
    model: Optional[str] = None
    timeout: Optional[float] = setting(None, gt=0)
    url: Optional[str] = setting(None, endpoint=True)


class LlmSection(Section):
    provider: str = setting(
        "openai", env="LLM_PROVIDER", description="Default profile."
    )
    profiles: dict[str, LlmProfile] = setting(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


# LLM_PROVIDER value -> prefix the providers use for their env vars today.
_LLM_ENV_PREFIX = {
    "openai": "OPENAI",
    "gemini": "GEMINI",
    "anthropic": "ANTHROPIC",
    "custom": "CUSTOM_LLM",
}
_LLM_ENV_FIELDS = (
    ("API_KEY", "api_key"),
    ("MODEL", "model"),
    ("TIMEOUT", "timeout"),
    ("URL", "url"),
)
_LLM_ENV_NAMES = tuple(
    f"{prefix}_{suffix}"
    for prefix in _LLM_ENV_PREFIX.values()
    for suffix, _ in _LLM_ENV_FIELDS
)


def llm_profile_from_env(
    prefix: str, environ: Optional[Mapping[str, str]] = None
) -> LlmProfile:
    """Build a profile from ``{prefix}_API_KEY|MODEL|TIMEOUT|URL``.

    Compatibility for providers whose name is not one of the built-ins (a
    third-party provider loaded through an entry point, or a test stub): they
    historically got their settings from env vars derived from their own name.
    """
    environ = os.environ if environ is None else environ
    values = {
        field_name: environ[f"{prefix}_{suffix}"]
        for suffix, field_name in _LLM_ENV_FIELDS
        if environ.get(f"{prefix}_{suffix}", "") != ""
    }
    return LlmProfile(**values)


def _llm_legacy_env(
    environ: Mapping[str, str],
) -> Iterable[tuple[tuple[str, ...], Any, str]]:
    for profile, prefix in _LLM_ENV_PREFIX.items():
        for suffix, field_name in _LLM_ENV_FIELDS:
            name = f"{prefix}_{suffix}"
            if environ.get(name, "") != "":
                yield ("profiles", profile, field_name), environ[name], name


# ── transcription ────────────────────────────────────────────────────────────


class VexaConfig(Section):
    api_url: str = setting(
        "https://api.cloud.vexa.ai", env="VEXA_API_URL", endpoint=True
    )
    api_key: str = setting("", env="VEXA_API_KEY", secret=True)


class TranscriptionSection(Section):
    provider: str = setting("vexa", env="TRANSCRIPTION_PROVIDER", reload="restart")
    vexa: VexaConfig = setting(default_factory=VexaConfig)


# ── features / content ───────────────────────────────────────────────────────


class FeaturesSection(Section):
    transcript_publish: bool = setting(
        False,
        env="DNA_ENABLE_TRANSCRIPT_PUBLISH",
        scope=("site", "project"),
        description="Publish meeting transcripts to the production tracker.",
    )
    testing_enabled: bool = setting(
        False,
        env="DNA_TESTING_ENABLED",
        reload="restart",
        description="Enables dev-only test endpoints.  Never enable in production.",
    )


class ContentSection(Section):
    default_note_prompt_path: Optional[str] = setting(
        None, env="DNA_DEFAULT_NOTE_PROMPT_PATH"
    )
    glossary_global_path: Optional[str] = setting(None, env="DNA_GLOSSARY_GLOBAL_PATH")


# ── frontend ─────────────────────────────────────────────────────────────────


class FrontendSection(Section):
    """Settings the browser app reads at boot (``GET /config/public``) instead of
    having them baked in at build time, so one image serves every deployment.

    Nothing here may be secret: it is served unauthenticated.  ``None`` means "use
    the app's own default".  The API address itself stays a build-time/same-origin
    matter, since the browser needs it to ask for this.
    """

    feature_transcription: Optional[bool] = setting(
        None,
        env="DNA_FEATURE_TRANSCRIPTION",
        description="Force transcription on/off for everyone.",
    )
    feature_in_review: Optional[bool] = setting(
        None,
        env="DNA_FEATURE_IN_REVIEW",
        description="Force the in-review view on/off for everyone.",
    )
    feature_ai: Optional[bool] = setting(
        None,
        env="DNA_FEATURE_AI",
        description="Force AI note generation on/off for everyone.",
    )
    ws_url: Optional[str] = setting(
        None,
        env="DNA_WS_URL",
        description="WebSocket URL for live events, if not derivable from the page.",
    )
    prodtrack_tab_sync_extension_id: Optional[str] = setting(
        None,
        env="DNA_PRODTRACK_TAB_SYNC_EXTENSION_ID",
        description="Browser extension id that keeps a tracker tab in sync with the selected version.",
    )
    dev_mode: bool = setting(
        False, env="DNA_DEV_MODE", description="Show developer-only UI."
    )


# ── admin ────────────────────────────────────────────────────────────────────


class AdminSection(Section):
    """Who may change configuration at runtime, and how changes persist."""

    enabled: bool = setting(
        False,
        env="DNA_CONFIG_ADMIN_ENABLED",
        reload="restart",
        description="Serve the /config admin API.  Off by default.",
    )
    admins: CsvList = setting(
        default_factory=list,
        env="DNA_CONFIG_ADMINS",
        reload="restart",
        description="Who may change configuration (case-insensitive): tracker logins for users who sign in through a tracker (ShotGrid, AYON), emails for Google sign-in.",
    )
    persist: bool = setting(
        False,
        env="DNA_CONFIG_PERSIST",
        reload="restart",
        description="Keep runtime changes in MongoDB (shared by every instance, with "
        "history and rollback).  Off: changes live in this process only.",
    )
    allow_unauthenticated: bool = setting(
        False,
        env="DNA_CONFIG_ALLOW_UNAUTHENTICATED",
        reload="restart",
        description="Development only: let an unauthenticated caller administer "
        "config when AUTH_PROVIDER=none.  Refused when authentication is on.",
    )
    allowed_hosts: CsvList = setting(
        default_factory=list,
        env="DNA_CONFIG_ALLOWED_HOSTS",
        reload="restart",
        description="Host names an administrator may point a tracker, LLM or "
        "transcription URL at, beyond the hosts already in the config files and "
        "environment.  Without this a runtime change can only keep using the "
        "configured hosts, so API keys cannot be redirected.",
    )
    refresh_seconds: float = setting(
        5.0,
        env="DNA_CONFIG_REFRESH_SECONDS",
        gt=0,
        description="How often this instance checks the store for newer changes.",
    )


# ── registration ─────────────────────────────────────────────────────────────

default_registry.register("server", ServerSection, runtime_overridable=False)
default_registry.register("auth", AuthSection, runtime_overridable=False)
default_registry.register("storage", StorageSection, runtime_overridable=False)
default_registry.register("prodtrack", ProdtrackSection)
default_registry.register(
    "llm",
    LlmSection,
    legacy_env=_llm_legacy_env,
    legacy_env_names=_LLM_ENV_NAMES,
)
default_registry.register("transcription", TranscriptionSection)
default_registry.register("features", FeaturesSection)
default_registry.register("frontend", FrontendSection)
default_registry.register("content", ContentSection)
default_registry.register("admin", AdminSection, runtime_overridable=False)
