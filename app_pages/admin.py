import polars as pl
import streamlit as st

from app_pages import _state, _ui
from core import ai, db, files, write
from core.schema import LOADED

st.title("Admin")

tab_ai, tab_counts, tab_orphans, tab_cost = st.tabs(
    ["AI processing", "Table sizes", "Orphaned files", "Cost"]
)

with tab_ai:
    st.caption("Uploads append to `ai_request`; the stream `ai_request_stream` fires the "
               "triggered task `process_ai_task` on its warehouse, which runs "
               "`process_ai()`: conversion to PDF pages, OCR per page with AI_COMPLETE "
               f"({ai.PAGE_MODEL}) and enrichment ({ai.MODEL}).")
    try:
        task = ai.task_state()
    except Exception as exc:
        task = None
        st.error(f"Could not read the task: {exc}")
    if task is None:
        st.warning("The AI task does not exist or is not visible to this role. "
                   "Run the *AI pipeline* step on the Setup page.")
    else:
        state = str(task.get("state", "?"))
        (st.success if state.lower() == "started" else st.warning)(
            f"Task `{ai.TASK}` is **{state}** (warehouse `{task.get('warehouse', '?')}`)."
            + ("" if state.lower() == "started" else
               " New files are not processed until it is resumed (Setup page).")
        )
    all_files = _state.files()
    counts = ai.state_counts(all_files)
    cols = st.columns(5)
    for col, key in zip(cols, ("queued", "done", "failed", "skipped", "missing")):
        col.metric(key.capitalize(), counts.get(key, 0))
    failed = all_files.filter(pl.col("ai_state") == "failed")
    if failed.height:
        st.markdown("**Failed files** (retry with *Process again* on the document)")
        st.dataframe(failed.select("filename", "document_id", "text_error", "enrich_error",
                                   "enriched_at"), hide_index=True)
    missing = ai.files_needing_ai(all_files)
    c1, c2, c3 = st.columns(3)
    if c1.button(f"Process {len(missing)} unprocessed file(s)", disabled=not missing,
                 help="Files attached without an AI request (e.g. an interrupted upload)."):
        _state.act(write.request_ai, _state.user(), missing, "missing",
                   success="AI processing requested.")
        st.rerun()
    if c2.button("Run the task now", help="EXECUTE TASK — normally not needed, the task "
                                          "fires by itself when files are uploaded."):
        try:
            ai.run_now()
            st.success("Task started. Results appear when it finishes.")
        except Exception as exc:
            st.error(f"Could not start the task: {exc}")
    if c3.button("Show recent task runs"):
        try:
            st.dataframe(ai.recent_runs(), hide_index=True)
        except Exception as exc:
            st.error(f"Could not read the task history: {exc}")

with tab_counts:
    st.caption("The tables the app loads and caches (shared by all viewers). Hybrid tables "
               "hold current state, one row per entity, so they grow with the number of "
               "documents, not with the number of edits.")
    rows = []
    for name in LOADED:
        df = _state.table(name)
        rows.append({"table": name, "rows": df.height,
                     "in_memory_mb": round(df.estimated_size("mb"), 2)})
    sizes = pl.DataFrame(rows)
    st.dataframe(sizes, hide_index=True)
    st.metric("Total in-memory (shared across viewers)", f"{sizes['in_memory_mb'].sum():.1f} MB")

with tab_orphans:
    st.caption("Files are staged first, then recorded in `stored_file`. An interrupted upload "
               "leaves orphaned bytes. Files referenced by `stored_file` are kept, and so is "
               "anything younger than the minimum age (someone may be mid-upload).")
    min_age = st.number_input("Minimum age (days)", 0, 3650, 2)

    def scan_orphans() -> tuple[pl.DataFrame, pl.DataFrame]:
        staged = files.staged_files()
        referenced = set(_state.table("stored_file")["stage_path"].drop_nulls().to_list())
        return staged, files.orphans(staged, referenced, int(min_age))

    c1, c2 = st.columns(2)
    if c1.button("Scan @doc_files"):
        with st.spinner("Scanning stage…"):
            staged, orphaned = scan_orphans()
        m1, m2 = st.columns(2)
        m1.metric("Staged files", staged.height, help=_ui.fmt_size(int(staged["size"].sum() or 0)))
        m2.metric("Orphaned", orphaned.height, help=_ui.fmt_size(int(orphaned["size"].sum() or 0)))
        st.dataframe(orphaned, hide_index=True)
    if c2.button("Remove orphaned files", help="Re-scans, then removes what the scan finds."):
        with st.spinner("Scanning and removing…"):
            _, orphaned = scan_orphans()
            n = files.remove_files(orphaned["relative_path"].to_list())
        st.success(f"Removed {n} orphaned file(s).")

with tab_cost:
    st.caption("Credits from `SNOWFLAKE.ACCOUNT_USAGE.METERING_HISTORY` (needs access to the "
               "SNOWFLAKE database; data lags up to a few hours).")
    days = st.number_input("Days", 1, 365, 30, key="cost:days")
    if st.button("Load credit usage"):
        try:
            df = db.query_frame(
                "SELECT service_type, name, SUM(credits_used) AS credits "
                "FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_HISTORY "
                "WHERE start_time >= DATEADD('day', ?, CURRENT_TIMESTAMP()) "
                "GROUP BY 1, 2 ORDER BY credits DESC",
                params=[-int(days)],
            )
            df = df.rename({c: c.lower() for c in df.columns})
            st.dataframe(df, hide_index=True)
            total = float(df["credits"].sum() or 0) if df.height else 0.0
            st.metric(f"Total credits, last {days} days", f"{total:.2f}",
                      help=f"≈ {total * 30 / days:.2f} credits per 30 days")
        except Exception as exc:
            st.error(f"Could not read metering history: {exc}")

