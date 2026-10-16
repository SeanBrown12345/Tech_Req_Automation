import hashlib
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app import review_grid
from app.admin_layout import ACCENT
from app.common import card_styles
from kb import db, draft, modules

KIND_LABELS = {"choice": "Pick from list", "text": "Free text", "marks": "Mark one column"}

# Keep the page readable on wide screens instead of stretching edge to edge.
st.html("<style>[data-testid='stMainBlockContainer'] { max-width: 1200px; margin: 0 auto; }</style>")


@st.cache_data(show_spinner="Reading workbook...", max_entries=3)
def _read(data: bytes) -> dict:
    return draft.read_workbook(data)


@st.cache_data(show_spinner="Working out how to fill this worksheet...", max_entries=6)
def _plan(data: bytes, header_rows: tuple) -> dict:
    return draft.guess_plan(data, _read(data), dict(header_rows))


def _toggle_auto_approve(job_id: str, rule: dict, key: str) -> None:
    """The tick box on an existing draft: switching auto-approve on or off applies at once."""
    n = draft.set_auto_approve(job_id, rule | {"enabled": st.session_state[key]})
    st.session_state.auto_note = (f"Auto-approve now covers {n:,} rows." if st.session_state[key]
                                  else "Auto-approve is off; its approvals went back to Needs Review.")
    review_grid.refresh()


def _auto_approve(rule: dict, key: str, job_id: str | None = None) -> dict:
    """An Auto-approve tick box beside a dropdown holding which rows it approves and how similar a past answer
    must be. On an existing draft (`job_id`) the tick box applies at once and the dropdown has Apply; for a
    new draft the picked rule is just returned."""
    row = st.container(horizontal=True, vertical_alignment="center", gap="small")
    enabled = row.checkbox(
        "Auto-approve", value=rule["enabled"], key=f"{key}:on",
        on_change=_toggle_auto_approve if job_id else None, args=(job_id, rule, f"{key}:on") if job_id else None,
        help="Approve rows without a person when the AI is sure and a past answer is nearly identical. Rows "
             "flagged for pricing always need a person, and anything a person approves or unapproves is left alone.")
    mode = st.session_state.get(f"{key}:mode", rule["mode"])
    sim = st.session_state.get(f"{key}:sim", round(rule["min_sim"] * 100))
    # The dropdown is wide enough for the longest option name.
    st.html('<style>[data-testid="stPopoverBody"]:has([class*="st-key-auto"][class*="-mode"]) '
            '{ min-width: 27rem; }</style>')
    with row.popover("Configure", key=f"{key}:menu",
                     help=f"Approves: {draft.AUTO_MODES[mode]}, {sim}% similar or more"):
        mode = st.selectbox(
            "Approve", list(draft.AUTO_MODES), index=list(draft.AUTO_MODES).index(rule["mode"]),
            format_func=draft.AUTO_MODES.get, key=f"{key}:mode",
            help="**High confidence**: any row the AI is sure of.  \n"
                 "**Standard**: only rows answered as standard functionality (or a plain Yes).  \n"
                 "**Standard, nothing written**: and with no comment or narrative.")
        sim = st.slider("Closest past answer at least", 85, 100, round(rule["min_sim"] * 100),
                        format="%d%% similar", key=f"{key}:sim",
                        help="High confidence already needs 85%. 95% means the past requirement is all but identical.")
        picked = {"enabled": enabled, "mode": mode, "min_sim": sim / 100}
        if job_id and st.button("Apply", type="primary", width="stretch", disabled=picked == rule,
                                key=f"{key}:apply"):
            n = draft.set_auto_approve(job_id, picked)
            st.session_state.auto_note = (f"Auto-approve now covers {n:,} rows." if enabled
                                          else "Saved; tick Auto-approve to use it.")
            review_grid.refresh()
            st.rerun()
    return picked


def _jobs() -> pd.DataFrame:
    conn = draft.connect_drafts()
    try:
        return db.read_sql(conn, "SELECT * FROM jobs ORDER BY created_at DESC")
    finally:
        conn.close()


# ---- Pick a job --------------------------------------------------------------------------------
jobs = _jobs()
NEW = "➕ New draft"
labels = {NEW: ":material/add: New draft"} | {j.job_id: f"{j.name} — {j.client if isinstance(j.client, str) and j.client else 'no client'} ({j.created_at[:10]})" for j in jobs.itertuples()}
if "draft_job_next" in st.session_state:  # set by Start / Delete: switch before the picker is drawn
    st.session_state.draft_job = st.session_state.pop("draft_job_next")
