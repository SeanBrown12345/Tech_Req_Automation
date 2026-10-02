import altair as alt
import pandas as pd
import streamlit as st

from app.common import connect, db_version, embedder, status_help, status_label
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

# ---- Conflicts -------------------------------------------------------------------------------
st.subheader("Conflicting answers")
st.caption("Near-identical requirements from different worksheets whose answers disagree "
           "(e.g. *Standard* in one RFP, *Not supported* in another). Settle these before the AI reuses them. "
           "The more recent answer is shown first.")
threshold = st.slider("Minimum text similarity", 0.85, 1.0, 0.95, 0.01,
                      help="1.0 = identical wording. Lower values surface reworded duplicates, with more false matches.")


@st.cache_data(show_spinner="Comparing requirements...")
def _conflicts(threshold: float, model: str, _version: float) -> pd.DataFrame:
    return analysis.conflicts(connect(), model, threshold)


found = _conflicts(threshold, embedder().model_name, db_version())
if found.empty:
    st.success("No conflicting answers at this similarity level.")
else:
    high = int((found.severity == "high").sum())
    st.write(f"**{len(found)}** conflicting pairs, **{high}** of them yes-vs-no.")
    for _, row in found.iterrows():
        title = (f"{'🔴 Yes vs. no' if row.severity == 'high' else '🟠 Qualified'} · "
                 f"{status_label(row.status_a)} vs. {status_label(row.status_b)} · {row.requirement_a[:90]}")
        with st.expander(title):
            a, b = st.columns(2)
            for col, side in ((a, "a"), (b, "b")):
                with col:
                    st.markdown(f"**{status_label(row[f'status_{side}'])}** — {row[f'source_{side}']}")
                    st.write(row[f"requirement_{side}"])
                    st.caption(row[f"comment_{side}"] or "_No comment_")
            st.caption(f"Similarity {row.similarity:.3f}")
