"""Task board: everyone with rows to review. Each person opens their own view, sees the sheets assigned to
them, and either browses a sheet's rows in a grid or reviews them one at a time.

No sign-in: people find themselves by name. The address carries where they are (?person=, then &draft=
and &sheet= in a review), so a link opens someone's view directly and a refresh keeps the place."""

import json

import pandas as pd
import streamlit as st

from app import review_grid
from app.admin_layout import ACCENT
from app.common import status_badge
from kb import db, draft

st.html(f"""<style>
[data-testid='stMainBlockContainer'] {{ max-width: 1200px; margin: 0 auto; }}
[data-testid="stMain"] [data-testid="stHeading"] h1 {{
    padding-bottom: 0.6rem; border-bottom: 1px solid rgba(128, 128, 128, 0.25); margin-bottom: 0.5rem;
}}
/* The requirement under review stands out from the rest of the page. */
.st-key-rv-requirement {{ border-left: 4px solid {ACCENT}; padding-left: 1rem; }}
.st-key-rv-requirement p {{ font-size: 1.15rem; line-height: 1.5; }}
</style>""")


def _load() -> pd.DataFrame:
    conn = draft.connect_drafts()
    rows = db.read_sql(conn, "SELECT r.*, j.name AS draft_name, j.client, j.plan, j.created_at FROM rows r"
                             " JOIN jobs j ON j.job_id = r.job_id WHERE r.ai IS NOT NULL"
                             " ORDER BY j.created_at DESC, r.sheet, r.row_num")
    conn.close()
    rows["who"] = [review_grid.name_or_none(a) for a in rows.assignee]
    rows["draft"] = [f"{n} — {c}" if isinstance(c, str) and c else n for n, c in zip(rows.draft_name, rows.client)]
    return rows


def _counts(rows: pd.DataFrame) -> dict:
    open_ = rows.reviewed == 0
    return {"rows": len(rows), "approved": int((~open_).sum()), "left": int(open_.sum()),
            "low": int((open_ & (rows.confidence == "low")).sum()),
            "pricing": int((open_ & (rows.cost_impact == 1)).sum())}


def _go(**params) -> None:
    """Move to another level of the board (params: person, draft, sheet; omitted ones are cleared)."""
    st.query_params.clear()
    st.query_params.update({k: v for k, v in params.items() if v})


def _sheet_plan(view: pd.DataFrame, sheet: str) -> tuple[dict, bool]:
    plan = json.loads(view.plan.iloc[0])
    multi = len([s for s in plan["sheets"] if s.get("include", True)]) > 1
    return next(s for s in plan["sheets"] if s["name"] == sheet), multi


# =================================================================================================
# Everyone
# =================================================================================================
def people_list(rows: pd.DataFrame) -> None:
    st.title("Task board")
    assigned = rows[rows.who.notna()]
    if assigned.empty:
        st.info("No one has rows to review yet. Assign rows from a draft's page: filter its review grid, "
                "then **Assign shown rows**.")
        return
    st.caption("Open your name to see the sheets assigned to you.")
    names = sorted(set(assigned.who), key=str.lower)
    for start in range(0, len(names), 3):
        for col, name in zip(st.columns(3), names[start:start + 3]):
            mine = assigned[assigned.who == name]
            c = _counts(mine)
            sheets = mine.groupby(["job_id", "sheet"]).ngroups
            with col.container(border=True):
                st.markdown(f"#### {name}")
                st.progress(c["approved"] / c["rows"],
                            text=f"{c['approved']:,} of {c['rows']:,} approved · {sheets} sheet{'s' * (sheets != 1)}")
                st.caption(f"{c['left']:,} to review · {c['low']} low confidence · {c['pricing']} needs pricing")
                st.button("Open", key=f"open:{name}", width="stretch", type="primary" if c["left"] else "secondary",
                          on_click=_go, kwargs={"person": name})
    if unassigned := int((rows.who.isna() & rows.returned_by.isna()).sum()):
        st.caption(f"{unassigned:,} drafted rows aren't assigned to anyone yet.")


