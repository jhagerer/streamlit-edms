-- ─────────────────────────────────────────────────────────────────────────────
-- MiniDMS — setup
--
-- Everything the app needs: a warehouse (created only if missing), one stage,
-- seven hybrid tables, eight standard tables, a dozen views, the AI pipeline
-- (one stream, three procedures, one triggered task) and one Cortex Search
-- service.
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
--                     requests, the PDF pages, the structured OCR per page,
--                     the text and the enrichments. The AI output is written
--                     by INSERT … SELECT inside Snowflake, so Snowflake records
--                     column-level lineage from the page file to the OCR
--                     fields, the text, the summary and the suggestions.
--   Cortex Search   — the sink of the ingest pipeline: every file is converted
--                     to PDF and split into pages, every page gets a
--                     structured OCR (AI_COMPLETE, claude-haiku-4-5), and the
--                     service indexes one search document per page.
--
-- Idempotent. Tables, the stream and the search service use IF NOT EXISTS so
-- re-running never drops data, resets the stream offset or rebuilds the index;
-- views, the procedure and the task use OR REPLACE because they hold no state
-- (the ALTER TASK … RESUME at the end starts the task again).
--
-- Privileges the owning role needs: CREATE WAREHOUSE (account, only if
-- minidms_wh does not exist), CREATE TABLE / HYBRID TABLE / VIEW / STAGE /
-- STREAM / TASK / PROCEDURE / CORTEX SEARCH SERVICE (schema), EXECUTE TASK
-- (account) and the SNOWFLAKE.CORTEX_USER database role. The Python procedure
-- uses pypdf, Pillow and reportlab from Snowflake's Anaconda channel (accept the
-- Anaconda terms once per account).
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
-- extension stays on the name because the converter picks the format from it.
-- Derived files (the document as PDF, one PDF per page) are written by the
-- pipeline to @doc_files/{sha256}/derived/ — same content, same files.
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

-- Per processing run: the file converted to PDF (PDFs pass through) by
-- convert_to_pdf_pages(). One row per run and file.
CREATE TABLE IF NOT EXISTS pdf_conversion (
    event_id    VARCHAR       NOT NULL,
    created_at  TIMESTAMP_NTZ NOT NULL,
    run_id      VARCHAR       NOT NULL,       -- the ai_request it came from
    file_id     VARCHAR       NOT NULL,
    status      VARCHAR       NOT NULL,       -- done | failed | skipped
    converter   VARCHAR,                      -- pdf | image | text
    pdf_path    VARCHAR,                      -- the whole document as PDF
    page_count  NUMBER,
    error       VARCHAR
)   COMMENT = 'Append-only. Conversion of each file to PDF, per processing run.';

-- One single-page PDF per page, written to @doc_files/{checksum}/derived/.
-- The unit of OCR and of the search index.
CREATE TABLE IF NOT EXISTS pdf_page (
    event_id    VARCHAR       NOT NULL,
    created_at  TIMESTAMP_NTZ NOT NULL,
    run_id      VARCHAR       NOT NULL,
    file_id     VARCHAR       NOT NULL,
    page_no     NUMBER        NOT NULL,       -- 1-based
    stage_path  VARCHAR       NOT NULL        -- '@doc_files/{checksum}/derived/page-0001.pdf'
)   COMMENT = 'Append-only. Single-page PDFs per file and processing run.';

