import streamlit as st

from app_pages import _state, _ui
from core import write

st.title("Tags")
st.caption("Tags are never deleted: deactivating hides them. Every change is recorded in the "
           "history and checked against concurrent edits.")

user = _state.user()
defs = _ui.tags(include_inactive=True)

with st.form("tags:new", clear_on_submit=True):
    c1, c2, c3 = st.columns([3, 1, 1])
    label = c1.text_input("New tag")
    color = c2.color_picker("Color", value="#1f77b4")
    c3.write("")
    if c3.form_submit_button("Add", type="primary"):
        if not label.strip():
            st.error("Enter a label.")
        elif label.strip().lower() in {(lbl or "").lower() for lbl in defs["label"].to_list()}:
            st.error("A tag with this label exists already.")
        else:
            _state.act(write.create_definition, "tag", user,
                       {"label": label.strip(), "color": color}, success="Tag added.")
            st.rerun()

if not defs.height:
    st.info("No tags yet.")
for t in defs.iter_rows(named=True):
    tk = f"tag:{t['tag_id']}"  # stable key; the version the user saw is remembered
    seen = {**t, "version": _state.baseline(f"{tk}:label", t["version"])}
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
        new_label = c1.text_input("Label", value=t["label"] or "", key=f"{tk}:label",
                                  label_visibility="collapsed")
        new_color = c2.color_picker("Color", value=t["color"] or "#888888", key=f"{tk}:color",
                                    label_visibility="collapsed")
        c3.caption("active" if t["active"] else "inactive")
        if c4.button("Save", key=f"{tk}:save"):
            changes = {f: v for f, v in (("label", new_label.strip()), ("color", new_color))
                       if v != t[f]}
            if changes:
                _state.act(write.update_definition, "tag", user, seen, changes,
                           success="Saved.", reset=tk)
                st.rerun()
        if c4.button("Deactivate" if t["active"] else "Activate", key=f"{tk}:toggle"):
            _state.act(write.update_definition, "tag", user, seen, {"active": not t["active"]})
            st.rerun()
