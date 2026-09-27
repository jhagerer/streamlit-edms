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


def _pem_to_der_b64(pem: str, passphrase: str | None = None) -> str:
    """The connector wants a key as base64 DER; secrets usually hold PEM."""
    import base64

    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(
        pem.strip().encode(), password=passphrase.encode() if passphrase else None
    )
    der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(der).decode()


def _connection_overrides(conn_name: str) -> dict[str, Any]:
    """Extra kwargs for st.connection. Lets a hosted app (e.g. Streamlit
    Community Cloud) keep a PEM private key directly in its secrets."""
    try:
        cfg = st.secrets.get("connections", {}).get(conn_name, {})
    except Exception:
        return {}
    pem = cfg.get("private_key")
    if isinstance(pem, str) and pem.lstrip().startswith("-----BEGIN"):
        return {
            "private_key": _pem_to_der_b64(pem, cfg.get("private_key_passphrase")),
            "private_key_passphrase": None,
            "authenticator": cfg.get("authenticator", "SNOWFLAKE_JWT"),
        }
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

    * MINIDMS_SETUP_USERS (comma-separated, env or secrets) wins if set.
    * In SiS: only the app owner (the viewer equals CURRENT_USER()).
    * Shared deployment with login: nobody unless listed.
    * Plain local run: yes — it is your own connection.
    """
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
