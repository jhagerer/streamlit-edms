import os

import polars as pl
import streamlit as st

from app_pages import _state, _ui
from core import files, write

st.title("Upload")
st.info(_ui.SAVE_COPY, icon="ℹ️")

types = _ui.document_types()
mtypes = _ui.metadata_types()
tag_defs = _ui.tags()
type_labels = _ui.label_map(types, "document_type_id")
tag_labels = _ui.label_map(tag_defs, "tag_id")
mt_by_id = {r["metadata_type_id"]: r for r in mtypes.iter_rows(named=True)}

if not types.height:
    st.warning("No document types yet. You can upload without one, or create types on "
               "the **Document types** page first.")

with st.form("upload", clear_on_submit=True):
    uploaded = st.file_uploader("Files", accept_multiple_files=True)
    c1, c2 = st.columns([3, 1])
    type_id = c1.selectbox("Document type", [None, *type_labels],
                           format_func=lambda k: "— none —" if k is None else type_labels[k])
    language = c2.text_input("Language", max_chars=8, placeholder="e.g. en, de")
    description = st.text_area("Description (applies to all files)", height=80)
    tag_ids = st.multiselect("Tags", list(tag_labels), format_func=tag_labels.get)
    values = {}
    if mt_by_id:
        st.markdown("**Metadata**")
        cols = st.columns(2)
        for i, (mt_id, mt) in enumerate(mt_by_id.items()):
            with cols[i % 2]:
                values[mt_id] = _ui.metadata_input(mt, key=f"upload:meta:{mt_id}")
    submitted = st.form_submit_button("Upload", type="primary")

if submitted:
    bad = _ui.invalid_numbers(values, mt_by_id)
    if not uploaded:
        st.warning("Choose at least one file.")
    elif bad:
        st.error(f"Not a number: {', '.join(bad)}")
    else:
        user = _state.user()
        progress = st.progress(0.0)
        created, failed = 0, []
        for i, up in enumerate(uploaded, start=1):
            with st.spinner(f"Uploading {up.name} ({i}/{len(uploaded)})"):
                doc_id = write.new_id()
                try:
                    stored = files.stage_upload(up.getvalue(), up.name, up.type, doc_id)
                except Exception as exc:
                    failed.append(f"{up.name}: upload failed — {exc}")
                    progress.progress(i / len(uploaded))
                    continue
                # 1. The document (hybrid tables, one transaction) …
                ok = _state.act(
                    write.create_document, user, document_id=doc_id,
                    fields={"document_type_id": type_id, "label": os.path.splitext(up.name)[0],
                            "description": description or None, "language": language or None},
                    files=[stored], metadata=values, tag_ids=tag_ids,
                )
                # 2. … then the file facts + AI request (standard tables): the stream
                #    on ai_request starts OCR and enrichment in Snowflake.
                if ok:
                    try:
                        write.register_files(user, [stored])
                    except Exception as exc:
                        failed.append(f"{up.name}: saved, but AI processing was not requested "
                                      f"— use “Process again” on the document. ({exc})")
                    created += 1
            if ok and not files.extractable(up.name):
                _state.flash(f"{up.name}: this file type is not converted to PDF (supported: "
                             "PDF, images, plain text) — the document is stored, but its "
                             "content is not searchable.", "info")
            progress.progress(i / len(uploaded))
        for msg in failed:
            _state.flash(msg, "error")
        if created:
            _state.flash(f"{created} document(s) saved. Snowflake is now extracting text and "
                         "enriching them with AI_COMPLETE.")
        st.rerun()

recent = _state.document_table().filter(~pl.col("trashed")).sort("created_at", descending=True)
if recent.height:
    st.subheader("Recently added")
    _ui.document_list(recent.head(10), "upload:recent")
