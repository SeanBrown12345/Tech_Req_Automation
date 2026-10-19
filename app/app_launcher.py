"""App launcher: the Google-style "apps" grid button at the left of the top banner, which drops down a panel
linking to each of our tools (RFP Pilot, BidWrite, ReqFill) so it's one click to hop between them.

This file and the app_launcher_icons/ folder next to it are kept identical in all three repos (RFP Pilot,
BidWrite, ReqFill) — change it in one, copy it to the others.

Pure HTML + CSS, no script, so it survives Streamlit's HTML sanitizer: the panel shows while the button (or a
link in it) has focus and hides when focus leaves, i.e. on any click outside it. Icons are CSS background
images because st.html strips <img> and <svg>.

Links default to where the apps live behind RFP Pilot's nginx front door (deploy/nginx.conf). Override each
with RFPPILOT_URL / BIDWRITE_URL / REQFILL_URL, e.g. when running the apps on separate local ports.
"""
import base64
import os
from pathlib import Path

_ICONS = Path(__file__).with_name("app_launcher_icons")

# (key, display name, default URL, icon file). Each URL can be overridden by the <KEY>_URL env var.
APPS = [
    ("rfppilot", "RFP Pilot", "/", "rfppilot.png"),
    ("bidwrite", "BidWrite", "/RFPDrafter/", "bidwrite.svg"),
    ("reqfill", "ReqFill", "/ReqFill/", "reqfill.png"),
]

# Google's nine-dot grid, white so it reads on the maroon banner.
_GRID_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#fff">'
    + "".join(f'<circle cx="{x}" cy="{y}" r="2"/>' for y in (5, 12, 19) for x in (5, 12, 19))
    + "</svg>"
)


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _icon_uri(name: str) -> str:
    mime = "image/svg+xml" if name.endswith(".svg") else "image/png"
    return _data_uri((_ICONS / name).read_bytes(), mime)


def _url(key: str, default: str) -> str:
    return os.environ.get(f"{key.upper()}_URL", default)


def css() -> str:
    """The launcher's stylesheet (bare CSS, no <style> tag)."""
    icons = "".join(
        f'.app-launcher-icon-{key} {{ background-image: url("{_icon_uri(icon)}"); }}\n'
        for key, _, _, icon in APPS
    )
    return """
.app-launcher { position: relative; flex: 0 0 auto; margin-left: -.5rem; }
.app-launcher-btn {
  display: block; width: 2.5rem; height: 2.5rem; padding: 0; margin: 0; border: 0; border-radius: 50%;
  cursor: pointer; background: transparent url("__GRID__") no-repeat center / 1.5rem;
  transition: background-color .15s;
}
.app-launcher-btn:hover, .app-launcher:focus-within .app-launcher-btn { background-color: rgba(255,255,255,.18); }
.app-launcher-btn:focus { outline: none; }
.app-launcher-btn:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }
/* The panel. Hiding waits out the fade (visibility delay), so a click on a link still lands even in
   browsers that don't move focus to a clicked link. */
.app-launcher-menu {
  position: absolute; top: calc(100% + .5rem); left: 0; width: 19rem; box-sizing: border-box;
  display: grid; grid-template-columns: repeat(3, 1fr); gap: .25rem; padding: .75rem;
  background: #fff; border-radius: 1.25rem;
  box-shadow: 0 4px 8px 3px rgba(0,0,0,.15), 0 1px 3px rgba(0,0,0,.3);
  opacity: 0; visibility: hidden; transform: translateY(-4px);
  transition: opacity .12s ease, transform .12s ease, visibility 0s linear .12s;
}
.app-launcher:focus-within .app-launcher-menu {
  opacity: 1; visibility: visible; transform: none; transition: opacity .12s ease, transform .12s ease;
}
.app-launcher-menu a, .app-launcher-menu a:link, .app-launcher-menu a:visited,
.app-launcher-menu a:hover, .app-launcher-menu a:active {
  display: flex; flex-direction: column; align-items: center; gap: .5rem; margin: 0;
  padding: .85rem .25rem .7rem; border-radius: .9rem;
  color: #202124 !important; text-decoration: none !important; opacity: 1;
  font-family: inherit; font-size: .8rem; font-weight: 500; line-height: 1.2; white-space: nowrap;
  transition: background-color .12s;
}
.app-launcher-menu a:hover, .app-launcher-menu a:focus-visible { background: #F1F3F4; outline: none; }
.app-launcher-menu a.current { background: #F9EBEF; }
/* Dark panel, for the apps with a dark theme. */
.app-launcher.dark .app-launcher-menu {
  background: #1F232A; border: 1px solid #343A44; box-shadow: 0 6px 16px 4px rgba(0,0,0,.45);
}
.app-launcher.dark .app-launcher-menu a, .app-launcher.dark .app-launcher-menu a:link,
.app-launcher.dark .app-launcher-menu a:visited, .app-launcher.dark .app-launcher-menu a:hover,
.app-launcher.dark .app-launcher-menu a:active { color: #E6E8EB !important; }
.app-launcher.dark .app-launcher-menu a:hover, .app-launcher.dark .app-launcher-menu a:focus-visible {
  background: #2C313A;
}
.app-launcher.dark .app-launcher-menu a.current { background: rgba(139,21,56,.35); }
.app-launcher-icon {
  display: block; width: 3rem; height: 3rem; border-radius: .7rem;
  background: no-repeat center / contain;
}
""".replace("__GRID__", _data_uri(_GRID_SVG.encode(), "image/svg+xml")) + icons


def html(current: str, dark: bool = False) -> str:
    """The launcher markup (one line, so Markdown renderers keep it as a single HTML block). `current` is the
    key of the app it sits in; that tile is highlighted. `dark` gives the panel a dark background."""
    tiles = ""
    for key, name, default, _ in APPS:
        here = ' class="current" aria-current="page"' if key == current else ""
        tiles += (f'<a{here} href="{_url(key, default)}">'
                  f'<span class="app-launcher-icon app-launcher-icon-{key}"></span>{name}</a>')
    return (
        f'<div class="app-launcher{" dark" if dark else ""}">'
        '<button type="button" class="app-launcher-btn" title="Cayenta apps" aria-label="Cayenta apps"></button>'
        f'<nav class="app-launcher-menu" aria-label="Cayenta apps">{tiles}</nav>'
        "</div>"
    )
