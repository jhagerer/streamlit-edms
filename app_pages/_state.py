"""Per-browser-session helpers. The only page module touching ``st.session_state``.

There is no local copy of any data: the one global state is in Snowflake.
Session state holds only this rerun's change tokens, the user's notices, the
versions the user last saw (for conflict checks), the warehouse check, and
the Setup page's log and generated key pair.

Every write goes straight to Snowflake through ``write`` / ``act`` below; what
a page renders comes from ``table(name)``, the shared cache keyed on the
change counters read at the top of the rerun.
"""

from __future__ import annotations

import polars as pl
import streamlit as st

from core import model, read
from core.db import Conflict
from core.session import actor

_TOKENS = "_tokens"
_FLASH = "_flash"
_WAREHOUSE = "_warehouse"
_RENDERED = "_rendered"
_BASELINE = "_baseline"


# ── Per-rerun setup ──────────────────────────────────────────────────────────


def begin_run() -> None:
    """Called at the top of every rerun by streamlit_app.py: one point query
    for all change counters."""
    st.session_state[_TOKENS] = read.change_tokens()


def try_begin_run() -> str | None:
    """begin_run() for pages that must also work before the schema exists.
    Returns the error message, or None on success."""
    try:
        begin_run()
        return None
    except Exception as exc:
        return str(exc)


def tokens() -> dict[str, str]:
    if _TOKENS not in st.session_state:
        st.session_state[_TOKENS] = read.change_tokens()
    return st.session_state[_TOKENS]


def refresh() -> None:
    """Re-read the change counters (after this rerun wrote something)."""
    st.session_state[_TOKENS] = read.change_tokens()


def user() -> str:
    return actor()


def warehouse() -> tuple[str | None, str | None]:
    """(current warehouse, type), checked once per scope and browser session."""
    scope = read.scope_of(tokens())
    cached = st.session_state.get(_WAREHOUSE)
    if not cached or cached[0] != scope:
        cached = (scope, *read.warehouse_type())
        st.session_state[_WAREHOUSE] = cached
    return cached[1], cached[2]


# ── Reading (shared cache, keyed on the change counters) ─────────────────────


def table(name: str) -> pl.DataFrame:
    return read.load(name, read.token_for(name, tokens()))


def files(include_inactive: bool = False) -> pl.DataFrame:
    """Attached files with their stored details and AI state."""
    return model.files_view(table("document_file"), table("stored_file"),
                            table("ai_status_v"), include_inactive)


def document_table() -> pl.DataFrame:
    return model.document_table(
        table("document"), table("document_type"), files(), table("document_metadata"),
        table("document_tag"), table("tag"), table("document_enrichment_current"),
    )


# ── Writing ──────────────────────────────────────────────────────────────────


def rendered(key: str, current):
    """What the user saw when this page was last drawn.

    A click reruns the script, which reloads the data first — so the version
    check must use the value from the *previous* render, not the one just
    loaded, or a concurrent edit would never be detected. Returns that value
    (``current`` on the first render) and remembers ``current`` for the next
    rerun. Used for row versions and for sets such as the attached tags."""
    store = st.session_state.setdefault(_RENDERED, {})
    seen = store.get(key, current)
    store[key] = current
    return seen


def baseline(widget_key: str, current):
    """The value an input widget started from — the basis of the conflict check
    for drafts. Captured when the widget is first drawn (its key is not in
    session state yet) and kept, however often the page reloads meanwhile,
    until the widget is reset (after saving or a conflict) or disappears."""
    store = st.session_state.setdefault(_BASELINE, {})
    if widget_key not in st.session_state or widget_key not in store:
        store[widget_key] = current
    return store[widget_key]


def reset_widgets(prefix: str) -> None:
    """Forget widget drafts (and their baselines) whose key starts with
    ``prefix`` so they show the latest values again."""
    for k in [k for k in st.session_state.keys() if str(k).startswith(prefix)]:
        del st.session_state[k]
    store = st.session_state.get(_BASELINE, {})
    for k in [k for k in store if k.startswith(prefix)]:
        del store[k]


def act(fn, *args, success: str | None = None, reset: str | None = None, **kwargs) -> bool:
    """Run one write command; turn conflicts and errors into notices.
    Returns True when something was written. Widget drafts under the ``reset``
    key prefix are cleared after a save or a conflict, so they show the latest
    data (including other people's changes)."""
    try:
        applied = fn(*args, **kwargs)
    except Conflict as exc:
        flash(str(exc), "error")
        if reset:
            reset_widgets(reset)
        return False
    except Exception as exc:
        flash(f"Saving failed: {exc}", "error")
        return False
    if reset:
        reset_widgets(reset)
    if applied is False:
        flash("This change had already been saved.", "info")
        return False
    if success:
        flash(success)
    return True


# ── Notices ──────────────────────────────────────────────────────────────────


def flash(message: str, kind: str = "success") -> None:
    st.session_state.setdefault(_FLASH, []).append((kind, message))


def take_flashes() -> list[tuple[str, str]]:
    return st.session_state.pop(_FLASH, [])


def debug_state() -> dict:
    """What session state holds, for the diagnostics page: no data rows."""
    return {"session_state_keys": sorted(str(k) for k in st.session_state.keys())}


# ── Setup page log ───────────────────────────────────────────────────────────

_SETUP_LOG = "_setup_log"


def setup_log_add(sql: str, ok: bool, message: str) -> None:
    st.session_state.setdefault(_SETUP_LOG, []).insert(0, (ok, sql, message))


def setup_log() -> list[tuple[bool, str, str]]:
    return st.session_state.get(_SETUP_LOG, [])


# ── Key pair generated on the Setup page (this browser session only) ─────────

_GENERATED_KEY = "_generated_key"


def set_generated_key(private_pem: str, public_pem: str, passphrase: str) -> None:
    st.session_state[_GENERATED_KEY] = (private_pem, public_pem, passphrase)


def generated_key() -> tuple[str, str, str] | None:
    return st.session_state.get(_GENERATED_KEY)
