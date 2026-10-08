# Configuring DNA

DNA reads its settings from one place, whichever production tracker you use (ShotGrid,
AYON, or one added later). A studio configures it with files and environment variables,
and an administrator can adjust the parts that are safe to change while it runs.

Every setting is listed, with its default and environment variable, in the generated
[Configuration reference](CONFIGURATION_REFERENCE.md). This page explains how the
pieces fit together.

## Where settings come from

Settings are merged from these layers. A later layer wins over an earlier one:

| # | Layer | Typical use |
|---|---|---|
| 1 | Built-in defaults | what you get with nothing configured |
| 2 | YAML files in `DNA_CONFIG_DIR` | the studio's configuration, kept in version control |
| 3 | Environment variables | secrets and per-deployment values (`SHOTGRID_URL`, `AYON_API_KEY` ...) |
| 4 | Runtime overrides | changes made through the admin page, API or CLI |

Files in `DNA_CONFIG_DIR` load in name order, so `00-base.yaml` followed by
`10-site.yaml` behaves as you would expect. A top-level key is a section name:

```yaml
prodtrack:
  connector: ayon
  ayon:
    url: https://ayon.studio.com
    api_key: ${env:AYON_API_KEY}
```

Existing deployments keep working unchanged: every environment variable DNA has ever
read (`SHOTGRID_URL`, `MONGODB_URL`, `AUTH_PROVIDER` ...) is still honoured. Any
setting can also be given as `DNA__<SECTION>__<PATH>`, for example
`DNA__SERVER__LOG_LEVEL=DEBUG`.

A typo in a file is an error at startup (`unknown setting`), not a silently ignored
key. Check a configuration without starting the server:

```bash
cd backend/src
python -m dna.config_cli validate
python -m dna.config_cli show        # effective settings, secrets masked
```

Copy [`config.example/`](../config.example/) to start.

## Secrets

A secret is never written into a file or stored in the database. The setting holds a
**reference** that is resolved when the configuration loads:

| Reference | Meaning |
|---|---|
| `${env:NAME}` | the value of environment variable `NAME` |
| `${env:NAME:-fallback}` | the same, or `fallback` if it is unset |
| `${file:/run/secrets/key}` | the contents of a file (for Docker and Kubernetes secrets) |

Secret settings (API keys, the JWT key, the MongoDB URL) are masked in every API
response and log line. A change made at runtime must be a reference; a plain value is
refused. A plain secret written in a config file or given as a classic environment
variable (`AYON_API_KEY=...`) still works, but the file case logs a warning
(`dna.config_cli validate` shows it) because the file will sit in version control.

References that arrive **at runtime** (through the admin page, API or CLI) are held to
a stricter rule, so an administrator cannot use them to read other things from the
server:

* they are accepted only on settings marked secret;
* `${env:NAME}` is accepted only for a variable that a secret setting is documented to
  read (`AYON_API_KEY`, `SHOTGRID_API_KEY`, `OPENAI_API_KEY` ...) or one that the
  operator already referenced in the config files;
* `${file:...}` cannot be set at runtime. Put it in a config file.

