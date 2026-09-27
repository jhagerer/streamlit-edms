import polars as pl
import streamlit as st

from core import read

st.title("Audit trail")
st.caption("Every change is an appended event with actor and timestamp, so the history "
           "*is* the data. This page reads the `audit_v` view (committed events only).")

limit = st.select_slider("Rows", options=[500, 1000, 5000, 20000], value=1000)
try:
    audit = read.load_audit(limit)
except Exception as exc:
    st.error(f"Could not read audit_v: {exc}")
    st.stop()

if not audit.height:
    st.info("No events yet.")
    st.stop()

c1, c2, c3 = st.columns(3)
actors = c1.multiselect("Actor", sorted(audit["actor"].unique().to_list()))
kinds = c2.multiselect("Object type", sorted(audit["object_type"].unique().to_list()))
obj = c3.text_input("Object id contains")
if actors:
    audit = audit.filter(pl.col("actor").is_in(actors))
if kinds:
    audit = audit.filter(pl.col("object_type").is_in(kinds))
if obj.strip():
    audit = audit.filter(pl.col("object_id").str.contains(obj.strip(), literal=True))

st.dataframe(audit, hide_index=True,
             column_config={"event_ts": st.column_config.DatetimeColumn(
                 "When (UTC)", format="YYYY-MM-DD HH:mm:ss")})
st.caption(f"{audit.height} event(s)")
