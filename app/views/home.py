"""Home: two ways in, the task board (SMEs reviewing their rows) and a new worksheet (the bid team drafting one).
Below, every draft out for review and who it waits on."""

from datetime import date

import pandas as pd
import streamlit as st

from app.common import card_styles
from app.header import BANNER_COLOR
from kb import draft

st.html("""<style>
[data-testid='stMainBlockContainer'] { max-width: 1200px; margin: 0 auto; }
[data-testid="stMain"] [data-testid="stHeading"] h1 {
    padding-bottom: 0.6rem; border-bottom: 1px solid rgba(128, 128, 128, 0.25); margin-bottom: 0.5rem;
}
[data-testid="stMain"] [data-testid="stHeading"] h3 {
    padding: 0 0 0.6rem; border-bottom: 1px solid rgba(128, 128, 128, 0.25); margin: 0.75rem 0 0.5rem;
}
/* The two ways in take the banner's maroon in place of the theme blue, as the task board's buttons do. */
.st-key-card-board [data-testid="stBaseButton-primary"], .st-key-card-new [data-testid="stBaseButton-primary"] {
    background: BANNER; border-color: BANNER; color: #fff;
}
.st-key-card-board [data-testid="stBaseButton-primary"]:hover, .st-key-card-new [data-testid="stBaseButton-primary"]:hover {
    background: #A3214A; border-color: #A3214A; color: #fff;
}
.st-key-card-board [data-testid="stBaseButton-primary"]:active, .st-key-card-new [data-testid="stBaseButton-primary"]:active,
.st-key-card-board [data-testid="stBaseButton-primary"]:focus, .st-key-card-new [data-testid="stBaseButton-primary"]:focus {
    color: #fff;
}
</style>""".replace("BANNER", BANNER_COLOR))
card_styles()

# Leaving the page happens here, not in the buttons' callbacks (switch_page can't run inside one).
if go := st.session_state.pop("home_go", None):
    if go == "board":
        st.switch_page("views/task_board.py")
    if go == "new":
        st.session_state.pop("draft_job", None)  # the draft page falls back to New draft
    else:
        st.session_state.draft_job_next = go
    st.switch_page("views/draft_worksheet.py")


def _go(target: str) -> None:
    """'board', 'new', or a draft's job_id."""
    st.session_state.home_go = target


def _due(due: str | None) -> str:
    if not due:
        return ""
    d = date.fromisoformat(due)
    return f", due {d:%a} {d.day} {d:%b}" + (" · **past due**" if d < date.today() else "")


def _title(job) -> str:
    return f"{job.name} — {job.client}" if isinstance(job.client, str) and job.client.strip() else job.name


st.title("ReqFill")

board, new = st.columns(2, gap="medium")
with board.container(border=True, key="card-board"):
    st.subheader(":material/assignment_ind: Task Board")
    st.caption("For SMEs: review and approve the rows assigned to you.")
    st.button("Open the task board", type="primary", width="stretch", on_click=_go, args=("board",))
with new.container(border=True, key="card-new"):
    st.subheader(":material/edit_note: New Worksheet")
    st.caption("For the bid team: upload a client's worksheet to have it drafted.")
    st.button("Start a new worksheet", type="primary", width="stretch", on_click=_go, args=("new",))

# ---- Drafts out for review ---------------------------------------------------------------------
jobs = draft.job_progress()
work = draft.sme_workload()
st.subheader("Drafts in review")
out = jobs[jobs.assigned_open > 0]
if out.empty:
    st.caption("No drafts are out for review. Assign a draft's rows from its page to send them to SMEs.")
for job in out.itertuples():
    with st.container(border=True):
        title, action = st.columns([4, 1], vertical_alignment="center")
        title.markdown(f"**{_title(job)}**")
        action.button("Open draft", key=f"out:{job.job_id}", width="stretch", on_click=_go, args=(job.job_id,))
        st.progress(job.approved / job.rows, text=f"{job.approved:,} of {job.rows:,} approved")
        theirs: pd.DataFrame = work[work.job_id == job.job_id]
        who = [f"{r.person} ({int(r.open):,}{_due(r.due)})" for r in theirs.itertuples()]
        if partner_rows := job.assigned_open - int(theirs.open.sum()):
            who.append(f"partners ({partner_rows:,})")
        st.caption("Waiting on " + ", ".join(who))
