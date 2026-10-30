"""Test fixtures: a stand-in for a Snowpark session backed by in-memory SQLite.

The tables are generated from core/schema.py with the same primary keys as the
hybrid tables in sql/minidms-setup.sql, so primary-key violations, version
checks and transaction rollbacks behave for real. Snowflake-only statements
(SHOW, information_schema, the token probe, SEARCH, stage commands) are
answered by small patterns. No Snowflake needed.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pytest
from snowflake.snowpark import Row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import schema  # noqa: E402

PRIMARY_KEYS = {
    "document_type": ["document_type_id"],
    "metadata_type": ["metadata_type_id"],
    "tag": ["tag_id"],
    "document": ["document_id"],
    "document_file": ["file_id"],
    "document_metadata": ["document_id", "metadata_type_id"],
    "document_tag": ["document_id", "tag_id"],
    "change_log": ["event_id"],
    "sync_state": ["table_name"],
}

VIEWS_SQL = [
    """CREATE VIEW document_text_current AS SELECT * FROM (
         SELECT *, ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY extracted_at DESC, event_id DESC) AS rn
         FROM document_text) WHERE rn = 1""",
    """CREATE VIEW document_enrichment_current AS SELECT * FROM (
         SELECT *, ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY created_at DESC, event_id DESC) AS rn
         FROM document_enrichment) WHERE rn = 1""",
    """CREATE VIEW ai_status_v AS
       WITH r AS (SELECT file_id, MAX(requested_at) AS requested_at FROM ai_request GROUP BY file_id)
       SELECT r.file_id, r.requested_at, t.status AS text_status, t.error AS text_error,
              t.extracted_at, LENGTH(t.content) AS chars, e.status AS enrich_status,
              e.error AS enrich_error, e.created_at AS enriched_at,
              (e.created_at IS NULL OR e.created_at < r.requested_at) AS pending
       FROM r LEFT JOIN document_text_current t ON t.file_id = r.file_id
              LEFT JOIN document_enrichment_current e ON e.file_id = r.file_id""",
    """CREATE VIEW audit_v AS
       SELECT event_ts, actor, op, entity, document_id, entity_id AS object_id FROM change_log
       UNION ALL SELECT uploaded_at, uploaded_by, 'upload', 'stored_file', document_id, file_id FROM stored_file
       UNION ALL SELECT extracted_at, actor, 'ocr_' || status, 'document_text', NULL, file_id FROM document_text
       UNION ALL SELECT created_at, actor, 'enrich_' || status, 'document_enrichment', document_id, file_id
       FROM document_enrichment""",
]

_CURRENT = {"USER": "OWNER", "ROLE": "ROLE", "WAREHOUSE": "MINIDMS_WH", "DATABASE": "DB",
            "SCHEMA": "SCHEMA", "ACCOUNT": "ACCT"}


def _sql_type(dtype) -> str:
    return "INTEGER" if dtype in (schema.I, schema.B) else "TEXT"


def create_schema(conn: sqlite3.Connection) -> None:
    for table in schema.HYBRID_TABLES + schema.STANDARD_TABLES:
        cols = [f"{c} {_sql_type(t)}" for c, t in schema.COLUMNS[table].items()]
        if table in PRIMARY_KEYS:
            cols.append(f"PRIMARY KEY ({', '.join(PRIMARY_KEYS[table])})")
        conn.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
    for sql in VIEWS_SQL:
        conn.execute(sql)
    conn.executemany("INSERT INTO sync_state (table_name, seq) VALUES (?, 0)",
                     [(t,) for t in schema.TOKEN_TABLES])


def _param(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S.%f")  # one fixed format, compares as text
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        return int(v)
    return v


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


class FakeSession:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        create_schema(self.conn)
        self.stage: dict[str, bytes] = {}
        self.file = _Files(self)
        self.queries: list[str] = []
        self.executed: list[str] = []
        self.schema_missing = False   # simulate a fresh schema without MiniDMS objects
        self.offline = False          # simulate "no connection configured"
        self.closed = False
        self.task_state = "started"
        self.warehouse_type = "ADAPTIVE"

    def close(self):
        self.closed = True

    # ── helpers for tests ──
    def rows(self, sql: str, params=()) -> list[dict]:
        cur = self.conn.execute(sql, [_param(p) for p in params])
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def seq(self, table: str) -> int:
        return self.conn.execute("SELECT seq FROM sync_state WHERE table_name = ?",
                                 [table]).fetchone()[0]

    # ── Snowpark surface ──
    def sql(self, query, params=None):
        if self.offline:
            raise RuntimeError("Missing Snowflake connection configuration")
        self.queries.append(query)
        q = query.strip()
        up = q.upper()
        params = [_param(p) for p in (params or [])]

        if up in ("BEGIN TRANSACTION", "BEGIN"):
            self.conn.execute("BEGIN")
            return _Result([])
        if up in ("COMMIT", "ROLLBACK"):
            self.conn.execute(up)
            return _Result([])
        if up.startswith("SELECT CURRENT_ACCOUNT(), CURRENT_ROLE()") and "OBJECT_AGG" in up:
            if self.schema_missing:
                raise RuntimeError("Object 'SYNC_STATE' does not exist")
            seqs = dict(self.conn.execute("SELECT table_name, seq FROM sync_state").fetchall())
            return _Result([("ACCT", "ROLE", "DB", "SCHEMA", json.dumps(seqs))])
        if up.startswith("SELECT CURRENT_") and "FROM" not in up:
            return _Result([tuple(_CURRENT[n] for n in re.findall(r"CURRENT_(\w+)\(\)", up))])
        if up.startswith("SHOW WAREHOUSES"):
            return _Result([Row(name="MINIDMS_WH", type=self.warehouse_type)])
        if up.startswith("SHOW TASKS"):
            return _Result([] if self.schema_missing else
                           [Row(name="PROCESS_AI_TASK", state=self.task_state,
                                warehouse="MINIDMS_WH")])
        if up.startswith("SHOW STREAMS"):
            return _Result([] if self.schema_missing else [Row(name="AI_REQUEST_STREAM")])
        if up.startswith("SHOW HYBRID TABLES"):
            return _Result([] if self.schema_missing else
                           [Row(name=t.upper()) for t in schema.HYBRID_TABLES])
        if "INFORMATION_SCHEMA.TABLES" in up:
            if self.schema_missing:
                return _Result([])
            return _Result([(t, "BASE TABLE") for t in schema.HYBRID_TABLES + schema.STANDARD_TABLES]
                           + [(v, "VIEW") for v in schema.VIEWS])
        if "INFORMATION_SCHEMA.STAGES" in up:
            return _Result([] if self.schema_missing else [("doc_files",)])
        if "INFORMATION_SCHEMA.PROCEDURES" in up:
            return _Result([] if self.schema_missing else [("process_ai",)])
        if "TASK_HISTORY" in up:
            return _Result([], arrow=pa.table({"STATE": pa.array([], pa.string())}))
        if up.startswith(("CREATE", "GRANT", "USE ", "ALTER", "EXECUTE")) or "FROM VALUES" in up:
            self.executed.append(q)
            if up.startswith(("CREATE HYBRID TABLE", "CREATE TABLE")):
                self.schema_missing = False
            return _Result([])
        if up.startswith("REMOVE"):
            prefix = q.split()[1].strip("'").lstrip("@")
            for k in [k for k in self.stage if k.startswith(prefix)]:
                del self.stage[k]
            return _Result([])
        if self.schema_missing:
            raise RuntimeError("Object does not exist")

        # Translate the few Snowflake-only bits the app uses, then run on SQLite.
        q = re.sub(r"SEARCH\(([\w.]+), \?, SEARCH_MODE => '\w+'\)", r"\1 LIKE '%' || ? || '%'", q)
        q = q.replace(" ILIKE ", " LIKE ").replace("ESCAPE '\\\\'", "ESCAPE '\\'")
        try:
            cur = self.conn.execute(q, params)
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(f"Duplicate key value violates unique constraint: {exc}") from exc
        if cur.description is None:
            return _Result([(cur.rowcount,)])
        names = [d[0].upper() for d in cur.description]
        data = cur.fetchall()
        rows = [Row(**dict(zip(names, r))) for r in data] if len(set(names)) == len(names) \
            else [tuple(r) for r in data]
        arrow = pa.Table.from_pylist([dict(zip(names, r)) for r in data]) if data else \
            pa.table({n: pa.array([], pa.null()) for n in dict.fromkeys(names)})
        return _Result(rows, arrow)


@pytest.fixture
def fake(monkeypatch):
    """Route the real get_session() to the fake: it stands in for the app's
    shared connection, so the per-browser-session connection logic in
    core/session.py is exercised as well."""
    from core import files, read, session

    s = FakeSession()
    monkeypatch.setattr(session, "_local_session", lambda: s)
    monkeypatch.setenv("MINIDMS_USER", "alice")
    monkeypatch.setattr(session, "_mode", "local")
    for fn in (read.load, read.load_texts, read.load_history, files.file_bytes):
        fn.clear()
    return s


SHA = "ab" * 32


def stored(file_id="f1", document_id="d1", name="a.pdf"):
    """A stored-file dict as files.stage_upload() returns it."""
    return {"file_id": file_id, "document_id": document_id, "filename": name,
            "stage_path": f"@doc_files/{SHA}/{name}", "mimetype": "application/pdf",
            "size": 3, "checksum": SHA}
