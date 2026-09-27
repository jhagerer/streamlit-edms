# 9 — Architecture: "MiniDMS" on Streamlit on Compute Pools

A deliberately small document management system, native to Snowflake. Supersedes the earlier SQLite design; §11 remains valid as the background analysis that led here.

**Target:** a team of 5–20 people, tens of thousands of documents, one Snowflake account.
**Non-target:** multi-tenant SaaS, per-object access control, workflow compliance, hundreds of concurrent users.

Four decisions define this architecture; everything else follows from them:

1. **All reads are bulk.** Each view loads whole tables into in-memory polars frames; the UI never queries per interaction.
2. **All writes are appends.** No `UPDATE`, no `DELETE`. Current state is a reduction over an event log.
3. **Session state is durable.** The user's appends are persisted as parquet on a stage, per user, and restored automatically on return.
4. **Writing has two stages.** *Save to session* writes the appends to the stage; *save to database* merges them into Snowflake.

---

## 9.1 Scope

Seven features: upload, immutable files with checksums, page rendering, full-text search over extracted text, document types with typed metadata, tags, audit trail.

Deliberately out: version/page indirection, object ACLs, workflows, auto-maintained index trees, cabinets, smart links, signatures, checkouts, duplicates, quotas.

One consequence of decision 2 worth naming up front: **the audit trail is no longer a feature to build.** Every log row carries actor and timestamp, so history is the data. That removes a table, a module and roughly thirty call sites from the earlier design.

---

## 9.2 Runtime: Streamlit on Compute Pools

The app runs as a Streamlit object on the **container runtime**, which executes on a **compute pool**. Stated precisely, because there are three things that get conflated:

| | Used here |
|---|---|
| Streamlit in warehouses (`SYSTEM$WAREHOUSE_RUNTIME`) | **No** |
| Streamlit on a compute pool (`SYSTEM$ST_CONTAINER_RUNTIME_PY3_11`) | **Yes** |
| A hand-rolled Snowpark Container Services `SERVICE` with your own image | **No** |

The middle option is what Snowflake's docs call the container runtime. You do not build or maintain an image.

Two reasons this is a requirement rather than a preference:

- **Shared caching.** On a compute pool all viewers share one server instance, so `@st.cache_data` is a shared cache — one bulk read serves everybody. On warehouse runtime each viewer gets their own instance: ten users mean ten full table loads and no cross-user cache coherence. The entire read design depends on the shared instance, so **verify it empirically** (§10 pre-flight item 6).
- **PyPI access.** Package installation from `requirements.txt` settles polars availability without depending on Snowflake's Anaconda channel.

### Deployment and prerequisites

The app is deployed through **Snowflake Workspaces**. Database, schema and warehouse are assumed to exist already.

Two things to confirm before starting:

1. **The runtime.** After deploying, run `DESC STREAMLIT <name>` and check `RUNTIME_NAME` is `SYSTEM$ST_CONTAINER_RUNTIME_PY3_11` and that a compute pool is attached. The account default is shifting toward container runtime, but the two runtimes differ on exactly what this design depends on, so verify rather than assume — and set it explicitly if the deployment path did not.
2. **PyPI egress.** The container runtime cannot install polars without an external access integration. If your account already has one for PyPI, reference it. Otherwise it needs creating once by `ACCOUNTADMIN`:

```sql
CREATE NETWORK RULE pypi_rule MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = ('pypi.org', 'files.pythonhosted.org');
CREATE EXTERNAL ACCESS INTEGRATION pypi_eai
  ALLOWED_NETWORK_RULES = (pypi_rule) ENABLED = TRUE;
```

This is the one prerequisite that will block startup outright, so check it first.

```
# requirements.txt — pin these; the reduction leans on version-sensitive API
polars==1.*
pyarrow>=15
```

Everything else the app needs — two stages, eight tables, one view — is in `minidms-setup.sql`, run once in your schema.

| Concern | Choice |
|---|---|
| Database | Snowflake **standard** tables |
| Document files | Internal stage `@doc_files` |
| Session state | Internal stage `@sessions`, parquet |
| Text extraction / OCR | `AI_PARSE_DOCUMENT`, called inline by the app |
| Search | `SEARCH()` or Cortex Search |
| In-memory data | **polars**, via `to_arrow()` |
| Identity | `st.user` (see §9.3) |

