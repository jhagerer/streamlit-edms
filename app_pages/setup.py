"""Run the MiniDMS setup step by step from inside the app.

Works before the schema exists. Every statement is shown before it runs, and
all schema statements are idempotent (IF NOT EXISTS / OR REPLACE on the view).
"""

import streamlit as st

from app_pages import _connection, _state
from core import setup, write
from core.session import auth_configured, connection_source, in_sis, setup_allowed

st.title("Setup")
st.caption("Create the MiniDMS objects in Snowflake step by step. Each statement is shown "
           "before it runs. Re-running is safe: nothing is dropped.")

# ── Step 1: connection ───────────────────────────────────────────────────────

ctx = _connection.connection_step()
if ctx is None:
    st.stop()

allowed, why = setup_allowed()
if not allowed:
    st.warning(f"You can view the setup but not run it: {why}. You can still connect "
               "with your own credentials above.", icon="🔒")


def run_statement(sql: str) -> bool:
    try:
        setup.run(sql)
        _state.setup_log_add(sql, True, "OK")
        return True
    except Exception as exc:
        _state.setup_log_add(sql, False, str(exc))
        return False


def run_all(statements, label: str) -> None:
    ok = all(run_statement(s.sql) for s in statements)
    _state.flash(f"{label}: {'done' if ok else 'finished with errors — see the log below'}.",
                 "success" if ok else "error")
    st.rerun()


# Switching db/schema changes the session. Only offer it where that session
# belongs to this viewer alone, or on a plain single-user local run.
can_switch = allowed and not in_sis() and (
    connection_source() == "own" or not auth_configured()
)
if can_switch:
    with st.expander("Use another database / schema / warehouse", expanded=not ctx["schema"]):
        if connection_source() == "own":
            st.caption("Applies to your own connection for this browser session.")
        else:
            st.caption("Applies to the shared connection of this app process until it "
                       "restarts. To make it permanent, set database/schema/warehouse in "
                       "the connection settings.")
        c1, c2, c3 = st.columns(3)
        db = c1.text_input("Database", value=ctx["database"] or "")
        sch = c2.text_input("Schema", value=ctx["schema"] or "MINIDMS")
        wh = c3.text_input("Warehouse", value=ctx["warehouse"] or "")
        create = st.checkbox("Create database/schema if missing", value=False)
        if st.button("Apply"):
            try:
                if wh and wh != ctx["warehouse"]:
                    setup.use("WAREHOUSE", wh)
                if db:
                    setup.use("DATABASE", db, create=create)
                if sch:
                    setup.use("SCHEMA", sch, create=create)
                _state.flash("Connection context changed.")
            except Exception as exc:
                _state.flash(f"Could not switch: {exc}", "error")
            st.rerun()

if not ctx["database"] or not ctx["schema"]:
    st.error("No current database/schema. Choose one above (local) or set it in the "
             "connection settings.")
    st.stop()
if not ctx["warehouse"]:
    st.warning("No current warehouse — statements that need compute will fail.")

# ── Status ───────────────────────────────────────────────────────────────────

try:
    existing = setup.existing_objects()
except Exception as exc:
    st.error(f"Could not inspect the schema: {exc}")
    st.stop()
missing = setup.missing(existing)
n_missing = sum(len(v) for v in missing.values())
total = len(setup.STAGES) + len(setup.APPEND_TABLES) + len(setup.VIEWS)
st.progress((total - n_missing) / total,
            text=f"{total - n_missing} of {total} MiniDMS objects exist in "
                 f"{ctx['database']}.{ctx['schema']}")
if not n_missing:
    st.success("The schema is complete.", icon="✅")

# ── Steps 2–4: the schema script ─────────────────────────────────────────────

for i, step in enumerate(setup.steps(), start=2):
    todo = [s for s in step.statements if s.name and s.name not in existing.get(s.kind, set())]
    icon = "✅" if not todo else "⬜"
    st.header(f"{i} · {step.title} {icon}")
    st.caption(step.description)
    for j, stmt in enumerate(step.statements):
        exists = stmt.name in existing.get(stmt.kind, set()) if stmt.name else None
        label = f"{stmt.kind or 'statement'} `{stmt.name or j}`"
        with st.expander(f"{'✅' if exists else '⬜'} {label}", expanded=False):
            st.code(stmt.sql, language="sql")
            if st.button("Run", key=f"setup:{step.key}:{j}", disabled=not allowed):
                run_statement(stmt.sql)
                st.rerun()
    if st.button(f"Run all {len(step.statements)} statement(s) of this step",
                 key=f"setup:{step.key}:all", type="primary" if todo else "secondary",
                 disabled=not allowed):
        run_all(step.statements, step.title)

