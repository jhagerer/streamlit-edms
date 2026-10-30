-- ─────────────────────────────────────────────────────────────────────────────
-- MiniDMS — setup
--
-- Everything the app needs: a warehouse (created only if missing), one stage,
-- seven hybrid tables, six standard tables, a dozen views, the AI pipeline (one
-- stream, one procedure, one triggered task) and one Cortex Search service.
-- Run once in the database and schema you already use.
--
-- One global state, in Snowflake only, and EVERY table is append-only — the
-- hybrid tables too. Nothing is ever updated or deleted; the current state is
-- the latest version per key (the *_current views).
--
--   Hybrid tables   — everything people edit, one data point at a time
--                     (documents, metadata values, tag assignments, file
--                     attachments, definitions). Each change appends the next
--                     version of the record. PRIMARY KEY (id, version) makes
--                     concurrent edits collide: two users saving version n+1 of
--                     the same record — the second insert fails and the app
--                     reports a conflict. UNIQUE (event_id) makes a retried
--                     action fail as a whole, so it is never applied twice.
--   Standard tables — immutable facts and AI output: uploaded files, AI
--                     requests, OCR text, enrichments and the search corpus.
--                     Written by INSERT … SELECT inside Snowflake, so Snowflake
--                     records column-level lineage from the file to the text to
--                     the summary, suggestions and search chunks.
--   Cortex Search   — the sink of the ingest pipeline: it indexes the search
--                     corpus that process_ai() writes, and the Search page
--                     queries it.
--
-- Idempotent. Tables, the stream and the search service use IF NOT EXISTS so
-- re-running never drops data, resets the stream offset or rebuilds the index;
-- views, the procedure and the task use OR REPLACE because they hold no state
-- (the ALTER TASK … RESUME at the end starts the task again).
--
-- Privileges the owning role needs: CREATE WAREHOUSE (account, only if
-- minidms_wh does not exist), CREATE TABLE / HYBRID TABLE / VIEW / STAGE /
-- STREAM / TASK / PROCEDURE / CORTEX SEARCH SERVICE (schema), EXECUTE TASK
-- (account) and the SNOWFLAKE.CORTEX_USER database role.
-- ─────────────────────────────────────────────────────────────────────────────

-- USE DATABASE <yours>;
-- USE SCHEMA   <yours>;

-- ── Warehouse ────────────────────────────────────────────────────────────────

