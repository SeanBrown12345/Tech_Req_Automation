"""Task board: everyone with rows to review. Each person opens their own view, sees the sheets assigned to
them, and either browses a sheet's rows in a grid or reviews them one at a time.

No sign-in: people find themselves by name. The address carries where they are (?person=, then &draft=
and &sheet= in a review), so a link opens someone's view directly and a refresh keeps the place."""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from app import review_grid
from app.admin_layout import ACCENT
from app.common import card_styles, status_badge
from app.header import BANNER_COLOR
from kb import db, draft, notify

MAROON = BANNER_COLOR
MAROON_HOVER = "#A3214A"
MAROON_LIGHT = "#C8456A"  # outlines
MAROON_TEXT = "#F0A3BA"  # text on the dark page

st.html(f"""<style>
[data-testid='stMainBlockContainer'] {{ max-width: 1200px; margin: 0 auto; }}
[data-testid="stMain"] [data-testid="stHeading"] h1 {{
    padding-bottom: 0.6rem; border-bottom: 1px solid rgba(128, 128, 128, 0.25); margin-bottom: 0.5rem;
}}
/* The task board's buttons take the banner's maroon in place of the theme blue: filled buttons in the maroon,
   outlines and text in a lighter shade that reads on the dark page. */
[data-testid="stBaseButton-primary"] {{ background: {MAROON}; border-color: {MAROON}; color: #fff; }}
[data-testid="stBaseButton-primary"]:hover {{ background: {MAROON_HOVER}; border-color: {MAROON_HOVER}; color: #fff; }}
[data-testid="stBaseButton-primary"]:active, [data-testid="stBaseButton-primary"]:focus {{ color: #fff; }}
[data-testid="stBaseButton-secondary"]:hover, [data-testid="stPopoverButton"]:hover {{
    border-color: {MAROON_LIGHT}; color: {MAROON_LIGHT};
}}
[data-testid="stBaseButton-tertiary"]:hover {{ color: {MAROON_LIGHT}; }}
[data-testid="stButtonGroup"] button[aria-checked="true"] {{  /* Streamlit's own rule for it is more specific */
    background: {MAROON} !important; border-color: {MAROON} !important; color: #fff !important;
}}
[data-testid="stButtonGroup"] button:hover {{ border-color: {MAROON} !important; }}
[data-testid="stButtonGroup"] button[aria-checked="true"] * {{ color: #fff !important; }}
button:focus-visible {{ box-shadow: 0 0 0 0.2rem rgba(200, 69, 106, 0.5) !important; }}
/* Notes sits beside the Row by row | Table switch, at its height. */
[class*="st-key-notes-panel-"] button {{ min-height: 32px; height: 32px; padding-top: 0; padding-bottom: 0; }}
/* Except Notes when something waits on you: blue, as the rows with notes are. */
[class*="st-key-notes-panel-"] [data-testid="stBaseButton-primary"] {{ background: #3987e5; border-color: #3987e5; }}
[class*="st-key-notes-panel-"] [data-testid="stBaseButton-primary"]:hover {{ background: #5A9CEB; border-color: #5A9CEB; }}
/* Open on each person's card: white with dark text. */
[class*="st-key-open-"] button {{ background: #ffffff; color: #16181D; border-color: #ffffff; }}
[class*="st-key-open-"] button:hover {{ background: #E6E8EC; color: #16181D; border-color: #E6E8EC; }}
[class*="st-key-open-"] button:active, [class*="st-key-open-"] button:focus {{ color: #16181D; }}
/* The requirement under review stands out from the rest of the page. */
.st-key-rv-requirement {{ border-left: 4px solid {ACCENT}; padding-left: 1rem; }}
.st-key-rv-requirement p {{ font-size: 1.15rem; line-height: 1.5; }}
/* Back (home, or to all people): an arrow in the left margin, level with the page title, taking no space in the flow. */
[data-testid="stElementContainer"]:has(> .st-key-board-back), .st-key-board-back {{ height: 0; margin: 0; }}
.st-key-board-back {{ position: relative; overflow: visible; }}
.st-key-board-back button {{ position: absolute; left: -3rem; top: 2.6rem; font-size: 1.5rem; }}
.st-key-board-back button span {{ font-size: 1.75rem; }}
</style>""")
card_styles()


def _load() -> pd.DataFrame:
    conn = draft.connect_drafts()
    rows = db.read_sql(conn, "SELECT r.*, j.name AS draft_name, j.client, j.plan, j.created_at, j.filename FROM rows r"
                             " JOIN jobs j ON j.job_id = r.job_id WHERE r.ai IS NOT NULL"
                             " ORDER BY j.created_at DESC, r.sheet, r.row_num")
    conn.close()
    rows = rows.merge(draft.note_summary(), on=["job_id", "sheet", "row_num"], how="left")
    rows["notes"], rows["all_notes"] = rows.notes.fillna(0).astype(int), rows.all_notes.fillna(0).astype(int)
    rows.loc[rows.notes == 0, "last_by"] = None  # resolved conversations wait on no one
    rows["who"] = [review_grid.name_or_none(a) for a in rows.assignee]
    rows["draft"] = [f"{n} — {c}" if isinstance(c, str) and c else n for n, c in zip(rows.draft_name, rows.client)]
    return rows


def _counts(rows: pd.DataFrame) -> dict:
    open_ = rows.reviewed == 0
    return {"rows": len(rows), "approved": int((~open_).sum()), "left": int(open_.sum()),
            "low": int((open_ & (rows.confidence == "low")).sum()),
            "pricing": int((open_ & (rows.cost_impact == 1)).sum()),
            # Someone else wrote the latest note on the row: a question or hand-off note waiting on its SME.
            "messages": int((open_ & rows.last_by.notna() & (rows.last_by != rows.who)).sum())}


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
def _partner_counts(rows: pd.DataFrame) -> dict:
    status = rows.partner_status
    return {"sent": int(status.isin(["sent", "received"]).sum()), "back": int((status == "received").sum()),
            "to_approve": int(((status == "received") & (rows.reviewed == 0)).sum())}


