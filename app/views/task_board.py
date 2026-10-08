"""Task board: everyone with rows to review. Each person opens their own view, sees the sheets assigned to
them, and either browses a sheet's rows in a grid or reviews them one at a time.

No sign-in: people find themselves by name. The address carries where they are (?person=, then &draft=
and &sheet= in a review), so a link opens someone's view directly and a refresh keeps the place."""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app import review_grid
from app.admin_layout import ACCENT
from app.common import card_styles, status_badge
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
card_styles()


def _load() -> pd.DataFrame:
    conn = draft.connect_drafts()
    rows = db.read_sql(conn, "SELECT r.*, j.name AS draft_name, j.client, j.plan, j.created_at, j.filename FROM rows r"
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
    st.title("Task board")
    assigned = rows[rows.who.notna()]
    if assigned.empty:
        st.info("No one has rows to review yet. Assign rows from a draft's page: filter its review grid, "
                "then **Assign shown rows**.")
        return
    partner_names = set(draft.partners())
    smes, theirs = assigned[~assigned.who.isin(partner_names)], assigned[assigned.who.isin(partner_names)]
    if not smes.empty:
        st.caption("Open your name to see the sheets assigned to you.")
        _cards(smes, partner=False)
    if not theirs.empty:
        st.subheader("Partners")
        st.caption("Partners answer by email. Open a partner to export their worksheet, import their reply and "
                   "approve the answers they sent back.")
        _cards(theirs, partner=True)
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
    partner = person in draft.partners()
    if partner:
        partner_view(mine, person)
        return
    st.caption("Your assigned sheets. **Start review** takes you through the rows one at a time, "
               "least confident first, then the ones that need pricing.")
    for (job_id, sheet), view in mine.groupby(["job_id", "sheet"], sort=False):
        sheet_plan, multi = _sheet_plan(view, sheet)
        c = _counts(view)
        busy = c["left"]
        with st.container(border=True):
            title, action = st.columns([4, 1], vertical_alignment="center")
            title.markdown(f"#### {view.draft.iloc[0]}" + (f" · {sheet}" if multi else ""))
            label = "Start review" if c["left"] else "Review again"
            action.button(label, key=f"start:{job_id}:{sheet}",
                          icon=":material/play_arrow:", type="primary" if busy else "secondary",
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
        done = draft.approve_partner_answers(job_id, partner)
        st.session_state.partner_flash = f"Approved {done:,} answers from {partner}."
        review_grid.refresh()
        st.rerun()


def partner_view(mine: pd.DataFrame, person: str) -> None:
    """A partner's rows, per draft: export their trimmed worksheet, import their reply, approve the answers."""
    st.caption(f"{person} is a partner and answers by email. For each draft: **Export worksheet** gives the client's "
               "worksheet with only their rows showing, filled with our drafted answers; email it to them, then "
               "**import their reply**. **Review answers** takes you (the bid manager) through what they sent back.")
    if flash := st.session_state.pop("partner_flash", None):
        st.toast(flash, icon=":material/check:")
    for job_id, job_rows in mine.groupby("job_id", sort=False):
        pc, c = _partner_counts(job_rows), _counts(job_rows)
        unsent = c["rows"] - pc["sent"]
        with st.container(border=True):
            title, out = st.columns([3, 1], vertical_alignment="center")
            title.markdown(f"#### {job_rows.draft.iloc[0]}")
            last = max((t[:10] for t in job_rows.sent_at if isinstance(t, str)), default="")
            title.caption((f"Last sent {last}" if last else "Not sent yet")
                          + (f" · **{unsent:,} rows not sent yet**" if last and unsent else ""))
            out.download_button(
                "Export worksheet", lambda j=job_id: draft.build_partner_export(j, person),
                file_name=f"{Path(job_rows.filename.iloc[0]).stem} - {person}.xlsx", icon=":material/outgoing_mail:",
                width="stretch", type="primary" if unsent else "secondary", key=f"export:{job_id}:{person}",
                on_click=draft.mark_sent, args=(job_id, person),
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
            for sheet, view in job_rows.groupby("sheet", sort=False):
                sheet_plan, multi = _sheet_plan(view, sheet)
                sc = _counts(view)
                label = "Review answers" + (f" · {sheet}" if multi else "") if sc["left"] else "Review again"
                st.button(label, key=f"start:{job_id}:{sheet}", icon=":material/play_arrow:", type="secondary",
                          on_click=_start_review, args=(person, job_id, sheet, view))
                with st.expander("Browse the rows" + (f" · {sheet}" if multi else "")):
                    review_grid.grid(job_id, sheet_plan, view.drop(columns=["who", "draft"]),
                                     key=f"board:{person}:{job_id}:{sheet}")
                    st.markdown("**Answer details**")
                    review_grid.details(view, key=f"board:{person}:{job_id}:{sheet}")


# =================================================================================================
# Row-by-row review
# =================================================================================================
def _queue(view: pd.DataFrame, partner: bool = False) -> list[int]:
    """Review order: low confidence, then needs pricing, then medium and high confidence. For a partner's
    rows, answers they changed come first, then answers they sent back as drafted, then rows not answered
    yet. Rows already approved are left out unless everything is (then it's a second pass over them all)."""
    todo = view[view.reviewed == 0] if (view.reviewed == 0).any() else view

    def group(status, final, ai):
        if not partner:
            return 0
        if status != "received":
            return 2
        return 0 if isinstance(final, str) and json.loads(final) != json.loads(ai) else 1

    rank = [group(s, f, a) * 10 + (0 if c == "low" else 1 if p else 2 if c == "medium" else 3)
            for c, p, s, f, a in zip(todo.confidence, todo.cost_impact, todo.partner_status, todo.final, todo.ai)]
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
        rv = st.session_state.review = {"job": job_id, "sheet": sheet,
                                        "queue": _queue(view, person in draft.partners()), "pos": 0}
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

        for f in fields:
            key, value = _field_key(job_id, sheet, row_num, f["key"]), answers.get(f["key"]) or ""
            if f["kind"] in ("choice", "marks"):
                options = f["options"] if f["kind"] == "choice" else [f["labels"][c] for c in f["columns"]]
                st.selectbox(f["label"], options, index=options.index(value) if value in options else None,
                             key=key, on_change=_save_answers, args=nav)
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
            covering = draft.partners()
            choice = st.selectbox("Reassign to", [*[n for n in draft.people() if n != person], UNASSIGN], index=None,
                                  placeholder="Pick a person", key=_field_key(job_id, sheet, row_num, "_reassign"),
                                  format_func=lambda n: f"{n} · partner" if n in covering else n)
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
rows = _load()
person, job_id, sheet = (st.query_params.get(k) for k in ("person", "draft", "sheet"))
if person and job_id and sheet:
    review_view(rows, person, job_id, sheet)
elif person:
    person_view(rows, person)
else:
    people_list(rows)
