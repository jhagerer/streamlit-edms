"""Setup step 1: see, enter and change the Snowflake connection.

Outside Streamlit in Snowflake (locally, on Streamlit Community Cloud) a viewer
can enter credentials here — account, user, key pair / token / password,
role, warehouse, database, schema. They are used for this browser session
only, kept in server memory, never written to disk. To make them permanent,
the page renders a secrets.toml section to paste into the Community Cloud
secrets settings or a local .streamlit/secrets.toml.
"""

from __future__ import annotations

import streamlit as st

from app_pages import _state
from core import connection, setup
from core.connection import AUTH_METHODS, ConnectionSettings
from core.session import (connect_own, connection_source, disconnect_own, open_session,
                          own_settings, reconnect_shared, secrets_connection_config)

SOURCE_LABELS = {
    "sis": "Streamlit in Snowflake — the app's own session",
    "own": "Credentials you entered for this browser session",
    "shared": "The app's shared connection (secrets / connections.toml)",
}


def _show_context(ctx: dict) -> None:
    c = st.columns(6)
    for col, key in zip(c, ("account", "user", "role", "warehouse", "database", "schema")):
        col.metric(key.capitalize(), ctx[key] or "—")


def _prefill() -> ConnectionSettings:
    return own_settings() or connection.settings_from_secrets(secrets_connection_config())


def _read_key(uploaded, pasted: str) -> str:
    if uploaded is not None:
        return uploaded.getvalue().decode("utf-8", errors="replace")
    if pasted.strip():
        return pasted
    gen = _state.generated_key()
    return gen[0] if gen else ""


def _credentials_form(pre: ConnectionSettings, have_previous: bool) -> None:
    auth_keys = list(AUTH_METHODS)
    auth = st.radio("Authentication", auth_keys, index=auth_keys.index(pre.auth),
                    format_func=AUTH_METHODS.get, key="conn:auth", horizontal=True)
    keep = " Leave empty to keep the current one." if have_previous else ""
    gen = _state.generated_key()

    with st.form("conn:form"):
        c1, c2 = st.columns(2)
        account = c1.text_input("Account", value=pre.account,
                                help="Account identifier, e.g. `myorg-myaccount`. A full "
                                     "`https://….snowflakecomputing.com` URL also works.")
        user = c2.text_input("User", value=pre.user)
        uploaded, pasted, passphrase, password, passcode, token = None, "", "", "", "", ""
        if auth == "keypair":
            uploaded = st.file_uploader("Private key file (.p8 / .pem)", type=["p8", "pem", "key"])
            pasted = st.text_area("…or paste the private key (PEM)", height=100,
                                  placeholder="-----BEGIN PRIVATE KEY-----")
            passphrase = st.text_input("Key passphrase (only for encrypted keys)", type="password")
            if gen:
                st.caption("Leave both empty to use the key pair generated below.")
            elif keep:
                st.caption(keep.strip())
        elif auth == "pat":
            token = st.text_input("Programmatic access token", type="password",
                                  help="Created in Snowsight under your user profile, or with "
                                       "ALTER USER … ADD PROGRAMMATIC ACCESS TOKEN." + keep)
        elif auth == "password":
            password = st.text_input("Password", type="password", help=keep.strip() or None)
            passcode = st.text_input("MFA passcode (if your user has MFA)", type="password")
            st.caption("Snowflake is phasing out password-only sign-in; prefer a key pair "
                       "or a token for hosted apps.")
        else:
            st.warning("Browser SSO opens a login window on the machine that runs the app. "
                       "It only works when you run the app on your own computer — not on "
                       "Streamlit Community Cloud.")
        c1, c2, c3, c4 = st.columns(4)
        role = c1.text_input("Role", value=pre.role)
        warehouse = c2.text_input("Warehouse", value=pre.warehouse)
        database = c3.text_input("Database", value=pre.database)
        schema = c4.text_input("Schema", value=pre.schema)
        b1, b2 = st.columns(2)
        test = b1.form_submit_button("Test connection")
        use = b2.form_submit_button("Connect for this browser session", type="primary")

    if not (test or use):
        return
    settings = ConnectionSettings(
        account=account, user=user, auth=auth, role=role, warehouse=warehouse,
        database=database, schema=schema,
        private_key_pem=_read_key(uploaded, pasted) if auth == "keypair" else "",
        private_key_passphrase=passphrase or (gen[2] if gen and auth == "keypair"
                                              and not (uploaded or pasted.strip()) else ""),
        password=password, passcode=passcode, token=token,
    ).merged_with(own_settings())
    errors = connection.validate(settings)
    if errors:
        for e in errors:
            st.error(e)
        return
    try:
        with st.spinner("Connecting to Snowflake…"):
            if use:
                user_name = connect_own(settings)
                _state.flash(f"Connected as {user_name} for this browser session.")
                st.rerun()
            session, user_name = open_session(settings)
            try:
                ctx = setup.context(session)
            finally:
                session.close()
        st.success(f"Connection works — signed in as {user_name}.")
        _show_context(ctx)
    except Exception as exc:
        st.error(f"Connection failed: {exc}")