def _card(col, name: str, mine: pd.DataFrame, partner: bool) -> None:
    c = _counts(mine)
    sheets = mine.groupby(["job_id", "sheet"]).ngroups
    with col.container(border=True):
        st.markdown(f"#### {name}")
        st.progress(c["approved"] / c["rows"],
                    text=f"{c['approved']:,} of {c['rows']:,} approved · {sheets} sheet{'s' * (sheets != 1)}")
        if partner:
            pc = _partner_counts(mine)
            st.caption(f"{pc['sent']:,} sent · {pc['back']:,} answered · {pc['to_approve']:,} to approve"
                       + (f" · {c['rows'] - pc['sent']:,} not sent" if c["rows"] > pc["sent"] else ""))
            busy = pc["to_approve"]
        else:
            st.caption(f"{c['left']:,} to review · {c['low']} low confidence · {c['pricing']} needs pricing")
            busy = c["left"]
        st.button("Open", key=f"open:{name}", width="stretch", type="primary" if busy else "secondary",
                  on_click=_go, kwargs={"person": name})


def _cards(assigned: pd.DataFrame, partner: bool) -> None:
    names = sorted(set(assigned.who), key=str.lower)
    for start in range(0, len(names), 3):
        for col, name in zip(st.columns(3), names[start:start + 3]):
            _card(col, name, assigned[assigned.who == name], partner)


def people_list(rows: pd.DataFrame) -> None:
    with st.container(key="board-back"):
        if st.button("", icon=":material/arrow_back:", type="tertiary", help="Home"):
            st.switch_page("views/home.py")
    st.title("Task board")
    assigned = rows[rows.who.notna()]
    if assigned.empty:
        st.info("No one has rows to review yet. Assign rows from a draft's page: filter its review grid, "
                "then **Assign shown rows**.")
        return
    # People whose rows are all approved are done and drop off the board (their page still opens by link).
    assigned = assigned[assigned.who.isin(set(assigned.who[assigned.reviewed == 0]))]
    if assigned.empty:
        st.success("Everyone's done: every assigned row is approved.")
    partner_names = set(draft.partners())
    smes, theirs = assigned[~assigned.who.isin(partner_names)], assigned[assigned.who.isin(partner_names)]
    if not smes.empty:
        st.caption("Open your name to see the sheets assigned to you.")
        _cards(smes, partner=False)
    if not theirs.empty:
        st.subheader("Partners")
        _cards(theirs, partner=True)
    if unassigned := int((rows.who.isna() & rows.returned_by.isna() & (rows.approved_by != "auto")).sum()):
        st.caption(f"{unassigned:,} drafted rows aren't assigned to anyone yet.")


# =================================================================================================
# One person: their sheets
# =================================================================================================
def _replies(rows: pd.DataFrame, person: str) -> None:
    """Rows the person left a note on that aren't theirs now (handed on, or returned to the pool) and someone
    has written on since: read the conversation and answer it here."""
    if not rows.last_by.notna().any():
        return
    wrote = draft.notes()
    wrote = set(map(tuple, wrote.loc[wrote.author == person, ["job_id", "sheet", "row_num"]].values))
    noted = pd.Series([(j, s, n) in wrote for j, s, n in zip(rows.job_id, rows.sheet, rows.row_num)], index=rows.index)
    waiting = rows[noted & (rows.who != person) & rows.last_by.notna() & (rows.last_by != person)]
    if waiting.empty:
        return
    st.html("""<style>
.st-key-replies { padding: 0.6rem 0.9rem; gap: 0.4rem; }
.st-key-replies [data-testid="stExpander"] summary { padding-top: 0.35rem; padding-bottom: 0.35rem; font-size: 0.9rem; }
.st-key-replies [data-testid="stExpanderDetails"] { padding-top: 0.25rem; padding-bottom: 0.5rem; }
</style>""")
    with st.container(border=True, key="replies"):
        st.markdown(f"**:material/forum: Replies on rows you handed on ({len(waiting)})**",
                    help="These rows aren't yours any more, but someone has written since your note. Answer here.")
        for r in waiting.itertuples():
            holder = r.who or "the pool"
            with st.expander(f"Row {r.row_num} · {r.draft}{f' · {r.sheet}' if r.sheet else ''} · now with {holder}"):
                st.caption(r.requirement if len(r.requirement) <= 160 else r.requirement[:157] + "…")
                review_grid.thread(r.job_id, r.sheet, int(r.row_num), person, key="replies", compact=True)


def person_view(rows: pd.DataFrame, person: str) -> None:
    with st.container(key="board-back"):
        st.button("", icon=":material/arrow_back:", type="tertiary", help="All people", on_click=_go)
    st.title(person)
    _replies(rows, person)
    mine = rows[rows.who == person]
    if mine.empty:
        st.info(f"Nothing is assigned to {person} right now.")
        return
    partner = person in draft.partners()
    if partner:
        partner_view(mine, person)
        return
    st.caption("Your assigned sheets. **Review** takes you through the rows one at a time, in sheet order. "
               "**Review in table** shows them all at once.")
    dues = draft.due_dates(person)
    # Sheets with rows still to review first, finished ones (every row approved) below; newest draft first in each.
    sheets = sorted(mine.groupby(["job_id", "sheet"], sort=False), key=lambda g: bool(g[1].reviewed.all()))
    for (job_id, sheet), view in sheets:
        _, multi = _sheet_plan(view, sheet)
        c = _counts(view)
        with st.container(border=True):
            title, actions = st.columns([3, 2], vertical_alignment="center")
            title.markdown(f"#### {view.draft.iloc[0]}" + (f" · {sheet}" if multi else ""))
            if due := dues.get(job_id):
                d = date.fromisoformat(due)
                title.caption(f":material/event: Due {d:%a} {d.day} {d:%b %Y}"
                              + (" · **past due**" if d < date.today() and c["left"] else ""))
            with actions.container(horizontal=True, horizontal_alignment="right"):
                st.button("Review" if c["left"] else "Review again", key=f"start:{job_id}:{sheet}",
                          icon=":material/play_arrow:", type="primary" if c["left"] else "secondary",
                          on_click=_start_review, args=(person, job_id, sheet, view))
                st.button("Review in table", key=f"table:{job_id}:{sheet}", icon=":material/table_rows:",
                          on_click=_go, kwargs={"person": person, "draft": job_id, "sheet": sheet, "mode": "table"})
            m = st.columns(4)
            m[0].metric("Rows", f"{c['rows']:,}")
            m[1].metric("To review", f"{c['left']:,}")
            m[2].metric("Low confidence", c["low"], help="Not yet approved")
            m[3].metric("Needs pricing", c["pricing"], help="Not yet approved")
            st.progress(c["approved"] / c["rows"], text=f"{c['approved']:,} of {c['rows']:,} approved")
            if c["messages"]:
                st.caption(f":blue[:material/forum: **{c['messages']} row{'s' * (c['messages'] != 1)} with a "
                           "note for you**]: open the row to read it and reply.")


