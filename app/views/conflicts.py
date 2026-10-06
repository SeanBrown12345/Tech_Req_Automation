import pandas as pd
import streamlit as st

from app.common import connect, db_version, embedder, status_label
from kb import analysis

st.title("Conflicts")

if analysis.sources(connect()).empty:
    st.info("The knowledge base is empty. Add a completed worksheet on the **Add worksheet** page.")
    st.stop()

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
