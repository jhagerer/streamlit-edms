"""Current state, derived from event logs. Pure polars; no I/O.

Every function takes frames as returned by ``read.view()`` (committed rows
plus this session's appends, with ``_pending``) and reduces them.
"""

from __future__ import annotations

import polars as pl

from core.read import latest
from core.schema import KEYS


def _pending_col(df: pl.DataFrame) -> pl.DataFrame:
    if "_pending" not in df.columns:
        return df.with_columns(pl.lit(False).alias("_pending"))
    return df


def documents(doc_log: pl.DataFrame) -> pl.DataFrame:
    """One row per document with lifecycle columns. Includes trashed ones."""
    doc_log = _pending_col(doc_log)
    created = doc_log.group_by("document_id").agg(
        pl.col("event_ts").min().alias("created_at"),
        pl.col("actor").sort_by(["event_ts", "event_id"]).first().alias("created_by"),
        pl.col("_pending").any().alias("_doc_pending"),
    )
    cur = latest(doc_log, KEYS["document_log"]).rename(
        {"event_ts": "updated_at", "actor": "updated_by"}
    )
    return (
        cur.join(created, on="document_id", how="left")
        .with_columns((pl.col("op") == "trash").alias("trashed"))
        .drop("event_id")
    )


def definitions(log: pl.DataFrame, table: str, include_inactive: bool = False) -> pl.DataFrame:
    """Document types, metadata types, tags."""
    cur = latest(_pending_col(log), KEYS[table])
    if not include_inactive:
        cur = cur.filter(pl.col("active").fill_null(True))
    return cur.sort("label", nulls_last=True)


def current_files(file_log: pl.DataFrame, include_inactive: bool = False) -> pl.DataFrame:
    cur = latest(_pending_col(file_log), KEYS["document_file_log"])
    if not include_inactive:
        cur = cur.filter(pl.col("active").fill_null(True))
    return cur.sort("event_ts")


def current_metadata(meta_log: pl.DataFrame) -> pl.DataFrame:
    """Latest value per (document, metadata type); empty value = removed."""
    cur = latest(_pending_col(meta_log), KEYS["metadata_log"])
    return cur.filter(pl.col("value").is_not_null() & (pl.col("value") != ""))


def current_tags(assign_log: pl.DataFrame) -> pl.DataFrame:
    """Latest link per (document, tag); assigned = FALSE means detached."""
    cur = latest(_pending_col(assign_log), KEYS["tag_assignment_log"])
    return cur.filter(pl.col("assigned").fill_null(False))


def document_table(
    doc_log: pl.DataFrame,
    doc_type_log: pl.DataFrame,
    file_log: pl.DataFrame,
    meta_log: pl.DataFrame,
    assign_log: pl.DataFrame,
    tag_log: pl.DataFrame,
) -> pl.DataFrame:
    """The list the Documents page renders: one row per document, joined to its
    type, tags, files and metadata. ``pending`` is true when any of the
    document's rows are still unsaved."""
    docs = documents(doc_log)
    types = definitions(doc_type_log, "document_type_log", include_inactive=True).select(
        "document_type_id", pl.col("label").alias("document_type")
    )
    files = current_files(file_log)
    file_agg = files.group_by("document_id").agg(
        pl.len().alias("files"),
        pl.col("filename").alias("filenames"),
        pl.col("_pending").any().alias("_file_pending"),
    )
    tag_defs = definitions(tag_log, "tag_log", include_inactive=True).select(
        "tag_id", pl.col("label").alias("tag_label"), pl.col("active").alias("tag_active")
    )
    all_links = _pending_col(assign_log)
    tag_pending = all_links.group_by("document_id").agg(pl.col("_pending").any().alias("_tag_pending"))
    tags = (
        current_tags(assign_log)
        .join(tag_defs, on="tag_id", how="left")
        .filter(pl.col("tag_active").fill_null(True))
        .group_by("document_id")
        .agg(pl.col("tag_id").alias("tag_ids"), pl.col("tag_label").sort().alias("tags"))
    )
    meta_all = _pending_col(meta_log)
    meta_pending = meta_all.group_by("document_id").agg(pl.col("_pending").any().alias("_meta_pending"))
    meta = current_metadata(meta_log).group_by("document_id").agg(
        pl.col("value").str.join(" ").alias("metadata_text")
    )
    out = (
        docs.join(types, on="document_type_id", how="left")
        .join(file_agg, on="document_id", how="left")
        .join(tags, on="document_id", how="left")
        .join(tag_pending, on="document_id", how="left")
        .join(meta, on="document_id", how="left")
        .join(meta_pending, on="document_id", how="left")
        .with_columns(
            pl.col("files").fill_null(0),
            pl.col("filenames").fill_null([]),
            pl.col("tags").fill_null([]),
            pl.col("tag_ids").fill_null([]),
            pl.col("metadata_text").fill_null(""),
            pl.any_horizontal(
                pl.col("_doc_pending").fill_null(False),
                pl.col("_file_pending").fill_null(False),
                pl.col("_tag_pending").fill_null(False),
                pl.col("_meta_pending").fill_null(False),
            ).alias("pending"),
        )
        .drop("_pending", "_doc_pending", "_file_pending", "_tag_pending", "_meta_pending")
    )
    return out.sort("updated_at", descending=True)


def filter_documents(
    table: pl.DataFrame,
    text: str = "",
    document_type_ids: list[str] | None = None,
    tag_ids: list[str] | None = None,
    trashed: bool = False,
    ids: list[str] | None = None,
) -> pl.DataFrame:
    """In-memory filters. ``tag_ids`` requires all given tags."""
    out = table.filter(pl.col("trashed") == trashed)
    if ids is not None:
        out = out.filter(pl.col("document_id").is_in(ids))
    if document_type_ids:
        out = out.filter(pl.col("document_type_id").is_in(document_type_ids))
    for tag in tag_ids or []:
        out = out.filter(pl.col("tag_ids").list.contains(tag))
    text = (text or "").strip().lower()
    if text:
        hay = pl.concat_str(
            [
                pl.col("label").fill_null(""),
                pl.col("description").fill_null(""),
                pl.col("filenames").list.join(" "),
                pl.col("metadata_text"),
                pl.col("tags").list.join(" "),
            ],
            separator=" ",
        ).str.to_lowercase()
        out = out.filter(hay.str.contains(text, literal=True))
    return out


def history(log: pl.DataFrame, **match: str) -> pl.DataFrame:
    """Unreduced events for one object, oldest first."""
    out = _pending_col(log)
    for col, value in match.items():
        out = out.filter(pl.col(col) == value)
    return out.sort(["event_ts", "event_id"])
