import pytest

from core import setup


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
    assert [st.key for st in setup.steps()] == ["stages", "tables", "views", "pipeline"]
    assert setup.total_needed() == 2 + 9 + 2 + 1 + 1 + 1 + 1


def test_pipeline_objects_are_idempotent_and_ordered():
    stmts = setup.setup_statements()
    by = {(s.kind, s.name): s.sql for s in stmts}
    assert "IF NOT EXISTS" in by[("stream", "document_file_log_stream")]  # keeps the offset
    task = by[("task", "extract_text_task")]
    assert task.startswith("CREATE OR REPLACE TASK extract_text_task")
    assert "SCHEDULE" not in task                                        # triggered, not scheduled
    assert "TARGET_COMPLETION_INTERVAL = '15 MINUTE'" in task
    # Clause order: COMMENT before WHEN (task), COMMENT before EXECUTE AS (procedure).
    assert task.index("COMMENT") < task.index("TARGET_COMPLETION_INTERVAL") \
        < task.index("WHEN SYSTEM$STREAM_HAS_DATA('document_file_log_stream')") < task.index("\nAS")
    proc = by[("procedure", "extract_text")]
    assert proc.index("COMMENT") < proc.index("EXECUTE AS OWNER") < proc.index("\nAS")
    assert "AI_PARSE_DOCUMENT(TO_FILE('@doc_files'" in proc
    assert "FROM document_file_log_stream" in proc
    assert "UPDATE " not in proc and "DELETE " not in proc                # append-only
    order = [(s.kind, s.name) for s in stmts]
    assert order.index(("table", "document_file_log")) < order.index(("stream", "document_file_log_stream"))
    assert order.index(("task", "extract_text_task")) < order.index(("resume", "extract_text_task"))


def test_no_file_bytes_in_tables():
    """Files live in @doc_files; the tables only reference them."""
    ddl = setup.SETUP_SQL.read_text().upper()
    assert "BINARY" not in ddl


def test_missing():
    existing = {k: set(v) for k, v in setup.NEEDED.items()}
    existing["stage"] = {"doc_files"}
    existing["resume"] = set()
    got = setup.missing(existing)
    assert got["stage"] == ["sessions"] and got["resume"] == ["extract_text_task"]
    assert got["table"] == [] and got["view"] == []


def test_grants_validate_role_and_never_grant_update_or_delete():
    grants = [g.sql for g in setup.grant_statements("MINIDMS_APP", "DB", "S", "WH")]
    sql = " ".join(grants)
    assert "UPDATE" not in sql and "DELETE" not in sql
    # The app only reads what the extraction task writes.
    assert 'GRANT SELECT ON TABLE "DB"."S".document_text TO ROLE MINIDMS_APP' in grants
    assert not any("INSERT" in g and ("document_text" in g or "ocr_log" in g) for g in grants)
    assert 'ON TABLE "DB"."S".document_log' in sql
    with pytest.raises(ValueError):
        setup.grant_statements("x; DROP DATABASE y", "DB", "S")
    with pytest.raises(ValueError):
        setup.grant_statements("R", "DB", "S", "wh; --")
