import streamlit as st

from app_pages import _ui
from core import model

st.title("Documents")

docs = _ui.document_table()
f = _ui.filter_bar("docs")
shown = model.filter_documents(docs, f["text"], f["document_type_ids"], f["tag_ids"])
_ui.document_list(shown, "docs", empty_text="No documents yet — upload some on the Upload page.")