# =================================================================================================
# One person: their sheets
# =================================================================================================
def person_view(rows: pd.DataFrame, person: str) -> None:
    st.button("All people", icon=":material/arrow_back:", type="tertiary", on_click=_go)
    st.title(person)
    mine = rows[rows.who == person]
    if mine.empty:
        st.info(f"Nothing is assigned to {person} right now.")
        return
    st.caption("Your assigned sheets. **Start review** takes you through the rows one at a time, "
               "least confident first, then the ones that need pricing.")
    for (job_id, sheet), view in mine.groupby(["job_id", "sheet"], sort=False):
        sheet_plan, multi = _sheet_plan(view, sheet)
        c = _counts(view)
        with st.container(border=True):
            title, action = st.columns([4, 1], vertical_alignment="center")
            title.markdown(f"#### {view.draft.iloc[0]}" + (f" · {sheet}" if multi else ""))
            action.button("Start review" if c["left"] else "Review again", key=f"start:{job_id}:{sheet}",
                          icon=":material/play_arrow:", type="primary" if c["left"] else "secondary",
                          width="stretch", on_click=_start_review, args=(person, job_id, sheet, view))
            m = st.columns(4)
            m[0].metric("Rows", f"{c['rows']:,}")
            m[1].metric("To review", f"{c['left']:,}")
            m[2].metric("Low confidence", c["low"], help="Not yet approved")
            m[3].metric("Needs pricing", c["pricing"], help="Not yet approved")
            st.progress(c["approved"] / c["rows"], text=f"{c['approved']:,} of {c['rows']:,} approved")
            with st.expander("Browse the rows"):
                review_grid.grid(job_id, sheet_plan, view.drop(columns=["who", "draft"]),
                                 key=f"board:{person}:{job_id}:{sheet}")
                st.markdown("**Answer details**")
                review_grid.details(view, key=f"board:{person}:{job_id}:{sheet}")


# =================================================================================================
# Row-by-row review
# =================================================================================================
def _queue(view: pd.DataFrame) -> list[int]:
    """Review order: low confidence, then needs pricing, then medium and high confidence; rows already
    approved are left out unless everything is (then it's a second pass over all of them)."""
    todo = view[view.reviewed == 0] if (view.reviewed == 0).any() else view
    rank = [0 if c == "low" else 1 if p else 2 if c == "medium" else 3
            for c, p in zip(todo.confidence, todo.cost_impact)]
    return todo.assign(_rank=rank).sort_values(["_rank", "row_num"]).row_num.tolist()


def _start_review(person: str, job_id: str, sheet: str, view: pd.DataFrame) -> None:
    st.session_state.review = {"job": job_id, "sheet": sheet, "queue": _queue(view), "pos": 0}
    _go(person=person, draft=job_id, sheet=sheet)


def _field_key(job_id: str, sheet: str, row_num: int, field: str) -> str:
    return f"rv:{job_id}:{sheet}:{row_num}:{field}"


def _save_answers(job_id: str, sheet: str, row_num: int, fields: list[dict], answers: dict) -> None:
    """Store whatever is in the answer boxes, if it differs from what's saved."""
    new = {f["key"]: st.session_state.get(_field_key(job_id, sheet, row_num, f["key"])) or "" for f in fields}
    if new != {f["key"]: answers.get(f["key"]) or "" for f in fields}:
        draft.save_review(job_id, sheet, row_num, final=new)


def _move(step: int, approve: bool | None, job_id: str, sheet: str, row_num: int, fields: list[dict],
          answers: dict) -> None:
    _save_answers(job_id, sheet, row_num, fields, answers)
    if approve is not None:
        draft.save_review(job_id, sheet, row_num, reviewed=approve)
    st.session_state.review["pos"] += step


def _flag(job_id: str, sheet: str, row_num: int) -> None:
    draft.save_review(job_id, sheet, row_num,
                      cost_impact=st.session_state[_field_key(job_id, sheet, row_num, "_pricing")])


UNASSIGN = "Unassign (return to the pool)"


def _reassign(person: str, job_id: str, sheet: str, row_num: int, fields: list[dict], answers: dict) -> None:
    """Give the row to someone else, or return it to the pool as not this person's (keeping any edits).
    It leaves this review either way, so the next row shows."""
    _save_answers(job_id, sheet, row_num, fields, answers)
    to = st.session_state.get(_field_key(job_id, sheet, row_num, "_reassign"))
    if to == UNASSIGN:
        draft.return_row(job_id, sheet, row_num, person)
        st.session_state.review_notice = f"Row {row_num} went back to the pool."
    else:
        draft.assign(job_id, sheet, [row_num], to)
        st.session_state.review_notice = f"Row {row_num} handed to {to}."


