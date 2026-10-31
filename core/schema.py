"""Tables, views and pinned dtypes, mirrored from sql/minidms-setup.sql.

Pure: polars only. One global state lives in Snowflake, and every table is
append-only:

* hybrid tables — everything people edit (one row per version, the current
  state is the latest version per key, the *_current views), plus
  document_enrichment, the AI result people act on (one row per file and run);
* standard tables — immutable facts and pipeline steps: the files, their PDF
  pages and the structured OCR per page that feeds the Cortex Search service.

Every frame the app loads goes through ``conform()`` so dtypes are pinned in
one place.
"""

from __future__ import annotations

import polars as pl

# Human-edited data. Append-only: PRIMARY KEY (key…, version), UNIQUE (event_id).
LOG_TABLES: tuple[str, ...] = (
    "document_type_log",
    "metadata_type_log",
    "tag_log",
    "document_log",
    "document_file_log",
    "metadata_log",
    "tag_assignment_log",
)

# Hybrid tables written by the ingest pipeline (a sink), with their keys.
PIPELINE_KEYS: dict[str, tuple[str, ...]] = {
    "document_enrichment": ("file_id", "run_id"),
}

HYBRID_TABLES: tuple[str, ...] = LOG_TABLES + tuple(PIPELINE_KEYS)

# Immutable facts and AI output. Append-only; written by INSERT (… SELECT).
STANDARD_TABLES: tuple[str, ...] = (
    "stored_file",
    "ai_request",
    "pdf_conversion",
    "pdf_page",
    "page_ocr",
    "ai_job_log",
)

VIEWS: tuple[str, ...] = (
    "document_type_current",
    "metadata_type_current",
    "tag_current",
    "document_current",
    "document_file_current",
    "metadata_current",
    "tag_assignment_current",
    "document_text_current",
    "document_enrichment_current",
    "ai_status_v",
    "document_history_v",
    "audit_v",
)

SEARCH_SERVICE = "document_search"

# Every table's row count is its change token: append-only, so the count
# grows with every commit and never shrinks.
TOKEN_TABLES: tuple[str, ...] = HYBRID_TABLES + STANDARD_TABLES

# Views the app bulk-loads (small: one row per entity or per file), with the
# tables whose change tokens they depend on. document_text_current and page_ocr
# (the large OCR text) and the history are never bulk-loaded.
LOADED: dict[str, tuple[str, ...]] = {
    "document_type_current": ("document_type_log",),
    "metadata_type_current": ("metadata_type_log",),
    "tag_current": ("tag_log",),
    "document_current": ("document_log",),
    "document_file_current": ("document_file_log",),
    "metadata_current": ("metadata_log",),
    "tag_assignment_current": ("tag_assignment_log",),
    "stored_file": ("stored_file",),
    "ai_status_v": ("ai_request", "pdf_conversion", "page_ocr", "document_enrichment",
                    "ai_job_log"),
    "document_enrichment_current": ("document_enrichment",),
}

# Version chains of the hybrid logs: key columns and the record's fields.
LOG_KEYS: dict[str, tuple[str, ...]] = {
    "document_type_log": ("document_type_id",),
    "metadata_type_log": ("metadata_type_id",),
    "tag_log": ("tag_id",),
    "document_log": ("document_id",),
    "document_file_log": ("file_id",),
    "metadata_log": ("document_id", "metadata_type_id"),
    "tag_assignment_log": ("document_id", "tag_id"),
}

TS = pl.Datetime("us")
S = pl.Utf8
B = pl.Boolean
I = pl.Int64

_EVENT = {"version": I, "event_id": S, "event_ts": TS, "actor": S, "op": S}
_CREATED = {"created_at": TS, "created_by": S, "updated_at": TS, "updated_by": S}

