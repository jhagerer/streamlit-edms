"""Writing: every user action goes straight to Snowflake.

Human edits target hybrid tables and run as one transaction:

1. INSERT INTO change_log — event_id is the idempotency key (enforced primary
   key): a retried action is rejected as a whole and reported as "already
   applied", never applied twice;
2. the change itself — UPDATE/DELETE … WHERE version = <version the user saw>;
   if no row matches, someone else changed it first: Conflict, nothing written;
3. bump the touched tables' counters in sync_state (the read caches' keys).

Uploads and AI requests are facts and go to standard tables (stored_file,
ai_request) with plain INSERTs, outside any hybrid transaction.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from core import db, schema
from core.db import Conflict

__all__ = ["Conflict", "new_id", "now_utc"]


def new_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    """Naive UTC, microsecond precision — matches TIMESTAMP_NTZ."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(data: dict) -> str:
    def default(v):
        return v.isoformat() if isinstance(v, (datetime, date)) else str(v)
    return json.dumps(data, default=default, sort_keys=True)


@dataclass
class Stmt:
    sql: str
    params: list
    expect_one: bool = False       # must affect exactly one row, else Conflict
    conflict: str = "This was changed by someone else in the meantime."
    idempotency: bool = False      # duplicate key here = action already applied


class _AlreadyApplied(Exception):
    pass


def _log(event_id: str, ts: datetime, actor: str, entity: str, entity_id: str,
         document_id: str | None, op: str, version: int | None, data: dict) -> Stmt:
    return Stmt(
        "INSERT INTO change_log (event_id, event_ts, actor, entity, entity_id, document_id, "
        "op, version, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [event_id, ts, actor, entity, entity_id, document_id, op, version, _json(data)],
        idempotency=True,
    )


def _bump(tables: set[str], ts: datetime) -> Stmt:
    names = sorted(tables | {"change_log"})
    marks = ", ".join("?" for _ in names)
    return Stmt(f"UPDATE sync_state SET seq = seq + 1, changed_at = ? WHERE table_name IN ({marks})",
                [ts, *names])


def apply(stmts: list[Stmt], tables: set[str], ts: datetime, session=None) -> bool:
    """Run one action as a transaction. True = applied, False = it had been
    applied before (same event_id). Raises Conflict on a version mismatch."""
    try:
        with db.transaction(session) as tx:
            for st in stmts:
                try:
                    rows = tx.run(st.sql, st.params)
                except Exception as exc:
                    if db.is_duplicate_key(exc):
                        if st.idempotency:
                            raise _AlreadyApplied() from exc
                        raise Conflict(st.conflict) from exc
                    raise
                if st.expect_one and db.affected(rows) != 1:
                    raise Conflict(st.conflict)
            b = _bump(tables, ts)
            tx.run(b.sql, b.params)
    except _AlreadyApplied:
        return False
    return True


def _with_source(data: dict, source: str | None) -> dict:
    """Provenance in the history, e.g. source='ai:<enrichment event_id>' when a
    user applies an AI suggestion."""
    return {**data, "source": source} if source else data


def _ids(event_id: str | None, n: int) -> list[str]:
    """Deterministic event ids for an action with n log rows: a retry of the
    same action (same event_id) produces the same ids."""
    base = event_id or new_id()
    return [base] + [f"{base}:{i}" for i in range(1, n)]


# ── Documents ────────────────────────────────────────────────────────────────

DOCUMENT_FIELDS = ("document_type_id", "label", "description", "language", "status")


