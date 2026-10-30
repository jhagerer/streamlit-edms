"""Full-text search over the OCR text and the AI summaries — the one read that
stays server-side, because document_text is never bulk-loaded. Snowflake
matches, polars filters."""

from __future__ import annotations

from core import db

MODES = {
    "All words": "AND",
    "Any word": "OR",
    "Exact phrase": "PHRASE",
    "Substring": "ILIKE",
}


def _condition(column: str, mode: str) -> str:
    if mode == "ILIKE":
        return f"{column} ILIKE ? ESCAPE '\\\\'"
    if mode not in ("AND", "OR", "PHRASE"):
        raise ValueError(f"bad search mode {mode!r}")
    return f"SEARCH({column}, ?, SEARCH_MODE => '{mode}')"


def search_ids(query: str, mode: str = "AND", session=None) -> list[str]:
    """Document ids whose text or summary matches. User input is always
    bound, never interpolated."""
    query = (query or "").strip()
    if not query:
        return []
    if mode == "ILIKE":
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        param = f"%{escaped}%"
    else:
        param = query
    sql = (
        "SELECT f.document_id FROM document_text_current t "
        "JOIN stored_file f ON f.file_id = t.file_id "
        f"WHERE t.status = 'done' AND {_condition('t.content', mode)} "
        "UNION "
        "SELECT e.document_id FROM document_enrichment_current e "
        f"WHERE e.status = 'done' AND {_condition('e.summary', mode)}"
    )
    return [r[0] for r in db.query(sql, [param, param], session=session)]
