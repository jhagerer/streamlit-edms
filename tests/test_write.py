"""Write commands against the SQLite-backed fake: one transaction per action,
version checks, idempotent event ids, counters."""

import json

import pytest

from conftest import stored
from core import write
from core.db import Conflict


def make_doc(fake, doc_id="d1", **kw):
    args = dict(document_id=doc_id, fields={"label": "Invoice 42", "document_type_id": None},
                files=[stored(document_id=doc_id)], metadata={"m1": "12.5", "m2": ""},
                tag_ids=["red"])
    args.update(kw)
    return write.create_document("alice", **args)


def doc_row(fake, doc_id="d1"):
    return fake.rows("SELECT * FROM document WHERE document_id = ?", [doc_id])[0]


def test_create_document_is_one_transaction_with_history(fake):
    before = fake.seq("document")
    assert make_doc(fake, event_id="e1") is True
    d = doc_row(fake)
    assert (d["label"], d["status"], d["version"], d["created_by"]) == ("Invoice 42", "active", 1, "alice")
    assert fake.count("document_file") == 1
    assert fake.rows("SELECT value FROM document_metadata") == [{"value": "12.5"}]  # '' skipped
    assert fake.count("document_tag") == 1
    log = fake.rows("SELECT event_id, entity, op FROM change_log ORDER BY event_id")
    assert [r["event_id"] for r in log] == ["e1", "e1:1", "e1:2", "e1:3"]
    assert {r["entity"] for r in log} == {"document", "document_file", "document_metadata",
                                          "document_tag"}
    assert fake.seq("document") == before + 1 and fake.seq("change_log") == 1


def test_retried_action_is_not_applied_twice(fake):
    assert make_doc(fake, event_id="e1") is True
    seq = fake.seq("document")
    assert make_doc(fake, event_id="e1") is False      # same event id: already applied
    assert fake.count("change_log") == 4 and fake.count("document") == 1
    assert fake.seq("document") == seq                  # rolled back, counters untouched


def test_update_with_stale_version_is_a_conflict(fake):
    make_doc(fake)
    seen = doc_row(fake)                                 # both users load version 1
    assert write.update_document("alice", seen, {"label": "A"}) is True
    with pytest.raises(Conflict):
        write.update_document("bob", seen, {"description": "B"})
    d = doc_row(fake)
    assert (d["label"], d["description"], d["version"]) == ("A", None, 2)
    assert fake.count("change_log") == 5                 # bob's log row rolled back too


def test_trash_and_restore(fake):
    make_doc(fake)
    write.update_document("alice", doc_row(fake), {}, op="trash")
    assert doc_row(fake)["status"] == "trashed"
    write.update_document("alice", doc_row(fake), {}, op="restore")
    assert doc_row(fake)["status"] == "active"
    ops = [r["op"] for r in fake.rows("SELECT op FROM change_log WHERE entity = 'document' "
                                      "ORDER BY event_ts, event_id")]
    assert ops == ["create", "trash", "restore"]


def test_metadata_set_change_remove_and_conflicts(fake):
    make_doc(fake)
    # m3 had no value when two users looked at it; both try to set it.
    assert write.set_metadata("alice", "d1", "m3", "x", None) is True
    with pytest.raises(Conflict):
        write.set_metadata("bob", "d1", "m3", "y", None)
    assert write.set_metadata("alice", "d1", "m3", "z", 1) is True
    with pytest.raises(Conflict):
        write.set_metadata("bob", "d1", "m3", "w", 1)    # stale version
    assert write.set_metadata("alice", "d1", "m3", "", 2) is True   # remove
    assert fake.rows("SELECT * FROM document_metadata WHERE metadata_type_id = 'm3'") == []


def test_tags_attach_detach_atomic(fake):
    make_doc(fake)
    assert write.set_tags("alice", "d1", ["blue"], ["red"]) is True
    assert {r["tag_id"] for r in fake.rows("SELECT tag_id FROM document_tag")} == {"blue"}
    # Detaching a tag that is gone conflicts — and the attach in the same action is undone.
    with pytest.raises(Conflict):
        write.set_tags("bob", "d1", ["green"], ["red"])
    assert {r["tag_id"] for r in fake.rows("SELECT tag_id FROM document_tag")} == {"blue"}
    with pytest.raises(Conflict):
        write.set_tags("bob", "d1", ["blue"], [])         # double attach: primary key


def test_source_is_recorded_for_applied_ai_suggestions(fake):
    make_doc(fake)
    write.set_metadata("alice", "d1", "m9", "ACME", None, source="ai:enr1")
    data = json.loads(fake.rows("SELECT data FROM change_log WHERE entity_id = 'd1/m9'")[0]["data"])
    assert data == {"metadata_type_id": "m9", "value": "ACME", "source": "ai:enr1"}


def test_file_detach_with_version(fake):
    make_doc(fake)
    f = fake.rows("SELECT * FROM document_file")[0]
    assert write.set_file_active("alice", f, False) is True
    with pytest.raises(Conflict):
        write.set_file_active("bob", f, True)
    assert fake.rows("SELECT active, version FROM document_file") == [{"active": 0, "version": 2}]


def test_register_files_records_fact_and_requests_ai(fake):
    make_doc(fake)
    write.register_files("alice", [stored()])
    assert fake.count("stored_file") == 1
    req = fake.rows("SELECT * FROM ai_request")
    assert len(req) == 1 and req[0]["reason"] == "upload" and req[0]["file_id"] == "f1"
    assert fake.seq("stored_file") == 1 and fake.seq("ai_request") == 1
    assert write.request_ai("bob", [("f1", "d1")]) is True
    assert fake.count("ai_request") == 2 and fake.seq("ai_request") == 2


def test_definitions_create_update_conflict(fake):
    tag_id = write.create_definition("tag", "alice", {"label": "Red", "color": "#f00"})
    t = fake.rows("SELECT * FROM tag")[0]
    assert (t["tag_id"], t["active"], t["version"]) == (tag_id, 1, 1)
    assert write.update_definition("tag", "alice", t, {"active": False}) is True
    with pytest.raises(Conflict):
        write.update_definition("tag", "bob", t, {"label": "Rot"})
    with pytest.raises(ValueError):
        write.create_definition("metadata_type", "alice", {"name": "x", "label": "X",
                                                           "data_type": "money"})
    with pytest.raises(ValueError):
        write.update_definition("tag", "alice", t, {"secret": 1})
