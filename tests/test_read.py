from datetime import datetime

import polars as pl

from core import read, schema, search, write
from conftest import stored


def test_tokens_carry_scope_and_move_with_writes(fake):
    t1 = read.change_tokens()
    assert set(t1) == set(schema.TOKEN_TABLES)
    assert all(v.startswith("ACCT/ROLE/DB/SCHEMA|") for v in t1.values())
    write.create_definition("tag", "alice", {"label": "Red"})
    t2 = read.change_tokens()
    assert t2["tag"] != t1["tag"] and t2["change_log"] != t1["change_log"]
    assert t2["document"] == t1["document"]               # untouched tables keep their key


def test_load_pins_dtypes_and_is_keyed_on_tokens(fake):
    write.create_definition("tag", "alice", {"label": "Red"})
    tokens = read.change_tokens()
    df = read.load("tag", read.token_for("tag", tokens))
    assert df.schema == schema.polars_schema("tag")
    assert isinstance(df["created_at"][0], datetime) and df["active"][0] is True
    write.create_definition("tag", "alice", {"label": "Blue"})
    assert read.load("tag", read.token_for("tag", tokens)).height == 1   # same key: cached
    assert read.load("tag", read.token_for("tag", read.change_tokens())).height == 2


def test_views_and_history(fake):
    write.create_document("alice", document_id="d1", fields={"label": "X"},
                          files=[stored()], metadata={}, tag_ids=[])
    write.register_files("alice", [stored()])
    tokens = read.change_tokens()
    status = read.load("ai_status_v", read.token_for("ai_status_v", tokens))
    assert status["pending"].to_list() == [True]
    hist = read.load_history("d1", tokens["change_log"])
    assert hist["entity"].to_list() == ["document", "document_file"]


def test_search_covers_text_and_summary(fake):
    write.create_document("alice", document_id="d1", fields={"label": "X"},
                          files=[stored()], metadata={}, tag_ids=[])
    write.register_files("alice", [stored()])
    fake.conn.execute("INSERT INTO document_text (event_id, extracted_at, actor, file_id, status, "
                      "content) VALUES ('t1', '2026-01-01 00:00:00.000000', 'system:ai', 'f1', "
                      "'done', 'Rechnung über 42 EUR')")
    fake.conn.execute("INSERT INTO document_enrichment (event_id, created_at, actor, file_id, "
                      "document_id, status, summary) VALUES ('e1', '2026-01-01 00:00:01.000000', "
                      "'system:ai', 'f1', 'd1', 'done', 'An invoice from ACME')")
    assert search.search_ids("Rechnung") == ["d1"]
    assert search.search_ids("ACME", "ILIKE") == ["d1"]
    assert search.search_ids("nothing") == []
    assert search.search_ids("") == []


def test_conform_empty_and_strings():
    assert schema.conform("tag", pl.DataFrame()).height == 0
    df = schema.conform("document", pl.DataFrame({"DOCUMENT_ID": ["d"], "VERSION": [1],
                                                  "CREATED_AT": ["2026-01-01 10:00:00.000000"]}))
    assert df.columns == schema.columns("document")
    assert df["created_at"][0] == datetime(2026, 1, 1, 10)