# =================================================================================================
# One partner: their drafts, the worksheet out and their reply back
# =================================================================================================
def _joined(answer: dict, keys: list[str]) -> str:
    return " | ".join(str(answer.get(k) or "") for k in keys if answer.get(k))


def _import_reply(job_id: str, partner: str) -> None:
    """Upload the partner's returned worksheet, preview how it matches their rows, then import."""
    upload_key = f"reply:{job_id}:{partner}:{st.session_state.get('reply_rev', 0)}"
    reply = st.file_uploader(f"{partner}'s returned worksheet (.xlsx)", type=["xlsx", "xlsm"], key=upload_key)
    if not reply:
        return
    items = draft.read_partner_answers(job_id, partner, reply.getvalue())
    counts = pd.Series([i["status"] for i in items]).value_counts()
    st.caption(" · ".join(f"{n} {status.lower()}" for status, n in counts.items()))
    st.dataframe(pd.DataFrame([{
        "Row": i["row_num"], "Requirement": i["requirement"], "Result": i["status"],
        "Their answer": _joined(i["theirs"], i["fill"]), "Our draft": _joined(i["ours"], i["fill"]),
    } for i in items]), hide_index=True, width="stretch", column_config={
        "Requirement": st.column_config.TextColumn(width="large"),
        "Their answer": st.column_config.TextColumn(width="large"),
        "Our draft": st.column_config.TextColumn(width="medium")})
    importable = sum(i["status"] in draft.IMPORTABLE for i in items)
    st.caption("Only *changed* and *same as our draft* rows are imported, as answered but not approved. Blank "
               "rows stay sent; rows whose requirement doesn't match are skipped.")
    if st.button(f"Import {importable:,} answer{'s' * (importable != 1)}", type="primary", disabled=not importable,
                 key=f"apply:{job_id}:{partner}"):
        done = draft.apply_partner_answers(job_id, items)
        st.session_state.partner_flash = f"Imported {done:,} answers from {partner}."
        st.session_state.reply_rev = st.session_state.get("reply_rev", 0) + 1
        review_grid.refresh()
        st.rerun()


@st.dialog("Approve all answers?")
def _confirm_approve_all(job_id: str, partner: str, count: int, draft_name: str) -> None:
    st.write(f"Approve the **{count:,}** answers {partner} sent back for **{draft_name}**?")
    st.caption("Rows they haven't answered stay open. You can still change or un-approve any row afterwards.")
    cancel, go = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if go.button("Approve all", type="primary", width="stretch"):
        done, held = draft.approve_partner_answers(job_id, partner)
        st.session_state.partner_flash = f"Approved {done:,} answers from {partner}." + (
            f" {held:,} left open: their response needs a comment and it's empty." if held else "")
        review_grid.refresh()
        st.rerun()


def partner_view(mine: pd.DataFrame, person: str) -> None:
    """A partner's rows, per draft: export their trimmed worksheet, import their reply, approve the answers."""
    if flash := st.session_state.pop("partner_flash", None):
        st.toast(flash, icon=":material/check:")
    for job_id, job_rows in mine.groupby("job_id", sort=False):
        pc, c = _partner_counts(job_rows), _counts(job_rows)
        unsent = c["rows"] - pc["sent"]
        with st.container(border=True):
            title, actions = st.columns([3, 2], vertical_alignment="center")
            title.markdown(f"#### {job_rows.draft.iloc[0]}")
            with actions.container(horizontal=True, horizontal_alignment="right"):
                # Reviewing what they sent back: one button per sheet they have rows on, beside the export.
                multi = job_rows.sheet.nunique() > 1
                for sheet, view in job_rows.groupby("sheet", sort=False):
                    st.button(("Review answers" if _counts(view)["left"] else "Review again")
                              + (f" · {sheet}" if multi else ""), key=f"start:{job_id}:{sheet}",
                              icon=":material/play_arrow:", on_click=_start_review, args=(person, job_id, sheet, view))
                st.download_button(
                    "Export worksheet", lambda j=job_id: draft.build_partner_export(j, person),
                    file_name=f"{Path(job_rows.filename.iloc[0]).stem} - {person}.xlsx",
                    icon=":material/outgoing_mail:", type="primary" if unsent else "secondary",
                    key=f"export:{job_id}:{person}", on_click=draft.mark_sent, args=(job_id, person),
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    help="Their rows filled with our drafts, every other row hidden. Marks the rows as sent.")
            m = st.columns(4)
            m[0].metric("Rows", f"{c['rows']:,}")
            m[1].metric("Sent", f"{pc['sent']:,}")
            m[2].metric("Answered", f"{pc['back']:,}", help="Imported from their reply")
            m[3].metric("To approve", f"{pc['to_approve']:,}", help="Answered, not yet approved")
            st.progress(c["approved"] / c["rows"], text=f"{c['approved']:,} of {c['rows']:,} approved")
            with st.expander("Import their reply"):
                _import_reply(job_id, person)
            if pc["to_approve"]:
                if st.button(f"Approve all {pc['to_approve']:,} answers", icon=":material/done_all:", type="primary",
                             key=f"approve-all:{job_id}:{person}",
                             help="Approve every answer they sent back that isn't approved yet"):
                    _confirm_approve_all(job_id, person, pc["to_approve"], job_rows.draft.iloc[0])


