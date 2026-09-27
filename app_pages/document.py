"""Document detail. Every widget key is namespaced by document id so switching
documents never shows the previous one's draft."""

import polars as pl
import streamlit as st

from app_pages import _state, _ui
from core import files, model, read, write

docs = _ui.document_table()

doc_id = st.query_params.get("doc")
if not doc_id or not docs.filter(pl.col("document_id") == doc_id).height:
    st.title("Document")
    if not docs.height:
        st.info("No documents yet.")
        st.stop()
    labels = {r["document_id"]: f"{r['label']}{'  (trashed)' if r['trashed'] else ''}"
              for r in docs.iter_rows(named=True)}
    picked = st.selectbox("Choose a document", list(labels), format_func=labels.get, index=None)
    if picked:
        st.query_params["doc"] = picked
        st.rerun()
    st.stop()

doc = docs.filter(pl.col("document_id") == doc_id).row(0, named=True)
user = _state.user()
k = f"doc:{doc_id}"  # widget key namespace

types = _ui.document_types(include_inactive=True)
type_labels = _ui.label_map(types, "document_type_id")
active_types = _ui.label_map(_ui.document_types(), "document_type_id")

st.page_link("app_pages/documents.py", label="← All documents")
title = doc["label"] or "(no label)"
st.title(("🗑️ " if doc["trashed"] else "") + title)
if doc["trashed"]:
    st.warning("This document is in the trash.")
if doc["pending"]:
    st.caption("● This document has unsaved changes.")

tab_props, tab_meta, tab_tags, tab_files, tab_text, tab_hist = st.tabs(
    ["Properties", "Metadata", "Tags", "Files", "Text", "History"]
)

# ── Properties ───────────────────────────────────────────────────────────────


@st.dialog("Edit properties")
def edit_properties():
    options = list(dict.fromkeys([None, *active_types, doc["document_type_id"]]))
    label = st.text_input("Label", value=doc["label"] or "", key=f"{k}:label")
    type_id = st.selectbox(
        "Document type", options, index=options.index(doc["document_type_id"]),
        format_func=lambda t: "— none —" if t is None else type_labels.get(t, t),
        key=f"{k}:type",
    )
    description = st.text_area("Description", value=doc["description"] or "", key=f"{k}:desc")
    language = st.text_input("Language", value=doc["language"] or "", max_chars=8, key=f"{k}:lang")
    if st.button("Apply", type="primary", key=f"{k}:apply"):
        changes = {"label": label.strip() or None, "document_type_id": type_id,
                   "description": description or None, "language": language.strip() or None}
        if any(changes[f] != doc[f] for f in changes):
            _state.add_appends("document_log",
                               [write.document_row_from(user, doc, "update", **changes)])
            _state.flash("Properties updated (pending).")
        st.rerun()


with tab_props:
    c1, c2 = st.columns(2)
    c1.markdown(f"**Type:** {type_labels.get(doc['document_type_id'], '—')}")
    c1.markdown(f"**Language:** {doc['language'] or '—'}")
    c1.markdown(f"**Tags:** {', '.join(doc['tags']) or '—'}")
    c2.markdown(f"**Created:** {doc['created_at']:%Y-%m-%d %H:%M} UTC by {doc['created_by']}")
    c2.markdown(f"**Updated:** {doc['updated_at']:%Y-%m-%d %H:%M} UTC by {doc['updated_by']}")
    c2.markdown(f"**Id:** `{doc_id}`")
    st.markdown("**Description**")
    st.write(doc["description"] or "—")
    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("Edit", key=f"{k}:edit"):
        edit_properties()
    if doc["trashed"]:
        if b2.button("Restore", key=f"{k}:restore"):
            _state.add_appends("document_log", [write.document_row_from(user, doc, "restore")])
            st.rerun()
    elif b2.button("Move to trash", key=f"{k}:trash"):
        _state.add_appends("document_log", [write.document_row_from(user, doc, "trash")])
        st.rerun()
    st.caption("Last writer wins: an edit saves the whole record, so a concurrent edit "
               "by someone else to another field of this document may be overwritten.")

# ── Metadata ─────────────────────────────────────────────────────────────────

