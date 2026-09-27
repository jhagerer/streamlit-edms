"""The Snowflake side of text extraction, as seen from the app: task state,
recent runs, and starting a run on demand. The work itself happens in
extract_text() (sql/minidms-setup.sql), fired by a triggered task."""

from __future__ import annotations

import polars as pl

from core.read import query_frame
from core.session import get_session

TASK = "extract_text_task"


def task_state(session=None) -> dict | None:
    """Row of SHOW TASKS for the extraction task (state, warehouse, …), or None."""
    session = session or get_session()
    rows = session.sql(f"SHOW TASKS LIKE '{TASK}' IN SCHEMA").collect()
    if not rows:
        return None
    return {k.lower().strip('"'): v for k, v in rows[0].as_dict().items()}


def recent_runs(limit: int = 20) -> pl.DataFrame:
    df = query_frame(
        "SELECT scheduled_time, completed_time, state, return_value, error_message "
        "FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => ?, RESULT_LIMIT => ?)) "
        "ORDER BY scheduled_time DESC",
        params=[TASK.upper(), int(limit)],
    )
    return df.rename({c: c.lower() for c in df.columns})


def run_now(session=None) -> None:
    """Start one run immediately (needs OPERATE on the task)."""
    session = session or get_session()
    session.sql(f"EXECUTE TASK {TASK}").collect()


def status_counts(ocr_status: pl.DataFrame) -> dict[str, int]:
    if not ocr_status.height:
        return {}
    return dict(ocr_status.group_by("status").len().iter_rows())