# =================================================================================================
# Row-by-row review
# =================================================================================================
def _queue(view: pd.DataFrame, partner: bool = False) -> list[int]:
    """Review order: the sheet's row order, the same as the table, so switching views doesn't reshuffle the
    rows. For a partner's rows, answers they changed come first, then answers they sent back as drafted, then
    rows not answered yet. Rows already approved are left out unless everything is (then it's a second pass
    over them all)."""
    todo = view[view.reviewed == 0] if (view.reviewed == 0).any() else view
    if not partner:
        return todo.sort_values("row_num").row_num.tolist()

    def group(status, final, ai):
        if status != "received":
            return 2
        return 0 if isinstance(final, str) and json.loads(final) != json.loads(ai) else 1

    rank = [group(s, f, a) for s, f, a in zip(todo.partner_status, todo.final, todo.ai)]
    return todo.assign(_rank=rank).sort_values(["_rank", "row_num"]).row_num.tolist()


def _start_review(person: str, job_id: str, sheet: str, view: pd.DataFrame) -> None:
    st.session_state.review = {"job": job_id, "sheet": sheet, "queue": _queue(view, person in draft.partners()),
                               "pos": 0}
    _go(person=person, draft=job_id, sheet=sheet)


def _field_key(job_id: str, sheet: str, row_num: int, field: str) -> str:
    return f"rv:{job_id}:{sheet}:{row_num}:{field}"


def _save_answers(job_id: str, sheet: str, row_num: int, fields: list[dict], answers: dict) -> None:
    """Store whatever is in the answer boxes, if it differs from what's saved."""
    new = {f["key"]: st.session_state.get(_field_key(job_id, sheet, row_num, f["key"])) or "" for f in fields}
    if new != {f["key"]: answers.get(f["key"]) or "" for f in fields}:
        draft.save_review(job_id, sheet, row_num, final=new)


def _move(step: int, approve: bool | None, job_id: str, sheet: str, row_num: int, fields: list[dict],
          answers: dict, sheet_plan: dict | None = None) -> None:
    _save_answers(job_id, sheet, row_num, fields, answers)
    if approve and sheet_plan:
        current = {f["key"]: st.session_state.get(_field_key(job_id, sheet, row_num, f["key"])) or "" for f in fields}
        if note := draft.missing_comment(sheet_plan, current, [f["key"] for f in fields]):
            st.session_state.review_blocked = (job_id, sheet, row_num, note)  # stay on the row and say why
            return
    if approve is not None:
        draft.save_review(job_id, sheet, row_num, reviewed=approve)
    st.session_state.review["pos"] += step


def _flag(job_id: str, sheet: str, row_num: int) -> None:
    draft.save_review(job_id, sheet, row_num,
                      cost_impact=st.session_state[_field_key(job_id, sheet, row_num, "_pricing")])


UNASSIGN = "Unassign (return to the pool)"


def _email_handoff(person: str, to: str, job_id: str, sheet: str, row_nums: list[int], note: str) -> str:
    """Email the SME the rows were handed to, straight away, with the note. Partners aren't emailed (they get
    the trimmed worksheet). Returns a sentence for the toast; rows that couldn't be emailed stay pending in
    Admin › Notifications."""
    if to in draft.partners():
        return ""
    later = " It can be sent from Admin › Notifications."
    if not notify.configured():
        return f" Email isn't set up, so {to} wasn't emailed."
    if not (email := draft.sme_email(to)):
        return f" {to} has no email address, so wasn't emailed." + later
    due = draft.due_dates(to).get(job_id)
    msg = notify.handoff_message(to, email, person, job_id, draft.job_title(job_id), sheet, row_nums, note or None,
                                 date.fromisoformat(due) if due else None)
    try:
        failed = notify.send([msg])
    except notify.SEND_ERRORS as e:
        failed = {0: str(e)}
    if failed:
        return f" The email to {to} didn't send ({failed[0][:80]})." + later
    draft.record_email(job_id, to, notify.NEW, [(sheet, n) for n in row_nums])
    return f" {to} was emailed."


def _reassign(person: str, job_id: str, sheet: str, row_num: int, fields: list[dict], answers: dict) -> None:
    """Give the row to someone else, or return it to the pool as not this person's (keeping any edits).
    It leaves this review either way, so the next row shows."""
    _save_answers(job_id, sheet, row_num, fields, answers)
    to = st.session_state.get(_field_key(job_id, sheet, row_num, "_reassign"))
    note = (st.session_state.get(_field_key(job_id, sheet, row_num, "_reassign_note")) or "").strip()
    draft.add_note(job_id, sheet, [row_num], person, note)
    if to == UNASSIGN:
        draft.return_row(job_id, sheet, row_num, person, note)
        st.session_state.review_notice = f"Row {row_num} went back to the pool."
    else:
        draft.assign(job_id, sheet, [row_num], to)
        st.session_state.review_notice = (f"Row {row_num} handed to {to}."
                                          + _email_handoff(person, to, job_id, sheet, [row_num], note))


MODES = {"Row by row": None, "Table": "table"}
# Switching mode or opening a row from the table counts as moving to a new view (review_nav); the browser
# scrolls to the top when it sees a new count. Streamlit keeps a page scrolled to the bottom while it fills
# in, so the new view would otherwise open part way down; it scrolls again once the last elements settle.
_TO_TOP = """<script>
if (window.reqfillReviewNav !== NAV) {
    window.reqfillReviewNav = NAV;
    const toTop = () => document.querySelector('[data-testid="stMain"]')?.scrollTo(0, 0);
    toTop(); setTimeout(toTop, 150); setTimeout(toTop, 600);
}
</script>"""


def _new_view() -> None:
    st.session_state.review_nav = st.session_state.get("review_nav", 0) + 1


def _panel_key(job_id: str, sheet: str) -> str:
    return f"notes-panel:{job_id}:{sheet}"


def _toggle_panel(panel: str) -> None:
    st.session_state[panel] = not st.session_state.get(panel)


def _table_key(person: str, job_id: str, sheet: str) -> str:
    return f"board:{person}:{job_id}:{sheet}"


