"""The two-stage write: build event rows, persist to the session stage, flush.

Every write is an append of a full snapshot of the record. Pages build rows
with the helpers below and hand them to the tier-1 store (pages/_state); the
store calls ``save_to_session`` and ``flush`` with its frames.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import polars as pl

from core import schema
from core.schema import APPEND_TABLES
from core.session import get_session

EVENT_FIELDS = ("event_id", "event_ts", "actor")


def new_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    """Naive UTC, microsecond precision — matches TIMESTAMP_NTZ."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def event_row(actor: str, **fields: Any) -> dict[str, Any]:
    """Stamp a row with event_id (uuid4), event_ts and actor."""
    return {"event_id": new_id(), "event_ts": now_utc(), "actor": actor, **fields}


def rows_frame(table: str, rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Validate rows against the table's columns, loudly, and return a frame
    with pinned dtypes. Column drift fails here, not at flush time."""
    if table not in schema.COLUMNS:
        raise ValueError(f"unknown table {table!r}")
    expected = set(schema.columns(table))
    for i, row in enumerate(rows):
        keys = set(row)
        missing, extra = expected - keys, keys - expected
        if missing or extra:
            raise ValueError(
                f"{table} row {i}: missing columns {sorted(missing)}, "
                f"unexpected columns {sorted(extra)}"
            )
        for f in EVENT_FIELDS:
            if row[f] in (None, ""):
                raise ValueError(f"{table} row {i}: {f} is empty")
    return pl.DataFrame(rows, schema=schema.polars_schema(table), orient="row")


# ── Row builders (full snapshots) ────────────────────────────────────────────


def document_row(actor: str, op: str, document_id: str, document_type_id: str | None,
                 label: str | None, description: str | None, language: str | None) -> dict:
    if op not in schema.DOCUMENT_OPS:
        raise ValueError(f"bad op {op!r}")
    return event_row(actor, op=op, document_id=document_id, document_type_id=document_type_id,
                     label=label, description=description, language=language)


def document_row_from(actor: str, current: dict, op: str, **changes) -> dict:
    """Next snapshot of a document: the current record plus the changes."""
    fields = {k: current.get(k) for k in ("document_id", "document_type_id", "label",
                                          "description", "language")}
    fields.update(changes)
    return document_row(actor, op, **fields)


def file_row(actor: str, *, document_id: str, file_id: str, filename: str, stage_path: str,
             mimetype: str | None, size: int | None, checksum: str | None,
             page_count: int | None, active: bool = True) -> dict:
    return event_row(actor, active=active, document_id=document_id, file_id=file_id,
                     filename=filename, stage_path=stage_path, mimetype=mimetype, size=size,
                     checksum=checksum, page_count=page_count)


def text_row(actor: str, file_id: str, content: str | None) -> dict:
    return event_row(actor, file_id=file_id, content=content)


def metadata_row(actor: str, document_id: str, metadata_type_id: str, value: str | None) -> dict:
    return event_row(actor, document_id=document_id, metadata_type_id=metadata_type_id,
                     value=value)


def tag_assignment_row(actor: str, document_id: str, tag_id: str, assigned: bool) -> dict:
    return event_row(actor, document_id=document_id, tag_id=tag_id, assigned=assigned)


def document_type_row(actor: str, document_type_id: str, label: str, active: bool = True) -> dict:
    return event_row(actor, active=active, document_type_id=document_type_id, label=label)


def metadata_type_row(actor: str, metadata_type_id: str, name: str, label: str, data_type: str,
                      choices: str | None, default_value: str | None, active: bool = True) -> dict:
    if data_type not in schema.DATA_TYPES:
        raise ValueError(f"bad data_type {data_type!r}")
    return event_row(actor, active=active, metadata_type_id=metadata_type_id, name=name,
                     label=label, data_type=data_type, choices=choices,
                     default_value=default_value)


def tag_row(actor: str, tag_id: str, label: str, color: str, active: bool = True) -> dict:
    return event_row(actor, active=active, tag_id=tag_id, label=label, color=color)


# ── Persisting ───────────────────────────────────────────────────────────────


def save_to_session(user: str, appends: dict[str, pl.DataFrame], session=None) -> int:
    """Tier 1 -> tier 2. A straight serialise: tier 1 already is exactly the set
    of rows to persist. Returns the number of files written."""
    from core import snapshot

    written = 0
    for table in APPEND_TABLES:
        mine = appends.get(table)
        if mine is not None and mine.height:
            snapshot.write_snapshot(user, table, mine, session)
            written += 1
    return written


def _snowpark_schema(table: str):
    from snowflake.snowpark.types import (BooleanType, LongType, StringType, StructField,
                                          StructType, TimestampTimeZone, TimestampType)

    mapping = {
        schema.S: StringType(),
        schema.B: BooleanType(),
        schema.I: LongType(),
        schema.TS: TimestampType(TimestampTimeZone.NTZ),
    }
    return StructType([StructField(name, mapping[dtype], nullable=True)
                       for name, dtype in schema.COLUMNS[table].items()])


def insert(table: str, df: pl.DataFrame, session=None) -> int:
    """Append one frame to its log table as-is."""
    session = session or get_session()
    df = schema.conform(table, df)
    if not df.height:
        return 0
    sp_df = session.create_dataframe(df.rows(), schema=_snowpark_schema(table))
    sp_df.write.save_as_table(table, mode="append", column_order="name")
    return df.height


def flush(user: str, appends: dict[str, pl.DataFrame], session=None) -> dict[str, int]:
    """Tier 1 -> tier 3. Insert, then remove the snapshot folder. The caller
    clears tier 1 last. If REMOVE fails after the inserts, the next restore's
    anti-join finds nothing uncommitted — the failure heals itself."""
    from core import snapshot

    session = session or get_session()
    counts = {}
    for table in APPEND_TABLES:
        mine = appends.get(table)
        if mine is not None and mine.height:
            counts[table] = insert(table, mine, session)
    snapshot.remove_snapshots(user, session)
    return counts
