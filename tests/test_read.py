from datetime import datetime

import polars as pl

from conftest import stored
from core import read, schema, search, write


def test_tokens_are_row_counts_and_move_with_writes(fake):
    t1 = read.change_tokens()
    assert set(t1) == set(schema.TOKEN_TABLES)
    assert all(v.startswith("ACCT/ROLE/DB/SCHEMA|") for v in t1.values())
    write.create_definition("tag", "alice", {"label": "Red"})
    t2 = read.change_tokens()
    assert t1["tag_log"].endswith("|0") and t2["tag_log"].endswith("|1")
    assert t2["document_log"] == t1["document_log"]          # untouched tables keep their key


def test_load_pins_dtypes_and_is_keyed_on_tokens(fake):
    write.create_definition("tag", "alice", {"label": "Red"})
    tokens = read.change_tokens()
    df = read.load("tag_current", read.token_for("tag_current", tokens))
    assert df.schema == schema.polars_schema("tag_current")
    assert isinstance(df["created_at"][0], datetime) and df["active"][0] is True
    write.create_definition("tag", "alice", {"label": "Blue"})
    assert read.load("tag_current", read.token_for("tag_current", tokens)).height == 1  # cached
    assert read.load("tag_current", read.token_for("tag_current", read.change_tokens())).height == 2


def test_views_and_history(fake):
    write.create_document("alice", document_id="d1", fields={"label": "X"},
                          files=[stored()], metadata={"m1": "v"}, tag_ids=[])
    write.register_files("alice", [stored()])
    tokens = read.change_tokens()
    status = read.load("ai_status_v", read.token_for("ai_status_v", tokens))
    assert status["pending"].to_list() == [True]
    hist = read.load_history("d1", read.history_token(tokens))
    assert sorted(hist["entity"].to_list()) == ["document", "file", "metadata"]
    assert hist.filter(pl.col("entity") == "metadata")["value"].to_list() == ["v"]


def _pages(fake, run, file_id, doc_id, summaries, ts, status="done"):
    for i, text in enumerate(summaries, start=1):
        fake.conn.execute(
            "INSERT INTO page_ocr (event_id, created_at, run_id, file_id, document_id, filename, "
            "page_no, status, ocr_text, page_short_summary, index_text) "
            "VALUES (?, ?, ?, ?, ?, 'a.pdf', ?, ?, 'raw text', ?, ?)",
            [f"{run}-{i}", ts, run, file_id, doc_id, i, status, text, text])


def test_cortex_search_request_and_ranking(fake):
    _pages(fake, "r1", "f1", "d1", ["Deckblatt", "Rechnung über 42 EUR von ACME"],
           "2026-01-01 00:00:00.000000")
    _pages(fake, "r2", "f2", "d2", ["ACME Vertrag"], "2026-01-01 00:00:00.000000")
    # f2 was processed again: only the latest run counts.
    _pages(fake, "r3", "f2", "d2", ["Kündigung"], "2026-01-02 00:00:00.000000")
    hits = search.search("ACME Rechnung", "DB", "SCHEMA")
    assert [(h.document_id, h.page_no) for h in hits] == [("d1", 2)]   # one hit per page
    assert search.best_snippets(hits)["d1"] == "p. 2: Rechnung über 42 EUR von ACME"
    req = fake.search_requests[-1]
    assert req["query"] == "ACME Rechnung" and req["columns"] == search.COLUMNS
    sql = [q for q in fake.queries if "SEARCH_PREVIEW" in q][-1]
    assert '"DB"."SCHEMA".document_search' in sql
    assert search.search("  ", "DB", "SCHEMA") == []


def test_search_query_cannot_break_out_of_the_literal(fake):
    nasty = "x') ; DROP TABLE document_log; -- \\' \""
    search.search(nasty, "DB", "SCHEMA")
    assert fake.search_requests[-1]["query"] == nasty.strip()
    assert fake.count("document_log") == 0                   # table still there


def test_ranking_helpers():
    hits = [search.Hit("d2", "f2", 4, "b  " * 200), search.Hit("d1", "f1", None, "a"),
            search.Hit("d2", "f3", 1, "c"), search.Hit("d3", "gone", 1, "d")]
    assert search.ranked_document_ids(hits, {"f1", "f2", "f3"}) == ["d2", "d1"]
    snips = search.best_snippets(hits, width=20)
    assert snips["d1"] == "a" and len(snips["d2"]) == 20 and snips["d2"].startswith("p. 4: b")
    assert snips["d2"].endswith("…")


def test_conform_empty_and_strings():
    assert schema.conform("tag_current", pl.DataFrame()).height == 0
    df = schema.conform("document_current", pl.DataFrame({
        "DOCUMENT_ID": ["d"], "VERSION": [1], "CREATED_AT": ["2026-01-01 10:00:00.000000"]}))
    assert df.columns == schema.columns("document_current")
    assert df["created_at"][0] == datetime(2026, 1, 1, 10)
