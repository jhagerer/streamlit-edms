from datetime import datetime, timedelta

import polars as pl

from core import model, schema

T = datetime(2026, 1, 1)


def frame(table, rows):
    return schema.conform(table, pl.DataFrame(rows, infer_schema_length=None))


def audit(i=0):
    return {"created_at": T + timedelta(seconds=i), "created_by": "alice",
            "updated_at": T + timedelta(seconds=i), "updated_by": "alice"}


def ai_status(rows):
    return frame("ai_status_v", rows)


def test_files_view_ai_states():
    links = frame("document_file", [
        {"file_id": f, "document_id": "d1", "active": a, "version": 1, **audit(i)}
        for i, (f, a) in enumerate([("new", True), ("q", True), ("ok", True), ("bad", True),
                                    ("skip", True), ("gone", False)])
    ])
    stored = frame("stored_file", [{"file_id": f, "document_id": "d1", "filename": f"{f}.pdf",
                                    "stage_path": f"@doc_files/x/{f}.pdf"}
                                   for f in ("new", "q", "ok", "bad", "skip", "gone")])
    status = ai_status([
        {"file_id": "q", "requested_at": T, "pending": True},
        {"file_id": "ok", "requested_at": T, "text_status": "done", "enrich_status": "done",
         "pending": False},
        {"file_id": "bad", "requested_at": T, "text_status": "done", "enrich_status": "failed",
         "pending": False},
        {"file_id": "skip", "requested_at": T, "text_status": "skipped",
         "enrich_status": "skipped", "pending": False},
    ])
    out = model.files_view(links, stored, status)
    assert dict(zip(out["file_id"], out["ai_state"])) == {
        "new": "missing", "q": "queued", "ok": "done", "bad": "failed", "skip": "skipped"}
    assert "gone" in model.files_view(links, stored, status, include_inactive=True)["file_id"]


def test_document_table_joins_and_filters():
    docs = frame("document", [
        {"document_id": "d1", "label": "Invoice 42", "document_type_id": "t1", "status": "active",
         "version": 3, **audit(1)},
        {"document_id": "d2", "label": "Letter", "status": "trashed", "version": 2, **audit(2)},
    ])
    types = frame("document_type", [{"document_type_id": "t1", "label": "Invoice", "active": True,
                                     "version": 1, **audit()}])
    files = pl.DataFrame({"document_id": ["d1"], "filename": ["scan.pdf"], "ai_state": ["queued"]})
    meta = frame("document_metadata", [{"document_id": "d1", "metadata_type_id": "m1",
                                        "value": "ACME", "version": 1}])
    doc_tags = frame("document_tag", [{"document_id": "d1", "tag_id": "red"},
                                      {"document_id": "d1", "tag_id": "old"}])
    tags = frame("tag", [{"tag_id": "red", "label": "Red", "active": True, "version": 1, **audit()},
                         {"tag_id": "old", "label": "Old", "active": False, "version": 2, **audit()}])
    enrich = frame("document_enrichment_current", [
        {"event_id": "e1", "created_at": T, "file_id": "f1", "document_id": "d1", "status": "done",
         "summary": "Invoice from ACME for 42 EUR"}])
    table = model.document_table(docs, types, files, meta, doc_tags, tags, enrich)
    d1 = table.filter(pl.col("document_id") == "d1").row(0, named=True)
    assert d1["document_type"] == "Invoice" and d1["tags"] == ["Red"]   # inactive tag hidden
    assert d1["files"] == 1 and d1["ai_state"] == "queued" and d1["version"] == 3
    assert d1["summary"].startswith("Invoice from ACME")
    assert model.filter_documents(table)["document_id"].to_list() == ["d1"]
    assert model.filter_documents(table, trashed=True)["document_id"].to_list() == ["d2"]
    assert model.filter_documents(table, text="42 eur").height == 1     # matches the summary
    assert model.filter_documents(table, text="scan.pdf").height == 1
    assert model.filter_documents(table, tag_ids=["red"]).height == 1
    assert model.filter_documents(table, ids=[]).height == 0


def test_suggestions_only_valid_and_different():
    enr = {"status": "done", "suggested_type_id": "t2",
           "suggested_tag_ids": '["red", "ghost", "blue"]',
           "extracted_metadata": '[{"metadata_type_id": "amount", "value": "42"},'
                                 ' {"metadata_type_id": "status", "value": "maybe"},'
                                 ' {"metadata_type_id": "date", "value": "2026-01-05"},'
                                 ' {"metadata_type_id": "unknown", "value": "x"}]'}
    mts = {"amount": {"data_type": "number", "active": True},
           "status": {"data_type": "choice", "choices": "open\ndone", "active": True},
           "date": {"data_type": "date", "active": True}}
    s = model.suggestions(enr, {"document_type_id": "t1"}, ["blue"], {"date": "2026-01-05"},
                          {"t1", "t2"}, {"red", "blue"}, mts)
    assert s == {"document_type_id": "t2", "tag_ids": ["red"], "metadata": {"amount": "42"}}
    assert model.has_suggestions(s)
    none = model.suggestions({"status": "failed"}, {}, [], {}, set(), set(), {})
    assert not model.has_suggestions(none)
