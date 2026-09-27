"""Shared UI pieces: sidebar, restore dialog, document list, lookups."""

from __future__ import annotations

import polars as pl
import streamlit as st

from app_pages import _state
from core import model
from core.session import auth_configured, connection_source
from core.schema import APPEND_TABLES

DOCUMENT_PAGE = "app_pages/document.py"

SAVE_COPY = (
    "Files upload immediately; entries are saved when you click "
    "**Save to database**. Text (OCR) is extracted in Snowflake after that, "
    "usually within a minute. **Save to session** keeps your unsaved work safe "
    "if the browser closes."
)


# ── Sidebar ──────────────────────────────────────────────────────────────────


def sidebar() -> None:
    counts = _state.pending_counts()
    with st.sidebar:
        st.caption(f"Signed in as **{_state.user()}**"
                   + ("  \nSnowflake: your own credentials" if connection_source() == "own" else ""))
        if auth_configured():
            st.button("Log out", on_click=st.logout)
        total = sum(counts.values())
        if total:
            if _state.unsaved_to_session():
                st.warning(f"{total} unsaved change(s) — not yet saved to session.", icon="⚠️")
            else:
                st.info(f"{total} change(s) saved to session, not yet to database.", icon="💾")
            with st.expander("Pending rows"):
                for t in APPEND_TABLES:
                    if counts.get(t):
                        st.write(f"`{t}`: {counts[t]}")
        else:
            st.success("All changes saved.", icon="✅")

        c1, c2 = st.columns(2)
        if c1.button("Save to session", disabled=not total,
                     help="Write your unsaved changes to your personal session folder."):
            try:
                n = _state.save_to_session()
                _state.flash(f"Saved {n} table(s) to your session.")
            except Exception as exc:
                _state.flash(f"Save to session failed: {exc}", "error")
            st.rerun()
        if c2.button("Save to database", type="primary", disabled=not total,
                     help="Commit your changes for everyone."):
            try:
                with st.spinner("Saving to database…"):
                    counts_done = _state.save_to_database()
                _state.flash(f"Saved {sum(counts_done.values())} row(s) to the database.")
            except Exception as exc:
                _state.flash(f"Save to database failed: {exc}", "error")
            st.rerun()
        if total and st.button("Discard changes"):
            confirm_discard()


@st.dialog("Discard all unsaved changes?")
def confirm_discard() -> None:
    st.write(
        "This removes your pending changes from this browser session and from your "
        "session folder. Files you already uploaded stay in the stage as orphans "
        "(an admin can clean them up)."
    )
    c1, c2 = st.columns(2)
    if c1.button("Discard", type="primary"):
        _state.discard_session()
        _state.flash("Changes discarded.", "info")
        st.rerun()
    if c2.button("Cancel"):
        st.rerun()


@st.dialog("Previous session restored")
def restore_notice(restored: dict[str, int]) -> None:
    st.success(f"{sum(restored.values())} unsaved change(s) restored from your last session.")
    for table, n in restored.items():
        st.write(f"- `{table}`: {n} row(s)")
    c1, c2 = st.columns(2)
    if c1.button("Continue", type="primary"):
        st.rerun()
    if c2.button("Discard restored changes"):
        _state.discard_session()
        st.rerun()


def show_notices() -> None:
    for kind, msg in _state.take_flashes():
        getattr(st, kind if kind in ("success", "info", "warning", "error") else "info")(msg)
    report = _state.take_restore_report()
    if report:
        restore_notice(report)


# ── Lookups over the composed frames ─────────────────────────────────────────


def document_table() -> pl.DataFrame:
    return model.document_table(
        _state.view("document_log"),
        _state.view("document_type_log"),
        _state.view("document_file_log"),
        _state.view("metadata_log"),
        _state.view("tag_assignment_log"),
        _state.view("tag_log"),
    )


def file_status(include_inactive: bool = False) -> pl.DataFrame:
    """Current files with their text-extraction status."""
    return model.file_text_status(
        model.current_files(_state.view("document_file_log"), include_inactive),
        _state.ocr_status(),
    )


def document_types(include_inactive: bool = False) -> pl.DataFrame:
    return model.definitions(_state.view("document_type_log"), "document_type_log", include_inactive)


def metadata_types(include_inactive: bool = False) -> pl.DataFrame:
    return model.definitions(_state.view("metadata_type_log"), "metadata_type_log", include_inactive)


