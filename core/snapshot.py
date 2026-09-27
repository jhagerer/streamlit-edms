"""Tier 2: per-user parquet snapshots of un-flushed appends on @sessions.

Layout: ``@sessions/{slug(user)}/{table}.parquet``. The functions take the
user identifier as an argument; nothing here knows about Streamlit.
"""

from __future__ import annotations

import io
from datetime import datetime

import polars as pl

from core.schema import APPEND_TABLES
from core.session import get_session, slug

STAGE = "@sessions"


def session_folder(user: str) -> str:
    return f"{STAGE}/{slug(user)}/"


def session_path(user: str, table: str) -> str:
    return f"{session_folder(user)}{table}.parquet"


def to_parquet_bytes(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def from_parquet_bytes(data: bytes) -> pl.DataFrame:
    return pl.read_parquet(io.BytesIO(data))


def _list(location: str, session=None) -> list[dict]:
    session = session or get_session()
    try:
        rows = session.sql(f"LIST {location}").collect()
    except Exception:
        return []
    out = []
    for r in rows:
        d = {k.lower(): v for k, v in r.as_dict().items()}
        out.append(d)
    return out


def write_snapshot(user: str, table: str, df: pl.DataFrame, session=None) -> None:
    """overwrite=True is essential: without it PUT silently skips an existing
    file and the save looks successful while changing nothing."""
    session = session or get_session()
    session.file.put_stream(
        io.BytesIO(to_parquet_bytes(df)),
        session_path(user, table),
        auto_compress=False,
        overwrite=True,
    )


def read_snapshot(user: str, table: str, session=None) -> pl.DataFrame | None:
    session = session or get_session()
    try:
        stream = session.file.get_stream(session_path(user, table))
        return from_parquet_bytes(stream.read())
    except Exception:
        return None  # no snapshot for this user/table


def read_all(user: str, session=None) -> dict[str, pl.DataFrame]:
    """All snapshot files of one user. One LIST, then only the files that exist."""
    session = session or get_session()
    present = set()
    for f in _list(session_folder(user), session):
        name = str(f.get("name", "")).rsplit("/", 1)[-1]
        if name.endswith(".parquet"):
            present.add(name[: -len(".parquet")])
    out = {}
    for table in APPEND_TABLES:
        if table in present:
            df = read_snapshot(user, table, session)
            if df is not None:
                out[table] = df
    return out


def remove_snapshots(user: str, session=None) -> None:
    """REMOVE this user's folder only. The trailing slash keeps 'alice/' from
    matching 'alice2/'."""
    session = session or get_session()
    session.sql(f"REMOVE {session_folder(user)}").collect()


def remove_folder(folder: str, session=None) -> None:
    """Admin housekeeping: remove one user folder by its slug."""
    session = session or get_session()
    safe = slug(folder)
    session.sql(f"REMOVE {STAGE}/{safe}/").collect()


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def list_snapshots(session=None) -> pl.DataFrame:
    """Every user's snapshot files, for the admin page."""
    rows = []
    for f in _list(STAGE, session):
        parts = str(f.get("name", "")).split("/")
        if len(parts) < 3:
            continue
        rows.append(
            {
                "folder": parts[1],
                "table": parts[-1].removesuffix(".parquet"),
                "size": int(f.get("size") or 0),
                "last_modified": _parse_ts(f.get("last_modified")),
            }
        )
    return pl.DataFrame(
        rows,
        schema={
            "folder": pl.Utf8,
            "table": pl.Utf8,
            "size": pl.Int64,
            "last_modified": pl.Datetime("us"),
        },
    )
