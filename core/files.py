"""@doc_files: immutable, content-addressed uploads.

File bytes land in the stage at upload, at ``@doc_files/{sha256}/{filename}``.
Then the app writes the document (hybrid tables) and records the file in
stored_file + ai_request (standard tables). If an upload is interrupted between
staging and stored_file, the staged bytes are orphaned; the admin page can
clean them up.

Text extraction does NOT happen here: ai_request fires a triggered task in
Snowflake that runs AI_COMPLETE (process_ai() in sql/minidms-setup.sql), so the
OCR text and enrichments are lineage-linked to stored_file.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone

import polars as pl

from core import db, write
from core.session import cache_data

STAGE = "@doc_files"

# Formats AI_COMPLETE reads with claude-sonnet-4-5; others are marked
# 'skipped' by the task. Keep in sync with the extension list in process_ai().
AI_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".md", ".xhtml",
                 ".jpg", ".jpeg", ".png", ".gif", ".webp"}


def safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ""


def safe_filename(filename: str) -> str:
    """A stage-path-safe version of the original name. Keeps the extension
    (AI_COMPLETE detects the format from it); the original name is kept
    unchanged in stored_file.filename."""
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
    stage_path = f"{STAGE}/{rel_path}"
    db.put_stream(io.BytesIO(data), stage_path, session=session,
                  auto_compress=False, overwrite=False)
    return stage_path


def extractable(filename: str) -> bool:
    return safe_ext(filename) in AI_EXTENSIONS


def stage_upload(data: bytes, filename: str, declared_type: str | None, document_id: str,
                 session=None) -> dict:
    """Stage the bytes; return the stored-file dict for the write commands.
    Staging failures raise; nothing is written then."""
    checksum = sha256_hex(data)
    stage_path = put_file(data, relative_path(checksum, filename), session)
    return {
        "file_id": write.new_id(), "document_id": document_id, "filename": filename,
        "stage_path": stage_path, "mimetype": guess_mimetype(filename, declared_type),
        "size": len(data), "checksum": checksum,
    }


def get_bytes(stage_path: str, session=None) -> bytes:
    return db.get_stream(stage_path, session=session).read()


@cache_data(max_entries=16, ttl=600)
def file_bytes(stage_path: str, scope: str) -> bytes:
    """Cached download. Staged files are immutable, so caching is safe;
    ``scope`` (account/role/db/schema) keeps connections apart."""
    return get_bytes(stage_path)


# ── Housekeeping ─────────────────────────────────────────────────────────────


def _naive_utc(ts):
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def staged_files(session=None) -> pl.DataFrame:
    """Contents of @doc_files via its directory table."""
    db.query(f"ALTER STAGE {STAGE[1:]} REFRESH", session=session)
    rows = db.query(f"SELECT relative_path, size, last_modified FROM DIRECTORY({STAGE})",
                    session=session)
    return pl.DataFrame(
        [(r[0], int(r[1] or 0), _naive_utc(r[2])) for r in rows],
        schema={"relative_path": pl.Utf8, "size": pl.Int64, "last_modified": pl.Datetime("us")},
        orient="row",
    )


def orphans(staged: pl.DataFrame, referenced_paths: set[str], min_age_days: int,
            now: datetime | None = None) -> pl.DataFrame:
    """Staged files that no stored_file row references and that are older than
    ``min_age_days`` (someone may be mid-upload). Pure."""
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
    removed = 0
    for rel in relative_paths:
        stmt = remove_statement(rel)
        if stmt:
            db.query(stmt, session=session)
            removed += 1
    return removed
