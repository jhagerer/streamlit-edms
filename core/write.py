"""Writing: every user action goes straight to Snowflake, and only appends.

Every table is append-only. A human edit appends the *next version* of each
record it touches (to hybrid tables), in one transaction:

* the new row has version = <the version the user saw> + 1. The primary key
  (key…, version) is enforced: if someone else saved that version first, the
  insert fails, the transaction rolls back, and the user sees a Conflict;
* every row carries a client-generated event_id with a UNIQUE constraint: a
  retried action fails the same way, but its event_id is already in the table,
  so it is reported as "already applied", never applied twice.

Uploads and AI requests are facts and go to standard tables (stored_file,
ai_request) with plain INSERTs, outside any hybrid transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from core import db, schema
from core.db import Conflict

__all__ = ["Conflict", "new_id", "now_utc"]


def new_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    """Naive UTC, microsecond precision — matches TIMESTAMP_NTZ."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Row:
    table: str
    values: dict
    conflict: str = "This was changed by someone else in the meantime."


def _ids(event_id: str | None, n: int) -> list[str]:
    """Deterministic event ids for an action with n rows: a retry of the same
    action (same event_id) produces the same ids."""
    base = event_id or new_id()
    return [base] + [f"{base}:{i}" for i in range(1, n)]


def _row(table: str, event_id: str, ts: datetime, actor: str, op: str, version: int,
         key: dict, fields: dict, source: str | None = None, conflict: str | None = None) -> Row:
    values = {**key, "version": version, "event_id": event_id, "event_ts": ts, "actor": actor,
              "op": op, **fields, "source": source}
    unknown = set(values) - set(schema.COLUMNS[table])
    missing = set(schema.COLUMNS[table]) - set(values)
    if unknown or missing:
        raise ValueError(f"{table}: unknown {sorted(unknown)}, missing {sorted(missing)}")
    return Row(table, values, conflict or Row.conflict)


def apply(rows: list[Row], session=None) -> bool:
    """Append all rows in one transaction. True = written; False = this action
    had been applied before (its event_id exists). Raises Conflict when a
    version was taken by someone else first."""
    if not rows:
        return False
    try:
        with db.transaction(session) as tx:
            for r in rows:
                cols = list(r.values)
                tx.run(f"INSERT INTO {r.table} ({', '.join(cols)}) "
                       f"VALUES ({', '.join('?' for _ in cols)})", list(r.values.values()))
    except Exception as exc:
        if not db.is_duplicate_key(exc):
            raise
        first = rows[0]
        seen = db.query(f"SELECT COUNT(*) FROM {first.table} WHERE event_id = ?",
                        [first.values["event_id"]], session=session)
        if db.affected(seen):
            return False
        failed = next((r for r in rows if r.table in str(exc).lower()), first)
        raise Conflict(failed.conflict) from exc
    return True


_DOC_CONFLICT = ("This document was changed by someone else in the meantime. The page now "
                 "shows the latest version — please make your change again.")
_META_CONFLICT = ("This field was changed by someone else in the meantime. The page now shows "
                  "the latest value.")
_TAG_CONFLICT = "The tags were changed by someone else in the meantime."
_FILE_CONFLICT = "This file was changed by someone else in the meantime."
_DEF_CONFLICT = ("This was changed by someone else in the meantime. The page now shows the "
                 "latest version.")


# ── Documents ────────────────────────────────────────────────────────────────

DOCUMENT_FIELDS = ("document_type_id", "label", "description", "language", "status")


def _doc_fields(record: dict) -> dict:
    return {k: record.get(k) for k in DOCUMENT_FIELDS}


