"""The instructions SMEs and partners read for one sheet (the Instructions dialog on the review page, and its
preview on the draft page): which columns they fill and how.

The bid team sets what goes in it on the draft page, in the sheet plan:
- "sme_note": a note at the top, for anything that applies to the whole sheet;
- each field's "sme_help": how to fill that column, in the team's words.
Response rules ("requires", see draft.required_comment) are listed under the response they apply to."""

import streamlit as st


def md_escape(text: str) -> str:
    """Text shown as is where Markdown is read."""
    return "".join("\\" + c if c in "\\`*_{}[]()#+-.!|~<>$:" else c for c in text)


def fields(sheet_plan: dict) -> list[dict]:
    """The columns people fill on this sheet, left to right."""
    return [f for f in sheet_plan["fields"] if f.get("include", True)]


def kind_text(f: dict) -> str:
    """What sort of answer a column takes, in a few words."""
    if f["kind"] == "choice":
        return "Pick one from the list"
    if f["kind"] == "marks":
        return "Mark one of these columns"
    return "Write the answer"


def how_to(f: dict) -> str:
    """The team's note on how to fill a column. Until they write one, the column's note to the AI (set when the
    draft was started, e.g. "Only comment when the answer is not Y") stands in."""
    return (f["sme_help"] if "sme_help" in f else f.get("guidance") or "").strip()


def _field(f: dict) -> None:
    """One column to fill: its letter and header, what it takes, the team's note, and its choices with what a
    comment must give for each."""
    letters = ", ".join(f["columns"])
    with st.container(border=True, gap="small"):
        st.markdown(f"**{md_escape(f['label'])}** :gray-badge[Column{'s' * (len(f['columns']) > 1)} {letters}]  \n"
                    f":gray[{kind_text(f)}]")
        if help_text := how_to(f):
            st.markdown(help_text)
        if f["kind"] == "choice":
            requires = f.get("requires") or {}
            st.markdown("\n".join(f"- **{md_escape(o)}**" + (f" — comment must give: {md_escape(requires[o])}"
                                                               if requires.get(o) else "") for o in f["options"]))
        elif f["kind"] == "marks":
            st.markdown("\n".join(f"- **{c}**: {md_escape(f['labels'][c])}" for c in f["columns"]))


def render(sheet_plan: dict) -> None:
    """The instructions for one sheet."""
    if note := (sheet_plan.get("sme_note") or "").strip():
        st.info(note, icon=":material/campaign:")
    st.subheader("Columns you fill", anchor=False)
    columns = sheet_plan.get("columns") or {}
    where = [f"the requirement is in column **{columns['requirement']}**"] if columns.get("requirement") else []
    if columns.get("req_id"):
        where.append(f"its ID in column **{columns['req_id']}**")
    st.caption(("In the worksheet, " + " and ".join(where) + ". " if where else "")
               + "Fill only the columns below and leave the rest as they are. A cell the client already filled "
               "stays as it is.")
    for f in fields(sheet_plan):
        _field(f)
