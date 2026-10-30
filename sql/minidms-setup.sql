-- ─────────────────────────────────────────────────────────────────────────────
-- MiniDMS — setup
--
-- Everything the app needs: a warehouse (created only if missing), one stage, nine hybrid
-- tables, five standard tables, four views, and the AI pipeline (one stream,
-- one procedure, one triggered task). Run once in the database and schema you
-- already use.
--
-- One global state, in Snowflake only. There is no local or per-session copy:
-- every change in the app is written straight to these tables.
--
--   Hybrid tables   — everything people edit, one data point at a time
--                     (documents, metadata values, tags, definitions). Enforced
--                     primary keys make ids unique; a version column per row
--                     lets the app detect concurrent edits ("changed by someone
--                     else") instead of overwriting them. change_log keeps the
--                     full history, with event_id as an enforced primary key so
--                     a retried write can never be applied twice.
--   Standard tables — immutable facts and AI output: uploaded files, AI
--                     requests, OCR text, enrichments. Written only by INSERT
--                     (… SELECT …), so Snowflake records column-level lineage
--                     from the file to the text to the summary/suggestions.
--
-- Idempotent. Tables and the stream use IF NOT EXISTS so re-running never drops
-- data or resets the stream offset; views, the procedure and the task use
-- OR REPLACE because they hold no state (the ALTER TASK … RESUME at the end
-- starts the task again).
--
-- Privileges the owning role needs: CREATE WAREHOUSE (account), CREATE TABLE /
-- VIEW / STAGE / STREAM / TASK / PROCEDURE (schema), EXECUTE TASK (account) and
-- the SNOWFLAKE.CORTEX_USER database role (AI_COMPLETE).
-- ─────────────────────────────────────────────────────────────────────────────

-- USE DATABASE <yours>;
-- USE SCHEMA   <yours>;

-- ── Warehouse ────────────────────────────────────────────────────────────────

-- Bootstrap only: in an empty account there is nothing to run queries or the AI
-- task on, so the setup creates one — adaptive, so there is no size to pick.
-- IF NOT EXISTS: an existing minidms_wh is left as it is. The app itself runs on
-- any warehouse (the connection's or the Streamlit app's QUERY_WAREHOUSE); the
-- AI task below uses minidms_wh (change with ALTER TASK … SET WAREHOUSE).
CREATE ADAPTIVE WAREHOUSE IF NOT EXISTS minidms_wh
    COMMENT = 'MiniDMS: created by the setup; runs the AI task';

-- ── Stage ────────────────────────────────────────────────────────────────────

-- Uploaded documents, written once and never modified.
-- Layout: @doc_files/{sha256 of the content}/{filename}. Content-addressed:
-- the same bytes uploaded under the same name land on the same path. The
-- extension stays on the name because AI_COMPLETE detects the format from it.
-- Server-side encryption is required for AI_COMPLETE to read staged files.
CREATE STAGE IF NOT EXISTS doc_files
    DIRECTORY  = (ENABLE = TRUE)
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    COMMENT    = 'Immutable uploaded document files';

-- ── Hybrid tables: human-edited state ────────────────────────────────────────
--
-- Current state, one row per entity. Every edit runs as one transaction:
-- insert into change_log (event_id PK) + update the row WHERE version = the
-- version the user saw + bump sync_state. If the version no longer matches,
-- nothing is written and the app reports a conflict.

CREATE HYBRID TABLE IF NOT EXISTS document_type (
    document_type_id VARCHAR       NOT NULL,
    label            VARCHAR       NOT NULL,
    active           BOOLEAN       NOT NULL DEFAULT TRUE,
    version          NUMBER        NOT NULL DEFAULT 1,
    created_at       TIMESTAMP_NTZ NOT NULL,
    created_by       VARCHAR       NOT NULL,
    updated_at       TIMESTAMP_NTZ NOT NULL,
    updated_by       VARCHAR       NOT NULL,
    PRIMARY KEY (document_type_id)
)   COMMENT = 'Document types. Human-edited; version = optimistic lock.';

CREATE HYBRID TABLE IF NOT EXISTS metadata_type (
    metadata_type_id VARCHAR       NOT NULL,
    name             VARCHAR       NOT NULL,   -- machine name
    label            VARCHAR       NOT NULL,
    data_type        VARCHAR       NOT NULL,   -- text | number | date | choice
    choices          VARCHAR,                  -- newline-separated
    default_value    VARCHAR,
    active           BOOLEAN       NOT NULL DEFAULT TRUE,
    version          NUMBER        NOT NULL DEFAULT 1,
    created_at       TIMESTAMP_NTZ NOT NULL,
    created_by       VARCHAR       NOT NULL,
    updated_at       TIMESTAMP_NTZ NOT NULL,
    updated_by       VARCHAR       NOT NULL,
    PRIMARY KEY (metadata_type_id)
)   COMMENT = 'Typed metadata fields. Human-edited; version = optimistic lock.';

CREATE HYBRID TABLE IF NOT EXISTS tag (
    tag_id      VARCHAR       NOT NULL,
    label       VARCHAR       NOT NULL,
    color       VARCHAR(7),                    -- #rrggbb
    active      BOOLEAN       NOT NULL DEFAULT TRUE,
    version     NUMBER        NOT NULL DEFAULT 1,
    created_at  TIMESTAMP_NTZ NOT NULL,
    created_by  VARCHAR       NOT NULL,
    updated_at  TIMESTAMP_NTZ NOT NULL,
    updated_by  VARCHAR       NOT NULL,
    PRIMARY KEY (tag_id)
)   COMMENT = 'Tags. Human-edited; version = optimistic lock.';

CREATE HYBRID TABLE IF NOT EXISTS document (
    document_id      VARCHAR       NOT NULL,   -- uuid4, client-generated
    document_type_id VARCHAR,
    label            VARCHAR,
    description      VARCHAR,
    language         VARCHAR(8),
    status           VARCHAR       NOT NULL DEFAULT 'active',  -- active | trashed
    version          NUMBER        NOT NULL DEFAULT 1,
    created_at       TIMESTAMP_NTZ NOT NULL,
    created_by       VARCHAR       NOT NULL,
    updated_at       TIMESTAMP_NTZ NOT NULL,
    updated_by       VARCHAR       NOT NULL,
    PRIMARY KEY (document_id)
)   COMMENT = 'Documents. Human-edited; version = optimistic lock.';

-- Which files belong to a document, and whether they are still attached.
-- The file itself (name, size, checksum, stage path) is in stored_file.
CREATE HYBRID TABLE IF NOT EXISTS document_file (
    file_id     VARCHAR       NOT NULL,
    document_id VARCHAR       NOT NULL,
    active      BOOLEAN       NOT NULL DEFAULT TRUE,
    version     NUMBER        NOT NULL DEFAULT 1,
    created_at  TIMESTAMP_NTZ NOT NULL,
    created_by  VARCHAR       NOT NULL,
    updated_at  TIMESTAMP_NTZ NOT NULL,
    updated_by  VARCHAR       NOT NULL,
    PRIMARY KEY (file_id),
    INDEX document_file_document_idx (document_id)
)   COMMENT = 'File attachments. Human-edited; version = optimistic lock.';

-- One row per (document, field) that has a value. Removing a value deletes the
-- row; the history is in change_log.
CREATE HYBRID TABLE IF NOT EXISTS document_metadata (
    document_id      VARCHAR       NOT NULL,
    metadata_type_id VARCHAR       NOT NULL,
    value            VARCHAR       NOT NULL,
    version          NUMBER        NOT NULL DEFAULT 1,
    updated_at       TIMESTAMP_NTZ NOT NULL,
    updated_by       VARCHAR       NOT NULL,
    PRIMARY KEY (document_id, metadata_type_id)
)   COMMENT = 'Metadata values. Human-edited; version = optimistic lock.';

-- One row per attached tag; detaching deletes the row. The primary key makes a
-- double attach impossible.
CREATE HYBRID TABLE IF NOT EXISTS document_tag (
    document_id VARCHAR       NOT NULL,
    tag_id      VARCHAR       NOT NULL,
    assigned_at TIMESTAMP_NTZ NOT NULL,
    assigned_by VARCHAR       NOT NULL,
    PRIMARY KEY (document_id, tag_id)
)   COMMENT = 'Tag assignments. Human-edited.';

-- Append-only history of every human edit, written in the same transaction as
-- the edit. event_id is client-generated per action: a retried action hits the
-- primary key and is rejected as a whole, so it can never be applied twice.
CREATE HYBRID TABLE IF NOT EXISTS change_log (
    event_id    VARCHAR       NOT NULL,
    event_ts    TIMESTAMP_NTZ NOT NULL,
    actor       VARCHAR       NOT NULL,       -- viewer (st.user / login e-mail)
    entity      VARCHAR       NOT NULL,       -- document | document_file | document_metadata | …
    entity_id   VARCHAR       NOT NULL,
    document_id VARCHAR,                      -- set for document-scoped changes
    op          VARCHAR       NOT NULL,       -- create | update | trash | restore | delete | attach | detach
    version     NUMBER,                       -- row version after the change
    data        VARCHAR,                      -- JSON of the new values
    PRIMARY KEY (event_id),
    INDEX change_log_document_idx (document_id)
)   COMMENT = 'Append-only history of human edits. event_id = idempotency key.';

-- Change counters, one row per table. Every write bumps the rows of the tables
-- it touched (in the same transaction for hybrid writes). The app reads all of
-- them in one point query per click and uses them as cache keys.
CREATE HYBRID TABLE IF NOT EXISTS sync_state (
    table_name  VARCHAR       NOT NULL,
    seq         NUMBER        NOT NULL DEFAULT 0,
    changed_at  TIMESTAMP_NTZ,
    PRIMARY KEY (table_name)
)   COMMENT = 'Per-table change counters; cache keys for the app.';

INSERT INTO sync_state (table_name, seq, changed_at)
SELECT n.table_name, 0, SYSDATE()
FROM (SELECT column1 AS table_name FROM VALUES
        ('document_type'), ('metadata_type'), ('tag'), ('document'),
        ('document_file'), ('document_metadata'), ('document_tag'), ('change_log'),
        ('stored_file'), ('ai_request'), ('document_text'), ('document_enrichment'),
        ('ai_job_log')) n
WHERE n.table_name NOT IN (SELECT table_name FROM sync_state);

-- ── Standard tables: immutable facts and AI output ───────────────────────────

-- One row per uploaded file. Never changed. The source of the lineage chain.
CREATE TABLE IF NOT EXISTS stored_file (
    file_id     VARCHAR       NOT NULL,       -- uuid4
    document_id VARCHAR       NOT NULL,       -- document it was uploaded to
    uploaded_at TIMESTAMP_NTZ NOT NULL,
    uploaded_by VARCHAR       NOT NULL,
    filename    VARCHAR,                      -- original upload name
    stage_path  VARCHAR       NOT NULL,       -- '@doc_files/{checksum}/{filename}'
    mimetype    VARCHAR,
    size        NUMBER,
    checksum    VARCHAR(64)                   -- sha256 hex
)   COMMENT = 'Append-only. Uploaded files; bytes live in @doc_files.';

-- A request to run OCR + enrichment for a file: written at upload and when a
-- user asks to process a file again. The stream on this table fires the task.
CREATE TABLE IF NOT EXISTS ai_request (
    event_id     VARCHAR       NOT NULL,
    requested_at TIMESTAMP_NTZ NOT NULL,
    actor        VARCHAR       NOT NULL,
    file_id      VARCHAR       NOT NULL,
    document_id  VARCHAR       NOT NULL,
    reason       VARCHAR                      -- upload | retry | missing
)   COMMENT = 'Append-only. AI processing requests; consumed by process_ai().';

-- OCR text from AI_COMPLETE. Written by INSERT … SELECT FROM stored_file, so
-- content is lineage-linked to stored_file.stage_path.
CREATE TABLE IF NOT EXISTS document_text (
    event_id     VARCHAR       NOT NULL,
    extracted_at TIMESTAMP_NTZ NOT NULL,
    actor        VARCHAR       NOT NULL,      -- 'system:ai'
    file_id      VARCHAR       NOT NULL,
    model        VARCHAR,
    status       VARCHAR       NOT NULL,      -- done | failed | skipped
    content      VARCHAR,
    error        VARCHAR
)   COMMENT = 'Append-only. OCR text per file; latest row per file_id wins.';

-- Summary, document-type / tag suggestions and extracted metadata from
-- AI_COMPLETE. Written by INSERT … SELECT FROM document_text (+ the current
-- definitions), so every column is lineage-linked to its sources.
CREATE TABLE IF NOT EXISTS document_enrichment (
    event_id           VARCHAR       NOT NULL,
    created_at         TIMESTAMP_NTZ NOT NULL,
    actor              VARCHAR       NOT NULL,  -- 'system:ai'
    file_id            VARCHAR       NOT NULL,
    document_id        VARCHAR       NOT NULL,
    model              VARCHAR,
    status             VARCHAR       NOT NULL,  -- done | failed | skipped
    summary            VARCHAR,
    suggested_type_id  VARCHAR,
    suggested_tag_ids  ARRAY,
    extracted_metadata ARRAY,                   -- [{metadata_type_id, value}]
    error              VARCHAR
)   COMMENT = 'Append-only. AI enrichment per file; latest row per file_id wins.';

-- What process_ai() did: one row when a request is taken from the stream
-- (queued) and one when it is finished.
CREATE TABLE IF NOT EXISTS ai_job_log (
    event_id    VARCHAR       NOT NULL,
    event_ts    TIMESTAMP_NTZ NOT NULL,
    request_id  VARCHAR       NOT NULL,
    file_id     VARCHAR       NOT NULL,
    document_id VARCHAR       NOT NULL,
    step        VARCHAR       NOT NULL,       -- queued | finished
    message     VARCHAR
)   COMMENT = 'Append-only. AI processing log.';

-- ── Views ────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW document_text_current
    COMMENT = 'Latest OCR result per file.'
AS
SELECT *
FROM document_text
QUALIFY ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY extracted_at DESC, event_id DESC) = 1;

