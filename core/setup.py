"""Setup steps for the Setup page: the statements of sql/minidms-setup.sql,
split and grouped, plus generated grants and helpers to check what exists.

Parsing and statement generation are pure; ``run()`` and ``existing_objects()``
talk to Snowflake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from core import db
from core.schema import HYBRID_TABLES, STANDARD_TABLES
from core.schema import VIEWS as SCHEMA_VIEWS

SETUP_SQL = Path(__file__).resolve().parents[1] / "sql" / "minidms-setup.sql"

_IDENT = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]+")$')
_CREATE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(ADAPTIVE\s+WAREHOUSE|HYBRID\s+TABLE|CORTEX\s+SEARCH\s+SERVICE|"
    r"STAGE|TABLE|VIEW|STREAM|FUNCTION|PROCEDURE|TASK)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w$]*)",
    re.IGNORECASE,
)
_KIND = {"adaptive warehouse": "warehouse", "hybrid table": "hybrid",
         "cortex search service": "search"}
_RESUME = re.compile(r"^\s*ALTER\s+TASK\s+([A-Za-z_][\w$]*)\s+RESUME\b", re.IGNORECASE)

WAREHOUSES = ("minidms_wh",)
STAGES = ("doc_files",)
VIEWS = SCHEMA_VIEWS
STREAMS = ("ai_request_stream",)
FUNCTIONS = ("pdf_converter", "pdf_path", "pdf_page_path", "pdf_page_count")
PROCEDURES = ("convert_to_pdf_pages", "ocr_pages", "process_ai")
TASKS = ("process_ai_task",)
SEARCH_SERVICES = ("document_search",)


@dataclass
class Statement:
    sql: str
    kind: str | None = None      # warehouse | stage | hybrid | table | view | stream
                                 # | function | procedure | task | resume (ALTER TASK … RESUME)
                                 # | search (Cortex Search service) | None
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
        kind = re.sub(r"\s+", " ", m.group(1).lower())
        return Statement(sql, _KIND.get(kind, kind), m.group(2).lower())
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


# Standard tables the app itself appends to; the rest are written by process_ai().
APP_APPENDED = ("stored_file", "ai_request")


def grant_statements(role: str, database: str, schema: str,
                     warehouse: str | None = "MINIDMS_WH") -> list[Statement]:
    """Privileges for the app's role: what it exercises and nothing more.
    Every table is append-only, and the grants enforce it: SELECT and INSERT,
    never UPDATE or DELETE. ``database``/``schema`` come from the session.

    AI processing runs as the *owner* of the task and procedure, so the
    Cortex privilege belongs to the owning role, not here."""
    if not valid_ident(role):
        raise ValueError(f"not a valid role name: {role!r}")
    if warehouse and not valid_ident(warehouse):
        raise ValueError(f"not a valid warehouse name: {warehouse!r}")
    dbq, sch = quote(database), f"{quote(database)}.{quote(schema)}"
    stmts = [f"GRANT USAGE ON DATABASE {dbq} TO ROLE {role}",
             f"GRANT USAGE ON SCHEMA {sch} TO ROLE {role}"]
    if warehouse:
        stmts.append(f"GRANT USAGE ON WAREHOUSE {warehouse} TO ROLE {role}")
    stmts += [f"GRANT READ, WRITE ON STAGE {sch}.{s} TO ROLE {role}" for s in STAGES]
    stmts += [f"GRANT SELECT, INSERT ON TABLE {sch}.{t} TO ROLE {role}"
              for t in HYBRID_TABLES + APP_APPENDED]
    stmts += [f"GRANT SELECT ON TABLE {sch}.{t} TO ROLE {role}"   # written only by process_ai()
              for t in STANDARD_TABLES if t not in APP_APPENDED]
    stmts += [f"GRANT SELECT ON VIEW {sch}.{v} TO ROLE {role}" for v in VIEWS]
    stmts += [f"GRANT USAGE ON CORTEX SEARCH SERVICE {sch}.{s} TO ROLE {role}"
              for s in SEARCH_SERVICES]
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
        Step("warehouse", "Warehouse (only if missing)",
             "For an empty account: creates the adaptive warehouse `minidms_wh` if it does not "
             "exist yet (an existing one is left alone). The AI task runs on it; the app itself "
             "can run on any warehouse. Needs CREATE WAREHOUSE.",
             by_kind("warehouse")),
        Step("stage", "Stage",
             "`@doc_files` holds uploaded files at `{checksum}/{filename}` and the derived PDFs "
             "at `{checksum}/derived/` (with a directory table and server-side encryption, "
             "which AI_COMPLETE needs to read them).",
             by_kind("stage")),
        Step("hybrid", "Hybrid tables (what people edit, append-only)",
             "Documents, metadata values, tag assignments, file attachments and the "
             "definitions. One row per version — nothing is updated or deleted. "
             "PRIMARY KEY (id, version) makes concurrent edits collide instead of overwriting "
             "each other; UNIQUE (event_id) makes every action idempotent.",
             by_kind("hybrid")),
        Step("standard", "Standard tables (files, pages, AI output)",
             "Uploaded files, AI requests, the PDF conversion and pages, the structured OCR per "
             "page, the text and the enrichments: append-only; the AI output is written by "
             "INSERT … SELECT inside Snowflake, so it carries column-level lineage.",
             by_kind("table")),
        Step("views", "Views",
             "The current state (latest version per key), the latest OCR text and enrichment "
             "per file, the AI state per file, the per-document history and the audit trail. "
             "`OR REPLACE` is safe — views hold no data.",
             by_kind("view")),
        Step("pipeline", "AI pipeline (stream + triggered task)",
             "A stream on `ai_request` fires the triggered task on `minidms_wh` right after a "
             "file is uploaded; it calls `process_ai()`: every file is converted to PDF and "
             "split into pages (Python procedure), every page gets a structured OCR with "
             "AI_COMPLETE (claude-haiku-4-5) into `page_ocr`, then AI_COMPLETE "
             "(claude-sonnet-4-5) writes the summary, type and tag suggestions and metadata. "
             "The owning role needs CREATE STREAM/TASK/FUNCTION/PROCEDURE, EXECUTE TASK, USAGE on the "
             "warehouse, SNOWFLAKE.CORTEX_USER and the Anaconda terms accepted. The last "
             "statement resumes the task. Every row is written by INSERT … SELECT from its "
             "sources, so Snowflake records column-level lineage.",
             by_kind("stream", "function", "procedure", "task", "resume")),
        Step("search", "Cortex Search (the sink of the pipeline)",
             "The service `document_search` indexes one search document per page from "
             "`page_ocr` (summary, headings, image and table summaries — not the raw OCR text) "
             "and refreshes itself (target lag 1 minute). The Search page queries it. Needs CREATE CORTEX SEARCH SERVICE and CORTEX_USER.",
             by_kind("search")),
    ]
    if other:
        out.append(Step("other", "Other statements", "Remaining statements from the script.",
                        other))
    return out


# ── Talking to Snowflake ─────────────────────────────────────────────────────


def context(session=None) -> dict[str, str | None]:
    r = db.query(
        "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), "
        "CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_ACCOUNT()", session=session
    )[0]
    keys = ("user", "role", "warehouse", "database", "schema", "account")
    return dict(zip(keys, r))


def _show(session, what: str) -> dict[str, dict] | None:
    """SHOW <what> -> {lower name: row dict}; None if the command failed."""
    try:
        rows = db.query(f"SHOW {what}", session=session)
    except Exception:
        return None
    out = {}
    for r in rows:
        d = {k.lower().strip('"'): v for k, v in r.as_dict().items()}
        if d.get("name"):
            out[str(d["name"]).lower()] = d
    return out


def existing_objects(session=None) -> dict[str, set[str]]:
    """What exists in the current schema (names lower case), keyed like the
    statement kinds. ``resume``
    the started tasks."""
    tables = db.query(
        "SELECT LOWER(table_name), table_type FROM information_schema.tables "
        "WHERE table_schema = CURRENT_SCHEMA()", session=session)
    stages = db.query(
        "SELECT LOWER(stage_name) FROM information_schema.stages "
        "WHERE stage_schema = CURRENT_SCHEMA()", session=session)
    procedures = db.query(
        "SELECT LOWER(procedure_name) FROM information_schema.procedures "
        "WHERE procedure_schema = CURRENT_SCHEMA()", session=session)
    functions = db.query(
        "SELECT LOWER(function_name) FROM information_schema.functions "
        "WHERE function_schema = CURRENT_SCHEMA()", session=session)
    table_names = {r[0] for r in tables if r[1] in ("BASE TABLE", "HYBRID TABLE")}
    hybrid = _show(session, "HYBRID TABLES IN SCHEMA")
    tasks = _show(session, "TASKS IN SCHEMA") or {}
    warehouses = _show(session, "WAREHOUSES LIKE 'MINIDMS_WH'") or {}
    return {
        "warehouse": set(warehouses),
        "hybrid": set(hybrid) if hybrid is not None else table_names & set(HYBRID_TABLES),
        "table": table_names,
        "view": {r[0] for r in tables if r[1] == "VIEW"},
        "stage": {r[0] for r in stages},
        "function": {r[0] for r in functions},
        "procedure": {r[0] for r in procedures},
        "stream": set(_show(session, "STREAMS IN SCHEMA") or {}),
        "task": set(tasks),
        "resume": {n for n, d in tasks.items() if str(d.get("state", "")).lower() == "started"},
        "search": set(_show(session, "CORTEX SEARCH SERVICES IN SCHEMA") or {}),
    }


NEEDED = {
    "warehouse": WAREHOUSES,
    "stage": STAGES,
    "hybrid": HYBRID_TABLES,
    "table": STANDARD_TABLES,
    "view": VIEWS,
    "stream": STREAMS,
    "function": FUNCTIONS,
    "procedure": PROCEDURES,
    "task": TASKS,
    "resume": TASKS,
    "search": SEARCH_SERVICES,
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
    if create and kind != "WAREHOUSE":
        db.query(f"CREATE {kind} IF NOT EXISTS {name}", session=session)
    db.query(f"USE {kind} {name}", session=session)


def run(sql: str, session=None) -> list:
    return db.query(sql, session=session)
