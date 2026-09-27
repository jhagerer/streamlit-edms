# MiniDMS — a small document management system on Snowflake

A deliberately small clone of [Mayan EDMS](https://www.mayan-edms.com/), built with
Streamlit. It runs **in Streamlit in Snowflake** (container runtime) and **locally**
on your laptop. Both versions keep all data in Snowflake.

Features: upload, immutable files with SHA-256 checksums, OCR / text extraction with
`AI_PARSE_DOCUMENT`, full-text search, document types, typed metadata, tags, trash,
per-document history and an audit trail.

Design: [`docs/09-streamlit-architecture.md`](docs/09-streamlit-architecture.md) ·
Plan: [`docs/10-streamlit-implementation-plan.md`](docs/10-streamlit-implementation-plan.md) ·
Schema: [`sql/minidms-setup.sql`](sql/minidms-setup.sql)

## The four decisions

1. **All reads are bulk.** Each table is loaded whole into a polars frame. The frame is cached
   and keyed on the table's change token (`SYSTEM$LAST_CHANGE_COMMIT_TIME`).
2. **All writes are appends.** No `UPDATE`, no `DELETE`. The current state is the latest
   row per key.
3. **Session state is durable.** Your unsaved changes can be saved as parquet files in
   `@sessions/<you>/`. They come back automatically the next time you open the app.
4. **Writing has two stages.** *Save to session* writes to the stage; *Save to database*
   inserts into the log tables.

## Project layout

```
streamlit_app.py        entry point (st.navigation, one token probe per rerun, restore on entry)
app_pages/              Streamlit pages
  _state.py             tier-1 appends store — the only module touching st.session_state
  _ui.py                sidebar, dialogs, document list, metadata widgets
  documents.py upload.py search.py trash.py document.py
  document_types.py metadata_types.py tags.py audit.py admin.py diagnostics.py
  setup.py              runs the setup SQL step by step from inside the app
core/                   plain Python (no Streamlit, except core/session.py)
  session.py            Snowpark session (SiS or local), viewer identity, cache decorator
  schema.py             table columns, dtypes, reduction keys, READ_/APPEND_TABLES
  read.py               change tokens, cached loaders, latest(), view(), restore()
  snapshot.py           parquet snapshots on @sessions
  write.py              row builders, validation, save_to_session(), flush()
  files.py              @doc_files upload, AI_PARSE_DOCUMENT, orphan housekeeping
  setup.py              setup script parsing, grants, schema status checks
  search.py             SEARCH() / ILIKE over document_text
  model.py              current state: documents, tags, metadata, filters
sql/                    setup DDL, grants and deployment
tests/                  pytest; runs without Snowflake (fake session)
```

The pages folder is called `app_pages/` (not `pages/`), so Streamlit's automatic
multipage detection does not interfere with `st.navigation`.

## 1. Set up the schema (once)

Two ways — both run the same idempotent statements (nothing is ever dropped):

**A. From the app: System → Setup.** The page works before any MiniDMS object exists
(when the schema is missing, every other page links to it). It walks through:

1. **Connection** — account, user, role, warehouse, database, schema. On a local run you
   can switch (or create) database and schema here.
2. **Stages** — `@doc_files`, `@sessions`
3. **Tables** — the eight event logs
4. **Audit view** — `audit_v`
5. **Grants** (optional) — `SELECT, INSERT` (no `UPDATE`/`DELETE`) for an app role
6. **PyPI integration** (optional, SiS container runtime; usually ACCOUNTADMIN)
7. **`CREATE STREAMLIT`** (shown for copying only)
8. **Starter definitions** (optional) — a few document types, metadata types and tags,
   added as pending changes you then *Save to database*

Every statement is shown before it runs; each has its own *Run* button, and every step
has *Run all*. A progress bar shows how many of the 11 objects exist; a log shows
each result.

Who may run it: on a local run, you. In Streamlit in Snowflake, only the app owner. On a
shared deployment with login (e.g. Community Cloud), only the users listed in
`MINIDMS_SETUP_USERS`. Everyone else sees the page read-only.

**B. In a worksheet.** Run [`sql/minidms-setup.sql`](sql/minidms-setup.sql), then adapt
[`sql/minidms-grants-and-deploy.sql`](sql/minidms-grants-and-deploy.sql).

Note: a role that *owns* the tables can always delete from them. To have the database
enforce append-only, let a different role own the tables and grant the app role only
`SELECT, INSERT`.

## 2a. Run in Streamlit in Snowflake

1. Put the files of this repository into a **Snowflake Workspace** (for example connect
   the Git repository) and create a Streamlit app from it with
   `streamlit_app.py` as the main file. Or use the `CREATE STREAMLIT` statement in
   `sql/minidms-grants-and-deploy.sql`.
2. Use the **container runtime** (`SYSTEM$ST_CONTAINER_RUNTIME_PY3_11`) with a compute
   pool, and attach the PyPI external access integration. The app installs
   `requirements.txt` from PyPI; without the integration it will not start.
3. Create the app **in the same schema** as the MiniDMS tables. The app uses
   unqualified names (`document_log`, `@doc_files`, …).
4. Check: `DESC STREAMLIT <name>` shows the container runtime and a compute pool.
   Open **Diagnostics** as two different users — the *Viewer (actor)* must differ.

In SiS the viewer comes from `st.user.user_name`. `CURRENT_USER()` would return the
app owner, so it is never used for attribution there.

## 2b. Run locally

The local app talks to the same Snowflake tables and stages.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then edit it
streamlit run streamlit_app.py
```

The connection is created with `st.connection("snowflake")`. It reads
`[connections.snowflake]` from `.streamlit/secrets.toml`, or else your default
connection in `~/.snowflake/connections.toml`. The connection must set a
**database and schema** where the MiniDMS objects live.

Optional environment variables:

| Variable | Purpose |
|---|---|
| `MINIDMS_CONNECTION` | Name of the Streamlit connection to use (default `snowflake`) |
| `MINIDMS_DATABASE`, `MINIDMS_SCHEMA`, `MINIDMS_WAREHOUSE` | Override the connection's defaults |
| `MINIDMS_USER` | Actor name to record (default: `CURRENT_USER()` of your connection) |
| `MINIDMS_PARSE_MODE` | `OCR` (default) or `LAYOUT` for `AI_PARSE_DOCUMENT` |
| `MINIDMS_RUNTIME` | Force `local` or `sis` if auto-detection gets it wrong |
| `MINIDMS_SETUP_USERS` | Comma-separated users allowed to run the Setup page (env or secrets) |

Locally, the person at the screen is the person whose credentials are used, so
`CURRENT_USER()` is the right actor.

## 2c. Run on Streamlit Community Cloud

The same code runs on [Streamlit Community Cloud](https://share.streamlit.io). It
connects to Snowflake over the internet like the local version. Two things differ from a
local run:

- **No browser SSO.** The app needs a Snowflake user that can log in without a person:
  a *service user with key-pair authentication*.
- **One Snowflake user for everybody.** So `CURRENT_USER()` cannot tell viewers apart.
  Turn on Streamlit's login (`[auth]` in the secrets). The app then records the viewer's
  login e-mail as the actor and gives each viewer their own `@sessions` folder. When
  `[auth]` is configured, the app asks for a login before it touches Snowflake.

Steps:

1. **Service user** (in Snowflake, as an admin). Create a key pair:
   ```bash
   openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
   openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
   ```
   ```sql
   CREATE ROLE IF NOT EXISTS MINIDMS_APP;
   CREATE USER IF NOT EXISTS MINIDMS_SVC TYPE = SERVICE
     DEFAULT_ROLE = MINIDMS_APP DEFAULT_WAREHOUSE = <wh>
     RSA_PUBLIC_KEY = '<contents of rsa_key.pub without the BEGIN/END lines>';
   GRANT ROLE MINIDMS_APP TO USER MINIDMS_SVC;
   ```
   Then create the schema objects (worksheet, or the Setup page from a local run) and
   grant `MINIDMS_APP` its privileges (Setup step 5). If your account uses a network
   policy, it must allow connections from Community Cloud.
2. **Login provider.** Create an OpenID Connect client, e.g. in the Google Cloud Console
   (OAuth client ID, type *Web application*), with the redirect URI
   `https://<your-app>.streamlit.app/oauth2callback`.
3. **Deploy.** On [share.streamlit.io](https://share.streamlit.io) → *Create app* → pick
   the GitHub repository and branch, main file `streamlit_app.py`. Under *Advanced
   settings* choose Python 3.11 and paste the secrets — template in
   [`.streamlit/secrets.community-cloud.toml.example`](.streamlit/secrets.community-cloud.toml.example).
   The private key can be pasted as PEM; the app converts it.
4. **Check.** Log in, open **Diagnostics**: *Viewer (actor)* must be your e-mail, and
   `CURRENT_USER()` the service user. Open **Setup**: the progress bar must be full.

Notes: queries, Cortex text extraction and storage are billed to your Snowflake
account. The cache is shared by all viewers of the app. Community Cloud gives an app
limited memory, which caps how large the logs can grow before you need the reduced
view (§9.10).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests need no Snowflake account. They cover the reduction (`latest()` with its
tie-break), `view()`/`compose()`, `clean_restored()`, column validation, the parquet
round trip, save-to-session → restore → flush, the interrupted flush that heals
itself, the layering rules, the setup script parsing and grants, the PEM key
conversion, the login gate, and smoke tests that render every page (including running
the setup against an empty schema) with Streamlit's `AppTest` and an in-memory fake
session.

## Using it

- **Upload** — choose files, a document type, tags and metadata. Each file becomes one
  document. *Files upload immediately; entries are saved when you click
  **Save to database**.*
- Text is extracted during upload with `AI_PARSE_DOCUMENT` (PDF, DOCX, PPTX, images,
  HTML, TXT). Plain-text formats (`.txt`, `.md`, `.csv`, …) are read directly. If
  extraction fails, the document is not created — unless you tick *Keep documents whose
  text extraction fails*; then you can retry on the document's **Text** tab.
- **Documents / Search / Trash** — filter in memory by type, tags and text. **Search**
  asks Snowflake for full-text matches (`SEARCH()` or substring).
- **Document** — properties, metadata, tags, files (download, add, remove), extracted
  text, and the full history of the document including unsaved events.
- The **sidebar** on every page shows your pending changes and the three buttons
  *Save to session*, *Save to database*, *Discard changes*.
- **Admin** — your pending rows, everyone's session folders (with cleanup), raw vs.
  current row counts and memory, orphaned-file scan and cleanup, credit usage.
- **Diagnostics** — actor, `st.user`, tokens, per-table counts and load times, probe
  latency, and what is in session state.

## FAQ

**Why can't I delete anything?**
Every change is an appended event. That makes the history complete, and the audit
trail comes for free. "Delete" means *trash* (documents), *deactivate* (types, tags),
*remove* (files, marked inactive) or an empty value (metadata). Trashed documents can
be restored.

**Where did my unsaved changes go?**
Changes you have not saved live only in your browser session. After
*Save to session* they are in `@sessions/<you>/` and come back automatically when you
open the app again (a dialog tells you). After *Save to database* they are in the
tables for everyone.

## Known limits (by design)

- **Last writer wins on the whole record.** Each edit appends a full snapshot. If two
  people edit different fields of the same document, the second save overwrites the
  first.
- **Several tabs, one user**: all tabs write the same `@sessions/<you>/` folder; the
  later *Save to session* wins.
- Unsaved work is only safe after *Save to session*.
- Uploaded bytes stay in `@doc_files` even if you discard — clean them up on **Admin**.
- Text that is not yet saved to the database is not searchable yet.
- No page viewer — use the extracted text and the download button.
- The schema has no link between document types and metadata types, so all active
  metadata types are offered for every document.
- Logs grow without bound. When the in-memory size (Admin → Log sizes) reaches a few
  hundred MB, switch the loaders to a reduced server-side view (architecture §9.10).

## Pre-flight checks (from the plan) — to do in your account

These could not be verified without a Snowflake account. Please check them once:

1. `create_dataframe` + `save_as_table` round-trip keeps `TIMESTAMP_NTZ` (the app passes
   an explicit Snowpark schema, so this should hold).
2. `SYSTEM$LAST_CHANGE_COMMIT_TIME` works on an empty table (the app falls back to
   `COUNT(*)`/`MAX(event_ts)` if it does not).
3. `REMOVE @sessions/<user>/` only removes that folder.
4. `pl.read_parquet` reads what `session.file.get_stream()` returns.
5. `AI_PARSE_DOCUMENT(TO_FILE('@doc_files', ?), {'mode': 'OCR'})` works with a bound path.
6. `@st.cache_data` is shared across viewers on the compute pool (check `QUERY_HISTORY`
   after opening the app as two users).