CREATE OR REPLACE VIEW document_enrichment_current
    COMMENT = 'Latest enrichment per file.'
AS
SELECT *
FROM document_enrichment
QUALIFY ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY created_at DESC, event_id DESC) = 1;

-- AI state per file: pending while the newest request is newer than the newest
-- enrichment row (process_ai() always writes one, also when OCR failed).
CREATE OR REPLACE VIEW ai_status_v
    COMMENT = 'AI processing state per file.'
AS
WITH r AS (SELECT file_id, MAX(requested_at) AS requested_at FROM ai_request GROUP BY file_id)
SELECT r.file_id,
       r.requested_at,
       t.status                    AS text_status,
       t.error                     AS text_error,
       t.extracted_at,
       LENGTH(t.content)           AS chars,
       e.status                    AS enrich_status,
       e.error                     AS enrich_error,
       e.created_at                AS enriched_at,
       (e.created_at IS NULL OR e.created_at < r.requested_at) AS pending
FROM r
LEFT JOIN document_text_current       t ON t.file_id = r.file_id
LEFT JOIN document_enrichment_current e ON e.file_id = r.file_id;

-- History is the data: human edits from change_log, plus uploads and AI output.
CREATE OR REPLACE VIEW audit_v
    COMMENT = 'Unified history: human edits, uploads, AI processing.'
