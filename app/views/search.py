import json

import streamlit as st

from app.common import STATUS_LABELS, connect, embedder, status_badge, status_help, status_label
from kb import analysis, store

st.title("Search past answers")
st.caption("Paste a requirement from a new RFP to see how we've answered similar ones before.")

conn = connect()
options = analysis.filter_options(conn)

query = st.text_area("Requirement or keywords", height=90,
                     placeholder="e.g. The system shall support single sign-on via SAML 2.0 for staff users")
f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
statuses = f1.multiselect("Status", options["statuses"], format_func=status_label, help=status_help())
clients = f2.multiselect("Client", options["clients"])
modules = f3.multiselect("Module", options["modules"])
limit = f4.number_input("Results", 5, 50, 10, step=5)
mode = st.segmented_control(
    "Match by", ["hybrid", "semantic", "keyword"], default="hybrid", format_func=str.title,
    help="**Hybrid** combines both. **Semantic** matches meaning (finds rewordings). "
         "**Keyword** matches exact terms (best for acronyms and product names).") or "hybrid"

if not query.strip():
    st.stop()

with st.spinner("Searching..."):
    results = store.search(conn, query, int(limit), mode=mode, embedder=embedder(),
                           filters={"statuses": statuses, "clients": clients, "modules": modules})

if not results:
    st.warning("No matches. Try fewer filters or the **Semantic** mode.")
    st.stop()

st.write(f"Top {len(results)} matches")
for r in results:
    with st.container(border=True):
        head, meta = st.columns([1, 4])
        with head:
            status_badge(r["status"])
        with meta:
            bits = [r["client"], r["submitted"] or "date unknown", r["sheet"], r["req_id"]]
            st.caption(" · ".join(b for b in bits if b))
        if r["parent_text"]:
            st.caption(f"Under: {r['parent_text']}")
        st.markdown(f"**{r['requirement']}**")
        if r["comment"]:
            st.write(r["comment"])
        else:
            st.caption("_No comment was given with this answer._")
        with st.expander("Details"):
            raw = json.loads(r["answer_raw"])
            attributes = json.loads(r["attributes"])
            st.write({
                "Answer as written": raw or "(none)",
                "Normalized status": STATUS_LABELS.get(r["status"], r["status"]),
                "Module": r["module"],
                "Section": r["section"],
                "Other columns": attributes or "(none)",
                "Similarity": round(r["similarity"], 3) if r["similarity"] is not None else "n/a",
                "Found by": " + ".join(r["found_by"]),
                "Record": r["record_id"],
            })
