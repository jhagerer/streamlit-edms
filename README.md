# MiniDMS — a small document management system on Snowflake

A deliberately small clone of [Mayan EDMS](https://www.mayan-edms.com/), built with
Streamlit. It runs **in Streamlit in Snowflake** (container runtime), **locally**, and on
**Streamlit Community Cloud**. All data lives in Snowflake.

Features: upload, immutable content-addressed files with SHA-256 checksums, conversion to
PDF and page-by-page structured OCR with `AI_COMPLETE`, AI enrichment (summary,
document-type and tag suggestions, metadata extraction), page-level semantic and keyword
search with **Cortex Search**, document types, typed
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
   the PDF conversion and pages, the structured OCR per page, the text and the enrichments
   are append-only standard tables. The AI output is written by `INSERT … SELECT` inside
   Snowflake, so Snowflake records **column-level lineage** from the page file to the OCR
   fields, the text, the summary and the suggestions.
5. **The ingest pipeline runs in Snowflake, right after the upload.** An upload appends to
   `ai_request`; a stream on it fires a triggered task, which calls `process_ai()`:
   1. **Convert and split** — the Python procedure `convert_to_pdf_pages()` converts the
      file to PDF (PDF passes through; images become one page per frame; plain text is
      typeset) and writes one single-page PDF per page to `@doc_files/{sha256}/derived/`.
      It writes files only; the rows in `pdf_conversion` and `pdf_page` are then recorded
      with `INSERT … SELECT FROM stored_file`, using the SQL functions `pdf_converter()`,
      `pdf_path()`, `pdf_page_path()` and the Python UDF `pdf_page_count()`. Other file
      types are `skipped`.
   2. **Page OCR** — `ocr_pages()` calls `AI_COMPLETE` (`claude-haiku-4-5`) with a JSON
      schema on every page, in parallel, into `page_ocr`: `ocr_text` (images described at
      length inline as `[Image: …]`), `headings_list`, `image_short_summaries_list`,
      `table_summaries_list`, `page_short_summary`, plus `index_text` = all fields except
      `ocr_text`. If one page fails, the pages are retried one by one.
   3. **Enrichment** — `AI_COMPLETE` (`claude-sonnet-4-5`) reads the full text (the view
      `document_text_current`: the pages' `ocr_text` in order) and writes the summary, type
      and tag suggestions and metadata to the **hybrid table** `document_enrichment` — one
      row per file and run (`PRIMARY KEY (file_id, run_id)`), so a rerun never writes a
      second result.

   The app never calls Cortex itself.

   **Column-level lineage:** every row the pipeline writes comes from an
   `INSERT … SELECT` whose data columns are computed from columns of its source tables.
   Procedure variables appear only in `WHERE` clauses (which request, file or page) and as
   error messages, never as values; a test enforces this. The chain is
   `ai_request` / `stored_file` → `pdf_conversion` → `pdf_page` → `page_ocr` →
   `document_text_current` (view) → `document_enrichment`, and `page_ocr.index_text` →
   Cortex Search.
   Writing the PDF files is the one step outside SQL; the tables see it through the stage
   paths, which are themselves derived columns.
6. **Two sinks: Cortex Search and a hybrid table.** The ingest output ends up in the
   Cortex Search service (for search) and in the hybrid table `document_enrichment` (the
   AI result people read and act on, looked up one document at a time). The Cortex Search
   service can't be written to directly; its source `page_ocr` stays a standard table with
   change tracking, as a service source must be. The full text is a view, not a table.
   The service `document_search`
   indexes **one search document per page**: the `index_text` of the latest successful run
   per file in `page_ocr` (multilingual embedding model, target lag 1 minute). The Search
   page queries it and gets ranked pages with `document_id`/`file_id`/`page_no`; type, tag
   and trash filters run in the app, because those live in the hybrid tables.
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
| `document_enrichment` | **hybrid**, append-only | Sink of the pipeline; read per document and acted on by people (*Apply*); `PRIMARY KEY (file_id, run_id)` makes the pipeline idempotent |
| `stored_file`, `ai_request`, `pdf_conversion`, `pdf_page`, `page_ocr`, `ai_job_log` | standard, append-only | Facts and pipeline steps written by `INSERT … SELECT` — lineage; large text; `page_ocr` is the change-tracked source of the Cortex Search service |
| `document_text_current` | view | The full text, derived from `page_ocr` — not stored twice |

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
4. **Hybrid tables** — the seven append-only logs people edit, plus `document_enrichment`
5. **Standard tables** — `stored_file`, `ai_request`, `pdf_conversion`, `pdf_page`,
   `page_ocr`, `ai_job_log`
6. **Views** — current state per entity, latest text / enrichment per file, AI state per
   file, per-document history, audit trail
7. **AI pipeline** — stream `ai_request_stream`, procedure `process_ai()`, triggered task
   `process_ai_task` on `minidms_wh`, and `ALTER TASK … RESUME`
8. **Cortex Search** — the service `document_search` over `page_ocr`
9. **Grants** (optional) — for an app role: `SELECT, INSERT` on the hybrid logs,
   `stored_file` and `ai_request`; `SELECT` only on the pages and the AI output; never
   `UPDATE` or `DELETE`; `USAGE` on the search service and `minidms_wh`; `MONITOR, OPERATE`
   on the task
10. **PyPI integration** (optional, SiS container runtime; usually ACCOUNTADMIN)
11. **`CREATE STREAMLIT`** (shown for copying only)
12. **Starter definitions** (optional) — a few document types, metadata types and tags

Every statement is shown before it runs; each has its own *Run* button, and every step
has *Run all*. A progress bar shows how many of the 40 objects exist (including whether
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
small stand-in answers Cortex Search queries from `page_ocr.index_text`. The tests cover the
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
- **Ingest pipeline** — right after the upload, Snowflake runs `process_ai()`: convert to
  PDF, split into pages, structured OCR per page with `AI_COMPLETE` (Haiku), then summary,
  document-type and tag suggestions and metadata extraction (Sonnet). An identical file
  that was processed before reuses its page OCR. Supported: PDF; JPG, PNG, GIF, WEBP, BMP,
  TIFF; TXT, MD, CSV, JSON, XML. Other types are `skipped`. Lists show the AI state per
  document.
- **Document → Text & AI** — the OCR text, the structured result per page (what the search
  indexes), the summary and the **suggestions**. Nothing
  changes until you click *Apply*; the new version then records `source = ai:<enrichment
  id>`. *Process again* runs the pipeline again (and re-indexes the file).
- **Editing** — every change appends a new version. If someone else changed the same thing
  since your page was drawn, you see *“changed by someone else”*, nothing is written, and
  the page shows the latest data. Edits to different things do not conflict.
- **Search** — type a question or keywords; Cortex Search returns the best-matching
  pages, the list shows the documents in order of relevance with their best page
  (*“p. 3: …”*). Type and
  tag filters apply on top. New documents are searchable once the pipeline has run and the
  service has refreshed (target lag 1 minute).
- **History** — every version of the document's records, plus conversion, OCR and
  enrichment events.
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
  pages), so a heavily filtered search can show fewer results than exist.
- The raw `ocr_text` is not indexed (by design): a search matches the page summary,
  headings and image/table summaries. A word that appears only in the body text may not
  be found.
- Office files (Word, Excel, PowerPoint) are not converted — that would need LibreOffice,
  which does not run inside Snowflake. Text files are typeset in Courier, which covers
  Latin-1 only (other characters show as boxes).
- Very long PDFs cost one `AI_COMPLETE` call per page and run inside one task run.
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
   `document_enrichment` (hybrid, with `ARRAY` columns) accepts `INSERT … SELECT` with
   `AI_COMPLETE` from inside `process_ai()`. If hybrid tables turn out to be allowed as a
   Cortex Search source, `page_ocr` could become hybrid too.
3. **Conversion:** the Anaconda terms are accepted and `pypdf`, `pillow` and `reportlab`
   are available to Python procedures and UDFs; `SnowflakeFile.open('@doc_files/…', 'rb',
   require_scoped_url=False)` reads staged files in both, `session.file.put_stream` writes
   the page PDFs from inside the procedure, and `CALL …` followed by
   `RESULT_SCAN(LAST_QUERY_ID())` returns its result inside `process_ai()`. Upload a PDF, a PNG and a TXT and check
   `SELECT * FROM pdf_conversion;` and `LIST @doc_files PATTERN = '.*/derived/.*';`.
4. **Page OCR:** `claude-haiku-4-5` is available to `AI_COMPLETE` in your region, and the
   call form `AI_COMPLETE(model => …, prompt => …, file => TO_FILE(…), model_parameters =>
   …, response_format => …)` works on a single-page PDF. Check `SELECT * FROM page_ocr;`,
   `SELECT * FROM ai_status_v;` and
   `TABLE(INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => 'PROCESS_AI_TASK'))`. If PDFs are
   not accepted as files, render the pages to PNG in the conversion procedure instead.
5. **Cortex Search:** `CREATE CORTEX SEARCH SERVICE … ON index_text … EMBEDDING_MODEL =
   'snowflake-arctic-embed-l-v2.0'` with a `QUALIFY` in the source query is accepted, and
   `SNOWFLAKE.CORTEX.SEARCH_PREVIEW('<db>.<schema>.document_search', '{"query": …}')`
   returns results. For production traffic, Snowflake recommends the Python/REST API of
   the service; switching `core/search.py` to it is a small change.
6. **Column-level lineage** appears for the `page_ocr` columns and the
   `document_enrichment` columns (a hybrid table, written through the view
   `document_text_current`) (Snowsight lineage or
   `SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY`, Enterprise edition or higher). The Python
   conversion step writes files, not columns: `pdf_page.stage_path` is derived from
   `stored_file.checksum`, and `pdf_conversion.page_count` from `stored_file.stage_path`
   through the UDF `pdf_page_count()` — check that lineage passes through the Python UDF
   and through `LATERAL FLATTEN` (for `pdf_page.page_no`).
7. `@st.cache_data` is shared across viewers on the compute pool (check `QUERY_HISTORY`
   after opening the app as two users).
