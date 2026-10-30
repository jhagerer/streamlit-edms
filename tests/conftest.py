"""Test fixtures: a stand-in for a Snowpark session backed by in-memory SQLite.

The tables are generated from core/schema.py with the same primary keys as the
hybrid tables in sql/minidms-setup.sql, so primary-key violations, version
checks and transaction rollbacks behave for real. Snowflake-only statements
(SHOW, information_schema, the token probe, Cortex Search, stage commands) are
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

from core import schema, setup  # noqa: E402

def _current(table: str, key: str, cols: str, created: bool = True) -> str:
    created_cols = (f", (SELECT event_ts FROM {table} f WHERE {_eq(key, 'f')} AND f.version = 1) "
                    f"AS created_at, (SELECT actor FROM {table} f WHERE {_eq(key, 'f')} "
                    f"AND f.version = 1) AS created_by, event_ts AS updated_at, "
                    f"actor AS updated_by") if created else ""
    return (f"SELECT {cols}, version{created_cols} FROM {table} l WHERE version = "
            f"(SELECT MAX(version) FROM {table} m WHERE {_eq(key, 'm')})")


def _eq(key: str, alias: str) -> str:
    return " AND ".join(f"{alias}.{k} = l.{k}" for k in key.split(","))


VIEWS_SQL = [
    "CREATE VIEW document_type_current AS " + _current(
        "document_type_log", "document_type_id", "document_type_id, label, active"),
    "CREATE VIEW metadata_type_current AS " + _current(
        "metadata_type_log", "metadata_type_id",
        "metadata_type_id, name, label, data_type, choices, default_value, active"),
    "CREATE VIEW tag_current AS " + _current("tag_log", "tag_id", "tag_id, label, color, active"),
    "CREATE VIEW document_current AS " + _current(
        "document_log", "document_id",
        "document_id, document_type_id, label, description, language, status"),
    "CREATE VIEW document_file_current AS " + _current(
        "document_file_log", "file_id", "file_id, document_id, active"),
    "CREATE VIEW metadata_current AS " + _current(
        "metadata_log", "document_id,metadata_type_id",
        "document_id, metadata_type_id, value, event_ts AS updated_at, actor AS updated_by",
        created=False),
    "CREATE VIEW tag_assignment_current AS " + _current(
        "tag_assignment_log", "document_id,tag_id",
        "document_id, tag_id, assigned, event_ts AS assigned_at, actor AS assigned_by",
        created=False),
    """CREATE VIEW document_text_current AS SELECT * FROM (
         SELECT *, ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY extracted_at DESC, event_id DESC) AS rn
         FROM document_text) WHERE rn = 1""",
    """CREATE VIEW document_enrichment_current AS SELECT * FROM (
         SELECT *, ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY created_at DESC, event_id DESC) AS rn
         FROM document_enrichment) WHERE rn = 1""",
    """CREATE VIEW ai_status_v AS
       WITH r AS (SELECT * FROM (SELECT file_id, event_id AS request_id, requested_at,
                  ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY requested_at DESC, event_id DESC) AS rn
                  FROM ai_request) WHERE rn = 1)
       SELECT r.file_id, r.requested_at, t.status AS text_status, t.error AS text_error,
              t.extracted_at, LENGTH(t.content) AS chars, t.pages, e.status AS enrich_status,
              e.error AS enrich_error, e.created_at AS enriched_at,
              NOT EXISTS (SELECT 1 FROM ai_job_log j WHERE j.request_id = r.request_id
                          AND j.step = 'finished') AS pending
       FROM r LEFT JOIN document_text_current t ON t.file_id = r.file_id
              LEFT JOIN document_enrichment_current e ON e.file_id = r.file_id""",
    """CREATE VIEW document_history_v AS
       SELECT event_ts, actor, 'document' AS entity, document_id, NULL AS item_id, op, version,
              label AS value, source FROM document_log
       UNION ALL SELECT event_ts, actor, 'file', document_id, file_id, op, version, NULL, source
       FROM document_file_log
       UNION ALL SELECT event_ts, actor, 'metadata', document_id, metadata_type_id, op, version,
              value, source FROM metadata_log
       UNION ALL SELECT event_ts, actor, 'tag', document_id, tag_id, op, version, NULL, source
       FROM tag_assignment_log""",
    """CREATE VIEW audit_v AS
       SELECT event_ts, actor, op, entity, document_id, COALESCE(item_id, document_id) AS object_id
       FROM document_history_v
       UNION ALL SELECT event_ts, actor, op, 'document_type', NULL, document_type_id FROM document_type_log
       UNION ALL SELECT event_ts, actor, op, 'metadata_type', NULL, metadata_type_id FROM metadata_type_log
       UNION ALL SELECT event_ts, actor, op, 'tag_definition', NULL, tag_id FROM tag_log
       UNION ALL SELECT uploaded_at, uploaded_by, 'upload', 'stored_file', document_id, file_id FROM stored_file
       UNION ALL SELECT created_at, 'system:ai', 'convert_' || status, 'pdf_conversion', NULL, file_id
       FROM pdf_conversion
       UNION ALL SELECT extracted_at, actor, 'ocr_' || status, 'document_text', NULL, file_id FROM document_text
       UNION ALL SELECT created_at, actor, 'enrich_' || status, 'document_enrichment', document_id, file_id
       FROM document_enrichment""",
]

_CURRENT = {"USER": "OWNER", "ROLE": "ROLE", "WAREHOUSE": "MINIDMS_WH", "DATABASE": "DB",
            "SCHEMA": "SCHEMA", "ACCOUNT": "ACCT"}


def _sql_type(dtype) -> str:
    return "INTEGER" if dtype in (schema.I, schema.B) else "TEXT"


def create_schema(conn: sqlite3.Connection) -> None:
    """Same constraints as sql/minidms-setup.sql: hybrid logs get
    PRIMARY KEY (key…, version) and UNIQUE (event_id)."""
    for table in schema.HYBRID_TABLES + schema.STANDARD_TABLES:
        cols = [f"{c} {_sql_type(t)}" for c, t in schema.COLUMNS[table].items()]
        if table in schema.LOG_KEYS:
            cols.append(f"PRIMARY KEY ({', '.join(schema.LOG_KEYS[table])}, version)")
            cols.append("UNIQUE (event_id)")
        conn.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
    for sql in VIEWS_SQL:
        conn.execute(sql)


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
        self.search_requests: list[dict] = []

    def close(self):
        self.closed = True

    def _search_preview(self, q: str) -> str:
        """Stand-in for Cortex Search: words of the query matched against
        index_text of the latest successful run per file in page_ocr, best
        (most words) first — one search document per page."""
        m = re.search(r"SEARCH_PREVIEW\('(.*?)', '(.*)'\)$", q, re.S)
        request = json.loads(m.group(2).replace("''", "'").replace("\\\\", "\\"))
        self.search_requests.append(request)
        rows = self.rows("SELECT * FROM page_ocr c WHERE status = 'done' AND run_id = (SELECT "
                         "run_id FROM page_ocr d WHERE d.file_id = c.file_id AND d.status = "
                         "'done' ORDER BY created_at DESC LIMIT 1) ORDER BY file_id, page_no")
        words = [w.lower() for w in request["query"].split()]
        scored = [(sum(w in (r["index_text"] or "").lower() for w in words), r) for r in rows]
        hits = [r for n, r in sorted(scored, key=lambda x: -x[0]) if n]
        return json.dumps({"results": [{c: r[c] for c in request["columns"]}
                                       for r in hits[:request["limit"]]]})

    # ── helpers for tests ──
    def rows(self, sql: str, params=()) -> list[dict]:
        cur = self.conn.execute(sql, [_param(p) for p in params])
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


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
        if up.startswith("SELECT CURRENT_ACCOUNT(), CURRENT_ROLE()") and "COUNT(*)" in up:
            if self.schema_missing:
                raise RuntimeError("Object 'DOCUMENT_LOG' does not exist")
            counts = [self.count(t) for t in re.findall(r"FROM (\w+)\)", q)]
            return _Result([("ACCT", "ROLE", "DB", "SCHEMA", *counts)])
        if "SNOWFLAKE.CORTEX.SEARCH_PREVIEW" in up:
            return _Result([(self._search_preview(q),)])
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
        if up.startswith("SHOW CORTEX SEARCH SERVICES"):
            return _Result([] if self.schema_missing else [Row(name="DOCUMENT_SEARCH")])
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
        if "INFORMATION_SCHEMA.FUNCTIONS" in up:
            return _Result([] if self.schema_missing else [(f,) for f in setup.FUNCTIONS])
        if "INFORMATION_SCHEMA.PROCEDURES" in up:
            return _Result([] if self.schema_missing else [(p,) for p in setup.PROCEDURES])
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
