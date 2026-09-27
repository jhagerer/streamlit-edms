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
  _state.py             tier-1 appends store — the only page module touching st.session_state
  _ui.py                sidebar, dialogs, document list, metadata widgets
  documents.py upload.py search.py trash.py document.py
  document_types.py metadata_types.py tags.py audit.py admin.py diagnostics.py
  setup.py              runs the setup SQL step by step from inside the app
  _connection.py        Setup step 1: enter/change Snowflake credentials, key pairs
core/                   plain Python (no Streamlit, except core/session.py)
  session.py            Snowpark session (SiS, own credentials, or secrets), viewer
                        identity, cache decorator; keeps a viewer's own connection
                        (never data) in st.session_state
  schema.py             table columns, dtypes, reduction keys, READ_/APPEND_TABLES
  read.py               change tokens, cached loaders, latest(), view(), restore()
  snapshot.py           parquet snapshots on @sessions
  write.py              row builders, validation, save_to_session(), flush()
  files.py              @doc_files upload ({checksum}/{filename}), orphan housekeeping
  ocr.py                extraction task state, recent runs, run on demand
  setup.py              setup script parsing, grants, schema status checks
  connection.py         connection settings → connector params, secrets.toml, key pairs
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

1. **Connection** — shows account, user, role, warehouse, database, schema and where
   the connection comes from. Outside Streamlit in Snowflake you can also:
   - **enter or change credentials**: account, user, role, warehouse, database, schema,
     and one of *key pair* (upload/paste a `.p8`, optional passphrase), *programmatic
     access token*, *password + MFA passcode*, or *browser SSO* (own computer only).
     *Test connection* checks them; *Connect for this browser session* uses them for you
     only — kept in server memory, never written to disk, gone when the tab closes.
     Leave a secret field empty to keep the one you entered before;
   - **generate a key pair**: download the private key, get the
     `ALTER USER … SET RSA_PUBLIC_KEY` statement, optionally run it right away;
   - **save the connection permanently**: the page renders the `secrets.toml` section to
     paste into Community Cloud (*Settings → Secrets*) or save locally;
   - **switch database/schema/warehouse**, optionally creating them;
   - **disconnect** or **reconnect** the shared connection after changing secrets.

   In Streamlit in Snowflake there is nothing to enter — Snowflake provides the session.
   While you have unsaved changes the connection cannot be changed (they belong to the
   current account and schema).
2. **Stages** — `@doc_files`, `@sessions`
3. **Tables** — the seven event logs the app writes, plus `document_text` and `ocr_log`
   (written only by the extraction task)
4. **Views** — `audit_v`, `ocr_status_v`
5. **Text extraction** — stream `document_file_log_stream`, procedure `extract_text()`,
   triggered task `extract_text_task`, and `ALTER TASK … RESUME`
6. **Grants** (optional) — `SELECT, INSERT` (no `UPDATE`/`DELETE`) for an app role;
   `SELECT` only on `document_text`/`ocr_log`; `MONITOR, OPERATE` on the task
7. **PyPI integration** (optional, SiS container runtime; usually ACCOUNTADMIN)
8. **`CREATE STREAMLIT`** (shown for copying only)
9. **Starter definitions** (optional) — a few document types, metadata types and tags,
   added as pending changes you then *Save to database*

Every statement is shown before it runs; each has its own *Run* button, and every step
has *Run all*. A progress bar shows how many of the 17 objects exist (including whether the task is
started); a log shows
each result.

