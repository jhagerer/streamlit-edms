"""Tables, views and pinned dtypes, mirrored from sql/minidms-setup.sql.

Pure: polars only. One global state lives in Snowflake:

* hybrid tables — everything people edit (current state, version column);
* standard tables — immutable facts and AI output (append-only, lineage).

Every frame the app loads goes through ``conform()`` so dtypes are pinned in
one place.
"""

from __future__ import annotations

import polars as pl

# Human-edited current state. Mutable, one row per entity, version-checked.
HYBRID_TABLES: tuple[str, ...] = (
    "document_type",
    "metadata_type",
    "tag",
    "document",
    "document_file",
    "document_metadata",
    "document_tag",
    "change_log",
    "sync_state",
)

# Immutable facts and AI output. Append-only; written by INSERT (… SELECT).
STANDARD_TABLES: tuple[str, ...] = (
    "stored_file",
    "ai_request",
    "document_text",
    "document_enrichment",
    "ai_job_log",
)

VIEWS: tuple[str, ...] = (
    "document_text_current",
    "document_enrichment_current",
    "ai_status_v",
    "audit_v",
)

# Tables with a change counter in sync_state (= cache keys).
TOKEN_TABLES: tuple[str, ...] = tuple(t for t in HYBRID_TABLES if t != "sync_state") + STANDARD_TABLES

# Tables / views the app bulk-loads (small: one row per entity or per file),
# with the change counters each one depends on. document_text (the large OCR
# text) and change_log (history) are never bulk-loaded.
LOADED: dict[str, tuple[str, ...]] = {
    "document_type": ("document_type",),
    "metadata_type": ("metadata_type",),
    "tag": ("tag",),
    "document": ("document",),
    "document_file": ("document_file",),
    "document_metadata": ("document_metadata",),
    "document_tag": ("document_tag",),
    "stored_file": ("stored_file",),
    "ai_status_v": ("ai_request", "document_text", "document_enrichment"),
    "document_enrichment_current": ("document_enrichment",),
}

TS = pl.Datetime("us")
S = pl.Utf8
B = pl.Boolean
I = pl.Int64

_AUDIT = {"created_at": TS, "created_by": S, "updated_at": TS, "updated_by": S}

COLUMNS: dict[str, dict[str, pl.DataType]] = {
    # ── hybrid ──
    "document_type": {"document_type_id": S, "label": S, "active": B, "version": I, **_AUDIT},
    "metadata_type": {
        "metadata_type_id": S, "name": S, "label": S, "data_type": S, "choices": S,
        "default_value": S, "active": B, "version": I, **_AUDIT,
    },
    "tag": {"tag_id": S, "label": S, "color": S, "active": B, "version": I, **_AUDIT},
    "document": {
        "document_id": S, "document_type_id": S, "label": S, "description": S, "language": S,
        "status": S, "version": I, **_AUDIT,
    },
    "document_file": {"file_id": S, "document_id": S, "active": B, "version": I, **_AUDIT},
    "document_metadata": {
        "document_id": S, "metadata_type_id": S, "value": S, "version": I,
        "updated_at": TS, "updated_by": S,
    },
    "document_tag": {"document_id": S, "tag_id": S, "assigned_at": TS, "assigned_by": S},
    "change_log": {
        "event_id": S, "event_ts": TS, "actor": S, "entity": S, "entity_id": S,
        "document_id": S, "op": S, "version": I, "data": S,
    },
    "sync_state": {"table_name": S, "seq": I, "changed_at": TS},
    # ── standard ──
    "stored_file": {
        "file_id": S, "document_id": S, "uploaded_at": TS, "uploaded_by": S, "filename": S,
        "stage_path": S, "mimetype": S, "size": I, "checksum": S,
    },
    "ai_request": {
        "event_id": S, "requested_at": TS, "actor": S, "file_id": S, "document_id": S,
        "reason": S,
    },
    "document_text": {
        "event_id": S, "extracted_at": TS, "actor": S, "file_id": S, "model": S, "status": S,
        "content": S, "error": S,
    },
    "document_enrichment": {
        "event_id": S, "created_at": TS, "actor": S, "file_id": S, "document_id": S,
        "model": S, "status": S, "summary": S, "suggested_type_id": S,
        "suggested_tag_ids": S, "extracted_metadata": S, "error": S,   # ARRAYs arrive as JSON text
    },
    "ai_job_log": {
        "event_id": S, "event_ts": TS, "request_id": S, "file_id": S, "document_id": S,
        "step": S, "message": S,
    },
    # ── views ──
    "ai_status_v": {
        "file_id": S, "requested_at": TS, "text_status": S, "text_error": S,
        "extracted_at": TS, "chars": I, "enrich_status": S, "enrich_error": S,
        "enriched_at": TS, "pending": B,
    },
}
COLUMNS["document_text_current"] = COLUMNS["document_text"]
COLUMNS["document_enrichment_current"] = COLUMNS["document_enrichment"]

DOCUMENT_STATUSES = ("active", "trashed")
DATA_TYPES = ("text", "number", "date", "choice")
AI_MODEL = "claude-sonnet-4-5"


def columns(table: str) -> list[str]:
    return list(COLUMNS[table])


def polars_schema(table: str) -> dict[str, pl.DataType]:
    return dict(COLUMNS[table])


def empty(table: str) -> pl.DataFrame:
    return pl.DataFrame(schema=polars_schema(table))


def conform(table: str, df: pl.DataFrame | None) -> pl.DataFrame:
    """Lower-case column names, add missing columns as null, drop unknown ones,
    cast to the pinned dtypes and return columns in DDL order."""
    if df is None:
        return empty(table)
    schema = COLUMNS[table]
    df = df.rename({c: c.lower() for c in df.columns if c != c.lower()})
    if not any(c in df.columns for c in schema):
        return empty(table)
    exprs = []
    for name, dtype in schema.items():
        if name not in df.columns:
            exprs.append(pl.lit(None, dtype=dtype).alias(name))
            continue
        col, src = pl.col(name), df.schema[name]
        if dtype == TS:
            if src == pl.Utf8:
                col = col.str.to_datetime(strict=False, time_unit="us")
            elif isinstance(src, pl.Datetime) and src.time_zone:
                # All timestamps are UTC, stored NTZ.
                col = col.dt.convert_time_zone("UTC").dt.replace_time_zone(None)
        elif dtype == B and src in (pl.Int64, pl.Int32, pl.Int8):
            col = col != 0
        elif dtype == S and src not in (pl.Utf8, pl.Null):
            col = col.cast(pl.Utf8, strict=False)
        exprs.append(col.cast(dtype, strict=False).alias(name))
    return df.select(exprs)
