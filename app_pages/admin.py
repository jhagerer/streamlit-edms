import polars as pl
import streamlit as st

from app_pages import _state, _ui
from core import files, ocr, read, snapshot, write
from core.schema import KEYS, READ_TABLES
from core.session import slug

st.title("Admin")

tab_pending, tab_snap, tab_ocr, tab_counts, tab_orphans, tab_cost = st.tabs(
    ["My pending changes", "Session snapshots", "Text extraction", "Log sizes",
     "Orphaned files", "Cost"]
)

with tab_pending:
    counts = _state.pending_counts()
    if not counts:
        st.success("You have no pending changes.")
    for table, n in counts.items():
        with st.expander(f"`{table}` — {n} row(s)"):
            st.dataframe(_state.appends(table), hide_index=True)

with tab_snap:
    st.caption("Per-user folders on `@sessions` holding un-flushed work. A user's folder is "
               "removed when they save to the database or discard.")
    snaps = snapshot.list_snapshots()
    if not snaps.height:
        st.info("No session snapshots.")
    else:
        now = write.now_utc()
        folders = (
            snaps.group_by("folder")
            .agg(pl.len().alias("files"), pl.col("size").sum().alias("bytes"),
                 pl.col("last_modified").max().alias("last_modified"),
                 pl.col("table").sort().alias("tables"))
            .with_columns(((pl.lit(now) - pl.col("last_modified")).dt.total_hours() / 24)
                          .round(1).alias("age_days"))
            .sort("last_modified", descending=True)
        )
        st.dataframe(folders, hide_index=True)
        me = slug(_state.user())
        days = st.number_input("Remove other users' folders older than (days)", 1, 3650, 30)
        stale = folders.filter((pl.col("age_days") > days) & (pl.col("folder") != me))
        st.write(f"{stale.height} folder(s) would be removed. **Their unsaved work is lost.**")
        if stale.height and st.button("Remove stale snapshot folders", type="primary"):
            for folder in stale["folder"].to_list():
                snapshot.remove_folder(folder)
            st.success(f"Removed {stale.height} folder(s).")

with tab_ocr:
    st.caption("Saving files to the database feeds the stream `document_file_log_stream`; "
               "the triggered task `extract_text_task` then runs `extract_text()`, which "
               "calls AI_PARSE_DOCUMENT in Snowflake.")
    try:
        task = ocr.task_state()
    except Exception as exc:
        task = None
        st.error(f"Could not read the task: {exc}")
    if task is None:
        st.warning("The extraction task does not exist or is not visible to this role. "
                   "Run the *Text extraction* step on the Setup page.")
    else:
        state = str(task.get("state", "?"))
        (st.success if state.lower() == "started" else st.warning)(
            f"Task `{ocr.TASK}` is **{state}**."
            + ("" if state.lower() == "started" else
               " New files are not processed until it is resumed (Setup page).")
        )
    status = _state.ocr_status()
    counts = ocr.status_counts(status)
    waiting = _ui.file_status().filter(pl.col("text_status") == "waiting").height
    cols = st.columns(5)
    for col, key in zip(cols, ("queued", "done", "failed", "skipped")):
        col.metric(key.capitalize(), counts.get(key, 0))
    cols[4].metric("Waiting", waiting, help="Saved files without a status yet.")
    failed = status.filter(pl.col("status") == "failed")
    if failed.height:
        st.markdown("**Failed files** (retry from the document's *Text* tab)")
        st.dataframe(failed.select("event_ts", "file_id", "stage_path", "message"),
                     hide_index=True)
    c1, c2 = st.columns(2)
    if c1.button("Run extraction now", help="EXECUTE TASK — normally not needed, the task "
                                           "fires by itself when files are saved."):
        try:
            ocr.run_now()
            st.success("Task started. Results appear here when it finishes.")
        except Exception as exc:
            st.error(f"Could not start the task: {exc}")
    if c2.button("Show recent task runs"):
        try:
            st.dataframe(ocr.recent_runs(), hide_index=True)
        except Exception as exc:
            st.error(f"Could not read the task history: {exc}")

with tab_counts:
    st.caption("Raw events vs. current records. Memory grows with raw rows; once the logs "
               "reach a few hundred MB, switch the loaders to a server-side reduced view "
               "(architecture §9.10).")
    tokens = _state.tokens()
    rows = []
    for t in READ_TABLES:
        df = read.load_log(t, tokens[t])
        rows.append({"table": t, "raw_rows": df.height,
                     "current_records": read.latest(df, KEYS[t]).height,
                     "in_memory_mb": round(df.estimated_size("mb"), 2)})
    sizes = pl.DataFrame(rows)
    st.dataframe(sizes, hide_index=True)
    st.metric("Total in-memory (shared across viewers)", f"{sizes['in_memory_mb'].sum():.1f} MB")

with tab_orphans:
    st.caption("Files are staged at upload; their log rows wait for *Save to database*. "
               "Discarded uploads leave orphaned bytes. Files referenced by any log row or "
               "by any user's session snapshot are kept, and so is anything younger than the "
               "minimum age (someone may be mid-upload).")
    min_age = st.number_input("Minimum age (days)", 0, 3650, 2)

    def scan_orphans() -> tuple[pl.DataFrame, pl.DataFrame]:
        staged = files.staged_files()
        referenced = set(_state.view("document_file_log")["stage_path"].drop_nulls().to_list())
        for folder in snapshot.list_snapshots()["folder"].unique().to_list():
            snap = snapshot.read_snapshot(folder, "document_file_log")
            if snap is not None and "stage_path" in snap.columns:
                referenced |= set(snap["stage_path"].drop_nulls().to_list())
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
            df = read.query_frame(
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

