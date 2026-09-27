# 10 — Step-by-step Implementation Plan (Streamlit on Compute Pools)

Companion to `09-streamlit-architecture.md`. Eight phases, each ending in something runnable with a stated acceptance test.

**Estimate:** 8–10 working days for one developer comfortable with Python and Snowflake. Roughly 1,800–2,500 lines.

Assumed to exist already: database, schema, warehouse. The app is deployed through **Snowflake Workspaces**. Schema objects come from `minidms-setup.sql` — two stages, eight tables, one view.

Rules that hold throughout:

- **`core/` never imports `streamlit`**, except `core/session.py`, the single place `st.user` is read. `pages/_state.py` is the only module touching `st.session_state`.
- **No `UPDATE`, no `DELETE` on log tables, ever.** If a phase seems to need one, the data model is wrong, not the rule.
- **Every write goes through save-to-session then save-to-database.** No page inserts directly.
- **Session state holds appends only.** Committed rows live exclusively in the shared cached frames and are never copied into `st.session_state` or into parquet.
- **`_pending` is a render-time annotation**, added in `view()` and nowhere else.

---

## Pre-flight: verify before building

Six assumptions this plan rests on that were not confirmed against documentation. Each is a few minutes in a worksheet or scratch app, and each would otherwise surface mid-phase as a confusing failure.

| # | Assumption | How to check |
|---|---|---|
| 1 | `session.create_dataframe(rows, schema=[names])` accepts a bare list of column names and infers `TIMESTAMP_NTZ` from Python `datetime` | Round-trip three rows into a scratch table; inspect resulting types |
| 2 | `SYSTEM$LAST_CHANGE_COMMIT_TIME` returns something usable for a table that has **never** been written to | Call it on a fresh empty table; confirm no error, and that a first insert changes the value |
| 3 | `REMOVE @sessions/{user}/` with a trailing slash removes that folder's contents and nothing outside it | Stage files in two folders, remove one, `LIST` both |
| 4 | `pl.read_parquet()` accepts the object `session.file.get_stream()` returns | Write a parquet via `put_stream`, read it back |
| 5 | `AI_PARSE_DOCUMENT(TO_FILE('@doc_files', <path>), {'mode':'OCR'})` in the exact form the app will call | Run the `SELECT` standalone against one staged PDF |
| 6 | **`@st.cache_data` is genuinely shared across viewers** on the compute pool | Open the app as two users; confirm the second sees no reload (check `QUERY_HISTORY`) |

Item 6 is the load-bearing one — the entire read design assumes one bulk load serves everybody. If it turns out to be per-session, the architecture still works but the cost and latency arguments weaken considerably, and that is worth knowing on day one rather than day eight.

Also confirm the two prerequisites from §9.2: that `DESC STREAMLIT` shows the container runtime with a pool attached, and that a PyPI external access integration exists. The second is the one thing that blocks startup outright.

---

## Phase 0 — Schema and app skeleton (0.5 day)

### Work

1. **Run `minidms-setup.sql`** in your schema — two stages, eight tables, `audit_v`.
2. **Confirm grants** per §9.3. The owning role needs `READ`/`WRITE` on both stages, `SELECT`/`INSERT` on the tables, `USAGE` on the warehouse, and `SNOWFLAKE.CORTEX_USER`. Deliberately *not* `UPDATE` or `DELETE` — that makes append-only database-enforced. Viewers need `USAGE` on the app only.
3. **`requirements.txt`** — `polars==1.*`, `pyarrow>=15`.
4. **`core/session.py`** — `get_session()` wrapping `get_active_session()`, `actor()` returning `st.user.user_name`, `slug(user)` producing a stage-path-safe folder name. The only module importing Streamlit under `core/`.
5. **`app.py`** — `st.navigation` and a diagnostics page printing `actor()`, the token dict, per-table row counts, and `polars.__version__`. **No login gate** — in SiS the viewer is already authenticated.
6. **Layering guard** as a test: no `streamlit` import under `core/` except in `session.py`.
7. **Run the six pre-flight checks** and record the results.

### Acceptance
App opens. Diagnostics shows the *viewer's* username — confirm by opening as a second user, it must differ — plus eight tokens, eight zero counts, a polars version. All six pre-flight items recorded.