def _goto_row(person: str, job_id: str, sheet: str, row_num: int, mode: str | None) -> None:
    """Take the person to a row from the notes panel, staying in the view they're in: in the table, clear the
    filters, scroll to it and open it under Answer details; row by row, make it the current row."""
    if mode == "table":
        key = _table_key(person, job_id, sheet)
        st.session_state.update({f"{key}:show": "All", f"{key}:section": review_grid.ALL_SECTIONS,
                                 f"{key}:module": review_grid.ALL_MODULES, f"why:{key}": row_num})
        st.session_state.notes_jump = {"row": row_num, "n": st.session_state.get("notes_jump", {}).get("n", 0) + 1}
        return
    rv = st.session_state.review
    if row_num not in rv["queue"]:  # approved rows aren't in the review: slot it in at its place on the sheet
        rv["queue"] = sorted([*rv["queue"], row_num])
    rv["pos"] = rv["queue"].index(row_num)
    _new_view()


_NOTE_CARDS = f"""<style>
/* The panel stands apart from the page, as Office's comments pane does: a lighter pane, cards lighter again
   (the app's theme is always dark, see .streamlit/config.toml). */
.st-key-notes-pane {{
    background: #1E2128; border: 1px solid rgba(255, 255, 255, 0.09); border-radius: 0.6rem;
    padding: 0.6rem 0.75rem 0.75rem; box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
}}
.st-key-notes-pane h4 {{ padding: 0.25rem 0; }}
[class*="st-key-notecard-"] {{ background: #282C34; border-color: rgba(255, 255, 255, 0.07) !important; }}
[class*="st-key-notecard-"] {{ position: relative; padding: 0.5rem 0.75rem; gap: 0.35rem; cursor: pointer; }}
[class*="st-key-notecard-"]:hover {{ border-color: {ACCENT} !important; }}
/* Clicks on the text reach the button underneath; the ⋮ menu and the reply box still take their own. */
[class*="st-key-notecard-"] [data-testid="stElementContainer"]:not([class*="st-key-notego-"]):not(:has([data-testid="stChatInput"])):not(:has([data-testid="stPopover"])) {{
    pointer-events: none;
}}
[class*="st-key-notego-"] {{ position: absolute; inset: 0; z-index: 1; }}
[class*="st-key-notego-"], [class*="st-key-notego-"] * {{ width: 100% !important; height: 100%; }}
[class*="st-key-notego-"] button {{ opacity: 0; }}
[class*="st-key-notecard-"] [data-testid="stPopover"] {{ position: relative; z-index: 2; }}
[class*="st-key-notemenu-"] button {{ border: none; background: transparent; padding: 0 0.25rem; min-height: 0; }}
[class*="st-key-notemenu-"] button div[aria-hidden="true"] {{ display: none; }}  /* the popover's chevron */
/* The ⋮ menu opens outside the card, so it's found by its Resolve button: a small menu, not a padded box. */
[data-testid="stPopoverBody"]:has([class*="st-key-resolve"]) {{ padding: 0.25rem; min-width: 0; width: auto; }}
[data-testid="stPopoverBody"] [class*="st-key-resolve"] button {{ padding: 0.3rem 0.6rem; min-height: 0; }}
[class*="st-key-notecard-"] [data-testid="stChatInput"] {{ position: relative; z-index: 2; }}
[class*="st-key-notecard-"] p {{ font-size: 0.875rem; }}  /* the Row line, as the thread's text */
</style>"""


_FULLSCREEN_ON = 'body:has([class*="st-key-gridbox-"] button[aria-label="Close fullscreen"])'
# Open: the panel takes the right of the page, as Office's comments pane does, and the row view widens to make
# room. In the table's fullscreen it floats on the right, over the page, and the table narrows beside it.
_PANEL_OPEN = f"""<style>
[data-testid='stMainBlockContainer'] {{ max-width: 1600px; }}
{_FULLSCREEN_ON} [class*="st-key-gridbox-"] [data-testid="stFullScreenFrame"] {{ width: calc(100vw - 400px) !important; }}
{_FULLSCREEN_ON} [class*="st-key-gridbox-"] [data-testid="stFullScreenFrame"] [data-testid="stDataFrame"],
{_FULLSCREEN_ON} [class*="st-key-gridbox-"] [data-testid="stFullScreenFrame"] [data-testid="stDataFrameResizable"] {{
    width: calc(100vw - 424px) !important;  /* the table keeps the whole window's width otherwise */
}}
{_FULLSCREEN_ON} .st-key-notes-pane {{
    position: fixed; top: 0; right: 0; bottom: 0; width: 400px; z-index: 1000051; overflow-y: auto;
    border-radius: 0; border-width: 0 0 0 1px;
}}
{_FULLSCREEN_ON} .st-key-notes-list {{ height: auto !important; max-height: none !important; }}
</style>"""
# Closed: the panel's column is hidden and the rows take the whole width.
_PANEL_CLOSED = """<style>
[data-testid="stHorizontalBlock"]:has(.st-key-notes-pane) > [data-testid="stColumn"]:has(.st-key-notes-pane) {
    display: none;
}
[data-testid="stHorizontalBlock"]:has(.st-key-notes-pane) > [data-testid="stColumn"]:not(:has(.st-key-notes-pane)) {
    width: 100% !important; flex: 1 1 100% !important;
}
</style>"""


def _notes_panel(person: str, job_id: str, sheet: str, view: pd.DataFrame, mode: str | None) -> None:
    """The notes on this person's rows of the sheet, in sheet order: each row's conversation with a reply box,
    and Go to row. Rows where someone else wrote last are marked as waiting on them."""
    st.html(_NOTE_CARDS)
    with st.container(key="notes-pane"):
        _notes_pane(person, job_id, sheet, view, mode)


