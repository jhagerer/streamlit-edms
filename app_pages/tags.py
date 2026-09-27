import streamlit as st

from app_pages import _state, _ui
from core import write

st.title("Tags")
st.caption("Tags are never deleted: deactivating appends `active = false`.")

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
        elif label.strip().lower() in {(l or "").lower() for l in defs["label"].to_list()}:
            st.error("A tag with this label exists already.")
        else:
            _state.add_appends("tag_log", [write.tag_row(user, write.new_id(), label.strip(), color)])
            st.rerun()

if not defs.height:
    st.info("No tags yet.")
for t in defs.iter_rows(named=True):
    tk = f"tag:{t['tag_id']}"
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
        new_label = c1.text_input("Label", value=t["label"] or "", key=f"{tk}:label",
                                  label_visibility="collapsed")
        new_color = c2.color_picker("Color", value=t["color"] or "#888888", key=f"{tk}:color",
                                    label_visibility="collapsed")
        status = ("active" if t["active"] else "inactive") + ("  ● unsaved" if t["_pending"] else "")
        c3.caption(status)
        if c4.button("Save", key=f"{tk}:save"):
            if (new_label, new_color) != (t["label"], t["color"]):
                _state.add_appends("tag_log", [write.tag_row(user, t["tag_id"], new_label.strip(),
                                                             new_color, t["active"])])
                st.rerun()
        if c4.button("Deactivate" if t["active"] else "Activate", key=f"{tk}:toggle"):
            _state.add_appends("tag_log", [write.tag_row(user, t["tag_id"], t["label"],
                                                         t["color"], not t["active"])])
            st.rerun()