def _key_pair_helper(pre: ConnectionSettings, connected: bool) -> None:
    st.write("Creates a new RSA key pair in the app. Register the **public** key with "
             "your Snowflake user, keep the **private** key safe.")
    passphrase = st.text_input("Encrypt the private key with a passphrase (optional)",
                               type="password", key="conn:gen:pass")
    if st.button("Generate key pair", key="conn:gen"):
        private_pem, public_pem = connection.generate_key_pair(passphrase or None)
        _state.set_generated_key(private_pem, public_pem, passphrase)
    gen = _state.generated_key()
    if not gen:
        return
    private_pem, public_pem, _ = gen
    st.download_button("Download private key (rsa_key.p8)", private_pem, file_name="rsa_key.p8",
                       mime="application/x-pem-file", key="conn:gen:dl")
    user = st.text_input("Snowflake user to register the key for", value=pre.user,
                         key="conn:gen:user")
    if user.strip():
        sql = connection.alter_user_sql(user, public_pem)
        st.code(sql + ";", language="sql")
        st.caption("Run this as the user itself or as an admin (e.g. in a worksheet), or "
                   "below with the current connection. Snowflake users can hold two keys "
                   "(RSA_PUBLIC_KEY and RSA_PUBLIC_KEY_2) for rotation.")
        if connected and st.button("Run ALTER USER with the current connection",
                                   key="conn:gen:alter"):
            try:
                setup.run(sql)
                _state.setup_log_add(sql, True, "OK")
                st.success("Public key registered.")
            except Exception as exc:
                _state.setup_log_add(sql, False, str(exc))
                st.error(f"Could not register the key: {exc}")


def _save_permanently() -> None:
    settings = own_settings()
    if not settings:
        st.info("Connect with the form above first; the settings then appear here.")
        return
    show = st.toggle("Include secrets (key, token, password)", value=False, key="conn:toml:show")
    toml = connection.secrets_toml(settings, include_secrets=show)
    st.code(toml, language="toml")
    if show:
        st.download_button("Download secrets.toml", toml, file_name="secrets.toml",
                           key="conn:toml:dl")
    st.markdown(
        "- **Streamlit Community Cloud:** your app → **⋮ → Settings → Secrets**, paste, "
        "**Save**. The app restarts and uses it as the shared connection.\n"
        "- **Local:** save it as `.streamlit/secrets.toml` next to `streamlit_app.py`, "
        "then restart the app (or press *Reconnect shared connection*).\n"
        "- Keep the file private. `.streamlit/secrets.toml` is git-ignored."
    )


def connection_step() -> dict | None:
    """Render step 1. Returns the current context, or None when not connected."""
    st.header("1 · Connection")
    source = connection_source()
    ctx, error = None, None
    try:
        ctx = setup.context()
    except Exception as exc:
        error = str(exc)

    if source == "sis":
        if ctx:
            _show_context(ctx)
        st.caption("Streamlit in Snowflake provides the session — there are no credentials "
                   "to enter. The app uses its own database and schema; the objects must be "
                   "created there.")
        return ctx

    if ctx:
        _show_context(ctx)
        st.caption(f"Source: {SOURCE_LABELS[source]}.")
    else:
        st.warning(f"Not connected to Snowflake. Enter credentials below. ({error})")

    locked = _state.has_pending()
    if locked:
        st.warning("You have unsaved changes. Save them to the database (or discard them) "
                   "before you change the connection — they belong to the current "
                   "account and schema.", icon="⚠️")
    else:
        b1, b2, _ = st.columns([1, 1, 2])
        if source == "own" and b1.button(
                "Disconnect", help="Forget the credentials entered here and fall back to "
                                   "the app's shared connection, if any."):
            disconnect_own()
            _state.flash("Disconnected.", "info")
            st.rerun()
        if source == "shared" and b2.button(
                "Reconnect shared connection",
                help="Re-read the secrets, e.g. after you changed them."):
            reconnect_shared()
            st.rerun()

    pre = _prefill()
    if not locked:
        with st.expander("Enter or change Snowflake credentials", expanded=ctx is None):
            st.caption("Used for **this browser session only**: kept in the app server's "
                       "memory, never written to disk, gone when you close the tab. Other "
                       "viewers are not affected.")
            _credentials_form(pre, have_previous=own_settings() is not None)
    with st.expander("Generate a key pair"):
        _key_pair_helper(pre, connected=ctx is not None)
    with st.expander("Save the connection permanently (secrets.toml)"):
        _save_permanently()
    return ctx