with tab_meta:
    mtypes_all = _ui.metadata_types(include_inactive=True)
    mt_all = {r["metadata_type_id"]: r for r in mtypes_all.iter_rows(named=True)}
    current = {
        r["metadata_type_id"]: r["value"]
        for r in model.current_metadata(_state.view("metadata_log"))
        .filter(pl.col("document_id") == doc_id).iter_rows(named=True)
    }
    active_ids = [r["metadata_type_id"] for r in _ui.metadata_types().iter_rows(named=True)]
    # Show active types plus any inactive type that still has a value here.
    shown_ids = list(dict.fromkeys(active_ids + [m for m in current if m in mt_all]))
    if not shown_ids:
        st.info("No metadata types defined yet — create some on the Metadata types page.")
    else:
        with st.form(f"{k}:meta"):
            values = {}
            cols = st.columns(2)
            for i, mt_id in enumerate(shown_ids):
                with cols[i % 2]:
                    values[mt_id] = _ui.metadata_input(
                        {**mt_all[mt_id], "default_value": None}, key=f"{k}:meta:{mt_id}",
                        current=current.get(mt_id, ""),
                    )
            if st.form_submit_button("Apply metadata changes"):
                bad = _ui.invalid_numbers(values, mt_all)
                if bad:
                    st.error(f"Not a number: {', '.join(bad)}")
                else:
                    rows = [write.metadata_row(user, doc_id, mt_id, v)
                            for mt_id, v in values.items() if v != current.get(mt_id, "")]
                    _state.add_appends("metadata_log", rows)
                    if rows:
                        _state.flash(f"{len(rows)} metadata change(s) pending.")
                    st.rerun()

# ── Tags ─────────────────────────────────────────────────────────────────────

with tab_tags:
    tag_defs = _ui.tags()
    tag_labels = _ui.label_map(tag_defs, "tag_id")
    colors = {r["tag_id"]: r["color"] for r in tag_defs.iter_rows(named=True)}
    attached = [t for t in doc["tag_ids"] if t in tag_labels]
    if attached:
        st.markdown(" ".join(
            f"<span style='background:{colors.get(t) or '#888'};color:white;padding:2px 8px;"
            f"border-radius:10px;margin-right:4px'>{tag_labels[t]}</span>" for t in attached
        ), unsafe_allow_html=True)
    if not tag_labels:
        st.info("No tags defined yet — create some on the Tags page.")
    else:
        chosen = st.multiselect("Tags", list(tag_labels), default=attached,
                                format_func=tag_labels.get, key=f"{k}:tags")
        if st.button("Apply tag changes", key=f"{k}:tags:apply"):
            rows = [write.tag_assignment_row(user, doc_id, t, True)
                    for t in chosen if t not in attached]
            rows += [write.tag_assignment_row(user, doc_id, t, False)
                     for t in attached if t not in chosen]
            _state.add_appends("tag_assignment_log", rows)
            st.rerun()

# ── Files ────────────────────────────────────────────────────────────────────

doc_files = model.current_files(_state.view("document_file_log")).filter(
    pl.col("document_id") == doc_id
)

with tab_files:
    if not doc_files.height:
        st.info("No files.")
    for f in doc_files.iter_rows(named=True):
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 1, 1])
            c1.markdown(f"**{f['filename']}**" + ("  ● unsaved" if f["_pending"] else ""))
            c1.caption(
                f"{f['mimetype']} · {_ui.fmt_size(f['size'])} · "
                f"{f['page_count'] or '?'} page(s) · sha256 `{(f['checksum'] or '')[:16]}…`  \n"
                f"added {f['event_ts']:%Y-%m-%d %H:%M} UTC by {f['actor']}"
            )
            fk = f"{k}:file:{f['file_id']}"
            if c2.button("Download", key=f"{fk}:prep"):
                try:
                    data = files.file_bytes(
                        f["stage_path"], _state.tokens()["document_file_log"].split("|")[0])
                    c2.download_button("Save file", data, file_name=f["filename"],
                                       mime=f["mimetype"], key=f"{fk}:dl")
                except Exception as exc:
                    st.error(f"Download failed: {exc}")
            if c3.button("Remove", key=f"{fk}:rm", help="Marks the file inactive; bytes are kept."):
                row = write.file_row(user, **{c: f[c] for c in (
                    "document_id", "file_id", "filename", "stage_path", "mimetype", "size",
                    "checksum", "page_count")}, active=False)
                _state.add_appends("document_file_log", [row])
                st.rerun()

    with st.expander("Add a file to this document"):
        up = st.file_uploader("File", key=f"{k}:addfile")
        if up is not None and st.button("Upload file", key=f"{k}:addfile:go"):
            with st.spinner("Uploading and extracting text…"):
                try:
                    res = files.store_upload(up.getvalue(), up.name, up.type, doc_id, user)
                except Exception as exc:
                    st.error(f"Upload failed: {exc}")
                    st.stop()
            _state.append_many({
                "document_file_log": [res.file_row],
                "document_text": [res.text_row] if res.text_row else [],
            })
            if res.error:
                _state.flash(f"Added without text — {res.error}", "warning")
            st.rerun()