-- Structured OCR per page from AI_COMPLETE (claude-haiku-4-5). Written by
-- INSERT … SELECT FROM pdf_page, so every column is lineage-linked to the page
-- file. Images are described at length inside ocr_text; index_text is
-- everything except ocr_text and is what Cortex Search indexes. The sink of
-- the ingest pipeline and the source of the document_search service.
CREATE TABLE IF NOT EXISTS page_ocr (
    event_id                   VARCHAR       NOT NULL,
    created_at                 TIMESTAMP_NTZ NOT NULL,
    run_id                     VARCHAR       NOT NULL,
    file_id                    VARCHAR       NOT NULL,
    document_id                VARCHAR       NOT NULL,
    filename                   VARCHAR,
    page_no                    NUMBER        NOT NULL,
    model                      VARCHAR,
    status                     VARCHAR       NOT NULL,  -- done | failed
    ocr_text                   VARCHAR,                 -- full text, images as [Image: …]
    headings_list              ARRAY,
    image_short_summaries_list ARRAY,
    table_summaries_list       ARRAY,
    page_short_summary         VARCHAR,
    index_text                 VARCHAR,                 -- all fields except ocr_text
    error                      VARCHAR
)   CHANGE_TRACKING = TRUE
    COMMENT = 'Append-only. Structured OCR per page; indexed by the document_search service.';

-- The OCR text of the whole file: the pages of one run in order. Written by
-- INSERT … SELECT FROM page_ocr. Input of the enrichment and the Text tab.
CREATE TABLE IF NOT EXISTS document_text (
    event_id     VARCHAR       NOT NULL,
    extracted_at TIMESTAMP_NTZ NOT NULL,
    actor        VARCHAR       NOT NULL,      -- 'system:ai'
    file_id      VARCHAR       NOT NULL,
    model        VARCHAR,
    status       VARCHAR       NOT NULL,      -- done | failed | skipped
    pages        NUMBER,
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
       t.pages,
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
UNION ALL SELECT created_at,   'system:ai', 'convert_' || status, 'pdf_conversion',     NULL,        file_id FROM pdf_conversion
UNION ALL SELECT extracted_at, actor,       'ocr_' || status,    'document_text',       NULL,        file_id FROM document_text
UNION ALL SELECT created_at,   actor,       'enrich_' || status, 'document_enrichment', document_id, file_id FROM document_enrichment;

-- ── AI pipeline: stream + procedures + triggered task ────────────────────────
--
-- Saving an upload (or "process again") appends to ai_request. The stream sees
-- it, which fires the triggered task on minidms_wh, which calls process_ai():
--   1. consume the stream: log every new request as 'queued' (this INSERT is
--      what advances the stream offset);
--   2. per queued request:
--      a. pages — reuse the page OCR of an identical file (same checksum, not
--         on "process again"), or
--         · convert_to_pdf_pages(): convert the file to PDF and split it into
--           single-page PDFs on the stage (Python: pypdf, Pillow, reportlab);
--         · ocr_pages(): AI_COMPLETE (claude-haiku-4-5) with a JSON schema on
--           every page file, as INSERT … SELECT FROM pdf_page — the pages run
--           in parallel. If that statement fails, the pages are retried one by
--           one so one bad page gets a 'failed' row instead of failing the file;
--      b. text — the pages' ocr_text in order, as INSERT … SELECT FROM page_ocr;
--      c. enrichment — AI_COMPLETE (claude-sonnet-4-5) with a JSON schema over
--         the text and the current definitions, as INSERT … SELECT FROM
--         document_text;
--      d. log 'finished'.
--   page_ocr is the sink: the Cortex Search service indexes its index_text.
--   One failing file never blocks the others: it gets 'failed' rows with the
--   error message. Only INSERTs: append-only.

CREATE STREAM IF NOT EXISTS ai_request_stream
    ON TABLE ai_request
    APPEND_ONLY = TRUE
    COMMENT = 'New AI requests, consumed by process_ai()';

-- Converts one file to PDF and writes one single-page PDF per page to
-- @doc_files/{checksum}/derived/. PDF passes through; images become one page
-- per frame; plain text is typeset. Other types are 'skipped'. Records the
-- result in pdf_conversion and the pages in pdf_page. Keep the extension
-- lists in sync with core/files.py.
CREATE OR REPLACE PROCEDURE convert_to_pdf_pages(run_id VARCHAR, file_id VARCHAR)
    RETURNS VARCHAR
    LANGUAGE PYTHON
    RUNTIME_VERSION = '3.11'
    PACKAGES = ('snowflake-snowpark-python', 'pypdf', 'pillow', 'reportlab')
    HANDLER = 'run'
    COMMENT = 'Converts a stored file to PDF and splits it into single-page PDFs.'
    EXECUTE AS OWNER
AS
$$
import io
import os

from snowflake.snowpark.files import SnowflakeFile

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".xml"}