-- Bootstrap only: in an empty account there is nothing to run queries, the AI
-- task or the search service on, so the setup creates one — adaptive, so there
-- is no size to pick. IF NOT EXISTS: an existing minidms_wh is left as it is.
-- The app itself runs on any warehouse (the connection's or the Streamlit
-- app's QUERY_WAREHOUSE).
CREATE ADAPTIVE WAREHOUSE IF NOT EXISTS minidms_wh
    COMMENT = 'MiniDMS: created by the setup; runs the AI task and the search service';

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

-- ── Hybrid tables: human-edited state, append-only ───────────────────────────
--
-- One row per version. Every edit is one transaction that appends the next
-- version (the user's version + 1) of each record it touches, with the full
-- record, who and when. No UPDATE, no DELETE: removing a metadata value
-- appends value = NULL, detaching a tag appends assigned = FALSE, trashing a
-- document appends status = 'trashed'. `source` records provenance, e.g.
-- 'ai:<enrichment event_id>' when a user applies an AI suggestion.

CREATE HYBRID TABLE IF NOT EXISTS document_type_log (
    document_type_id VARCHAR       NOT NULL,
    version          NUMBER        NOT NULL,
    event_id         VARCHAR       NOT NULL,   -- client-generated per action
    event_ts         TIMESTAMP_NTZ NOT NULL,
    actor            VARCHAR       NOT NULL,   -- viewer (st.user / login e-mail)
    op               VARCHAR       NOT NULL,   -- create | update
    label            VARCHAR       NOT NULL,
    active           BOOLEAN       NOT NULL,
    source           VARCHAR,
    PRIMARY KEY (document_type_id, version),
    UNIQUE (event_id)
)   COMMENT = 'Append-only. Document types; latest version per id is current.';

CREATE HYBRID TABLE IF NOT EXISTS metadata_type_log (
    metadata_type_id VARCHAR       NOT NULL,
    version          NUMBER        NOT NULL,
    event_id         VARCHAR       NOT NULL,
    event_ts         TIMESTAMP_NTZ NOT NULL,
    actor            VARCHAR       NOT NULL,
    op               VARCHAR       NOT NULL,   -- create | update
    name             VARCHAR       NOT NULL,   -- machine name
    label            VARCHAR       NOT NULL,
    data_type        VARCHAR       NOT NULL,   -- text | number | date | choice
    choices          VARCHAR,                  -- newline-separated
    default_value    VARCHAR,
    active           BOOLEAN       NOT NULL,
    source           VARCHAR,
    PRIMARY KEY (metadata_type_id, version),
    UNIQUE (event_id)
)   COMMENT = 'Append-only. Typed metadata fields; latest version per id is current.';

CREATE HYBRID TABLE IF NOT EXISTS tag_log (
    tag_id      VARCHAR       NOT NULL,
    version     NUMBER        NOT NULL,
    event_id    VARCHAR       NOT NULL,
    event_ts    TIMESTAMP_NTZ NOT NULL,
    actor       VARCHAR       NOT NULL,
    op          VARCHAR       NOT NULL,        -- create | update
    label       VARCHAR       NOT NULL,
    color       VARCHAR(7),                    -- #rrggbb
    active      BOOLEAN       NOT NULL,
    source      VARCHAR,
    PRIMARY KEY (tag_id, version),
    UNIQUE (event_id)
)   COMMENT = 'Append-only. Tags; latest version per id is current.';

CREATE HYBRID TABLE IF NOT EXISTS document_log (
    document_id      VARCHAR       NOT NULL,   -- uuid4, client-generated
    version          NUMBER        NOT NULL,
    event_id         VARCHAR       NOT NULL,
    event_ts         TIMESTAMP_NTZ NOT NULL,
    actor            VARCHAR       NOT NULL,
    op               VARCHAR       NOT NULL,   -- create | update | trash | restore
    document_type_id VARCHAR,
    label            VARCHAR,
    description      VARCHAR,
    language         VARCHAR(8),
    status           VARCHAR       NOT NULL,   -- active | trashed
    source           VARCHAR,
    PRIMARY KEY (document_id, version),
    UNIQUE (event_id)
)   COMMENT = 'Append-only. Documents; latest version per id is current.';

-- Which files belong to a document, and whether they are still attached.
-- The file itself (name, size, checksum, stage path) is in stored_file.
CREATE HYBRID TABLE IF NOT EXISTS document_file_log (
    file_id     VARCHAR       NOT NULL,
    version     NUMBER        NOT NULL,
    event_id    VARCHAR       NOT NULL,
    event_ts    TIMESTAMP_NTZ NOT NULL,
    actor       VARCHAR       NOT NULL,
    op          VARCHAR       NOT NULL,        -- attach | detach
    document_id VARCHAR       NOT NULL,
    active      BOOLEAN       NOT NULL,
    source      VARCHAR,
    PRIMARY KEY (file_id, version),
    UNIQUE (event_id),
    INDEX document_file_log_document_idx (document_id)
)   COMMENT = 'Append-only. File attachments; latest version per file is current.';

-- One version chain per (document, field). value = NULL means removed.
CREATE HYBRID TABLE IF NOT EXISTS metadata_log (
    document_id      VARCHAR       NOT NULL,
    metadata_type_id VARCHAR       NOT NULL,
    version          NUMBER        NOT NULL,
    event_id         VARCHAR       NOT NULL,
    event_ts         TIMESTAMP_NTZ NOT NULL,
    actor            VARCHAR       NOT NULL,
    op               VARCHAR       NOT NULL,   -- set | remove
    value            VARCHAR,
    source           VARCHAR,
    PRIMARY KEY (document_id, metadata_type_id, version),
    UNIQUE (event_id)
)   COMMENT = 'Append-only. Metadata values; latest version per (document, field) is current.';

-- One version chain per (document, tag). assigned = FALSE means detached.
CREATE HYBRID TABLE IF NOT EXISTS tag_assignment_log (
    document_id VARCHAR       NOT NULL,
    tag_id      VARCHAR       NOT NULL,
    version     NUMBER        NOT NULL,
    event_id    VARCHAR       NOT NULL,
    event_ts    TIMESTAMP_NTZ NOT NULL,
    actor       VARCHAR       NOT NULL,
    op          VARCHAR       NOT NULL,        -- attach | detach
    assigned    BOOLEAN       NOT NULL,
    source      VARCHAR,
    PRIMARY KEY (document_id, tag_id, version),
    UNIQUE (event_id)
)   COMMENT = 'Append-only. Tag assignments; latest version per (document, tag) is current.';

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

-- A request to run the ingest pipeline for a file: written at upload and when
-- a user asks to process a file again. The stream on this table fires the task.
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

-- The sink of the ingest pipeline and the source of the Cortex Search service:
-- the OCR text split into chunks, plus the summary, per processing run.
CREATE TABLE IF NOT EXISTS search_corpus (
    event_id    VARCHAR       NOT NULL,
    created_at  TIMESTAMP_NTZ NOT NULL,
    run_id      VARCHAR       NOT NULL,       -- the ai_request it came from
    file_id     VARCHAR       NOT NULL,
    document_id VARCHAR       NOT NULL,
    filename    VARCHAR,
    kind        VARCHAR       NOT NULL,       -- text | summary
    chunk_no    NUMBER        NOT NULL,       -- -1 for the summary
    text        VARCHAR
)   CHANGE_TRACKING = TRUE
    COMMENT = 'Append-only. Searchable chunks; indexed by the document_search service.';

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

-- ── Views: current state ─────────────────────────────────────────────────────
--
-- The latest version per key, with who created it and who changed it last.
-- Removed values and detached tags/files stay visible here (with their
-- version), because the next edit must append version + 1.

CREATE OR REPLACE VIEW document_type_current AS
SELECT document_type_id, label, active, version,
       FIRST_VALUE(event_ts) OVER (PARTITION BY document_type_id ORDER BY version) AS created_at,
       FIRST_VALUE(actor)    OVER (PARTITION BY document_type_id ORDER BY version) AS created_by,
       event_ts AS updated_at, actor AS updated_by
FROM document_type_log
QUALIFY ROW_NUMBER() OVER (PARTITION BY document_type_id ORDER BY version DESC) = 1;

CREATE OR REPLACE VIEW metadata_type_current AS
SELECT metadata_type_id, name, label, data_type, choices, default_value, active, version,
       FIRST_VALUE(event_ts) OVER (PARTITION BY metadata_type_id ORDER BY version) AS created_at,
       FIRST_VALUE(actor)    OVER (PARTITION BY metadata_type_id ORDER BY version) AS created_by,
       event_ts AS updated_at, actor AS updated_by
FROM metadata_type_log
QUALIFY ROW_NUMBER() OVER (PARTITION BY metadata_type_id ORDER BY version DESC) = 1;

CREATE OR REPLACE VIEW tag_current AS
SELECT tag_id, label, color, active, version,
       FIRST_VALUE(event_ts) OVER (PARTITION BY tag_id ORDER BY version) AS created_at,
       FIRST_VALUE(actor)    OVER (PARTITION BY tag_id ORDER BY version) AS created_by,
       event_ts AS updated_at, actor AS updated_by
FROM tag_log
QUALIFY ROW_NUMBER() OVER (PARTITION BY tag_id ORDER BY version DESC) = 1;

CREATE OR REPLACE VIEW document_current AS
SELECT document_id, document_type_id, label, description, language, status, version,
       FIRST_VALUE(event_ts) OVER (PARTITION BY document_id ORDER BY version) AS created_at,
       FIRST_VALUE(actor)    OVER (PARTITION BY document_id ORDER BY version) AS created_by,
       event_ts AS updated_at, actor AS updated_by
FROM document_log
QUALIFY ROW_NUMBER() OVER (PARTITION BY document_id ORDER BY version DESC) = 1;

CREATE OR REPLACE VIEW document_file_current AS
SELECT file_id, document_id, active, version,
       FIRST_VALUE(event_ts) OVER (PARTITION BY file_id ORDER BY version) AS created_at,
       FIRST_VALUE(actor)    OVER (PARTITION BY file_id ORDER BY version) AS created_by,
       event_ts AS updated_at, actor AS updated_by
FROM document_file_log
QUALIFY ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY version DESC) = 1;

CREATE OR REPLACE VIEW metadata_current AS
SELECT document_id, metadata_type_id, value, version,
       event_ts AS updated_at, actor AS updated_by
FROM metadata_log
QUALIFY ROW_NUMBER() OVER (PARTITION BY document_id, metadata_type_id ORDER BY version DESC) = 1;

CREATE OR REPLACE VIEW tag_assignment_current AS
SELECT document_id, tag_id, assigned, version,
       event_ts AS assigned_at, actor AS assigned_by
FROM tag_assignment_log
QUALIFY ROW_NUMBER() OVER (PARTITION BY document_id, tag_id ORDER BY version DESC) = 1;

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

-- AI state per file: pending while the newest request has no 'finished' entry
-- in ai_job_log yet.
CREATE OR REPLACE VIEW ai_status_v
    COMMENT = 'AI processing state per file.'
AS
WITH r AS (
    SELECT file_id, event_id AS request_id, requested_at
    FROM ai_request
    QUALIFY ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY requested_at DESC, event_id DESC) = 1
)
SELECT r.file_id,
       r.requested_at,
       t.status                    AS text_status,
       t.error                     AS text_error,
       t.extracted_at,
       LENGTH(t.content)           AS chars,
       e.status                    AS enrich_status,
       e.error                     AS enrich_error,
       e.created_at                AS enriched_at,
       NOT EXISTS (SELECT 1 FROM ai_job_log j
                   WHERE j.request_id = r.request_id AND j.step = 'finished') AS pending
