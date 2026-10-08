# Configuration reference

_Generated from the settings registry by `backend/scripts/generate_config_reference.py`. Do not edit by hand._

Every setting can be given in a YAML file, as the environment variable shown, or as `DNA__<SECTION>__<PATH>` (for example `DNA__SERVER__LOG_LEVEL`). See [CONFIGURATION.md](CONFIGURATION.md) for how the layers combine.

* **Restart** settings are read at startup; the others apply live.
* **Secret** settings accept only a reference such as `${env:NAME}`, never the value.
* Sections `admin`, `auth`, `server`, `storage` are read at startup and cannot be changed through the admin API.
* **Project** scope means the setting may also be overridden for one project.

## `server` _(locked: files and environment only)_

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `log_level` | `LOG_LEVEL` | `INFO` | restart | Python log level. |
| `disable_docs` | `DISABLE_DOCS` | `false` | restart | Hide the OpenAPI docs endpoints. |
| `attachment_store_dir` | `ATTACHMENT_STORE_DIR` | `/tmp/dna_attachments` | restart | Directory for uploaded note attachments. |
| `event_bus` | `DNA_EVENT_BUS` | `memory` | restart | How live events (transcripts, bot status) reach browsers when more than one backend instance runs.  'memory': one instance only.  'mongodb': instances share events through the storage MongoDB, and each meeting is handled by exactly one of them. |
| `cors_allowed_origins` | `CORS_ALLOWED_ORIGINS` | _empty_ | restart | Origins allowed by CORS (comma separated in the environment). |
| `cloud_run_service` | `K_SERVICE` | _unset_ | restart | Set by Cloud Run. |
| `cloud_run_revision` | `K_REVISION` | _unset_ | restart | Set by Cloud Run. |

## `auth` _(locked: files and environment only)_

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `provider` | `AUTH_PROVIDER` | `none` | restart | none \| google \| shotgrid \| ayon (more are added by connectors). |
| `google_client_id` | `GOOGLE_CLIENT_ID` | _unset_ |  |  |
| `jwt.secret_key` | `JWT_SECRET_KEY` | _unset_ | secret, restart | HMAC key. |
| `jwt.algorithm` | `JWT_ALGORITHM` | `HS256` | restart |  |
| `jwt.issuer` | `JWT_ISSUER` | `dna-backend` | restart |  |
| `jwt.audience` | `JWT_AUDIENCE` | `dna-api` | restart |  |
| `jwt.access_token_minutes` | `JWT_EXPIRE_MINUTES` | `15` | restart |  |
| `session.ttl_seconds` | `SESSION_TTL_SECONDS` | `28800` | restart |  |
| `session.max_lifetime_seconds` | `SESSION_MAX_LIFETIME_SECONDS` | `43200` | restart |  |
| `login_throttle.enabled` |  | `true` | restart |  |
| `login_throttle.max_failures` |  | `10` |  | Failed password attempts allowed per address and user name within the window (three times as many per user name alone). |
| `login_throttle.window_seconds` |  | `300.0` |  |  |
| `refresh_cookie.samesite` | `REFRESH_COOKIE_SAMESITE` | `strict` | restart | strict \| lax \| none.  Use none only for cross-site frontends. |
| `refresh_cookie.secure` | `REFRESH_COOKIE_SECURE` | `true` | restart |  |

## `storage` _(locked: files and environment only)_

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `provider` | `STORAGE_PROVIDER` | `mongodb` | restart |  |
| `mongodb_url` | `MONGODB_URL` | `mongodb://localhost:27017` | secret, restart | May embed credentials, so it is treated as a secret. |
| `mongodb_db` | `MONGODB_DB` | `dna` | restart |  |

## `prodtrack`

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `connector` | `PRODTRACK_PROVIDER` | `shotgrid` | restart | shotgrid \| ayon \| mock (ftrack and kitsu follow). |
| `startup_check_timeout` | `SG_STARTUP_CHECK_TIMEOUT` | `10.0` |  |  |
| `shotgrid.url` | `SHOTGRID_URL` | _unset_ |  |  |
| `shotgrid.script_name` | `SHOTGRID_SCRIPT_NAME` | _unset_ |  |  |
| `shotgrid.api_key` | `SHOTGRID_API_KEY` | _unset_ | secret |  |
| `shotgrid.sudo_user` | `SHOTGRID_SUDO_USER` | _unset_ |  | Default impersonation for background jobs. |
| `shotgrid.site_type` | `SG_SITE_TYPE` | `cloud` |  |  |
| `shotgrid.transcript_entity` | `SHOTGRID_TRANSCRIPT_ENTITY` | `CustomEntity01` | project | Custom-entity slot that stores published transcripts. |
| `ayon.url` | `AYON_URL` | _unset_ |  |  |
| `ayon.api_key` | `AYON_API_KEY` | _unset_ | secret | Service-user API key. |
| `ayon.impersonation_header` |  | `x-as-user` |  | Header used to act as the signed-in user (service users only). |
| `ayon.sudo_user` | `AYON_SUDO_USER` | _unset_ |  | Default user to act as for background jobs; unset means the service account itself. |
| `ayon.timeout` | `AYON_TIMEOUT` | `30.0` |  | Seconds. |
| `mock.api_base_url` | `API_BASE_URL` | `http://localhost:8000` |  |  |
| `profile_path` |  | _unset_ | restart | A complete connector profile file, replacing the one DNA ships for this connector (use for a tracker DNA has no profile for). |
| `profile` |  | _empty_ |  | Studio overrides deep-merged over the connector profile: entity and field mappings, labels, capabilities, URLs. |

