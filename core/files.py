"""@doc_files: immutable, content-addressed uploads.

File bytes land in the stage immediately at upload, at
``@doc_files/{sha256}/{filename}``; the document_file_log row that references
them waits for "Save to database". A discarded session therefore leaves
orphaned files, which the admin page can clean up.

Text extraction does NOT happen here. Once the rows are saved, a stream on
document_file_log fires a triggered task in Snowflake that runs
AI_PARSE_DOCUMENT (see extract_text() in sql/minidms-setup.sql).
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone

import polars as pl

from core import write
from core.session import cache_data, get_session

STAGE = "@doc_files"

# Formats AI_PARSE_DOCUMENT accepts; others are marked 'skipped' by the task.
# Keep in sync with the extension list in extract_text().
CORTEX_EXTENSIONS = {".pdf", ".docx", ".pptx", ".jpeg", ".jpg", ".png", ".tif", ".tiff",
                     ".html", ".htm", ".txt"}


def safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ""


def safe_filename(filename: str) -> str:
    """A stage-path-safe version of the original name. Keeps the extension
    (AI_PARSE_DOCUMENT detects the format from it); the original name is kept
    unchanged in document_file_log.filename."""
    base = os.path.basename(filename.replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:120] or "file"
    return stem + safe_ext(base)


def relative_path(checksum: str, filename: str) -> str:
    """{sha256}/{filename}: identical content under the same name maps to the
    same path, so re-uploading a file does not store it twice."""
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("checksum must be a lower-case sha256 hex digest")
    return f"{checksum}/{safe_filename(filename)}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def guess_mimetype(filename: str, declared: str | None = None) -> str:
    return declared or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def put_file(data: bytes, rel_path: str, session=None) -> str:
    """Stage the bytes. overwrite=False is deliberate here: the path is derived
    from the content, so an existing file already holds exactly these bytes."""
    session = session or get_session()
    stage_path = f"{STAGE}/{rel_path}"
    session.file.put_stream(io.BytesIO(data), stage_path, auto_compress=False, overwrite=False)
    return stage_path


def extractable(filename: str) -> bool:
    return safe_ext(filename) in CORTEX_EXTENSIONS


def store_upload(data: bytes, filename: str, declared_type: str | None, document_id: str,
                 actor: str, session=None) -> dict:
    """Stage the bytes and return the document_file_log row to append.
    Staging failures raise; nothing is appended then."""
    checksum = sha256_hex(data)
    stage_path = put_file(data, relative_path(checksum, filename), session)
    return write.file_row(
        actor, document_id=document_id, file_id=write.new_id(), filename=filename,
        stage_path=stage_path, mimetype=guess_mimetype(filename, declared_type),
        size=len(data), checksum=checksum, page_count=None,
    )


def resubmit_row(file: dict, actor: str) -> dict:
    """Request text extraction again: a fresh, identical snapshot of the file
    row. Once saved, the stream picks it up and the task queues the file
    (only if it has no text yet)."""
    return write.file_row(actor, active=True, **{c: file[c] for c in (
        "document_id", "file_id", "filename", "stage_path", "mimetype", "size",
        "checksum", "page_count")})


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


_MANAGED = re.compile(r"^([0-9a-f]{64})/([A-Za-z0-9._-]+)$")


def remove_statement(rel: str) -> str | None:
    """REMOVE for exactly one managed file. REMOVE matches by prefix, so the
    folder is given as the path and the file name as an anchored PATTERN —
    otherwise removing '<sha>/a.pd' would also remove '<sha>/a.pdf'. Paths
    that do not look like '{sha256}/{safe name}' are never touched. Pure."""
    m = _MANAGED.match(rel)
    if not m:
        return None
    pattern = ".*/" + re.escape(m.group(2)) + "$"
    pattern = pattern.replace("\\", "\\\\")  # SQL string literal escaping
    return f"REMOVE {STAGE}/{m.group(1)}/ PATTERN = '{pattern}'"


def remove_files(relative_paths: list[str], session=None) -> int:
    session = session or get_session()
    removed = 0
    for rel in relative_paths:
        stmt = remove_statement(rel)
        if stmt:
            session.sql(stmt).collect()
            removed += 1
    return removed


@cache_data(max_entries=16, ttl=600)
def file_bytes(stage_path: str, scope: str) -> bytes:
    """Cached download. Staged files are immutable, so caching is safe;
    ``scope`` (from the change token) keeps connections apart."""
    return get_bytes(stage_path)
