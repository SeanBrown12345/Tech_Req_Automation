"""Top banner shared with our other apps: company logo, separator, app name, and a gear that opens
the knowledge-base admin pages."""

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
.app-top-banner {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 1000060;
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
/* Make room for the banner: push Streamlit's toolbar, sidebar and page content below it. */
header[data-testid="stHeader"] {{ top: {BANNER_HEIGHT}; }}
section[data-testid="stSidebar"] {{ top: {BANNER_HEIGHT}; height: calc(100vh - {BANNER_HEIGHT}) !important; }}
[data-testid="stAppViewContainer"] > .stMain,
[data-testid="stAppViewContainer"] > section.main {{ margin-top: {BANNER_HEIGHT}; }}
/* The banner is fixed, so its own (empty) slot in the page flow shouldn't take space. */
[data-testid="stElementContainer"]:has(.app-top-banner) {{ position: absolute; height: 0; margin: 0; }}
/* Gear: a real Streamlit page link (no page reload), pinned into the banner's right side. */
.st-key-reqfill-gear {{
    position: fixed; top: calc(({BANNER_HEIGHT} - 2.25rem) / 2); right: 1rem; z-index: 1000061;
    width: auto; gap: 0;
}}
.st-key-reqfill-gear a {{
    width: 2.25rem; height: 2.25rem; padding: 0; justify-content: center; border-radius: 6px;
    background: transparent;
}}
.st-key-reqfill-gear a:hover {{ background: rgba(255, 255, 255, 0.15); }}
.st-key-reqfill-gear a span, .st-key-reqfill-gear a svg {{ color: #fff !important; fill: #fff; font-size: 1.35rem; }}
.st-key-reqfill-gear a p {{ display: none; }}
</style>
"""


def render_header(admin_page) -> None:
    """Draw the banner; the gear links to `admin_page` (an st.Page)."""
    st.html(
        _CSS
        + f'<div class="app-top-banner"><span class="app-top-logo" role="img" aria-label="Cayenta"></span>'
        '<span class="banner-sep"></span>'
        f'<span class="app-top-title">{APP_NAME}</span></div>'
    )
    with st.container(key="reqfill-gear"):
        st.page_link(admin_page, label="Knowledge base admin", icon=":material/settings:",
                     help="Knowledge base admin: overview, search, add worksheets")
