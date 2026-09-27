-- ─────────────────────────────────────────────────────────────────────────────
-- MiniDMS — privileges and Streamlit object (run after minidms-setup.sql)
--
-- Replace the <placeholders>. Verify the CREATE STREAMLIT options against the
-- current Snowflake documentation for Streamlit on the container runtime.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Owner of the schema objects (creates them by running minidms-setup.sql) ──
-- The text-extraction task and procedure run with this role's rights
-- (EXECUTE AS OWNER), so Cortex and task privileges belong here.
GRANT USAGE ON DATABASE <db> TO ROLE <schema_owner_role>;
GRANT USAGE, CREATE TABLE, CREATE VIEW, CREATE STAGE, CREATE STREAM, CREATE TASK,
      CREATE PROCEDURE ON SCHEMA <db>.<schema> TO ROLE <schema_owner_role>;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE <schema_owner_role>;   -- AI_PARSE_DOCUMENT
-- As ACCOUNTADMIN: run and resume tasks; serverless tasks need MANAGED.
GRANT EXECUTE TASK, EXECUTE MANAGED TASK ON ACCOUNT TO ROLE <schema_owner_role>;
-- If you give the task a WAREHOUSE instead of running it serverless:
-- GRANT USAGE ON WAREHOUSE <wh> TO ROLE <schema_owner_role>;

-- ── App role (if different from the owner): what the app exercises, no more ──
-- No UPDATE / DELETE / TRUNCATE: append-only is enforced by the database, not
-- just by the code. document_text and ocr_log are written only by the task,
-- so the app role may only read them.
GRANT USAGE ON DATABASE <db> TO ROLE <app_role>;
GRANT USAGE ON SCHEMA <db>.<schema> TO ROLE <app_role>;
GRANT USAGE ON WAREHOUSE <wh> TO ROLE <app_role>;
GRANT READ, WRITE ON STAGE <db>.<schema>.doc_files TO ROLE <app_role>;
GRANT READ, WRITE ON STAGE <db>.<schema>.sessions  TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.document_log       TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.document_file_log  TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.metadata_log       TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.tag_assignment_log TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.document_type_log  TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.metadata_type_log  TO ROLE <app_role>;
GRANT SELECT, INSERT ON TABLE <db>.<schema>.tag_log            TO ROLE <app_role>;
GRANT SELECT ON TABLE <db>.<schema>.document_text TO ROLE <app_role>;
GRANT SELECT ON TABLE <db>.<schema>.ocr_log       TO ROLE <app_role>;
GRANT SELECT ON VIEW  <db>.<schema>.audit_v       TO ROLE <app_role>;
GRANT SELECT ON VIEW  <db>.<schema>.ocr_status_v  TO ROLE <app_role>;
-- Admin page: see the task and start a run on demand.
GRANT MONITOR, OPERATE ON TASK <db>.<schema>.extract_text_task TO ROLE <app_role>;
-- Optional, for the Admin → Cost tab:
-- GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE <app_role>;

-- ── PyPI access for the container runtime (once, as ACCOUNTADMIN) ───────────
-- CREATE NETWORK RULE pypi_rule MODE = EGRESS TYPE = HOST_PORT
--   VALUE_LIST = ('pypi.org', 'files.pythonhosted.org');
-- CREATE EXTERNAL ACCESS INTEGRATION pypi_eai
--   ALLOWED_NETWORK_RULES = (pypi_rule) ENABLED = TRUE;
-- GRANT USAGE ON INTEGRATION pypi_eai TO ROLE <app_role>;

-- ── The app (if not created from a Workspace) ───────────────────────────────
-- The app runs in <db>.<schema>: the stages and tables above are resolved
-- relative to that schema, so create the app in the same schema.
-- CREATE STREAMLIT <db>.<schema>.minidms
--   FROM '@<db>.<schema>.<source_stage_or_repo>/<path>'
--   MAIN_FILE = 'streamlit_app.py'
--   QUERY_WAREHOUSE = <wh>
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