def _notes_pane(person: str, job_id: str, sheet: str, view: pd.DataFrame, mode: str | None) -> None:
    title, close = st.columns([5, 1], vertical_alignment="center")
    title.markdown("#### :material/forum: Notes")
    close.button("", icon=":material/close:", type="tertiary", help="Close the notes panel",
                 key=f"{_panel_key(job_id, sheet)}:close", on_click=_toggle_panel, args=(_panel_key(job_id, sheet),))
    noted = view[view.notes > 0].sort_values("row_num")
    if noted.empty:
        st.caption("No notes on your rows of this sheet yet. A note left when a row is handed to you, or written "
                   "on one of your rows, shows here.")
        return
    with st.container(height=700, border=False, gap="small", key="notes-list"):  # scrolls on its own, beside the rows
        for r in noted.itertuples():
            with st.container(border=True, gap=None, key=f"notecard-{r.row_num}"):
                # Clicking the card goes to the row: an invisible button over the card, under the reply box.
                st.button(f"Go to row {r.row_num}", key=f"notego-{r.row_num}",
                          on_click=_goto_row, args=(person, job_id, sheet, int(r.row_num), mode))
                with st.container(horizontal=True, vertical_alignment="center", gap=None):
                    st.markdown(f"**Row {r.row_num}**"
                                + (" :blue-badge[Waiting on you]" if r.last_by != person else ""), width="stretch")
                    with st.popover("", icon=":material/more_vert:", key=f"notemenu-{r.row_num}", width="content"):
                        st.button("Resolve", icon=":material/check:", key=f"resolve:{job_id}:{sheet}:{r.row_num}",
                                  type="tertiary", on_click=draft.resolve_notes,
                                  args=(job_id, sheet, int(r.row_num), person),
                                  help="Close this conversation: the row is no longer highlighted or listed here. "
                                       "Its notes stay on the row, and a new note opens it again.")
                review_grid.thread(job_id, sheet, int(r.row_num), person, key="panel", compact=True)


def _switch_mode(person: str, job_id: str, sheet: str, key: str) -> None:
    if (picked := st.session_state[key]) is not None:  # clicking the current mode again leaves it as it is
        _new_view()
        if MODES[picked] is None:  # into row by row: start afresh at the first row not approved (the table may
            st.session_state.pop("review", None)  # have approved some of the earlier review's rows)
        _go(person=person, draft=job_id, sheet=sheet, mode=MODES[picked])


def review_view(rows: pd.DataFrame, person: str, job_id: str, sheet: str, mode: str | None) -> None:
    """One sheet of one person's rows, reviewed row by row or in a table (SMEs can switch; partners'
    answers are reviewed row by row)."""
    if notice := st.session_state.pop("review_notice", None):
        st.toast(notice, icon=":material/move_item:")
    view = rows[(rows.who == person) & (rows.job_id == job_id) & (rows.sheet == sheet)]
    partner = person in draft.partners()
    rv = st.session_state.get("review")
    if not rv or (rv["job"], rv["sheet"]) != (job_id, sheet):  # opened from a link or refreshed
        rv = st.session_state.review = {"job": job_id, "sheet": sheet, "queue": _queue(view, partner), "pos": 0}
    queue = [n for n in rv["queue"] if n in set(view.row_num)]  # rows reassigned meanwhile drop out
    rv["queue"], rv["pos"] = queue, min(rv["pos"], len(queue))

    with st.container(key="board-back"):
        st.button("", icon=":material/arrow_back:", type="tertiary", help=f"Back to {person}'s sheets",
                  on_click=_go, kwargs={"person": person})
    if view.empty:
        st.info("These rows are no longer assigned to you.")
        return
    sheet_plan, multi = _sheet_plan(view, sheet)
    title = view.draft.iloc[0] + (f" · {sheet}" if multi else "")
    panel = _panel_key(job_id, sheet)
    if partner:
        st.title(title)
    else:
        head, notes_button, switch = st.columns([3, 0.55, 1], vertical_alignment="center")
        head.title(title)
        noted, waiting = int((view.notes > 0).sum()), int((view.last_by.notna() & (view.last_by != person)).sum())
        notes_button.button(f"Notes ({noted})" if noted else "Notes", icon=":material/forum:", width="stretch",
                            type="primary" if waiting and not st.session_state.get(panel) else "secondary",
                            on_click=_toggle_panel, args=(panel,), key=f"{panel}:toggle",
                            help="Show or hide the notes on your rows of this sheet"
                                 + (f" ({waiting} waiting on you)" if waiting else ""))
        key = f"mode:{job_id}:{sheet}"
        st.session_state[key] = next(k for k, v in MODES.items() if v == mode)  # follow the address
        switch.segmented_control("Review mode", list(MODES), key=key, label_visibility="collapsed",
                                 width="stretch", on_change=_switch_mode, args=(person, job_id, sheet, key))
    approved = int(view.reviewed.sum())
    st.progress(approved / len(view), text=f"{approved:,} of {len(view):,} approved")
    if partner:
        main = st.container()
    else:
        # The panel is always laid out and shown or hidden by style, so toggling it leaves the page's elements
        # where they are: the table isn't rebuilt, and stays in fullscreen.
        st.html(_PANEL_OPEN if st.session_state.get(panel) else _PANEL_CLOSED)
        main, side = st.columns([3, 1], gap="medium")
        with side:
            _notes_panel(person, job_id, sheet, view, mode)
    with main:
        if mode == "table" and not partner:
            _table_review(person, job_id, sheet, view, sheet_plan)
        else:
            _row_review(person, job_id, sheet, view, sheet_plan)