def tags(include_inactive: bool = False) -> pl.DataFrame:
    return model.definitions(_state.view("tag_log"), "tag_log", include_inactive)


def label_map(df: pl.DataFrame, key: str) -> dict[str, str]:
    return {r[key]: (r["label"] or r[key]) for r in df.iter_rows(named=True)}


# ── Document list ────────────────────────────────────────────────────────────


def filter_bar(key: str, show_text: bool = True) -> dict:
    """Type / tag / text filters. Returns the chosen values."""
    types, tag_defs = document_types(), tags()
    type_labels, tag_labels = label_map(types, "document_type_id"), label_map(tag_defs, "tag_id")
    cols = st.columns([2, 2, 3] if show_text else [1, 1])
    type_ids = cols[0].multiselect("Document type", list(type_labels),
                                   format_func=type_labels.get, key=f"{key}:types")
    tag_ids = cols[1].multiselect("Tags (all of)", list(tag_labels),
                                  format_func=tag_labels.get, key=f"{key}:tags")
    text = cols[2].text_input("Filter", key=f"{key}:text",
                              placeholder="label, description, filename, metadata…") if show_text else ""
    return {"document_type_ids": type_ids, "tag_ids": tag_ids, "text": text}


def document_list(docs: pl.DataFrame, key: str, empty_text: str = "No documents.") -> str | None:
    """Render documents with single-row selection; returns the selected id."""
    if not docs.height:
        st.info(empty_text)
        return None
    shown = docs.select(
        pl.when(pl.col("pending")).then(pl.lit("● unsaved")).otherwise(pl.lit("")).alias("status"),
        "label",
        pl.col("document_type").fill_null("—"),
        "tags",
        "files",
        pl.col("updated_at"),
        pl.col("updated_by"),
        "document_id",
    )
    event = st.dataframe(
        shown,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"{key}:grid",
        column_config={
            "status": st.column_config.TextColumn("", width="small"),
            "label": st.column_config.TextColumn("Label", width="large"),
            "document_type": "Type",
            "tags": st.column_config.ListColumn("Tags"),
            "files": st.column_config.NumberColumn("Files", width="small"),
            "updated_at": st.column_config.DatetimeColumn("Updated (UTC)", format="YYYY-MM-DD HH:mm"),
            "updated_by": "By",
            "document_id": None,
        },
    )
    st.caption(f"{docs.height} document(s). Rows marked ● have unsaved changes.")
    rows = event.selection.rows if event is not None else []
    if rows:
        doc_id = shown["document_id"][rows[0]]
        if st.button("Open selected document", type="primary", key=f"{key}:open"):
            open_document(doc_id)
        return doc_id
    return None


def open_document(doc_id: str) -> None:
    st.switch_page(DOCUMENT_PAGE, query_params={"doc": doc_id})


def fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── Metadata inputs ──────────────────────────────────────────────────────────


def metadata_input(mt: dict, key: str, current: str | None = None) -> str:
    """One widget per metadata type, returning the value as a string
    ('' = no value). ``key`` must be namespaced by object id."""
    import datetime as _dt

    label = mt["label"] or mt["name"] or mt["metadata_type_id"]
    value = current if current is not None else (mt["default_value"] or "")
    dtype = mt["data_type"] or "text"
    if dtype == "choice":
        options = [""] + [c.strip() for c in (mt["choices"] or "").splitlines() if c.strip()]
        if value and value not in options:
            options.append(value)
        return st.selectbox(label, options, index=options.index(value), key=key)
    if dtype == "date":
        try:
            parsed = _dt.date.fromisoformat(value) if value else None
        except ValueError:
            parsed = None
        picked = st.date_input(label, value=parsed, key=key)
        return picked.isoformat() if isinstance(picked, _dt.date) else ""
    if dtype == "number":
        raw = st.text_input(label, value=value, key=key, help="A number")
        if raw.strip():
            try:
                float(raw)
            except ValueError:
                st.warning(f"“{label}” must be a number.")
        return raw.strip()
    return st.text_input(label, value=value, key=key).strip()


def invalid_numbers(values: dict[str, str], mtypes: dict[str, dict]) -> list[str]:
    bad = []
    for mt_id, v in values.items():
        if v and (mtypes[mt_id]["data_type"] == "number"):
            try:
                float(v)
            except ValueError:
                bad.append(mtypes[mt_id]["label"] or mt_id)
    return bad
