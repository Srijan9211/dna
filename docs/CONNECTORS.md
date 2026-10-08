# Production tracker connectors

DNA is a note-taking application. It does not care what your tracker calls things; it
cares about a few **roles**, and a *connector* maps a tracker's own entities onto them.
ShotGrid, AYON and a local mock ship with DNA. This page explains how a connector is
described, how a studio adapts one, and how to write a new one (ftrack, Kitsu ...).

## The roles

| Role | What it is | ShotGrid | AYON |
|---|---|---|---|
| project | the show | Project | Project |
| subject | what is being reviewed (shots and assets) | Shot, Asset | folders of type Shot / Asset |
| task | work on a subject | Task | Task |
| reviewable | one submission to review | Version | Version |
| collection | a reviewable set, the thing you open | Playlist | List (entity list) |
| person | a user | HumanUser | User |
| note | what DNA writes | Note | Comment |

IDs are opaque to DNA: an integer from ShotGrid, a string like `dna_test:<uuid>` from
AYON. Nothing outside a connector may assume which.

## A connector profile

Each connector ships a YAML **profile** (`backend/src/dna/prodtrack_providers/profiles/`)
that declares everything that is data rather than logic: the display name, the labels
the UI shows ("Playlist" or "List", "Note" or "Comment"), what the tracker can do
(`capabilities`), which tracker entity and fields feed each role (`entities`), deep
links (`urls`), and a connector-specific `adapter` block. Read
[`ayon.yaml`](../backend/src/dna/prodtrack_providers/profiles/ayon.yaml) for a worked,
commented example.

The UI reads the labels from the server, so it adapts without a rebuild: with AYON it
says "Select a list" and "Comment" where ShotGrid says "Select a playlist" and
"Note". The capabilities describe what the tracker does with DNA's data; for example
AYON comments have no To/CC, so the people entered there are turned into `@mentions`
in the comment (`note_addressing: false`).

### Adapting a profile to your studio

A studio never edits the shipped profile. It overrides only what differs, in its own
configuration (`prodtrack.profile`), and the override is merged over the shipped one:

```yaml
prodtrack:
  connector: ayon
  profile:
    labels:
      playlist: Review list
    entities:
      shot:  {where: {folderType: [Shot, Sequence]}}
      asset: {where: {folderType: [Asset, Character, Prop]}}
      version:
        fields: {attrib.vendorNotes: description}   # read a custom attribute
```

Setting a key to `null` removes it from the shipped profile. A profile that does not
validate is rejected when the configuration loads, with the exact path of the problem.
To replace a profile outright (a tracker DNA has no profile for), point
`prodtrack.profile_path` at your own file.

## Checking a connection: the handshake

Setting up a tracker is the part most likely to go wrong, so DNA can *test* a
configuration against the real tracker before it is saved:

1. **connect**: can DNA reach it and authenticate?
2. **discover**: read the tracker's actual schema (entity types, fields).
3. **validate**: does every field in the profile exist? Is each role bound to
   something real?
4. **suggest**: for each problem, list the closest real names and an override that
   would fix it (`attrib.descripton` becomes `attrib.description`).
5. **smoke**: optionally read as a real user: projects, playlists, versions.

The result is a report of pass / warn / fail checks, nothing is changed, and a
failing result cannot be saved. It runs from the admin page (*Tracker setup*) or the
command line:

```bash
python -m dna.config_cli handshake --smoke-email you@studio.com
python -m dna.config_cli handshake --candidate try.yaml
```

## Writing a new connector

A connector is a Python class plus a profile. Nothing in DNA's core changes.

1. **Subclass** `ProdtrackProviderBase`
   (`backend/src/dna/prodtrack_providers/prodtrack_provider_base.py`) and implement the
   contract: read entities, list a user's projects, playlists and versions, create a
   playlist, add a version to one, publish a note, update a version's status, attach
   files. The base class documents each method. Set the class flags that describe
   you: `SUPPORTS_IMPERSONATION` (act as the signed-in user) and
   `MEDIA_REQUIRES_PROXY` (the tracker's media needs credentials a browser cannot send,
   so DNA serves it through a signed link).
2. **Write the profile** (`profiles/<name>.yaml`), copying an existing one.
3. **Implement the handshake hooks** (`for_handshake`, `check_connection`,
   `discover_schema`, `adapter_issues`) so *Test connection* works for your tracker.
4. **Register it.** For a connector kept outside the DNA repository, publish a package
   with an entry point and install it next to DNA:

   ```toml
   [project.entry-points."dna.prodtrack"]
   ftrack = "my_studio.dna_ftrack:FtrackProvider"
   ```

   A built-in connector is added to the `prodtrack` registry in
   `backend/src/dna/configsys/providers.py`.
5. **Add a login** if users should sign in with the tracker: an auth provider in the
   `dna.auth` group, like `auth_providers/ayon_login.py`. It checks the password once
   and keeps no credential.
6. **Pass the conformance suite.** `backend/tests/test_prodtrack_conformance.py` runs
   the same scenarios against every connector. A connector is done when it joins the
   `CONNECTORS` list there and the suite passes. Also add a fake-tracker test file like
   `tests/test_ayon_provider.py` so the connector is tested without a live server.

### Things every connector must get right

* **Act as the user.** Notes and reads should carry the signed-in user's identity and
  permissions, never a shared service account's. If the tracker cannot impersonate,
  say so in `capabilities` so the UI can tell users their notes are posted as DNA.
* **Fail with the standard errors** (`ProdtrackAuthError`, `ProdtrackPermissionError`,
  `ProdtrackUnavailableError`, `ValueError` for not-found) so the API returns the right
  status without knowing which tracker it talks to.
* **Never pass a user-supplied string into a query unescaped**, and keep reads
  paginated. A studio with thousands of users or tasks must not be silently truncated.
* **Retry reads, never writes.** A write that timed out may have succeeded.
* **Cache lookups only per acting user.** One person's access must not answer for
  another's.