def create_document(actor: str, *, document_id: str, fields: dict, files: list[dict],
                    metadata: dict[str, str], tag_ids: list[str],
                    event_id: str | None = None, session=None) -> bool:
    """A new document with its file attachments, metadata values and tags:
    version 1 of each record, in one transaction. ``files`` are stored-file
    dicts (see files.stage_upload); register them with register_files()
    after this commits."""
    ts = now_utc()
    f = _doc_fields(fields)
    f["status"] = f["status"] or "active"
    metadata = {k: v for k, v in metadata.items() if v not in (None, "")}
    ids = iter(_ids(event_id, 1 + len(files) + len(metadata) + len(tag_ids)))
    rows = [_row("document_log", next(ids), ts, actor, "create", 1,
                 {"document_id": document_id}, f,
                 conflict="A document with this id exists already.")]
    rows += [_file_row(next(ids), ts, actor, file["file_id"], document_id, True, 1)
             for file in files]
    rows += [_row("metadata_log", next(ids), ts, actor, "set", 1,
                  {"document_id": document_id, "metadata_type_id": mt}, {"value": v})
             for mt, v in metadata.items()]
    rows += [_row("tag_assignment_log", next(ids), ts, actor, "attach", 1,
                  {"document_id": document_id, "tag_id": t}, {"assigned": True})
             for t in tag_ids]
    return apply(rows, session)


def update_document(actor: str, current: dict, changes: dict, op: str = "update",
                    source: str | None = None, event_id: str | None = None,
                    session=None) -> bool:
    """Append the next version of a document the user saw at
    ``current['version']``. ``op`` is update | trash | restore."""
    if op == "trash":
        changes = {**changes, "status": "trashed"}
    elif op == "restore":
        changes = {**changes, "status": "active"}
    changes = {k: v for k, v in changes.items() if k in DOCUMENT_FIELDS}
    if not changes:
        return False
    row = _row("document_log", _ids(event_id, 1)[0], now_utc(), actor, op,
               int(current["version"]) + 1, {"document_id": current["document_id"]},
               {**_doc_fields(current), **changes}, source, _DOC_CONFLICT)
    return apply([row], session)


def set_metadata(actor: str, document_id: str, metadata_type_id: str, value: str | None,
                 current_version: int | None, source: str | None = None,
                 event_id: str | None = None, session=None) -> bool:
    """Set, change or remove (empty value) one metadata value. Appends the
    next version of the (document, field) chain; ``current_version`` is the
    version the user saw (None = the field never had a value)."""
    value = (value or "").strip() or None
    if current_version is None and value is None:
        return False
    row = _row("metadata_log", _ids(event_id, 1)[0], now_utc(), actor,
               "set" if value is not None else "remove", (current_version or 0) + 1,
               {"document_id": document_id, "metadata_type_id": metadata_type_id},
               {"value": value}, source, _META_CONFLICT)
    return apply([row], session)


def set_tags(actor: str, document_id: str, attach: dict[str, int | None],
             detach: dict[str, int], source: str | None = None,
             event_id: str | None = None, session=None) -> bool:
    """Attach and detach tags in one transaction. Each dict maps tag_id to
    the version of that (document, tag) chain the user saw (None = never
    assigned). Someone else changing the same tag first is a Conflict."""
    changes = [(t, True, v) for t, v in attach.items()] + [(t, False, v) for t, v in detach.items()]
    if not changes:
        return False
    ts = now_utc()
    ids = iter(_ids(event_id, len(changes)))
    rows = [_row("tag_assignment_log", next(ids), ts, actor, "attach" if on else "detach",
                 (version or 0) + 1, {"document_id": document_id, "tag_id": tag_id},
                 {"assigned": on}, source, _TAG_CONFLICT)
            for tag_id, on, version in changes]
    return apply(rows, session)


# ── Files ────────────────────────────────────────────────────────────────────


def _file_row(event_id: str, ts: datetime, actor: str, file_id: str, document_id: str,
              active: bool, version: int) -> Row:
    return _row("document_file_log", event_id, ts, actor, "attach" if active else "detach",
                version, {"file_id": file_id}, {"document_id": document_id, "active": active},
                conflict=_FILE_CONFLICT)