if "draft_job" not in st.session_state or st.session_state.draft_job not in labels:
    st.session_state.draft_job = NEW
choice = st.session_state.draft_job
# An existing draft is titled by its name and client; "New Draft" only fits starting a new one.
if choice == NEW:
    st.title("New Draft")
else:
    current = jobs.set_index("job_id").loc[choice]
    has_client = isinstance(current.client, str) and current.client.strip()
    st.title(f"{current['name']} — {current.client}" if has_client else current["name"])


@st.dialog("Delete draft?")
def _confirm_delete(job_id: str):
    st.write(f"**{labels[job_id]}**")
    st.write("Its drafted answers and review edits will be deleted. This can't be undone.")
    cancel, delete = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if delete.button("Delete permanently", type="primary", width="stretch"):
        draft.delete_job(job_id)
        if job_id == choice:
            st.session_state.draft_job_next = NEW
        st.rerun()


def _email_note(job_id: str) -> None:
    """Assigning doesn't email anyone: a reminder that SMEs here haven't been told, linking to where they are."""
    work = draft.sme_workload()
    pending = work[(work.job_id == job_id) & (work.new > 0)]
    if pending.empty:
        return
    rows, smes = int(pending.new.sum()), len(pending)
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f":material/mail: **{rows:,}** newly assigned row{'s' * (rows != 1)} for {smes} "
                   f"SME{'s' * (smes != 1)} not emailed yet", width="content")
        st.page_link("views/notifications.py", label="Notifications", icon=":material/arrow_forward:")


def _pick(job_id: str):
    st.session_state.draft_job = job_id
    st.session_state.draft_picker = False  # close the list


def _ask_delete(job_id: str):
    st.session_state.draft_delete = job_id
    st.session_state.draft_picker = False  # close the list so it doesn't sit over the dialog


# A popover instead of a selectbox, so each row can carry its own delete button. The CSS makes it
# look like Streamlit's selectbox: grey 40px field, white menu with 40px rows and a hover highlight.
HOVER = "rgba(151, 166, 195, 0.15)"
st.html(f"""<style>
.st-key-draft-field {{ gap: 0.25rem; }}
.st-key-draft-field > [data-testid="stElementContainer"]:first-child [data-testid="stMarkdownContainer"] {{ margin-bottom: 0; }}
.st-key-draft-field > [data-testid="stElementContainer"]:first-child p {{ font-size: 14px; line-height: 24px; margin: 0; }}
.st-key-draft_picker [data-testid="stPopoverButton"] [data-testid="stIconMaterial"] {{ font-size: 20px; }}
.st-key-draft_picker [data-testid="stPopoverButton"] {{
    min-height: 40px; padding: 0 8px; border-radius: 8px; background: {HOVER}; border: 1px solid transparent;
    font-size: 14px;
}}
.st-key-draft_picker [data-testid="stPopoverButton"]:hover {{ border-color: transparent; color: inherit; }}
.st-key-draft_picker [data-testid="stPopoverButton"][aria-expanded="true"] {{ border-color: #2a78d6; }}
.st-key-draft_picker [data-testid="stPopoverButton"] > div {{ flex: 1; display: flex; justify-content: space-between; }}
.st-key-draft_picker [data-testid="stPopoverButton"] > div > div {{ justify-content: flex-start; text-align: left; }}
.st-key-draft_picker [data-testid="stPopoverButton"] p {{ font-size: 14px; }}
.st-key-draft-field [data-testid="stMarkdownContainer"] [role="img"],
.st-key-draft-rows [data-testid="stMarkdownContainer"] [role="img"] {{
    color: {ACCENT}; font-size: 1.25em; font-variation-settings: "wght" 600; vertical-align: -0.2em !important;
}}

[data-testid="stPopoverBody"]:has(.st-key-draft-rows) {{
    padding: 0; border: none; border-radius: 8px; box-shadow: rgba(0, 0, 0, 0.16) 0 4px 16px;
}}
.st-key-draft-rows {{ gap: 0; padding: 0 5px; box-sizing: border-box; }}
.st-key-draft-rows [data-testid="stHorizontalBlock"] {{
    gap: 0.25rem; flex-wrap: nowrap; height: 28px; margin: 6px 0; padding: 0 8px; border-radius: 6px;
}}
.st-key-draft-rows [data-testid="stHorizontalBlock"]:hover {{ background: {HOVER}; }}
.st-key-draft-rows [data-testid="stColumn"] {{ min-width: 0 !important; flex: 1 1 auto !important; width: auto !important; }}
.st-key-draft-rows [data-testid="stColumn"]:last-child {{ flex: 0 0 1.5rem !important; }}
.st-key-draft-rows button {{
    min-height: 0; height: 28px; padding: 0; justify-content: flex-start; text-align: left;
    font-weight: 400; color: inherit;
}}
.st-key-draft-rows button > div {{ justify-content: flex-start; }}
.st-key-draft-rows button p {{ font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.st-key-draft-rows button:hover {{ color: inherit; }}
.st-key-draft-rows [class*="st-key-delete"] button:hover {{ color: #c62828; }}

/* Section headings (st.subheader) are underlined like the admin pages' headings, so each section's
   start stands out. */
[data-testid="stMain"] [data-testid="stHeading"] h3 {{
    padding: 0 0 0.6rem; border-bottom: 1px solid rgba(128, 128, 128, 0.25); margin: 0.75rem 0 0.5rem;
}}
/* The page title gets the same rule under it. */
[data-testid="stMain"] [data-testid="stHeading"] h1 {{
    padding-bottom: 0.6rem; border-bottom: 1px solid rgba(128, 128, 128, 0.25); margin-bottom: 0.5rem;
}}
</style>""")
card_styles()  # an existing draft's sections (results, review, answer details) each sit on a card
with st.container(key="draft-field"):
    st.markdown("Draft")
    with st.popover(labels[choice], width="stretch", key="draft_picker", on_change="rerun"), \
            st.container(key="draft-rows"):
        for job_id, label in labels.items():
            pick, trash = st.columns([12, 1], vertical_alignment="center")
            pick.button(label, key=f"pick:{job_id}", type="tertiary", width="stretch", on_click=_pick, args=(job_id,))
            if job_id != NEW:
                running = draft.is_running(job_id)
                trash.button("", key=f"delete:{job_id}", icon=":material/delete:", type="tertiary", disabled=running,
                             help="Stop the draft before deleting it" if running else "Delete",
                             on_click=_ask_delete, args=(job_id,))
