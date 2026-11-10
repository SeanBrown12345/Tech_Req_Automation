"""Notifications: email SMEs about the rows assigned to them, or create Microsoft Planner tasks for them, and set
when each SME's review of a draft is due. One line per SME per draft they have open rows in."""

from datetime import date, datetime

import pandas as pd
import streamlit as st

from app.common import as_date
from kb import draft, notify, planner

st.title("Notifications")
st.caption("Email SMEs about rows newly assigned to them, remind them of rows still open, or give them a Microsoft "
           "Planner task. Each line is one SME on one draft. A **Due** date goes in their emails, their Planner "
           "task and on their task board.")

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
        st.toast(text, icon=":material/check_circle:")

work = draft.sme_workload()
if work.empty:
    st.info("No SME has open rows right now. Assign rows from a draft's page.")
    st.stop()
work["draft"] = [f"{n} — {c}" if isinstance(c, str) and c.strip() else n for n, c in zip(work.draft_name, work.client)]



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
overdue = sum(1 for d in view.due if (as_date(d) or today) < today)
st.caption(f"{len(view):,} SME{'s' * (len(view) != 1)} with open rows · {int((view.new > 0).sum()):,} with rows "
           "they haven't been emailed about" + (f" · **{overdue} past their due date**" if overdue else ""))

# ---- The table: tick lines to email them; edit Due in place --------------------------------------------
board = planner.plan_url()   # "Open task" opens the plan's board, where the task sits in Next Due
shown = view.assign(send=False, email=view.email.fillna(""), due=[as_date(d) for d in view.due],
                    last=[_last(a, k) for a, k in zip(view.emailed_at, view.emailed_kind)],
                    planner=[board if isinstance(u, str) and u else None for u in view.planner_url])
columns = ["send", "person", "email", "draft", "open", "new", "last", "due", "planner", "job_id"]
edited = st.data_editor(
    shown[columns], key="notify-grid", hide_index=True, width="stretch",
    disabled=[c for c in columns if c not in ("send", "due")],
    column_config={
        "send": st.column_config.CheckboxColumn("", width="small", help="Tick to act on only these lines"),
        "person": st.column_config.TextColumn("SME"),
        "email": st.column_config.TextColumn("Email", help="Set under Admin › SMEs"),
        "draft": st.column_config.TextColumn("Draft", width="medium"),
        "open": st.column_config.NumberColumn("Open rows", help="Assigned to them, not yet approved"),
        "new": st.column_config.NumberColumn("New", help="Open rows they haven't been emailed about"),
        "last": st.column_config.TextColumn("Last emailed", width="medium"),
        "due": st.column_config.DateColumn("Due", format="YYYY-MM-DD", help="When their review is due; editable"),
        "planner": st.column_config.LinkColumn("Planner", display_text="Open task", width="small",
                                               help="A Planner task was created for them on this draft: opens the plan's board"),
        "job_id": None,
    })

# Due dates save as they're edited.
for i, t in enumerate(view.itertuples()):
    if as_date(edited.due.iloc[i]) != as_date(t.due):
        new_due = as_date(edited.due.iloc[i])
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
                                 covered.groupby("sheet", sort=False).size().to_dict(), len(rows), as_date(t.due))
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


@st.dialog("Create Planner tasks", width="large")
def _planner_tasks(targets: pd.DataFrame):
    cfg = planner.config()
    if not planner.ready():
        st.info("Connect Microsoft Planner first: sign in and pick a plan under **Planner settings**.")
        return
    st.write(f"One task per SME per draft in the plan **{cfg['plan_name'] or cfg['plan_id']}**, in the "
             f"**{planner.TASK_BUCKET}** bucket. Each is assigned to the SME, due on their due date, and links to their task board.")
    has_task = targets.planner.fillna("").astype(bool)
    again = False
    if has_task.any():
        n = int(has_task.sum())
        again = st.checkbox(f"Also for the {n} line{'s' * (n != 1)} that already {'has' if n == 1 else 'have'} "
                            "a task", value=False,
                            help="They'd get a second task")
    todo = targets if again else targets[~has_task]
    st.dataframe(todo[["person", "email", "draft", "open", "due"]].fillna(""), hide_index=True, width="stretch",
                 column_config={"person": "SME", "email": "Email", "draft": "Draft",
                                "open": st.column_config.NumberColumn("Rows"),
                                "due": st.column_config.DateColumn("Due", format="YYYY-MM-DD")})
    if missing := sorted(set(todo.person[~todo.email.fillna("").astype(bool)])):
        st.caption(f"No email address for **{', '.join(missing)}**: their task will be created unassigned "
                   "(add one under Admin › SMEs).")
    cancel, create = st.columns(2)
    if cancel.button("Cancel", width="stretch", key="planner-cancel"):
        st.rerun()
    if create.button(f"Create {len(todo)} task{'s' * (len(todo) != 1)}", type="primary", width="stretch",
                     disabled=todo.empty):
        bucket_id, made, failed = None, 0, {}
        names = {j: (n, c) for j, n, c in zip(work.job_id, work.draft_name, work.client)}
        with st.spinner("Creating Planner tasks…"):
            for t in todo.itertuples():
                try:
                    rows = draft.open_rows(t.job_id, t.person)
                    if rows.empty:
                        continue
                    if bucket_id is None:
                        bucket_id = planner.bucket(cfg["plan_id"])
                    assignee = planner.user_id(t.email)
                    due = as_date(t.due)
                    link = notify.board_link(t.person)
                    url = planner.create_task(
                        bucket_id, planner.review_title(*names[t.job_id]),
                        assignee=assignee, due=due.isoformat() if due else None,
                        description=planner.review_description(
                            t.person, t.draft, rows.groupby("sheet", sort=False).size().to_dict(),
                            notify.due_text(due), link, bool(assignee)),
                        link=(link, "ReqFill task board") if link else None,
                        label_names=[names[t.job_id][1], planner.TASK_LABEL])
                    draft.record_planner_task(t.job_id, t.person, url)
                    made += 1
                except planner.PlannerError as e:
                    failed[f"{t.person} ({t.draft})"] = str(e)
        text = f"Created {made} Planner task{'s' * (made != 1)}."
        if failed:
            st.session_state.notify_flash = ("warn", f"{text} Failed for {', '.join(failed)}: "
                                                     f"{next(iter(failed.values()))}")
        else:
            st.session_state.notify_flash = ("ok", text)
        st.session_state.pop("notify-grid", None)  # clear the ticks
        st.rerun()


