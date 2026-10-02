"""Settings-style layout for the knowledge-base admin pages (modelled on GitHub's settings):
a page header, then an always-visible section list on the left and the page content on the right."""

import streamlit as st

ACCENT = "#8B1538"

_CSS = f"""
<style>
.admin-head {{ padding: 0.25rem 0 1rem; border-bottom: 1px solid rgba(128, 128, 128, 0.25); }}
.admin-head-title {{ font-size: 1.5rem; font-weight: 600; line-height: 1.3; }}
.admin-head-sub {{ font-size: 0.9rem; opacity: 0.65; margin-top: 0.2rem; }}

/* Section list */
.st-key-admin-nav {{ gap: 0.1rem; }}
.st-key-admin-nav [data-testid="stCaptionContainer"] {{
    padding: 0 0.75rem 0.35rem; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.03em;
}}
.st-key-admin-nav [data-testid="stElementContainer"]:has([data-testid="stPageLink"]),
.st-key-admin-nav [data-testid="stPageLink"], .st-key-admin-nav [data-testid="stPageLink"] > div {{
    width: 100% !important;
}}
.st-key-admin-nav a {{
    position: relative; width: 100%; padding: 0.35rem 0.75rem; border-radius: 6px; background: transparent;
}}
.st-key-admin-nav a:hover {{ background: rgba(128, 128, 128, 0.12); }}
.st-key-admin-nav-active a {{ background: rgba(128, 128, 128, 0.12); }}
.st-key-admin-nav-active a p {{ font-weight: 600; }}
.st-key-admin-nav-active a::before {{
    content: ""; position: absolute; left: -0.55rem; top: 0.35rem; bottom: 0.35rem; width: 4px;
    border-radius: 6px; background: {ACCENT};
}}

/* Content: page titles become section headings with a rule under them, as on GitHub. */
.st-key-admin-content h1 {{
    font-size: 1.6rem; font-weight: 600; padding: 0 0 0.6rem;
    border-bottom: 1px solid rgba(128, 128, 128, 0.25); margin-bottom: 0.5rem;
}}
</style>
"""


def admin_layout(pages: list, current) -> st.delta_generator.DeltaGenerator:
    """Draw the admin header and section list; return the container the current page renders into."""
    st.html(_CSS + '<div class="admin-head"><div class="admin-head-title">Knowledge base admin</div>'
            '<div class="admin-head-sub">Manage the past answers ReqFill drafts from.</div></div>')
    nav, content = st.columns([1, 4], gap="large")
    with nav.container(key="admin-nav"):
        st.caption("KNOWLEDGE BASE")
        for p in pages:
            if p.url_path == current.url_path:
                with st.container(key="admin-nav-active"):
                    st.page_link(p)
            else:
                st.page_link(p)
    return content.container(key="admin-content")