def image_to_pdf(data):
    from PIL import Image, ImageSequence
    img = Image.open(io.BytesIO(data))
    frames = [f.convert("RGB") for f in ImageSequence.Iterator(img)]
    out = io.BytesIO()
    frames[0].save(out, "PDF", save_all=True, append_images=frames[1:], resolution=150)
    return out.getvalue()


def text_to_pdf(data):
    from xml.sax.saxutils import escape
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    text = data.decode("utf-8", errors="replace").expandtabs(4)
    style = ParagraphStyle("mono", fontName="Courier", fontSize=9, leading=11)
    story = []
    for line in text.splitlines():
        body = line.lstrip(" ")
        indent = "&nbsp;" * (len(line) - len(body))   # keep indentation, wrap the rest
        story.append(Paragraph(indent + escape(body), style) if body.strip() else Spacer(1, 11))
    story = story or [Spacer(1, 11)]
    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=A4).build(story)
    return out.getvalue()


def run(session, run_id, file_id):
    def record(status, converter=None, pdf_path=None, pages=None, error=None):
        session.sql(
            "INSERT INTO pdf_conversion (event_id, created_at, run_id, file_id, status, "
            "converter, pdf_path, page_count, error) "
            "SELECT UUID_STRING(), SYSDATE(), ?, ?, ?, ?, ?, ?, ?",
            params=[run_id, file_id, status, converter, pdf_path, pages, error]).collect()
        return status

    rows = session.sql("SELECT stage_path, checksum FROM stored_file WHERE file_id = ? LIMIT 1",
                       params=[file_id]).collect()
    if not rows:
        return record("failed", error="unknown file")
    stage_path, checksum = rows[0][0], rows[0][1]
    ext = os.path.splitext(stage_path)[1].lower()
    try:
        with SnowflakeFile.open(stage_path, "rb", require_scoped_url=False) as fh:
            data = fh.read()
        folder = f"@doc_files/{checksum}/derived"
        if ext in PDF_EXTENSIONS or data[:5] == b"%PDF-":
            converter, pdf, pdf_path = "pdf", data, stage_path
        elif ext in IMAGE_EXTENSIONS:
            converter, pdf, pdf_path = "image", image_to_pdf(data), f"{folder}/converted.pdf"
        elif ext in TEXT_EXTENSIONS:
            converter, pdf, pdf_path = "text", text_to_pdf(data), f"{folder}/converted.pdf"
        else:
            return record("skipped", error=f"no PDF conversion for {ext or 'this file type'}")

        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted:
            reader.decrypt("")
        if pdf_path != stage_path:
            session.file.put_stream(io.BytesIO(pdf), pdf_path, auto_compress=False,
                                    overwrite=True)
        pages = []
        for n, page in enumerate(reader.pages, start=1):
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            path = f"{folder}/page-{n:04d}.pdf"
            buf.seek(0)
            session.file.put_stream(buf, path, auto_compress=False, overwrite=True)
            pages.append((run_id, file_id, n, path))
        if not pages:
            return record("skipped", converter, pdf_path, 0, "the PDF has no pages")
        session.create_dataframe(pages, schema=["run_id", "file_id", "page_no", "stage_path"]) \
            .select_expr("UUID_STRING() AS event_id", "SYSDATE() AS created_at",
                         "run_id", "file_id", "page_no", "stage_path") \
            .write.mode("append").save_as_table("pdf_page", column_order="name")
        return record("done", converter, pdf_path, len(pages))
    except Exception as exc:
        return record("failed", error=str(exc)[:2000])
