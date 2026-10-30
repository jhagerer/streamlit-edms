"""Document detail. Every widget key is namespaced by document id so switching
documents never shows the previous one's draft.

Every change is written to Snowflake immediately and checked against the
version the page was rendered with: if someone else changed the same thing
first, nothing is written and the user sees a conflict notice."""

import json

import polars as pl
import streamlit as st

from app_pages import _state, _ui
from core import files, model, read, write

docs = _state.document_table()

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
k = f"doc:{doc_id}"  # widget key namespace (stable, so clicks survive a reload)

types = _ui.document_types(include_inactive=True)
type_labels = _ui.label_map(types, "document_type_id")
active_types = _ui.label_map(_ui.document_types(), "document_type_id")
tag_defs_all = _ui.tags(include_inactive=True)
tag_labels_all = _ui.label_map(tag_defs_all, "tag_id")
mtypes_all = _ui.metadata_types(include_inactive=True)
mt_all = {r["metadata_type_id"]: r for r in mtypes_all.iter_rows(named=True)}
current_meta = {r["metadata_type_id"]: r for r in _state.table("document_metadata")
                .filter(pl.col("document_id") == doc_id).iter_rows(named=True)}
doc_files = _state.files().filter(pl.col("document_id") == doc_id)

# What the user saw when the page was drawn — the basis of every conflict check
# (a click reruns the script, which has already reloaded the latest data).
seen_doc = {**doc, "version": _state.rendered(f"doc:{doc_id}", doc["version"])}
# Inputs with drafts: what each input started from, until it is saved or reset.
seen_meta = {m: _state.baseline(f"{k}:meta:{m}",
                                (r["version"], r["value"]) if r else (None, ""))
             for m, r in {**{m: None for m in mt_all}, **current_meta}.items()}
seen_tags = set(_state.baseline(f"{k}:tags", tuple(sorted(doc["tag_ids"]))))
seen_files = {f["file_id"]: {**f, "version": _state.rendered(f"file:{f['file_id']}",
                                                             f["version"])}
              for f in doc_files.iter_rows(named=True)}

st.page_link("app_pages/documents.py", label="← All documents")
st.title(("🗑️ " if doc["trashed"] else "") + (doc["label"] or "(no label)"))
if doc["trashed"]:
    st.warning("This document is in the trash.")
if doc["summary"]:
    st.info(doc["summary"], icon="🤖")

