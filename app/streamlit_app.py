"""ReqFill dashboard. Run: streamlit run app/streamlit_app.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app.header import APP_NAME, render_header  # noqa: E402

st.set_page_config(page_title=APP_NAME, page_icon=":material/fact_check:", layout="wide")

views = Path(__file__).parent / "views"
draft = st.Page(views / "draft_worksheet.py", title="Draft a worksheet", icon=":material/edit_note:", default=True)
# Knowledge-base admin, reached from the gear in the header.
admin = [
    st.Page(views / "overview.py", title="Overview", icon=":material/dashboard:", url_path="overview"),
    st.Page(views / "search.py", title="Search", icon=":material/search:", url_path="search"),
    st.Page(views / "add_worksheet.py", title="Add worksheet", icon=":material/upload_file:", url_path="add_worksheet"),
]
page = st.navigation([draft, *admin], position="hidden")
render_header(draft, admin[0])

# Only the admin pages get a sidebar.
if page.url_path in {p.url_path for p in admin}:
    with st.sidebar:
        st.caption("KNOWLEDGE BASE ADMIN")
        for p in admin:
            st.page_link(p)
        st.divider()
        st.page_link(draft, label="Back to drafting", icon=":material/arrow_back:")
else:
    # Coming back from an admin page leaves an empty sidebar shell behind; hide it and its toggle.
    st.html("<style>section[data-testid='stSidebar'], [data-testid='stExpandSidebarButton'],"
            " [data-testid='stSidebarCollapsedControl'] { display: none !important; }</style>")

page.run()
