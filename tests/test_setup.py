import re

import pytest

from core import schema, setup


def test_split_ignores_semicolons_in_comments_and_strings():
    text = """-- a; comment
    CREATE STAGE IF NOT EXISTS a COMMENT = 'x; y';
    /* block; */ CREATE TABLE IF NOT EXISTS t (c VARCHAR) -- tail; here
    ;
    -- only a comment;
    """
    stmts = setup.split_sql(text)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE STAGE") and "'x; y'" in stmts[0]
    assert stmts[1].startswith("/* block; */ CREATE TABLE")


def test_split_keeps_dollar_quoted_bodies_whole():
    text = "CREATE PROCEDURE p() RETURNS VARCHAR LANGUAGE SQL AS\n$$\nBEGIN\n  RETURN 'a;b';\nEND;\n$$;\nSELECT 1;"
    stmts = setup.split_sql(text)
    assert len(stmts) == 2
    assert stmts[0].endswith("END;\n$$")


def test_setup_script_covers_every_object():
    stmts = setup.setup_statements()
    names = {(s.kind, s.name) for s in stmts}
    for kind, needed in setup.NEEDED.items():
        assert {(kind, n) for n in needed} <= names, kind
    assert all(s.kind for s in stmts), [s.sql[:40] for s in stmts if not s.kind]
    assert [st.key for st in setup.steps()] == ["warehouse", "stage", "hybrid", "standard",
                                                "views", "pipeline", "search"]
    assert setup.total_needed() == 1 + 1 + 7 + 6 + 12 + 1 + 1 + 1 + 1 + 1


def _columns(sql: str) -> list[str]:
    """Column names from a CREATE TABLE statement."""
    body = sql[sql.index("(") + 1: sql.rindex(")")]
    out = []
    for line in body.splitlines():
        line = line.split("--")[0].strip()
        m = re.match(r"([a-z_]+)\s+(VARCHAR|TIMESTAMP_NTZ|BOOLEAN|NUMBER|ARRAY)", line)
        if m:
            out.append(m.group(1))
    return out


def test_setup_sql_matches_the_app_schema():
    """sql/minidms-setup.sql and core/schema.py describe the same columns, and
    the right tables are hybrid."""
    stmts = {s.name: s for s in setup.setup_statements() if s.kind in ("hybrid", "table")}
    assert {n for n, s in stmts.items() if s.kind == "hybrid"} == set(schema.HYBRID_TABLES)
    assert {n for n, s in stmts.items() if s.kind == "table"} == set(schema.STANDARD_TABLES)
    for name, st in stmts.items():
        assert _columns(st.sql) == schema.columns(name), name


def test_every_table_is_append_only():
    """Hybrid logs: PRIMARY KEY (key…, version) + UNIQUE (event_id). Nothing in
    the script or the app updates or deletes rows."""
    stmts = {s.name: s.sql for s in setup.setup_statements() if s.kind == "hybrid"}
    for name, sql in stmts.items():
        keys = ", ".join(schema.LOG_KEYS[name])
        assert f"PRIMARY KEY ({keys}, version)" in sql, name
        assert "UNIQUE (event_id)" in sql, name
    text = setup.SETUP_SQL.read_text()
    code = "\n".join(x.sql for x in setup.setup_statements())
    assert not re.search(r"\bUPDATE\s+\w+\s+SET\b", code)
    assert not re.search(r"\bDELETE\s+FROM\b", code)
    assert not re.search(r"\bMERGE\s+INTO\b", code)
    assert "sync_state" not in text and "change_log" not in text


def test_setup_creates_an_adaptive_warehouse_only_if_missing():
    text = setup.SETUP_SQL.read_text()
    by = {(s.kind, s.name): s.sql for s in setup.setup_statements()}
    assert by[("warehouse", "minidms_wh")].startswith("CREATE ADAPTIVE WAREHOUSE IF NOT EXISTS")
    task = by[("task", "process_ai_task")]
    assert "WAREHOUSE = minidms_wh" in task
    assert "TARGET_COMPLETION_INTERVAL" not in text   # that is a serverless-task setting
    # No other (non-adaptive) warehouse is created anywhere in the script.
    assert not [x.sql for x in setup.setup_statements()
                if re.match(r"CREATE\s+(OR\s+REPLACE\s+)?WAREHOUSE", x.sql, re.I)]