tab_props, tab_meta, tab_tags, tab_files, tab_ai, tab_hist = st.tabs(
    ["Properties", "Metadata", "Tags", "Files", "Text & AI", "History"]
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
    if st.button("Save", type="primary", key=f"{k}:apply"):
        changes = {"label": label.strip() or None, "document_type_id": type_id,
                   "description": description or None, "language": language.strip() or None}
        changes = {f: v for f, v in changes.items() if v != seen_doc[f]}
        if changes:
            _state.act(write.update_document, user, seen_doc, changes, success="Saved.",
                       reset=f"{k}:")
        st.rerun()


with tab_props:
    c1, c2 = st.columns(2)
    c1.markdown(f"**Type:** {type_labels.get(doc['document_type_id'], '—')}")
    c1.markdown(f"**Language:** {doc['language'] or '—'}")
    c1.markdown(f"**Tags:** {', '.join(doc['tags']) or '—'}")
    c2.markdown(f"**Created:** {doc['created_at']:%Y-%m-%d %H:%M} UTC by {doc['created_by']}")
    c2.markdown(f"**Updated:** {doc['updated_at']:%Y-%m-%d %H:%M} UTC by {doc['updated_by']}")
    c2.markdown(f"**Id:** `{doc_id}` · version {doc['version']}")
    st.markdown("**Description**")
    st.write(doc["description"] or "—")
    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("Edit", key=f"{k}:edit"):
        edit_properties()
    if doc["trashed"]:
        if b2.button("Restore", key=f"{k}:restore"):
            _state.act(write.update_document, user, seen_doc, {}, op="restore",
                       success="Restored.")
            st.rerun()
    elif b2.button("Move to trash", key=f"{k}:trash"):
        _state.act(write.update_document, user, seen_doc, {}, op="trash",
                   success="Moved to trash.")
        st.rerun()

# ── Metadata ─────────────────────────────────────────────────────────────────

with tab_meta:
    active_ids = [r["metadata_type_id"] for r in _ui.metadata_types().iter_rows(named=True)]
    shown_ids = list(dict.fromkeys(active_ids + [m for m in current_meta if m in mt_all]))
    if not shown_ids:
        st.info("No metadata types defined yet — create some on the Metadata types page.")
    else:
        mk = f"{k}:meta"
        with st.form(mk):
            values = {}
            cols = st.columns(2)
            for i, mt_id in enumerate(shown_ids):
                with cols[i % 2]:
                    values[mt_id] = _ui.metadata_input(
                        {**mt_all[mt_id], "default_value": None}, key=f"{mk}:{mt_id}",
                        current=(current_meta.get(mt_id) or {}).get("value", ""),
                    )
            if st.form_submit_button("Save metadata"):
                bad = _ui.invalid_numbers(values, mt_all)
                if bad:
                    st.error(f"Not a number: {', '.join(bad)}")
                else:
                    n = 0
                    for mt_id, v in values.items():
                        version, value = seen_meta.get(mt_id, (None, ""))
                        if v != value:  # only what this user changed
                            n += _state.act(write.set_metadata, user, doc_id, mt_id, v, version,
                                            reset=f"{mk}:")
                    if n:
                        _state.flash(f"{n} field(s) saved.")
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
        tk = f"{k}:tags"
        chosen = st.multiselect("Tags", list(tag_labels), default=attached,
                                format_func=tag_labels.get, key=tk)
        if st.button("Save tags", key=f"{tk}:apply"):
            _state.act(write.set_tags, user, doc_id,
                       [t for t in chosen if t not in seen_tags],
                       [t for t in seen_tags if t not in chosen and t in tag_labels],
                       success="Tags saved.", reset=tk)
            st.rerun()

# ── Files ────────────────────────────────────────────────────────────────────

with tab_files:
    if not doc_files.height:
        st.info("No files.")
    for f in doc_files.iter_rows(named=True):
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 1, 1])
            c1.markdown(f"**{f['filename'] or f['file_id']}**")
            c1.caption(
                f"{f['mimetype'] or '?'} · {_ui.fmt_size(f['size'])} · "
                f"sha256 `{(f['checksum'] or '')[:16]}…`  \n"
                f"added {f['created_at']:%Y-%m-%d %H:%M} UTC by {f['created_by']} · "
                f"AI: {f['ai_state']}"
            )
            fk = f"doc:{doc_id}:file:{f['file_id']}"
            if f["stage_path"] and c2.button("Download", key=f"{fk}:prep"):
                try:
                    data = files.file_bytes(f["stage_path"], read.scope_of(_state.tokens()))
                    c2.download_button("Save file", data, file_name=f["filename"],
                                       mime=f["mimetype"], key=f"{fk}:dl")
                except Exception as exc:
                    st.error(f"Download failed: {exc}")
            if c3.button("Remove", key=f"{fk}:rm", help="Detaches the file; the bytes are kept."):
                _state.act(write.set_file_active, user, seen_files[f["file_id"]], False,
                           success="File removed.")
                st.rerun()

    with st.expander("Add a file to this document"):
        up = st.file_uploader("File", key=f"doc:{doc_id}:addfile")
        if up is not None and st.button("Upload file", key=f"doc:{doc_id}:addfile:go"):
            with st.spinner("Uploading…"):
                try:
                    stored = files.stage_upload(up.getvalue(), up.name, up.type, doc_id)
                except Exception as exc:
                    st.error(f"Upload failed: {exc}")
                    st.stop()
                if _state.act(write.attach_files, user, doc_id, [stored],
                              success="File added; AI processing started."):
                    write.register_files(user, [stored])
            st.rerun()

# ── Text & AI ────────────────────────────────────────────────────────────────