def _row_review(person: str, job_id: str, sheet: str, view: pd.DataFrame, sheet_plan: dict) -> None:
    """The row the review is on, with its answers to edit and approve."""
    rv = st.session_state.review
    queue = rv["queue"]
    approved = int(view.reviewed.sum())
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

    with st.container(border=True, key="card-row"):
        st.subheader("Requirement")
        with st.container(key="rv-requirement"):
            if isinstance(r.parent_text, str) and r.parent_text:
                st.caption(f"Under: {r.parent_text}")
            st.markdown(r.requirement)
        with st.container(horizontal=True, gap="small"):
            status_badge(r.status)
            st.badge(f"{review_grid.CONF_LABELS.get(r.confidence)} confidence",
                     color={"low": "red", "medium": "orange", "high": "green"}.get(r.confidence, "gray"))
            if isinstance(r.module, str) and r.module:
                st.badge(r.module, icon=":material/category:", color="blue")
            if r.partner_status == "received":
                st.badge(f"Answered by {person} · {str(r.received_at)[:10]}", icon=":material/mark_email_read:",
                         color="violet")
            elif r.partner_status == "sent":
                st.badge(f"Sent to {person} · {str(r.sent_at)[:10]} · no answer yet", icon=":material/schedule_send:",
                         color="orange")
            if r.reviewed:
                st.badge("Approved", icon=":material/check:", color="green")
            if n_notes := int(r.notes):
                st.badge(f"{n_notes} note{'s' * (n_notes != 1)}" + (f" · latest from {r.last_by}"
                                                                    if r.last_by != person else ""),
                         icon=":material/forum:", color="blue" if r.last_by != person else "gray")

        for f in fields:
            key, value = _field_key(job_id, sheet, row_num, f["key"]), answers.get(f["key"]) or ""
            if f["kind"] in ("choice", "marks"):
                options = f["options"] if f["kind"] == "choice" else [f["labels"][c] for c in f["columns"]]
                st.selectbox(f["label"], options, index=options.index(value) if value in options else None,
                             key=key, on_change=_save_answers, args=nav)
                picked = st.session_state.get(key)
                if picked and draft.required_comment(sheet_plan, {f["key"]: picked}, [x["key"] for x in fields]):
                    st.error(f["requires"][picked], icon=":material/warning:")
            else:
                st.text_area(f["label"], value=value, key=key, height=140, on_change=_save_answers, args=nav)
        original = json.loads(r.ai)
        if any((original.get(f["key"]) or "") != (answers.get(f["key"]) or "") for f in fields):
            with st.expander("Our original AI draft"):
                for f in fields:
                    st.caption(f"**{f['label']}**: {original.get(f['key']) or '—'}")
        st.checkbox("Flag for pricing", value=bool(r.cost_impact), key=_field_key(job_id, sheet, row_num, "_pricing"),
                    on_change=_flag, args=(job_id, sheet, row_num),
                    help="Meeting this requirement may add cost. Highlighted yellow in the download.")
        # Open when there's something to read; otherwise a closed box for starting a conversation.
        # Only rows with notes (resolved ones included, as history) have the section, and it starts open.
        if r.all_notes:
            with st.expander(f"Notes ({int(r.all_notes)})", expanded=True, icon=":material/forum:"):
                review_grid.thread(job_id, sheet, row_num, person, key="rv", compact=True)

        blocked = st.session_state.pop("review_blocked", None)
        if blocked and blocked[:3] == (job_id, sheet, row_num):
            comment = draft.comment_field(sheet_plan)["label"]
            st.error(f"Not approved yet: this response needs a comment in **{comment}**. {blocked[3].rstrip('.')}.",
                     icon=":material/error:")
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
                     on_click=_move, args=(1, True, *nav), kwargs={"sheet_plan": sheet_plan})
        # Keyed per row, so it starts closed on the next row after a hand-off.
        with h.popover("Reassign", icon=":material/move_item:", width="stretch",
                       key=_field_key(job_id, sheet, row_num, "_reassign_menu"),
                       help="Out of your area? Give this row to someone else, or return it to the pool"):
            covering = draft.partners()
            choice = st.selectbox("Reassign to", [*[n for n in draft.people() if n != person], UNASSIGN], index=None,
                                  placeholder="Pick a person", key=_field_key(job_id, sheet, row_num, "_reassign"),
                                  format_func=lambda n: f"{n} · partner" if n in covering else n)
            st.text_area("Note (optional)", key=_field_key(job_id, sheet, row_num, "_reassign_note"), height=80,
                         placeholder="Why it's theirs, or what you need from them. They see it on the row.")
            st.button("Accept", type="primary", width="stretch", disabled=choice is None,
                      on_click=_reassign, args=(person, *nav))
        st.caption(f"Row {rv['pos'] + 1} of {len(queue)} in this review")
        context = " › ".join(v for v in json.loads(r.context).values() if v) if isinstance(r.context, str) else ""
        st.caption(" · ".join(p for p in [r.req_id if isinstance(r.req_id, str) else "", f"Worksheet row {row_num}",
                                         context] if p))
    with st.container(border=True, key="card-why"):
        st.subheader("Why the AI answered this way")
        st.caption(review_grid.confidence_note(r))
        review_grid.evidence(r, expanded=2)


# =================================================================================================
# Table review
# =================================================================================================
# The Fullscreen button above the table opens the table's own fullscreen view, by clicking the matching
# button in the table's toolbar. The table keeps its fixed page height in fullscreen unless stretched to fill it.
# While fullscreen, the bar above the table (Wrap text, Reassign) is pinned top left, level with the toolbar,
# without its Fullscreen button; the Reassign dropdown and its pickers open above the fullscreen view.
_FULLSCREEN = """<style>
[class*="st-key-gridbox-"]:has(button[aria-label="Close fullscreen"]) [data-testid="stDataFrameResizable"] {
    height: 100% !important; max-height: none !important;
}
body:has([class*="st-key-gridbox-"] button[aria-label="Close fullscreen"]) .st-key-table-bar {
    position: fixed; top: 6px; left: 1rem; z-index: 1000051; width: auto;
}
body:has([class*="st-key-gridbox-"] button[aria-label="Close fullscreen"]) .st-key-table-fullscreen { display: none; }
.st-key-table-notes { display: none; }
body:has([class*="st-key-gridbox-"] button[aria-label="Close fullscreen"]) .st-key-table-notes { display: block; }
/* In fullscreen, Reassign and Notes are plain text and icon over the table: no box (its border would run into
   the table's top edge). */
body:has([class*="st-key-gridbox-"] button[aria-label="Close fullscreen"]) .st-key-table-bar [data-testid="stPopover"] button,
body:has([class*="st-key-gridbox-"] button[aria-label="Close fullscreen"]) .st-key-table-notes button {
    border-color: transparent !important; background: transparent !important; box-shadow: none !important;
}
body:has([class*="st-key-gridbox-"] button[aria-label="Close fullscreen"]) [data-baseweb="popover"] {
    z-index: 1000052 !important;
}
</style>
<script>
if (!window.reqfillTableFullscreen) {
    window.reqfillTableFullscreen = true;
    document.addEventListener("click", e => {
        if (!e.target.closest(".st-key-table-fullscreen button")) return;
        document.querySelector('[class*="st-key-gridbox-"] [data-testid="stElementToolbar"] '
                               + 'button[aria-label="Fullscreen"]')?.click();
    }, true);
}
</script>"""


