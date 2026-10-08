# Running DNA in production

A checklist and reference for the people who deploy and look after DNA. For the
settings themselves see [CONFIGURATION.md](CONFIGURATION.md); for tracker setup see
[CONNECTORS.md](CONNECTORS.md).

## Before you go live

1. **Turn authentication on.** `AUTH_PROVIDER=ayon` or `shotgrid` (tracker login) or
   `google`. `none` is for development: anyone can use the API as anyone.
2. **Set a JWT secret**: a long random value, as a reference
   (`jwt.secret_key: ${env:JWT_SECRET_KEY}`). It also signs media links. Without one,
   DNA warns at startup that media links will break after a restart and across
   worker processes.
3. **Use a dedicated tracker service account.** DNA reaches the tracker with one
   service key and acts as each signed-in user (`x-as-user` for AYON, `sudo_as_login`
   for ShotGrid), so the tracker enforces each person's own permissions. The service
   account needs the right to impersonate, and nothing more. Rotate its key if it was
   ever shown anywhere.
4. **Lock down who can administer**: list tracker logins in `DNA_CONFIG_ADMINS`, turn on
   `DNA_CONFIG_PERSIST`, and leave `DNA_CONFIG_ALLOW_UNAUTHENTICATED` unset.
5. **Set CORS** to your frontend's origin (`server.cors_allowed_origins`). Never `*`.
6. **Serve over HTTPS.** Refresh cookies are `Secure` and `SameSite=Strict` by default;
   keep it that way unless the frontend is on another site.
7. **Run `python -m dna.config_cli validate`** with the production environment, then
   `python -m dna.config_cli handshake --smoke-email <a real user>`. Do not go live on
   a failing handshake.
8. **Keep secrets out of files.** `validate` warns about any plain secret in a config
   file.
9. **Back up MongoDB.** It holds draft notes, transcript segments, user settings, the
   session store and the configuration history.

## Health and monitoring

`GET /health` answers 200 `healthy` or 503 `degraded`, with one entry per dependency:

| Entry | Meaning |
|---|---|
| `mongo` | the session store answers (only reported when authentication is on) |
| `shotgrid` | the ShotGrid script account authenticated at startup |
| `ayon` | the AYON service key authenticated at startup |
| `config` | DNA is running on the configuration that was intended. `false` after a refused bad edit, an unreachable shared store, or an unusable stored revision (reasons on the admin page) |

A tracker failure is checked once at startup and reported thereafter, so a probe never
loads the tracker. A `degraded` instance should be taken out of rotation until the
credentials are fixed. The container image has a built-in health check and runs as an
unprivileged user.

Access logs show signed media tokens as `token=***`. Configuration is always printed
with secrets masked.

## What protects what

| Risk | Control |
|---|---|
| Password guessing | per-address-and-user and per-user throttling, `429` with `Retry-After` (`auth.login_throttle`) |
| An administrator redirecting an API key to their own server | runtime changes may only use hosts already trusted (config files, environment, `admin.allowed_hosts`) |
| A bad config edit or a bad stored revision taking DNA down | a bad edit is refused and the previous config kept; an unusable revision falls back to the last valid one; both reported on `/health` |
| The shared config store going down | refresh happens in the background with backoff, so requests never wait on it |
| A profile name altering a tracker query | field and entity names must be plain dotted identifiers |
| Someone reading server secrets through the admin page | references accepted only on secret settings, for sanctioned variables; no `${file:}` at runtime |
| A tracker user making themselves an admin | admins matched by tracker login, not the editable profile email |
| Copied media links | signed, expire after 15 minutes, bound to one version, one kind and one user |
| One user's cached data reaching another | every cache is keyed by the acting user |
| Tracker outage during a write | writes are never retried; reads retry twice on transient errors |
| A bad configuration change | validated whole before applying; kept in history; roll back with one click |
| Blocked server during a slow tracker call | request handlers run in a thread pool, not on the event loop |

The login throttle counts in memory, per process. With several instances the effective
limit is the configured one times the number of instances; add a rate limit at the
reverse proxy for a hard guarantee.

## Capacity

`backend/scripts/loadtest.py` simulates reviewers opening a playlist and flicking
through its versions (reading, searching mentions, loading thumbnails). It is
read-only.

```bash
python backend/scripts/loadtest.py --base-url http://localhost:8000 \
    --email you@studio.com --users 20 --seconds 30 \
    --p95-budget-ms 1500 --max-error-rate 0.01
```

It exits non-zero when a budget is exceeded, so it can gate a release. If
authentication is on, pass `--token`.

Measured against AYON on a single developer laptop that was also running AYON,
Postgres, Redis, MongoDB and the load generator (so these are conservative):

| Concurrent reviewers | Median latency | Worst p95 | Errors |
|---|---|---|---|
| 10 | 30-180 ms | about 850 ms | 0% |
| 30 | 170-1500 ms | about 2.4 s | 0% |

At 30 the AYON server itself was the limit: it answers a single query in about 55 ms
but about 240 ms (median) with 30 callers at once. Reducing the number of tracker
calls per request is where further gains are. Treat these as a baseline and re-measure
on your own hardware.

## Known limits

* **AYON deep links** open the project's pages, not the specific entity; the per-entity
  link format is not confirmed against a real server.
* **`@mentions` in AYON comments** are written as `[@login](user:login)`; whether AYON
  turns them into notifications is unconfirmed (they read as plain text either way).
* **AYON password sign-in** is covered by automated tests against a faithful fake of
  AYON's login API; a real password check has not been exercised, so sign in once in
  your environment before relying on it.
* **ShotGrid handshake and the shared override store** are tested against fakes, not a
  live ShotGrid site or MongoDB cluster.
* The frontend's **API address is still fixed when the image is built**
  (`VITE_API_BASE_URL`); everything else (login mode, features, labels) comes from the
  server at runtime.
* The frontend has **pre-existing** test and type-check failures unrelated to the
  tracker work (14 app tests, 18 type errors in the QC and Radix areas). They did not
  change with this work.
