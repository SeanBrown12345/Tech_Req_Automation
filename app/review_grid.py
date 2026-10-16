"""The answer review grid shared by the draft page and the task board. Answers, assignee and approval
are edited in place and saved row by row as they change, so several SMEs can work on one draft."""

import hashlib
import json
from datetime import datetime

import pandas as pd
import streamlit as st

from app.common import status_badge, status_label
from kb import draft, modules

CONF_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}
RULE = "Needs comment"  # the grid's ⚠️ column
# Rows with notes (rows loaded with draft.note_summary) are tinted; the table can only colour cells it doesn't edit.
NOTE_TINT = "background-color: rgba(28, 131, 225, 0.35)"


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


# ---- Filters shared by the draft page and the task board ----------------------------------------
SHOW = ["All", "Needs Review", "Low confidence", "Needs pricing", "Approved", "Auto-approved"]
ALL_SECTIONS, ALL_MODULES, NO_MODULE = "All sections", "All modules", "Not detected yet"


def show_rows(view: pd.DataFrame, show: str) -> pd.DataFrame:
    """Narrow drafted rows to one of the SHOW options."""
    if show == "Low confidence":
        return view[view.confidence == "low"]
    if show == "Needs pricing":
        return view[view.cost_impact == 1]
    if show == "Approved":
        return view[view.reviewed == 1]
    if show == "Auto-approved":  # by the draft's auto-approve rule, not a person
        return view[view.approved_by == "auto"]
    if show == "Needs Review":
        return view[view.reviewed == 0]
    return view


def auto_only(view: pd.DataFrame) -> pd.Series:
    """Rows the auto-approve rule approved that no SME has: they count as handled by the rule, not as
    unassigned."""
    return view.assignee.isna() & (view.approved_by == "auto")


def with_sections(view: pd.DataFrame) -> pd.DataFrame:
    """Add a `section` column: the coarsest heading level that varies in these rows (worksheets use
    different ones)."""
    contexts = [json.loads(c) if isinstance(c, str) and c else {} for c in view.context]
    level = next((lv for lv in ("category", "section", "subcategory")
                  if len({c.get(lv) for c in contexts} - {None}) > 1), None)
    return view.assign(section=[c.get(level) if level else None for c in contexts])


def section_select(col, view: pd.DataFrame, key: str) -> str:
    """Section picker over a with_sections view."""
    sections = sorted(set(view.section) - {None}, key=str.lower)
    return col.selectbox("Section", [ALL_SECTIONS, *sections], key=key, disabled=not sections)


def module_select(col, view: pd.DataFrame, key: str, counted: pd.DataFrame | None = None) -> str:
    """Module picker with row counts; NO_MODULE picks rows modules weren't detected for. The options come
    from `view`, the counts from `counted` (the rows the other filters leave), so the list stays put while
    the totals follow Show and Section."""
    module_names = modules.names() if modules.load() else []
    present = set(view.module.dropna())
    counted = view if counted is None else counted
    counts = counted.module.value_counts()
    options = [ALL_MODULES, *[m for m in module_names if m in present], *([NO_MODULE] if view.module.isna().any() else [])]
    return col.selectbox("Module", options, key=key, disabled=not module_names,
                         format_func=lambda o: o if o == ALL_MODULES else
                         f"{o} ({int(counted.module.isna().sum() if o == NO_MODULE else counts.get(o, 0)):,})",
                         help="The product module each requirement belongs to; set by **Detect modules**, "
                              "or change it in the grid's Module column. The list is in **Admin › Modules**.")


def in_section(view: pd.DataFrame, section: str) -> pd.DataFrame:
    return view if section == ALL_SECTIONS else view[view.section == section]


def section_and_module(view: pd.DataFrame, section: str, module: str) -> pd.DataFrame:
    view = in_section(view, section)
    if module == NO_MODULE:
        return view[view.module.isna()]
    if module != ALL_MODULES:
        return view[view.module == module]
    return view


