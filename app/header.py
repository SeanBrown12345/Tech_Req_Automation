"""Top banner shared with our other apps: company logo (links home), separator, app name, and on the
right a link to the task board and a gear that opens the knowledge-base admin pages."""

import base64
from pathlib import Path

import streamlit as st

APP_NAME = "ReqFill"
BANNER_HEIGHT = "3.5rem"
BANNER_COLOR = "#8B1538"

# Streamlit's HTML sanitizer strips both inline <svg> and <img>, so the logo is drawn as a CSS
# background image (stylesheets pass through untouched).
_LOGO_URI = "data:image/svg+xml;base64," + base64.b64encode(
    (Path(__file__).parent / "assets" / "logo.svg").read_bytes()).decode()

_CSS = f"""
<style>
/* The banner's layer sits just under a fullscreen table (Streamlit's fullscreen frame is at 1000050), so an
   expanded table covers it instead of losing its top rows behind it. */
.app-top-banner {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 1000040;
    height: {BANNER_HEIGHT}; padding: 0 1.25rem;
    display: flex; align-items: center; gap: 0;
    background: {BANNER_COLOR}; color: #fff;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18);
}}
.app-top-logo {{
    flex: 0 0 auto; display: block; width: 120px; height: 27px;
    background: url("{_LOGO_URI}") no-repeat center / contain;
}}
.banner-sep {{
    flex: 0 0 auto; width: 1px; height: 1.6rem; margin: 0 0.9rem;
    background: rgba(255, 255, 255, 0.55);
}}
.app-top-title {{
    font-size: 1.15rem; font-weight: 600; letter-spacing: 0.01em; color: #fff; white-space: nowrap;
}}
/* Make room for the banner: push the sidebar and page content below it. Streamlit's own toolbar
   strip is empty (toolbarMode = "minimal"), so hide it and reclaim its space. */
header[data-testid="stHeader"] {{ display: none; }}
[data-testid="stMainBlockContainer"] {{ padding-top: 2.5rem; }}
section[data-testid="stSidebar"] {{ top: {BANNER_HEIGHT}; height: calc(100vh - {BANNER_HEIGHT}) !important; }}
[data-testid="stAppViewContainer"] > .stMain,
[data-testid="stAppViewContainer"] > section.main {{ margin-top: {BANNER_HEIGHT}; }}
/* The banner is fixed, so its own (empty) slot in the page flow shouldn't take space. */
[data-testid="stElementContainer"]:has(.app-top-banner) {{ position: absolute; height: 0; margin: 0; }}
/* Task board and gear: real Streamlit page links (no page reload), pinned into the banner's right side. */
.st-key-reqfill-gear {{
    position: fixed; top: 0; right: 1rem; z-index: 1000041; width: auto; gap: 0.25rem;
}}
.st-key-reqfill-gear [data-testid="stElementContainer"], .st-key-reqfill-gear [data-testid="stPageLink"] {{
    margin: 0; padding: 0;
}}
/* Full banner height with the icon centred in it, so it lines up with the logo and title. */
.st-key-reqfill-gear a {{
    width: 2.25rem; height: {BANNER_HEIGHT}; min-height: 0; margin: 0; padding: 0;
    align-items: center; justify-content: center; background: transparent;
}}
.st-key-reqfill-gear a:hover, .st-key-reqfill-gear a:focus {{ background: transparent !important; }}
.st-key-reqfill-gear a span, .st-key-reqfill-gear a svg {{
    color: #fff !important; fill: #fff; font-size: 1.35rem; transition: color 0.15s;
}}
/* Hover: the icon itself shifts to a soft pink instead of a background highlight. */
.st-key-reqfill-gear a:hover span, .st-key-reqfill-gear a:hover svg {{ color: #F2C4D2 !important; fill: #F2C4D2; }}
.st-key-reqfill-gear a p {{ display: none; }}
/* Home link: an invisible page link laid exactly over the logo. */
.st-key-reqfill-home {{
    position: fixed; top: 0; left: 1.25rem; z-index: 1000041; width: 120px; gap: 0;
}}
.st-key-reqfill-home [data-testid="stElementContainer"], .st-key-reqfill-home [data-testid="stPageLink"] {{
    margin: 0; padding: 0;
}}
.st-key-reqfill-home a {{
    width: 120px; height: {BANNER_HEIGHT}; min-height: 0; padding: 0; margin: 0;
    background: transparent !important; border-radius: 0; cursor: pointer;
}}
.st-key-reqfill-home a > * {{ display: none; }}
</style>
"""


def render_header(home_page, board_page, admin_page, in_admin: bool = False) -> None:
    """Draw the banner; the logo links to `home_page`, the task icon to `board_page` and the gear to
    `admin_page` (st.Page objects). On the admin pages the gear becomes a back arrow to `home_page`."""
    st.html(
        _CSS
        + f'<div class="app-top-banner"><span class="app-top-logo" role="img" aria-label="Cayenta"></span>'
        '<span class="banner-sep"></span>'
        f'<span class="app-top-title">{APP_NAME}</span></div>'
    )
    with st.container(key="reqfill-home"):
        st.page_link(home_page, label="Home")
    with st.container(key="reqfill-gear", horizontal=True):
        st.page_link(board_page, label="Task board", icon=":material/assignment_ind:", help="Task board")
        if in_admin:
            st.page_link(home_page, label="Back to drafting", icon=":material/arrow_back:")
        else:
            st.page_link(admin_page, label="Admin", icon=":material/settings:")
