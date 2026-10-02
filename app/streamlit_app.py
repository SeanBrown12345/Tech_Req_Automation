"""ReqFill dashboard. Run: streamlit run app/streamlit_app.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app.header import APP_NAME, render_header  # noqa: E402

st.set_page_config(page_title=APP_NAME, page_icon=":material/fact_check:", layout="wide")
render_header()

views = Path(__file__).parent / "views"
page = st.navigation([
    st.Page(views / "overview.py", title="Overview", icon=":material/dashboard:", default=True),
    st.Page(views / "search.py", title="Search", icon=":material/search:"),
    st.Page(views / "draft_worksheet.py", title="Draft a worksheet", icon=":material/edit_note:"),
    st.Page(views / "add_worksheet.py", title="Add worksheet", icon=":material/upload_file:"),
])
page.run()
