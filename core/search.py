"""Full-text search over extracted text — the one read that stays server-side,
because document_text is never bulk-loaded. Snowflake matches, polars filters."""

from __future__ import annotations

from core.session import get_session

MODES = {
    "All words": "AND",
    "Any word": "OR",
    "Exact phrase": "PHRASE",
    "Substring": "ILIKE",
}


def search_ids(query: str, mode: str = "AND", session=None) -> list[str]:
    """Document ids whose committed text matches. User input is always bound,
    never interpolated."""
    query = (query or "").strip()
    if not query:
        return []
    session = session or get_session()
    base = (
        "SELECT DISTINCT f.document_id FROM document_text t "
        "JOIN document_file_log f ON f.file_id = t.file_id WHERE "
    )
    if mode == "ILIKE":
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = session.sql(base + "t.content ILIKE ? ESCAPE '\\\\'",
                           params=[f"%{escaped}%"]).collect()
    else:
        if mode not in ("AND", "OR", "PHRASE"):
            raise ValueError(f"bad search mode {mode!r}")
        rows = session.sql(base + f"SEARCH(t.content, ?, SEARCH_MODE => '{mode}')",
                           params=[query]).collect()
    return [r[0] for r in rows]
