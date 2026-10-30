import streamlit as st

from app_pages import _state, _ui
from core import write

st.title("Document types")
st.caption("Types are never deleted: deactivating hides them. All active metadata types are "
           "offered for every document type.")

user = _state.user()
defs = _ui.document_types(include_inactive=True)

with st.form("dtypes:new", clear_on_submit=True):
    c1, c2 = st.columns([4, 1])
    label = c1.text_input("New document type", placeholder="e.g. Invoice, Contract")
    c2.write("")
    if c2.form_submit_button("Add", type="primary"):
        if not label.strip():
            st.error("Enter a label.")
        elif label.strip().lower() in {(lbl or "").lower() for lbl in defs["label"].to_list()}:
            st.error("A document type with this label exists already.")
        else:
            _state.act(write.create_definition, "document_type", user, {"label": label.strip()},
                       success="Document type added.")
            st.rerun()

if not defs.height:
    st.info("No document types yet.")
for t in defs.iter_rows(named=True):
    tk = f"dtype:{t['document_type_id']}"  # stable key; the version the user saw is remembered
    seen = {**t, "version": _state.baseline(f"{tk}:label", t["version"])}
    with st.container(border=True):
        c1, c2, c3 = st.columns([4, 1, 1])
        new_label = c1.text_input("Label", value=t["label"] or "", key=f"{tk}:label",
                                  label_visibility="collapsed")
        c2.caption("active" if t["active"] else "inactive")
        if c3.button("Save", key=f"{tk}:save") and new_label.strip() != t["label"]:
            _state.act(write.update_definition, "document_type", user, t,
                       {"label": new_label.strip()}, success="Saved.", reset=tk)
            st.rerun()
        if c3.button("Deactivate" if t["active"] else "Activate", key=f"{tk}:toggle"):
            _state.act(write.update_definition, "document_type", user, seen,
                       {"active": not t["active"]})
            st.rerun()
