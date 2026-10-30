"""What the pages show, built from the loaded tables. Pure polars; no I/O.

The tables already hold current state (hybrid tables are updated in place;
the views pick the latest AI result per file), so this module only joins and
filters.
"""

from __future__ import annotations

import json

import polars as pl

AI_STATE_LABELS = {
    "missing": "Not requested yet — use “Process again”.",
    "queued": "Queued — OCR and enrichment are running in Snowflake.",
    "done": "Text extracted and enriched.",
    "failed": "AI processing failed.",
    "skipped": "No text extraction for this file type.",
}


def parse_list(value) -> list:
    """ARRAY columns arrive as JSON text."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return []
    return out if isinstance(out, list) else []


def definitions(df: pl.DataFrame, include_inactive: bool = False) -> pl.DataFrame:
    """Document types, metadata types, tags."""
    if not include_inactive:
        df = df.filter(pl.col("active").fill_null(True))
    return df.sort("label", nulls_last=True)


def files_view(document_file: pl.DataFrame, stored_file: pl.DataFrame,
               ai_status: pl.DataFrame, include_inactive: bool = False) -> pl.DataFrame:
    """Attached files with their stored details and AI state."""
    links = document_file if include_inactive else document_file.filter(pl.col("active"))
    stored = stored_file.unique(subset=["file_id"], keep="first").drop("document_id")
    out = (
        links.join(stored, on="file_id", how="left")
        .join(ai_status, on="file_id", how="left")
    )
    return out.with_columns(
        pl.when(pl.col("requested_at").is_null()).then(pl.lit("missing"))
        .when(pl.col("pending").fill_null(False)).then(pl.lit("queued"))
        .when((pl.col("text_status") == "failed") | (pl.col("enrich_status") == "failed"))
        .then(pl.lit("failed"))
        .when(pl.col("text_status") == "skipped").then(pl.lit("skipped"))
        .otherwise(pl.lit("done"))
        .alias("ai_state")
    ).sort("created_at")


def _doc_ai_state(states: pl.Expr) -> pl.Expr:
    return (
        pl.when(states.list.contains("queued")).then(pl.lit("queued"))
        .when(states.list.contains("failed")).then(pl.lit("failed"))
        .when(states.list.contains("missing")).then(pl.lit("missing"))
        .when(states.list.len() == 0).then(pl.lit(None, dtype=pl.Utf8))
        .otherwise(pl.lit("done"))
    )


def document_table(docs: pl.DataFrame, types: pl.DataFrame, files: pl.DataFrame,
                   doc_meta: pl.DataFrame, doc_tags: pl.DataFrame, tags: pl.DataFrame,
                   enrich: pl.DataFrame) -> pl.DataFrame:
    """One row per document, joined to its type, files, tags, metadata and
    AI summary."""
    type_labels = types.select("document_type_id", pl.col("label").alias("document_type"))
    file_agg = files.group_by("document_id").agg(
        pl.len().alias("files"),
        pl.col("filename").alias("filenames"),
        pl.col("ai_state").alias("_states"),
    )
    tag_agg = (
        doc_tags.join(tags.select("tag_id", pl.col("label").alias("tag_label"),
                                  pl.col("active").alias("tag_active")), on="tag_id", how="left")
        .filter(pl.col("tag_active").fill_null(True))
        .group_by("document_id")
        .agg(pl.col("tag_id").alias("tag_ids"), pl.col("tag_label").sort().alias("tags"))
    )
    meta_agg = doc_meta.group_by("document_id").agg(
        pl.col("value").str.join(" ").alias("metadata_text"))
    summary = (
        enrich.filter(pl.col("status") == "done")
        .sort("created_at")
        .group_by("document_id")
        .agg(pl.col("summary").last().alias("summary"))
    )
    out = (
        docs.join(type_labels, on="document_type_id", how="left")
        .join(file_agg, on="document_id", how="left")
        .join(tag_agg, on="document_id", how="left")
        .join(meta_agg, on="document_id", how="left")
        .join(summary, on="document_id", how="left")
        .with_columns(
            pl.col("files").fill_null(0),
            pl.col("filenames").fill_null([]),
            pl.col("tags").fill_null([]),
            pl.col("tag_ids").fill_null([]),
            pl.col("metadata_text").fill_null(""),
            pl.col("summary").fill_null(""),
            pl.col("_states").fill_null([]),
            (pl.col("status") == "trashed").alias("trashed"),
        )
        .with_columns(_doc_ai_state(pl.col("_states")).alias("ai_state"))
        .drop("_states")
    )
    return out.sort("updated_at", descending=True)


def filter_documents(table: pl.DataFrame, text: str = "", document_type_ids: list[str] | None = None,
                     tag_ids: list[str] | None = None, trashed: bool = False,
                     ids: list[str] | None = None) -> pl.DataFrame:
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
            [pl.col("label").fill_null(""), pl.col("description").fill_null(""),
             pl.col("filenames").list.join(" "), pl.col("metadata_text"),
             pl.col("tags").list.join(" "), pl.col("summary")],
            separator=" ",
        ).str.to_lowercase()
        out = out.filter(hay.str.contains(text, literal=True))
    return out


def suggestions(enrichment: dict | None, document: dict, tag_ids: list[str],
                metadata: dict[str, str], active_type_ids: set[str], active_tag_ids: set[str],
                metadata_types: dict[str, dict]) -> dict:
    """What the AI suggests that differs from the current state, limited to
    ids that exist and are active. Pure."""
    out = {"document_type_id": None, "tag_ids": [], "metadata": {}}
    if not enrichment or enrichment.get("status") != "done":
        return out
    t = enrichment.get("suggested_type_id")
    if t and t in active_type_ids and t != document.get("document_type_id"):
        out["document_type_id"] = t
    out["tag_ids"] = [x for x in parse_list(enrichment.get("suggested_tag_ids"))
                      if isinstance(x, str) and x in active_tag_ids and x not in tag_ids]
    for item in parse_list(enrichment.get("extracted_metadata")):
        if not isinstance(item, dict):
            continue
        mt_id, value = item.get("metadata_type_id"), str(item.get("value") or "").strip()
        mt = metadata_types.get(mt_id)
        if not mt or not mt.get("active", True) or not value:
            continue
        if mt.get("data_type") == "choice":
            choices = [c.strip() for c in (mt.get("choices") or "").splitlines() if c.strip()]
            if value not in choices:
                continue
        if metadata.get(mt_id) != value:
            out["metadata"][mt_id] = value
    return out


def has_suggestions(s: dict) -> bool:
    return bool(s["document_type_id"] or s["tag_ids"] or s["metadata"])
