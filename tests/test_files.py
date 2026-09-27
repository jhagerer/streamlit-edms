from datetime import datetime

import polars as pl

from core import files


def test_relative_path_keeps_safe_extension():
    assert files.relative_path("d", "f", "Scan 1.PDF") == "d/f.pdf"
    assert files.relative_path("d", "f", "noext") == "d/f"
    assert files.relative_path("d", "f", "evil.p'df") == "d/f"


def test_orphans_respects_references_and_age():
    staged = pl.DataFrame({
        "relative_path": ["a/1.pdf", "b/2.pdf", "c/3.pdf"],
        "size": [1, 1, 1],
        "last_modified": [datetime(2026, 1, 1), datetime(2026, 1, 1), datetime(2026, 1, 10)],
    })
    out = files.orphans(staged, {"@doc_files/a/1.pdf"}, min_age_days=2, now=datetime(2026, 1, 11))
    assert out["relative_path"].to_list() == ["b/2.pdf"]


def test_store_upload_plain_text(fake):
    res = files.store_upload(b"hello world", "note.txt", None, "doc1", "alice")
    assert res.error is None
    assert res.text_row["content"] == "hello world"
    assert res.file_row["checksum"] == files.sha256_hex(b"hello world")
    assert res.file_row["size"] == 11
    assert fake.stage[res.file_row["stage_path"].lstrip("@")] == b"hello world"


def test_store_upload_extraction_failure_keeps_file(fake):
    res = files.store_upload(b"%PDF-broken", "x.pdf", "application/pdf", "doc1", "alice")
    assert res.error  # fake session cannot run AI_PARSE_DOCUMENT
    assert res.text_row is None
    assert res.file_row["stage_path"].lstrip("@") in fake.stage