### Watch out
Identity is the trap. `CURRENT_USER()` returns the app **owner** under owner's rights, not the viewer, so an app built on it attributes every audit row to whoever deployed it and gives all users the same `@sessions/` folder. Use `st.user.user_name`, and verify with two accounts before building anything on top.

---

## Phase 1 — The read path (1 day)

The foundation everything else sits on. Get it right before building any view.

### Work

1. **`core/read.py`**:
   - `READ_TABLES` and `APPEND_TABLES` per §9.5. Two lists, because `document_text` is appended to but never bulk-loaded.
   - `change_tokens()` — one `SELECT`, one `SYSTEM$LAST_CHANGE_COMMIT_TIME` per table in `APPEND_TABLES`. **No decorator.**
   - `load_log(table, token)` — `@st.cache_data`, `to_arrow()` → `pl.from_arrow()`. `READ_TABLES` only.
   - `load_event_ids(table, token)` — ids only, for `document_text`.
   - `latest(log, keys)` — sort by `(event_ts, event_id)`, then `unique(subset=keys, keep="last", maintain_order=True)`.
   - `view(table, appends)` — concat committed with the supplied appends, annotating `_pending`. Takes appends as an argument so `core/` never imports from `pages/`.
2. **Seed data by hand** — a few `INSERT`s so there is something to reduce.
3. **Extend the diagnostics page** with raw vs. reduced row counts and load timings. Keep it for the whole project; it is how you notice the memory ceiling approaching.
4. **Measure the probe** — time `change_tokens()` over twenty calls and write the number down. It is the per-interaction latency floor for the whole app.

### Acceptance
Insert a row from a worksheet; the next interaction shows it with no manual refresh. Interact repeatedly without changing data; `load_log` does not re-execute (confirm in `QUERY_HISTORY`).

### Watch out
`to_arrow()` preserves Snowflake types, not what pandas inference would have given you. Check `TIMESTAMP_NTZ` arrives as a datetime and `NUMBER` scale behaves. Pin expected dtypes in a test now — a silent type shift later breaks sorts and comparisons in ways that read as logic bugs.

---

## Phase 2 — Session snapshots and merge (1.5 days)

The heart of the design. Build and test it before anything writes.

### Work

1. **`core/snapshot.py`** — pure, takes the user identifier as an argument:
   - `session_path(user, table)`
   - `read_snapshot(user, table) -> pl.DataFrame | None`
   - `write_snapshot(user, table, df)` — `write_parquet(BytesIO, compression="zstd")` → `put_stream(..., overwrite=True)`
   - `remove_snapshots(user)` — `REMOVE @sessions/{slug}/`
   - `list_snapshots()` — for the admin page
2. **`core/read.py`: `clean_restored(snap, committed_ids)`** — the anti-join on `event_id`, returning only still-uncommitted rows. Pure over two frames, no I/O, directly testable.
3. **`restore(user)`** — iterate `APPEND_TABLES`, anti-join each snapshot against either the full frame (`READ_TABLES`) or the id-only frame (`document_text`), and set tier 1 to the result.
4. **`pages/_state.py`** — the tier-1 appends store: `appends(table)`, `set_appends(table, df)`, `add_appends(table, rows)`, `clear_appends()`, `pending_counts()`, `discard_session()`. On first access it runs `restore(user)`. **Session state must never contain committed rows** — assert that in a test if it helps.
5. **The restore dialog** — `@st.dialog`, shown once per browser session behind a flag, reporting counts per table with Continue and Discard.
6. **Hand-build a snapshot** to test against: write a parquet with two synthetic `document_log` rows to `@sessions/{you}/`, reload, confirm restore and the dialog.

### Acceptance
With a hand-written snapshot present: the app restores it unasked, the dialog reports two rows, the list shows them marked pending. Inspect `st.session_state` on the diagnostics page — it holds **two rows for `document_log`, not two plus the committed ones**. Insert one of those exact `event_id` values into `document_log` from a worksheet and reload: that row shows committed, the other still pending, dialog reports one, session state holds one row. Click Discard: `@sessions/{you}/` is empty, session state is empty, only committed rows remain.