AS
          SELECT event_ts,     actor,       op,                  entity,                document_id, entity_id AS object_id FROM change_log
UNION ALL SELECT uploaded_at,  uploaded_by, 'upload',            'stored_file',         document_id, file_id                FROM stored_file
UNION ALL SELECT extracted_at, actor,       'ocr_' || status,    'document_text',       NULL,        file_id                FROM document_text
UNION ALL SELECT created_at,   actor,       'enrich_' || status, 'document_enrichment', document_id, file_id                FROM document_enrichment;

-- ── AI pipeline: stream + procedure + triggered task ─────────────────────────
--
-- Saving an upload (or "process again") appends to ai_request. The stream sees
-- it, which fires the triggered task on minidms_wh, which calls process_ai():
--   1. consume the stream: log every new request as 'queued' (this INSERT is
--      what advances the stream offset);
--   2. per queued request:
--      a. OCR — reuse the text of an identical file (same checksum), or call
--         AI_COMPLETE on the staged file, as INSERT … SELECT FROM stored_file;
--      b. enrichment — AI_COMPLETE with a JSON schema over the text and the
--         current definitions, as INSERT … SELECT FROM document_text;
--      c. log 'finished' and bump sync_state so the app reloads.
--   One failing file never blocks the others: it gets 'failed' rows with the
--   error message. Model: claude-sonnet-4-5.