with tab_ai:
    st.caption("After an upload, Snowflake runs AI_COMPLETE (claude-sonnet-4-5) for the OCR "
               "text, a summary, type and tag suggestions and metadata extraction. The results "
               "are lineage-linked to the file. Suggestions change nothing until you apply them.")
    texts = read.load_texts(tuple(doc_files["file_id"].to_list()),
                            _state.tokens()["document_text"])
    enrich_all = _state.table("document_enrichment_current")
    active_type_ids = set(active_types)
    active_tag_ids = set(_ui.label_map(_ui.tags(), "tag_id"))
    current_values = {m: r["value"] for m, r in current_meta.items()}
    for f in doc_files.iter_rows(named=True):
        with st.container(border=True):
            st.markdown(f"**{f['filename'] or f['file_id']}**")
            state = f["ai_state"]
            msg = model.AI_STATE_LABELS.get(state, state)
            err = f["text_error"] or f["enrich_error"]
            if state in ("failed", "skipped") and err:
                msg += f" {err}"
            {"failed": st.error, "missing": st.warning}.get(state, st.caption)(msg)

            txt = texts.filter(pl.col("file_id") == f["file_id"])
            if txt.height and txt["status"][0] == "done":
                with st.expander(f"OCR text ({len(txt['content'][0] or '')} characters)"):
                    st.text_area("OCR text", txt["content"][0] or "", height=300, disabled=True,
                                 key=f"doc:{doc_id}:text:{f['file_id']}",
                                 label_visibility="collapsed")

            enr = enrich_all.filter(pl.col("file_id") == f["file_id"])
            enr_row = enr.row(0, named=True) if enr.height else None
            if enr_row and enr_row["status"] == "done":
                if enr_row["summary"]:
                    st.markdown(f"**Summary:** {enr_row['summary']}")
                sug = model.suggestions(enr_row, doc, doc["tag_ids"], current_values,
                                        active_type_ids, active_tag_ids, mt_all)
                source = f"ai:{enr_row['event_id']}"
                sk = f"{k}:sug:{enr_row['event_id']}"
                if model.has_suggestions(sug):
                    st.markdown("**AI suggestions**")
                    if sug["document_type_id"]:
                        c1, c2 = st.columns([4, 1])
                        c1.write(f"Document type: {type_labels.get(sug['document_type_id'])}")
                        if c2.button("Apply", key=f"{sk}:type"):
                            _state.act(write.update_document, user, seen_doc,
                                       {"document_type_id": sug["document_type_id"]},
                                       source=source, success="Document type applied.")
                            st.rerun()
                    if sug["tag_ids"]:
                        c1, c2 = st.columns([4, 1])
                        c1.write("Tags: " + ", ".join(tag_labels_all.get(t, t)
                                                      for t in sug["tag_ids"]))
                        if c2.button("Apply", key=f"{sk}:tags"):
                            _state.act(write.set_tags, user, doc_id, sug["tag_ids"], [],
                                       source=source, success="Tags applied.")
                            st.rerun()
                    for mt_id, value in sug["metadata"].items():
                        c1, c2 = st.columns([4, 1])
                        c1.write(f"{mt_all[mt_id]['label']}: {value}")
                        if c2.button("Apply", key=f"{sk}:meta:{mt_id}"):
                            _state.act(write.set_metadata, user, doc_id, mt_id, value,
                                       seen_meta.get(mt_id, (None, ""))[0], source=source,
                                       success="Value applied.")
                            st.rerun()
                else:
                    st.caption("No suggestions that differ from the document.")
            if state != "queued" and st.button(
                    "Process again", key=f"doc:{doc_id}:ai:{f['file_id']}",
                    help="Runs OCR and enrichment again in Snowflake."):
                _state.act(write.request_ai, user, [(f["file_id"], doc_id)], "retry",
                           success="AI processing requested.")
                st.rerun()

# ── History ──────────────────────────────────────────────────────────────────


def _detail(row: dict) -> str:
    try:
        data = json.loads(row["data"] or "{}")
    except ValueError:
        return row["data"] or ""
    if "metadata_type_id" in data:
        label = (mt_all.get(data["metadata_type_id"]) or {}).get("label", data["metadata_type_id"])
        data = {label: data.get("value", "(removed)"),
                **({"source": data["source"]} if "source" in data else {})}
    if "tag_id" in data:
        data = {"tag": tag_labels_all.get(data["tag_id"], data["tag_id"]),
                **({"source": data["source"]} if "source" in data else {})}
    if "document_type_id" in data:
        data = {**data, "document_type_id": type_labels.get(data["document_type_id"],
                                                             data["document_type_id"])}
    return ", ".join(f"{k}={v}" for k, v in data.items())


with tab_hist:
    hist = read.load_history(doc_id, _state.tokens()["change_log"])
    rows = [{"event_ts": r["event_ts"], "actor": r["actor"],
             "event": f"{r['entity'].replace('document_', '')} {r['op']}",
             "detail": _detail(r)} for r in hist.iter_rows(named=True)]
    for f in doc_files.iter_rows(named=True):
        if f["extracted_at"] is not None:
            rows.append({"event_ts": f["extracted_at"], "actor": "system:ai",
                         "event": f"ocr {f['text_status']}", "detail": f["filename"]})
        if f["enriched_at"] is not None:
            rows.append({"event_ts": f["enriched_at"], "actor": "system:ai",
                         "event": f"enrich {f['enrich_status']}", "detail": f["filename"]})
    if rows:
        st.dataframe(
            pl.DataFrame(rows).sort("event_ts", descending=True), hide_index=True,
            column_config={"event_ts": st.column_config.DatetimeColumn(
                "When (UTC)", format="YYYY-MM-DD HH:mm:ss")},
        )
    else:
        st.info("No history yet.")
