-- ─────────────────────────────────────────────────────────────────────────────
-- MiniDMS — setup
--
-- Everything the app needs that does not already exist: two stages, eight
-- tables, one view. Run once in the database and schema you already use.
--
-- Assumes: database, schema and warehouse exist; the Streamlit app is deployed
-- via Snowflake Workspaces.
--
-- Idempotent. Tables use IF NOT EXISTS so re-running never drops data; the view
-- uses OR REPLACE because it holds no state.
--
-- One prerequisite outside this file: the container runtime needs an external
-- access integration to install polars from PyPI. Reference an existing one if
-- your account has it; otherwise it needs creating once by ACCOUNTADMIN:
--
--   CREATE NETWORK RULE pypi_rule MODE = EGRESS TYPE = HOST_PORT
--     VALUE_LIST = ('pypi.org', 'files.pythonhosted.org');
--   CREATE EXTERNAL ACCESS INTEGRATION pypi_eai
--     ALLOWED_NETWORK_RULES = (pypi_rule) ENABLED = TRUE;
-- ─────────────────────────────────────────────────────────────────────────────

-- USE DATABASE <yours>;
-- USE SCHEMA   <yours>;

-- ── Stages ───────────────────────────────────────────────────────────────────

-- Uploaded documents, written once and never modified.
CREATE STAGE IF NOT EXISTS doc_files
    DIRECTORY  = (ENABLE = TRUE)
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    COMMENT    = 'Immutable uploaded document files';

-- Tier 2: per-user parquet snapshots of un-flushed appends.
-- Layout: @sessions/{user}/{table}.parquet
CREATE STAGE IF NOT EXISTS sessions
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    COMMENT    = 'Per-user session state (appends only)';

-- ── Tables ───────────────────────────────────────────────────────────────────
--
-- All append-only. No UPDATE, no DELETE. Every mutation appends a full snapshot
-- of the record; current state is the latest row per reduction key, ordered by
-- (event_ts, event_id). No primary keys or unique constraints — standard tables
-- do not enforce them, and client-generated event_id gives idempotency instead.
--
-- State columns differ by entity on purpose: op where there are four states, a
-- boolean where there are two, an empty value for metadata removal.

CREATE TABLE IF NOT EXISTS document_log (
    event_id         VARCHAR       NOT NULL,   -- uuid4, client-generated
    event_ts         TIMESTAMP_NTZ NOT NULL,
    actor            VARCHAR       NOT NULL,   -- st.user.user_name
    op               VARCHAR       NOT NULL,   -- create | update | trash | restore
    document_id      VARCHAR       NOT NULL,   -- uuid4, stable across events
    document_type_id VARCHAR,
    label            VARCHAR,
    description      VARCHAR,
    language         VARCHAR(8)
)   COMMENT = 'Append-only. Reduce by document_id.';

CREATE TABLE IF NOT EXISTS document_file_log (
    event_id    VARCHAR       NOT NULL,
    event_ts    TIMESTAMP_NTZ NOT NULL,
    actor       VARCHAR       NOT NULL,
    active      BOOLEAN       NOT NULL DEFAULT TRUE,
    document_id VARCHAR       NOT NULL,
    file_id     VARCHAR       NOT NULL,        -- uuid4
    filename    VARCHAR,                      -- original upload name
    stage_path  VARCHAR,                      -- '@doc_files/{document_id}/{file_id}'
    mimetype    VARCHAR,
    size        NUMBER,
    checksum    VARCHAR(64),                  -- sha256 hex
    page_count  NUMBER
)   COMMENT = 'Append-only. Reduce by file_id. Bytes live in @doc_files.';

CREATE TABLE IF NOT EXISTS metadata_log (
    event_id         VARCHAR       NOT NULL,
    event_ts         TIMESTAMP_NTZ NOT NULL,
    actor            VARCHAR       NOT NULL,
    document_id      VARCHAR       NOT NULL,
    metadata_type_id VARCHAR       NOT NULL,
    value            VARCHAR                  -- '' or NULL = removed
)   COMMENT = 'Append-only. Reduce by (document_id, metadata_type_id).';

