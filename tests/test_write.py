from datetime import datetime

import polars as pl
import pytest

from core import read, schema, snapshot, write


def test_rows_frame_rejects_missing_column():
    row = write.tag_row("alice", "t1", "Red", "#ff0000")
    del row["color"]
    with pytest.raises(ValueError, match="missing columns"):
        write.rows_frame("tag_log", [row])


def test_rows_frame_rejects_extra_column():
    row = write.tag_row("alice", "t1", "Red", "#ff0000")
    row["_pending"] = True
    with pytest.raises(ValueError, match="unexpected columns"):
        write.rows_frame("tag_log", [row])


def test_builders_produce_valid_rows_for_every_table():
    u = "alice"
    rows = {
        "document_log": write.document_row(u, "create", "d", None, "L", None, None),
        "document_file_log": write.file_row(u, document_id="d", file_id="f", filename="a.pdf",
                                            stage_path="@doc_files/d/f.pdf", mimetype="application/pdf",
                                            size=1, checksum="00", page_count=None),
        "metadata_log": write.metadata_row(u, "d", "m", "v"),
        "tag_assignment_log": write.tag_assignment_row(u, "d", "t", True),
        "document_type_log": write.document_type_row(u, "dt", "Invoice"),
        "metadata_type_log": write.metadata_type_row(u, "m", "amount", "Amount", "number", None, None),
        "tag_log": write.tag_row(u, "t", "Red", "#ff0000"),
        "document_text": write.text_row(u, "f", "hello"),
    }
    assert set(rows) == set(schema.APPEND_TABLES)
    for table, row in rows.items():
        df = write.rows_frame(table, [row])
        assert df.columns == schema.columns(table)
        assert df.schema["event_ts"] == pl.Datetime("us")


def test_document_row_from_keeps_other_fields():
    cur = {"document_id": "d", "document_type_id": "t", "label": "old", "description": "desc",
           "language": "en", "op": "create"}
    row = write.document_row_from("bob", cur, "update", label="new")
    assert (row["label"], row["description"], row["op"], row["actor"]) == ("new", "desc", "update", "bob")


def test_parquet_round_trip_preserves_dtypes():
    df = write.rows_frame("document_file_log", [write.file_row(
        "alice", document_id="d", file_id="f", filename="a.pdf", stage_path="p",
        mimetype=None, size=10, checksum=None, page_count=None)])
    back = snapshot.from_parquet_bytes(snapshot.to_parquet_bytes(df))
    assert back.equals(df)
    assert back.schema == df.schema


def _pending_doc(user="alice"):
    return {"document_log": write.rows_frame(
        "document_log", [write.document_row(user, "create", write.new_id(), None, "L", None, None)])}


def test_save_to_session_overwrites(fake):
    first = _pending_doc()
    write.save_to_session("Alice", first)
    second = {"document_log": pl.concat([first["document_log"], _pending_doc()["document_log"]])}
    write.save_to_session("Alice", second)
    assert snapshot.read_snapshot("Alice", "document_log").height == 2


def test_restore_and_flush_cycle(fake):
    appends = _pending_doc()
    write.save_to_session("alice", appends)
    restored = read.restore("alice", read.change_tokens())
    assert restored["document_log"].height == 1

    write.flush("alice", appends)
    assert len(fake.tables["document_log"]) == 1
    assert not any(k.startswith("sessions/alice/") for k in fake.stage)
    assert read.restore("alice", read.change_tokens()) == {}


def test_flush_only_removes_own_folder(fake):
    write.save_to_session("alice", _pending_doc())
    write.save_to_session("alice2", _pending_doc("alice2"))
    write.flush("alice", {})
    assert any(k.startswith("sessions/alice2/") for k in fake.stage)
    assert not any(k.startswith("sessions/alice/") for k in fake.stage)


def test_interrupted_flush_heals(fake):
    appends = _pending_doc()
    write.save_to_session("alice", appends)
    fake.fail_remove = True
    with pytest.raises(RuntimeError):
        write.flush("alice", appends)
    assert len(fake.tables["document_log"]) == 1           # insert happened
    assert any(k.startswith("sessions/alice/") for k in fake.stage)  # snapshot survived
    assert read.restore("alice", read.change_tokens()) == {}  # nothing pending


def test_other_users_flush_does_not_touch_my_pending(fake):
    mine = _pending_doc("alice")
    write.save_to_session("alice", mine)
    write.flush("bob", _pending_doc("bob"))
    restored = read.restore("alice", read.change_tokens())
    assert restored["document_log"]["event_id"].to_list() == mine["document_log"]["event_id"].to_list()


def test_inserted_rows_load_with_pinned_dtypes(fake):
    write.flush("alice", _pending_doc())
    df = read.load_log("document_log", read.change_tokens()["document_log"])
    assert df.schema == schema.polars_schema("document_log")
    assert isinstance(df["event_ts"][0], datetime)


def test_document_text_is_never_bulk_loaded(fake):
    with pytest.raises(ValueError):
        read.load_log("document_text", "0")
