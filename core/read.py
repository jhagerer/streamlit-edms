"""The read path: change tokens, cached bulk loaders, reduction and merge.

All reads are bulk. Each table is loaded whole into a polars frame, keyed on
its change token, and shared by every viewer through the Streamlit cache. The
UI never issues per-interaction queries except the token probe (and the
Text tab / search, which touch ``document_text``).
"""

from __future__ import annotations

import time

import polars as pl

from core import schema
from core.schema import APPEND_TABLES, READ_TABLES
from core.session import cache_data, get_session

# ── Tokens ───────────────────────────────────────────────────────────────────


def _probe_one(session, table: str) -> str:
    try:
        row = session.sql(f"SELECT SYSTEM$LAST_CHANGE_COMMIT_TIME('{table}')").collect()[0]
        return str(row[0])
    except Exception:
        # Fallback that is still cheap and still moves on every append.
        row = session.sql(f"SELECT COUNT(*), MAX(event_ts) FROM {table}").collect()[0]
        return f"{row[0]}:{row[1]}"


def change_tokens() -> dict[str, str]:
    """One round trip, all tokens. Deliberately NOT cached: the UI is always
    consistent with the database as of this rerun."""
    session = get_session()
    cols = ", ".join(f"SYSTEM$LAST_CHANGE_COMMIT_TIME('{t}')" for t in APPEND_TABLES)
    try:
        row = session.sql(f"SELECT {cols}").collect()[0]
        return {t: str(row[i]) for i, t in enumerate(APPEND_TABLES)}
    except Exception:
        return {t: _probe_one(session, t) for t in APPEND_TABLES}


def time_probe(n: int = 20) -> float:
    """Average milliseconds per change_tokens() call."""
    start = time.perf_counter()
    for _ in range(n):
        change_tokens()
    return (time.perf_counter() - start) * 1000 / n


# ── Loaders ──────────────────────────────────────────────────────────────────


def _arrow_to_polars(tbl) -> pl.DataFrame:
    if tbl is None:
        return pl.DataFrame()
    return pl.from_arrow(tbl)


def query_frame(sql: str, params: list | None = None) -> pl.DataFrame:
    session = get_session()
    return _arrow_to_polars(session.sql(sql, params=params).to_arrow())


@cache_data
def load_log(table: str, token: str) -> pl.DataFrame:
    """Full rows. READ_TABLES only. ``token`` is a cache key, never in the query."""
    if table not in READ_TABLES:
        raise ValueError(f"{table} is not bulk-loadable")
    return schema.conform(table, query_frame(f"SELECT * FROM {table}"))


@cache_data
def load_event_ids(table: str, token: str) -> pl.DataFrame:
    """Ids only — for tables too large to bulk-load (document_text)."""
    if table not in APPEND_TABLES:
        raise ValueError(f"unknown table {table}")
    df = query_frame(f"SELECT event_id FROM {table}")
    if not df.width:
        return pl.DataFrame(schema={"event_id": pl.Utf8})
    return df.rename({df.columns[0]: "event_id"}).select(pl.col("event_id").cast(pl.Utf8))


@cache_data
def load_texts(file_ids: tuple[str, ...], token: str) -> pl.DataFrame:
    """Latest extracted text for a handful of files (the Text tab)."""
    if not file_ids:
        return schema.empty("document_text")
    placeholders = ", ".join("?" for _ in file_ids)
    df = query_frame(
        f"SELECT * FROM document_text WHERE file_id IN ({placeholders})",
        params=list(file_ids),
    )
    return latest(schema.conform("document_text", df), schema.KEYS["document_text"])


def load_audit(limit: int = 5000) -> pl.DataFrame:
    df = query_frame(f"SELECT * FROM audit_v ORDER BY event_ts DESC LIMIT {int(limit)}")
    return df.rename({c: c.lower() for c in df.columns})


# ── Reduction and merge (pure) ───────────────────────────────────────────────


def latest(log: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    """Current state: the last row per key by (event_ts, event_id). The event_id
    tie-break makes the reduction deterministic for rows of one flush, which
    share a timestamp."""
    return log.sort(["event_ts", "event_id"]).unique(
        subset=keys, keep="last", maintain_order=True
    )


def compose(committed: pl.DataFrame, appends: pl.DataFrame | None) -> pl.DataFrame:
    """Committed rows + this session's appends, annotating ``_pending``."""
    db = committed.with_columns(pl.lit(False).alias("_pending"))
    if appends is None or not appends.height:
        return db
    return pl.concat(
        [db, appends.with_columns(pl.lit(True).alias("_pending"))],
        how="vertical_relaxed",
    )


def view(table: str, appends: pl.DataFrame | None, tokens: dict[str, str]) -> pl.DataFrame:
    """Committed rows (cached, shared) + this session's appends (tier 1).

    Takes appends and tokens as arguments so core/ never imports from the
    pages. The result is recomputed every rerun and never stored."""
    return compose(load_log(table, tokens[table]), appends)


def clean_restored(snap: pl.DataFrame | None, committed_ids: pl.DataFrame) -> pl.DataFrame | None:
    """Drop staged rows that are already committed. Pure: no I/O."""
    if snap is None or not snap.height:
        return None
    if not committed_ids.height:
        return snap
    return snap.join(committed_ids.select("event_id"), on="event_id", how="anti")


def restore(user: str, tokens: dict[str, str]) -> dict[str, pl.DataFrame]:
    """Tier 2 minus tier 3: this user's snapshot rows that are still uncommitted.
    Returns the frames; the caller puts them into tier 1."""
    from core import snapshot

    snaps = snapshot.read_all(user)
    restored: dict[str, pl.DataFrame] = {}
    for table in APPEND_TABLES:
        snap = snaps.get(table)
        if snap is None:
            continue
        committed = (
            load_log(table, tokens[table])
            if table in READ_TABLES
            else load_event_ids(table, tokens[table])
        )
        mine = clean_restored(schema.conform(table, snap), committed)
        if mine is not None and mine.height:
            restored[table] = mine
    return restored
