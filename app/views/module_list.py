"""Modules: the product modules drafted requirements are sorted into, for filtering and assigning rows."""

import streamlit as st

from kb import modules

st.title("Modules")
st.caption("The product modules requirements are sorted into (**Detect modules** on a draft's page). Rows not "
           f"tied to one module are **{modules.GENERAL}**, which is always available.")

if flash := st.session_state.pop("modules_flash", None):
    st.toast(flash, icon=":material/check:")

table = modules.table()
if table.empty:
    st.info("No modules yet. Add the ones your RFPs cover below.")
else:
    st.dataframe(table.fillna(""), hide_index=True, width="stretch", column_config={
        "code": st.column_config.TextColumn("Module"),
        "description": st.column_config.TextColumn("Description", width="large"),
        "rows": st.column_config.NumberColumn("Rows", help="Drafted rows in this module, all drafts"),
    })


def _clean(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


# ---- Add -------------------------------------------------------------------------------------------
st.subheader("Add a module")
with st.form("module-add", clear_on_submit=True, border=False):
    c1, c2 = st.columns([1, 3])
    code = c1.text_input("Module", placeholder="e.g. MWFM", help="Short name shown in filters and badges")
    description = c2.text_input("Description (optional)", placeholder="e.g. Mobile Workforce Management",
                                help="Helps the AI recognise requirements for this module")
    if st.form_submit_button("Add module", type="primary"):
        if not _clean(code):
            st.error("Enter a module name.")
        elif modules.add(_clean(code), _clean(description)):
            st.session_state.modules_flash = f"Added {_clean(code)}. Use Detect modules on a draft to sort rows into it."
            st.rerun()
        else:
            st.error(f"{_clean(code)} is already a module." if _clean(code).lower() != modules.GENERAL.lower()
                     else f"{modules.GENERAL} is built in.")


# ---- Edit / reorder / remove -----------------------------------------------------------------------
@st.dialog("Remove module?")
def _confirm_remove(code: str, rows: int):
    st.write(f"**{code}** will be taken off the list.")
    if rows:
        st.write(f"Its **{rows:,}** rows lose their module. **Detect modules** on their drafts sorts them again.")
    cancel, remove = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if remove.button("Remove", type="primary", width="stretch"):
        modules.remove(code)
        st.session_state.modules_flash = f"Removed {code}."
        st.session_state.module_pick_next = None
        st.rerun()


def _move(code: str, step: int):
    modules.move(code, step)


if not table.empty:
    st.subheader("Edit, reorder or remove")
    if "module_pick_next" in st.session_state:  # set after a rename or removal: applied before the picker is drawn
        st.session_state["module-pick"] = st.session_state.pop("module_pick_next")
    pick = st.selectbox("Module", table.code.tolist(), index=None, placeholder="Pick a module", key="module-pick")
    if pick:
        current = table.set_index("code").loc[pick]
        position = table.code.tolist().index(pick)
        with st.form(f"module-edit:{pick}", border=False):
            c1, c2 = st.columns([1, 3])
            new_code = c1.text_input("Module", value=pick, help="Renaming carries the new name onto its rows")
            new_description = c2.text_input("Description (optional)", value=_clean(current.description) or "")
            save, remove = st.columns([1, 5])
            saved = save.form_submit_button("Save", type="primary")
            removing = remove.form_submit_button("Remove module", type="tertiary", icon=":material/delete:")
        with st.container(horizontal=True):
            st.button("Move up", icon=":material/arrow_upward:", disabled=position == 0,
                      on_click=_move, args=(pick, -1))
            st.button("Move down", icon=":material/arrow_downward:", disabled=position == len(table) - 1,
                      on_click=_move, args=(pick, 1))
        if saved:
            if not _clean(new_code):
                st.error("Enter a module name.")
            elif modules.update(pick, _clean(new_code), _clean(new_description)):
                st.session_state.modules_flash = f"Saved {_clean(new_code)}."
                st.session_state.module_pick_next = _clean(new_code)
                st.rerun()
            else:
                st.error(f"{_clean(new_code)} is already a module.")
        if removing:
            _confirm_remove(pick, int(current.rows))
