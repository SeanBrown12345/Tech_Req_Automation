"""The answer review grid shared by the draft page and the task board. Answers, assignee and approval
are edited in place and saved row by row as they change, so several SMEs can work on one draft."""

import hashlib
import json

import pandas as pd
import streamlit as st

from app.common import status_badge, status_label
from kb import draft, modules

CONF_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}


def name_or_none(value) -> str | None:
    """Assignee cells come back as None, NaN or a name."""
    return value.strip() or None if isinstance(value, str) else None


def refresh() -> None:
    """Call after a bulk change (assign, approve): the grids are redrawn from the database, otherwise
    they would replay their earlier cell edits over the change."""
    st.session_state.grid_rev = st.session_state.get("grid_rev", 0) + 1


def set_selection(key: str, all_rows: bool) -> None:
    """Tick every row of the grid with this key (all_rows=True) or none; redraws it, so ticks made by hand
    are replaced."""
    st.session_state[f"select_all:{key}"] = all_rows
    refresh()


def _rows_sig(view: pd.DataFrame) -> str:
    """The grid's edits are stored by position, so when its rows change (an approved row leaves a
    "to review" list) it must start afresh, or an edit would land on the row that moved up."""
    return hashlib.sha1(",".join(map(str, view.row_num)).encode()).hexdigest()[:10]


def grid(job_id: str, sheet_plan: dict, view: pd.DataFrame, key: str, height: int | None = None,
         returned: bool = False, selectable: bool = False) -> list[int]:
    """Editable grid of drafted rows (`view`: rows of one sheet of one draft). `returned` adds who sent
    each row back, for rows SMEs unassigned. `selectable` adds a Select tick box per row; the ticked
    row numbers are returned (ticking isn't an edit, nothing is saved). The height fits the rows, up
    to 520px, unless given."""
    if saved := st.session_state.pop("grid_saved", 0):
        st.toast(f"Saved {saved} change{'s' if saved > 1 else ''}")
    sheet = sheet_plan["name"]
    module_names = modules.names() if modules.load() else []
    fields = [f for f in sheet_plan["fields"] if f.get("include", True)]
    answers = [json.loads(f if isinstance(f, str) else a) for f, a in zip(view.final, view.ai)]  # edits, else AI
    frame = pd.DataFrame({
        **({"Select": [st.session_state.get(f"select_all:{key}", False)] * len(view)} if selectable else {}),
        "Row": view.row_num.values,
        "Requirement": [f"{r}  (under: {p})" if isinstance(p, str) and p else r
                        for p, r in zip(view.parent_text, view.requirement)],
        **({"Unassigned by": view.returned_by.values} if returned else {}),
        **({"Module": [name_or_none(m) for m in view.module]} if module_names else {}),
        **{f["label"]: [a.get(f["key"], "") if f["key"] in json.loads(fl) else None
                        for a, fl in zip(answers, view.fill)] for f in fields},
        "Confidence": view.confidence.map(CONF_LABELS).values,
        "Cost": view.cost_impact.astype(bool).values,
        "Status": view.status.map(status_label).values,
        "Assigned to": [name_or_none(a) for a in view.assignee],
        "Approved": view.reviewed.astype(bool).values,
    })
    config = {
        "Select": st.column_config.CheckboxColumn("", width=40, help="Tick rows to assign them or set their module"),
        "Row": st.column_config.NumberColumn(width="small"),
        "Requirement": st.column_config.TextColumn(width="large"),
        "Confidence": st.column_config.TextColumn(width="small"),
        "Cost": st.column_config.CheckboxColumn("Needs pricing", width="small",
                                                help="May add implementation cost; highlighted yellow in the download"),
        "Assigned to": st.column_config.SelectboxColumn(
            options=draft.people(), help="SMEs are added under **Admin › SMEs**"),
        "Approved": st.column_config.CheckboxColumn(width="small", help="The SME is happy with this answer"),
        "Module": st.column_config.SelectboxColumn(options=module_names, width="small",
                                                   help="Product module the requirement belongs to"),
    }
    for f in fields:
        if f["kind"] == "choice":
            config[f["label"]] = st.column_config.SelectboxColumn(options=f["options"])
        elif f["kind"] == "marks":
            config[f["label"]] = st.column_config.SelectboxColumn(options=[f["labels"][c] for c in f["columns"]])
        else:
            config[f["label"]] = st.column_config.TextColumn(width="large")
    edited = st.data_editor(frame, hide_index=True, width="stretch", column_config=config,
                            height=height or min(520, 38 + 35 * len(view)),
                            disabled=["Row", "Requirement", "Confidence", "Status", "Unassigned by"],
                            key=f"grid:{key}:{_rows_sig(view)}:{st.session_state.get('grid_rev', 0)}")

    # Persist edits.
    changed = 0
    for (_, before), (_, after), fill in zip(frame.iterrows(), edited.iterrows(), view.fill):
        row_num, keys = int(after.Row), json.loads(fill)
        new_values = {f["key"]: after[f["label"]] or "" for f in fields if f["key"] in keys}
        old_values = {f["key"]: before[f["label"]] or "" for f in fields if f["key"] in keys}
        if new_values != old_values:
            draft.save_review(job_id, sheet, row_num, final=new_values)
        if bool(after.Cost) != bool(before.Cost):
            draft.save_review(job_id, sheet, row_num, cost_impact=bool(after.Cost))
        if bool(after.Approved) != bool(before.Approved):
            draft.save_review(job_id, sheet, row_num, reviewed=bool(after.Approved))
        if module_names and name_or_none(after.Module) not in (None, name_or_none(before.Module)):
            draft.set_module(job_id, sheet, [row_num], after.Module)
        if name_or_none(after["Assigned to"]) != name_or_none(before["Assigned to"]):
            draft.assign(job_id, sheet, [row_num], name_or_none(after["Assigned to"]))
        changed += not after.drop("Select", errors="ignore").equals(before.drop("Select", errors="ignore"))
    if changed:
        # Rerun so everything drawn before this grid (board totals, other grids) shows the change too.
        st.session_state.grid_saved = changed
        st.rerun()
    return edited.loc[edited.Select, "Row"].astype(int).tolist() if selectable else []


