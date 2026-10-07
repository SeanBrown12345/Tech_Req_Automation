"""ReqFill dashboard. Run: streamlit run app/streamlit_app.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app.admin_layout import admin_layout  # noqa: E402
from app.header import APP_NAME, render_header  # noqa: E402

st.set_page_config(page_title=APP_NAME, page_icon=str(Path(__file__).parent / "assets" / "favicon.png"), layout="wide")

views = Path(__file__).parent / "views"
draft = st.Page(views / "draft_worksheet.py", title="Draft a worksheet", icon=":material/edit_note:", default=True)
board = st.Page(views / "task_board.py", title="Task board", icon=":material/assignment_ind:", url_path="tasks")
# Admin, reached from the gear in the header: the knowledge base, and the team that reviews drafts.
kb_admin = [
    st.Page(views / "overview.py", title="Overview", icon=":material/dashboard:", url_path="overview"),
    st.Page(views / "search.py", title="Search", icon=":material/search:", url_path="search"),
    st.Page(views / "add_worksheet.py", title="Add worksheet", icon=":material/upload_file:", url_path="add_worksheet"),
    st.Page(views / "conflicts.py", title="Conflicts", icon=":material/compare_arrows:", url_path="conflicts"),
]
team_admin = [st.Page(views / "smes.py", title="SMEs", icon=":material/group:", url_path="smes")]
admin = kb_admin + team_admin
page = st.navigation([draft, board, *admin], position="hidden")
in_admin = page.url_path in {p.url_path for p in admin}
render_header(draft, board, admin[0], in_admin)

# No sidebar anywhere: admin navigation lives in the page itself (see admin_layout).
st.html("<style>section[data-testid='stSidebar'], [data-testid='stExpandSidebarButton'],"
        " [data-testid='stSidebarCollapsedControl'] { display: none !important; }</style>")

if in_admin:
    with admin_layout({"KNOWLEDGE BASE": kb_admin, "TEAM": team_admin}, page):
        page.run()
else:
    page.run()
