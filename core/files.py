"""@doc_files: immutable uploads plus inline text extraction.

File bytes land in the stage immediately at upload; the log rows wait for
"Save to database". A discarded session therefore leaves orphaned files, which
the admin page can clean up.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import polars as pl

from core import write
from core.session import cache_data, get_session

STAGE = "@doc_files"

# Formats AI_PARSE_DOCUMENT accepts.
CORTEX_EXTENSIONS = {".pdf", ".docx", ".pptx", ".jpeg", ".jpg", ".png", ".tif", ".tiff",
                     ".html", ".htm", ".txt"}
# Read directly, no Cortex call needed.
PLAIN_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".xml", ".log", ".yaml", ".yml"}

PARSE_MODE = os.environ.get("MINIDMS_PARSE_MODE", "OCR")  # OCR | LAYOUT


@dataclass
class UploadResult:
    file_row: dict
    text_row: dict | None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ""


def relative_path(document_id: str, file_id: str, filename: str) -> str:
    # The extension is kept: AI_PARSE_DOCUMENT detects the format from it.
    return f"{document_id}/{file_id}{safe_ext(filename)}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def guess_mimetype(filename: str, declared: str | None = None) -> str:
    return declared or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def put_file(data: bytes, rel_path: str, session=None) -> str:
    session = session or get_session()
    stage_path = f"{STAGE}/{rel_path}"
    session.file.put_stream(io.BytesIO(data), stage_path, auto_compress=False, overwrite=True)
    return stage_path


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_text(rel_path: str, session=None, mode: str | None = None) -> tuple[str, int | None]:
    """AI_PARSE_DOCUMENT on one staged file. Returns (content, page_count).
    Raises on failure."""
    session = session or get_session()
    mode = (mode or PARSE_MODE).upper()
    if mode not in ("OCR", "LAYOUT"):
        raise ValueError(f"bad parse mode {mode!r}")
    row = session.sql(
        "SELECT r:content::VARCHAR, r:metadata:pageCount::INT, r:errorInformation::VARCHAR "
        f"FROM (SELECT AI_PARSE_DOCUMENT(TO_FILE('{STAGE}', ?), {{'mode': '{mode}'}}) AS r)",
        params=[rel_path],
    ).collect()[0]
    content, pages, err = row[0], row[1], row[2]
    if err and not content:
        raise RuntimeError(f"AI_PARSE_DOCUMENT: {err}")
    return content or "", pages


def extract_for(filename: str, data: bytes | None, rel_path: str, session=None) -> tuple[str | None, int | None, str | None]:
    """Pick the extraction route by extension. Returns (text, page_count, note)."""
    ext = safe_ext(filename)
    if ext in PLAIN_TEXT_EXTENSIONS and data is not None:
        return _decode_text(data), None, None
    if ext in CORTEX_EXTENSIONS:
        text, pages = extract_text(rel_path, session)
        return text, pages, None
    return None, None, f"No text extraction for '{ext or 'no extension'}' files."


def store_upload(data: bytes, filename: str, declared_type: str | None, document_id: str,
                 actor: str, session=None) -> UploadResult:
    """Stage the bytes, extract text inline, return the log rows to append.

    Staging failures raise. Extraction failures are returned in ``error`` with
    ``text_row=None`` so the caller decides whether to keep the document."""
    session = session or get_session()
    file_id = write.new_id()
    rel = relative_path(document_id, file_id, filename)
    stage_path = put_file(data, rel, session)

    text, pages, error, notes = None, None, None, []
    try:
        text, pages, note = extract_for(filename, data, rel, session)
        if note:
            notes.append(note)
    except Exception as exc:  # Cortex failed; the bytes are already staged.
        error = str(exc)

    file_row = write.file_row(
        actor, document_id=document_id, file_id=file_id, filename=filename,
        stage_path=stage_path, mimetype=guess_mimetype(filename, declared_type),
        size=len(data), checksum=sha256_hex(data), page_count=pages,
    )
    text_row = write.text_row(actor, file_id, text) if text is not None else None
    return UploadResult(file_row, text_row, error, notes)


def retry_extraction(file: dict, actor: str, session=None) -> dict:
    """Re-run extraction for a staged file; returns a document_text row."""
    rel = str(file["stage_path"]).removeprefix(f"{STAGE}/")
    data = None
    if safe_ext(file["filename"] or "") in PLAIN_TEXT_EXTENSIONS:
        data = get_bytes(file["stage_path"], session)
    text, _, note = extract_for(file["filename"] or rel, data, rel, session)
    if text is None:
        raise RuntimeError(note or "no text")
    return write.text_row(actor, file["file_id"], text)


def get_bytes(stage_path: str, session=None) -> bytes:
    session = session or get_session()
    return session.file.get_stream(stage_path).read()


# ── Housekeeping ─────────────────────────────────────────────────────────────


def _naive_utc(ts):
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def staged_files(session=None) -> pl.DataFrame:
    """Contents of @doc_files via its directory table."""
    session = session or get_session()
    session.sql(f"ALTER STAGE {STAGE[1:]} REFRESH").collect()
    rows = session.sql(
        f"SELECT relative_path, size, last_modified FROM DIRECTORY({STAGE})"
    ).collect()
    return pl.DataFrame(
        [(r[0], int(r[1] or 0), _naive_utc(r[2])) for r in rows],
        schema={"relative_path": pl.Utf8, "size": pl.Int64, "last_modified": pl.Datetime("us")},
        orient="row",
    )


def orphans(staged: pl.DataFrame, referenced_paths: set[str], min_age_days: int,
            now: datetime | None = None) -> pl.DataFrame:
    """Staged files that no log row and no session snapshot references, and that
    are older than ``min_age_days`` (another user may be mid-upload). Pure."""
    now = now or write.now_utc()
    refs = {p.removeprefix(f"{STAGE}/") for p in referenced_paths if p}
    cutoff = now - timedelta(days=min_age_days)
    return staged.filter(
        ~pl.col("relative_path").is_in(list(refs))
        & (pl.col("last_modified").is_null() | (pl.col("last_modified") < cutoff))
    )


def remove_files(relative_paths: list[str], session=None) -> int:
    session = session or get_session()
    for rel in relative_paths:
        quoted = f"{STAGE}/{rel}".replace("'", "\\'")
        session.sql(f"REMOVE '{quoted}'").collect()
    return len(relative_paths)


@cache_data(max_entries=16, ttl=600)
def file_bytes(stage_path: str, scope: str) -> bytes:
    """Cached download. Staged files are immutable, so caching is safe;
    ``scope`` (from the change token) keeps connections apart."""
    return get_bytes(stage_path)
