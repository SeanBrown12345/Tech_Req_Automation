"""SME list: the people rows can be assigned to for review. Add, edit, remove, or import them from a CSV."""

import pandas as pd
import streamlit as st

from kb import draft

st.title("SMEs")
st.caption("The people drafted rows can be assigned to for review. They appear in the **Assigned to** "
           "lists and on the task board.")

if flash := st.session_state.pop("smes_flash", None):
    st.toast(flash, icon=":material/check:")

table = draft.people_table()
if table.empty:
    st.info("No SMEs yet. Add them below, one at a time or from a CSV.")
else:
    st.dataframe(
        table.fillna(""), hide_index=True, width="stretch",
        column_config={
            "name": st.column_config.TextColumn("Name"),
            "email": st.column_config.TextColumn("Email"),
            "areas": st.column_config.TextColumn("Areas", width="large"),
            "open": st.column_config.NumberColumn("Rows to review", help="Assigned, not yet approved, all drafts"),
            "approved": st.column_config.NumberColumn("Approved"),
        })


def _clean(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


# ---- Add -------------------------------------------------------------------------------------------
st.subheader("Add an SME")
with st.form("sme-add", clear_on_submit=True, border=False):
    c1, c2 = st.columns(2)
    name = c1.text_input("Name", placeholder="e.g. Rachelle Clowers")
    email = c2.text_input("Email (optional)")
    areas = st.text_area("Areas (optional)", height=80,
                         placeholder="What they review, e.g. CIS functional requirements; billing & rates")
    if st.form_submit_button("Add SME", type="primary"):
        if not _clean(name):
            st.error("Enter a name.")
        elif draft.add_person(_clean(name), _clean(email), _clean(areas)):
            st.session_state.smes_flash = f"Added {_clean(name)}."
            st.rerun()
        else:
            st.error(f"There's already an SME called {_clean(name)}.")


# ---- Edit / remove ---------------------------------------------------------------------------------
@st.dialog("Remove SME?")
def _confirm_remove(person: dict):
    st.write(f"**{person['name']}** will be taken off the SME list.")
    if person["open"]:
        st.write(f"Their **{person['open']:,}** rows still to review go to the *Unassigned by an SME* pool on the "
                 "draft pages, ready to reassign.")
    if person["approved"]:
        st.write(f"The {person['approved']:,} rows they approved stay approved.")
    cancel, remove = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if remove.button("Remove", type="primary", width="stretch"):
        draft.remove_person(person["name"])
        st.session_state.smes_flash = f"Removed {person['name']}."
        st.session_state.sme_pick_next = None
        st.rerun()


if not table.empty:
    st.subheader("Edit or remove")
    if "sme_pick_next" in st.session_state:  # set after a rename or removal: applied before the picker is drawn
        st.session_state["sme-pick"] = st.session_state.pop("sme_pick_next")
    pick = st.selectbox("SME", table.name.tolist(), index=None, placeholder="Pick an SME", key="sme-pick")
    if pick:
        person = table.set_index("name").loc[pick].to_dict() | {"name": pick}
        with st.form(f"sme-edit:{pick}", border=False):
            c1, c2 = st.columns(2)
            new_name = c1.text_input("Name", value=pick, help="Renaming keeps their assigned rows with them")
            new_email = c2.text_input("Email (optional)", value=_clean(person["email"]) or "")
            new_areas = st.text_area("Areas (optional)", value=_clean(person["areas"]) or "", height=80)
            save, remove = st.columns([1, 5])
            saved = save.form_submit_button("Save", type="primary")
            removing = remove.form_submit_button("Remove SME", type="tertiary", icon=":material/person_remove:")
        if saved:
            if not _clean(new_name):
                st.error("Enter a name.")
            elif draft.update_person(pick, _clean(new_name), _clean(new_email), _clean(new_areas)):
                st.session_state.smes_flash = f"Saved {_clean(new_name)}."
                st.session_state.sme_pick_next = _clean(new_name)
                st.rerun()
            else:
                st.error(f"There's already an SME called {_clean(new_name)}.")
        if removing:
            _confirm_remove(person)


# ---- Import ----------------------------------------------------------------------------------------
st.subheader("Import from CSV")
st.caption("A CSV with a **name** column, and optionally **email** and **areas**. SMEs already on the list "
           "are left as they are.")
uploaded = st.file_uploader("SME list (.csv)", type=["csv"], key="sme-csv")
if uploaded:
    try:
        df = pd.read_csv(uploaded, encoding="utf-8-sig", dtype=str)
    except Exception as e:  # noqa: BLE001 - shown to the user
        st.error(f"Couldn't read that file: {e}")
        st.stop()
    df.columns = [c.strip().lower() for c in df.columns]
    if "area" in df.columns and "areas" not in df.columns:
        df = df.rename(columns={"area": "areas"})
    if "name" not in df.columns:
        st.error("The file needs a **name** column.")
        st.stop()
    df = df.reindex(columns=["name", "email", "areas"]).map(_clean).dropna(subset=["name"])
    known = {n.lower() for n in table.name}
    df["status"] = ["Already on the list" if n.lower() in known else "New" for n in df.name]
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"areas": st.column_config.TextColumn(width="large")})
    new = df[df.status == "New"].drop_duplicates(subset="name")
    if st.button(f"Add {len(new)} new SME{'s' * (len(new) != 1)}", type="primary", disabled=new.empty):
        added = sum(draft.add_person(r.name, r.email, r.areas) for r in new.itertuples())
        st.session_state.smes_flash = f"Added {added} SME{'s' * (added != 1)}."
        st.session_state.pop("sme-csv", None)
        st.rerun()
