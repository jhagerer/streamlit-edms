"""Search with Cortex Search — the sink of the ingest pipeline.

process_ai() (sql/minidms-setup.sql) converts every file to PDF, splits it
into pages and writes a structured OCR per page to page_ocr. The Cortex Search
service document_search indexes one search document per page: its index_text
(page summary, headings, image and table summaries — not the raw OCR text).
The app sends the query to the service and gets back the best-matching pages
with their document_id, file_id and page number. Hybrid keyword + semantic
matching, ranked by relevance. Type, tag and trash filters are applied
afterwards in polars.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from core import db
from core.schema import SEARCH_SERVICE

COLUMNS = ["document_id", "file_id", "page_no", "page_short_summary", "index_text"]


@dataclass
class Hit:
    document_id: str
    file_id: str
    page_no: int | None
    text: str      # the page's short summary (or its index text)


def _literal(value: str) -> str:
    """A Snowflake single-quoted string literal (backslashes and quotes escaped)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def service_name(database: str, schema: str) -> str:
    def q(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'
    return f"{q(database)}.{q(schema)}.{SEARCH_SERVICE}"


def request_json(query: str, limit: int = 50) -> str:
    return json.dumps({"query": query, "columns": COLUMNS, "limit": int(limit)})


def search(query: str, database: str, schema: str, limit: int = 50, session=None) -> list[Hit]:
    """Ranked hits from the Cortex Search service. The query is serialised
    with json.dumps and passed as an escaped string literal — user input never
    becomes SQL."""
    query = (query or "").strip()
    if not query:
        return []
    sql = (f"SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW("
           f"{_literal(service_name(database, schema))}, {_literal(request_json(query, limit))})")
    raw = db.query(sql, session=session)[0][0]
    results = json.loads(raw).get("results", []) if raw else []
    return [Hit(str(r.get("document_id")), str(r.get("file_id")), _int(r.get("page_no")),
                str(r.get("page_short_summary") or r.get("index_text") or "")) for r in results]


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ranked_document_ids(hits: list[Hit], active_file_ids: set[str] | None = None) -> list[str]:
    """Document ids in order of their best hit; hits on detached files are
    dropped. Pure."""
    out: list[str] = []
    for h in hits:
        if active_file_ids is not None and h.file_id not in active_file_ids:
            continue
        if h.document_id not in out:
            out.append(h.document_id)
    return out


def best_snippets(hits: list[Hit], width: int = 220) -> dict[str, str]:
    """document_id -> its best page ("p. 3: summary"), shortened. Pure."""
    out: dict[str, str] = {}
    for h in hits:
        if h.document_id not in out:
            text = " ".join(h.text.split())
            if h.page_no is not None:
                text = f"p. {h.page_no}: {text}"
            out[h.document_id] = text if len(text) <= width else text[:width - 1] + "…"
    return out