if (pending := st.session_state.pop("draft_delete", None)) in labels:
    _confirm_delete(pending)


# =================================================================================================
# New draft
# =================================================================================================
def new_draft():
    uploaded = st.file_uploader("Blank worksheet (.xlsx / .xlsm)", type=["xlsx", "xlsm"], key="draft_upload")
    if not uploaded:
        return
    data = uploaded.getvalue()
    fhash = hashlib.sha256(data).hexdigest()[:12]
    k = lambda name: f"d:{fhash}:{name}"

    c1, c2 = st.columns(2)
    name = c1.text_input("Draft name", value=Path(uploaded.name).stem, key=k("name"))
    client = c2.text_input("Client", key=k("client"), placeholder="e.g. City of Springfield")

    # Header-row overrides feed back into the detected plan.
    overrides = st.session_state.setdefault(k("hdr"), {})
    plan = json.loads(json.dumps(_plan(data, tuple(sorted(overrides.items())))))
    if not plan["sheets"]:
        st.error("Couldn't find a requirements table in this workbook.")
        return

    st.subheader("What to fill")
    st.caption("Check the detected layout. **Pick from list** answers must be one of the listed options "
               "(taken from the worksheet's dropdowns). Add notes to steer a column, e.g. "
               "\"Only comment when the answer is not Y\".")
    for tab, sp in zip(st.tabs([s["name"] for s in plan["sheets"]]), plan["sheets"]):
        with tab:
            a, b, c = st.columns([1, 1, 3])
            sp["include"] = a.toggle("Fill this sheet", value=sp["include"], key=k(f"{sp['name']}:inc"))
            new_hdr = b.number_input("Header row", 1, 200, sp["header_row"], key=k(f"{sp['name']}:hdr"))
            if new_hdr != sp["header_row"]:
                overrides[sp["name"]] = int(new_hdr)
                st.rerun()
            c.caption(f"Requirement text: column **{sp['columns'].get('requirement')}**"
                      + (f" · ID: **{sp['columns']['req_id']}**" if sp["columns"].get("req_id") else ""))
            if not sp["include"]:
                continue
            df = pd.DataFrame([{
                "Fill": f["include"], "Column": ", ".join(f["columns"]), "Header": f["label"],
                "Type": KIND_LABELS[f["kind"]],
                "Options": " | ".join(f.get("options") or [f["labels"][c] for c in f["columns"]]
                                      if f["kind"] == "marks" else f.get("options") or []),
                "Guidance": f.get("guidance", "")} for f in sp["fields"]])
            edited = st.data_editor(
                df, hide_index=True, width="stretch", key=k(f"{sp['name']}:fields:{sp['header_row']}"),
                disabled=["Column", "Header"],
                column_config={
                    "Fill": st.column_config.CheckboxColumn(width="small"),
                    "Type": st.column_config.SelectboxColumn(options=list(KIND_LABELS.values()), required=True),
                    "Options": st.column_config.TextColumn("Response Options", help="Allowed answers, separated by |"),
                    "Guidance": st.column_config.TextColumn("Notes", width="large")})
            for f, row in zip(sp["fields"], edited.itertuples()):
                f["include"] = bool(row.Fill)
                f["guidance"] = row.Guidance or ""
                kind = next(key for key, lab in KIND_LABELS.items() if lab == row.Type)
                if f["kind"] != "marks":
                    f["kind"] = "choice" if kind == "choice" else "text"
                    opts = [o.strip() for o in (row.Options or "").split("|") if o.strip()]
                    if f["kind"] == "choice":
                        f["options"] = opts
                        if not opts:
                            st.warning(f"Column {f['columns'][0]} is *Pick from list* but has no response options.")

    st.subheader("Instructions")
    plan["instructions"] = st.text_area(
        "What the client's workbook says about how to respond (edit freely - the AI follows this)",
        value=plan["instructions"], height=220, key=k("instructions"))

    auto = _auto_approve(draft.AUTO_DEFAULT, k("auto"))

    rows = draft.collect_rows(plan, _read(data))
    no_options =[f"{sp['name']} column {f['columns'][0]}" for sp in plan["sheets"] if sp["include"]
                  for f in sp["fields"] if f["include"] and f["kind"] == "choice" and not f.get("options")]
    if no_options:
        st.error("Add response options (or switch to free text) before starting: " + ", ".join(no_options))
    st.write(f"**{len(rows):,}** requirement rows have empty answer cells to fill"
             f" · about {max(1, round(len(rows) / 100))} min")
    if st.button("Start drafting", type="primary", disabled=not rows or bool(no_options)):
        job_id, _ = draft.create_job(name.strip() or uploaded.name, client.strip() or None, uploaded.name, data, plan,
                                     auto)
        draft.start_job(job_id)
        st.session_state.draft_job_next = job_id
        st.rerun()