### Why standard tables, not hybrid

Three independent reasons pointing the same way: append-only means there are no point updates to optimise; reads are full scans of modest tables, which is what columnar storage is for; and **Snowflake's result cache excludes hybrid table queries**, so standard tables keep that cache available as a second layer beneath `@st.cache_data`.

---

## 9.3 Identity and privileges

Use **`st.user`**, not `CURRENT_USER()`.

```python
import streamlit as st

def actor() -> str:
    return st.user.user_name          # Snowflake username of the viewer
```

This matters and is easy to get wrong. Snowflake's guidance is explicit: `CURRENT_USER()` returns the username of the *session owner*, and an app running with owner's rights — the default — makes that whoever deployed the app, **not the person looking at the screen**. Using it would attribute every audit row to the deployer and give every user the same `@sessions/` folder.

`st.user` returns the viewer correctly under both owner's rights and caller's rights. Two attributes are available: `user_name` and `email`. Use `user_name` for the `actor` column and the session folder.

There is no login gate to write. In SiS the viewer is authenticated by Snowflake before the app runs; `st.user.is_logged_in` belongs to the OIDC flow configured through `secrets.toml` and is not the mechanism here.

### Privileges

Because the app runs with owner's rights, this is short:

- **The owning role** needs the privileges the app exercises: `READ`/`WRITE` on `@doc_files` and `@sessions`, `SELECT`/`INSERT` on the tables, `USAGE` on the warehouse, and the `SNOWFLAKE.CORTEX_USER` database role for `AI_PARSE_DOCUMENT`.
- **Viewers** need `USAGE` on the app and its containing database and schema. No table or stage grants at all — they never query these objects themselves.

Withholding `UPDATE` and `DELETE` from the owning role is worth doing deliberately: it makes append-only something the database enforces rather than a convention the code follows.

---

## 9.4 Three tiers of state

The mental model to hold. Each tier has a different lifetime and sharing scope.

```
┌──────────────────────────────────────────────────────────────────┐
│ Tier 1 — appended rows only (st.session_state["appends"])         │
│          volatile · this browser session · kilobytes             │
│          THIS session's appends. Committed rows are never copied. │
└───────────────┬──────────────────────────────────────────────────┘
                │  "Save to session"          ▲ restore on entry
                ▼                             │
┌──────────────────────────────────────────────────────────────────┐
│ Tier 2 — @sessions/{user}/{table}.parquet                        │
│          durable · one folder per user · survives session death   │
│          byte-for-byte the same rows as tier 1                    │
└───────────────┬──────────────────────────────────────────────────┘
                │  "Save to database"         ▲ bulk load (cached)
                ▼                             │
┌──────────────────────────────────────────────────────────────────┐
│ Tier 3 — Snowflake log tables                                    │
│          committed · shared by everyone · the system of record    │
└──────────────────────────────────────────────────────────────────┘

        What the UI renders is tier 3 (cached, shared)
        concatenated with tier 1 (small, per-session), computed
        per rerun and never stored.
```

Tiers 1 and 2 hold **exactly the same rows** — this session's appends and nothing else. That equivalence is the point: saving to session is a straight serialise, restoring is a straight deserialise, and neither has to filter or reconcile anything.

Committed rows live in one place only: the shared, cached tier-3 frames. They are never duplicated into a session. So a user's session footprint is kilobytes regardless of corpus size, and there is no way for a session to hold a stale copy of committed data — because it holds no copy at all.

Tier 2 removes the earlier design's worst limitation: unsaved work no longer dies with the session. A browser crash, a container restart, a suspension — the user returns and their work is there.

---

## 9.5 Schema

Eight tables and one view; full DDL in `minidms-setup.sql`. Every table is an append-only event log carrying `event_id`, `event_ts`, `actor`; current state is the latest row per reduction key.

