"""Admin page for the people rows are assigned to: SMEs (review in the app) or partners (answer by email).
Add, edit, remove, or import them from a CSV. Partners also list the modules they cover."""

import json

import pandas as pd
import streamlit as st

from kb import draft, modules

_TEXT = {
    draft.SME: {"title": "SMEs", "one": "SME", "many": "SMEs",
                "intro": "The people drafted rows can be assigned to for review. They appear in the **Assigned to** "
                         "lists and on the task board.",
                "example": "e.g. Rachelle Clowers", "areas": "What they review, e.g. CIS functional requirements"},
    draft.PARTNER: {"title": "Partners", "one": "partner", "many": "partners",
                    "intro": "Business partners who answer their rows by email: export their trimmed worksheet from "
                             "a draft's page, then import their reply. They never use the app; their progress is "
                             "tracked on the draft page and the task board.",
                    "example": "e.g. SilverBlaze", "areas": "What they provide, e.g. Customer Engagement Portal"},
}


def _clean(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _modules(value) -> list[str]:
    return json.loads(value) if isinstance(value, str) and value else []


def render(kind: str) -> None:
    text = _TEXT[kind]
    partner = kind == draft.PARTNER
    module_names = [m for m in modules.names() if m != modules.GENERAL]
    key = lambda name: f"{kind}-{name}"

    st.title(text["title"])
    st.caption(text["intro"])
    if flash := st.session_state.pop(key("flash"), None):
        st.toast(flash, icon=":material/check:")

    table = draft.people_table(kind)
    if table.empty:
        st.info(f"No {text['many']} yet. Add them below, one at a time or from a CSV.")
    else:
        shown = table.assign(modules=[", ".join(_modules(m)) for m in table.modules])
        if not partner:
            shown = shown.drop(columns="modules")
        st.dataframe(shown.fillna(""), hide_index=True, width="stretch", column_config={
            "name": st.column_config.TextColumn("Name"),
            "email": st.column_config.TextColumn("Email"),
            "areas": st.column_config.TextColumn("Areas", width="large"),
            "modules": st.column_config.TextColumn("Modules", help="Suggested for these modules' rows when assigning"),
            "open": st.column_config.NumberColumn("Rows open", help="Assigned, not yet approved, all drafts"),
            "approved": st.column_config.NumberColumn("Approved"),
        })

    # ---- Add ---------------------------------------------------------------------------------------
    st.subheader(f"Add {'a partner' if partner else 'an SME'}")
    with st.form(key("add"), clear_on_submit=True, border=False):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", placeholder=text["example"])
        email = c2.text_input("Email (optional)")
        areas = st.text_area("Areas (optional)", height=80, placeholder=text["areas"])
        covered = st.multiselect("Modules they cover", module_names, placeholder="Pick modules") if partner else []
        if st.form_submit_button(f"Add {text['one']}", type="primary"):
            if not _clean(name):
                st.error("Enter a name.")
            elif draft.add_person(_clean(name), _clean(email), _clean(areas), kind, covered):
                st.session_state[key("flash")] = f"Added {_clean(name)}."
                st.rerun()
            else:
                st.error(f"The name {_clean(name)} is already taken (by an SME or a partner).")

    # ---- Edit / remove -----------------------------------------------------------------------------
    @st.dialog(f"Remove {text['one']}?")
    def confirm_remove(person: dict):
        st.write(f"**{person['name']}** will be taken off the {text['one']} list.")
        if person["open"]:
            st.write(f"Their **{person['open']:,}** rows still open go to the *Unassigned by an SME* pool on the "
                     "draft pages, ready to reassign.")
        if person["approved"]:
            st.write(f"The {person['approved']:,} approved rows stay approved.")
        cancel, remove = st.columns(2)
        if cancel.button("Cancel", width="stretch"):
            st.rerun()
        if remove.button("Remove", type="primary", width="stretch"):
            draft.remove_person(person["name"])
            st.session_state[key("flash")] = f"Removed {person['name']}."
            st.session_state[key("pick-next")] = None
            st.rerun()

    if not table.empty:
        st.subheader("Edit or remove")
        if key("pick-next") in st.session_state:  # set after a rename or removal: applied before the picker
            st.session_state[key("pick")] = st.session_state.pop(key("pick-next"))
        pick = st.selectbox(text["one"].capitalize(), table.name.tolist(), index=None,
                            placeholder=f"Pick {'a partner' if partner else 'an SME'}", key=key("pick"))
        if pick:
            person = table.set_index("name").loc[pick].to_dict() | {"name": pick}
            with st.form(key(f"edit:{pick}"), border=False):
                c1, c2 = st.columns(2)
                new_name = c1.text_input("Name", value=pick, help="Renaming keeps their assigned rows with them")
                new_email = c2.text_input("Email (optional)", value=_clean(person["email"]) or "")
                new_areas = st.text_area("Areas (optional)", value=_clean(person["areas"]) or "", height=80)
                new_covered = st.multiselect(
                    "Modules they cover", module_names,
                    default=[m for m in _modules(person["modules"]) if m in module_names]) if partner else []
                save, remove = st.columns([1, 5])
                saved = save.form_submit_button("Save", type="primary")
                removing = remove.form_submit_button(f"Remove {text['one']}", type="tertiary",
                                                     icon=":material/person_remove:")
            if saved:
                if not _clean(new_name):
                    st.error("Enter a name.")
                elif draft.update_person(pick, _clean(new_name), _clean(new_email), _clean(new_areas), new_covered):
                    st.session_state[key("flash")] = f"Saved {_clean(new_name)}."
                    st.session_state[key("pick-next")] = _clean(new_name)
                    st.rerun()
                else:
                    st.error(f"The name {_clean(new_name)} is already taken.")
            if removing:
                confirm_remove(person)

    # ---- Import ------------------------------------------------------------------------------------
    st.subheader("Import from CSV")
    st.caption("A CSV with a **name** column, and optionally **email** and **areas**"
               + (" and **modules** (separated by ;). Without a modules column, modules named in the areas text "
                  "are picked up." if partner else ".")
               + f" Anyone already on a list is left as they are.")
    uploaded = st.file_uploader(f"{text['title']} (.csv)", type=["csv"], key=key("csv"))
    if not uploaded:
        return
    try:
        df = pd.read_csv(uploaded, encoding="utf-8-sig", dtype=str)
    except Exception as e:  # noqa: BLE001 - shown to the user
        st.error(f"Couldn't read that file: {e}")
        return
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.rename(columns={"area": "areas", "module": "modules"})
    if "name" not in df.columns:
        st.error("The file needs a **name** column.")
        return
    df = df.reindex(columns=["name", "email", "areas", "modules"]).map(_clean).dropna(subset=["name"])
    if partner:
        df["modules"] = [[m.strip() for m in v.split(";") if m.strip() in module_names] if v else modules.mentioned(a or "")
                         for v, a in zip(df.modules, df.areas)]
    else:
        df = df.drop(columns="modules")
    taken = {n.lower() for n in draft.people()}
    df["status"] = ["Already on a list" if n.lower() in taken else "New" for n in df.name]
    st.dataframe(df.assign(**({"modules": [", ".join(m) for m in df.modules]} if partner else {})),
                 hide_index=True, width="stretch", column_config={"areas": st.column_config.TextColumn(width="large")})
    new = df[df.status == "New"].drop_duplicates(subset="name")
    label = text["one"] if len(new) == 1 else text["many"]
    if st.button(f"Add {len(new)} new {label}", type="primary", disabled=new.empty):
        added = sum(draft.add_person(r.name, r.email, r.areas, kind, r.modules if partner else None)
                    for r in new.itertuples())
        st.session_state[key("flash")] = f"Added {added} {text['one'] if added == 1 else text['many']}."
        st.session_state.pop(key("csv"), None)
        st.rerun()