# ── Step 5: grants (optional) ────────────────────────────────────────────────

st.header("5 · Grants for the app role (optional)")
st.caption("Only needed when the app runs with a role that does not own the objects. "
           "Grants SELECT and INSERT but no UPDATE or DELETE, so the database enforces "
           "append-only. Needs a role that may grant these privileges.")
c1, c2 = st.columns(2)
role = c1.text_input("App role", placeholder="MINIDMS_APP")
wh = c2.text_input("Warehouse to grant", value=ctx["warehouse"] or "")
if role:
    try:
        grants = setup.grant_statements(role, ctx["database"], ctx["schema"], wh or None)
        st.code(";\n".join(g.sql for g in grants) + ";", language="sql")
        if st.button("Run grants", disabled=not allowed):
            run_all(grants, "Grants")
    except ValueError as exc:
        st.error(str(exc))

# ── Step 6: PyPI access (SiS container runtime) ──────────────────────────────

st.header("6 · PyPI access for Streamlit in Snowflake (optional)")
st.caption("Only for the SiS container runtime, which installs requirements.txt from PyPI. "
           "Usually needs ACCOUNTADMIN. The network rule is created in the current schema.")
pypi_role = st.text_input("Grant usage on the integration to role (optional)", key="setup:pypi:role")
try:
    pypi = setup.pypi_statements(pypi_role or None)
    st.code(";\n".join(p.sql for p in pypi) + ";", language="sql")
    if st.button("Create PyPI integration", disabled=not allowed):
        run_all(pypi, "PyPI integration")
except ValueError as exc:
    st.error(str(exc))

# ── Step 7: create the Streamlit object (copy only) ──────────────────────────

st.header("7 · Create the Streamlit app in Snowflake (optional)")
st.caption("Shown for copying — where the source files live depends on your setup "
           "(Workspace, Git repository or stage). Verify the options against the "
           "Snowflake documentation.")
c1, c2, c3 = st.columns(3)
src = c1.text_input("Source location", value="@<stage_or_repo>/<path>")
pool = c2.text_input("Compute pool", value="<compute_pool>")
qwh = c3.text_input("Query warehouse", value=ctx["warehouse"] or "<warehouse>")
st.code(setup.create_streamlit_sql("minidms", src, qwh, pool), language="sql")

# ── Step 8: starter data (optional) ──────────────────────────────────────────

st.header("8 · Starter definitions (optional)")
st.caption("Adds a few document types, metadata types and tags as normal pending "
           "changes. Click **Save to database** in the sidebar to keep them.")
if n_missing:
    st.info("Available once the schema is complete.")
elif st.button("Add starter definitions", disabled=not allowed):
    user = _state.user()
    _state.append_many({
        "document_type_log": [write.document_type_row(user, write.new_id(), label)
                              for label in ("Invoice", "Contract", "Letter", "Report")],
        "metadata_type_log": [
            write.metadata_type_row(user, write.new_id(), "date", "Date", "date", None, None),
            write.metadata_type_row(user, write.new_id(), "amount", "Amount", "number", None, None),
            write.metadata_type_row(user, write.new_id(), "counterparty", "Counterparty",
                                    "text", None, None),
            write.metadata_type_row(user, write.new_id(), "status", "Status", "choice",
                                    "open\nin progress\ndone", "open"),
        ],
        "tag_log": [write.tag_row(user, write.new_id(), label, color) for label, color in
                    (("Important", "#d62728"), ("To review", "#ff7f0e"), ("Archived", "#7f7f7f"))],
    })
    _state.flash("Starter definitions added — pending until you save to the database.")
    st.rerun()

if not n_missing:
    st.page_link("app_pages/documents.py", label="Go to the documents", icon="📄")

# ── Log ──────────────────────────────────────────────────────────────────────

log = _state.setup_log()
if log:
    st.header("Log")
    for ok, sql, msg in log[:50]:
        with st.expander(f"{'✅' if ok else '❌'} {sql.splitlines()[0][:90]}"):
            st.code(sql, language="sql")
            (st.success if ok else st.error)(msg)
