"""Setup steps for the Setup page: the statements of sql/minidms-setup.sql,
split and grouped, plus generated grants and helpers to check what exists.

Parsing and statement generation are pure; ``run()`` and ``existing_objects()``
talk to Snowflake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.schema import APPEND_TABLES, SYSTEM_TABLES, TOKEN_TABLES
from core.session import get_session

SETUP_SQL = Path(__file__).resolve().parents[1] / "sql" / "minidms-setup.sql"

_IDENT = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]+")$')
_CREATE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(STAGE|TABLE|VIEW|STREAM|PROCEDURE|TASK)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w$]*)",
    re.IGNORECASE,
)
_RESUME = re.compile(r"^\s*ALTER\s+TASK\s+([A-Za-z_][\w$]*)\s+RESUME\b", re.IGNORECASE)

STAGES = ("doc_files", "sessions")
VIEWS = ("audit_v", "ocr_status_v")
STREAMS = ("document_file_log_stream",)
PROCEDURES = ("extract_text",)
TASKS = ("extract_text_task",)


@dataclass
class Statement:
    sql: str
    kind: str | None = None      # stage | table | view | stream | procedure | task
                                 # | resume (ALTER TASK … RESUME) | None
    name: str | None = None      # object name, lower case


@dataclass
class Step:
    key: str
    title: str
    description: str
    statements: list[Statement] = field(default_factory=list)
    optional: bool = False


def split_sql(text: str) -> list[str]:
    """Split a script into statements on ';', ignoring semicolons inside
    quotes, $$-bodies and comments. Comment-only chunks are dropped."""
    out, buf, i, n = [], [], 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            buf.append(c)
            if c == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == "'":
                in_str = False
        elif text.startswith("$$", i):
            # Dollar-quoted body (procedures): copy verbatim up to the closing $$.
            j = text.find("$$", i + 2)
            j = n if j == -1 else j + 2
            buf.append(text[i:j])
            i = j
            continue
        elif c == "'":
            in_str = True
            buf.append(c)
        elif text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            buf.append(text[i:j])
            i = j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            buf.append(text[i:j])
            i = j
            continue
        elif c == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return [s for s in (_strip_comments_edges(x) for x in out) if s]


def _strip_comments_edges(stmt: str) -> str:
    """Drop leading comment/blank lines; keep comments inside the statement."""
    lines = stmt.strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].strip().startswith("--")):
        lines.pop(0)
    return "\n".join(lines).strip()


def classify(sql: str) -> Statement:
    m = _CREATE.match(sql)
    if m:
        return Statement(sql, m.group(1).lower(), m.group(2).lower())
    m = _RESUME.match(sql)
    if m:
        return Statement(sql, "resume", m.group(1).lower())
    return Statement(sql)


def setup_statements(text: str | None = None) -> list[Statement]:
    text = SETUP_SQL.read_text() if text is None else text
    return [classify(s) for s in split_sql(text)]


def valid_ident(name: str) -> bool:
    return bool(_IDENT.match(name or ""))


def quote(name: str) -> str:
    """Quote an identifier as returned by Snowflake (e.g. CURRENT_DATABASE())."""
    return '"' + name.replace('"', '""') + '"'


def grant_statements(role: str, database: str, schema: str,
                     warehouse: str | None = None) -> list[Statement]:
    """Privileges for the app's role: what it exercises and nothing more — no
    UPDATE or DELETE. ``database``/``schema`` come from the session context.

    Text extraction runs as the *owner* of the task and procedure, so the
    Cortex privilege belongs to the owning role, not here."""
    if not valid_ident(role):
        raise ValueError(f"not a valid role name: {role!r}")
    if warehouse and not valid_ident(warehouse):
        raise ValueError(f"not a valid warehouse name: {warehouse!r}")
    db, sch = quote(database), f"{quote(database)}.{quote(schema)}"
    stmts = [f"GRANT USAGE ON DATABASE {db} TO ROLE {role}",
             f"GRANT USAGE ON SCHEMA {sch} TO ROLE {role}"]
    if warehouse:
        stmts.append(f"GRANT USAGE ON WAREHOUSE {warehouse} TO ROLE {role}")
    stmts += [f"GRANT READ, WRITE ON STAGE {sch}.{s} TO ROLE {role}" for s in STAGES]
    stmts += [f"GRANT SELECT, INSERT ON TABLE {sch}.{t} TO ROLE {role}" for t in APPEND_TABLES]
    # Written only by the extraction task; the app just reads them.
    stmts += [f"GRANT SELECT ON TABLE {sch}.{t} TO ROLE {role}" for t in SYSTEM_TABLES]
    stmts += [f"GRANT SELECT ON VIEW {sch}.{v} TO ROLE {role}" for v in VIEWS]
    # Lets the Admin page show the task and start a run on demand.
    stmts += [f"GRANT MONITOR, OPERATE ON TASK {sch}.{t} TO ROLE {role}" for t in TASKS]
    return [Statement(s) for s in stmts]


def pypi_statements(role: str | None = None) -> list[Statement]:
    stmts = [
        "CREATE NETWORK RULE IF NOT EXISTS pypi_rule MODE = EGRESS TYPE = HOST_PORT "
        "VALUE_LIST = ('pypi.org', 'files.pythonhosted.org')",
        "CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS pypi_eai "
        "ALLOWED_NETWORK_RULES = (pypi_rule) ENABLED = TRUE",
    ]
    if role:
        if not valid_ident(role):
            raise ValueError(f"not a valid role name: {role!r}")
        stmts.append(f"GRANT USAGE ON INTEGRATION pypi_eai TO ROLE {role}")
    return [Statement(s) for s in stmts]


def create_streamlit_sql(name: str, source: str, warehouse: str, pool: str) -> str:
    """Shown for copying, not executed: where the source lives is site-specific."""
    return (
        f"CREATE STREAMLIT {name}\n"
        f"  FROM '{source}'\n"
        "  MAIN_FILE = 'streamlit_app.py'\n"
        f"  QUERY_WAREHOUSE = {warehouse}\n"
        "  RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'\n"
        f"  COMPUTE_POOL = {pool}\n"
        "  EXTERNAL_ACCESS_INTEGRATIONS = (pypi_eai)\n"
        "  TITLE = 'MiniDMS';"
    )


def steps(text: str | None = None) -> list[Step]:
    """The schema script grouped into steps."""
    stmts = setup_statements(text)
    by_kind = lambda *k: [s for s in stmts if s.kind in k]  # noqa: E731
    other = [s for s in stmts if s.kind is None]
    out = [
        Step("stages", "Stages",
             "`@doc_files` holds uploaded files at `{checksum}/{filename}` (with a "
             "directory table and server-side encryption, which AI_PARSE_DOCUMENT needs). "
             "`@sessions` holds per-user parquet snapshots of unsaved work.",
             by_kind("stage")),
        Step("tables", "Tables",
             "Seven append-only event logs written by the app, plus `document_text` and "
             "`ocr_log`, written by the text-extraction task. `IF NOT EXISTS` — re-running "
             "never drops data.",
             by_kind("table")),
        Step("views", "Views",
             "`audit_v` unions all logs; `ocr_status_v` is the latest extraction status per "
             "file. `OR REPLACE` is safe — views hold no data.",
             [s for s in by_kind("view")]),
        Step("pipeline", "Text extraction (stream + triggered task)",
             "A stream on `document_file_log` sees files as soon as they are saved to the "
             "database; the triggered task then calls `extract_text()`, which runs "
             "AI_PARSE_DOCUMENT in Snowflake and appends to `document_text` and `ocr_log`. "
             "The owning role needs CREATE STREAM/TASK/PROCEDURE, EXECUTE TASK, EXECUTE "
             "MANAGED TASK (serverless) and SNOWFLAKE.CORTEX_USER. The last statement "
             "resumes the task (tasks are created suspended).",
             by_kind("stream", "procedure", "task", "resume")),
    ]
    if other:
        out.append(Step("other", "Other statements", "Remaining statements from the script.",
                        other))
    return out


# ── Talking to Snowflake ─────────────────────────────────────────────────────


def context(session=None) -> dict[str, str | None]:
    session = session or get_session()
    r = session.sql(
        "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), "
        "CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_ACCOUNT()"
    ).collect()[0]
    keys = ("user", "role", "warehouse", "database", "schema", "account")
    return dict(zip(keys, r))


def _show_names(session, what: str) -> dict[str, dict]:
    """SHOW <what> IN SCHEMA (current schema) -> {lower name: row dict}."""
    try:
        rows = session.sql(f"SHOW {what} IN SCHEMA").collect()
    except Exception:
        return {}
    out = {}
    for r in rows:
        d = {k.lower().strip('"'): v for k, v in r.as_dict().items()}
        if d.get("name"):
            out[str(d["name"]).lower()] = d
    return out


def existing_objects(session=None) -> dict[str, set[str]]:
    """Stages, tables, views, streams, procedures and tasks in the current
    schema (names lower case). ``resume`` holds the tasks that are started."""
    session = session or get_session()
    tables = session.sql(
        "SELECT LOWER(table_name), table_type FROM information_schema.tables "
        "WHERE table_schema = CURRENT_SCHEMA()"
    ).collect()
    stages = session.sql(
        "SELECT LOWER(stage_name) FROM information_schema.stages "
        "WHERE stage_schema = CURRENT_SCHEMA()"
    ).collect()
    procedures = session.sql(
        "SELECT LOWER(procedure_name) FROM information_schema.procedures "
        "WHERE procedure_schema = CURRENT_SCHEMA()"
    ).collect()
    tasks = _show_names(session, "TASKS")
    return {
        "table": {r[0] for r in tables if r[1] == "BASE TABLE"},
        "view": {r[0] for r in tables if r[1] == "VIEW"},
        "stage": {r[0] for r in stages},
        "procedure": {r[0] for r in procedures},
        "stream": set(_show_names(session, "STREAMS")),
        "task": set(tasks),
        "resume": {n for n, d in tasks.items() if str(d.get("state", "")).lower() == "started"},
    }


NEEDED = {
    "stage": STAGES,
    "table": TOKEN_TABLES,
    "view": VIEWS,
    "stream": STREAMS,
    "procedure": PROCEDURES,
    "task": TASKS,
    "resume": TASKS,
}


def missing(existing: dict[str, set[str]]) -> dict[str, list[str]]:
    """What the app needs but the schema does not have. Pure."""
    return {k: [n for n in names if n not in existing.get(k, set())]
            for k, names in NEEDED.items()}


def total_needed() -> int:
    return sum(len(v) for v in NEEDED.values())


def use(kind: str, name: str, create: bool = False, session=None) -> None:
    """USE DATABASE / USE SCHEMA, optionally creating it first."""
    if kind not in ("DATABASE", "SCHEMA", "WAREHOUSE"):
        raise ValueError(kind)
    if not valid_ident(name):
        raise ValueError(f"not a valid name: {name!r}")
    session = session or get_session()
    if create and kind != "WAREHOUSE":
        session.sql(f"CREATE {kind} IF NOT EXISTS {name}").collect()
    session.sql(f"USE {kind} {name}").collect()


def run(sql: str, session=None) -> list:
    session = session or get_session()
    return session.sql(sql).collect()
