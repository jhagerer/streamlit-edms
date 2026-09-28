-- ─────────────────────────────────────────────────────────────────────────────
-- MiniDMS — setup
--
-- Everything the app needs that does not already exist: two stages, nine
-- tables, two views, and the text-extraction pipeline (one stream, one
-- procedure, one triggered task). Run once in the database and schema you
-- already use.
--
-- Assumes: database, schema and warehouse exist; the Streamlit app is deployed
-- via Snowflake Workspaces.
--
-- Idempotent. Tables and the stream use IF NOT EXISTS so re-running never
-- drops data or resets the stream offset; views, the procedure and the task
-- use OR REPLACE because they hold no state (re-running suspends the task
-- again, and the ALTER TASK … RESUME at the end starts it).
--
-- Privileges the owning role needs for the pipeline: CREATE STREAM / TASK /
-- PROCEDURE on the schema, EXECUTE TASK and EXECUTE MANAGED TASK on the
-- account (serverless task), and the SNOWFLAKE.CORTEX_USER database role.
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
-- Layout: @doc_files/{sha256 of the content}/{filename}. Content-addressed:
-- the same bytes uploaded under the same name land on the same path. The
-- extension stays on the name because AI_PARSE_DOCUMENT detects the format
-- from it.
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
    stage_path  VARCHAR,                      -- '@doc_files/{checksum}/{filename}'
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

-- Extracted text, appended by extract_text() (the triggered task below) after
-- the document_file_log rows are saved. Kept in its own table so it is never
-- pulled into the bulk read — this is the column that turns megabytes into
-- gigabytes.
CREATE TABLE IF NOT EXISTS document_text (
    event_id     VARCHAR       NOT NULL,
    event_ts     TIMESTAMP_NTZ NOT NULL,
    actor        VARCHAR       NOT NULL,   -- 'system:ocr'
    file_id      VARCHAR       NOT NULL,
    content      VARCHAR
)   COMMENT = 'Append-only. Reduce by file_id. Searched, never bulk-loaded.';

-- Text-extraction status per file: queued -> done | failed | skipped.
-- Written only by extract_text(). The latest row per file_id is the status.
CREATE TABLE IF NOT EXISTS ocr_log (
    event_id    VARCHAR       NOT NULL,
    event_ts    TIMESTAMP_NTZ NOT NULL,
    actor       VARCHAR       NOT NULL,    -- 'system:ocr'
    file_id     VARCHAR       NOT NULL,
    stage_path  VARCHAR,
    status      VARCHAR       NOT NULL,    -- queued | done | failed | skipped
    page_count  NUMBER,                    -- from AI_PARSE_DOCUMENT, when done
    message     VARCHAR                    -- error text, when failed/skipped
)   COMMENT = 'Append-only. Reduce by file_id. Text extraction status.';

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
UNION ALL SELECT event_ts, actor, 'tag_definition', 'tag',                         tag_id                        FROM tag_log
UNION ALL SELECT event_ts, actor, 'ocr_' || status, 'document_file',               file_id                       FROM ocr_log;

-- ── Text extraction: stream + procedure + triggered task ─────────────────────
--
-- Saving to the database appends document_file_log rows. The stream sees them,
-- which fires the triggered task, which calls extract_text():
--   1. consume the stream: queue every new active file that has no text yet
--      (this INSERT is what advances the stream offset);
--   2. per queued file: reuse the text of an identical file (same checksum)
--      or call AI_PARSE_DOCUMENT on the staged file, then append the text to
--      document_text and 'done' to ocr_log. One failing file never blocks the
--      others: it gets a 'failed' row with the error message.
-- "Retry" in the app appends a fresh snapshot of the file row, which goes
-- through the same stream.

CREATE STREAM IF NOT EXISTS document_file_log_stream
    ON TABLE document_file_log
    APPEND_ONLY = TRUE
    COMMENT = 'New document_file_log rows, consumed by extract_text()';

-- Current extraction status per file.
CREATE OR REPLACE VIEW ocr_status_v
    COMMENT = 'Latest ocr_log row per file_id.'
AS
SELECT *
FROM ocr_log
QUALIFY ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY event_ts DESC, event_id DESC) = 1;

CREATE OR REPLACE PROCEDURE extract_text()
    RETURNS VARCHAR
    LANGUAGE SQL
    COMMENT = 'Extract text for queued files with AI_PARSE_DOCUMENT.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    v_done    INTEGER DEFAULT 0;
    v_failed  INTEGER DEFAULT 0;
    v_skipped INTEGER DEFAULT 0;
    v_fid     VARCHAR;
    v_path    VARCHAR;
    v_rel     VARCHAR;
    v_chk     VARCHAR;
    v_content VARCHAR;
    v_pages   INTEGER;
    v_err     VARCHAR;
    v_msg     VARCHAR;
    queued CURSOR FOR
        SELECT q.file_id, q.stage_path, f.checksum
        FROM ocr_status_v q
        LEFT JOIN (SELECT file_id, checksum FROM document_file_log
                   QUALIFY ROW_NUMBER() OVER (PARTITION BY file_id
                                              ORDER BY event_ts DESC, event_id DESC) = 1) f
          ON f.file_id = q.file_id
        WHERE q.status = 'queued';