def review_view(rows: pd.DataFrame, person: str, job_id: str, sheet: str) -> None:
    if notice := st.session_state.pop("review_notice", None):
        st.toast(notice, icon=":material/move_item:")
    view = rows[(rows.who == person) & (rows.job_id == job_id) & (rows.sheet == sheet)]
    rv = st.session_state.get("review")
    if not rv or (rv["job"], rv["sheet"]) != (job_id, sheet):  # opened from a link or refreshed
        rv = st.session_state.review = {"job": job_id, "sheet": sheet, "queue": _queue(view), "pos": 0}
    queue = [n for n in rv["queue"] if n in set(view.row_num)]  # rows reassigned meanwhile drop out
    rv["queue"], rv["pos"] = queue, min(rv["pos"], len(queue))

    st.button(f"Back to {person}'s sheets", icon=":material/arrow_back:", type="tertiary",
              on_click=_go, kwargs={"person": person})
    if view.empty:
        st.info("These rows are no longer assigned to you.")
        return
    sheet_plan, multi = _sheet_plan(view, sheet)
    st.title(view.draft.iloc[0] + (f" · {sheet}" if multi else ""))
    approved = int(view.reviewed.sum())
    st.progress(approved / len(view), text=f"{approved:,} of {len(view):,} approved")

    if rv["pos"] >= len(queue):
        st.success(f"That's every row in this review. {len(view) - approved:,} left unapproved on this sheet."
                   if approved < len(view) else "Every row on this sheet is approved.")
        a, b, _ = st.columns([1, 1, 3])
        if approved < len(view):
            a.button("Review what's left", type="primary", width="stretch",
                     on_click=_start_review, args=(person, job_id, sheet, view))
        b.button("Back to sheets", width="stretch", on_click=_go, kwargs={"person": person})
        return

    row_num = queue[rv["pos"]]
    r = view.set_index("row_num").loc[row_num]
    keys = json.loads(r.fill)
    fields = [f for f in sheet_plan["fields"] if f.get("include", True) and f["key"] in keys]
    answers = json.loads(r.final if isinstance(r.final, str) else r.ai)
    nav = (job_id, sheet, row_num, fields, answers)

    with st.container(key="rv-requirement"):
        if isinstance(r.parent_text, str) and r.parent_text:
            st.caption(f"Under: {r.parent_text}")
        st.markdown(r.requirement)
    with st.container(horizontal=True, gap="small"):
        status_badge(r.status)
        st.badge(f"{review_grid.CONF_LABELS.get(r.confidence)} confidence",
                 color={"low": "red", "medium": "orange", "high": "green"}.get(r.confidence, "gray"))
        if r.reviewed:
            st.badge("Approved", icon=":material/check:", color="green")

    for f in fields:
        key, value = _field_key(job_id, sheet, row_num, f["key"]), answers.get(f["key"]) or ""
        if f["kind"] in ("choice", "marks"):
            options = f["options"] if f["kind"] == "choice" else [f["labels"][c] for c in f["columns"]]
            st.selectbox(f["label"], options, index=options.index(value) if value in options else None,
                         key=key, on_change=_save_answers, args=nav)
        else:
            st.text_area(f["label"], value=value, key=key, height=140, on_change=_save_answers, args=nav)
    st.checkbox("Flag for pricing", value=bool(r.cost_impact), key=_field_key(job_id, sheet, row_num, "_pricing"),
                on_change=_flag, args=(job_id, sheet, row_num),
                help="Meeting this requirement may add cost. Highlighted yellow in the download.")

    p, s, h, a = st.columns([1, 1, 1, 2])
    p.button("Previous", icon=":material/chevron_left:", width="stretch", disabled=rv["pos"] == 0,
             on_click=_move, args=(-1, None, *nav))
    s.button("Skip", icon=":material/chevron_right:", width="stretch", on_click=_move, args=(1, None, *nav),
             help="Keep your edits and come back to this row later")
    if r.reviewed:
        a.button("Next", type="primary", icon=":material/arrow_forward:", width="stretch",
                 on_click=_move, args=(1, None, *nav))
        st.button("Undo approval", type="tertiary", on_click=_move, args=(0, False, *nav))
    else:
        a.button("Approve & next", type="primary", icon=":material/check:", width="stretch",
                 on_click=_move, args=(1, True, *nav))
    # Keyed per row, so it starts closed on the next row after a hand-off.
    with h.popover("Reassign", icon=":material/move_item:", width="stretch",
                   key=_field_key(job_id, sheet, row_num, "_reassign_menu"),
                   help="Out of your area? Give this row to someone else, or return it to the pool"):
        choice = st.selectbox("Reassign to", [*[n for n in draft.people() if n != person], UNASSIGN], index=None,
                              placeholder="Pick a person", key=_field_key(job_id, sheet, row_num, "_reassign"))
        st.button("Accept", type="primary", width="stretch", disabled=choice is None,
                  on_click=_reassign, args=(person, *nav))
    st.caption(f"Row {rv['pos'] + 1} of {len(queue)} in this review")
    context = " › ".join(v for v in json.loads(r.context).values() if v) if isinstance(r.context, str) else ""
    st.caption(" · ".join(p for p in [r.req_id if isinstance(r.req_id, str) else "", f"Worksheet row {row_num}",
                                     context] if p))
    st.divider()
    st.subheader("Why the AI answered this way")
    st.caption(review_grid.confidence_note(r))
    review_grid.evidence(r, expanded=2)


# =================================================================================================
rows = _load()
person, job_id, sheet = (st.query_params.get(k) for k in ("person", "draft", "sheet"))
if person and job_id and sheet:
    review_view(rows, person, job_id, sheet)
elif person:
    person_view(rows, person)
else:
    people_list(rows)