CREATE STREAM IF NOT EXISTS ai_request_stream
    ON TABLE ai_request
    APPEND_ONLY = TRUE
    COMMENT = 'New AI requests, consumed by process_ai()';

CREATE OR REPLACE PROCEDURE process_ai()
    RETURNS VARCHAR
    LANGUAGE SQL
    COMMENT = 'OCR and enrichment with AI_COMPLETE for queued AI requests.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    v_done    INTEGER DEFAULT 0;
    v_failed  INTEGER DEFAULT 0;
    v_req     VARCHAR;
    v_fid     VARCHAR;
    v_doc     VARCHAR;
    v_rel     VARCHAR;
    v_chk     VARCHAR;
    v_n       INTEGER;
    v_msg     VARCHAR;
    queued CURSOR FOR
        SELECT q.request_id, q.file_id, q.document_id, f.stage_path, f.checksum
        FROM ai_job_log q
        JOIN stored_file f ON f.file_id = q.file_id
        WHERE q.step = 'queued'
          AND q.request_id NOT IN (SELECT request_id FROM ai_job_log WHERE step = 'finished');
BEGIN
    -- 1. Consume the stream.
    INSERT INTO ai_job_log (event_id, event_ts, request_id, file_id, document_id, step)
    SELECT UUID_STRING(), SYSDATE(), s.event_id, s.file_id, s.document_id, 'queued'
    FROM ai_request_stream s;

    -- 2. Work through the queue.
    FOR r IN queued DO
        v_req := r.request_id;
        v_fid := r.file_id;
        v_doc := r.document_id;
        v_chk := r.checksum;
        v_rel := REGEXP_REPLACE(r.stage_path, '^@doc_files/', '');

        -- 2a. OCR
        BEGIN
            IF (NOT REGEXP_LIKE(LOWER(v_rel),
                    '.*[.](pdf|doc|docx|xls|xlsx|csv|txt|md|xhtml|jpg|jpeg|png|gif|webp)$')) THEN
                INSERT INTO document_text (event_id, extracted_at, actor, file_id, model, status, error)
                SELECT UUID_STRING(), SYSDATE(), 'system:ai', :v_fid, NULL, 'skipped',
                       'AI_COMPLETE does not read this file type';
            ELSE
                -- Same bytes already extracted? Copy the text, no AI call.
                INSERT INTO document_text (event_id, extracted_at, actor, file_id, model, status, content)
                SELECT UUID_STRING(), SYSDATE(), 'system:ai', :v_fid, t.model, 'done', t.content
                FROM document_text_current t
                JOIN stored_file f ON f.file_id = t.file_id
                WHERE f.checksum = :v_chk AND t.status = 'done' AND t.file_id <> :v_fid
                LIMIT 1;
                v_n := SQLROWCOUNT;
                IF (v_n = 0) THEN
                    INSERT INTO document_text (event_id, extracted_at, actor, file_id, model, status, content)
                    SELECT UUID_STRING(), SYSDATE(), 'system:ai', f.file_id, 'claude-sonnet-4-5', 'done',
                           AI_COMPLETE(
                               'claude-sonnet-4-5',
                               PROMPT('Transcribe all text in this document exactly as written (OCR). '
                                      || 'Keep the reading order. Separate pages with a line that '
                                      || 'contains only ---. Return only the text, without comments. {0}',
                                      TO_FILE('@doc_files', REGEXP_REPLACE(f.stage_path, '^@doc_files/', ''))))
                    FROM stored_file f
                    WHERE f.file_id = :v_fid
                    LIMIT 1;
                END IF;
            END IF;
        EXCEPTION
            WHEN OTHER THEN
                v_msg := SQLERRM;
                INSERT INTO document_text (event_id, extracted_at, actor, file_id, model, status, error)
                SELECT UUID_STRING(), SYSDATE(), 'system:ai', :v_fid, 'claude-sonnet-4-5', 'failed', :v_msg;
        END;

        -- 2b. Enrichment: summary, type + tag suggestions, metadata extraction.
        BEGIN
            INSERT INTO document_enrichment (event_id, created_at, actor, file_id, document_id, model,
                                             status, summary, suggested_type_id, suggested_tag_ids,
                                             extracted_metadata)
            SELECT UUID_STRING(), SYSDATE(), 'system:ai', x.file_id, :v_doc, 'claude-sonnet-4-5', 'done',
                   x.j:summary::VARCHAR,
                   NULLIF(x.j:document_type_id::VARCHAR, ''),
                   x.j:tag_ids::ARRAY,
                   x.j:metadata::ARRAY
            FROM (
                SELECT t.file_id,
                       TRY_PARSE_JSON(TO_VARCHAR(AI_COMPLETE(
                           model => 'claude-sonnet-4-5',
                           prompt => 'You are filing a document in a document management system. '
                               || 'Write a short summary (at most 5 sentences, in the document''s language). '
                               || 'Choose the best matching document type from DOCUMENT_TYPES (its id, or an '
                               || 'empty string if none fits) and up to 5 fitting tags from TAGS (ids only). '
                               || 'Extract a value for each METADATA_TYPES field that the document clearly '
                               || 'contains; dates as YYYY-MM-DD, numbers without units, choices exactly as '
                               || 'listed. Only use ids from the lists.'
                               || '\nDOCUMENT_TYPES: ' || COALESCE((SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                                      'id', document_type_id, 'label', label))::VARCHAR
                                      FROM document_type WHERE active), '[]')
                               || '\nTAGS: ' || COALESCE((SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                                      'id', tag_id, 'label', label))::VARCHAR
                                      FROM tag WHERE active), '[]')
                               || '\nMETADATA_TYPES: ' || COALESCE((SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                                      'id', metadata_type_id, 'label', label, 'data_type', data_type,
                                      'choices', choices))::VARCHAR
                                      FROM metadata_type WHERE active), '[]')
                               || '\nDOCUMENT TEXT:\n' || LEFT(t.content, 100000),
                           response_format => {
                               'type': 'json',
                               'schema': {
                                   'type': 'object',
                                   'properties': {
                                       'summary': {'type': 'string'},
                                       'document_type_id': {'type': 'string'},
                                       'tag_ids': {'type': 'array', 'items': {'type': 'string'}},
                                       'metadata': {'type': 'array', 'items': {
                                           'type': 'object',
                                           'properties': {
                                               'metadata_type_id': {'type': 'string'},
                                               'value': {'type': 'string'}
                                           },
                                           'required': ['metadata_type_id', 'value']
                                       }}
                                   },
                                   'required': ['summary', 'document_type_id', 'tag_ids', 'metadata']
                               }
                           }))) AS j
                FROM document_text_current t
                WHERE t.file_id = :v_fid AND t.status = 'done' AND LENGTH(t.content) > 0
            ) x;
            v_n := SQLROWCOUNT;
            IF (v_n = 0) THEN
                INSERT INTO document_enrichment (event_id, created_at, actor, file_id, document_id,
                                                 model, status, error)
                SELECT UUID_STRING(), SYSDATE(), 'system:ai', :v_fid, :v_doc, NULL, 'skipped',
                       'No text to enrich';
            END IF;
            v_done := v_done + 1;
        EXCEPTION
            WHEN OTHER THEN
                v_msg := SQLERRM;
                INSERT INTO document_enrichment (event_id, created_at, actor, file_id, document_id,
                                                 model, status, error)
                SELECT UUID_STRING(), SYSDATE(), 'system:ai', :v_fid, :v_doc, 'claude-sonnet-4-5',
                       'failed', :v_msg;
                v_failed := v_failed + 1;
        END;

        -- 2c. Done: log it and tell the app.
        INSERT INTO ai_job_log (event_id, event_ts, request_id, file_id, document_id, step)
        SELECT UUID_STRING(), SYSDATE(), :v_req, :v_fid, :v_doc, 'finished';
        UPDATE sync_state SET seq = seq + 1, changed_at = SYSDATE()
        WHERE table_name IN ('document_text', 'document_enrichment', 'ai_job_log');
    END FOR;
    RETURN v_done || ' processed, ' || v_failed || ' failed';
END;
$$;

-- Triggered task: no schedule, runs when the stream has data, on minidms_wh.
CREATE OR REPLACE TASK process_ai_task
    COMMENT = 'Runs process_ai() when files are uploaded or reprocessing is requested'
    WAREHOUSE = minidms_wh
    WHEN SYSTEM$STREAM_HAS_DATA('ai_request_stream')
AS
    CALL process_ai();

-- Tasks are created suspended.
ALTER TASK process_ai_task RESUME;

-- ── Later, if needed ─────────────────────────────────────────────────────────
-- Housekeeping, when it starts to matter rather than now:
--   · staged files with no stored_file row (upload interrupted)
-- A query plus a REMOVE loop; the Admin page has a button for it.
