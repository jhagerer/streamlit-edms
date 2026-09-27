import streamlit as st

from app_pages import _state, _ui
from core import model, write

st.title("Trash")
st.caption("Trashed documents are hidden from the list. Nothing is ever deleted — "
           "restoring appends a `restore` event.")

docs = _ui.document_table()
f = _ui.filter_bar("trash")
shown = model.filter_documents(docs, f["text"], f["document_type_ids"], f["tag_ids"], trashed=True)
doc_id = _ui.document_list(shown, "trash", empty_text="Trash is empty.")

if doc_id:
    current = docs.filter(docs["document_id"] == doc_id).row(0, named=True)
    if st.button(f"Restore “{current['label']}”", key=f"trash:{doc_id}:restore"):
        _state.add_appends("document_log",
                           [write.document_row_from(_state.user(), current, "restore")])
        _state.flash("Document restored (pending until you save to database).")
        st.rerun()
