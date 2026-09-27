from datetime import datetime

import polars as pl
import pytest

from core import files


SHA = "ab" * 32


def test_relative_path_is_checksum_and_safe_filename():
    assert files.relative_path(SHA, "Scan 1.PDF") == f"{SHA}/Scan_1.pdf"
    assert files.relative_path(SHA, "../../etc/passwd") == f"{SHA}/passwd"
    assert files.relative_path(SHA, "Rechnung März (2).pdf") == f"{SHA}/Rechnung_M_rz_2.pdf"
    assert files.relative_path(SHA, "noext") == f"{SHA}/noext"
    with pytest.raises(ValueError):
        files.relative_path("not-a-hash", "a.pdf")


def test_remove_statement_is_exact_and_only_for_managed_paths():
    stmt = files.remove_statement(f"{SHA}/a.pd")
    assert stmt == f"REMOVE @doc_files/{SHA}/ PATTERN = '.*/a\\\\.pd$'"
    assert files.remove_statement("../x") is None
    assert files.remove_statement(f"{SHA}/it's.pdf") is None


def test_orphans_respects_references_and_age():
    staged = pl.DataFrame({
        "relative_path": ["a/1.pdf", "b/2.pdf", "c/3.pdf"],
        "size": [1, 1, 1],
        "last_modified": [datetime(2026, 1, 1), datetime(2026, 1, 1), datetime(2026, 1, 10)],
    })
    out = files.orphans(staged, {"@doc_files/a/1.pdf"}, min_age_days=2, now=datetime(2026, 1, 11))
    assert out["relative_path"].to_list() == ["b/2.pdf"]


def test_store_upload_stages_content_addressed_without_ocr(fake):
    row = files.store_upload(b"%PDF-1.7 hello", "Invoice 42.pdf", "application/pdf", "doc1", "alice")
    checksum = files.sha256_hex(b"%PDF-1.7 hello")
    assert row["checksum"] == checksum and row["size"] == 14
    assert row["stage_path"] == f"@doc_files/{checksum}/Invoice_42.pdf"
    assert row["filename"] == "Invoice 42.pdf"          # original name kept in the log
    assert fake.stage[f"doc_files/{checksum}/Invoice_42.pdf"] == b"%PDF-1.7 hello"
    assert not any("AI_PARSE_DOCUMENT" in q for q in fake.queries)  # OCR runs in Snowflake


def test_same_content_same_name_is_stored_once(fake):
    r1 = files.store_upload(b"same", "a.txt", None, "d1", "alice")
    r2 = files.store_upload(b"same", "a.txt", None, "d2", "bob")
    assert r1["stage_path"] == r2["stage_path"] and r1["file_id"] != r2["file_id"]
    assert len([k for k in fake.stage if k.startswith("doc_files/")]) == 1


def test_resubmit_row_is_identical_snapshot():
    f = {"document_id": "d", "file_id": "f", "filename": "a.pdf", "stage_path": "p",
         "mimetype": "application/pdf", "size": 1, "checksum": SHA, "page_count": None,
         "event_id": "old"}
    again = files.resubmit_row(f, "bob")
    assert again["file_id"] == "f" and again["active"] is True and again["event_id"] != "old"
