"""MiniDMS — a small document management system on Snowflake.

Entry point for all runtimes:
  * Streamlit in Snowflake (container runtime): MAIN_FILE = 'streamlit_app.py'
  * Local: `streamlit run streamlit_app.py` (connects to Snowflake, see README)
  * Streamlit Community Cloud: same file, secrets in the app settings
"""

import streamlit as st

st.set_page_config(page_title="MiniDMS", page_icon="📁", layout="wide")

from app_pages import _state, _ui  # noqa: E402
from core.session import auth_configured, is_logged_in  # noqa: E402

setup_page = st.Page("app_pages/setup.py", title="Setup", icon="⚙️")

pages = {
    "Documents": [
        st.Page("app_pages/documents.py", title="Documents", icon="📄", default=True),
        st.Page("app_pages/upload.py", title="Upload", icon="⬆️"),
        st.Page("app_pages/search.py", title="Search", icon="🔎"),
        st.Page("app_pages/trash.py", title="Trash", icon="🗑️"),
        st.Page("app_pages/document.py", title="Document", icon="📑", visibility="hidden"),
    ],
    "Setup": [
        st.Page("app_pages/document_types.py", title="Document types", icon="🗂️"),
        st.Page("app_pages/metadata_types.py", title="Metadata types", icon="🏷️"),
        st.Page("app_pages/tags.py", title="Tags", icon="🔖"),
    ],
    "System": [
        st.Page("app_pages/audit.py", title="Audit trail", icon="📜"),
        st.Page("app_pages/admin.py", title="Admin", icon="🛠️"),
        st.Page("app_pages/diagnostics.py", title="Diagnostics", icon="🩺"),
        setup_page,
    ],
}

page = st.navigation(pages)

# Shared hosted deployment (e.g. Community Cloud): require a login so every
# viewer has their own identity and their own @sessions folder.
if auth_configured() and not is_logged_in():
    st.title("MiniDMS")
    st.write("Please log in to continue.")
    st.button("Log in", type="primary", on_click=st.login)
    st.stop()

if page.url_path == setup_page.url_path:
    # The Setup page must work before the schema exists.
    if _state.try_begin_run() is None:
        _ui.sidebar()
    _ui.show_notices()
    page.run()
    st.stop()

error = _state.try_begin_run()  # one token probe per rerun + restore on entry
if error:
    st.error(f"Cannot reach the MiniDMS schema in Snowflake: {error}")
    st.info(
        "Check your Snowflake connection (see README) and that the MiniDMS objects "
        "exist in the current database/schema."
    )
    st.page_link(setup_page, label="Open the Setup page to create them step by step", icon="⚙️")
    st.stop()

_ui.sidebar()
_ui.show_notices()
page.run()