# =================================================================================================
# Existing job
# =================================================================================================
def show_job(job_id: str):
    _job_page(job_id)
    # Last, so it ends the page even when the page above stops early (no rows yet, Errors view...).
    job = jobs.set_index("job_id").loc[job_id]
    st.caption(f"{job.filename} · created {job.created_at[:16].replace('T', ' ')} UTC · model {job.model}")


def _response_rules(job_id: str, plan: dict) -> None:
    """The responses each sheet wants a comment with (see draft.required_comment), editable; SMEs see them
    beside the response and can't approve such a row with an empty comment."""
    choices = [(sp, f) for sp in plan["sheets"] if sp.get("include", True) and draft.comment_field(sp)
               for f in sp["fields"] if f.get("include", True) and f["kind"] == "choice" and f.get("options")]
    if not choices:
        return
    count = sum(len(f.get("requires") or {}) for _, f in choices)
    label = (f"Responses that need a comment ({count})" if count else
             "Responses that need a comment" + ("" if plan.get("rules_read") else " (not read yet)"))
    with st.expander(label, icon=":material/edit_note:"):
        st.caption("Read from the worksheet instructions. SMEs see the note beside the response, and a row with "
                   "that response can't be approved while its comment is empty. Clear a note to drop the rule.")
        for sp, f in choices:
            if len(choices) > 1:
                st.markdown(f"**{sp['name']} · {f['label']}**")
            requires = f.get("requires") or {}
            frame = pd.DataFrame({"Response": f["options"], "Comment must include": [requires.get(o, "") for o in
                                                                                     f["options"]]})
            key = f"rules:{job_id}:{sp['name']}:{f['key']}:{st.session_state.get('rules_rev', 0)}"
            edited = st.data_editor(frame, hide_index=True, width="stretch", key=key, disabled=["Response"],
                                    column_config={"Comment must include": st.column_config.TextColumn(
                                        width="large", help="Leave empty when this response needs no comment")})
            new = dict(zip(edited.Response, edited["Comment must include"].fillna("")))
            if {o: n.strip() for o, n in new.items() if n.strip()} != requires:
                draft.set_rules(job_id, sp["name"], f["key"], new)
                review_grid.refresh()
                st.rerun()
        if st.button("Read again from instructions" if plan.get("rules_read") else "Read from instructions",
                     icon=":material/auto_awesome:", key=f"rules-read:{job_id}",
                     help="Replaces the notes above with what the AI reads in the worksheet instructions"):
            with st.spinner("Reading the instructions..."):
                draft.save_plan(job_id, draft.read_rules(plan))
            draft.apply_auto_approve(job_id)
            st.session_state.rules_rev = st.session_state.get("rules_rev", 0) + 1
            review_grid.refresh()
            st.rerun()


