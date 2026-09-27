"""Test fixtures: a small in-memory stand-in for a Snowpark session, enough for
the stage and log-table operations core/ performs. No Snowflake needed."""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pyarrow as pa
import pytest
from snowflake.snowpark import Row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import schema  # noqa: E402


class _Result:
    def __init__(self, rows=None, arrow=None):
        self._rows, self._arrow = rows or [], arrow

    def collect(self):
        return self._rows

    def to_arrow(self):
        return self._arrow


class _Files:
    def __init__(self, fake):
        self.fake = fake

    def put_stream(self, stream, path, *, auto_compress=True, overwrite=False, **_):
        key = path.lstrip("@")
        if key in self.fake.stage and not overwrite:
            return  # real PUT silently skips an existing file
        self.fake.stage[key] = stream.read()

    def get_stream(self, path, **_):
        key = path.lstrip("@")
        if key not in self.fake.stage:
            raise FileNotFoundError(path)
        return io.BytesIO(self.fake.stage[key])


class _Writer:
    def __init__(self, fake, rows, names):
        self.fake, self.rows, self.names = fake, rows, names

    def save_as_table(self, table, mode=None, column_order="index", **_):
        assert mode == "append"
        if self.fake.fail_insert_on == table:
            raise RuntimeError(f"insert into {table} failed")
        self.fake.tables.setdefault(table, [])
        for r in self.rows:
            self.fake.tables[table].append(dict(zip(self.names, r)))
        self.fake.commits[table] = self.fake.commits.get(table, 0) + 1


class _SpDF:
    def __init__(self, fake, rows, schema_):
        self.write = _Writer(fake, rows, [f.name.lower() for f in schema_.fields])


class FakeSession:
    def __init__(self):
        self.stage: dict[str, bytes] = {}
        self.tables: dict[str, list[dict]] = {t: [] for t in schema.APPEND_TABLES}
        self.commits: dict[str, int] = {}
        self.fail_remove = False
        self.fail_insert_on: str | None = None
        self.file = _Files(self)
        self.queries: list[str] = []
        self.schema_missing = False  # simulate a fresh schema without MiniDMS objects
        self.offline = False  # simulate "no connection configured"
        self.closed = False
        self.executed: list[str] = []

    def create_dataframe(self, rows, schema=None):
        return _SpDF(self, rows, schema)

    def _arrow(self, table, cols=None):
        pa_schema = {"event_ts": pa.timestamp("us"), "size": pa.int64(), "page_count": pa.int64(),
                     "active": pa.bool_(), "assigned": pa.bool_()}
        names = cols or schema.columns(table)
        data = {c.upper(): [r.get(c) for r in self.tables[table]] for c in names}
        return pa.table({k: pa.array(v, type=pa_schema.get(k.lower(), pa.string()))
                         for k, v in data.items()})

    def close(self):
        self.closed = True

    def sql(self, query, params=None):
        if self.offline:
            raise RuntimeError("Missing Snowflake connection configuration")
        self.queries.append(query)
        q = query.strip()
        if q.startswith(("CREATE", "GRANT", "USE")):
            self.executed.append(q)
            if q.startswith("CREATE TABLE"):
                self.schema_missing = False
            return _Result([])
        if "information_schema.tables" in q:
            if self.schema_missing:
                return _Result([])
            return _Result([(t, "BASE TABLE") for t in self.tables] + [("audit_v", "VIEW")])
        if "information_schema.stages" in q:
            return _Result([] if self.schema_missing else [("doc_files",), ("sessions",)])
        if self.schema_missing and ("SYSTEM$" in q or "FROM" in q) and "CURRENT_USER()" not in q:
            raise RuntimeError("Object does not exist")
        if q.startswith("LIST"):
            prefix = q.split()[1].lstrip("@")
            return _Result([Row(name=k, size=len(v), md5="", last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
                            for k, v in self.stage.items() if k.startswith(prefix)])
        if q.startswith("REMOVE"):
            if self.fail_remove:
                raise RuntimeError("REMOVE failed")
            prefix = q.split()[1].strip("'").lstrip("@")
            for k in [k for k in self.stage if k.startswith(prefix)]:
                del self.stage[k]
            return _Result([])
        scope = (("ACCT", "ROLE", "DB", "SCHEMA")
                 if "CURRENT_ACCOUNT()" in q and "CURRENT_USER()" not in q else ())
        if "SYSTEM$LAST_CHANGE_COMMIT_TIME" in q:
            return _Result([scope + tuple(self.commits.get(t, 0) for t in re.findall(r"'(\w+)'", q))])
        if scope and "FROM" not in q:
            return _Result([scope])
        m = re.fullmatch(r"SELECT \* FROM (\w+)", q)
        if m:
            return _Result(arrow=self._arrow(m.group(1)))
        m = re.fullmatch(r"SELECT event_id FROM (\w+)", q)
        if m:
            return _Result(arrow=self._arrow(m.group(1), ["event_id"]))
        if "CURRENT_USER()" in q:
            return _Result([("OWNER", "ROLE", "WH", "DB", "SCHEMA", "ACCT")])
        if q.startswith("SELECT"):
            return _Result([], arrow=None)  # anything else: empty result
        raise NotImplementedError(q)


@pytest.fixture
def fake(monkeypatch):
    """Route the real get_session() to an in-memory fake: the fake stands in
    for the app's shared connection, so the per-browser-session connection
    logic in core/session.py is exercised as well."""
    from core import read, session

    s = FakeSession()
    monkeypatch.setattr(session, "_local_session", lambda: s)
    monkeypatch.setenv("MINIDMS_USER", "alice")
    monkeypatch.setattr(session, "_mode", "local")
    read.load_log.clear()
    read.load_event_ids.clear()
    return s
