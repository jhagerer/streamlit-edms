import polars as pl
import streamlit as st

from app_pages import _ui
from core import model, search

st.title("Search")
st.caption("Full-text search over the text extracted from your documents (OCR included). "
           "Snowflake finds the matches; type and tag filters then apply in memory.")

c1, c2 = st.columns([4, 1])
query = c1.text_input("Search text", key="search:q", placeholder="words to find…")
mode_label = c2.selectbox("Match", list(search.MODES), key="search:mode")
f = _ui.filter_bar("search", show_text=False)

docs = _ui.document_table()
ids = None
if query.strip():
    try:
        with st.spinner("Searching…"):
            ids = search.search_ids(query, search.MODES[mode_label])
    except Exception as exc:
        st.error(f"Search failed: {exc}")
        st.stop()

shown = model.filter_documents(docs, "", f["document_type_ids"], f["tag_ids"], ids=ids)
_ui.document_list(shown, "search", empty_text="No matching documents.")

not_yet = _ui.file_status().filter(
    pl.col("text_status").is_in(["unsaved", "waiting", "queued"])
)
if not_yet.height:
    n = not_yet["document_id"].n_unique()
    st.info(f"{n} document(s) are not searchable yet: their text is extracted in Snowflake "
            "after they are saved to the database.", icon="ℹ️")
