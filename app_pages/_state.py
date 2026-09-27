"""Tier 1: this browser session's appends. The only module touching
``st.session_state``.

Session state holds appends only — never committed rows. What a page renders
is ``view(table)``: the shared cached committed frame concatenated with these
appends, recomputed on every rerun and never stored.
"""

from __future__ import annotations

import polars as pl
import streamlit as st

from core import read, schema, snapshot, write
from core.schema import APPEND_TABLES
from core.session import actor

_APPENDS = "appends"
_TOKENS = "_tokens"
_RESTORED = "_restored"
_REPORT = "_restore_report"
_DIRTY = "_dirty"
_FLASH = "_flash"
_SCOPE = "_scope"


# ── Per-rerun setup ──────────────────────────────────────────────────────────


def begin_run() -> None:
    """Called at the top of every rerun by streamlit_app.py: one token probe,
    then restore-on-entry the first time."""
    st.session_state[_TOKENS] = read.change_tokens()
    scope = current_scope()
    if st.session_state.get(_SCOPE) not in (None, scope):
        # The connection now points at another account/role/db/schema. Pending
        # rows belong to the old one: never flush them here. Start over and
        # restore this connection's own snapshot instead.
        _store().clear()
        st.session_state[_RESTORED] = False
    st.session_state[_SCOPE] = scope
    if not st.session_state.get(_RESTORED):
        _restore_on_entry()


def current_scope() -> str:
    """account/role/database/schema of the current connection (from the tokens)."""
    return next(iter(tokens().values())).split("|", 1)[0]


def tokens() -> dict[str, str]:
    if _TOKENS not in st.session_state:
        st.session_state[_TOKENS] = read.change_tokens()
    return st.session_state[_TOKENS]


def user() -> str:
    return actor()


def _restore_on_entry() -> None:
    frames = read.restore(user(), tokens())
    _store().clear()
    _store().update(frames)
    st.session_state[_RESTORED] = True
    counts = {t: df.height for t, df in frames.items() if df.height}
    if counts:
        st.session_state[_REPORT] = counts


# ── The appends store ────────────────────────────────────────────────────────


def _store() -> dict[str, pl.DataFrame]:
    if _APPENDS not in st.session_state:
        st.session_state[_APPENDS] = {}
    return st.session_state[_APPENDS]


def appends(table: str) -> pl.DataFrame:
    df = _store().get(table)
    return df if df is not None else schema.empty(table)


def all_appends() -> dict[str, pl.DataFrame]:
    return {t: df for t, df in _store().items() if df is not None and df.height}


def set_appends(table: str, df: pl.DataFrame | None) -> None:
    if df is None or not df.height:
        _store().pop(table, None)
    else:
        _store()[table] = schema.conform(table, df)


def add_appends(table: str, rows: list[dict]) -> None:
    """Validate (loudly) and add rows to tier 1. ``_pending`` is not set here;
    ``view()`` adds it at render time."""
    if not rows:
        return
    new = write.rows_frame(table, rows)
    cur = _store().get(table)
    _store()[table] = new if cur is None else pl.concat([cur, new], how="vertical_relaxed")
    st.session_state[_DIRTY] = True


def append_many(batch: dict[str, list[dict]]) -> None:
    """Validate everything first, then add — so a bad row adds nothing."""
    frames = {t: write.rows_frame(t, rows) for t, rows in batch.items() if rows}
    for table, new in frames.items():
        cur = _store().get(table)
        _store()[table] = new if cur is None else pl.concat([cur, new], how="vertical_relaxed")
    if frames:
        st.session_state[_DIRTY] = True


def clear_appends() -> None:
    _store().clear()
    st.session_state[_DIRTY] = False


def pending_counts() -> dict[str, int]:
    return {t: appends(t).height for t in APPEND_TABLES if appends(t).height}


def has_pending() -> bool:
    return bool(pending_counts())


def unsaved_to_session() -> bool:
    """True when tier 1 holds changes not yet written to tier 2."""
    return bool(st.session_state.get(_DIRTY)) and has_pending()


# ── Reading ──────────────────────────────────────────────────────────────────


def view(table: str) -> pl.DataFrame:
    return read.view(table, appends(table), tokens())


def pending_text_file_ids() -> set[str]:
    df = appends("document_text")
    return set(df["file_id"].to_list()) if df.height else set()


# ── Writing ──────────────────────────────────────────────────────────────────


def save_to_session() -> int:
    n = write.save_to_session(user(), all_appends())
    st.session_state[_DIRTY] = False
    return n


def _prune_committed() -> None:
    """After a partial flush, drop rows that did make it into the database, so a
    retry does not insert them twice."""
    fresh = read.change_tokens()
    st.session_state[_TOKENS] = fresh
    for table, df in list(all_appends().items()):
        committed = (
            read.load_log(table, fresh[table]) if table in schema.READ_TABLES
            else read.load_event_ids(table, fresh[table])
        )
        set_appends(table, read.clean_restored(df, committed))


def save_to_database() -> dict[str, int]:
    """Insert, remove snapshot folder, clear tier 1 — in that order."""
    try:
        counts = write.flush(user(), all_appends())
    except Exception:
        _prune_committed()
        raise
    clear_appends()
    st.session_state[_TOKENS] = read.change_tokens()
    return counts


def discard_session() -> None:
    snapshot.remove_snapshots(user())
    clear_appends()


# ── Restore notice and flash messages ────────────────────────────────────────


def take_restore_report() -> dict[str, int] | None:
    return st.session_state.pop(_REPORT, None)


def flash(message: str, kind: str = "success") -> None:
    st.session_state.setdefault(_FLASH, []).append((kind, message))


def take_flashes() -> list[tuple[str, str]]:
    return st.session_state.pop(_FLASH, [])


def debug_state() -> dict:
    """What tier 1 holds, for the diagnostics page."""
    return {
        "restored": st.session_state.get(_RESTORED, False),
        "dirty": st.session_state.get(_DIRTY, False),
        "appends": {t: df.height for t, df in _store().items()},
        "session_state_keys": sorted(str(k) for k in st.session_state.keys()),
    }


# ── Setup page log ───────────────────────────────────────────────────────────

_SETUP_LOG = "_setup_log"


def setup_log_add(sql: str, ok: bool, message: str) -> None:
    st.session_state.setdefault(_SETUP_LOG, []).insert(0, (ok, sql, message))


def setup_log() -> list[tuple[bool, str, str]]:
    return st.session_state.get(_SETUP_LOG, [])


def try_begin_run() -> str | None:
    """begin_run() for pages that must also work before the schema exists.
    Returns the error message, or None on success."""
    try:
        begin_run()
        return None
    except Exception as exc:
        return str(exc)


# ── Key pair generated on the Setup page (this browser session only) ─────────

_GENERATED_KEY = "_generated_key"


def set_generated_key(private_pem: str, public_pem: str, passphrase: str) -> None:
    st.session_state[_GENERATED_KEY] = (private_pem, public_pem, passphrase)


def generated_key() -> tuple[str, str, str] | None:
    return st.session_state.get(_GENERATED_KEY)
