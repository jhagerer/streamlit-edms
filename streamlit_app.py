"""MiniDMS — a small document management system on Snowflake.

Entry point for both runtimes:
  * Streamlit in Snowflake (container runtime): MAIN_FILE = 'streamlit_app.py'
  * Local: `streamlit run streamlit_app.py` (connects to Snowflake, see README)
"""

import streamlit as st

st.set_page_config(page_title="MiniDMS", page_icon="📁", layout="wide")

from app_pages import _state, _ui  # noqa: E402

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
    ],
}

page = st.navigation(pages)

try:
    _state.begin_run()  # one token probe per rerun + restore on entry
except Exception as exc:  # most likely: no connection or schema not set up
    st.error(f"Cannot reach the MiniDMS schema in Snowflake: {exc}")
    st.info(
        "Check your Snowflake connection (see README → Running locally) and that "
        "`sql/minidms-setup.sql` has been run in the current database/schema."
    )
    st.stop()

_ui.sidebar()
_ui.show_notices()
page.run()
