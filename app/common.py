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
