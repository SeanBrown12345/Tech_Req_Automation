"""Notifications: email SMEs about the rows assigned to them, and set when each SME's review of a draft is due.
One line per SME per draft they have open rows in."""

from datetime import date, datetime

import pandas as pd
import streamlit as st

from kb import draft, notify

st.title("Notifications")
st.caption("Email SMEs about rows newly assigned to them, or remind them of rows still open. Each line is one SME "
           "on one draft. A **Due** date goes in their emails and on their task board.")

if not notify.configured():
    st.info("Email isn't set up on this server: set `NOTIFY_WEBHOOK_URL` (a Power Automate flow, see "
            "docs/email-notifications.md) or `SMTP_HOST` and `SMTP_FROM`.")
elif not notify.BASE_URL:
    st.caption("`REQFILL_BASE_URL` isn't set, so emails won't link to the task board.")

if flash := st.session_state.pop("notify_flash", None):
    kind, text = flash
    if kind == "warn":
        st.warning(text)
    else:
        st.toast(text, icon=":material/mail:")

work = draft.sme_workload()
if work.empty:
    st.info("No SME has open rows right now. Assign rows from a draft's page.")
    st.stop()
work["draft"] = [f"{n} — {c}" if isinstance(c, str) and c.strip() else n for n, c in zip(work.draft_name, work.client)]