Who may run the setup statements: on a local run, you; with credentials you entered
yourself, you (Snowflake's privileges decide what succeeds). In Streamlit in Snowflake,
only the app owner. On a shared deployment with login (e.g. Community Cloud) using the
shared connection, only the users listed in `MINIDMS_SETUP_USERS`. Everyone else sees
the statements read-only — but can always connect with their own credentials.

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
| `MINIDMS_RUNTIME` | Force `local` or `sis` if auto-detection gets it wrong |
| `MINIDMS_SETUP_USERS` | Comma-separated users allowed to run the Setup page (env or secrets) |

Locally, the person at the screen is the person whose credentials are used, so
`CURRENT_USER()` is the right actor.

## 2c. Run on Streamlit Community Cloud

The same code runs on [Streamlit Community Cloud](https://share.streamlit.io). It
connects to Snowflake over the internet, like the local version.

**Deploy:** on [share.streamlit.io](https://share.streamlit.io) → *Create app* → pick the
GitHub repository and branch, main file `streamlit_app.py`; under *Advanced settings*
choose Python 3.11. Then choose how the app gets its Snowflake credentials:

**Option A — enter them in the app (quickest).** Deploy without secrets. Open the app →
it links to **Setup** → step 1: enter account, user, role, warehouse, database, schema
and a key pair / token / password → *Connect for this browser session*. Then run the
setup steps. Each viewer connects with their own Snowflake user, so the actor is right
automatically. Credentials live only in memory for that browser tab; after a reload you
enter them again.

**Option B — a shared service user in the secrets (for a team).** Everybody uses one
connection; nobody has to enter credentials.

1. **Service user with a key pair.** Generate the key pair on the Setup page (step 1 →
   *Generate a key pair*, e.g. from a local run or with Option A), or with openssl:
   ```bash
   openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
   openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
   ```
   ```sql
   CREATE ROLE IF NOT EXISTS MINIDMS_APP;
   CREATE USER IF NOT EXISTS MINIDMS_SVC TYPE = SERVICE
     DEFAULT_ROLE = MINIDMS_APP DEFAULT_WAREHOUSE = <wh>
     RSA_PUBLIC_KEY = '<public key without the BEGIN/END lines>';
   GRANT ROLE MINIDMS_APP TO USER MINIDMS_SVC;
   ```
   Create the objects and grant `MINIDMS_APP` its privileges (Setup steps 2–5).
2. **Secrets.** Paste into *⋮ → Settings → Secrets*. Template:
   [`.streamlit/secrets.community-cloud.toml.example`](.streamlit/secrets.community-cloud.toml.example).
   The Setup page can also generate the `[connections.snowflake]` section for you
   (step 1 → *Save the connection permanently*). The private key can stay in PEM form;
   the app converts it.
3. **Login.** With one Snowflake user for everybody, `CURRENT_USER()` cannot tell viewers
   apart. Add Streamlit's login (`[auth]` in the secrets, e.g. Google as OpenID Connect
   provider with the redirect URI `https://<your-app>.streamlit.app/oauth2callback`).
   The app then records the viewer's login e-mail as the actor, gives each viewer their
   own `@sessions` folder, and asks for login before it touches Snowflake. List the
   people who may run Setup in `MINIDMS_SETUP_USERS`.
4. **Check.** Log in, open **Diagnostics**: *Viewer (actor)* must be your e-mail and
   `CURRENT_USER()` the service user. Open **Setup**: the progress bar must be full.

Both options can be combined: with secrets configured, a viewer can still connect with
their own credentials on the Setup page (and disconnect again).

Notes: if your Snowflake account has a network policy, it must allow connections from
Community Cloud. Queries, Cortex text extraction and storage are billed to your Snowflake
account. Cached data is keyed by account, role, database and schema, so viewers on
different connections never see each other's cached data; viewers on the same one share
it. Community Cloud gives an app limited memory, which caps how large the logs can grow
before you need the reduced view (§9.10).

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
- **Files** go to `@doc_files/{sha256}/{filename}` at upload time (the name is made
  path-safe; the original name stays in `document_file_log.filename`). Same content under
  the same name is stored once. The `document_file_log` row that points to the file waits
  in your session like every other change.
- **Text (OCR) is extracted in Snowflake, after Save to database.** The new
  `document_file_log` rows appear in the stream `document_file_log_stream`; that fires
  the triggered task `extract_text_task`, which calls `extract_text()`:
  1. it consumes the stream and queues each new active file without text (`ocr_log`
     status `queued`);
  2. for each queued file it reuses the text of an identical file (same checksum) or
     calls `AI_PARSE_DOCUMENT(TO_FILE('@doc_files', path), {'mode': 'OCR'})`, then
     appends to `document_text` and `ocr_log` (`done` with page count, `failed` with the
     error, or `skipped` for file types Cortex does not read: supported are PDF, DOCX,
     PPTX, JPEG/PNG/TIFF, HTML, TXT).

  The app never calls Cortex. The document's **Text** tab shows the status (*not saved
  yet*, *waiting*, *queued*, *done*, *failed*, *skipped*); for *failed* or *waiting*
  files, *Request text extraction again* appends a fresh file row, which goes through
  the same stream after saving. **Admin → Text extraction** shows the task state,
  counts, failures and recent runs, and can start a run on demand.
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
- A document is searchable only after it is saved *and* the task has extracted its text
  (usually well under a minute; the Search page says how many are still waiting).
- Files of other types (e.g. `.md`, `.csv`, `.xlsx`) are stored and downloadable but get
  no text (`skipped`).
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
5. `extract_text()` compiles and works: `CALL extract_text();` after saving one PDF, then
   `SELECT * FROM ocr_status_v;` (it binds the path into
   `AI_PARSE_DOCUMENT(TO_FILE('@doc_files', :v_rel), {'mode': 'OCR'})`).
6. `@st.cache_data` is shared across viewers on the compute pool (check `QUERY_HISTORY`
   after opening the app as two users).
7. The triggered task fires by itself: save a document, then check
   `TABLE(INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => 'EXTRACT_TEXT_TASK'))`.