### Watch out
`overwrite=True` on `put_stream`. Without it `PUT` silently skips an existing file, so a save appears to succeed while changing nothing — the worst failure shape in the design, because the user has just been told their work is safe.

Second: resist storing the composed frame. It is tempting to cache `view()`'s output in `st.session_state` to save a concat — that reintroduces every staleness bug this design avoids, because the stored copy does not move when the token does. Recompute per rerun; the concat is sub-millisecond.

---

## Phase 3 — The two-stage write (1 day)

### Work

1. **`core/write.py`**:
   - `event_row(actor, **fields)` — stamps `event_id` (uuid4) and `event_ts`.
   - `append(table, rows)` — validates columns against a per-table list, then `add_appends(table, rows)`. No `_pending` set here; `view()` adds it.
   - `flush(user)` — per table in `APPEND_TABLES`, insert the appends frame as-is via `session.create_dataframe(...).write.save_as_table(table, mode="append")`; then `remove_snapshots(user)`; then `clear_appends()`. **Insert, remove, clear — in that order.**
2. **`save_to_session(user)`** — write each non-empty appends frame to parquet. No filtering or column dropping: tier 1 already is exactly what gets persisted.
3. **Sidebar UI, visible on every page** — pending counts, **Save to session**, **Save to database**, **Discard changes**.
4. **A throwaway test page** that appends a synthetic row, saves to session, then saves to database.

### Acceptance
Append two rows → visible, marked pending, nothing in Snowflake, nothing in `@sessions`. Save to session → parquet appears, rows still pending. Close the browser entirely, reopen → rows restored, dialog shown. Save to database → log rows appear, `@sessions/{you}/` is gone, rows show committed. Then: append, save to session, and while that snapshot exists have a second user flush their own work — your pending rows survive and their commits appear.

### Watch out
Column-set drift between the working frame and the table. Validate in `append()`, loudly, not at flush time.

Test the interrupted flush explicitly: insert succeeds, `REMOVE` fails. The next entry's anti-join must find zero pending rows and heal silently. Simulate by calling the two halves separately.

---

## Phase 4 — Upload, extraction, document list (2 days)

Text extraction lives here rather than in a phase of its own, because it is one inline call.

### Work

1. **`core/files.py`** — `store_upload(uploaded, document_id, user) -> list[dict]` per §9.8: `put_stream` to `@doc_files`, hash with `hashlib` while reading, then call `AI_PARSE_DOCUMENT` inline and return **two** rows — one `document_file_log`, one `document_text`.
2. **`pages/upload.py`** — `st.file_uploader(accept_multiple_files=True)`, document-type select, that type's metadata fields. On submit, per file: upload and extract behind a spinner, then append the `document_log` create row, the file row, the text row, and the metadata rows.
3. **`pages/documents.py`** — list from the reduced `view()`, filters (text, type, tags) in polars, `st.dataframe` with row selection, pending rows visually distinct.
4. Document-type and metadata-type admin pages, both append-only.

### Acceptance
Upload three PDFs with metadata. Before any save: files in the stage (`LIST @doc_files`), nothing in the logs, all three listed as pending, and the extracted text visible from session state. Save to session, close the browser, reopen: all three restored **including their text**. Save to database: log rows appear in all four tables. Alternatively discard: staged files remain orphaned, logs stay empty.

Upload a born-digital PDF and a scan; both produce text. Upload a corrupt file; the upload reports the failure and the document is not created.

### Watch out
Pre-flight item 5 covers the `AI_PARSE_DOCUMENT` call form — get that working standalone before wiring it into the upload path.

Extraction adds seconds to the upload. Use `st.spinner` with per-file progress, and handle the Cortex call failing without losing the file that was already staged: decide explicitly whether to append the file row with no text, or to abort. Appending with empty text and letting the user retry extraction is the kinder option.

The bytes-durable-before-the-log asymmetry is intentional but surprising. Put it in the UI copy: *files upload immediately; entries are saved when you click Save.*

---

## Phase 5 — Document detail (1 day)

### Work