def create_document(actor: str, *, document_id: str, fields: dict, files: list[dict],
                    metadata: dict[str, str], tag_ids: list[str],
                    event_id: str | None = None, session=None) -> bool:
    """A new document with its file attachments, metadata values and tags, in
    one transaction. ``files`` are stored-file dicts (see files.stage_upload);
    register them with register_files() after this commits."""
    ts = now_utc()
    f = {k: fields.get(k) for k in DOCUMENT_FIELDS}
    f["status"] = f["status"] or "active"
    metadata = {k: v for k, v in metadata.items() if v not in (None, "")}
    n = 1 + len(files) + len(metadata) + len(tag_ids)
    ids = iter(_ids(event_id, n))
    stmts = [
        _log(next(ids), ts, actor, "document", document_id, document_id, "create", 1, f),
        Stmt("INSERT INTO document (document_id, document_type_id, label, description, language, "
             "status, version, created_at, created_by, updated_at, updated_by) "
             "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
             [document_id, f["document_type_id"], f["label"], f["description"], f["language"],
              f["status"], ts, actor, ts, actor],
             conflict="A document with this id exists already."),
    ]
    for file in files:
        stmts += _attach_file_stmts(next(ids), ts, actor, document_id, file)
    for mt_id, value in metadata.items():
        stmts += [
            _log(next(ids), ts, actor, "document_metadata", f"{document_id}/{mt_id}", document_id,
                 "create", 1, {"metadata_type_id": mt_id, "value": value}),
            Stmt("INSERT INTO document_metadata (document_id, metadata_type_id, value, version, "
                 "updated_at, updated_by) VALUES (?, ?, ?, 1, ?, ?)",
                 [document_id, mt_id, value, ts, actor]),
        ]
    for tag_id in tag_ids:
        stmts += [
            _log(next(ids), ts, actor, "document_tag", f"{document_id}/{tag_id}", document_id,
                 "attach", None, {"tag_id": tag_id}),
            Stmt("INSERT INTO document_tag (document_id, tag_id, assigned_at, assigned_by) "
                 "VALUES (?, ?, ?, ?)", [document_id, tag_id, ts, actor]),
        ]
    tables = {"document", "document_file", "document_metadata", "document_tag"}
    return apply(stmts, tables, ts, session)


def update_document(actor: str, current: dict, changes: dict, op: str = "update",
                    source: str | None = None, event_id: str | None = None,
                    session=None) -> bool:
    """Change fields of a document the user saw at ``current['version']``.
    ``op`` is update | trash | restore (trash/restore set the status)."""
    if op == "trash":
        changes = {**changes, "status": "trashed"}
    elif op == "restore":
        changes = {**changes, "status": "active"}
    changes = {k: v for k, v in changes.items() if k in DOCUMENT_FIELDS}
    if not changes:
        return False
    ts, version = now_utc(), int(current["version"])
    sets = ", ".join(f"{k} = ?" for k in changes)
    stmts = [
        _log(_ids(event_id, 1)[0], ts, actor, "document", current["document_id"],
             current["document_id"], op, version + 1, _with_source(changes, source)),
        Stmt(f"UPDATE document SET {sets}, version = version + 1, updated_at = ?, updated_by = ? "
             "WHERE document_id = ? AND version = ?",
             [*changes.values(), ts, actor, current["document_id"], version],
             expect_one=True,
             conflict="This document was changed by someone else in the meantime. "
                      "The page now shows the latest version — please make your change again."),
    ]
    return apply(stmts, {"document"}, ts, session)


def set_metadata(actor: str, document_id: str, metadata_type_id: str, value: str | None,
                 current_version: int | None, source: str | None = None,
                 event_id: str | None = None, session=None) -> bool:
    """Set, change or remove one metadata value. ``current_version`` is the
    version the user saw (None = the field had no value)."""
    ts, value = now_utc(), (value or "").strip()
    key = f"{document_id}/{metadata_type_id}"
    conflict = ("This field was changed by someone else in the meantime. "
                "The page now shows the latest value.")
    eid = _ids(event_id, 1)[0]
    if current_version is None:
        if not value:
            return False
        stmts = [
            _log(eid, ts, actor, "document_metadata", key, document_id, "create", 1,
                 _with_source({"metadata_type_id": metadata_type_id, "value": value}, source)),
            Stmt("INSERT INTO document_metadata (document_id, metadata_type_id, value, version, "
                 "updated_at, updated_by) VALUES (?, ?, ?, 1, ?, ?)",
                 [document_id, metadata_type_id, value, ts, actor], conflict=conflict),
        ]
    elif value:
        stmts = [
            _log(eid, ts, actor, "document_metadata", key, document_id, "update",
                 current_version + 1,
                 _with_source({"metadata_type_id": metadata_type_id, "value": value}, source)),
            Stmt("UPDATE document_metadata SET value = ?, version = version + 1, updated_at = ?, "
                 "updated_by = ? WHERE document_id = ? AND metadata_type_id = ? AND version = ?",
                 [value, ts, actor, document_id, metadata_type_id, current_version],
                 expect_one=True, conflict=conflict),
        ]
    else:
        stmts = [
            _log(eid, ts, actor, "document_metadata", key, document_id, "delete", None,
                 {"metadata_type_id": metadata_type_id}),
            Stmt("DELETE FROM document_metadata WHERE document_id = ? AND metadata_type_id = ? "
                 "AND version = ?", [document_id, metadata_type_id, current_version],
                 expect_one=True, conflict=conflict),
        ]
    return apply(stmts, {"document_metadata"}, ts, session)