**Endpoints.** A URL that DNA sends credentials to (the ShotGrid and AYON addresses, an
LLM profile's `url`, the Vexa `api_url`) can be changed at runtime only to a host the
operator already trusts: one in the config files or environment, the built-in default,
or one listed in `admin.allowed_hosts` (`DNA_CONFIG_ALLOWED_HOSTS`, comma separated;
set it in the environment or a file, it cannot be changed at runtime). Without this,
an administrator could point a real API key at a server of their choosing. To move DNA
to a new tracker host, set it in the files or environment (or allow the host), then
test it on the *Tracker setup* tab.

## Changing settings while DNA runs

Turn the admin features on with environment variables (they are read at startup):

| Variable | Meaning |
|---|---|
| `DNA_CONFIG_ADMIN_ENABLED=true` | serve the admin API and page |
| `DNA_CONFIG_ADMINS=alice,bob` | who may administer (see below) |
| `DNA_CONFIG_PERSIST=true` | keep changes in MongoDB so every instance sees them, with history and rollback. Off: changes last only until restart and affect one process |

**Who is an administrator.** Users who sign in through a tracker (ShotGrid, AYON) are
matched by their tracker *login*, not their email: the email on a tracker account is a
profile field the user may be able to edit. Users who sign in with Google are matched
by their verified email. A deployment with `AUTH_PROVIDER=none` cannot be administered
unless `DNA_CONFIG_ALLOW_UNAUTHENTICATED=true`, which is for development only.

**The admin page** is at `/?admin=config`. It has three tabs:

* **Tracker setup**: choose the tracker, enter its address and a reference to its API
  key, and press *Test connection*. DNA connects for real, checks that the tracker's
  entities and fields match, and reads sample data. *Save* is available only after a
  passing test. A failure explains what to fix, and often offers a one-click correction.
* **Settings**: every effective value, and which layer set it.
* **History**: every saved change with who made it, and *Roll back to this*. A rollback
  is itself saved as a new revision, so nothing is lost.

**The command line** does the same without a browser:

```bash
python -m dna.config_cli handshake --smoke-email you@studio.com
python -m dna.config_cli handshake --candidate try.yaml          # test a change first
python -m dna.config_cli handshake --candidate try.yaml --apply  # save it if it passes
python -m dna.config_cli history
python -m dna.config_cli export -o overrides.yaml
python -m dna.config_cli import overrides.yaml
python -m dna.config_cli rollback 3
```

**The audit trail.** Every revision records who, when, the note, and for each setting
**what it was and what it became** (secrets appear only as the reference). *History*
shows them. Each handshake is logged with who ran it and which host it targeted, saved
or not.

**What cannot be changed at runtime.** The `server`, `auth`, `storage` and `admin`
sections are read at startup and are locked: they decide how the process starts and who
is trusted, so changing them needs a file or environment change and a restart. Some
other settings are flagged *restart* in the reference: a change is saved, and the admin
page tells you a restart is needed to apply it.

## When things change underneath a running DNA

* **Files and mounted secrets are re-read.** Editing a YAML file in `DNA_CONFIG_DIR`,
  adding one, or rotating a file a `${file:...}` reference reads, takes effect within
  a few seconds. No restart is needed. A **bad edit is refused**: DNA keeps the
  configuration it has and reports the problem (below), and the next good edit is
  applied. An invalid configuration at *startup* is fatal and says exactly why.
* **The shared store is read in the background.** With `admin.persist`, a background
  thread (never a request) polls MongoDB every `admin.refresh_seconds` and adopts
  newer revisions. If MongoDB is unreachable DNA keeps the configuration it has, backs
  off (up to a minute between tries), and recovers by itself. A save while it is down
  is refused with a `503` and changes nothing.
* **A stored revision that stops validating** (after an upgrade renamed a setting, for
  example) no longer discards the administrators' work. DNA uses the newest revision
  that still validates and reports which one it is running. Saving a corrected
  revision, or rolling back, clears the problem.
* **Where it shows.** `GET /health` includes `config`; it is `false` (and the status
  `degraded`) in any of these situations. The admin page shows a red banner with the
  reasons, and the *effective* view carries a `status` block.
* **Upgrading the settings themselves.** Each stored revision records the schema it was
  written under. When a release renames or reshapes a setting it adds a migration
  (`dna/configsys/migrations.py`), and old revisions are upgraded as they are read. A
  revision written by a *newer* DNA than the one reading it is refused, not guessed at.
* **An override can hide a file or environment value.** A setting saved on the admin
  page outranks the files and environment, so editing those has no effect until the
  override is removed. DNA warns about each such case (admin page banner, `validate`).

## Different settings for one project

A few settings can be overridden per project (marked *project* in the
[reference](CONFIGURATION_REFERENCE.md)), for example publishing transcripts for one show
without turning it on everywhere. A project without an override uses the site-wide value.
Only settings that apply to the tracker in use are offered.

**On the admin page**, open the *Projects* tab. Enter the project's id as your tracker
knows it (the field suggests your own projects), choose a setting, give the value, and
save. The table below lists every override with the site-wide value beside it and
where it was set; *Remove* returns that project to the site-wide value. Each change is
recorded in *History* with your note and can be rolled back.

**In a config file**, under `projects`:

```yaml
features:
  transcript_publish: false
projects:
  "dna_test":                 # the project's id in your tracker
    features:
      transcript_publish: true
```

An override set in a file is shown on the *Projects* tab but can only be changed in the
file; an override set on the page takes effect over the file's.

**Over the API** (administrators only):

| Request | Does |
|---|---|
| `GET /config/projects` | the overridable settings (with site-wide values) and the overrides now set |
| `PUT /config/projects/{project_id}` | set overrides: `{"values": {"features": {"transcript_publish": true}}, "note": "..."}` |
| `DELETE /config/projects/{project_id}?section=features&path=transcript_publish` | remove one override; omit `path`/`section` to remove more |
| `GET /config/public?project_id={project_id}` | the settings as they apply to that project |

An override of any setting that is not marked *project*, of a locked section, or with
the wrong type is rejected with the reason, and nothing is saved.

## Security and reliability notes

* Tracker URLs must be plain `http://` or `https://` and may not contain a user name
  or password.
* Every change is validated as a whole before it is applied; an invalid change is
  refused and nothing is saved.
* If the shared override store is unreachable, DNA keeps running on the files and
  environment it already has and logs the problem.
* Changes made through the admin API are applied to every instance within
  `admin.refresh_seconds` (default 5).
