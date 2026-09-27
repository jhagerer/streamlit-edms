-- ─────────────────────────────────────────────────────────────────────────────
-- MiniDMS — privileges and Streamlit object (run after minidms-setup.sql)
--
-- Replace the <placeholders>. Verify the CREATE STREAMLIT options against the
-- current Snowflake documentation for Streamlit on the container runtime.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Owning role: what the app exercises, and deliberately nothing more ──────
-- No UPDATE / DELETE / TRUNCATE on the logs: append-only is enforced by the
-- database, not just by the code.
GRANT USAGE ON DATABASE <db> TO ROLE <app_owner_role>;
GRANT USAGE ON SCHEMA <db>.<schema> TO ROLE <app_owner_role>;
GRANT USAGE ON WAREHOUSE <wh> TO ROLE <app_owner_role>;
GRANT READ, WRITE ON STAGE <db>.<schema>.doc_files TO ROLE <app_owner_role>;
GRANT READ, WRITE ON STAGE <db>.<schema>.sessions  TO ROLE <app_owner_role>;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA <db>.<schema> TO ROLE <app_owner_role>;
GRANT SELECT ON VIEW <db>.<schema>.audit_v TO ROLE <app_owner_role>;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE <app_owner_role>;   -- AI_PARSE_DOCUMENT
-- Optional, for the Admin → Cost tab:
-- GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE <app_owner_role>;

-- ── PyPI access for the container runtime (once, as ACCOUNTADMIN) ───────────
-- CREATE NETWORK RULE pypi_rule MODE = EGRESS TYPE = HOST_PORT
--   VALUE_LIST = ('pypi.org', 'files.pythonhosted.org');
-- CREATE EXTERNAL ACCESS INTEGRATION pypi_eai
--   ALLOWED_NETWORK_RULES = (pypi_rule) ENABLED = TRUE;
-- GRANT USAGE ON INTEGRATION pypi_eai TO ROLE <app_owner_role>;

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
