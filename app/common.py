"""Shared dashboard helpers: database access, cached model, status display."""

import streamlit as st

from kb import store
from kb.embed import Embedder
from kb.statuses import STATUSES

STATUS_LABELS = {
    "STANDARD": "Standard", "SUPPORTED": "Supported", "PARTIAL": "Partial", "SCHEDULED": "Scheduled",
    "FUTURE": "Future", "CUSTOM": "Custom", "THIRD_PARTY": "Third party", "NOT_SUPPORTED": "Not supported",
    "NEEDS_DISCUSSION": "Needs discussion", "NOT_APPLICABLE": "N/A", "NARRATIVE": "Narrative",
}
# Badge color always travels with the text label, never alone.
_STATUS_COLORS = {
    "STANDARD": "green", "SUPPORTED": "green", "NOT_SUPPORTED": "red", "NEEDS_DISCUSSION": "violet",
    "NARRATIVE": "gray", "NOT_APPLICABLE": "gray",
}


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status, status or "No status")


def _status_color(status: str | None) -> str:
    return _STATUS_COLORS.get(status, "orange" if status else "gray")


def status_badge(status: str | None) -> None:
    st.badge(status_label(status), color=_status_color(status))


def status_badge_md(status: str | None) -> str:
    """The same badge as Markdown, for labels (e.g. an expander's title)."""
    return f":{_status_color(status)}-badge[{status_label(status)}]"


def status_help() -> str:
    return "\n".join(f"- **{status_label(k)}**: {v}" for k, v in STATUSES.items())


def connect():
    """A fresh connection per script run (connections can't be shared across Streamlit threads)."""
    return store.connect()


@st.cache_resource(show_spinner="Loading embedding model...")
def embedder() -> Embedder:
    model = Embedder()
    model.embed(["warm up"])  # load the ONNX model once per server process
    return model


def db_version() -> str:
    """Changes whenever the knowledge base changes; use as a cache key for derived data."""
    conn = connect()
    try:
        return store.data_version(conn)
    finally:
        conn.close()


# Tinted section cards: st.container(border=True, key="card-<name>"), each starting with an
# st.subheader, so it's clear where one section ends and the next begins. Inputs on a card get a
# lighter fill and a faint outline, so they stand out from the card's tint.
_CARD_CSS = """<style>
[class*="st-key-card-"] {
    padding: 1.25rem 1.5rem 1.5rem; margin-top: 0.75rem; background: rgba(151, 166, 195, 0.06);
}
[class*="st-key-card-"] [data-testid="stHeading"] h3 {
    margin-top: 0; padding: 0 0 0.6rem; border-bottom: 1px solid rgba(128, 128, 128, 0.25); margin-bottom: 0.5rem;
}
[class*="st-key-card-"] [data-testid="stSelectbox"] [role="group"],
[class*="st-key-card-"] [data-testid="stTextInputRootElement"],
[class*="st-key-card-"] [data-testid="stTextAreaRootElement"] {
    background: #2C3039; border-color: rgba(255, 255, 255, 0.14);
}
</style>"""


def card_styles() -> None:
    st.html(_CARD_CSS)
