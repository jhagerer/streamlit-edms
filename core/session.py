"""Snowflake session and viewer identity.

The ONLY module under core/ that imports Streamlit. Everything else in core/
gets the session from ``get_session()``, the viewer from ``actor()`` (as a
plain string) and caching from ``cache_data`` — so a different front end
(a FastAPI service, a test) only has to replace this file.

Two runtimes are supported:

* **Streamlit in Snowflake** — ``get_active_session()`` returns the app's
  session and ``st.user.user_name`` is the *viewer*. ``CURRENT_USER()`` would be
  the app owner and must never be used for attribution there.
* **Local** — ``streamlit run streamlit_app.py`` on a laptop. The session is
  built by ``st.connection("snowflake")`` from ``.streamlit/secrets.toml`` or
  ``~/.snowflake/connections.toml``. The viewer is the person whose
  credentials are used, so ``CURRENT_USER()`` is correct here (and can be
  overridden with ``MINIDMS_USER``).
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable

import streamlit as st

_lock = threading.Lock()
_mode: str | None = None  # "sis" | "local", decided once per process


def _detect_mode() -> str:
    """Decide once, before any local session exists: if Snowpark already has an
    active session at that point, Snowflake created it for us -> SiS."""
    forced = os.environ.get("MINIDMS_RUNTIME", "").strip().lower()
    if forced in ("sis", "local"):
        return forced
    try:
        from snowflake.snowpark.context import get_active_session

        get_active_session()
        return "sis"
    except Exception:
        return "local"


def runtime() -> str:
    global _mode
    if _mode is None:
        with _lock:
            if _mode is None:
                _mode = _detect_mode()
    return _mode


def in_sis() -> bool:
    return runtime() == "sis"


@st.cache_resource(show_spinner="Connecting to Snowflake…")
def _local_session():
    conn_name = os.environ.get("MINIDMS_CONNECTION", "snowflake")
    session = st.connection(conn_name, type="snowflake").session()
    # Optional overrides, handy when the connection has no default db/schema.
    for env, kind in (
        ("MINIDMS_WAREHOUSE", "WAREHOUSE"),
        ("MINIDMS_DATABASE", "DATABASE"),
        ("MINIDMS_SCHEMA", "SCHEMA"),
    ):
        if os.environ.get(env):
            session.sql(f"USE {kind} IDENTIFIER(?)", params=[os.environ[env]]).collect()
    return session


def get_session():
    """The Snowpark session for this process."""
    if in_sis():
        from snowflake.snowpark.context import get_active_session

        return get_active_session()
    return _local_session()


@st.cache_resource(show_spinner=False)
def _local_current_user() -> str:
    return get_session().sql("SELECT CURRENT_USER()").collect()[0][0]


def _st_user_name() -> str | None:
    try:
        user = st.user
        for attr in ("user_name", "email"):
            value = user.get(attr)
            if value:
                return str(value)
    except Exception:
        pass
    return None


def actor() -> str:
    """Name of the person looking at the screen. Used for the actor column and
    for the @sessions/{user}/ folder."""
    if in_sis():
        # Never fall back to CURRENT_USER() in SiS: it is the app owner.
        name = _st_user_name() or os.environ.get("MINIDMS_USER")
    else:
        name = (
            os.environ.get("MINIDMS_USER")
            or _st_user_name()
            or _local_current_user()
        )
    if not name:
        raise RuntimeError(
            "Cannot determine the viewer. st.user.user_name is empty — "
            "set MINIDMS_USER or check the Streamlit runtime."
        )
    return name


def slug(user: str) -> str:
    """Stage-path-safe folder name for a user."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", user.strip()).strip("._-")
    return s.lower() or "anonymous"


def user_tokens() -> dict[str, Any]:
    """The raw st.user dict, for the diagnostics page."""
    try:
        return st.user.to_dict()
    except Exception:
        return {}


def cache_data(func: Callable | None = None, **kwargs):
    """Re-export of ``st.cache_data`` so the rest of core/ stays free of
    Streamlit imports. On the container runtime this cache is shared across
    all viewers of the app — one bulk load serves everybody."""
    kwargs.setdefault("show_spinner", False)
    if func is None:
        return st.cache_data(**kwargs)
    return st.cache_data(func, **kwargs)
