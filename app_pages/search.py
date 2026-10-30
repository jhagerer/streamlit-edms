import polars as pl
import streamlit as st

from app_pages import _state, _ui
from core import model, read, search

st.title("Search")
st.caption("Semantic and keyword search over every page — its summary, headings and image "
           "and table summaries — served by the Cortex Search service `document_search`, the "
           "sink of the ingest pipeline. Results are ranked by their best page; type and tag "
           "filters apply afterwards.")

query = st.text_input("Search", key="search:q",
                      placeholder="e.g. “invoices from ACME over 500 EUR” or a phrase")
f = _ui.filter_bar("search", show_text=False)

docs = _state.document_table()
if query.strip():
    _, _, database, schema = read.scope_of(_state.tokens()).split("/")
    try:
        with st.spinner("Searching…"):
            hits = search.search(query, database, schema)
    except Exception as exc:
        st.error(f"Search failed: {exc}")
        st.stop()
    active = set(_state.files()["file_id"].to_list())
    ids = search.ranked_document_ids(hits, active)
    snippets = search.best_snippets(hits)
    shown = model.filter_documents(docs, "", f["document_type_ids"], f["tag_ids"], ids=ids)
    order = {d: i for i, d in enumerate(ids)}
    shown = (shown.with_columns(
                 pl.col("document_id").replace_strict(order, default=len(order)).alias("_rank"),
                 pl.col("document_id").replace_strict(snippets, default="").alias("match"))
             .sort("_rank").drop("_rank"))
    _ui.document_list(shown, "search", empty_text="No matching documents.", extra=["match"])
else:
    shown = model.filter_documents(docs, "", f["document_type_ids"], f["tag_ids"])
    _ui.document_list(shown, "search", empty_text="No documents.")

not_yet = _state.files().filter(pl.col("ai_state").is_in(["queued", "missing"]))
st.caption("New documents become searchable once the AI has processed them and the search "
           "service has refreshed (target lag: 1 minute).")
if not_yet.height:
    st.info(f"{not_yet['document_id'].n_unique()} document(s) are not searchable yet: "
            "Snowflake is still processing them.", icon="ℹ️")
