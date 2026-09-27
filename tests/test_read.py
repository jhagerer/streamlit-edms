from datetime import datetime, timedelta

import polars as pl

from core import read, schema, write

T0 = datetime(2026, 1, 1, 12, 0, 0)


def doc(event_id, ts, op="create", document_id="d1", label="x"):
    return {"event_id": event_id, "event_ts": ts, "actor": "alice", "op": op,
            "document_id": document_id, "document_type_id": None, "label": label,
            "description": None, "language": None}


def frame(rows):
    return write.rows_frame("document_log", rows)


def test_latest_takes_last_event_per_key():
    log = frame([doc("a", T0, label="one"), doc("b", T0 + timedelta(seconds=1), "update", label="two"),
                 doc("c", T0, document_id="d2", label="other")])
    cur = read.latest(log, ["document_id"])
    assert dict(zip(cur["document_id"], cur["label"])) == {"d1": "two", "d2": "other"}


def test_latest_tie_break_on_event_id_is_deterministic():
    rows = [doc("b", T0, "update", label="B"), doc("a", T0, label="A")]
    for order in (rows, rows[::-1]):
        cur = read.latest(frame(order), ["document_id"])
        assert cur["label"].to_list() == ["B"]  # 'b' > 'a'


def test_trash_then_restore_ordering():
    log = frame([doc("1", T0), doc("2", T0 + timedelta(seconds=1), "trash"),
                 doc("3", T0 + timedelta(seconds=2), "restore")])
    assert read.latest(log, ["document_id"])["op"].to_list() == ["restore"]


def test_idempotent_double_flush_reduces_to_one_record():
    row = doc("same", T0)
    log = frame([row, row])
    assert read.latest(log, ["document_id"]).height == 1


def test_compose_marks_exactly_appended_rows_pending():
    committed = frame([doc("a", T0)])
    appended = frame([doc("b", T0, "update")])
    out = read.compose(committed, appended)
    assert out.height == 2
    assert out.filter(pl.col("_pending"))["event_id"].to_list() == ["b"]


def test_compose_with_empty_appends_is_committed_frame():
    committed = frame([doc("a", T0)])
    out = read.compose(committed, schema.empty("document_log"))
    assert out.drop("_pending").equals(committed)
    assert not out["_pending"].any()


def test_clean_restored():
    snap = frame([doc("a", T0), doc("b", T0)])
    committed = frame([doc("a", T0)])
    assert read.clean_restored(snap, committed)["event_id"].to_list() == ["b"]
    assert read.clean_restored(None, committed) is None
    assert read.clean_restored(schema.empty("document_log"), committed) is None
    assert read.clean_restored(snap, schema.empty("document_log")).height == 2
    # Result never contains committed rows.
    assert read.clean_restored(snap, snap).height == 0


def test_conform_pins_dtypes_and_order():
    raw = pl.DataFrame({"LABEL": ["x"], "EVENT_ID": ["e"], "DOCUMENT_ID": ["d"],
                        "EVENT_TS": [T0], "ACTOR": ["a"], "OP": ["create"], "EXTRA": [1]})
    out = schema.conform("document_log", raw)
    assert out.columns == schema.columns("document_log")
    assert out.schema["event_ts"] == pl.Datetime("us")
    assert out["description"].to_list() == [None]


def test_conform_empty_arrow_result():
    assert schema.conform("tag_log", pl.DataFrame()).height == 0
