"""Shared UI pieces: sidebar, notices, document list, lookups."""

from __future__ import annotations

import polars as pl
import streamlit as st

from app_pages import _state
from core import model
from core.session import auth_configured, connection_source

DOCUMENT_PAGE = "app_pages/document.py"
SETUP_PAGE = "app_pages/setup.py"

SAVE_COPY = (
    "Every change is saved to Snowflake immediately — there is no separate save step. "
    "After an upload, Snowflake runs OCR, a summary, type and tag suggestions and "
    "metadata extraction with AI_COMPLETE; the results appear on the document shortly after."
)

AI_BADGES = {"queued": "⏳ AI running", "failed": "⚠️ AI failed", "missing": "⚠️ not processed",
             "done": "✓", None: ""}


# ── Sidebar ──────────────────────────────────────────────────────────────────


def sidebar() -> None:
    with st.sidebar:
        st.caption(f"Signed in as **{_state.user()}**"
                   + ("  \nSnowflake: your own credentials" if connection_source() == "own" else ""))
        if auth_configured():
            st.button("Log out", on_click=st.logout)
        try:
            queued = _state.files().filter(pl.col("ai_state") == "queued").height
        except Exception:
            queued = 0
        if queued:
            st.info(f"AI is processing {queued} file(s) in Snowflake.", icon="⏳")
            if st.button("Refresh", help="Reload to see finished AI results."):
                st.rerun()


def show_notices() -> None:
    for kind, msg in _state.take_flashes():
        getattr(st, kind if kind in ("success", "info", "warning", "error") else "info")(msg)


def warehouse_gate() -> bool:
    """Any warehouse will do — but there must be one. Shows an error and
    returns False when the session has no warehouse."""
    name, _ = _state.warehouse()
    if name:
        return True
    st.error("No warehouse is in use. Set one in the connection (or the Streamlit app's query "
             "warehouse) — or let the Setup page create `minidms_wh`.")
    st.page_link(SETUP_PAGE, label="Open the Setup page", icon="⚙️")
    return False


# ── Lookups ──────────────────────────────────────────────────────────────────


def document_types(include_inactive: bool = False) -> pl.DataFrame:
    return model.definitions(_state.table("document_type_current"), include_inactive)


def metadata_types(include_inactive: bool = False) -> pl.DataFrame:
    return model.definitions(_state.table("metadata_type_current"), include_inactive)


def tags(include_inactive: bool = False) -> pl.DataFrame:
    return model.definitions(_state.table("tag_current"), include_inactive)


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
                              placeholder="label, description, filename, metadata, summary…") if show_text else ""
    return {"document_type_ids": type_ids, "tag_ids": tag_ids, "text": text}


def document_list(docs: pl.DataFrame, key: str, empty_text: str = "No documents.",
                  extra: list[str] | None = None) -> str | None:
    """Render documents with single-row selection; returns the selected id.
    ``extra`` columns (e.g. the search match) are shown after the label."""
    if not docs.height:
        st.info(empty_text)
        return None
    shown = docs.select(
        pl.col("ai_state").replace_strict(AI_BADGES, default="").alias("ai"),
        "label",
        *(extra or []),
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
            "ai": st.column_config.TextColumn("AI", width="small"),
            "label": st.column_config.TextColumn("Label", width="large"),
            "match": st.column_config.TextColumn("Best match", width="large"),
            "document_type": "Type",
            "tags": st.column_config.ListColumn("Tags"),
            "files": st.column_config.NumberColumn("Files", width="small"),
            "updated_at": st.column_config.DatetimeColumn("Updated (UTC)", format="YYYY-MM-DD HH:mm"),
            "updated_by": "By",
            "document_id": None,
        },
    )
    st.caption(f"{docs.height} document(s).")
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