1. **`pages/document.py`** reading `st.query_params["doc"]`, tabs: Properties, Metadata, Tags, Files, Text, History.
2. All tabs read the in-memory frames — no queries, except Text, which loads one document's content on demand.
3. History is the unreduced `document_log` filtered to this `document_id`, sorted by time, pending events marked. That tab is free.
4. Edits (`st.dialog`) build a **full snapshot** row from the current record plus the change, then `append`.
5. File download via `session.file.get_stream(stage_path)` into `st.download_button`.
6. Tag attach/detach appends `tag_assignment_log` rows with `assigned` true/false.

### Acceptance
Edit a label, save to session: list and detail show the new value, History shows a pending `update`. Save to database: History shows two committed events with the right actor — and the actor is the *viewer*, not the app owner. Attach then detach a tag, flush: three `tag_assignment_log` rows and the tag not attached.

### Watch out
Namespace widget keys by `document_id` (`f"doc:{doc_id}:label"`) or switching documents shows the previous one's draft. The single most common Streamlit bug in this kind of app.

---

## Phase 6 — Search (0.5 day)

### Work

1. **`core/search.py`** — `search_ids(query)` per §9.9, joining `document_text` to `document_file_log`, parameterised.
2. **`pages/search.py`** — query box; ids from Snowflake; `pl.col("document_id").is_in(ids)` against the frame from `view()`; render with the list component from Phase 4.
3. Combine with type and tag filters in polars, after the id filter.

### Acceptance
Find a word occurring only in a scanned document's OCR text. Combine with a type filter and get the intersection. An empty query returns everything without touching Snowflake.

### Watch out
Never interpolate user input into SQL — use `params=[...]`.

A document whose text is still in tier 1 or 2 is not searchable, because its `document_text` row is not yet in the database. Show that state rather than letting it read as a bug — "not yet saved, not yet searchable" is a fine label.

---

## Phase 7 — Trash, tags, admin (1 day)

### Work

1. **Trash and restore as appends** — `op='trash'` / `op='restore'`; the reduction filters on the last op, and a Trash page flips that filter.
2. **Tag admin** — create and edit via `tag_log`; "delete" appends `active=false`.
3. **Admin page** — pending counts for the current user; `list_snapshots()` showing every user's folder with row counts and age; raw vs. reduced row counts; stage sizes; an orphaned-file count (stage directory anti-joined with `document_file_log`) shown as a number, with a manual cleanup button rather than a scheduled job.
4. **The audit page** — read `audit_v` directly. That is the entire audit feature.

### Acceptance
Trash a document and flush: it leaves the list, appears in Trash. Restore and flush: it returns. `audit_v` shows both events with the right actor. The orphan count is non-zero after a discarded upload, and the cleanup button clears it. The snapshot list shows another user's pending folder.

---

## Phase 8 — Hardening (1 day)

### Work

1. **Unsaved-work discipline** — sidebar indicator always visible, warning before navigation, pending rows distinct in every list.
2. **Housekeeping, manual for now** — buttons on the admin page for the two cleanups: remove orphaned staged files, remove snapshot folders older than N days. Scheduled tasks are the right answer once volumes are real; a button is the right answer today.
3. **Memory watch** — the diagnostics page, with a documented threshold at which `load_log` switches to a server-side reduced view (§9.10).
4. **Cost page** — compute-pool and warehouse credit usage, so the monthly figure is observed rather than assumed.
5. **README** — the four defining decisions, what is deliberately absent (§9.1), the six pre-flight results, and the three caveats users will hit: last-writer-wins, multi-tab overwrite, unsaved work living only in `@sessions`.
6. **Measure the probe with real volumes.** Above ~300 ms per rerun, apply step 1 of the §9.6 ladder.

### Acceptance
The cost page shows a plausible monthly figure. The README answers "why can't I delete anything?" and "where did my unsaved changes go?" before either is asked.

---

## Sequencing

