"""The answer review grid shared by the draft page and the task board. Answers, assignee and approval
are edited in place and saved row by row as they change, so several SMEs can work on one draft."""

import hashlib
import json

import pandas as pd
import streamlit as st

from app.common import STATUS_LABELS, status_badge, status_label
from kb import draft

CONF_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}


def name_or_none(value) -> str | None:
    """Assignee cells come back as None, NaN or a name."""
    return value.strip() or None if isinstance(value, str) else None


def refresh() -> None:
    """Call after a bulk change (assign, approve): the grids are redrawn from the database, otherwise
    they would replay their earlier cell edits over the change."""
    st.session_state.grid_rev = st.session_state.get("grid_rev", 0) + 1


def _rows_sig(view: pd.DataFrame) -> str:
    """The grid's edits are stored by position, so when its rows change (an approved row leaves a
    "to review" list) it must start afresh, or an edit would land on the row that moved up."""
    return hashlib.sha1(",".join(map(str, view.row_num)).encode()).hexdigest()[:10]


def grid(job_id: str, sheet_plan: dict, view: pd.DataFrame, key: str, height: int | None = None,
         returned: bool = False) -> None:
    """Editable grid of drafted rows (`view`: rows of one sheet of one draft). `returned` adds who sent
    each row back, for rows SMEs unassigned. The height fits the rows, up to 520px, unless given."""
    if saved := st.session_state.pop("grid_saved", 0):
        st.toast(f"Saved {saved} change{'s' if saved > 1 else ''}")
    sheet = sheet_plan["name"]
    fields = [f for f in sheet_plan["fields"] if f.get("include", True)]
    answers = [json.loads(f if isinstance(f, str) else a) for f, a in zip(view.final, view.ai)]  # edits, else AI
    frame = pd.DataFrame({
        "Row": view.row_num.values,
        "ID": view.req_id.values,
        "Requirement": [f"{r}  (under: {p})" if isinstance(p, str) and p else r
                        for p, r in zip(view.parent_text, view.requirement)],
        **({"Unassigned by": view.returned_by.values} if returned else {}),
        **{f["label"]: [a.get(f["key"], "") if f["key"] in json.loads(fl) else None
                        for a, fl in zip(answers, view.fill)] for f in fields},
        "Confidence": view.confidence.map(CONF_LABELS).values,
        "Cost": view.cost_impact.astype(bool).values,
        "Status": view.status.map(status_label).values,
        "Assigned to": [name_or_none(a) for a in view.assignee],
        "Approved": view.reviewed.astype(bool).values,
    })
    config = {
        "Row": st.column_config.NumberColumn(width="small"),
        "Requirement": st.column_config.TextColumn(width="large"),
        "Confidence": st.column_config.TextColumn(width="small"),
        "Cost": st.column_config.CheckboxColumn("Needs pricing", width="small",
                                                help="May add implementation cost; highlighted yellow in the download"),
        "Assigned to": st.column_config.SelectboxColumn(
            options=draft.people(), help="SMEs are added under **Admin › SMEs**"),
        "Approved": st.column_config.CheckboxColumn(width="small", help="The SME is happy with this answer"),
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
                            disabled=["Row", "ID", "Requirement", "Confidence", "Status", "Unassigned by"],
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
        if name_or_none(after["Assigned to"]) != name_or_none(before["Assigned to"]):
            draft.assign(job_id, sheet, [row_num], name_or_none(after["Assigned to"]))
        changed += not after.equals(before)
    if changed:
        # Rerun so everything drawn before this grid (board totals, other grids) shows the change too.
        st.session_state.grid_saved = changed
        st.rerun()


def details(view: pd.DataFrame, key: str) -> None:
    """Pick a row and see why the AI answered it as it did."""
    pick = st.selectbox("Row", view.row_num.tolist(), key=f"why:{key}",
                        format_func=lambda n: f"Row {n}: {view.set_index('row_num').loc[n, 'requirement'][:100]}")
    r = view.set_index("row_num").loc[pick]
    h1, h2 = st.columns([1, 5])
    with h1:
        status_badge(r.status)
    h2.caption(confidence_note(r))
    evidence(r)


def confidence_note(r) -> str:
    return (f"Confidence **{CONF_LABELS.get(r.confidence)}** (AI said {CONF_LABELS.get(r.ai_confidence)}, "
            f"closest past answer similarity {r.best_similarity:.2f})")


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
        sim = f" · similarity {e['similarity']:.2f}" if e.get("similarity") is not None else ""
        with st.expander(f"{e['id']} · {STATUS_LABELS.get(e['status'], e['status'])} · {e['client']} "
                         f"({e['submitted'] or '?'}){sim}", expanded=i < expanded):
            st.write(e["requirement"])
            st.caption(e["comment"] or "_No comment_")
