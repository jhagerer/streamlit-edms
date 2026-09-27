"""Snowflake session and viewer identity.

The ONLY module under core/ that imports Streamlit. Everything else in core/
gets the session from ``get_session()``, the viewer from ``actor()`` (as a
plain string) and caching from ``cache_data`` — so a different front end
(a FastAPI service, a test) only has to replace this file.

Three ways to get a connection, checked in this order:

* **Streamlit in Snowflake** — ``get_active_session()`` returns the app's
  session and ``st.user.user_name`` is the *viewer*. ``CURRENT_USER()`` would be
  the app owner and must never be used for attribution there.
* **Credentials entered on the Setup page** (outside SiS only) — a Snowpark
  session for this browser session only, kept in memory, never written to
  disk. The viewer is the Snowflake user of those credentials (or the login
  e-mail when Streamlit login is configured).
* **Shared connection from secrets** — local runs, Streamlit Community Cloud.
  Built by ``st.connection("snowflake")`` from ``.streamlit/secrets.toml``
  (Community Cloud: the app's Secrets settings) or
  ``~/.snowflake/connections.toml``. On a plain local run the viewer is the
  person whose credentials are used, so ``CURRENT_USER()`` is correct there
  (overridable with ``MINIDMS_USER``).

The connection entered on the Setup page is the one deliberate use of
``st.session_state`` outside ``app_pages/_state.py``: it is a connection, not
data, and it must never be shared between viewers.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable

import streamlit as st

from core.connection import ConnectionSettings, connector_params, pem_to_der_b64

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


def _connection_overrides(conn_name: str) -> dict[str, Any]:
    """Extra kwargs for st.connection. Lets a hosted app (e.g. Streamlit
    Community Cloud) keep a PEM private key directly in its secrets."""
    try:
        cfg = st.secrets.get("connections", {}).get(conn_name, {})
    except Exception:
        return {}
    # Without keep-alive an idle shared session expires after a few hours
    # and every query fails until the app restarts.
    overrides: dict[str, Any] = {"client_session_keep_alive": True}
    pem = cfg.get("private_key")
    if isinstance(pem, str) and pem.lstrip().startswith("-----BEGIN"):
        overrides.update({
            "private_key": pem_to_der_b64(pem, cfg.get("private_key_passphrase")),
            "private_key_passphrase": None,
            "authenticator": cfg.get("authenticator", "SNOWFLAKE_JWT"),
        })
    return overrides


def secrets_connection_config() -> dict[str, Any]:
    """The [connections.<name>] section from secrets, or {}."""
    conn_name = os.environ.get("MINIDMS_CONNECTION", "snowflake")
    try:
        return dict(st.secrets.get("connections", {}).get(conn_name, {}))
    except Exception:
        return {}


@st.cache_resource(show_spinner="Connecting to Snowflake…")
def _local_session():
    conn_name = os.environ.get("MINIDMS_CONNECTION", "snowflake")
    session = st.connection(
        conn_name, type="snowflake", **_connection_overrides(conn_name)
    ).session()
    # Optional overrides, handy when the connection has no default db/schema.
    for env, kind in (
        ("MINIDMS_WAREHOUSE", "WAREHOUSE"),
        ("MINIDMS_DATABASE", "DATABASE"),
        ("MINIDMS_SCHEMA", "SCHEMA"),
    ):
        if os.environ.get(env):
            session.sql(f"USE {kind} IDENTIFIER(?)", params=[os.environ[env]]).collect()
    return session


_OWN = "_minidms_own_connection"


def _own() -> dict | None:
    """The connection entered on the Setup page for this browser session."""
    try:
        return st.session_state.get(_OWN)
    except Exception:
        return None


def connection_source() -> str:
    """'sis' | 'own' (entered in this browser session) | 'shared' (secrets)."""
    if in_sis():
        return "sis"
    return "own" if _own() else "shared"


def open_session(settings: ConnectionSettings):
    """Open (and test) a Snowpark session from settings. Returns
    (session, CURRENT_USER())."""
    runtime()  # decide the runtime before any local session exists
    from snowflake.snowpark import Session

    params = connector_params(settings)
    params["client_session_keep_alive"] = True
    params["login_timeout"] = 30  # fail fast on a wrong account name
    # A fresh builder every time: configs() MERGES options into the builder, so
    # a builder reused across viewers could carry one viewer's key into another
    # viewer's connection. Whether Session.builder is shared depends on the
    # Snowpark version (1.55 returns a new one per access), so do not rely on it.
    session = Session.SessionBuilder().configs(params).create()
    try:
        user = session.sql("SELECT CURRENT_USER()").collect()[0][0]
    except Exception:
        session.close()
        raise
    return session, str(user)


def connect_own(settings: ConnectionSettings) -> str:
    """Use these credentials for this browser session. Returns CURRENT_USER()."""
    if in_sis():
        raise RuntimeError("In Streamlit in Snowflake the app's own session is used.")
    previous = own_settings()
    settings = settings.merged_with(previous)
    session, user = open_session(settings)
    disconnect_own()
    st.session_state[_OWN] = {"session": session, "settings": settings, "user": user}
    return user


def own_settings() -> ConnectionSettings | None:
    own = _own()
    return own["settings"] if own else None


def disconnect_own() -> None:
    own = _own()
    if own:
        try:
            own["session"].close()
        except Exception:
            pass
        st.session_state.pop(_OWN, None)


def reconnect_shared() -> None:
    """Drop the cached shared connection, e.g. after the secrets changed."""
    _local_session.clear()
    _local_current_user.clear()
    try:
        st.connection(os.environ.get("MINIDMS_CONNECTION", "snowflake"),
                      type="snowflake").reset()
    except Exception:
        pass


def get_session():
    """The Snowpark session for this rerun."""
    if in_sis():
        from snowflake.snowpark.context import get_active_session

        return get_active_session()
    own = _own()
    if own:
        return own["session"]
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
    elif connection_source() == "own":
        # Own credentials: the Snowflake user is the viewer, unless a login
        # identity is available (several people may share a service user).
        name = (_st_user_name() if is_logged_in() else None) or _own()["user"]
    elif auth_configured():
        # Shared deployment: everyone connects as one service user, so
        # CURRENT_USER() would merge all viewers. Use the logged-in identity.
        name = _st_user_name() if is_logged_in() else None
    else:
        name = (
            os.environ.get("MINIDMS_USER")
            or _st_user_name()
            or _local_current_user()
        )
    if not name:
        raise RuntimeError(
            "Cannot determine the viewer: st.user is empty (not logged in?). "
            "Set MINIDMS_USER or check the Streamlit runtime."
        )
    return name


def auth_configured() -> bool:
    """True when Streamlit's OIDC login ([auth] in secrets) is set up — the case
    for a shared hosted deployment such as Streamlit Community Cloud, where
    everyone uses the same Snowflake service user."""
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def is_logged_in() -> bool:
    try:
        return bool(st.user.is_logged_in)
    except Exception:
        return False


def _setting(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        try:
            value = st.secrets.get(name)
        except Exception:
            value = None
    return str(value or "")


def setup_allowed() -> tuple[bool, str]:
    """Who may run DDL and grants from the Setup page.

    * Own credentials entered on the Setup page: yes — Snowflake's own
      privileges decide what actually succeeds.
    * MINIDMS_SETUP_USERS (comma-separated, env or secrets), if set.
    * In SiS: only the app owner (the viewer equals CURRENT_USER()).
    * Shared deployment with login: nobody unless listed.
    * Plain local run: yes — it is your own connection.
    """
    if connection_source() == "own":
        return True, "your own credentials — Snowflake decides what you may do"
    listed = [u.strip().lower() for u in _setting("MINIDMS_SETUP_USERS").split(",") if u.strip()]
    me = actor().lower()
    if listed:
        return me in listed, "listed in MINIDMS_SETUP_USERS" if me in listed else \
            "not listed in MINIDMS_SETUP_USERS"
    if in_sis():
        owner = str(get_session().sql("SELECT CURRENT_USER()").collect()[0][0]).lower()
        return me == owner, "you are the app owner" if me == owner else \
            "only the app owner may run setup in Streamlit in Snowflake"
    if auth_configured():
        return False, "shared deployment: set MINIDMS_SETUP_USERS to allow setup"
    return True, "local run with your own connection"


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
