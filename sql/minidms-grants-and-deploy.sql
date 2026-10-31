-- ─────────────────────────────────────────────────────────────────────────────
-- MiniDMS — privileges and Streamlit object (run after minidms-setup.sql)
--
-- Replace the <placeholders>. Verify the CREATE STREAMLIT options against the
-- current Snowflake documentation for Streamlit on the container runtime.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Owner of the schema objects (creates them by running minidms-setup.sql) ──
-- The AI task, the procedure and the search service run with this role's
-- rights, so Cortex, warehouse and task privileges belong here.
GRANT CREATE WAREHOUSE ON ACCOUNT TO ROLE <schema_owner_role>;           -- minidms_wh, if it does not exist yet
GRANT USAGE ON DATABASE <db> TO ROLE <schema_owner_role>;
GRANT USAGE, CREATE TABLE, CREATE HYBRID TABLE, CREATE VIEW, CREATE STAGE, CREATE STREAM,
      CREATE TASK, CREATE FUNCTION, CREATE PROCEDURE, CREATE CORTEX SEARCH SERVICE
      ON SCHEMA <db>.<schema> TO ROLE <schema_owner_role>;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE <schema_owner_role>;   -- AI_COMPLETE
-- As ACCOUNTADMIN: run and resume tasks.
GRANT EXECUTE TASK ON ACCOUNT TO ROLE <schema_owner_role>;
-- The procedure convert_to_pdf_pages() uses pypdf, Pillow and reportlab from
-- Snowflake's Anaconda channel: an ORGADMIN accepts the Anaconda terms once
-- (Snowsight → Admin → Billing & Terms).

-- ── App role (if different from the owner): what the app exercises, no more ──
-- Every table is append-only, and the grants enforce it: SELECT and INSERT,
-- never UPDATE or DELETE. Any warehouse works for the app.
GRANT USAGE ON DATABASE <db> TO ROLE <app_role>;
GRANT USAGE ON SCHEMA <db>.<schema> TO ROLE <app_role>;
GRANT USAGE ON WAREHOUSE minidms_wh TO ROLE <app_role>;
GRANT READ, WRITE ON STAGE <db>.<schema>.doc_files TO ROLE <app_role>;

-- Hybrid tables people edit: each change appends a version.
GRANT SELECT, INSERT ON TABLE <db>.<schema>.document_type_log  TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.metadata_type_log  TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.tag_log            TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.document_log       TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.document_file_log  TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.metadata_log       TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.tag_assignment_log TO ROLE <app_role>;

-- Standard tables: the app appends uploads and AI requests, and only reads AI output.
GRANT SELECT, INSERT ON TABLE <db>.<schema>.stored_file         TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.ai_request          TO ROLE <app_role>;
GRANT SELECT         ON TABLE <db>.<schema>.pdf_conversion      TO ROLE <app_role>;
GRANT SELECT         ON TABLE <db>.<schema>.pdf_page            TO ROLE <app_role>;
GRANT SELECT         ON TABLE <db>.<schema>.page_ocr            TO ROLE <app_role>;
-- Hybrid table written only by the pipeline: the app reads the AI result.
GRANT SELECT         ON TABLE <db>.<schema>.document_enrichment TO ROLE <app_role>;
GRANT SELECT         ON TABLE <db>.<schema>.ai_job_log          TO ROLE <app_role>;
GRANT SELECT ON ALL VIEWS IN SCHEMA <db>.<schema> TO ROLE <app_role>;
-- Search page: query the Cortex Search service.
GRANT USAGE ON CORTEX SEARCH SERVICE <db>.<schema>.document_search TO ROLE <app_role>;
-- Admin page: see the task and start a run on demand.
GRANT MONITOR, OPERATE ON TASK <db>.<schema>.process_ai_task TO ROLE <app_role>;
-- Optional, for the Admin → Cost tab:
-- GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE <app_role>;

-- ── PyPI access for the container runtime (once, as ACCOUNTADMIN) ───────────
-- CREATE NETWORK RULE pypi_rule MODE = EGRESS TYPE = HOST_PORT
--   VALUE_LIST = ('pypi.org', 'files.pythonhosted.org');
-- CREATE EXTERNAL ACCESS INTEGRATION pypi_eai
--   ALLOWED_NETWORK_RULES = (pypi_rule) ENABLED = TRUE;
-- GRANT USAGE ON INTEGRATION pypi_eai TO ROLE <app_role>;

-- ── The app (if not created from a Workspace) ───────────────────────────────
-- The app runs in <db>.<schema>: the stage and tables above are resolved
-- relative to that schema, so create the app in the same schema.
-- CREATE STREAMLIT <db>.<schema>.minidms
--   FROM '@<db>.<schema>.<source_stage_or_repo>/<path>'
--   MAIN_FILE = 'streamlit_app.py'
--   QUERY_WAREHOUSE = minidms_wh
--   RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'
--   COMPUTE_POOL = <compute_pool>
--   EXTERNAL_ACCESS_INTEGRATIONS = (pypi_eai)
--   TITLE = 'MiniDMS';

-- Check the runtime and pool after deploying:
-- DESC STREAMLIT <db>.<schema>.minidms;

-- ── Viewers: the app only, no table or stage grants ─────────────────────────
-- GRANT USAGE ON DATABASE <db> TO ROLE <viewer_role>;
-- GRANT USAGE ON SCHEMA <db>.<schema> TO ROLE <viewer_role>;
-- GRANT USAGE ON STREAMLIT <db>.<schema>.minidms TO ROLE <viewer_role>;
