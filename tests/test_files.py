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
    assert files.remove_statement(f"{SHA}/derived/page-0001.pdf") == (
        f"REMOVE @doc_files/{SHA}/derived/ PATTERN = '.*/page\\\\-0001\\\\.pdf$'")
    assert files.remove_statement(f"{SHA}/other/x.pdf") is None


def test_orphans_respects_references_and_age():
    staged = pl.DataFrame({
        "relative_path": ["a/1.pdf", "b/2.pdf", "c/3.pdf"],
        "size": [1, 1, 1],
        "last_modified": [datetime(2026, 1, 1), datetime(2026, 1, 1), datetime(2026, 1, 10)],
    })
    out = files.orphans(staged, {"@doc_files/a/1.pdf"}, min_age_days=2, now=datetime(2026, 1, 11))
    assert out["relative_path"].to_list() == ["b/2.pdf"]


def test_derived_pages_belong_to_their_checksum_folder():
    a, b = "a" * 64, "b" * 64
    staged = pl.DataFrame({
        "relative_path": [f"{a}/x.png", f"{a}/derived/converted.pdf", f"{a}/derived/page-0001.pdf",
                          f"{b}/derived/page-0001.pdf"],
        "size": [1] * 4,
        "last_modified": [datetime(2026, 1, 1)] * 4,
    })
    out = files.orphans(staged, {f"@doc_files/{a}/x.png"}, min_age_days=2,
                        now=datetime(2026, 1, 11))
    assert out["relative_path"].to_list() == [f"{b}/derived/page-0001.pdf"]


def test_stage_upload_is_content_addressed_and_writes_no_rows(fake):
    row = files.stage_upload(b"%PDF-1.7 hello", "Invoice 42.pdf", "application/pdf", "doc1")
    checksum = files.sha256_hex(b"%PDF-1.7 hello")
    assert row["checksum"] == checksum and row["size"] == 14 and row["document_id"] == "doc1"
    assert row["stage_path"] == f"@doc_files/{checksum}/Invoice_42.pdf"
    assert row["filename"] == "Invoice 42.pdf"          # original name kept for stored_file
    assert fake.stage[f"doc_files/{checksum}/Invoice_42.pdf"] == b"%PDF-1.7 hello"
    assert fake.count("stored_file") == 0               # rows come with the document write
    assert not any("AI_COMPLETE" in q for q in fake.queries)   # AI runs inside Snowflake


def test_same_content_same_name_is_stored_once(fake):
    r1 = files.stage_upload(b"same", "a.txt", None, "d1")
    r2 = files.stage_upload(b"same", "a.txt", None, "d2")
    assert r1["stage_path"] == r2["stage_path"] and r1["file_id"] != r2["file_id"]
    assert len([k for k in fake.stage if k.startswith("doc_files/")]) == 1


def test_ai_extensions_match_the_procedure():
    """The app's lists and the ones in convert_to_pdf_pages() must agree."""
    import ast
    import re

    from core import setup

    proc = {s.name: s.sql for s in setup.setup_statements() if s.kind == "procedure"}
    code = proc["convert_to_pdf_pages"]
    for name in ("PDF_EXTENSIONS", "IMAGE_EXTENSIONS", "TEXT_EXTENSIONS"):
        literal = re.search(rf"^{name} = (\{{.*\}})$", code, re.M).group(1)
        assert ast.literal_eval(literal) == getattr(files, name), name
