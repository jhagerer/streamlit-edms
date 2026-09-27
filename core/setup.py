"""Setup steps for the Setup page: the statements of sql/minidms-setup.sql,
split and grouped, plus generated grants and helpers to check what exists.

Parsing and statement generation are pure; ``run()`` and ``existing_objects()``
talk to Snowflake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.schema import APPEND_TABLES
from core.session import get_session

SETUP_SQL = Path(__file__).resolve().parents[1] / "sql" / "minidms-setup.sql"

_IDENT = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]+")$')
_CREATE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(STAGE|TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w$]*)",
    re.IGNORECASE,
)

STAGES = ("doc_files", "sessions")
VIEWS = ("audit_v",)


@dataclass
class Statement:
    sql: str
    kind: str | None = None      # stage | table | view | None
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
    quotes and comments. Comment-only chunks are dropped."""
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
    if not m:
        return Statement(sql)
    return Statement(sql, m.group(1).lower(), m.group(2).lower())


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
    UPDATE or DELETE. ``database``/``schema`` come from the session context."""
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
    stmts += [f"GRANT SELECT ON VIEW {sch}.{v} TO ROLE {role}" for v in VIEWS]
    stmts.append(f"GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE {role}")
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
    by_kind = lambda k: [s for s in stmts if s.kind == k]  # noqa: E731
    other = [s for s in stmts if s.kind is None]
    out = [
        Step("stages", "Stages",
             "`@doc_files` holds uploaded files (with a directory table and server-side "
             "encryption, which AI_PARSE_DOCUMENT needs). `@sessions` holds per-user "
             "parquet snapshots of unsaved work.", by_kind("stage")),
        Step("tables", "Tables",
             "Eight append-only event logs. `IF NOT EXISTS` — re-running never drops data.",
             by_kind("table")),
        Step("view", "Audit view",
             "`audit_v` unions all logs. `OR REPLACE` is safe — the view holds no data.",
             by_kind("view")),
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


def existing_objects(session=None) -> dict[str, set[str]]:
    """Stages, tables and views in the current schema (names lower case)."""
    session = session or get_session()
    tables = session.sql(
        "SELECT LOWER(table_name), table_type FROM information_schema.tables "
        "WHERE table_schema = CURRENT_SCHEMA()"
    ).collect()
    stages = session.sql(
        "SELECT LOWER(stage_name) FROM information_schema.stages "
        "WHERE stage_schema = CURRENT_SCHEMA()"
    ).collect()
    return {
        "table": {r[0] for r in tables if r[1] == "BASE TABLE"},
        "view": {r[0] for r in tables if r[1] == "VIEW"},
        "stage": {r[0] for r in stages},
    }


def missing(existing: dict[str, set[str]]) -> dict[str, list[str]]:
    """What the app needs but the schema does not have. Pure."""
    need = {"stage": STAGES, "table": APPEND_TABLES, "view": VIEWS}
    return {k: [n for n in names if n not in existing.get(k, set())] for k, names in need.items()}


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