| Table | Reduction key | Notes |
|---|---|---|
| `document_log` | `document_id` | `op` ∈ create / update / trash / restore |
| `document_file_log` | `file_id` | filename, `stage_path`, mimetype, size, checksum, page_count |
| `metadata_log` | `(document_id, metadata_type_id)` | empty `value` = removed |
| `tag_assignment_log` | `(document_id, tag_id)` | `assigned = FALSE` = detached |
| `document_type_log` | `document_type_id` | definitions; `active` |
| `metadata_type_log` | `metadata_type_id` | definitions; name, label, data_type, choices, default |
| `tag_log` | `tag_id` | definitions; label, color, `active` |
| `document_text` | `file_id` | extracted text; **appended but never bulk-loaded** |
| `audit_v` (view) | — | `UNION ALL` over the seven logs |

Every mutation appends **a full snapshot** of the record, not a delta. The reduction stays a one-liner and the UI already holds the current record in memory to build the next snapshot from. Redundancy is irrelevant under columnar compression at this scale.

No primary keys or unique constraints — standard tables do not enforce them and append-only does not need them. Client-generated `event_id` provides idempotency: a replayed flush produces identical rows and the reduction dedupes.

**State columns differ by entity, deliberately.** `document_log` carries `op` because it has four states; everything else carries a boolean — `active` for definitions and files, `assigned` for tag links — and metadata uses an empty `value`. Each matches its entity's semantics rather than forcing one convention.

Trash appends `op='trash'`; restore appends `op='restore'`. Tag detach appends `assigned=false`. Nothing is ever removed.

### Two table lists, not one

`document_text` is written by the app like any other log, but must never be pulled into memory — it is the column that turns megabytes into gigabytes. So the plumbing needs two lists:

```python
# Bulk-loaded into memory, reduced in polars, rendered.
READ_TABLES = (
    "document_log", "document_file_log", "metadata_log",
    "tag_assignment_log", "document_type_log", "metadata_type_log", "tag_log",
)

# Appended to, persisted to parquet, flushed. Superset of READ_TABLES.
APPEND_TABLES = READ_TABLES + ("document_text",)
```

`change_tokens()` probes all of `APPEND_TABLES`; `load_log()` bulk-loads only `READ_TABLES`; `document_text` gets an id-only loader used by the restore anti-join (§9.7).

**The trade-off to state in the UI: last writer wins on the whole record.** Each append is a full snapshot, so if A edits a label and B edits the description of the same document, whoever flushes second overwrites the other's field. Acceptable for a small team; if it bites, the fix is per-field delta rows with a forward-fill in the reduction, not locking.

---

## 9.6 The read path

One probe per rerun, one cached load per token value.

```python
import io, polars as pl, streamlit as st
from snowflake.snowpark.context import get_active_session

session = get_active_session()

def change_tokens() -> dict[str, int]:
    """One round trip, all tokens. Deliberately NOT cached: the UI is always
    consistent with the database as of this rerun."""
    cols = ", ".join(
        f"SYSTEM$LAST_CHANGE_COMMIT_TIME('{t}') AS {t}" for t in APPEND_TABLES
    )
    row = session.sql(f"SELECT {cols}").collect()[0]
    return {t: row[t.upper()] for t in APPEND_TABLES}

@st.cache_data(show_spinner=False)
def load_log(table: str, token: int) -> pl.DataFrame:
    """Full rows. READ_TABLES only. token is a cache key, never in the query."""
    return pl.from_arrow(session.sql(f"SELECT * FROM {table}").to_arrow())

@st.cache_data(show_spinner=False)
def load_event_ids(table: str, token: int) -> pl.DataFrame:
    """Ids only — for tables too large to bulk-load (document_text)."""
    return pl.from_arrow(
        session.sql(f"SELECT event_id FROM {table}").to_arrow()
    )
```

The token is a cache-key argument, so there is no invalidation logic anywhere in the app. When a table changes its token changes, the call misses, the frame reloads.

### Cost of the uncached probe

Leaving `change_tokens()` undecorated is a deliberate trade: **one small round trip per rerun in exchange for zero staleness.** Streamlit reruns on every widget interaction, so a user typing in a filter issues one probe per keystroke, roughly 50–150 ms against a single-row metadata function.

This is the knob to turn if the UI feels heavy. In order: wrap the probe in `@st.cache_data(ttl=5)`; move it into an `st.fragment(run_every="10s")`; debounce the filter widgets. Start undecorated — *the screen always shows current database state* is worth real latency and removes a whole class of stale-data bug.

### Reduction to current state

