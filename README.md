# MiniDMS — a small document management system on Snowflake

A deliberately small clone of [Mayan EDMS](https://www.mayan-edms.com/), built with
Streamlit. It runs **in Streamlit in Snowflake** (container runtime), **locally**, and on
**Streamlit Community Cloud**. All data lives in Snowflake.

Features: upload, immutable content-addressed files with SHA-256 checksums, OCR and AI
enrichment with `AI_COMPLETE` (summary, document-type and tag suggestions, metadata
extraction), full-text search, document types, typed metadata, tags, trash, conflict
detection for concurrent edits, per-document history and an audit trail — with
column-level lineage for everything the AI produces.

Original design: [`docs/09-streamlit-architecture.md`](docs/09-streamlit-architecture.md) ·
[`docs/10-streamlit-implementation-plan.md`](docs/10-streamlit-implementation-plan.md)
(the architecture has since moved on — see below) ·
Schema: [`sql/minidms-setup.sql`](sql/minidms-setup.sql)

## Architecture

1. **One global state, in Snowflake.** There is no local or per-session copy of any
   data and no separate save step: every change is written to Snowflake immediately.
2. **Hybrid tables for everything people edit.** Documents, metadata values, tags, file
   attachments and the definitions (types, metadata types, tags) are hybrid tables with
   enforced primary keys and a `version` per row. Each edit is one transaction:
   - a row in `change_log` — its `event_id` is the **idempotency key** (enforced primary
     key), so a retried action is rejected as a whole and never applied twice;
   - the change, `… WHERE version = <the version the user saw>` — if someone else
     changed it first, nothing is written and the user sees a **conflict** message;
   - a bump of the touched tables' counters in `sync_state`.
3. **Standard tables for facts and AI output.** Uploaded files (`stored_file`), AI
   requests, OCR text and enrichments are append-only standard tables, written by
   `INSERT … SELECT` inside Snowflake — so Snowflake records **column-level lineage** from
   the file to the text to the summary and suggestions.
4. **AI runs in Snowflake, right after the save.** An upload appends to `ai_request`; a
   stream on it fires a triggered task, which calls `process_ai()`: OCR and enrichment
   with `AI_COMPLETE` (`claude-sonnet-4-5`). The app never calls Cortex — the AI calls
   take seconds to minutes and would block the shared Snowflake session for every
   viewer, and calling it from the app would lose the lineage.
5. **Any warehouse; adaptive only for bootstrapping.** The app runs on whatever warehouse
   the connection (or the Streamlit app) uses. For an empty account, the setup creates the
   adaptive warehouse `minidms_wh` if it does not exist; the AI task runs on it (change
   with `ALTER TASK … SET WAREHOUSE`). No serverless tasks.
6. **Caching where it is cheap and safe.** The current-state tables are small (one row
   per entity) and are loaded whole into polars frames, cached and shared by all
   viewers. The cache key is the table's counter in `sync_state`, read with one point
   query per click; a 10-minute TTL covers writes made outside the app. The large OCR
   text and the history are queried per document, never bulk-loaded.

### Where hybrid tables make sense — and where they don't

| Data | Table type | Why |
|---|---|---|
| Documents, metadata values, tags, attachments, definitions | **hybrid** | Edited one data point at a time by people; enforced primary keys and row versions give conflict detection instead of last-writer-wins |
| `change_log` | **hybrid** | Its `event_id` primary key makes every action idempotent; indexed by document for the History tab |
| `sync_state` | **hybrid** | One point read per click, small point updates on every write — exactly what hybrid tables are fast at |
| `stored_file`, `ai_request`, `document_text`, `document_enrichment`, `ai_job_log` | standard | Append-only facts written by `INSERT … SELECT`; large text, full-text search and lineage; scans and the result cache work better on standard tables |

## Project layout

```
streamlit_app.py        entry point (st.navigation, one counter probe per rerun, warehouse gate)
app_pages/              Streamlit pages
  _state.py             per-session helpers: counters, cached tables, act(), conflict baselines
  _ui.py                sidebar, notices, warehouse gate, document list, metadata widgets
  documents.py upload.py search.py trash.py document.py
  document_types.py metadata_types.py tags.py audit.py admin.py diagnostics.py
  setup.py              runs the setup SQL step by step from inside the app
  _connection.py        Setup step 1: enter/change Snowflake credentials, key pairs
core/                   plain Python (no Streamlit, except core/session.py)
  session.py            Snowpark session (SiS, own credentials, or secrets), viewer
                        identity, cache decorator
  db.py                 the one place SQL runs: per-session lock, transactions, conflicts
  schema.py             tables, views, pinned dtypes; hybrid vs. standard
  read.py               change counters (sync_state), cached loads, history, audit
  write.py              commands: one transaction per action, version checks, change_log
  files.py              @doc_files upload ({checksum}/{filename}), orphan housekeeping
  ai.py                 AI task state, recent runs, run on demand
  model.py              what the pages show: documents, files + AI state, suggestions
  search.py             SEARCH() / ILIKE over OCR text and summaries
  setup.py              setup script parsing, grants, schema status checks
  connection.py         connection settings → connector params, secrets.toml, key pairs
sql/                    setup DDL, grants and deployment
tests/                  pytest; runs without Snowflake (SQLite-backed fake session)
```

The pages folder is called `app_pages/` (not `pages/`), so Streamlit's automatic
multipage detection does not interfere with `st.navigation`.

## 1. Set up the schema (once)

Two ways — both run the same idempotent statements (nothing is ever dropped):

**A. From the app: System → Setup.** The page works before any MiniDMS object (and
before any warehouse) exists; when the schema is missing, every other page
links to it. It walks through:

1. **Connection** — shows account, user, role, warehouse, database, schema and where
   the connection comes from. Outside Streamlit in Snowflake you can also enter or
   change credentials (key pair, programmatic access token, password + MFA passcode,
   or browser SSO on your own computer), generate a key pair, render a `secrets.toml`,
   switch database/schema/warehouse, and disconnect/reconnect. In Streamlit in Snowflake
   there is nothing to enter.
2. **Warehouse, only if missing** — creates the adaptive `minidms_wh` if it does not exist
   (then *Use minidms_wh now* when the session has no warehouse)
3. **Stage** — `@doc_files`
4. **Hybrid tables** — the nine tables people edit, plus filling `sync_state`
5. **Standard tables** — `stored_file`, `ai_request`, `document_text`,
   `document_enrichment`, `ai_job_log`
6. **Views** — latest text / enrichment per file, AI state per file, audit trail
7. **AI pipeline** — stream `ai_request_stream`, procedure `process_ai()`, triggered task
   `process_ai_task` on `minidms_wh`, and `ALTER TASK … RESUME`
8. **Grants** (optional) — for an app role: `SELECT, INSERT, UPDATE` on the hybrid
   tables (plus `DELETE` where removing is the operation: metadata values, tag
   assignments), `SELECT, INSERT` only on `change_log`, `SELECT` only on the AI output,
   `USAGE` on `minidms_wh`, `MONITOR, OPERATE` on the task
9. **PyPI integration** (optional, SiS container runtime; usually ACCOUNTADMIN)
10. **`CREATE STREAMLIT`** (shown for copying only)
11. **Starter definitions** (optional) — a few document types, metadata types and tags

Every statement is shown before it runs; each has its own *Run* button, and every step
has *Run all*. A progress bar shows how many of the 25 objects exist (including whether
the task is started); a log shows each result.

Who may run the setup statements: on a local run, you; with credentials you entered
yourself, you (Snowflake's privileges decide what succeeds). In Streamlit in Snowflake,
only the app owner. On a shared deployment with login (e.g. Community Cloud) using the
shared connection, only the users listed in `MINIDMS_SETUP_USERS`.

**B. In a worksheet.** Run [`sql/minidms-setup.sql`](sql/minidms-setup.sql), then adapt
[`sql/minidms-grants-and-deploy.sql`](sql/minidms-grants-and-deploy.sql).

## 2a. Run in Streamlit in Snowflake

1. Put the files of this repository into a **Snowflake Workspace** (for example connect
   the Git repository) and create a Streamlit app from it with
   `streamlit_app.py` as the main file. Or use the `CREATE STREAMLIT` statement in
   `sql/minidms-grants-and-deploy.sql`.
2. Use the **container runtime** (`SYSTEM$ST_CONTAINER_RUNTIME_PY3_11`) with a compute
   pool, and attach the PyPI external access integration. The app installs
   `requirements.txt` from PyPI; without the integration it will not start.
3. Create the app **in the same schema** as the MiniDMS tables. The app uses
   unqualified names (`document`, `@doc_files`, …).
4. Set the app's **query warehouse** — any warehouse works, e.g. `minidms_wh` from the setup.
5. Check: `DESC STREAMLIT <name>` shows the container runtime and a compute pool.
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
**database and schema** where the MiniDMS objects live, and a warehouse (any; or set
`MINIDMS_WAREHOUSE`, e.g. to `MINIDMS_WH` from the setup).

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
     DEFAULT_ROLE = MINIDMS_APP DEFAULT_WAREHOUSE = MINIDMS_WH
     RSA_PUBLIC_KEY = '<public key without the BEGIN/END lines>';
   GRANT ROLE MINIDMS_APP TO USER MINIDMS_SVC;
   ```
   Create the objects and grant `MINIDMS_APP` its privileges (Setup page).
2. **Secrets.** Paste into *⋮ → Settings → Secrets*. Template:
   [`.streamlit/secrets.community-cloud.toml.example`](.streamlit/secrets.community-cloud.toml.example).
   The Setup page can also generate the `[connections.snowflake]` section for you
   (step 1 → *Save the connection permanently*). The private key can stay in PEM form;
   the app converts it.
3. **Login.** With one Snowflake user for everybody, `CURRENT_USER()` cannot tell viewers
   apart. Add Streamlit's login (`[auth]` in the secrets, e.g. Google as OpenID Connect
   provider with the redirect URI `https://<your-app>.streamlit.app/oauth2callback`).
   The app then records the viewer's login e-mail as the actor and asks for login
   before it touches Snowflake. List the
   people who may run Setup in `MINIDMS_SETUP_USERS`.
4. **Check.** Log in, open **Diagnostics**: *Viewer (actor)* must be your e-mail and
   `CURRENT_USER()` the service user. Open **Setup**: the progress bar must be full.

Both options can be combined: with secrets configured, a viewer can still connect with
their own credentials on the Setup page (and disconnect again).

Notes: if your Snowflake account has a network policy, it must allow connections from
Community Cloud. Queries, Cortex text extraction and storage are billed to your Snowflake
account. Cached data is keyed by account, role, database and schema, so viewers on
different connections never see each other's cached data; viewers on the same one share
it. Community Cloud gives an app limited memory; the cached tables hold current state
(one row per entity), so this grows with the number of documents, not with edits.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests need no Snowflake account. The fake session runs on in-memory SQLite with the
same primary keys as the hybrid tables, so transactions, rollbacks, version conflicts and
idempotent retries behave for real. They cover the write commands (one transaction per
action, conflicts, retries, provenance), the change counters and caches, the per-session
lock, the AI state per file and the suggestions, search, the setup script (it must match
`core/schema.py` column for column; warehouse bootstrap; lineage-friendly
`INSERT … SELECT`), grants, connection settings, the login and warehouse gates, and the
real pages driven with Streamlit's `AppTest` — including concurrent edits by two users.

## Using it

- **Upload** — choose files, a document type, tags and metadata. Each file becomes one
  document and is saved immediately. The bytes go to `@doc_files/{sha256}/{filename}`;
  same content under the same name is stored once.
- **AI** — right after the upload, Snowflake runs `process_ai()`: it copies the text of an
  identical file if there is one, or runs `AI_COMPLETE` on the staged file for the OCR
  text; then `AI_COMPLETE` with a JSON schema writes a summary, a document-type and tag
  suggestion and extracted metadata values. File types `AI_COMPLETE` does not read are
  `skipped`. Lists show the AI state per document; the sidebar says how many files are
  still being processed.
- **Document → Text & AI** — the OCR text, the summary and the **suggestions**. Nothing
  changes until you click *Apply*; the history then records `source = ai:<enrichment id>`.
  *Process again* asks Snowflake to run the AI again.
- **Editing** — properties, metadata, tags and files are saved when you click the button
  next to them. If someone else changed the same thing since your page was drawn, you see
  *“changed by someone else”*, nothing is written, and the page shows the latest data.
- **Documents / Search / Trash** — filter in memory by type, tags and text (including the
  summary). **Search** asks Snowflake for full-text matches in the OCR text and the
  summaries (`SEARCH()` or substring).
- **History** — every human edit from `change_log` plus OCR and enrichment events.
- **Admin** — AI task state, counts, failures, *process unprocessed files*, *run the
  task now*, recent runs; table sizes; orphaned-file scan and cleanup; credit usage.
- **Diagnostics** — actor, warehouse type, `st.user`, change counters, cached tables and
  load times, probe latency.

## FAQ

**Is there a save button?**
No. Every change is written to Snowflake the moment you make it — one global state.

**What happens if two people edit the same document?**
Each row has a version. The second save of the same thing is rejected with a conflict
message instead of silently overwriting the first; the page then shows the latest data.
Edits to *different* things (two metadata fields, two tags) do not conflict.

**Why can't I delete documents?**
"Delete" means *trash* (a status, restorable), *deactivate* (types, tags), *remove*
(files are detached, bytes kept) or an empty value (metadata). Every change is in
`change_log`, which the app can only append to.

## Known limits

- One point query on `sync_state` per click, plus small loads when something changed.
- Each write takes a lock on the shared Snowflake session for a few milliseconds, so
  writes from many simultaneous users queue up briefly.
- A document is searchable, and has suggestions, only after the AI task has run for it.
- Writes made outside the app (manual SQL) reach the app's cache only after the TTL
  (10 minutes) unless they also bump `sync_state`.
- Uploaded bytes of an interrupted upload stay in `@doc_files` until cleaned up on
  **Admin**.
- No page viewer — use the OCR text and the download button.
- The schema has no link between document types and metadata types, so all active
  metadata types are offered for every document.

## Pre-flight checks — to do in your account

These could not be verified without a Snowflake account. Please check them once:

1. `CREATE ADAPTIVE WAREHOUSE` is available to your role (only needed if `minidms_wh` does
   not exist), and a **triggered task with `WAREHOUSE = minidms_wh`** runs on it.
2. **Hybrid tables** are available in your account/region; the app's `UPDATE … WHERE version = ?` returns the number of
   rows updated, and a duplicate primary key raises an error mentioning the duplicate key.
3. `process_ai()` compiles and works: upload one PDF, then check
   `SELECT * FROM ai_status_v;` and `TABLE(INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME =>
   'PROCESS_AI_TASK'))`. In particular the `AI_COMPLETE` calls: `PROMPT(…, TO_FILE(…))`
   for the OCR and `AI_COMPLETE(model => …, prompt => …, response_format => {…})` for the
   enrichment.
4. **Column-level lineage** appears for `document_text.content` (from
   `stored_file.stage_path`) and the `document_enrichment` columns (from
   `document_text.content` and the definitions) — in Snowsight's lineage view or
   `SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY` (Enterprise edition or higher).
5. `@st.cache_data` is shared across viewers on the compute pool (check `QUERY_HISTORY`
   after opening the app as two users).