## `llm`

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `provider` | `LLM_PROVIDER` | `openai` |  | Default profile. |
| `profiles` |  | _empty_ |  |  |

Also read from the environment (per-profile aliases, mapped onto `profiles.<name>.*`): `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_TIMEOUT`, `ANTHROPIC_URL`, `CUSTOM_LLM_API_KEY`, `CUSTOM_LLM_MODEL`, `CUSTOM_LLM_TIMEOUT`, `CUSTOM_LLM_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_TIMEOUT`, `GEMINI_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_TIMEOUT`, `OPENAI_URL`.

## `transcription`

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `provider` | `TRANSCRIPTION_PROVIDER` | `vexa` | restart |  |
| `vexa.api_url` | `VEXA_API_URL` | `https://api.cloud.vexa.ai` |  |  |
| `vexa.api_key` | `VEXA_API_KEY` | _unset_ | secret |  |

## `features`

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `transcript_publish` | `DNA_ENABLE_TRANSCRIPT_PUBLISH` | `false` | project | Publish meeting transcripts to the production tracker. |
| `testing_enabled` | `DNA_TESTING_ENABLED` | `false` | restart | Enables dev-only test endpoints.  Never enable in production. |

## `frontend`

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `feature_transcription` | `DNA_FEATURE_TRANSCRIPTION` | _unset_ |  | Force transcription on/off for everyone. |
| `feature_in_review` | `DNA_FEATURE_IN_REVIEW` | _unset_ |  | Force the in-review view on/off for everyone. |
| `feature_ai` | `DNA_FEATURE_AI` | _unset_ |  | Force AI note generation on/off for everyone. |
| `ws_url` | `DNA_WS_URL` | _unset_ |  | WebSocket URL for live events, if not derivable from the page. |
| `prodtrack_tab_sync_extension_id` | `DNA_PRODTRACK_TAB_SYNC_EXTENSION_ID` | _unset_ |  | Browser extension id that keeps a tracker tab in sync with the selected version. |
| `dev_mode` | `DNA_DEV_MODE` | `false` |  | Show developer-only UI. |

## `content`

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `default_note_prompt_path` | `DNA_DEFAULT_NOTE_PROMPT_PATH` | _unset_ |  |  |
| `glossary_global_path` | `DNA_GLOSSARY_GLOBAL_PATH` | _unset_ |  |  |

## `admin` _(locked: files and environment only)_

| Setting | Environment variable | Default | Flags | Notes |
|---|---|---|---|---|
| `enabled` | `DNA_CONFIG_ADMIN_ENABLED` | `false` | restart | Serve the /config admin API.  Off by default. |
| `admins` | `DNA_CONFIG_ADMINS` | _empty_ | restart | Who may change configuration (case-insensitive): tracker logins for users who sign in through a tracker (ShotGrid, AYON), emails for Google sign-in. |
| `persist` | `DNA_CONFIG_PERSIST` | `false` | restart | Keep runtime changes in MongoDB (shared by every instance, with history and rollback).  Off: changes live in this process only. |
| `allow_unauthenticated` | `DNA_CONFIG_ALLOW_UNAUTHENTICATED` | `false` | restart | Development only: let an unauthenticated caller administer config when AUTH_PROVIDER=none.  Refused when authentication is on. |
| `allowed_hosts` | `DNA_CONFIG_ALLOWED_HOSTS` | _empty_ | restart | Host names an administrator may point a tracker, LLM or transcription URL at, beyond the hosts already in the config files and environment.  Without this a runtime change can only keep using the configured hosts, so API keys cannot be redirected. |
| `refresh_seconds` | `DNA_CONFIG_REFRESH_SECONDS` | `5.0` |  | How often this instance checks the store for newer changes. |