def set_tags(actor: str, document_id: str, attach: list[str], detach: list[str],
             source: str | None = None, event_id: str | None = None, session=None) -> bool:
    """Attach and detach tags in one transaction. Attaching an attached tag or
    detaching a detached one is a Conflict (someone else did it first)."""
    if not attach and not detach:
        return False
    ts = now_utc()
    ids = iter(_ids(event_id, len(attach) + len(detach)))
    stmts: list[Stmt] = []
    for tag_id in attach:
        stmts += [
            _log(next(ids), ts, actor, "document_tag", f"{document_id}/{tag_id}", document_id,
                 "attach", None, _with_source({"tag_id": tag_id}, source)),
            Stmt("INSERT INTO document_tag (document_id, tag_id, assigned_at, assigned_by) "
                 "VALUES (?, ?, ?, ?)", [document_id, tag_id, ts, actor],
                 conflict="Someone else attached this tag in the meantime."),
        ]
    for tag_id in detach:
        stmts += [
            _log(next(ids), ts, actor, "document_tag", f"{document_id}/{tag_id}", document_id,
                 "detach", None, {"tag_id": tag_id}),
            Stmt("DELETE FROM document_tag WHERE document_id = ? AND tag_id = ?",
                 [document_id, tag_id], expect_one=True,
                 conflict="Someone else removed this tag in the meantime."),
        ]
    return apply(stmts, {"document_tag"}, ts, session)


# ── Files ────────────────────────────────────────────────────────────────────


def _attach_file_stmts(event_id: str, ts: datetime, actor: str, document_id: str,
                       file: dict) -> list[Stmt]:
    return [
        _log(event_id, ts, actor, "document_file", file["file_id"], document_id, "create", 1,
             {"filename": file["filename"], "checksum": file["checksum"]}),
        Stmt("INSERT INTO document_file (file_id, document_id, active, version, created_at, "
             "created_by, updated_at, updated_by) VALUES (?, ?, TRUE, 1, ?, ?, ?, ?)",
             [file["file_id"], document_id, ts, actor, ts, actor]),
    ]


def attach_files(actor: str, document_id: str, files: list[dict],
                 event_id: str | None = None, session=None) -> bool:
    """Attach newly uploaded files to an existing document."""
    if not files:
        return False
    ts = now_utc()
    ids = iter(_ids(event_id, len(files)))
    stmts: list[Stmt] = []
    for file in files:
        stmts += _attach_file_stmts(next(ids), ts, actor, document_id, file)
    return apply(stmts, {"document_file"}, ts, session)


def set_file_active(actor: str, current: dict, active: bool, event_id: str | None = None,
                    session=None) -> bool:
    """Detach (active = False) or re-attach a file; the bytes are kept."""
    ts, version = now_utc(), int(current["version"])
    stmts = [
        _log(_ids(event_id, 1)[0], ts, actor, "document_file", current["file_id"],
             current["document_id"], "update", version + 1, {"active": active}),
        Stmt("UPDATE document_file SET active = ?, version = version + 1, updated_at = ?, "
             "updated_by = ? WHERE file_id = ? AND version = ?",
             [active, ts, actor, current["file_id"], version], expect_one=True,
             conflict="This file was changed by someone else in the meantime."),
    ]
    return apply(stmts, {"document_file"}, ts, session)