def _planner_settings():
    """Sign in to Microsoft (device code) and choose the plan tasks go into."""
    cfg = planner.config()
    if planner.signed_in():
        st.success("Signed in to Microsoft. Tasks are created as that account.", icon=":material/check:")
        st.markdown("**Plan**")
        st.caption(f"Tasks go into: **{cfg['plan_name'] or cfg['plan_id'] or 'no plan chosen yet'}**")
        if st.button("Load my plans", width="stretch"):
            try:
                st.session_state.planner_plans = planner.list_plans()
            except planner.PlannerError as e:
                st.error(str(e))
        plans = st.session_state.get("planner_plans")
        if plans:
            chosen = st.selectbox("Your plans", plans, format_func=lambda p: p["title"])
            if st.button("Use this plan", type="primary", width="stretch"):
                planner.set_config(plan_id=chosen["id"], plan_name=chosen["title"])
                st.session_state.notify_flash = ("ok", f"Planner tasks will go into {chosen['title']}.")
                st.rerun()
        elif plans == []:
            st.caption("You don't belong to any plans.")
        ref = st.text_input("Or paste a plan's link", placeholder="A Planner link containing planId=…",
                            help="For a plan shared with you that isn't listed")
        if st.button("Use this link", width="stretch", disabled=not ref.strip()):
            try:
                plan = planner.set_plan(ref)
                st.session_state.notify_flash = ("ok", f"Planner tasks will go into {plan['title']}.")
                st.rerun()
            except planner.PlannerError as e:
                st.error(str(e))
        st.divider()
        if st.button("Sign out", width="stretch"):
            planner.sign_out()
            st.session_state.pop("planner_plans", None)
            st.rerun()
    elif device := st.session_state.get("planner_device"):
        st.markdown(f"1. Go to **{device.get('verification_uri', 'https://microsoft.com/devicelogin')}**\n"
                    f"2. Enter the code **`{device.get('user_code', '')}`**\n"
                    "3. Sign in with your work account, then come back here.")
        done, cancel = st.columns(2)
        if done.button("I've signed in", type="primary", width="stretch"):
            try:
                if planner.finish_sign_in(device.get("device_code", "")):
                    st.session_state.pop("planner_device", None)
                    st.rerun()
                st.info("Not signed in yet: finish in the browser, then try again.")
            except planner.PlannerError as e:
                st.session_state.pop("planner_device", None)
                st.error(str(e))
        if cancel.button("Cancel", width="stretch", key="planner-device-cancel"):
            st.session_state.pop("planner_device", None)
            st.rerun()
    else:
        st.caption("Sign in once with your Microsoft work account; tasks are created as you. See docs/planner.md.")
        if st.button("Sign in to Microsoft", type="primary", width="stretch"):
            try:
                st.session_state.planner_device = planner.begin_sign_in()
                st.rerun()
            except planner.PlannerError as e:
                st.error(str(e))


# ---- Actions: on the ticked lines, or on every line shown when none are ticked -------------------------
ticked = edited[edited.send]
base = ticked if not ticked.empty else edited
which = "ticked" if not ticked.empty else "shown"
new_lines, open_lines = base[base.new > 0], base
a1, a2, a3, a4 = st.columns(4)
if a1.button(f"Email new assignments ({len(new_lines)})", icon=":material/mail:", type="primary", width="stretch",
             disabled=new_lines.empty, help=f"To the {which} SMEs with rows they haven't been emailed about"):
    _confirm(notify.NEW, new_lines)
if a2.button(f"Send reminders ({len(open_lines)})", icon=":material/notifications:", width="stretch",
             help=f"To the {which} SMEs: every row they still have open, and when it's due"):
    _confirm(notify.REMINDER, open_lines)
if a3.button(f"Create Planner tasks ({len(open_lines)})", icon=":material/task_alt:", width="stretch",
             help=f"A Microsoft Planner task for each of the {which} SMEs, assigned to them and due on their due date"):
    _planner_tasks(open_lines)
with a4.popover("Planner settings", icon=":material/settings:", width="stretch"):
    _planner_settings()
