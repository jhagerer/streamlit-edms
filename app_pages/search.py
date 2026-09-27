import polars as pl
import streamlit as st

from app_pages import _state, _ui
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

pending_text = _state.pending_text_file_ids()
if pending_text:
    files = model.current_files(_state.view("document_file_log"))
    n = files.filter(pl.col("file_id").is_in(list(pending_text)))["document_id"].n_unique()
    st.info(f"{n} document(s) have text that is not yet saved to the database — "
            "not yet saved, not yet searchable.", icon="ℹ️")
