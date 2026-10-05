import altair as alt
import streamlit as st

from app.common import connect, status_help, status_label
from kb import analysis

st.title("Knowledge base overview")

conn = connect()
sources = analysis.sources(conn)
if sources.empty:
    st.info("The knowledge base is empty. Add a completed worksheet on the **Add worksheet** page.")
    st.stop()

# ---- Headline numbers ------------------------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Answered requirements", f"{int(sources.records.sum()):,}")
c2.metric("Worksheets", len(sources))
c3.metric("Clients", sources.client.nunique())
c4.metric("Most recent submission", sources.submitted.dropna().max() or "—")

# ---- Status mix ------------------------------------------------------------------------------
left, right = st.columns([3, 2], gap="large")
with left:
    st.subheader("Answers by status", help=status_help())
    chosen = st.multiselect("Worksheets", sources.source_id, format_func=lambda s: sources.set_index(
        "source_id").loc[s, "worksheet"] or s, placeholder="All worksheets")
    counts = analysis.status_counts(conn, chosen or None)
    counts["label"] = counts.status.map(status_label)
    counts["share"] = counts.records / counts.records.sum()

    dark = getattr(st.context, "theme", None) and st.context.theme.type == "dark"
    bar = alt.Chart(counts).encode(
        y=alt.Y("label:N", sort="-x", title=None),
        x=alt.X("records:Q", title="Requirements", axis=alt.Axis(grid=True, tickCount=5)),
        tooltip=[alt.Tooltip("label:N", title="Status"), alt.Tooltip("records:Q", title="Requirements", format=","),
                 alt.Tooltip("share:Q", title="Share", format=".1%")],
    )
    chart = (bar.mark_bar(color="#3987e5" if dark else "#2a78d6", cornerRadiusEnd=4, height={"band": 0.7})
             + bar.mark_text(align="left", dx=4).encode(text=alt.Text("records:Q", format=",")))
    # Right padding keeps the value label on the longest bar from being clipped.
    st.altair_chart(chart.properties(height=36 * len(counts) + 40, padding={"right": 40}), width="stretch")
    with st.expander("Show as table"):
        st.dataframe(counts[["label", "records", "share"]], hide_index=True, column_config={
            "label": "Status", "records": st.column_config.NumberColumn("Requirements", format="%d"),
            "share": st.column_config.NumberColumn("Share", format="percent")})

with right:
    st.subheader("By module")
    table = analysis.module_status(conn)
    table.columns = [status_label(c) if c != "TOTAL" else "Total" for c in table.columns]
    table.index.name = "Module"
    st.dataframe(table, width="stretch")

# ---- Sources ---------------------------------------------------------------------------------
st.subheader("Loaded worksheets")
st.dataframe(sources, hide_index=True, width="stretch", column_config={
    "source_id": "ID", "client": "Client", "rfp": "RFP", "worksheet": "Worksheet",
    "submitted": "Submitted", "loaded_at": "Loaded (UTC)",
    "records": st.column_config.NumberColumn("Requirements", format="%d")})
