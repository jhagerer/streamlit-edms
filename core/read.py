"""The read path: change tokens and cached bulk loads.

There is one global state, in Snowflake. The app keeps no copy of it in a
session; it caches *query results* where that is cheap and safe:

* The current-state tables are small (one row per entity) and are loaded
  whole into polars frames, cached and shared by all viewers.
* The cache key is the row count of the tables a frame is built from. Every
  table is append-only, so a table's count grows with every commit and never
  shrinks: a perfect change token, even for writes made outside the app, and
  no counters to maintain. One query per rerun reads all of them.
* A TTL is an extra safety net.

document_text_current and page_ocr (large OCR text) and the history are never
bulk-loaded; they are queried per document (search: Cortex Search).
"""

from __future__ import annotations

import time

import polars as pl

from core import db, schema
from core.schema import LOADED, TOKEN_TABLES
from core.session import cache_data

CACHE_TTL = 600  # seconds; safety net for writes made outside the app

_TOKEN_SQL = (
    "SELECT CURRENT_ACCOUNT(), CURRENT_ROLE(), CURRENT_DATABASE(), CURRENT_SCHEMA(), "
    + ", ".join(f"(SELECT COUNT(*) FROM {t})" for t in TOKEN_TABLES)
)


# ── Tokens ───────────────────────────────────────────────────────────────────


def change_tokens() -> dict[str, str]:
    """One round trip: the connection scope plus every table's row count.
    Deliberately NOT cached: the screen always matches the database as of
    this rerun. The scope keeps viewers on different connections apart."""
    row = db.query(_TOKEN_SQL)[0]
    scope = "/".join(str(v) for v in row[:4])
    return {t: f"{scope}|{row[4 + i]}" for i, t in enumerate(TOKEN_TABLES)}


def token_for(name: str, tokens: dict[str, str]) -> str:
    """Cache key of a loaded table/view: the counters it depends on."""
    return "+".join(tokens[t] for t in LOADED[name])


def scope_of(tokens: dict[str, str]) -> str:
    return next(iter(tokens.values())).split("|", 1)[0]


def time_probe(n: int = 20) -> float:
    """Average milliseconds per change_tokens() call."""
    start = time.perf_counter()
    for _ in range(n):
        change_tokens()
    return (time.perf_counter() - start) * 1000 / n


# ── Loaders ──────────────────────────────────────────────────────────────────


@cache_data(ttl=CACHE_TTL)
def load(name: str, token: str) -> pl.DataFrame:
    """One whole table / view. ``token`` is a cache key, never in the query."""
    if name not in LOADED:
        raise ValueError(f"{name} is not bulk-loadable")
    return schema.conform(name, db.query_frame(f"SELECT * FROM {name}"))


@cache_data(ttl=CACHE_TTL)
def load_texts(file_ids: tuple[str, ...], token: str) -> pl.DataFrame:
    """Latest OCR text for a handful of files (the Text tab)."""
    if not file_ids:
        return schema.empty("document_text_current")
    placeholders = ", ".join("?" for _ in file_ids)
    df = db.query_frame(
        f"SELECT * FROM document_text_current WHERE file_id IN ({placeholders})",
        params=list(file_ids),
    )
    return schema.conform("document_text_current", df)


@cache_data(ttl=CACHE_TTL)
def load_pages(file_id: str, token: str) -> pl.DataFrame:
    """Structured OCR of every page of one file, from its latest run."""
    df = db.query_frame(
        "SELECT * FROM page_ocr WHERE file_id = ? AND run_id = (SELECT run_id FROM page_ocr "
        "WHERE file_id = ? ORDER BY created_at DESC LIMIT 1) ORDER BY page_no",
        params=[file_id, file_id],
    )
    return schema.conform("page_ocr", df)


@cache_data(ttl=CACHE_TTL)
def load_history(document_id: str, token: str) -> pl.DataFrame:
    """Every human change of one document, oldest first."""
    df = db.query_frame(
        "SELECT * FROM document_history_v WHERE document_id = ? ORDER BY event_ts",
        params=[document_id],
    )
    return schema.conform("document_history_v", df)


def history_token(tokens: dict[str, str]) -> str:
    return "+".join(tokens[t] for t in ("document_log", "document_file_log", "metadata_log",
                                        "tag_assignment_log"))


def load_audit(limit: int = 5000) -> pl.DataFrame:
    df = db.query_frame(f"SELECT * FROM audit_v ORDER BY event_ts DESC LIMIT {int(limit)}")
    return df.rename({c: c.lower() for c in df.columns})


def warehouse_type(session=None) -> tuple[str | None, str | None]:
    """(current warehouse, its type) — e.g. ('MINIDMS_WH', 'ADAPTIVE')."""
    name = db.query("SELECT CURRENT_WAREHOUSE()", session=session)[0][0]
    if not name:
        return None, None
    safe = str(name).replace("'", "''")
    rows = db.query(f"SHOW WAREHOUSES LIKE '{safe}'", session=session)
    if not rows:
        return str(name), None
    d = {k.lower().strip('"'): v for k, v in rows[0].as_dict().items()}
    return str(name), (str(d.get("type")) if d.get("type") is not None else None)