def _day(value) -> date | None:
    """A date from the table (date, Timestamp, ISO text or empty)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return date.fromisoformat(value) if isinstance(value, str) else getattr(value, "date", lambda: value)()


def _last(at, kind) -> str:
    if not isinstance(at, str):
        return "Never"
    sent = datetime.fromisoformat(at)
    return f"{sent:%b} {sent.day}, {sent:%H:%M} UTC · {'reminder' if kind == notify.REMINDER else 'new rows'}"


# ---- Filter, and one due date for a whole draft ------------------------------------------------------
f1, f2 = st.columns([3, 2], vertical_alignment="bottom")
drafts = list(dict.fromkeys(work.draft))
pick = f1.selectbox("Draft", ["All drafts", *drafts], key="notify-draft")
view = (work if pick == "All drafts" else work[work.draft == pick]).reset_index(drop=True)
if pick != "All drafts":
    with f2.popover("Set due date for everyone", icon=":material/event:", width="stretch"):
        everyone = st.date_input("Due", value=None, format="YYYY-MM-DD", key="notify-due-all")
        if st.button("Apply to all SMEs on this draft", type="primary", disabled=not everyone, width="stretch"):
            for t in view.itertuples():
                draft.set_due(t.job_id, t.person, everyone.isoformat())
            st.session_state.pop("notify-grid", None)
            st.rerun()

today = date.today()
overdue = sum(1 for d in view.due if (_day(d) or today) < today)
st.caption(f"{len(view):,} SME{'s' * (len(view) != 1)} with open rows · {int((view.new > 0).sum()):,} with rows "
           "they haven't been emailed about" + (f" · **{overdue} past their due date**" if overdue else ""))

# ---- The table: tick lines to email them; edit Due in place --------------------------------------------
shown = view.assign(send=False, email=view.email.fillna(""), due=[_day(d) for d in view.due],
                    last=[_last(a, k) for a, k in zip(view.emailed_at, view.emailed_kind)])
columns = ["send", "person", "email", "draft", "open", "new", "last", "due", "job_id"]
edited = st.data_editor(
    shown[columns], key="notify-grid", hide_index=True, width="stretch",
    disabled=[c for c in columns if c not in ("send", "due")],
    column_config={
        "send": st.column_config.CheckboxColumn("", width="small", help="Tick to email only these lines"),
        "person": st.column_config.TextColumn("SME"),
        "email": st.column_config.TextColumn("Email", help="Set under Admin › SMEs"),
        "draft": st.column_config.TextColumn("Draft", width="medium"),
        "open": st.column_config.NumberColumn("Open rows", help="Assigned to them, not yet approved"),
        "new": st.column_config.NumberColumn("New", help="Open rows they haven't been emailed about"),
        "last": st.column_config.TextColumn("Last emailed", width="medium"),
        "due": st.column_config.DateColumn("Due", format="YYYY-MM-DD", help="When their review is due; editable"),
        "job_id": None,
    })

# Due dates save as they're edited.
for i, t in enumerate(view.itertuples()):
    if _day(edited.due.iloc[i]) != _day(t.due):
        new_due = _day(edited.due.iloc[i])
        draft.set_due(t.job_id, t.person, new_due.isoformat() if new_due else None)
        st.toast(f"Due date saved for {t.person}.", icon=":material/event:")


@st.dialog("Send emails", width="large")
def _confirm(kind: str, targets: pd.DataFrame):
    count = "new" if kind == notify.NEW else "open"
    st.write("**New assignments**: the rows each SME hasn't been emailed about yet." if kind == notify.NEW else
             "**Reminders**: every row each SME still has open, and when it's due.")
    st.dataframe(targets[["person", "email", "draft", count, "due"]].fillna(""), hide_index=True, width="stretch",
                 column_config={"person": "SME", "email": "Email", "draft": "Draft",
                                count: st.column_config.NumberColumn("Rows"),
                                "due": st.column_config.DateColumn("Due", format="YYYY-MM-DD")})
    has_email = targets.email.fillna("").astype(bool)
    reachable = targets[has_email]
    if missing := sorted(set(targets.person[~has_email])):
        st.warning(f"No email address for **{', '.join(missing)}**: add one under Admin › SMEs.")
    cancel, send = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if send.button(f"Send {len(reachable)} email{'s' * (len(reachable) != 1)}", type="primary", width="stretch",
                   disabled=reachable.empty or not notify.configured()):
        batch = []  # (line, message, the rows it covers)
        for t in reachable.itertuples():
            rows = draft.open_rows(t.job_id, t.person)
            covered = rows[rows.new == 1] if kind == notify.NEW else rows
            if covered.empty:
                continue
            msg = notify.message(kind, t.person, t.email, t.draft,
                                 covered.groupby("sheet", sort=False).size().to_dict(), len(rows), _day(t.due))
            batch.append((t, msg, covered))
        try:
            failed = notify.send([msg for _, msg, _ in batch])
        except notify.SEND_ERRORS as e:
            st.error(f"Couldn't send: {e}")
            return
        for i, (t, _, covered) in enumerate(batch):
            if i not in failed:
                draft.record_email(t.job_id, t.person, kind, list(zip(covered.sheet, covered.row_num)))
        sent = len(batch) - len(failed)
        text = f"Sent {sent} email{'s' * (sent != 1)}."
        if failed:
            who = ", ".join(f"{batch[i][0].person} ({batch[i][0].draft})" for i in failed)
            st.session_state.notify_flash = ("warn", f"{text} Failed for {who}: {next(iter(failed.values()))}")
        else:
            st.session_state.notify_flash = ("ok", text)
        st.session_state.pop("notify-grid", None)  # clear the ticks
        st.rerun()


# ---- Actions: on the ticked lines, or on every line shown when none are ticked -------------------------
ticked = edited[edited.send]
base = ticked if not ticked.empty else edited
which = "ticked" if not ticked.empty else "shown"
new_lines, open_lines = base[base.new > 0], base
a1, a2, _ = st.columns([1, 1, 2])
if a1.button(f"Email new assignments ({len(new_lines)})", icon=":material/mail:", type="primary", width="stretch",
             disabled=new_lines.empty, help=f"To the {which} SMEs with rows they haven't been emailed about"):
    _confirm(notify.NEW, new_lines)
if a2.button(f"Send reminders ({len(open_lines)})", icon=":material/notifications:", width="stretch",
             help=f"To the {which} SMEs: every row they still have open, and when it's due"):
    _confirm(notify.REMINDER, open_lines)
