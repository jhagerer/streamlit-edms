import pytest

from core import setup
from core.schema import APPEND_TABLES


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


def test_setup_script_covers_every_object():
    stmts = setup.setup_statements()
    names = {(s.kind, s.name) for s in stmts}
    assert {("stage", n) for n in setup.STAGES} <= names
    assert {("table", t) for t in APPEND_TABLES} <= names
    assert ("view", "audit_v") in names
    assert all(s.kind for s in stmts), [s.sql[:40] for s in stmts if not s.kind]
    assert [st.key for st in setup.steps()] == ["stages", "tables", "view"]


def test_missing():
    got = setup.missing({"stage": {"doc_files"}, "table": set(APPEND_TABLES), "view": set()})
    assert got == {"stage": ["sessions"], "table": [], "view": ["audit_v"]}


def test_grants_validate_role_and_never_grant_update_or_delete():
    sql = " ".join(g.sql for g in setup.grant_statements("MINIDMS_APP", "DB", "S", "WH"))
    assert "UPDATE" not in sql and "DELETE" not in sql
    assert 'ON TABLE "DB"."S".document_log' in sql
    with pytest.raises(ValueError):
        setup.grant_statements("x; DROP DATABASE y", "DB", "S")
    with pytest.raises(ValueError):
        setup.grant_statements("R", "DB", "S", "wh; --")