```python
def latest(log: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    return (
        log.sort(["event_ts", "event_id"])
           .unique(subset=keys, keep="last", maintain_order=True)
    )
```

Sorting by `(event_ts, event_id)` rather than timestamp alone matters: one flush writes several rows with the same timestamp, and without the tie-break the reduction is non-deterministic.

### What the UI actually reads

`view()` is the single entry point for every page. It composes the shared committed frame with this session's appends, per rerun, and stores nothing.

```python
def view(table: str, appends: pl.DataFrame) -> pl.DataFrame:
    """Committed rows (cached, shared) + this session's appends (tier 1).
    Takes appends as an argument so core/ never imports from pages/."""
    db = load_log(table, change_tokens()[table]).with_columns(
        pl.lit(False).alias("_pending")
    )
    if not appends.height:
        return db
    return pl.concat(
        [db, appends.with_columns(pl.lit(True).alias("_pending"))],
        how="vertical_relaxed",
    )
```

Concatenating a cached 150k-row frame with a handful of appended rows costs well under a millisecond in polars, so recomputing per rerun is cheaper than the bookkeeping required to cache it — and it makes staleness structurally impossible. The committed half always comes from the token-keyed loader; the appended half is a separate, tiny frame. The two cannot drift apart because they are never merged into a stored object.

`_pending` is a render-time annotation added here and nowhere else. It is never written to parquet and never written to Snowflake.

Everything downstream — filtering, sorting, faceting, paging, joining documents to tags — is polars over the frame `view()` returns. No SQL.

---

## 9.7 Session persistence and merge

### Restore on entry, unasked

On open, the app restores the user's parquet snapshot without asking, loads current database state, and keeps only the rows that are still uncommitted.

```python
def session_path(user: str, table: str) -> str:
    return f"@sessions/{slug(user)}/{table}.parquet"

def read_snapshot(user: str, table: str) -> pl.DataFrame | None:
    try:
        return pl.read_parquet(session.file.get_stream(session_path(user, table)))
    except Exception:
        return None            # no snapshot for this user/table

def clean_restored(snap: pl.DataFrame | None, committed_ids: pl.DataFrame) -> pl.DataFrame:
    """Drop staged rows that are already committed. Pure: no I/O, testable."""
    if snap is None or not snap.height:
        return pl.DataFrame()
    return snap.join(committed_ids.select("event_id"), on="event_id", how="anti")

def restore(user: str) -> dict[str, int]:
    """Populate tier 1 from tier 2, minus anything already in tier 3."""
    tokens, restored = change_tokens(), {}
    for table in APPEND_TABLES:
        committed = (
            load_log(table, tokens[table]) if table in READ_TABLES
            else load_event_ids(table, tokens[table])
        )
        mine = clean_restored(read_snapshot(user, table), committed)
        set_appends(table, mine)              # tier 1 := appends only
        restored[table] = mine.height
    return restored
```

**The anti-join on `event_id` is the whole mechanism.** Because every event carries a client-generated UUID and nothing is ever updated, "which of my staged rows are still uncommitted" is answerable by set difference. No bookkeeping, no watermark, no sequence numbers. This is the second time append-only pays for itself.

Note what `restore()` puts into tier 1: the cleaned appends and nothing else. It does not build a merged frame. Composition with committed data happens later and repeatedly, in `view()`.

### The restore popup

```python
@st.dialog("Previous session restored")
def restore_notice(restored: dict[str, int]):
    st.success(f"{sum(restored.values())} unsaved change(s) restored from your last session.")
    for table, n in restored.items():
        if n:
            st.write(f"- {table}: {n} row(s)")
    st.button("Continue")
    if st.button("Discard restored changes", type="secondary"):
        discard_session()
        st.rerun()
```

Shown once per browser session behind a flag in `st.session_state`. The restore already happened — the dialog reports it rather than asking permission — but it offers a discard path, because a snapshot abandoned three weeks ago is sometimes not what someone wants back.

### Save to session

```python
def write_snapshot(user: str, table: str, df: pl.DataFrame) -> None:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    buf.seek(0)
    session.file.put_stream(
        buf, session_path(user, table), auto_compress=False, overwrite=True
    )

def save_to_session(user: str) -> None:
    for table in APPEND_TABLES:
        mine = appends(table)
        if mine.height:
            write_snapshot(user, table, mine)
```

