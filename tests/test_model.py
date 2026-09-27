from datetime import datetime, timedelta

import polars as pl

from core import model, read, schema, write

T = datetime(2026, 1, 1)


def fr(table, rows):
    return read.compose(write.rows_frame(table, rows), None)


def stamp(row, seconds):
    row["event_ts"] = T + timedelta(seconds=seconds)
    return row


def test_document_table_joins_and_filters():
    u = "alice"
    docs = fr("document_log", [
        stamp(write.document_row(u, "create", "d1", "t1", "Invoice 42", None, None), 0),
        stamp(write.document_row(u, "create", "d2", None, "Letter", None, None), 1),
        stamp(write.document_row(u, "trash", "d2", None, "Letter", None, None), 2),
    ])
    types = fr("document_type_log", [write.document_type_row(u, "t1", "Invoice")])
    files = fr("document_file_log", [write.file_row(u, document_id="d1", file_id="f1", filename="scan.pdf",
                                                    stage_path="p", mimetype=None, size=1, checksum=None,
                                                    page_count=1)])
    meta = fr("metadata_log", [stamp(write.metadata_row(u, "d1", "m1", "ACME"), 0),
                               stamp(write.metadata_row(u, "d1", "m2", "x"), 0),
                               stamp(write.metadata_row(u, "d1", "m2", ""), 1)])
    links = fr("tag_assignment_log", [stamp(write.tag_assignment_row(u, "d1", "red", True), 0),
                                      stamp(write.tag_assignment_row(u, "d1", "blue", True), 0),
                                      stamp(write.tag_assignment_row(u, "d1", "blue", False), 1)])
    tags = fr("tag_log", [write.tag_row(u, "red", "Red", "#f00"), write.tag_row(u, "blue", "Blue", "#00f")])
    appended = write.rows_frame("tag_assignment_log", [write.tag_assignment_row(u, "d1", "blue", True)])
    links = pl.concat([links, read.compose(schema.empty("tag_assignment_log"), appended)], how="vertical_relaxed")

    table = model.document_table(docs, types, files, meta, links, tags)
    d1 = table.filter(pl.col("document_id") == "d1").row(0, named=True)
    assert d1["document_type"] == "Invoice"
    assert sorted(d1["tags"]) == ["Blue", "Red"]
    assert d1["files"] == 1 and d1["pending"] is True
    assert d1["metadata_text"] == "ACME"

    active = model.filter_documents(table)
    assert active["document_id"].to_list() == ["d1"]
    assert model.filter_documents(table, trashed=True)["document_id"].to_list() == ["d2"]
    assert model.filter_documents(table, text="acme").height == 1
    assert model.filter_documents(table, text="scan.pdf").height == 1
    assert model.filter_documents(table, tag_ids=["red", "blue"]).height == 1
    assert model.filter_documents(table, document_type_ids=["nope"]).height == 0
    assert model.filter_documents(table, ids=[]).height == 0


def test_definitions_hide_inactive():
    u = "alice"
    log = fr("tag_log", [stamp(write.tag_row(u, "t", "Old", "#000"), 0),
                         stamp(write.tag_row(u, "t", "Old", "#000", active=False), 1)])
    assert model.definitions(log, "tag_log").height == 0
    assert model.definitions(log, "tag_log", include_inactive=True).height == 1