def register_files(actor: str, files: list[dict], reason: str = "upload",
                   session=None) -> None:
    """Record uploaded files (stored_file) and ask for AI processing
    (ai_request). Standard tables, so plain INSERTs outside the hybrid
    transaction. The stream on ai_request fires the AI task."""
    if not files:
        return
    ts = now_utc()
    for f in files:
        db.query("INSERT INTO stored_file (file_id, document_id, uploaded_at, uploaded_by, "
                 "filename, stage_path, mimetype, size, checksum) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [f["file_id"], f["document_id"], ts, actor, f["filename"], f["stage_path"],
                  f["mimetype"], f["size"], f["checksum"]], session=session)
    request_ai(actor, [(f["file_id"], f["document_id"]) for f in files], reason, session,
               extra_tables=("stored_file",))


def request_ai(actor: str, files: list[tuple[str, str]], reason: str = "retry", session=None,
               extra_tables: tuple[str, ...] = ()) -> bool:
    """Ask for OCR + enrichment of (file_id, document_id) pairs."""
    if not files:
        return False
    ts = now_utc()
    for file_id, document_id in files:
        db.query("INSERT INTO ai_request (event_id, requested_at, actor, file_id, document_id, "
                 "reason) VALUES (?, ?, ?, ?, ?, ?)",
                 [new_id(), ts, actor, file_id, document_id, reason], session=session)
    names = sorted({"ai_request", *extra_tables})
    marks = ", ".join("?" for _ in names)
    db.query(f"UPDATE sync_state SET seq = seq + 1, changed_at = ? WHERE table_name IN ({marks})",
             [ts, *names], session=session)
    return True


# ── Definitions: document types, metadata types, tags ────────────────────────

DEFINITIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "document_type": ("document_type_id", ("label", "active")),
    "metadata_type": ("metadata_type_id",
                      ("name", "label", "data_type", "choices", "default_value", "active")),
    "tag": ("tag_id", ("label", "color", "active")),
}


def _check_definition(table: str, values: dict) -> dict:
    if table not in DEFINITIONS:
        raise ValueError(f"unknown definition table {table!r}")
    _, fields = DEFINITIONS[table]
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError(f"{table}: unknown fields {sorted(unknown)}")
    if table == "metadata_type" and "data_type" in values \
            and values["data_type"] not in schema.DATA_TYPES:
        raise ValueError(f"bad data_type {values['data_type']!r}")
    return values


def create_definition(table: str, actor: str, values: dict, entity_id: str | None = None,
                      event_id: str | None = None, session=None) -> str:
    """Create a document type / metadata type / tag. Returns its id."""
    values = _check_definition(table, {"active": True, **values})
    key, fields = DEFINITIONS[table]
    entity_id = entity_id or new_id()
    ts = now_utc()
    cols = [key, *fields, "version", "created_at", "created_by", "updated_at", "updated_by"]
    params = [entity_id, *(values.get(f) for f in fields), 1, ts, actor, ts, actor]
    stmts = [
        _log(_ids(event_id, 1)[0], ts, actor, table, entity_id, None, "create", 1, values),
        Stmt(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
             params, conflict="This id exists already."),
    ]
    apply(stmts, {table}, ts, session)
    return entity_id


def update_definition(table: str, actor: str, current: dict, changes: dict,
                      event_id: str | None = None, session=None) -> bool:
    changes = _check_definition(table, changes)
    if not changes:
        return False
    key, _ = DEFINITIONS[table]
    ts, version = now_utc(), int(current["version"])
    sets = ", ".join(f"{k} = ?" for k in changes)
    stmts = [
        _log(_ids(event_id, 1)[0], ts, actor, table, current[key], None, "update", version + 1,
             changes),
        Stmt(f"UPDATE {table} SET {sets}, version = version + 1, updated_at = ?, updated_by = ? "
             f"WHERE {key} = ? AND version = ?",
             [*changes.values(), ts, actor, current[key], version], expect_one=True,
             conflict="This was changed by someone else in the meantime. The page now shows "
                      "the latest version."),
    ]
    return apply(stmts, {table}, ts, session)