# ── Text ─────────────────────────────────────────────────────────────────────

with tab_text:
    tokens = _state.tokens()
    committed = read.load_texts(tuple(doc_files["file_id"].to_list()), tokens["document_text"])
    pending_txt = read.latest(_state.appends("document_text"), ["file_id"])
    for f in doc_files.iter_rows(named=True):
        st.markdown(f"**{f['filename']}**")
        pend = pending_txt.filter(pl.col("file_id") == f["file_id"])
        comm = committed.filter(pl.col("file_id") == f["file_id"])
        if pend.height:
            st.caption("● Text not yet saved — not yet searchable.")
            text = pend["content"][0]
        elif comm.height:
            text = comm["content"][0]
        else:
            text = None
        if text is None:
            st.info("No extracted text for this file.")
        else:
            st.text_area("Extracted text", text or "", height=300, disabled=True,
                         key=f"{k}:text:{f['file_id']}", label_visibility="collapsed")
        if st.button("Re-run text extraction", key=f"{k}:text:{f['file_id']}:retry"):
            with st.spinner("Extracting text…"):
                try:
                    _state.add_appends("document_text", [files.retry_extraction(f, user)])
                    st.rerun()
                except Exception as exc:
                    st.error(f"Extraction failed: {exc}")

# ── History ──────────────────────────────────────────────────────────────────

with tab_hist:
    tag_names = _ui.label_map(_ui.tags(include_inactive=True), "tag_id")
    mt_names = _ui.label_map(_ui.metadata_types(include_inactive=True), "metadata_type_id")
    parts = [
        model.history(_state.view("document_log"), document_id=doc_id).select(
            "event_ts", "actor", pl.col("op").alias("event"),
            pl.concat_str([pl.lit("label="), pl.col("label").fill_null("")]).alias("detail"),
            "_pending"),
        model.history(_state.view("metadata_log"), document_id=doc_id).select(
            "event_ts", "actor", pl.lit("metadata").alias("event"),
            pl.concat_str([pl.col("metadata_type_id").replace_strict(mt_names, default=pl.col("metadata_type_id")),
                           pl.lit(" = "), pl.col("value").fill_null("(removed)")]).alias("detail"),
            "_pending"),
        model.history(_state.view("tag_assignment_log"), document_id=doc_id).select(
            "event_ts", "actor",
            pl.when(pl.col("assigned")).then(pl.lit("tag attached")).otherwise(pl.lit("tag detached")).alias("event"),
            pl.col("tag_id").replace_strict(tag_names, default=pl.col("tag_id")).alias("detail"),
            "_pending"),
        model.history(_state.view("document_file_log"), document_id=doc_id).select(
            "event_ts", "actor",
            pl.when(pl.col("active")).then(pl.lit("file")).otherwise(pl.lit("file removed")).alias("event"),
            pl.col("filename").alias("detail"), "_pending"),
    ]
    hist = pl.concat(parts, how="vertical_relaxed").sort("event_ts", descending=True)
    st.dataframe(
        hist.with_columns(
            pl.when(pl.col("_pending")).then(pl.lit("● unsaved")).otherwise(pl.lit("")).alias("status")
        ).select("status", "event_ts", "actor", "event", "detail"),
        hide_index=True,
        column_config={"event_ts": st.column_config.DatetimeColumn("When (UTC)",
                                                                   format="YYYY-MM-DD HH:mm:ss")},
    )
