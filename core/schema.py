"""Table definitions mirrored from sql/minidms-setup.sql.

Pure: polars only. Column order matches the DDL. Every frame that enters the
app (bulk load, snapshot restore, new append) is passed through ``conform()``
so dtypes are pinned in one place and concatenation never shifts types.
"""

from __future__ import annotations

import polars as pl

# Bulk-loaded into memory, reduced in polars, rendered.
READ_TABLES: tuple[str, ...] = (
    "document_log",
    "document_file_log",
    "metadata_log",
    "tag_assignment_log",
    "document_type_log",
    "metadata_type_log",
    "tag_log",
)

# Appended to, persisted to parquet, flushed. Superset of READ_TABLES.
# document_text is NEVER bulk-loaded; it only gets an id-only loader.
APPEND_TABLES: tuple[str, ...] = READ_TABLES + ("document_text",)

TS = pl.Datetime("us")
S = pl.Utf8
B = pl.Boolean
I = pl.Int64

_EVENT = {"event_id": S, "event_ts": TS, "actor": S}

COLUMNS: dict[str, dict[str, pl.DataType]] = {
    "document_log": {
        **_EVENT,
        "op": S,
        "document_id": S,
        "document_type_id": S,
        "label": S,
        "description": S,
        "language": S,
    },
    "document_file_log": {
        **_EVENT,
        "active": B,
        "document_id": S,
        "file_id": S,
        "filename": S,
        "stage_path": S,
        "mimetype": S,
        "size": I,
        "checksum": S,
        "page_count": I,
    },
    "metadata_log": {
        **_EVENT,
        "document_id": S,
        "metadata_type_id": S,
        "value": S,
    },
    "tag_assignment_log": {
        **_EVENT,
        "document_id": S,
        "tag_id": S,
        "assigned": B,
    },
    "document_type_log": {
        **_EVENT,
        "active": B,
        "document_type_id": S,
        "label": S,
    },
    "metadata_type_log": {
        **_EVENT,
        "active": B,
        "metadata_type_id": S,
        "name": S,
        "label": S,
        "data_type": S,
        "choices": S,
        "default_value": S,
    },
    "tag_log": {
        **_EVENT,
        "active": B,
        "tag_id": S,
        "label": S,
        "color": S,
    },
    "document_text": {
        **_EVENT,
        "file_id": S,
        "content": S,
    },
}

# Reduction keys: current state = latest row per key by (event_ts, event_id).
KEYS: dict[str, list[str]] = {
    "document_log": ["document_id"],
    "document_file_log": ["file_id"],
    "metadata_log": ["document_id", "metadata_type_id"],
    "tag_assignment_log": ["document_id", "tag_id"],
    "document_type_log": ["document_type_id"],
    "metadata_type_log": ["metadata_type_id"],
    "tag_log": ["tag_id"],
    "document_text": ["file_id"],
}

DOCUMENT_OPS = ("create", "update", "trash", "restore")
DATA_TYPES = ("text", "number", "date", "choice")


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
        if name in df.columns:
            col = pl.col(name)
            if dtype == TS and df.schema[name] != TS:
                # Drop any timezone; all event timestamps are UTC, stored NTZ.
                if isinstance(df.schema[name], pl.Datetime) and df.schema[name].time_zone:
                    col = col.dt.convert_time_zone("UTC").dt.replace_time_zone(None)
            exprs.append(col.cast(dtype, strict=False).alias(name))
        else:
            exprs.append(pl.lit(None, dtype=dtype).alias(name))
    return df.select(exprs)