def test_pipeline_is_triggered_and_lineage_friendly():
    stmts = setup.setup_statements()
    by = {(s.kind, s.name): s.sql for s in stmts}
    assert "IF NOT EXISTS" in by[("stream", "ai_request_stream")]           # keeps the offset
    task = by[("task", "process_ai_task")]
    assert task.startswith("CREATE OR REPLACE TASK process_ai_task")
    assert "SCHEDULE" not in task                                           # triggered
    # Clause order: COMMENT, WAREHOUSE, WHEN (task); COMMENT before EXECUTE AS (procedure).
    assert task.index("COMMENT") < task.index("WAREHOUSE") \
        < task.index("WHEN SYSTEM$STREAM_HAS_DATA('ai_request_stream')") < task.index("\nAS")
    proc = by[("procedure", "process_ai")]
    assert proc.index("COMMENT") < proc.index("EXECUTE AS OWNER") < proc.index("\nAS")
    assert "FROM ai_request_stream" in proc
    assert proc.count("'claude-sonnet-4-5'") >= 3
    assert "TO_FILE('@doc_files'" in proc and "response_format" in proc
    assert "AI_PARSE_DOCUMENT" not in proc
    # Lineage: AI output is written with INSERT … SELECT from its sources, never VALUES.
    assert "VALUES" not in proc
    ocr = proc[proc.index("'claude-sonnet-4-5', 'done',"):]
    assert ocr.index("AI_COMPLETE(") < ocr.index("FROM stored_file f")
    enrich = proc[proc.index("INSERT INTO document_enrichment"):]
    assert enrich.index("AI_COMPLETE(") < enrich.index("FROM document_text_current t")
    # The procedure only appends to standard tables.
    written = set(re.findall(r"INSERT INTO (\w+)", proc))
    assert written == {"ai_job_log", "document_text", "document_enrichment", "search_corpus"}
    assert written <= set(schema.STANDARD_TABLES)
    # The sink: text chunks and the summary go to search_corpus by INSERT … SELECT.
    sink = proc[proc.index("INSERT INTO search_corpus"):]
    assert "SPLIT_TEXT_RECURSIVE_CHARACTER" in sink and sink.index("SELECT") < sink.index(
        "FROM document_text_current t")
    order = [(s.kind, s.name) for s in stmts]
    assert order.index(("table", "ai_request")) < order.index(("stream", "ai_request_stream"))
    assert order.index(("task", "process_ai_task")) < order.index(("resume", "process_ai_task"))


def test_cortex_search_service_is_the_sink():
    by = {(s.kind, s.name): s.sql for s in setup.setup_statements()}
    svc = by[("search", "document_search")]
    assert svc.startswith("CREATE CORTEX SEARCH SERVICE IF NOT EXISTS document_search")
    assert "ON text" in svc and "ATTRIBUTES document_id, file_id, kind" in svc
    assert "WAREHOUSE = minidms_wh" in svc and "TARGET_LAG = '1 minute'" in svc
    assert "FROM search_corpus" in svc
    # Its source is a standard table with change tracking, written only by process_ai().
    corpus = by[("table", "search_corpus")]
    assert "CHANGE_TRACKING = TRUE" in corpus
    order = [(s.kind, s.name) for s in setup.setup_statements()]
    assert order.index(("table", "search_corpus")) < order.index(("search", "document_search"))


def test_no_file_bytes_in_tables_and_no_session_stage():
    """Files live in @doc_files; the tables only reference them. No @sessions:
    there is one global state, in the tables."""
    ddl = setup.SETUP_SQL.read_text().upper()
    assert "BINARY" not in ddl
    assert "STAGE IF NOT EXISTS SESSIONS" not in ddl


def test_missing():
    existing = {k: set(v) for k, v in setup.NEEDED.items()}
    existing["warehouse"] = set()
    existing["resume"] = set()
    got = setup.missing(existing)
    assert got["warehouse"] == ["minidms_wh"] and got["resume"] == ["process_ai_task"]
    assert got["hybrid"] == [] and got["table"] == [] and got["view"] == []


def test_grants_enforce_append_only():
    grants = [g.sql for g in setup.grant_statements("MINIDMS_APP", "DB", "S")]
    sql = " ".join(grants)
    assert "UPDATE" not in sql and "DELETE" not in sql            # append-only, enforced
    by_table = {g.split(" ON ")[1].split(" TO ")[0].split(".")[-1]: g for g in grants
                if " ON TABLE " in g}
    assert set(by_table) == set(schema.HYBRID_TABLES) | set(schema.STANDARD_TABLES)
    for t in schema.HYBRID_TABLES + ("stored_file", "ai_request"):
        assert by_table[t].startswith("GRANT SELECT, INSERT ON"), t
    for t in ("document_text", "document_enrichment", "search_corpus", "ai_job_log"):
        assert by_table[t].startswith("GRANT SELECT ON"), t     # written only by process_ai()
    assert 'GRANT USAGE ON CORTEX SEARCH SERVICE "DB"."S".document_search TO ROLE MINIDMS_APP' \
        in grants
    assert "GRANT USAGE ON WAREHOUSE MINIDMS_WH TO ROLE MINIDMS_APP" in grants
    with pytest.raises(ValueError):
        setup.grant_statements("x; DROP DATABASE y", "DB", "S")
    with pytest.raises(ValueError):
        setup.grant_statements("R", "DB", "S", "wh; --")
