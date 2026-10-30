import time

import polars as pl
import pyarrow
import streamlit as st

from app_pages import _state
from core import db, read
from core.schema import LOADED
from core.session import runtime, user_tokens

st.title("Diagnostics")

wh_name, wh_type = _state.warehouse()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Viewer (actor)", _state.user())
c2.metric("Warehouse", f"{wh_name or '—'} ({wh_type or '?'})")
c3.metric("Runtime", runtime())
c4.metric("polars / pyarrow", f"{pl.__version__} / {pyarrow.__version__}")

st.subheader("Identity")
st.caption("In Streamlit in Snowflake the actor must be the *viewer* (st.user), not the app "
           "owner. Open the app as a second user: the actor above must differ.")
ctx = db.query(
    "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA()"
)[0]
st.json({
    "st.user": user_tokens(),
    "CURRENT_USER() (session owner)": ctx[0],
    "role": ctx[1], "warehouse": ctx[2], "database": ctx[3], "schema": ctx[4],
    "streamlit": st.__version__,
})

st.subheader("Change tokens")
st.caption("One query per rerun: the row count of every table. All tables are append-only, so "
           "a count changes with every commit and never shrinks; the cached loads are keyed on it.")
st.json(_state.tokens())

st.subheader("Cached tables")
rows = []
for name in LOADED:
    start = time.perf_counter()
    df = _state.table(name)
    rows.append({"table": name, "rows": df.height,
                 "load_ms (cached)": round((time.perf_counter() - start) * 1000, 1),
                 "depends_on": ", ".join(LOADED[name])})
st.dataframe(pl.DataFrame(rows), hide_index=True)

st.subheader("Probe latency")
if st.button("Time change_tokens() × 20"):
    ms = read.time_probe(20)
    st.metric("Average per call", f"{ms:.0f} ms",
              help="The per-interaction latency floor (row counts of all tables).")

st.subheader("Session state")
st.caption("No data rows live in session state — the one global state is in Snowflake.")
st.json(_state.debug_state())