$$;

-- Structured OCR of the pages p_from..p_to of one run: one AI_COMPLETE call
-- per page file, all in one INSERT … SELECT FROM pdf_page (lineage).
CREATE OR REPLACE PROCEDURE ocr_pages(p_run VARCHAR, p_file VARCHAR, p_doc VARCHAR,
                                      p_from NUMBER, p_to NUMBER)
    RETURNS NUMBER
    LANGUAGE SQL
    COMMENT = 'Structured OCR (AI_COMPLETE, claude-haiku-4-5) of single-page PDFs.'
    EXECUTE AS OWNER
AS
$$
BEGIN
    INSERT INTO page_ocr (event_id, created_at, run_id, file_id, document_id, filename, page_no,
                          model, status, ocr_text, headings_list, image_short_summaries_list,
                          table_summaries_list, page_short_summary, index_text, error)
    SELECT UUID_STRING(), SYSDATE(), y.run_id, y.file_id, :p_doc, y.filename, y.page_no,
           'claude-haiku-4-5', IFF(y.ocr_text IS NULL, 'failed', 'done'),
           y.ocr_text, y.headings, y.images, y.tables, y.page_summary,
           -- What Cortex Search indexes: every field except ocr_text.
           NULLIF(ARRAY_TO_STRING(ARRAY_CONSTRUCT_COMPACT(
               NULLIF(y.page_summary, ''),
               NULLIF(ARRAY_TO_STRING(y.headings, '\n'), ''),
               NULLIF(ARRAY_TO_STRING(y.images, '\n'), ''),
               NULLIF(ARRAY_TO_STRING(y.tables, '\n'), '')), '\n'), ''),
           IFF(y.ocr_text IS NULL, 'No valid structured output', NULL)
    FROM (
        SELECT x.run_id, x.file_id, x.filename, x.page_no,
               x.j:ocr_text::VARCHAR                   AS ocr_text,
               x.j:headings_list::ARRAY                AS headings,
               x.j:image_short_summaries_list::ARRAY   AS images,
               x.j:table_summaries_list::ARRAY         AS tables,
               x.j:page_short_summary::VARCHAR         AS page_summary
        FROM (
            SELECT p.run_id, p.file_id, f.filename, p.page_no,
                   TRY_PARSE_JSON(TO_VARCHAR(AI_COMPLETE(
                       model => 'claude-haiku-4-5',
                       prompt => 'This file is one page of a document. Transcribe it (OCR) and '
                           || 'describe it, in the language of the page.\n'
                           || 'ocr_text: all text on the page exactly as written, in reading '
                           || 'order; tables as Markdown tables. Replace every image, photo, '
                           || 'chart, diagram, logo, stamp or signature with a long description '
                           || 'at its position, written as [Image: …] — what it shows, any text '
                           || 'or numbers in it, and what it means in context.\n'
                           || 'headings_list: the headings on the page, in order.\n'
                           || 'image_short_summaries_list: one short sentence per image.\n'
                           || 'table_summaries_list: per table, what it contains and its key '
                           || 'figures, in one or two sentences.\n'
                           || 'page_short_summary: what the page is about, in at most two '
                           || 'sentences.\n'
                           || 'Use empty lists when there is nothing of a kind. Return only the '
                           || 'JSON object.',
                       file => TO_FILE('@doc_files', REGEXP_REPLACE(p.stage_path, '^@doc_files/', '')),
                       model_parameters => {'temperature': 0, 'max_tokens': 16000},
                       response_format => {
                           'type': 'json',
                           'schema': {
                               'type': 'object',
                               'properties': {
                                   'ocr_text': {'type': 'string'},
                                   'headings_list': {'type': 'array', 'items': {'type': 'string'}},
                                   'image_short_summaries_list': {'type': 'array',
                                                                  'items': {'type': 'string'}},
                                   'table_summaries_list': {'type': 'array',
                                                            'items': {'type': 'string'}},
                                   'page_short_summary': {'type': 'string'}
                               },
                               'required': ['ocr_text', 'headings_list',
                                            'image_short_summaries_list',
                                            'table_summaries_list', 'page_short_summary']
                           }
                       }))) AS j
            FROM pdf_page p
            JOIN stored_file f ON f.file_id = p.file_id
            WHERE p.run_id = :p_run AND p.file_id = :p_file
              AND p.page_no BETWEEN :p_from AND :p_to
        ) x
    ) y;
    RETURN SQLROWCOUNT;
