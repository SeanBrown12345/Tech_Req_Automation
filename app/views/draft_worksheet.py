import hashlib
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app.admin_layout import ACCENT
from app.common import STATUS_LABELS, status_badge, status_label
from kb import db, draft

KIND_LABELS = {"choice": "Pick from list", "text": "Free text", "marks": "Mark one column"}
CONF_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}

# Keep the page readable on wide screens instead of stretching edge to edge.
st.html("<style>[data-testid='stMainBlockContainer'] { max-width: 1200px; margin: 0 auto; }</style>")


@st.cache_data(show_spinner="Reading workbook...", max_entries=3)
def _read(data: bytes) -> dict:
    return draft.read_workbook(data)


@st.cache_data(show_spinner="Working out how to fill this worksheet...", max_entries=6)
def _plan(data: bytes, header_rows: tuple) -> dict:
    return draft.guess_plan(data, _read(data), dict(header_rows))


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

    rows = draft.collect_rows(plan, _read(data))
    no_options = [f"{sp['name']} column {f['columns'][0]}" for sp in plan["sheets"] if sp["include"]
                  for f in sp["fields"] if f["include"] and f["kind"] == "choice" and not f.get("options")]
    if no_options:
        st.error("Add response options (or switch to free text) before starting: " + ", ".join(no_options))
    st.write(f"**{len(rows):,}** requirement rows have empty answer cells to fill"
             f" · about {max(1, round(len(rows) / 100))} min")
    if st.button("Start drafting", type="primary", disabled=not rows or bool(no_options)):
        job_id, _ = draft.create_job(name.strip() or uploaded.name, client.strip() or None, uploaded.name, data, plan)
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

    progress()

    if done.empty:
        return

    m = st.columns(3)
    m[0].metric("Drafted", f"{len(done):,}")
    m[1].metric("Low confidence", int((done.confidence == "low").sum()), help="Highlighted red in the download")
    m[2].metric("May add cost", int(done.cost_impact.sum()), help="Highlighted yellow in the download")

    # ---- Download ----
    st.html(f"""<style>
.st-key-draft-download [data-testid="stDownloadButton"] button {{ background: {ACCENT}; border-color: {ACCENT}; color: #fff; }}
.st-key-draft-download [data-testid="stDownloadButton"] button:hover {{ filter: brightness(1.15); color: #fff; }}
</style>""")
    with st.container(border=True, key="draft-download"):
        st.markdown("**Download the filled worksheet**")
        d1, d2, d3 = st.columns([2, 2, 2], vertical_alignment="center")
        red = d1.toggle("Red: low-confidence answers", value=True, key=f"red:{job_id}",
                        help="Fills the answer cells of low-confidence rows.")
        yellow = d2.toggle("Yellow: may add cost", value=True, key=f"yellow:{job_id}",
                           help="Fills the requirement cell of rows that may add implementation cost.")
        # The file is built when clicked, so it always has the latest edits and highlight choices.
        d3.download_button("Download", lambda: draft.build_output(job_id, red, yellow), type="primary",
                           file_name=f"{Path(job.filename).stem} - DRAFT.xlsx", width="stretch",
                           icon=":material/download:", on_click="ignore",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---- Review grid ----
    st.subheader("Review")
    sheets = [s for s in dict.fromkeys(done.sheet)]
    # The Sheet picker only exists for multi-sheet drafts; without it, Show and Find start at the left.
    if len(sheets) > 1:
        f1, f2, f3 = st.columns([2, 2, 3])
        sheet = f1.selectbox("Sheet", sheets, key=f"sheet:{job_id}")
    else:
        f2, f3, _ = st.columns([2, 3, 2])
        sheet = sheets[0]
    show = f2.selectbox("Show", ["All", "Low confidence", "May add cost", "Errors"], key=f"show:{job_id}")
    text = f3.text_input("Find", key=f"find:{job_id}", placeholder="Search requirement text")

    sp = next(s for s in plan["sheets"] if s["name"] == sheet)
    fields = [f for f in sp["fields"] if f.get("include", True)]
    view = rows[rows.sheet == sheet].copy()
    if show == "Errors":
        view = view[view.error.notna() & view.ai.isna()]
        st.dataframe(view[["row_num", "req_id", "requirement", "error"]], hide_index=True, width="stretch")
        return
    view = view[view.ai.notna()]
    if show == "Low confidence":
        view = view[view.confidence == "low"]
    elif show == "May add cost":
        view = view[view.cost_impact == 1]
    if text:
        view = view[view.requirement.str.contains(text, case=False, regex=False)]
    if view.empty:
        st.caption("No rows match.")
        return

    answers = [json.loads(f or a) for f, a in zip(view.final.where(view.final.notna(), None), view.ai)]
    grid = pd.DataFrame({
        "Row": view.row_num.values,
        "ID": view.req_id.values,
        "Requirement": [f"{r}  (under: {p})" if isinstance(p, str) and p else r
                        for p, r in zip(view.parent_text, view.requirement)],
        **{f["label"]: [a.get(f["key"], "") if f["key"] in json.loads(fl) else None
                        for a, fl in zip(answers, view.fill)] for f in fields},
        "Confidence": view.confidence.map(CONF_LABELS).values,
        "Cost": view.cost_impact.astype(bool).values,
        "Status": view.status.map(status_label).values,
    })
    config = {
        "Row": st.column_config.NumberColumn(width="small"),
        "Requirement": st.column_config.TextColumn(width="large"),
        "Confidence": st.column_config.TextColumn(width="small"),
        "Cost": st.column_config.CheckboxColumn("May add cost", width="small"),
    }
    for f in fields:
        if f["kind"] == "choice":
            config[f["label"]] = st.column_config.SelectboxColumn(options=f["options"])
        elif f["kind"] == "marks":
            config[f["label"]] = st.column_config.SelectboxColumn(options=[f["labels"][c] for c in f["columns"]])
        else:
            config[f["label"]] = st.column_config.TextColumn(width="large")
    edited = st.data_editor(grid, hide_index=True, width="stretch", height=520, column_config=config,
                            disabled=["Row", "ID", "Requirement", "Confidence", "Cost", "Status"],
                            key=f"grid:{job_id}:{sheet}:{show}:{text}")

    # Persist edits.
    changed = 0
    for (_, before), (_, after), fill in zip(grid.iterrows(), edited.iterrows(), view.fill):
        keys = json.loads(fill)
        new_values = {f["key"]: after[f["label"]] or "" for f in fields if f["key"] in keys}
        old_values = {f["key"]: before[f["label"]] or "" for f in fields if f["key"] in keys}
        if new_values != old_values:
            draft.save_review(job_id, sheet, int(after.Row), final=new_values)
            changed += 1
    if changed:
        st.toast(f"Saved {changed} change{'s' if changed > 1 else ''}")

    # ---- Answer details ----
    st.subheader("Answer details")
    pick = st.selectbox("Row", view.row_num.tolist(), key=f"why:{job_id}:{sheet}",
                        format_func=lambda n: f"Row {n}: {view.set_index('row_num').loc[n, 'requirement'][:100]}")
    r = view.set_index("row_num").loc[pick]
    h1, h2 = st.columns([1, 5])
    with h1:
        status_badge(r.status)
    h2.caption(f"Confidence **{CONF_LABELS.get(r.confidence)}** (AI said {CONF_LABELS.get(r.ai_confidence)}, "
               f"closest past answer similarity {r.best_similarity:.2f})")
    st.markdown("**AI reasoning**")
    st.write(r.rationale)
    if r.cost_impact:
        st.warning(f"May add cost: {r.cost_note}")
    st.markdown("**Past answers it used**")
    for e in json.loads(r.evidence or "[]"):
        sim = f" · similarity {e['similarity']:.2f}" if e.get("similarity") is not None else ""
        with st.expander(f"{e['id']} · {STATUS_LABELS.get(e['status'], e['status'])} · {e['client']} "
                         f"({e['submitted'] or '?'}){sim}"):
            st.write(e["requirement"])
            st.caption(e["comment"] or "_No comment_")

if choice == NEW:
    new_draft()
else:
    show_job(choice)