FROM r
LEFT JOIN document_text_current       t ON t.file_id = r.file_id
LEFT JOIN document_enrichment_current e ON e.file_id = r.file_id;

-- Every human change of one document, for the History tab.
CREATE OR REPLACE VIEW document_history_v
    COMMENT = 'Human changes per document, from the append-only logs.'
AS
          SELECT event_ts, actor, 'document' AS entity, document_id, NULL AS item_id, op,
                 version, label AS value, source FROM document_log
UNION ALL SELECT event_ts, actor, 'file', document_id, file_id, op, version, NULL, source
          FROM document_file_log
UNION ALL SELECT event_ts, actor, 'metadata', document_id, metadata_type_id, op, version, value,
                 source FROM metadata_log
UNION ALL SELECT event_ts, actor, 'tag', document_id, tag_id, op, version, NULL, source
          FROM tag_assignment_log;

-- History is the data: every log, plus uploads and AI output.
CREATE OR REPLACE VIEW audit_v
    COMMENT = 'Unified history: human edits, uploads, AI processing.'
AS
          SELECT event_ts, actor, op, entity, document_id, COALESCE(item_id, document_id) AS object_id
          FROM document_history_v
UNION ALL SELECT event_ts, actor, op, 'document_type', NULL, document_type_id FROM document_type_log
UNION ALL SELECT event_ts, actor, op, 'metadata_type', NULL, metadata_type_id FROM metadata_type_log
UNION ALL SELECT event_ts, actor, op, 'tag_definition', NULL, tag_id FROM tag_log
UNION ALL SELECT uploaded_at,  uploaded_by, 'upload',            'stored_file',         document_id, file_id FROM stored_file
UNION ALL SELECT extracted_at, actor,       'ocr_' || status,    'document_text',       NULL,        file_id FROM document_text
UNION ALL SELECT created_at,   actor,       'enrich_' || status, 'document_enrichment', document_id, file_id FROM document_enrichment;

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
--      c. sink — split the text into chunks (SPLIT_TEXT_RECURSIVE_CHARACTER)
--         and append them plus the summary to search_corpus, which the
--         Cortex Search service indexes;
--      d. log 'finished'.
--   One failing file never blocks the others: it gets 'failed' rows with the
--   error message. Model: claude-sonnet-4-5. Only INSERTs: append-only.

