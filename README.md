# MiniDMS — a small document management system on Snowflake

A deliberately small clone of [Mayan EDMS](https://www.mayan-edms.com/), built with
Streamlit. It runs **in Streamlit in Snowflake** (container runtime), **locally**, and on
**Streamlit Community Cloud**. All data lives in Snowflake.

Features: upload, immutable content-addressed files with SHA-256 checksums, OCR and AI
enrichment with `AI_COMPLETE` (summary, document-type and tag suggestions, metadata
extraction), semantic and keyword search with **Cortex Search**, document types, typed
metadata, tags, trash, conflict detection for concurrent edits, per-document history and
an audit trail — with column-level lineage for everything the AI produces.

Original design: [`docs/09-streamlit-architecture.md`](docs/09-streamlit-architecture.md) ·
[`docs/10-streamlit-implementation-plan.md`](docs/10-streamlit-implementation-plan.md)
(the architecture has since moved on — see below) ·
Schema: [`sql/minidms-setup.sql`](sql/minidms-setup.sql)

## Architecture

1. **One global state, in Snowflake.** No local or per-session copy of any data and no
   separate save step: every change is written to Snowflake immediately.
2. **Every table is append-only — hybrid tables included.** Nothing is ever updated or
   deleted; the grants enforce it (`SELECT, INSERT` only). The current state is the latest
   version per key, in the `*_current` views. Removing a metadata value appends
   `value = NULL`, detaching a tag appends `assigned = FALSE`, trashing appends
   `status = 'trashed'`.
3. **Hybrid tables for what people edit** — documents, metadata values, tag assignments,
   file attachments and the definitions, one row per version. Each user action is one
   transaction that appends the next version (*the version the user saw + 1*) of every
   record it touches:
   - `PRIMARY KEY (id, version)` is enforced: if someone else saved that version first, the
     insert fails, nothing is written, and the user sees a **conflict** message;
   - `UNIQUE (event_id)`: a retried action fails the same way, but its `event_id` is already
     there, so it is reported as **already applied**, never applied twice.
4. **Standard tables for facts and AI output.** Uploaded files (`stored_file`), AI requests,
   OCR text, enrichments and the search corpus are append-only standard tables, written by
   `INSERT … SELECT` inside Snowflake, so Snowflake records **column-level lineage** from the
   file to the text, the summary, the suggestions and the search chunks.
5. **The ingest pipeline runs in Snowflake, right after the upload.** An upload appends to
   `ai_request`; a stream on it fires a triggered task, which calls `process_ai()`:
   OCR and enrichment with `AI_COMPLETE` (`claude-sonnet-4-5`), then the text is split into
   chunks (`SPLIT_TEXT_RECURSIVE_CHARACTER`) and appended, with the summary, to
   `search_corpus`. The app never calls Cortex itself.
6. **Cortex Search is the sink and the search engine.** The service `document_search`
   indexes the latest run per file in `search_corpus` (multilingual embedding model,
   target lag 1 minute). The Search page queries it and gets ranked chunks with
   `document_id`/`file_id`; type, tag and trash filters run in the app, because those live
   in the hybrid tables.
7. **Any warehouse; adaptive only for bootstrapping.** The app runs on whatever warehouse
   the connection (or the Streamlit app) uses. For an empty account, the setup creates the
   adaptive warehouse `minidms_wh` if it does not exist; the AI task and the search service
   run on it. No serverless tasks.
8. **Caching where it is cheap and safe.** The current-state views are small (one row per
   entity) and are loaded whole into polars frames, cached and shared by all viewers. The
   cache key is the **row count** of the tables a frame is built from — append-only means a
   count changes with every commit and never shrinks, so it also catches writes made
   outside the app. One query per click reads all counts; a TTL is an extra safety net.
9. **One lock per shared Snowflake session.** All SQL runs through `core/db.py`, so one
   viewer's statement can never slip into another viewer's open transaction.

### Hybrid or standard?

| Data | Table type | Why |
|---|---|---|
| Documents, metadata values, tag assignments, attachments, definitions | **hybrid**, append-only | Edited one data point at a time by people; enforced `PRIMARY KEY (id, version)` and `UNIQUE (event_id)` give conflict detection and idempotent retries |
| `stored_file`, `ai_request`, `document_text`, `document_enrichment`, `search_corpus`, `ai_job_log` | standard, append-only | Facts and AI output written by `INSERT … SELECT` — lineage; large text; `search_corpus` is the change-tracked source of the Cortex Search service |

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
  read.py               change tokens (row counts), cached loads, history, audit
  write.py              commands: append the next version, one transaction per action
  files.py              @doc_files upload ({checksum}/{filename}), orphan housekeeping
  ai.py                 AI task state, recent runs, run on demand
  model.py              what the pages show: documents, files + AI state, suggestions
  search.py             Cortex Search queries (SEARCH_PREVIEW), ranking, snippets
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
4. **Hybrid tables** — the seven append-only logs people edit
5. **Standard tables** — `stored_file`, `ai_request`, `document_text`,
   `document_enrichment`, `search_corpus`, `ai_job_log`
6. **Views** — current state per entity, latest text / enrichment per file, AI state per
   file, per-document history, audit trail
7. **AI pipeline** — stream `ai_request_stream`, procedure `process_ai()`, triggered task
   `process_ai_task` on `minidms_wh`, and `ALTER TASK … RESUME`
8. **Cortex Search** — the service `document_search` over `search_corpus`
9. **Grants** (optional) — for an app role: `SELECT, INSERT` on the hybrid logs,
   `stored_file` and `ai_request`; `SELECT` only on the AI output and `search_corpus`; never
   `UPDATE` or `DELETE`; `USAGE` on the search service and `minidms_wh`; `MONITOR, OPERATE`
   on the task
10. **PyPI integration** (optional, SiS container runtime; usually ACCOUNTADMIN)
11. **`CREATE STREAMLIT`** (shown for copying only)
12. **Starter definitions** (optional) — a few document types, metadata types and tags

Every statement is shown before it runs; each has its own *Run* button, and every step
has *Run all*. A progress bar shows how many of the 32 objects exist (including whether
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
same constraints as the hybrid logs (`PRIMARY KEY (id, version)`, `UNIQUE (event_id)`),
so transactions, rollbacks, version conflicts and idempotent retries behave for real; a
small stand-in answers Cortex Search queries from `search_corpus`. The tests cover the
write commands (append-only, conflicts, retries, provenance), the row-count tokens and
caches, the per-session lock, the AI state per file and the suggestions, Cortex Search
requests, ranking and literal escaping, the setup script (it must match `core/schema.py`
column for column, contain no `UPDATE`/`DELETE`, write AI output with `INSERT … SELECT`,
feed the search service), the grants, connection settings, the login and warehouse gates,
and the real pages driven with Streamlit's `AppTest` — including concurrent edits.

## Using it

- **Upload** — choose files, a document type, tags and metadata. Each file becomes one
  document and is saved immediately. The bytes go to `@doc_files/{sha256}/{filename}`;
  same content under the same name is stored once.
- **Ingest pipeline** — right after the upload, Snowflake runs `process_ai()`: OCR with
  `AI_COMPLETE` (or the text of an identical file), then summary, document-type and tag
  suggestions and metadata extraction with `AI_COMPLETE` and a JSON schema, then the text
  chunks and the summary go to `search_corpus`, which Cortex Search indexes. File types
  `AI_COMPLETE` does not read are `skipped`. Lists show the AI state per document.
- **Document → Text & AI** — the OCR text, the summary and the **suggestions**. Nothing
  changes until you click *Apply*; the new version then records `source = ai:<enrichment
  id>`. *Process again* runs the pipeline again (and re-indexes the file).
- **Editing** — every change appends a new version. If someone else changed the same thing
  since your page was drawn, you see *“changed by someone else”*, nothing is written, and
  the page shows the latest data. Edits to different things do not conflict.
- **Search** — type a question or keywords; Cortex Search returns the best-matching
  chunks, the list shows the documents in order of relevance with the best match. Type and
  tag filters apply on top. New documents are searchable once the pipeline has run and the
  service has refreshed (target lag 1 minute).
- **History** — every version of the document's records, plus OCR and enrichment events.
- **Admin** — AI task state, counts, failures, *process unprocessed files*, *run the task
  now*, recent runs; table sizes; orphaned-file scan and cleanup; credit usage.

## FAQ

**Is there a save button?** No. Every change is written to Snowflake the moment you make it.

**Can anything be deleted or overwritten?** No. Every table is append-only, enforced by the
grants. "Delete" means a new version: *trash* (restorable), *deactivate* (types, tags),
*remove* (files detached, bytes kept) or an empty value (metadata).

**What happens if two people edit the same thing?** Both try to append the same next
version; the primary key lets only the first one through. The second gets a conflict
message and sees the latest data.

## Known limits

- One query per click counts the rows of every table. Cheap at this scale; with very large
  logs, cache it for a few seconds or switch to `SYSTEM$LAST_CHANGE_COMMIT_TIME`.
- The `*_current` views pick the latest version per key on every load; the logs grow with
  every edit. At large volumes, materialise the current state (e.g. a dynamic table).
- Each write takes a lock on the shared Snowflake session for a few milliseconds, so writes
  from many simultaneous users queue up briefly.
- Search results lag the upload by the pipeline run plus the service's target lag.
- Type, tag and trash filters are applied after the search (the service returns up to 50
  chunks), so a heavily filtered search can show fewer results than exist.
- Uploaded bytes of an interrupted upload stay in `@doc_files` until cleaned up on Admin.
- No page viewer — use the OCR text and the download button.
- The schema has no link between document types and metadata types, so all active
  metadata types are offered for every document.

## Pre-flight checks — to do in your account

These could not be verified without a Snowflake account. Please check them once:

1. `CREATE ADAPTIVE WAREHOUSE` is available to your role (only needed if `minidms_wh` does
   not exist), and a **triggered task with `WAREHOUSE = minidms_wh`** runs on it.
2. **Hybrid tables** are available in your account/region, with
   `PRIMARY KEY (…, version)` plus `UNIQUE (event_id)`; a duplicate raises an error that
   mentions the duplicate key (the app turns it into a conflict or "already applied").
3. `process_ai()` compiles and works: upload one PDF, then check
   `SELECT * FROM ai_status_v;`, `SELECT * FROM search_corpus;` and
   `TABLE(INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => 'PROCESS_AI_TASK'))` — in particular
   the `AI_COMPLETE` calls (`PROMPT(…, TO_FILE(…))`; `model => …, prompt => …,
   response_format => {…}`) and `SPLIT_TEXT_RECURSIVE_CHARACTER`.
4. **Cortex Search:** `CREATE CORTEX SEARCH SERVICE … EMBEDDING_MODEL =
   'snowflake-arctic-embed-l-v2.0'` with a `QUALIFY` in the source query is accepted, and
   `SNOWFLAKE.CORTEX.SEARCH_PREVIEW('<db>.<schema>.document_search', '{"query": …}')`
   returns results. For production traffic, Snowflake recommends the Python/REST API of
   the service; switching `core/search.py` to it is a small change.
5. **Column-level lineage** appears for `document_text.content`, the `document_enrichment`
   columns and `search_corpus.text` (Snowsight lineage or
   `SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY`, Enterprise edition or higher).
6. `@st.cache_data` is shared across viewers on the compute pool (check `QUERY_HISTORY`
   after opening the app as two users).