# Go to row from the notes panel: scroll the table's own scroll area so the row sits a third of the way down
# (rows are all one height, so its place is in proportion to its index). Retried while the redrawn table settles.
_JUMP = """<script>
if (window.reqfillNotesJump !== NONCE) {
    window.reqfillNotesJump = NONCE;
    const go = () => {
        const box = document.querySelector('[class*="st-key-gridbox-"]');
        const scroller = box && box.querySelector('.dvn-scroller');
        if (!scroller) return;
        box.scrollIntoView({block: "nearest"});
        scroller.scrollTop = Math.max(0, INDEX / COUNT * scroller.scrollHeight - scroller.clientHeight / 3);
    };
    [150, 500, 1000, 1800].forEach(t => setTimeout(go, t));
}
</script>"""


def _reassign_rows(person: str, job_id: str, sheet: str, row_nums: list[int], key: str) -> None:
    """Give the ticked rows to someone else, or return them to the pool as not this person's, as the
    row-by-row Reassign does. They leave this person's table either way."""
    to = st.session_state.get(key)
    note = (st.session_state.pop(f"{key}:note", None) or "").strip()
    draft.add_note(job_id, sheet, row_nums, person, note)
    if to == UNASSIGN:
        for n in row_nums:
            draft.return_row(job_id, sheet, n, person, note)
        st.session_state.review_notice = f"{len(row_nums):,} row{'s' * (len(row_nums) != 1)} went back to the pool."
    else:
        draft.assign(job_id, sheet, row_nums, to)
        st.session_state.review_notice = (f"{len(row_nums):,} row{'s' * (len(row_nums) != 1)} handed to {to}."
                                          + _email_handoff(person, to, job_id, sheet, row_nums, note))
    st.session_state.pop(key, None)  # the picker starts empty next time
    review_grid.refresh()  # and the table's ticks start clear


def _table_review(person: str, job_id: str, sheet: str, view: pd.DataFrame, sheet_plan: dict) -> None:
    """The sheet's rows in an editable table across the full page width, narrowed by Show, Section and
    Module; tick rows to reassign them."""
    st.html("<style>[data-testid='stMainBlockContainer'] { max-width: none; padding-left: 5rem; "
            "padding-right: 2rem; }</style>")
    key = _table_key(person, job_id, sheet)
    rows = review_grid.with_sections(view.drop(columns=["who", "draft"]))
    f1, f2, f3 = st.columns([2, 3, 2])
    show = f1.selectbox("Show", review_grid.SHOW, key=f"{key}:show")
    section = review_grid.section_select(f2, rows, key=f"{key}:section")
    module = review_grid.module_select(f3, rows, key=f"{key}:module",
                                       counted=review_grid.in_section(review_grid.show_rows(rows, show), section))
    shown = review_grid.section_and_module(review_grid.show_rows(rows, show), section, module)
    if shown.empty:
        st.caption("No rows match.")
        return
    # Above the table: Wrap text and reassigning ticked rows on the left, Fullscreen on the right (all but
    # Wrap text drawn after the table, which says what's ticked). In fullscreen the bar stays (see _FULLSCREEN).
    bar = st.container(horizontal=True, vertical_alignment="center", gap="large", key="table-bar")
    wrap = bar.toggle("Wrap text", key=f"{key}:wrap", help="Taller rows that show the whole requirement and answers")
    ticked = review_grid.grid(job_id, sheet_plan, shown, key=f"{key}:{show}:{section}:{module}", height=640,
                              reviewer=True, selectable=True, wrap=wrap)
    which = f"{len(ticked):,} row{'s' * (len(ticked) != 1)}"
    with bar.popover(f"Reassign {which}", icon=":material/move_item:", disabled=not ticked,
                     help="Out of your area? Give these rows to someone else, or return them to the pool"
                     if ticked else "Tick rows in the table to reassign them"):
        covering = draft.partners()
        pick_key = f"{key}:reassign"
        choice = st.selectbox("Reassign to", [*[n for n in draft.people() if n != person], UNASSIGN], index=None,
                              placeholder="Pick a person", key=pick_key,
                              format_func=lambda n: f"{n} · partner" if n in covering else n)
        st.text_area("Note (optional)", key=f"{pick_key}:note", height=80,
                     placeholder="Why they're theirs, or what you need from them. Left on each row.")
        st.button("Accept", type="primary", width="stretch", disabled=choice is None,
                  on_click=_reassign_rows, args=(person, job_id, sheet, ticked, pick_key))
    # Notes, for the fullscreen table (where the page's own Notes button is out of sight): shown there only.
    panel, noted = _panel_key(job_id, sheet), int((view.notes > 0).sum())
    bar.button(f"Notes ({noted})" if noted else "Notes", icon=":material/forum:", key="table-notes",
               on_click=_toggle_panel, args=(panel,), help="Show or hide the notes on your rows of this sheet")
    bar.space("stretch")
    bar.button("", icon=":material/fullscreen:", type="tertiary", key="table-fullscreen", help="Fullscreen")
    st.html(_FULLSCREEN, unsafe_allow_javascript=True)
    if (jump := st.session_state.get("notes_jump")) and jump["row"] in set(shown.row_num):
        st.html(_JUMP.replace("NONCE", str(jump["n"])).replace("INDEX", str(list(shown.row_num).index(jump["row"])))
                .replace("COUNT", str(len(shown))), unsafe_allow_javascript=True)
    with st.container(border=True, key="card-answer"):  # as on the draft page
        st.subheader("Answer details")
        review_grid.details(shown, key=key)


# =================================================================================================
rows = _load()
person, job_id, sheet, mode = (st.query_params.get(k) for k in ("person", "draft", "sheet", "mode"))
if person and job_id and sheet:
    review_view(rows, person, job_id, sheet, mode)
    st.html(_TO_TOP.replace("NAV", str(st.session_state.get("review_nav", 0))), unsafe_allow_javascript=True)
elif person:
    person_view(rows, person)
else:
    people_list(rows)
