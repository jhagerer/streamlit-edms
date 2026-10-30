"""The AI pipeline as seen from the app: task state, recent runs, starting a
run on demand. The work itself — PDF conversion, page OCR and enrichment with
AI_COMPLETE — runs inside Snowflake in process_ai() (sql/minidms-setup.sql),
fired by a stream on ai_request, so its output carries column-level lineage."""

from __future__ import annotations

import polars as pl

from core import db
from core.schema import AI_MODEL, OCR_MODEL

TASK = "process_ai_task"
MODEL = AI_MODEL
PAGE_MODEL = OCR_MODEL


def task_state(session=None) -> dict | None:
    """Row of SHOW TASKS for the AI task (state, warehouse, …), or None."""
    rows = db.query(f"SHOW TASKS LIKE '{TASK}' IN SCHEMA", session=session)
    if not rows:
        return None
    return {k.lower().strip('"'): v for k, v in rows[0].as_dict().items()}


def recent_runs(limit: int = 20) -> pl.DataFrame:
    df = db.query_frame(
        "SELECT scheduled_time, completed_time, state, return_value, error_message "
        "FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => ?, RESULT_LIMIT => ?)) "
        "ORDER BY scheduled_time DESC",
        params=[TASK.upper(), int(limit)],
    )
    return df.rename({c: c.lower() for c in df.columns})


def run_now(session=None) -> None:
    """Start one run immediately (needs OPERATE on the task)."""
    db.query(f"EXECUTE TASK {TASK}", session=session)


def state_counts(files: pl.DataFrame) -> dict[str, int]:
    if not files.height:
        return {}
    return dict(files.group_by("ai_state").len().iter_rows())


def files_needing_ai(files: pl.DataFrame) -> list[tuple[str, str]]:
    """(file_id, document_id) of attached files that were never processed."""
    return [(r["file_id"], r["document_id"])
            for r in files.filter(pl.col("ai_state") == "missing").iter_rows(named=True)]