CREATE STREAM IF NOT EXISTS ai_request_stream
    ON TABLE ai_request
    APPEND_ONLY = TRUE
    COMMENT = 'New AI requests, consumed by process_ai()';

CREATE OR REPLACE PROCEDURE process_ai()
    RETURNS VARCHAR
    LANGUAGE SQL
    COMMENT = 'OCR, enrichment (AI_COMPLETE) and search corpus for queued AI requests.'
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
                                      FROM document_type_current WHERE active), '[]')
                               || '\nTAGS: ' || COALESCE((SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                                      'id', tag_id, 'label', label))::VARCHAR
                                      FROM tag_current WHERE active), '[]')
                               || '\nMETADATA_TYPES: ' || COALESCE((SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                                      'id', metadata_type_id, 'label', label, 'data_type', data_type,
                                      'choices', choices))::VARCHAR
                                      FROM metadata_type_current WHERE active), '[]')
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

        -- 2c. Sink: chunks of the OCR text plus the summary go to search_corpus,
        --     the source of the Cortex Search service. One generation per run
        --     (run_id); the service indexes only the latest run per file.
        BEGIN
            INSERT INTO search_corpus (event_id, created_at, run_id, file_id, document_id,
                                       filename, kind, chunk_no, text)
            SELECT UUID_STRING(), SYSDATE(), :v_req, t.file_id, f.document_id, f.filename,
                   'text', c.index, c.value::VARCHAR
            FROM document_text_current t
            JOIN stored_file f ON f.file_id = t.file_id,
                 LATERAL FLATTEN(input => SNOWFLAKE.CORTEX.SPLIT_TEXT_RECURSIVE_CHARACTER(
                     t.content, 'none', 1500, 200)) c
            WHERE t.file_id = :v_fid AND t.status = 'done' AND LENGTH(t.content) > 0
            UNION ALL
            SELECT UUID_STRING(), SYSDATE(), :v_req, e.file_id, e.document_id, f.filename,
                   'summary', -1, e.summary
            FROM document_enrichment_current e
            JOIN stored_file f ON f.file_id = e.file_id
            WHERE e.file_id = :v_fid AND e.status = 'done' AND e.summary IS NOT NULL;
        EXCEPTION
            WHEN OTHER THEN
                v_msg := SQLERRM;
                v_failed := v_failed + 1;
        END;

        -- 2d. Done.
        INSERT INTO ai_job_log (event_id, event_ts, request_id, file_id, document_id, step, message)
        SELECT UUID_STRING(), SYSDATE(), :v_req, :v_fid, :v_doc, 'finished', :v_msg;
        v_msg := NULL;
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

