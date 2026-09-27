import time

import polars as pl
import pyarrow
import streamlit as st

from app_pages import _state
from core import read
from core.schema import KEYS, READ_TABLES, TOKEN_TABLES
from core.session import get_session, runtime, slug, user_tokens

st.title("Diagnostics")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Viewer (actor)", _state.user())
c2.metric("Session folder", f"@sessions/{slug(_state.user())}/")
c3.metric("Runtime", runtime())
c4.metric("polars / pyarrow", f"{pl.__version__} / {pyarrow.__version__}")

st.subheader("Identity")
st.caption("In Streamlit in Snowflake the actor must be the *viewer* (st.user), not the app "
           "owner. Open the app as a second user: the actor above must differ.")
ctx = get_session().sql(
    "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA()"
).collect()[0]
st.json({
    "st.user": user_tokens(),
    "CURRENT_USER() (session owner)": ctx[0],
    "role": ctx[1], "warehouse": ctx[2], "database": ctx[3], "schema": ctx[4],
    "streamlit": st.__version__,
})

st.subheader("Change tokens")
st.caption("One probe per rerun; the loaders are keyed on these values.")
st.json(_state.tokens())

st.subheader("Tables")
tokens = _state.tokens()
rows = []
for t in TOKEN_TABLES:
    if t in READ_TABLES:
        start = time.perf_counter()
        df = read.load_log(t, tokens[t])
        ms = (time.perf_counter() - start) * 1000
        rows.append({"table": t, "raw_rows": df.height,
                     "reduced_rows": read.latest(df, KEYS[t]).height,
                     "load_ms (cached)": round(ms, 1), "pending": _state.appends(t).height})
    else:
        ids = read.load_event_ids(t, tokens[t])
        rows.append({"table": t, "raw_rows": ids.height, "reduced_rows": None,
                     "load_ms (cached)": None, "pending": _state.appends(t).height})
st.dataframe(pl.DataFrame(rows), hide_index=True)

st.subheader("Probe latency")
if st.button("Time change_tokens() × 20"):
    ms = read.time_probe(20)
    st.metric("Average per call", f"{ms:.0f} ms",
              help="The per-interaction latency floor. Above ~300 ms, consider caching the "
                   "probe with a short TTL (architecture §9.6).")

st.subheader("Session state (tier 1)")
st.caption("Must contain only this session's appends — never committed rows.")
st.json(_state.debug_state())