def details(view: pd.DataFrame, key: str) -> None:
    """Pick a row and see why the AI answered it as it did."""
    pick = st.selectbox("Row", view.row_num.tolist(), key=f"why:{key}",
                        format_func=lambda n: f"Row {n}: {view.set_index('row_num').loc[n, 'requirement'][:80]}")
    r = view.set_index("row_num").loc[pick]
    with st.container(border=True):  # the full requirement; the picker only shows its start
        if isinstance(r.parent_text, str) and r.parent_text:
            st.caption(f"Under: {r.parent_text}")
        st.markdown(r.requirement)
    h1, h2 = st.columns([1, 5])
    with h1:
        status_badge(r.status)
    h2.caption(confidence_note(r))
    evidence(r)


def similarity_md(sim) -> str:
    """A similarity score as a coloured Markdown badge, banded by the same thresholds that cap the
    draft's confidence: green for a strong match, orange for a partial one, red for a weak one."""
    if sim is None or pd.isna(sim):
        return ""
    if sim >= draft.SIM_SAME:
        color, label = "green", "Same requirement"
    elif sim >= draft.SIM_HIGH:
        color, label = "green", "Strong match"
    elif sim >= draft.SIM_MEDIUM:
        color, label = "orange", "Partial match"
    else:
        color, label = "red", "Weak match"
    return f":{color}-badge[{sim:.2f} · {label}]"


def confidence_note(r) -> str:
    return f"Confidence **{CONF_LABELS.get(r.confidence)}**"


def evidence(r, expanded: int = 0) -> None:
    """The AI's reasoning for one row and the past answers it drew on (the first `expanded` of them open)."""
    st.markdown("**AI reasoning**")
    st.write(r.rationale)
    if r.cost_impact and isinstance(r.cost_note, str) and r.cost_note:
        st.warning(f"May add cost: {r.cost_note}")
    st.markdown("**Past answers it used**")
    past = json.loads(r.evidence or "[]")
    if not past:
        st.caption("None: no similar past answers were found.")
    for i, e in enumerate(past):
        with st.expander(f"{similarity_md(e.get('similarity'))} {e['client']} ({e['submitted'] or '?'})",
                         expanded=i < expanded):
            status_badge(e["status"])
            st.write(e["requirement"])
            st.caption(e["comment"] or "_No comment_")
