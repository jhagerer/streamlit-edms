"""Write commands against the SQLite-backed fake: every table append-only,
one transaction per action, (key, version) collisions as conflicts, event ids
as idempotency keys."""

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


def current(fake, doc_id="d1"):
    return fake.rows("SELECT * FROM document_current WHERE document_id = ?", [doc_id])[0]


def counts(fake):
    return {t: fake.count(t) for t in ("document_log", "document_file_log", "metadata_log",
                                       "tag_assignment_log")}


def test_create_document_appends_version_1_of_everything(fake):
    assert make_doc(fake, event_id="e1") is True
    d = current(fake)
    assert (d["label"], d["status"], d["version"], d["created_by"]) == ("Invoice 42", "active", 1, "alice")
    assert counts(fake) == {"document_log": 1, "document_file_log": 1, "metadata_log": 1,
                            "tag_assignment_log": 1}          # empty m2 is not stored
    ids = sorted(r["event_id"] for t in counts(fake) for r in fake.rows(f"SELECT event_id FROM {t}"))
    assert ids == ["e1", "e1:1", "e1:2", "e1:3"]


def test_retried_action_is_not_applied_twice(fake):
    assert make_doc(fake, event_id="e1") is True
    before = counts(fake)
    assert make_doc(fake, event_id="e1") is False             # same event id: already applied
    assert counts(fake) == before


def test_edits_append_versions_and_never_update(fake):
    make_doc(fake)
    write.update_document("alice", current(fake), {"label": "A"})
    write.update_document("alice", current(fake), {}, op="trash")
    write.update_document("alice", current(fake), {}, op="restore")
    log = fake.rows("SELECT version, op, label, status FROM document_log ORDER BY version")
    assert log == [
        {"version": 1, "op": "create", "label": "Invoice 42", "status": "active"},
        {"version": 2, "op": "update", "label": "A", "status": "active"},
        {"version": 3, "op": "trash", "label": "A", "status": "trashed"},
        {"version": 4, "op": "restore", "label": "A", "status": "active"},
    ]
    assert current(fake)["version"] == 4 and current(fake)["created_by"] == "alice"
    assert not any(q.lstrip().upper().startswith(("UPDATE", "DELETE")) for q in fake.queries)


def test_stale_version_is_a_conflict(fake):
    make_doc(fake)
    seen = current(fake)                                      # both users load version 1
    assert write.update_document("alice", seen, {"label": "A"}) is True
    with pytest.raises(Conflict):
        write.update_document("bob", seen, {"description": "B"})   # version 2 is taken
    d = current(fake)
    assert (d["label"], d["description"], d["version"]) == ("A", None, 2)
    assert fake.count("document_log") == 2                   # bob's row rolled back


def test_metadata_set_change_remove_and_conflicts(fake):
    make_doc(fake)
    assert write.set_metadata("alice", "d1", "m3", "x", None) is True
    with pytest.raises(Conflict):
        write.set_metadata("bob", "d1", "m3", "y", None)      # both thought it was empty
    assert write.set_metadata("alice", "d1", "m3", "z", 1) is True
    with pytest.raises(Conflict):
        write.set_metadata("bob", "d1", "m3", "w", 1)         # stale version
    assert write.set_metadata("alice", "d1", "m3", "", 2) is True     # remove = value NULL
    assert write.set_metadata("alice", "d1", "m3", "again", 3) is True
    chain = fake.rows("SELECT version, op, value FROM metadata_log WHERE metadata_type_id = 'm3' "
                      "ORDER BY version")
    assert chain == [{"version": 1, "op": "set", "value": "x"},
                     {"version": 2, "op": "set", "value": "z"},
                     {"version": 3, "op": "remove", "value": None},
                     {"version": 4, "op": "set", "value": "again"}]


def test_tags_attach_detach_atomic(fake):
    make_doc(fake)                                            # red: version 1, assigned
    assert write.set_tags("alice", "d1", {"blue": None}, {"red": 1}) is True
    cur = {r["tag_id"]: (r["version"], r["assigned"])
           for r in fake.rows("SELECT * FROM tag_assignment_current")}
    assert cur == {"red": (2, 0), "blue": (1, 1)}
    # Bob still saw red at version 1: his detach collides — and his attach is undone too.
    with pytest.raises(Conflict):
        write.set_tags("bob", "d1", {"green": None}, {"red": 1})
    assert fake.count("tag_assignment_log") == 3
    assert write.set_tags("alice", "d1", {"red": 2}, {}) is True   # re-attach continues the chain


def test_source_is_recorded_for_applied_ai_suggestions(fake):
    make_doc(fake)
    write.set_metadata("alice", "d1", "m9", "ACME", None, source="ai:enr1")
    assert fake.rows("SELECT source FROM metadata_log WHERE metadata_type_id = 'm9'") == [
        {"source": "ai:enr1"}]


def test_file_detach_with_version(fake):
    make_doc(fake)
    f = fake.rows("SELECT * FROM document_file_current")[0]
    assert write.set_file_active("alice", f, False) is True
    with pytest.raises(Conflict):
        write.set_file_active("bob", f, True)
    assert fake.rows("SELECT active, version FROM document_file_current") == [
        {"active": 0, "version": 2}]


def test_register_files_records_fact_and_requests_ai(fake):
    make_doc(fake)
    write.register_files("alice", [stored()])
    assert fake.count("stored_file") == 1
    req = fake.rows("SELECT * FROM ai_request")
    assert len(req) == 1 and req[0]["reason"] == "upload" and req[0]["file_id"] == "f1"
    assert write.request_ai("bob", [("f1", "d1")]) is True
    assert fake.count("ai_request") == 2


def test_definitions_create_update_conflict(fake):
    tag_id = write.create_definition("tag", "alice", {"label": "Red", "color": "#f00"})
    t = fake.rows("SELECT * FROM tag_current")[0]
    assert (t["tag_id"], t["active"], t["version"]) == (tag_id, 1, 1)
    assert write.update_definition("tag", "alice", t, {"active": False}) is True
    with pytest.raises(Conflict):
        write.update_definition("tag", "bob", t, {"label": "Rot"})
    after = fake.rows("SELECT label, color, active, version FROM tag_current")[0]
    assert after == {"label": "Red", "color": "#f00", "active": 0, "version": 2}
    with pytest.raises(ValueError):
        write.create_definition("metadata_type", "alice", {"name": "x", "label": "X",
                                                           "data_type": "money"})
    with pytest.raises(ValueError):
        write.update_definition("tag", "alice", t, {"secret": 1})