No filtering, no column dropping — tier 1 already *is* the set of rows to persist. That is the payoff of holding only appends in session state.

`overwrite=True` is essential — `PUT` silently skips an existing file without it, which would look like a save that did nothing.

### Save to database

```python
def save_to_database(user: str) -> None:
    for table in APPEND_TABLES:
        mine = appends(table)
        if not mine.height:
            continue
        session.create_dataframe(
            mine.rows(), schema=list(mine.columns)
        ).write.save_as_table(table, mode="append")
    session.sql(f"REMOVE @sessions/{slug(user)}/").collect()   # own folder only
    clear_appends()                                            # tier 1 := empty
```

Again no filtering: the appends frame is inserted as-is.

Insert first, remove second, clear last. If the `REMOVE` fails after the inserts succeeded, the snapshot survives — and the next entry's anti-join finds zero uncommitted rows, because everything in it is now in the database. The failure heals itself. Flushing the same snapshot twice is equally harmless: identical `event_id` values reduce to one logical record.

After the flush, tier 1 is empty and the tokens have moved, so `view()` returns purely committed rows on the next rerun and the user sees their work as saved.

### What "all user states become invalidated" means here

A flush touches only the flushing user's folder. Every *other* user's snapshot is now stale relative to the new baseline — and that resolves itself with no action: on their next interaction the anti-join runs against the updated database, rows that someone else's flush happened to cover drop out of their pending set, and their own un-flushed appends survive. Invalidation is a property of the merge, not an operation anyone performs.

Deleting other users' folders on a flush would destroy their drafts, so the flush is deliberately scoped to `@sessions/{me}/`.

---

## 9.8 Files and text extraction

Uploads go to a stage, and the text is extracted in the same step.

```python
def store_upload(uploaded, document_id: str, user: str) -> list[dict]:
    file_id    = str(uuid.uuid4())
    stage_path = f"@doc_files/{document_id}/{file_id}"
    session.file.put_stream(uploaded, stage_path, auto_compress=False)

    # Cortex, inline. A few seconds behind a spinner.
    text = session.sql(
        "SELECT AI_PARSE_DOCUMENT(TO_FILE('@doc_files', ?), {'mode': 'OCR'})"
        ":content::VARCHAR",
        params=[f"{document_id}/{file_id}"],
    ).collect()[0][0]

    return [
        event_row(user, table="document_file_log", file_id=file_id,
                  document_id=document_id, stage_path=stage_path, ...),
        event_row(user, table="document_text", file_id=file_id, content=text),
    ]
```

**Extraction is synchronous and in-app.** `AI_PARSE_DOCUMENT` is an ordinary SQL function; calling it at upload takes a few seconds for a typical document, which is acceptable behind a spinner in a flow the user is already waiting on. The text then becomes an ordinary `document_text` append and flows through the same two-stage write as everything else.

This is a deliberate reversal of an earlier draft that used a Stream on `document_file_log` plus a scheduled Task. That decoupling bought little — uploads are interactive and one at a time — and cost a stream, a task, an `EXECUTE TASK` grant, task monitoring, and a confusing gap where extraction only fired after *save to database*, so a staged-but-unflushed document appeared to have no text for no visible reason. Inline is simpler and more consistent.

If batch ingestion ever arrives, the Stream + Task pattern is the right answer for it — but it is not the right answer for a person clicking Upload.

### The one asymmetry

**File bytes land in the stage immediately, at upload — not at flush time.** Only the log rows wait. So discarding a session leaves an orphaned staged file with no `document_file_log` row.

That is the right trade: an orphan costs fractions of a cent, a lost upload costs a user's afternoon. It does mean the UI should say *files upload immediately; entries are saved when you click Save.*

Cleaning orphans is a query against the stage directory table anti-joined with `document_file_log`, plus a `REMOVE` loop. Worth doing when the volume becomes real, not before.

### Page rendering

No Cortex equivalent. Either drop the viewer and rely on extracted text plus a download link, or add `pymupdf` to `requirements.txt` and rasterise in-app with `@st.cache_data`. Recommend dropping for v1.

---

## 9.9 Search

