"""The one place where SQL is executed.

Streamlit serves all viewers from one process, and in Streamlit in Snowflake
(and with a shared connection from secrets) they share one Snowflake session.
A transaction is scoped to the session, so a statement from viewer B sent while
viewer A's BEGIN … COMMIT is open would silently become part of A's
transaction. Every statement therefore runs under a per-session lock, and a
transaction holds that lock from BEGIN to COMMIT. All statements are short —
the long AI calls run inside Snowflake (process_ai), never here.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

import polars as pl

from core.session import get_session

_locks: dict[int, threading.RLock] = {}
_locks_guard = threading.Lock()


class Conflict(Exception):
    """The row changed since the user loaded it (version mismatch), or a key
    the user tried to create exists already. Nothing was written."""


def lock_for(session) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(id(session), threading.RLock())


def query(sql: str, params: list | None = None, session=None) -> list:
    """Run one statement and return its rows."""
    session = session or get_session()
    with lock_for(session):
        return session.sql(sql, params=params).collect()


def query_frame(sql: str, params: list | None = None, session=None) -> pl.DataFrame:
    session = session or get_session()
    with lock_for(session):
        tbl = session.sql(sql, params=params).to_arrow()
    return pl.DataFrame() if tbl is None else pl.from_arrow(tbl)


def affected(rows: list) -> int:
    """Rows affected by INSERT / UPDATE / DELETE (first column of the result)."""
    try:
        return int(rows[0][0])
    except (IndexError, TypeError, ValueError):
        return 0


def is_duplicate_key(exc: Exception) -> bool:
    """A primary-key / unique violation (hybrid tables enforce them)."""
    msg = str(exc).lower()
    return any(s in msg for s in ("duplicate key", "unique constraint", "primary key",
                                  "unique key", "violates unique"))


class Tx:
    def __init__(self, session):
        self.session = session

    def run(self, sql: str, params: list | None = None) -> list:
        return self.session.sql(sql, params=params).collect()


@contextmanager
def transaction(session=None) -> Iterator[Tx]:
    """BEGIN … COMMIT under the session lock; ROLLBACK on any error."""
    session = session or get_session()
    with lock_for(session):
        session.sql("BEGIN TRANSACTION").collect()
        try:
            yield Tx(session)
        except BaseException:
            try:
                session.sql("ROLLBACK").collect()
            except Exception:
                pass  # keep the original error
            raise
        else:
            session.sql("COMMIT").collect()


def put_stream(data_stream: Any, stage_path: str, session=None, **kwargs) -> None:
    session = session or get_session()
    with lock_for(session):
        session.file.put_stream(data_stream, stage_path, **kwargs)


def get_stream(stage_path: str, session=None):
    session = session or get_session()
    with lock_for(session):
        return session.file.get_stream(stage_path)