| Phase | Days | Deliverable | Shippable? |
|---|---:|---|---|
| 0 Schema + skeleton + pre-flight | 0.5 | Tables exist, app runs, assumptions verified | no |
| 1 Read path | 1.0 | Tokens, cached polars loaders, `view()` | no |
| 2 Snapshots + merge | 1.5 | Durable session state, restore dialog | no |
| 3 Two-stage write | 1.0 | Append, save to session, save to DB | no |
| 4 Upload + extraction + list | 2.0 | **Documents in, with text, listed** | **yes — minimal** |
| 5 Detail | 1.0 | Edit, history, download | yes |
| 6 Search | 0.5 | **Full-text over OCR** | **yes — the product** |
| 7 Trash + admin | 1.0 | Lifecycle, audit, housekeeping | yes |
| 8 Hardening | 1.0 | Cost, docs, memory watch | yes — production |
| | **9.5** | | |

Two release points: **after Phase 4** you can put it in front of users to validate the interaction model; **after Phase 6** it does the thing that justifies the project.

Phases 0–3 are infrastructure with nothing a user would recognise — 4 of 9.5 days. Resist building a list view early to have something to show. Retrofitting the appends overlay into pages written without it is the one piece of rework that would genuinely cost days, because every page's data access goes through `view()`.

---

## Testing

`core/` is plain Python. `core/snapshot.py`, `clean_restored()`, `latest()` and `view()` take plain arguments and return plain frames, so the important logic is testable without Snowflake or Streamlit.

**Do test:**

- `clean_restored()` — snapshot rows absent from the DB survive; rows present drop out; empty snapshot and empty DB both behave; the result contains no committed rows.
- `view()` — composition is correct, `_pending` set on exactly the appended rows, an empty appends frame returns the committed frame unchanged.
- `latest()` — reduction, including the `(event_ts, event_id)` tie-break with duplicate timestamps, and trash-then-restore ordering.
- Idempotency — flushing the same `event_id` twice yields one logical record after reduction.
- Interrupted flush — insert succeeded, snapshot still present: the next restore finds zero pending.
- `append()` column validation — a row missing a column fails at append.
- Parquet round trip — write then read reproduces the frame including dtypes.
- dtype expectations from `to_arrow()` → `pl.from_arrow()`.
- The layering guard.

**Do not test:** pages, widgets, Cortex output quality. Use the phase acceptance criteria by hand.

Build frames in Python for the reduction and merge tests; no round trip needed, which keeps the suite fast enough to run on every save.

---

## Things that will go wrong

Ranked by likelihood:

1. **`CURRENT_USER()` used instead of `st.user`**, attributing every audit row to the app owner and collapsing all users into one `@sessions/` folder. Catch it by opening the app as two users in Phase 0.
2. **`overwrite=True` forgotten** on `put_stream`, so saves silently do nothing.
3. **Non-deterministic reduction** from a missing tie-break. Symptom: a field flickers between two values across reruns.
4. **Stale widget drafts** across documents. Namespace every key by object id.
5. **Composed frame cached in session state**, reintroducing staleness. Keep tier 1 to appends only; recompute `view()` per rerun.
6. **`document_text` bulk-loaded by accident** — it is in `APPEND_TABLES` but not `READ_TABLES`, and conflating the two lists will pull gigabytes into memory.
7. **Missing PyPI integration**, appearing as an app that will not start.
8. **Cortex call failing mid-upload** after the file is already staged. Decide the behaviour explicitly.
9. **Probe latency** making the UI feel heavy. Measure before tuning.
10. **Log growth** past comfortable memory. Switch to the reduced view.
11. **Multi-tab overwrite** losing one tab's edits. Accept and document.
12. **Concurrent whole-record clobber.** Accept and document, or move to per-field deltas.

---

## When this architecture stops fitting

- Different departments must not see each other's documents → needs real per-object access control, for which there is no room here.
- More than ~20 active users, or in-memory frames past a few hundred MB even after the reduced view.
- **Batch ingestion** — a watched folder, a mail drop, a bulk import. Interactive upload with inline extraction is wrong for that; a Stream on `document_file_log` plus a scheduled Task calling `AI_PARSE_DOCUMENT` is the right pattern, and this is the case it was designed for.
- Users need page-level viewing and annotation.
- Approval workflows with more than one step.
- Another system must push documents in.

The last one is cheap: a FastAPI service importing `core/` with its own Snowflake connection and identity source, which is why `core/session.py` isolates both. The others mean a different application.