-- ── Cortex Search: the sink of the ingest pipeline ───────────────────────────
--
-- Indexes the latest processing run of every file in search_corpus and keeps
-- itself up to date (TARGET_LAG). Hybrid keyword + semantic search; the
-- multilingual embedding model handles German and English documents. The
-- attributes come back with every hit; type, tag and trash filters are applied
-- in the app (they live in hybrid tables).
CREATE CORTEX SEARCH SERVICE IF NOT EXISTS document_search
    ON text
    ATTRIBUTES document_id, file_id, kind
    WAREHOUSE = minidms_wh
    TARGET_LAG = '1 minute'
    EMBEDDING_MODEL = 'snowflake-arctic-embed-l-v2.0'
    COMMENT = 'MiniDMS full-text and semantic search over OCR text and summaries'
AS (
    SELECT text, document_id, file_id, filename, kind, chunk_no
    FROM search_corpus
    QUALIFY run_id = FIRST_VALUE(run_id) OVER (PARTITION BY file_id ORDER BY created_at DESC)
);

-- ── Later, if needed ─────────────────────────────────────────────────────────
-- Housekeeping, when it starts to matter rather than now:
--   · staged files with no stored_file row (upload interrupted)
-- A query plus a REMOVE loop; the Admin page has a button for it.