def _wrapped_row_height(texts) -> int:
    """Row height that shows the longest of `texts` in full in a "large" column (~55 characters a line),
    capped at 12 lines. The grid has one height for every row, so all rows get it."""
    lines = max((len(t) // 55 + 1 for t in texts if isinstance(t, str)), default=1)
    return 16 + 20 * min(lines, 12)


# Wrap text only exists in fullscreen. A fullscreen grid's toolbar button reads "Close fullscreen": while it
# does, that grid's toggle shows, pinned top left over the fullscreen view; otherwise it's hidden. When the
# grid leaves fullscreen, the script switches wrapping back off, so the table on the page is never wrapped.
_WRAP_HTML = """<style>
[class*="st-key-gridwrap-"] { display: none; }
[class*="st-key-gridbox-"]:has(button[aria-label="Close fullscreen"]) [class*="st-key-gridwrap-"] {
    display: flex; position: fixed; top: 15px; left: 1rem; z-index: 1000051; width: auto;  /* level with the toolbar */
}
/* The page code (this style and script) sits in the toggle's container; it takes no space there. */
[class*="st-key-gridwrap-"] [data-testid="stElementContainer"]:has([data-testid="stHtml"]) { display: none; }
</style>
<script>
if (!window.reqfillWrapWatch) {
    window.reqfillWrapWatch = true;
    const wasFullscreen = new WeakSet();
    new MutationObserver(() => {
        for (const box of document.querySelectorAll('[class*="st-key-gridbox-"]')) {
            if (box.querySelector('button[aria-label="Close fullscreen"]')) {
                wasFullscreen.add(box);
            } else if (wasFullscreen.has(box)) {
                wasFullscreen.delete(box);
                const toggle = box.querySelector('[class*="st-key-gridwrap-"] input[type="checkbox"]');
                if (toggle && toggle.checked) toggle.click();
            }
        }
    }).observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ["aria-label"]});
}
</script>"""


def _rows_sig(view: pd.DataFrame) -> str:
    """The grid's edits are stored by position, so when its rows change (an approved row leaves a
    "to review" list) it must start afresh, or an edit would land on the row that moved up."""
    return hashlib.sha1(",".join(map(str, view.row_num)).encode()).hexdigest()[:10]


def grid(job_id: str, sheet_plan: dict, view: pd.DataFrame, key: str, height: int | None = None,
         returned: bool = False, selectable: bool = False, reviewer: bool = False,
         wrap: bool | None = None) -> list[int]:
    """Editable grid of drafted rows (`view`: rows of one sheet of one draft). `returned` adds who sent
    each row back, for rows SMEs unassigned. `selectable` adds a Select tick box per row; the ticked
    row numbers are returned (ticking isn't an edit, nothing is saved). `reviewer` is the SME's own
    view on the task board: Row, Requirement, the answers, Confidence, Needs pricing and Approved only,
    with a narrative/comments answer column titled just "Comments". `wrap` wraps long text in taller rows;
    left as None, the grid offers its own Wrap text toggle in fullscreen only. The height fits the rows,
    up to 520px, unless given."""
    sheet = sheet_plan["name"]
    module_names = modules.names() if modules.load() else []
    fields = [f for f in sheet_plan["fields"] if f.get("include", True)]
    answers = [json.loads(f if isinstance(f, str) else a) for f, a in zip(view.final, view.ai)]  # edits, else AI
    # Responses the worksheet wants a comment with (see draft.required_comment): a narrow last column marks
    # every such row, and its header's tooltip says what each response's comment must include.
    rules = draft.comment_field(sheet_plan) is not None and any(f.get("requires") for f in fields)
    noted = "notes" in view.columns
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
        **({RULE: [f"⚠️  {note}" if (note := draft.required_comment(sheet_plan, a, json.loads(fl))) else ""
                   for a, fl in zip(answers, view.fill)]} if rules else {}),
    })
    config = {
        "Select": st.column_config.CheckboxColumn("", width=40, help="Tick rows to assign them or set their module"),
        "Row": st.column_config.NumberColumn(width="small"),
        "Requirement": st.column_config.TextColumn(width="large", help="Rows highlighted blue have notes: open the "
                                                   "**Notes** panel to read them" if noted else None),
        "Confidence": st.column_config.TextColumn(width="small"),
        RULE: st.column_config.TextColumn("⚠️", width=28, help="The worksheet requires a comment with this "
                                          "response; the row can't be approved without one.\n\n" + "\n\n".join(
            f"**{o}**: {n}" for f in fields for o, n in (f.get("requires") or {}).items())),
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
            comments = reviewer and any(w in f["label"].lower() for w in ("comment", "narrative"))
            config[f["label"]] = st.column_config.TextColumn("Comments" if comments else None, width="large")
    # Wrap text: taller rows, so long requirements and written answers show in full. Set by the caller, or
    # else a toggle shown in fullscreen only (see _WRAP_HTML).
    box = hashlib.sha1(key.encode()).hexdigest()[:10]
    with st.container(key=f"gridbox-{box}"):
        if wrap is None:
            with st.container(key=f"gridwrap-{box}"):  # hidden outside fullscreen, so its contents take no space
                st.html(_WRAP_HTML, unsafe_allow_javascript=True)
                wrap = st.toggle("Wrap text", key=f"wrap:{key}",
                                 help="Make rows tall enough to show the whole requirement and written answers")
        written = [f["label"] for f in fields if f["kind"] not in ("choice", "marks")]
        row_height = _wrapped_row_height(frame[["Requirement", *written]].stack()) if wrap else 35
        tinted = view.notes.values > 0 if noted else None
        data = frame.style.apply(lambda col: [NOTE_TINT if t else "" for t in tinted],
                                 subset=["Row", "Requirement", "Confidence"]) if noted and tinted.any() else frame
        edited = st.data_editor(data, hide_index=True, width="stretch", column_config=config,
                                height=height or min(760 if wrap else 520, 38 + (row_height + 1) * len(view)),
                                row_height=row_height if wrap else None,
                                column_order=[*(["Select"] if selectable else []), "Row",
                                              "Requirement",
                                              *[f["label"] for f in fields],
                                              "Confidence", "Cost", "Approved", *([RULE] if rules else [])]
                                if reviewer else None,
                                disabled=["Row", "Requirement", RULE, "Confidence", "Status",
                                          "Unassigned by"],
                                key=f"grid:{key}:{_rows_sig(view)}:{st.session_state.get('grid_rev', 0)}")

    if blocked := st.session_state.pop(f"grid_blocked:{key}", None):
        st.error(f"Not approved: {blocked}", icon=":material/error:")

    # Persist edits.
    changed, blocked = 0, []
    for (_, before), (_, after), fill in zip(frame.iterrows(), edited.iterrows(), view.fill):
        row_num, keys = int(after.Row), json.loads(fill)
        new_values = {f["key"]: after[f["label"]] or "" for f in fields if f["key"] in keys}
        old_values = {f["key"]: before[f["label"]] or "" for f in fields if f["key"] in keys}
        if new_values != old_values:
            draft.save_review(job_id, sheet, row_num, final=new_values)
        if bool(after.Cost) != bool(before.Cost):
            draft.save_review(job_id, sheet, row_num, cost_impact=bool(after.Cost))
        if bool(after.Approved) != bool(before.Approved):
            if after.Approved and draft.missing_comment(sheet_plan, new_values, keys):
                blocked.append(row_num)  # its response needs a comment: the tick is undone below
            else:
                draft.save_review(job_id, sheet, row_num, reviewed=bool(after.Approved))
        if module_names and name_or_none(after.Module) not in (None, name_or_none(before.Module)):
            draft.set_module(job_id, sheet, [row_num], after.Module)
        if name_or_none(after["Assigned to"]) != name_or_none(before["Assigned to"]):
            draft.assign(job_id, sheet, [row_num], name_or_none(after["Assigned to"]))
        changed += not after.drop("Select", errors="ignore").equals(before.drop("Select", errors="ignore"))
    if blocked:
        st.session_state[f"grid_blocked:{key}"] = (
            f"row{'s' * (len(blocked) > 1)} {', '.join(map(str, blocked))} need{'s' * (len(blocked) == 1)} a comment "
            "first (hover ⚠️ for what it must include).")
        refresh()  # redraw from the database, so the refused ticks clear
    if changed:
        # Rerun so everything drawn before this grid (board totals, other grids) shows the change too.
        st.rerun()
    return edited.loc[edited.Select, "Row"].astype(int).tolist() if selectable else []


def _post_note(job_id: str, sheet: str, row_num: int, author: str, key: str) -> None:
    draft.add_note(job_id, sheet, [row_num], author, st.session_state.get(key) or "")
    st.session_state[key] = ""  # the box starts empty for the next message


def _reply(job_id: str, sheet: str, row_num: int, author: str, key: str) -> None:
    draft.add_note(job_id, sheet, [row_num], author, st.session_state.get(key) or "")


# Compact threads: small text, tight spacing and a slim one-line reply box.
_COMPACT = """<style>
[class*="st-key-thread-"] p { font-size: 0.875rem; line-height: 1.4; }
[class*="st-key-thread-"] [data-testid="stChatInput"] > div { min-height: 0; padding: 0.1rem 0.25rem 0.1rem 0.5rem; }
[class*="st-key-thread-"] [data-testid="stChatInput"] textarea {
    min-height: 0 !important; height: 1.9rem !important; padding: 0.3rem 0.25rem; font-size: 0.85rem; line-height: 1.3;
}
[class*="st-key-thread-"] [data-testid="stChatInput"] button { width: 1.6rem; height: 1.6rem; min-height: 0; }
</style>"""


def thread(job_id: str, sheet: str, row_num: int, author: str | None, key: str, compact: bool = False) -> None:
    """The messages left on a row, oldest first, and a box to add one as `author` (None = read only).
    `compact` (the notes panel, the row view): each message as a name line and its text, and a one-line reply
    sent with Enter."""
    if compact:
        st.html(_COMPACT)
        with st.container(gap="small", key=f"thread-{key}-{row_num}"):
            for n in draft.notes(job_id, sheet, row_num).itertuples():
                when = datetime.fromisoformat(n.created_at).astimezone()
                st.markdown(f":gray[**{n.author}** · {when.day} {when:%b %H:%M}]  \n{n.body}")
            if author:
                box = f"note:{key}:{job_id}:{sheet}:{row_num}"
                # A chat input sends only on Enter (a text box would also send when it loses focus) and clears itself.
                st.chat_input("Reply (Enter to send)", key=box, on_submit=_reply,
                              args=(job_id, sheet, row_num, author, box))
        return
    for n in draft.notes(job_id, sheet, row_num).itertuples():
        when = datetime.fromisoformat(n.created_at).astimezone()
        with st.chat_message("user" if n.author == author else "assistant",
                             avatar=":material/person:" if n.author == author else ":material/forum:"):
            st.caption(f"**{n.author}** · {when.day} {when:%b %H:%M}")
            st.markdown(n.body)
    if author:
        box = f"note:{key}:{job_id}:{sheet}:{row_num}"
        st.text_area("New note", key=box, height=80, placeholder="Ask a question or leave a note for whoever has "
                     "this row next", label_visibility="collapsed")
        st.button("Send", icon=":material/send:", key=f"{box}:send", on_click=_post_note, args=(job_id, sheet, row_num, author, box))


def details(view: pd.DataFrame, key: str, author: str | None = None) -> None:
    """Pick a row and see why the AI answered it as it did, and the notes left on it (`author` can add one)."""
    by_row = view.set_index("row_num")
    noted = set(by_row.index[by_row.notes > 0]) if "notes" in by_row else set()
    pick = st.selectbox("Row", view.row_num.tolist(), key=f"why:{key}",
                        format_func=lambda n: f"{'💬 ' * (n in noted)}Row {n}: {by_row.loc[n, 'requirement'][:80]}")
    r = by_row.loc[pick]
    with st.container(border=True):  # the full requirement; the picker only shows its start
        if isinstance(r.parent_text, str) and r.parent_text:
            st.caption(f"Under: {r.parent_text}")
        st.markdown(r.requirement)
    h1, h2 = st.columns([1, 5])
    with h1:
        status_badge(r.status)
    h2.caption(confidence_note(r))
    st.markdown("**Notes**")
    thread(r.job_id, r.sheet, int(pick), author, key=key)
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
        same = " · same question" if e.get("same_question") else ""
        with st.expander(f"{similarity_md(e.get('similarity'))} {e['client']} ({e['submitted'] or '?'}){same}",
                         expanded=i < expanded):
            status_badge(e["status"])
            st.write(e["requirement"])
            st.caption(e["comment"] or "_No comment_")
            # What was answered in each answer column (a questionnaire can have several).
            for name, value in (e.get("answer_raw") or {}).items():
                st.caption(f"**{name.replace('_', ' ').capitalize()}:** {value}")
