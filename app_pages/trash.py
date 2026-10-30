import streamlit as st

from app_pages import _state, _ui
from core import model, write

st.title("Trash")
st.caption("Trashed documents are hidden from the list. Nothing is deleted: the document's "
           "status changes, and the change is recorded in the history.")

docs = _state.document_table()
f = _ui.filter_bar("trash")
shown = model.filter_documents(docs, f["text"], f["document_type_ids"], f["tag_ids"], trashed=True)
doc_id = _ui.document_list(shown, "trash", empty_text="Trash is empty.")

if doc_id:
    current = docs.filter(docs["document_id"] == doc_id).row(0, named=True)
    if st.button(f"Restore “{current['label']}”", key=f"trash:{doc_id}:restore"):
        _state.act(write.update_document, _state.user(), current, {}, op="restore",
                   success="Document restored.")
        st.rerun()