END;
$$;

CREATE OR REPLACE PROCEDURE process_ai()
    RETURNS VARCHAR
    LANGUAGE SQL
    COMMENT = 'Page conversion, page OCR, text and enrichment (AI_COMPLETE) for queued AI requests.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    v_done    INTEGER DEFAULT 0;
    v_failed  INTEGER DEFAULT 0;
    v_req     VARCHAR;
    v_fid     VARCHAR;
    v_doc     VARCHAR;
    v_chk     VARCHAR;
    v_reason  VARCHAR;
    v_status  VARCHAR;
    v_page    INTEGER;
    v_n       INTEGER;
    v_msg     VARCHAR;
    pages     RESULTSET;
    queued CURSOR FOR
        SELECT q.request_id, q.file_id, q.document_id, f.checksum, r.reason
        FROM ai_job_log q
        JOIN stored_file f ON f.file_id = q.file_id
        LEFT JOIN ai_request r ON r.event_id = q.request_id
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
        v_reason := r.reason;

        -- 2a. Pages
        BEGIN
            v_n := 0;
            -- Same bytes already processed completely? Copy the page OCR, no AI
            -- call. Not on "process again": that is a request for a new result.
            IF (COALESCE(v_reason, '') <> 'retry') THEN
                INSERT INTO page_ocr (event_id, created_at, run_id, file_id, document_id, filename,
                                      page_no, model, status, ocr_text, headings_list,
                                      image_short_summaries_list, table_summaries_list,
                                      page_short_summary, index_text, error)
                SELECT UUID_STRING(), SYSDATE(), :v_req, :v_fid, :v_doc, f.filename, o.page_no,
                       o.model, o.status, o.ocr_text, o.headings_list,
                       o.image_short_summaries_list, o.table_summaries_list,
                       o.page_short_summary, o.index_text, o.error
                FROM page_ocr o
                JOIN stored_file f ON f.file_id = :v_fid
                WHERE o.run_id = (
                    SELECT o2.run_id
                    FROM page_ocr o2
                    JOIN stored_file t ON t.file_id = o2.file_id
                    WHERE t.checksum = :v_chk AND o2.file_id <> :v_fid
                    GROUP BY o2.run_id
                    HAVING COUNT_IF(o2.status <> 'done') = 0
                    ORDER BY MAX(o2.created_at) DESC
                    LIMIT 1);
                v_n := SQLROWCOUNT;
            END IF;
            IF (v_n = 0) THEN
                CALL convert_to_pdf_pages(:v_req, :v_fid);
                v_status := (SELECT MAX(status) FROM pdf_conversion
                             WHERE run_id = :v_req AND file_id = :v_fid);
                IF (v_status = 'done') THEN
                    BEGIN
                        CALL ocr_pages(:v_req, :v_fid, :v_doc, 1, 1000000);
                    EXCEPTION
                        WHEN OTHER THEN
                            -- Page by page, so one bad page does not fail the rest.
                            pages := (SELECT page_no FROM pdf_page
                                      WHERE run_id = :v_req AND file_id = :v_fid
                                      ORDER BY page_no);
                            LET c CURSOR FOR pages;
                            FOR p IN c DO
                                v_page := p.page_no;
                                BEGIN
                                    CALL ocr_pages(:v_req, :v_fid, :v_doc, :v_page, :v_page);
                                EXCEPTION
                                    WHEN OTHER THEN
                                        v_msg := SQLERRM;
                                        INSERT INTO page_ocr (event_id, created_at, run_id, file_id,
                                                              document_id, filename, page_no, model,
                                                              status, error)
                                        SELECT UUID_STRING(), SYSDATE(), :v_req, :v_fid, :v_doc,
                                               f.filename, :v_page, 'claude-haiku-4-5', 'failed',
                                               :v_msg
                                        FROM stored_file f WHERE f.file_id = :v_fid;
                                END;
                            END FOR;
                    END;
                END IF;
            END IF;

            -- 2b. Text: the pages in order, or why there are none.
            INSERT INTO document_text (event_id, extracted_at, actor, file_id, model, status,
                                       pages, content, error)
            SELECT UUID_STRING(), SYSDATE(), 'system:ai', :v_fid, MAX(o.model),
                   IFF(COUNT_IF(o.status <> 'done') > 0, 'failed', 'done'),
                   COUNT(*),
                   LISTAGG('--- Page ' || o.page_no || ' ---\n' || COALESCE(o.ocr_text, ''), '\n\n')
                       WITHIN GROUP (ORDER BY o.page_no),
                   IFF(COUNT_IF(o.status <> 'done') > 0,
                       COUNT_IF(o.status <> 'done') || ' of ' || COUNT(*) || ' pages failed: '
                       || ANY_VALUE(o.error), NULL)
            FROM page_ocr o
            WHERE o.run_id = :v_req AND o.file_id = :v_fid
            HAVING COUNT(*) > 0;
            v_n := SQLROWCOUNT;
            IF (v_n = 0) THEN
                INSERT INTO document_text (event_id, extracted_at, actor, file_id, model, status,
                                           pages, error)
                SELECT UUID_STRING(), SYSDATE(), 'system:ai', :v_fid, NULL,
                       COALESCE(MAX(c.status), 'failed'), 0,
                       COALESCE(MAX(c.error), 'no pages')
                FROM pdf_conversion c
                WHERE c.run_id = :v_req AND c.file_id = :v_fid;
            END IF;
        EXCEPTION
            WHEN OTHER THEN
                v_msg := SQLERRM;
                INSERT INTO document_text (event_id, extracted_at, actor, file_id, model, status, error)
                SELECT UUID_STRING(), SYSDATE(), 'system:ai', :v_fid, 'claude-haiku-4-5', 'failed', :v_msg;
        END;

        -- 2c. Enrichment: summary, type + tag suggestions, metadata extraction.
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
                WHERE t.file_id = :v_fid AND LENGTH(t.content) > 0
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
-- One search document per page: index_text, i.e. the page summary, headings,
-- image summaries and table summaries (not the raw OCR text). Only the latest
-- successful run per file is indexed; the service keeps itself up to date
-- (TARGET_LAG). Hybrid keyword + semantic search; the multilingual embedding
-- model handles German and English. Type, tag and trash filters are applied in
-- the app (they live in hybrid tables).
CREATE CORTEX SEARCH SERVICE IF NOT EXISTS document_search
    ON index_text
    ATTRIBUTES document_id, file_id
    WAREHOUSE = minidms_wh
    TARGET_LAG = '1 minute'
    EMBEDDING_MODEL = 'snowflake-arctic-embed-l-v2.0'
    COMMENT = 'MiniDMS search over the structured OCR of every page'
AS (
    SELECT index_text, document_id, file_id, filename, page_no, page_short_summary
    FROM page_ocr
    WHERE status = 'done'
    QUALIFY run_id = FIRST_VALUE(run_id) OVER (PARTITION BY file_id ORDER BY created_at DESC)
);

-- ── Later, if needed ─────────────────────────────────────────────────────────
-- Housekeeping, when it starts to matter rather than now:
--   · staged files with no stored_file row (upload interrupted)
-- A query plus a REMOVE loop; the Admin page has a button for it.
