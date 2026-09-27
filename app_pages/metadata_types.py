import re

import streamlit as st

from app_pages import _state, _ui
from core import schema, write

st.title("Metadata types")
st.caption("Typed fields that documents can carry. Never deleted: deactivating appends "
           "`active = false`. For *choice* fields, enter one option per line.")

user = _state.user()
defs = _ui.metadata_types(include_inactive=True)


def machine_name(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "field"


def edit_form(key: str, mt: dict | None) -> dict | None:
    mt = mt or {}
    with st.form(key, clear_on_submit=mt == {}):
        c1, c2 = st.columns([3, 1])
        label = c1.text_input("Label", value=mt.get("label") or "")
        dtype = c2.selectbox("Data type", schema.DATA_TYPES,
                             index=schema.DATA_TYPES.index(mt.get("data_type") or "text"))
        c3, c4 = st.columns(2)
        choices = c3.text_area("Choices (one per line, for choice)", value=mt.get("choices") or "",
                               height=100)
        default = c4.text_input("Default value", value=mt.get("default_value") or "")
        name = c4.text_input("Machine name (optional)", value=mt.get("name") or "")
        if st.form_submit_button("Save" if mt else "Add", type="primary"):
            if not label.strip():
                st.error("Enter a label.")
                return None
            if dtype == "choice" and not choices.strip():
                st.error("A choice field needs at least one choice.")
                return None
            return {"label": label.strip(), "data_type": dtype,
                    "choices": choices.strip() or None, "default_value": default.strip() or None,
                    "name": name.strip() or machine_name(label)}
    return None


st.subheader("New metadata type")
new = edit_form("mtypes:new", None)
if new:
    _state.add_appends("metadata_type_log",
                       [write.metadata_type_row(user, write.new_id(), **new)])
    st.rerun()

st.subheader("Existing")
if not defs.height:
    st.info("No metadata types yet.")
for mt in defs.iter_rows(named=True):
    mk = f"mtype:{mt['metadata_type_id']}"
    status = ("active" if mt["active"] else "inactive") + (" ● unsaved" if mt["_pending"] else "")
    with st.expander(f"{mt['label']} · {mt['data_type']} · {status}"):
        changed = edit_form(f"{mk}:form", mt)
        if changed:
            _state.add_appends("metadata_type_log", [write.metadata_type_row(
                user, mt["metadata_type_id"], active=mt["active"], **changed)])
            st.rerun()
        if st.button("Deactivate" if mt["active"] else "Activate", key=f"{mk}:toggle"):
            _state.add_appends("metadata_type_log", [write.metadata_type_row(
                user, mt["metadata_type_id"], mt["name"], mt["label"], mt["data_type"],
                mt["choices"], mt["default_value"], not mt["active"])])
            st.rerun()