def attach_files(actor: str, document_id: str, files: list[dict],
                 event_id: str | None = None, session=None) -> bool:
    """Attach newly uploaded files to an existing document."""
    ts = now_utc()
    ids = iter(_ids(event_id, len(files)))
    return apply([_file_row(next(ids), ts, actor, f["file_id"], document_id, True, 1)
                  for f in files], session)


def set_file_active(actor: str, current: dict, active: bool, event_id: str | None = None,
                    session=None) -> bool:
    """Detach (active = False) or re-attach a file; the bytes are kept."""
    row = _file_row(_ids(event_id, 1)[0], now_utc(), actor, current["file_id"],
                    current["document_id"], active, int(current["version"]) + 1)
    return apply([row], session)


def register_files(actor: str, files: list[dict], reason: str = "upload",
                   session=None) -> None:
    """Record uploaded files (stored_file) and ask for AI processing
    (ai_request). Standard tables, so plain INSERTs outside the hybrid
    transaction. The stream on ai_request fires the ingest pipeline."""
    ts = now_utc()
    for f in files:
        db.query("INSERT INTO stored_file (file_id, document_id, uploaded_at, uploaded_by, "
                 "filename, stage_path, mimetype, size, checksum) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [f["file_id"], f["document_id"], ts, actor, f["filename"], f["stage_path"],
                  f["mimetype"], f["size"], f["checksum"]], session=session)
    request_ai(actor, [(f["file_id"], f["document_id"]) for f in files], reason, session)


def request_ai(actor: str, files: list[tuple[str, str]], reason: str = "retry",
               session=None) -> bool:
    """Ask for OCR + enrichment + indexing of (file_id, document_id) pairs."""
    ts = now_utc()
    for file_id, document_id in files:
        db.query("INSERT INTO ai_request (event_id, requested_at, actor, file_id, document_id, "
                 "reason) VALUES (?, ?, ?, ?, ?, ?)",
                 [new_id(), ts, actor, file_id, document_id, reason], session=session)
    return bool(files)


# ── Definitions: document types, metadata types, tags ────────────────────────

DEFINITIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # table: (log table, key, fields)
    "document_type": ("document_type_log", "document_type_id", ("label", "active")),
    "metadata_type": ("metadata_type_log", "metadata_type_id",
                      ("name", "label", "data_type", "choices", "default_value", "active")),
    "tag": ("tag_log", "tag_id", ("label", "color", "active")),
}


def _check_definition(kind: str, values: dict) -> dict:
    if kind not in DEFINITIONS:
        raise ValueError(f"unknown definition {kind!r}")
    unknown = set(values) - set(DEFINITIONS[kind][2])
    if unknown:
        raise ValueError(f"{kind}: unknown fields {sorted(unknown)}")
    if kind == "metadata_type" and "data_type" in values \
            and values["data_type"] not in schema.DATA_TYPES:
        raise ValueError(f"bad data_type {values['data_type']!r}")
    return values


def create_definition(kind: str, actor: str, values: dict, entity_id: str | None = None,
                      event_id: str | None = None, session=None) -> str:
    """Create a document type / metadata type / tag (version 1). Returns its id."""
    values = _check_definition(kind, {"active": True, **values})
    table, key, fields = DEFINITIONS[kind]
    entity_id = entity_id or new_id()
    row = _row(table, _ids(event_id, 1)[0], now_utc(), actor, "create", 1, {key: entity_id},
               {f: values.get(f) for f in fields}, conflict="This id exists already.")
    apply([row], session)
    return entity_id


def update_definition(kind: str, actor: str, current: dict, changes: dict,
                      event_id: str | None = None, session=None) -> bool:
    """Append the next version of a definition the user saw at
    ``current['version']``."""
    changes = _check_definition(kind, changes)
    if not changes:
        return False
    table, key, fields = DEFINITIONS[kind]
    row = _row(table, _ids(event_id, 1)[0], now_utc(), actor, "update",
               int(current["version"]) + 1, {key: current[key]},
               {**{f: current.get(f) for f in fields}, **changes}, conflict=_DEF_CONFLICT)
    return apply([row], session)