CREATE TABLE IF NOT EXISTS tag_assignment_log (
    event_id    VARCHAR       NOT NULL,
    event_ts    TIMESTAMP_NTZ NOT NULL,
    actor       VARCHAR       NOT NULL,
    document_id VARCHAR       NOT NULL,
    tag_id      VARCHAR       NOT NULL,
    assigned    BOOLEAN       NOT NULL         -- FALSE = detached
)   COMMENT = 'Append-only. Reduce by (document_id, tag_id).';

CREATE TABLE IF NOT EXISTS document_type_log (
    event_id         VARCHAR       NOT NULL,
    event_ts         TIMESTAMP_NTZ NOT NULL,
    actor            VARCHAR       NOT NULL,
    active           BOOLEAN       NOT NULL DEFAULT TRUE,
    document_type_id VARCHAR       NOT NULL,
    label            VARCHAR
)   COMMENT = 'Append-only. Reduce by document_type_id.';

CREATE TABLE IF NOT EXISTS metadata_type_log (
    event_id         VARCHAR       NOT NULL,
    event_ts         TIMESTAMP_NTZ NOT NULL,
    actor            VARCHAR       NOT NULL,
    active           BOOLEAN       NOT NULL DEFAULT TRUE,
    metadata_type_id VARCHAR       NOT NULL,
    name             VARCHAR,                 -- machine name
    label            VARCHAR,
    data_type        VARCHAR,                 -- text | number | date | choice
    choices          VARCHAR,                 -- newline-separated
    default_value    VARCHAR
)   COMMENT = 'Append-only. Reduce by metadata_type_id.';

CREATE TABLE IF NOT EXISTS tag_log (
    event_id VARCHAR       NOT NULL,
    event_ts TIMESTAMP_NTZ NOT NULL,
    actor    VARCHAR       NOT NULL,
    active   BOOLEAN       NOT NULL DEFAULT TRUE,
    tag_id   VARCHAR       NOT NULL,
    label    VARCHAR,
    color    VARCHAR(7)                       -- #rrggbb
)   COMMENT = 'Append-only. Reduce by tag_id.';

-- Extracted text, appended by the app after calling AI_PARSE_DOCUMENT at upload.
-- Kept in its own table so it is never pulled into the bulk read — this is the
-- column that turns megabytes into gigabytes.
CREATE TABLE IF NOT EXISTS document_text (
    event_id     VARCHAR       NOT NULL,
    event_ts     TIMESTAMP_NTZ NOT NULL,
    actor        VARCHAR       NOT NULL,
    file_id      VARCHAR       NOT NULL,
    content      VARCHAR
)   COMMENT = 'Append-only. Reduce by file_id. Searched, never bulk-loaded.';

-- ── Audit ────────────────────────────────────────────────────────────────────
-- Append-only means history is the data, so this view is the entire audit
-- feature. No separate event table, no logging call sites.

CREATE OR REPLACE VIEW audit_v
    COMMENT = 'Unified history across all logs.'
AS
          SELECT event_ts, actor, op,               'document'      AS object_type, document_id      AS object_id FROM document_log
UNION ALL SELECT event_ts, actor, 'file',           'document_file',               file_id                       FROM document_file_log
UNION ALL SELECT event_ts, actor, 'metadata',       'document',                    document_id                   FROM metadata_log
UNION ALL SELECT event_ts, actor, 'tag',            'document',                    document_id                   FROM tag_assignment_log
UNION ALL SELECT event_ts, actor, 'document_type',  'document_type',               document_type_id              FROM document_type_log
UNION ALL SELECT event_ts, actor, 'metadata_type',  'metadata_type',               metadata_type_id              FROM metadata_type_log
UNION ALL SELECT event_ts, actor, 'tag_definition', 'tag',                         tag_id                        FROM tag_log;

-- ── Later, if needed ─────────────────────────────────────────────────────────
-- Once the larger logs pass a few million rows:
--   ALTER TABLE document_log       CLUSTER BY (document_id);
--   ALTER TABLE metadata_log       CLUSTER BY (document_id);
--   ALTER TABLE tag_assignment_log CLUSTER BY (document_id);
--
-- Housekeeping, when it starts to matter rather than now:
--   · staged files with no document_file_log row (discarded sessions)
--   · @sessions folders whose owner never came back
-- Both are a query plus a REMOVE loop; neither needs a task until the volumes
-- are real.