BEGIN
    -- 1. Consume the stream. Deactivations (active = FALSE) are consumed too,
    --    but not queued; files that already have text are not queued again.
    INSERT INTO ocr_log (event_id, event_ts, actor, file_id, stage_path, status)
    SELECT UUID_STRING(), SYSDATE(), 'system:ocr', s.file_id, s.stage_path, 'queued'
    FROM document_file_log_stream s
    WHERE s.active
      AND s.stage_path IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM document_text t WHERE t.file_id = s.file_id)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY s.file_id ORDER BY s.event_ts DESC) = 1;

    -- 2. Work through the queue.
    FOR r IN queued DO
        v_fid  := r.file_id;
        v_path := r.stage_path;
        v_chk  := r.checksum;
        v_rel  := REGEXP_REPLACE(v_path, '^@doc_files/', '');
        BEGIN
            IF (NOT REGEXP_LIKE(LOWER(v_rel),
                    '.*[.](pdf|docx|pptx|jpeg|jpg|png|tif|tiff|html|htm|txt)$')) THEN
                INSERT INTO ocr_log (event_id, event_ts, actor, file_id, stage_path, status, message)
                SELECT UUID_STRING(), SYSDATE(), 'system:ocr', :v_fid, :v_path, 'skipped',
                        'AI_PARSE_DOCUMENT does not support this file type';
                v_skipped := v_skipped + 1;
            ELSE
                -- Same bytes already extracted? Reuse the text, no Cortex call.
                v_content := NULL;
                v_pages := NULL;
                v_err := NULL;
                IF (v_chk IS NOT NULL) THEN
                    SELECT MAX(t.content) INTO :v_content
                    FROM document_text t
                    JOIN document_file_log f ON f.file_id = t.file_id
                    WHERE f.checksum = :v_chk;
                END IF;
                IF (v_content IS NULL) THEN
                    SELECT p:content::VARCHAR, p:metadata:pageCount::INT,
                           p:errorInformation::VARCHAR
                      INTO :v_content, :v_pages, :v_err
                      FROM (SELECT AI_PARSE_DOCUMENT(TO_FILE('@doc_files', :v_rel),
                                                     {'mode': 'OCR'}) AS p);
                END IF;
                IF (v_content IS NULL AND v_err IS NOT NULL) THEN
                    INSERT INTO ocr_log (event_id, event_ts, actor, file_id, stage_path, status, message)
                    SELECT UUID_STRING(), SYSDATE(), 'system:ocr', :v_fid, :v_path, 'failed', :v_err;
                    v_failed := v_failed + 1;
                ELSE
                    INSERT INTO document_text (event_id, event_ts, actor, file_id, content)
                    SELECT UUID_STRING(), SYSDATE(), 'system:ocr', :v_fid, COALESCE(:v_content, '');
                    INSERT INTO ocr_log (event_id, event_ts, actor, file_id, stage_path, status, page_count)
                    SELECT UUID_STRING(), SYSDATE(), 'system:ocr', :v_fid, :v_path, 'done', :v_pages;
                    v_done := v_done + 1;
                END IF;
            END IF;
        EXCEPTION
            WHEN OTHER THEN
                v_msg := SQLERRM;
                INSERT INTO ocr_log (event_id, event_ts, actor, file_id, stage_path, status, message)
                SELECT UUID_STRING(), SYSDATE(), 'system:ocr', :v_fid, :v_path, 'failed', :v_msg;
                v_failed := v_failed + 1;
        END;
    END FOR;
    RETURN v_done || ' done, ' || v_failed || ' failed, ' || v_skipped || ' skipped';
END;
$$;

-- Triggered task: no schedule, runs when the stream has data. Serverless (no
-- WAREHOUSE); TARGET_COMPLETION_INTERVAL tells Snowflake how quickly a run
-- should finish. To use a warehouse instead, add WAREHOUSE = <wh>.
CREATE OR REPLACE TASK extract_text_task
    COMMENT = 'Runs extract_text() when new files are saved'
    TARGET_COMPLETION_INTERVAL = '15 MINUTE'
    WHEN SYSTEM$STREAM_HAS_DATA('document_file_log_stream')
AS
    CALL extract_text();

-- Tasks are created suspended.
ALTER TASK extract_text_task RESUME;

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