COLUMNS: dict[str, dict[str, pl.DataType]] = {
    # ── hybrid logs ──
    "document_type_log": {"document_type_id": S, **_EVENT, "label": S, "active": B, "source": S},
    "metadata_type_log": {
        "metadata_type_id": S, **_EVENT, "name": S, "label": S, "data_type": S, "choices": S,
        "default_value": S, "active": B, "source": S,
    },
    "tag_log": {"tag_id": S, **_EVENT, "label": S, "color": S, "active": B, "source": S},
    "document_log": {
        "document_id": S, **_EVENT, "document_type_id": S, "label": S, "description": S,
        "language": S, "status": S, "source": S,
    },
    "document_file_log": {"file_id": S, **_EVENT, "document_id": S, "active": B, "source": S},
    "metadata_log": {"document_id": S, "metadata_type_id": S, **_EVENT, "value": S, "source": S},
    "tag_assignment_log": {"document_id": S, "tag_id": S, **_EVENT, "assigned": B, "source": S},
    # ── standard ──
    "stored_file": {
        "file_id": S, "document_id": S, "uploaded_at": TS, "uploaded_by": S, "filename": S,
        "stage_path": S, "mimetype": S, "size": I, "checksum": S,
    },
    "ai_request": {
        "event_id": S, "requested_at": TS, "actor": S, "file_id": S, "document_id": S,
        "reason": S,
    },
    "document_enrichment": {
        "file_id": S, "run_id": S, "event_id": S, "created_at": TS, "actor": S,
        "document_id": S, "model": S, "status": S, "summary": S, "suggested_type_id": S,
        "suggested_tag_ids": S, "extracted_metadata": S, "error": S,   # ARRAYs arrive as JSON text
    },
    "pdf_conversion": {
        "event_id": S, "created_at": TS, "run_id": S, "file_id": S, "status": S,
        "converter": S, "pdf_path": S, "page_count": I, "error": S,
    },
    "pdf_page": {
        "event_id": S, "created_at": TS, "run_id": S, "file_id": S, "page_no": I,
        "stage_path": S,
    },
    "page_ocr": {
        "event_id": S, "created_at": TS, "run_id": S, "file_id": S, "document_id": S,
        "filename": S, "page_no": I, "model": S, "status": S, "ocr_text": S,
        "headings_list": S, "image_short_summaries_list": S, "table_summaries_list": S,
        "page_short_summary": S, "index_text": S, "error": S,
    },
    "ai_job_log": {
        "event_id": S, "event_ts": TS, "request_id": S, "file_id": S, "document_id": S,
        "step": S, "message": S,
    },
    # ── views ──
    "document_type_current": {"document_type_id": S, "label": S, "active": B, "version": I,
                              **_CREATED},
    "metadata_type_current": {
        "metadata_type_id": S, "name": S, "label": S, "data_type": S, "choices": S,
        "default_value": S, "active": B, "version": I, **_CREATED,
    },
    "tag_current": {"tag_id": S, "label": S, "color": S, "active": B, "version": I, **_CREATED},
    "document_current": {
        "document_id": S, "document_type_id": S, "label": S, "description": S, "language": S,
        "status": S, "version": I, **_CREATED,
    },
    "document_file_current": {"file_id": S, "document_id": S, "active": B, "version": I,
                              **_CREATED},
    "metadata_current": {"document_id": S, "metadata_type_id": S, "value": S, "version": I,
                         "updated_at": TS, "updated_by": S},
    "tag_assignment_current": {"document_id": S, "tag_id": S, "assigned": B, "version": I,
                               "assigned_at": TS, "assigned_by": S},
    "ai_status_v": {
        "file_id": S, "requested_at": TS, "text_status": S, "text_error": S,
        "extracted_at": TS, "chars": I, "pages": I, "enrich_status": S, "enrich_error": S,
        "enriched_at": TS, "pending": B,
    },
    "document_history_v": {
        "event_ts": TS, "actor": S, "entity": S, "document_id": S, "item_id": S, "op": S,
        "version": I, "value": S, "source": S,
    },
}
COLUMNS["document_text_current"] = {
    "file_id": S, "run_id": S, "extracted_at": TS, "model": S, "status": S, "pages": I,
    "content": S, "error": S,
}
COLUMNS["document_enrichment_current"] = COLUMNS["document_enrichment"]

DOCUMENT_STATUSES = ("active", "trashed")
DATA_TYPES = ("text", "number", "date", "choice")
OCR_MODEL = "claude-haiku-4-5"      # structured OCR per page
AI_MODEL = "claude-sonnet-4-5"      # enrichment per file


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