`document_text` is deliberately excluded from the bulk read, so search is the one read that stays server-side:

```python
def search_ids(query: str) -> list[str]:
    rows = session.sql(
        "SELECT DISTINCT f.document_id FROM document_text t "
        "JOIN document_file_log f ON f.file_id = t.file_id "
        "WHERE SEARCH(t.content, ?)", params=[query]
    ).collect()
    return [r[0] for r in rows]
```

Snowflake matches, polars filters and renders. Swap `SEARCH()` for Cortex Search if semantic retrieval matters more than keyword matching.

Note that a document whose text is still in tier 1 or 2 is not yet searchable — its `document_text` row is not in the database. Make that visible rather than letting it read as a bug.

---

## 9.10 Memory and the ceiling

| Frame | Rows at 50k documents | Approx. in memory |
|---|---|---|
| `document_log` | 50k–150k | 20–60 MB |
| `metadata_log` | 150k–500k | 30–80 MB |
| `tag_assignment_log` | 50k–150k | 5–15 MB |
| `document_file_log` | 50k–80k | 15–25 MB |
| `document_text` | **not loaded** | — |

Shared across viewers on a compute pool, so this is **per-server, not per-user** — and because tier 1 holds only appends, adding a tenth concurrent user costs kilobytes rather than another copy of every frame. Parquet snapshots hold the same small set and stay in the kilobytes too.

The ceiling is log *growth*, driven by edits rather than document count. Two mitigations in order:

1. **Load a reduced view.** A Snowflake view applying `QUALIFY ROW_NUMBER() OVER (PARTITION BY … ORDER BY event_ts DESC, event_id DESC) = 1` so the app receives current state only. Caps memory at one row per entity and moves history to a detail-view query. Do this once raw logs pass a few hundred MB.
2. **Periodic compaction.** Rewriting each log as its reduced form plus an archive. Breaks append-only purity — an operational escape hatch, not part of the design.

---

## 9.11 Layering

`core/` must not import Streamlit, except `core/session.py`.

```
app.py                 # st.navigation, restore-on-entry
pages/                 # Streamlit only — widgets, layout, dialogs
  _state.py            # tier-1 appends dict, restore dialog
core/
  session.py           # get_active_session, actor(), slug(), token probe
  read.py              # cached loaders, latest(), view(), clean_restored()
  snapshot.py          # parquet read/write/remove on @sessions
  write.py             # append builders, flush to log tables
  files.py             # @doc_files upload + inline AI_PARSE_DOCUMENT
  search.py            # server-side match
```

`core/session.py` is the one module that reads `st.user`, and it exposes `actor()` as a plain string. Everything below it — including `core/snapshot.py`, which takes a user identifier as an argument — is testable without Streamlit.

`pages/_state.py` is the only module touching `st.session_state`, and it owns the tier-1 appends dict: `appends(table)`, `set_appends(table, df)`, `add_appends(table, rows)`, `clear_appends()`, `pending_counts()`. Functions in `core/` that need appends take them as an argument; `core/` never imports from `pages/`.

---

## 9.12 Honest limits

- **Last writer wins on whole records.** Concurrent edits to different fields of one document clobber. Fixable with per-field deltas.
- **Multiple tabs, one user.** Both tabs write `@sessions/{me}/` with `overwrite=True`, so the later save wins and the other tab's edits are lost. Blast radius is one user; documented rather than solved.
- **One probe per rerun** is a latency cost on every interaction. Tunable (§9.6); the default is deliberately the consistent one.
- **Upload blocks for a few seconds** while Cortex extracts text. Acceptable interactively; wrong for batch ingestion, which would want the Stream + Task pattern instead.
- **Abandoned snapshots and orphaned files accumulate** until something cleans them. Neither is urgent; both are a query plus a `REMOVE` loop when they matter.
- **Logs grow without bound** until the reduced view or compaction is added.
- **No page rendering** in v1.
- **No API.** A FastAPI service sharing `core/` would need its own Snowflake connection and identity source instead of `get_active_session()` and `st.user` — a change confined to `core/session.py`, which is why that module exists.
- **The compute pool is always-on** within its auto-suspend window, so there is a continuous baseline cost. Cheap at roughly 0.06 credits/hour for an XS node, but not zero.