def _job_page(job_id: str):
    if draft.reconcile(job_id):  # was left "running" by a restarted server
        st.rerun()
    job = jobs.set_index("job_id").loc[job_id]
    plan = json.loads(job.plan)

    @st.fragment(run_every=3 if job.status in ("running", "stopping") else None)
    def progress():
        conn = draft.connect_drafts()
        j = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.close()
        running = draft.is_running(job_id)
        if not running and job.status in ("running", "stopping"):
            st.rerun(scope="app")  # finished: refresh the review grid
        if not running and j["done"] >= j["total"]:
            return  # fully drafted: no progress bar
        cols = st.columns([5, 1])
        with cols[0]:
            st.progress(j["done"] / max(j["total"], 1), text=f"{j['done']:,} of {j['total']:,} rows drafted"
                        + (" — drafting..." if running else ""))
        with cols[1]:
            if running:
                if st.button("Stop", width="stretch"):
                    draft.stop_job(job_id)
                    st.rerun()
            elif j["done"] < j["total"]:
                if st.button("Resume", type="primary", width="stretch"):
                    draft.start_job(job_id)
                    st.rerun()
        if j["message"]:
            st.warning(j["message"])

    conn = draft.connect_drafts()
    rows = db.read_sql(conn, "SELECT * FROM rows WHERE job_id = ? ORDER BY sheet, row_num", [job_id])
    conn.close()
    done = rows[rows.ai.notna()]

    # ---- Draft results ----
    with st.container(border=True, key="card-draft"):
        st.subheader("Draft results")
        progress()

        if done.empty:
            return

        m = st.columns(3)
        m[0].metric("Drafted", f"{len(done):,}")
        m[1].metric("Low confidence", int((done.confidence == "low").sum()), help="Highlighted red in the download")
        m[2].metric("Needs pricing", int(done.cost_impact.sum()),
                    help="May add implementation cost. Highlighted yellow in the download")

        # ---- Download ----
        st.html(f"""<style>
.st-key-draft-download [data-testid="stDownloadButton"] button[kind="primary"] {{ background: {ACCENT}; border-color: {ACCENT}; color: #fff; }}
.st-key-draft-download [data-testid="stDownloadButton"] button[kind="primary"]:hover {{ filter: brightness(1.15); color: #fff; }}
</style>""")
        with st.container(border=True, key="draft-download"):
            st.markdown("**Download the filled worksheet**")
            d1, d2, d3, d4 = st.columns([3, 3, 2, 2], vertical_alignment="center")
            red = d1.toggle("Red: low-confidence answers", value=True, key=f"red:{job_id}",
                            help="Fills the answer cells of low-confidence rows.")
            yellow = d2.toggle("Yellow: needs pricing", value=True, key=f"yellow:{job_id}",
                               help="Fills the requirement cell of rows that may add implementation cost.")
            # The file is built when clicked, so it always has the latest edits and highlight choices.
            d4.download_button("Download", lambda: draft.build_output(job_id, red, yellow), type="primary",
                               file_name=f"{Path(job.filename).stem} - DRAFT.xlsx", width="stretch",
                               icon=":material/download:", on_click="ignore",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            pricing = int(done.cost_impact.sum())
            d3.download_button("Pricing rows", lambda: draft.build_pricing_export(job_id), width="stretch",
                               file_name=f"{Path(job.filename).stem} - PRICING.xlsx", disabled=not pricing,
                               icon=":material/request_quote:", on_click="ignore",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               help=f"Only the {pricing:,} rows that need pricing, filled with our drafts; "
                                    "every other row hidden." if pricing else "No rows need pricing")

    # ---- Review grid ----
    with st.container(border=True, key="card-review"):
        st.subheader("Review")
        # Progress across the whole draft (every sheet), before the filters narrow the grid.
        approved = int(done.reviewed.sum())
        auto_approved = int((done.approved_by == "auto").sum())
        auto_only = review_grid.auto_only(done)
        unassigned = done.assignee.isna() & ~auto_only
        r = st.columns(4)
        r[0].metric("Approved", f"{approved:,}", help=f"Rows an SME has approved, plus {auto_approved:,} approved "
                                                       "automatically (Show › Auto-approved lists them)")
        r[1].metric("Needs Review", f"{len(done) - approved:,}", help="Rows still waiting for review")
        r[2].metric("Auto-approved", f"{int(auto_only.sum()):,}",
                    help="Approved by the auto-approve rule with no SME assigned; Assigned to › Auto-approved "
                         "lists them")
        r[3].metric("Unassigned", f"{int(unassigned.sum()):,}",
                    help="Rows no SME has and the rule didn't approve: never assigned, or sent back by an SME as "
                         "out of their area (Assigned to › Unassigned by an SME lists those)")
        with st.container(key="auto-approve"):
            _auto_approve(draft.auto_approve_rule(job), f"auto:{job_id}", job_id)
        _response_rules(job_id, plan)
        if note := st.session_state.pop("auto_note", None):
            st.toast(note, icon=":material/check_circle:")
        if modules.load() and (undetected := int(done.module.isna().sum())):
            with st.container(border=True, horizontal=True, vertical_alignment="center"):
                st.markdown(f"**{undetected:,}** rows don't have a module yet. Detecting them lets you filter and "
                            "assign by module.", width="stretch")
                if st.button("Detect modules", icon=":material/category:", type="primary", key=f"modules:{job_id}"):
                    with st.status("Detecting modules...") as status:
                        bar = st.progress(0.0)
                        missing = draft.detect_modules(job_id, progress=lambda d, t: bar.progress(d / t))
                        status.update(label="Modules detected", state="complete")
                    if missing:
                        st.session_state.modules_note = f"{missing:,} rows couldn't be classified; try again."
                    review_grid.refresh()
                    st.rerun()
        if note := st.session_state.pop("modules_note", None):
            st.warning(note)
        sheets = [s for s in dict.fromkeys(done.sheet)]
        # The Sheet picker only exists for multi-sheet drafts; without it, Show and Find start at the left.
        if len(sheets) > 1:
            f1, f2, f3 = st.columns([2, 2, 3])
            sheet = f1.selectbox("Sheet", sheets, key=f"sheet:{job_id}")
        else:
            f2, f3, _ = st.columns([2, 3, 2])
            sheet = sheets[0]
        show = f2.selectbox("Show", [*review_grid.SHOW, "Errors"],
                            key=f"show:{job_id}")
        text = f3.text_input("Find", key=f"find:{job_id}", placeholder="Search requirement text")

        sp = next(s for s in plan["sheets"] if s["name"] == sheet)
        view = rows[rows.sheet == sheet].copy()
        if show == "Errors":
            view = view[view.error.notna() & view.ai.isna()]
            st.dataframe(view[["row_num", "req_id", "requirement", "error"]], hide_index=True, width="stretch")
            _email_note(job_id)
            return
        view = view[view.ai.notna()]

        # Sections and assignees narrow the grid, so a block of rows can be handed to one SME in one go.
        view = review_grid.with_sections(view)
        g1, gm, g2 = st.columns([3, 2, 2], vertical_alignment="bottom")
        section = review_grid.section_select(g1, view, key=f"section:{job_id}:{sheet}")
        module = review_grid.module_select(gm, view, key=f"module:{job_id}:{sheet}",
                                           counted=review_grid.in_section(review_grid.show_rows(view, show), section))
        UNASSIGNED, RETURNED, AUTO = "Never assigned", "Unassigned by an SME", "Auto-approved"
        auto_only = review_grid.auto_only(view)
        n_returned = int((view.assignee.isna() & view.returned_by.notna() & ~auto_only).sum())
        n_auto = int(auto_only.sum())
        who = g2.selectbox("Assigned to", ["Anyone", UNASSIGNED, RETURNED, AUTO, *draft.people()], key=f"who:{job_id}",
                           format_func=lambda o: f"{o} ({n_returned})" if o == RETURNED
                           else f"{o} ({n_auto})" if o == AUTO else o,
                           help="**Unassigned by an SME**: rows an SME sent back as out of their area, waiting to be "
                                "reassigned. Kept apart from rows never assigned. **Auto-approved**: rows the "
                                "auto-approve rule approved that no SME has.")

        view = review_grid.section_and_module(review_grid.show_rows(view, show), section, module)
        if who == UNASSIGNED:
            view = view[view.assignee.isna() & view.returned_by.isna() & ~review_grid.auto_only(view)]
        elif who == RETURNED:
            view = view[view.assignee.isna() & view.returned_by.notna() & ~review_grid.auto_only(view)]
        elif who == AUTO:
            view = view[review_grid.auto_only(view)]
        elif who != "Anyone":
            view = view[view.assignee == who]
        if text:
            view = view[view.requirement.str.contains(text, case=False, regex=False)]

        if view.empty:
            st.caption("No rows match.")
            _email_note(job_id)
            return
        grid_key = f"{job_id}:{sheet}:{show}:{section}:{module}:{who}:{text}"
        selected = review_grid.grid(job_id, sp, view, key=grid_key, returned=who == RETURNED, selectable=True)

        # Bulk actions: on the ticked rows, or on every row the grid shows when none are ticked.
        targets = selected or view.row_num.tolist()
        which = (f"{len(targets):,} selected row{'s' * (len(targets) != 1)}" if selected
                 else f"{len(targets):,} shown row{'s' * (len(targets) != 1)}")
        actions = st.container(horizontal=True, vertical_alignment="center")
        with actions.popover(f"Assign {which}", icon=":material/person_add:"):
            # Partners covering every module among these rows are suggested first.
            covering = draft.partners()
            target_modules = set(view[view.row_num.isin(targets)].module.dropna()) - {modules.GENERAL}
            suggested = [n for n, mods in covering.items() if target_modules and target_modules <= set(mods)]
            everyone = [*suggested, *[n for n in draft.people() if n not in suggested]]
            name = st.selectbox("Person", everyone, index=None, placeholder="Pick an SME or partner",
                                key=f"assign-to:{job_id}",
                                format_func=lambda n: f"{n} · partner" if n in covering else n)
            if suggested:
                st.caption(f"**{', '.join(suggested)}** cover{'s' * (len(suggested) == 1)} "
                           f"{', '.join(sorted(target_modules))}.")
            a1, a2 = st.columns(2)
            if a1.button("Assign", type="primary", width="stretch", disabled=not name, key=f"assign:{job_id}"):
                draft.assign(job_id, sheet, targets, name.strip())
                review_grid.set_selection(grid_key, False)
                st.rerun()
            if a2.button("Unassign", width="stretch", key=f"unassign:{job_id}"):
                draft.assign(job_id, sheet, targets, None)
                review_grid.set_selection(grid_key, False)
                st.rerun()
        if module_names := modules.names() if modules.load() else []:
            with actions.popover(f"Set module for {which}", icon=":material/category:"):
                st.caption(f"Put the {which} in one module.")
                new_module = st.selectbox("Module", module_names, index=None, placeholder="Pick a module",
                                          key=f"set-module:{job_id}")
                if st.button("Set module", type="primary", width="stretch", disabled=not new_module,
                             key=f"set-module-go:{job_id}"):
                    draft.set_module(job_id, sheet, targets, new_module)
                    review_grid.set_selection(grid_key, False)
                    st.rerun()
        if len(selected) < len(view):
            actions.button(f"Select all {len(view):,}", icon=":material/select_all:", type="tertiary",
                           on_click=review_grid.set_selection, args=(grid_key, True))
        if selected:
            actions.button("Clear selection", icon=":material/deselect:", type="tertiary",
                           on_click=review_grid.set_selection, args=(grid_key, False))
        _email_note(job_id)  # at the foot of the card, under the actions that assign rows

    # ---- Answer details ----
    with st.container(border=True, key="card-answer"):
        st.subheader("Answer details")
        review_grid.details(view, key=f"{job_id}:{sheet}", author=draft.BID_MANAGER)

if choice == NEW:
    new_draft()
else:
    show_job(choice)
