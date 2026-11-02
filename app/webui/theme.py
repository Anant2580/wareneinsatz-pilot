"""The sun / moon button at the top right: light theme by default, dark on request.

Streamlit has no API to change the theme from Python. Its own settings menu stores the choice in
the browser (localStorage key `stActiveTheme-<path>-v2`, value "Light", "Dark" or "System") and
applies it when the page loads. This button writes the same value and reloads the page, so every
part of the app switches, the canvas-drawn tables and the charts too.

A reload starts a new session, so the reviewer name and the drawings switch travel along in the
URL once and are read back by restore().
"""

from __future__ import annotations

import json

import streamlit as st

PATHS = ["/", "/dashboard", "/upload", "/review", "/ingredients", "/menu"]  # Streamlit stores one value per path
CARRIED = {"reviewer": "by", "decor": "decor"}  # session values kept across the reload, and their URL names


def current() -> str:
    return getattr(getattr(st.context, "theme", None), "type", None) or "light"


def restore() -> None:
    """Read back what the theme switch carried in the URL, then remove it from the URL."""
    params = st.query_params
    if "by" in params:
        st.session_state["reviewer"] = params["by"]
    if "decor" in params:
        st.session_state["decor"] = params["decor"] == "1"
    for name in CARRIED.values():
        if name in params:
            del params[name]


def switch() -> None:
    """The button (sun in light mode, crescent moon in dark mode) plus the script that applies a choice."""
    dark = current() == "dark"
    with st.container(horizontal=True, horizontal_alignment="right", key="theme_bar"):
        clicked = st.button(
            "",
            icon=":material/dark_mode:" if dark else ":material/light_mode:",
            key="theme_toggle",
            type="tertiary",
            help="Switch to light mode" if dark else "Switch to dark mode",
        )
    if clicked:
        _apply("Light" if dark else "Dark", always=True)
    elif not st.session_state.get("theme_checked"):
        # First visit: no choice stored yet (or "System") means "follow the system". Store light instead, and
        # reload only if the system is dark (the browser knows; Python may not know yet on the first run).
        st.session_state["theme_checked"] = True
        _apply("Light", always=False, reload="system-dark")


def _apply(theme: str, *, always: bool, reload: bool | str = True) -> None:
    carried = {
        "by": st.session_state.get("reviewer") or "",
        "decor": "1" if st.session_state.get("decor", True) else "0",
    }
    script = f"""
<script>
(() => {{
  const key = (path) => "stActiveTheme-" + path + "-v2";
  const always = {json.dumps(always)};
  const stored = localStorage.getItem(key(window.location.pathname));
  if (!always && stored !== null && stored !== '"System"') return;  // Streamlit itself stores "System" at first
  for (const path of {json.dumps(PATHS)}.concat([window.location.pathname])) {{
    localStorage.setItem(key(path), JSON.stringify({json.dumps(theme)}));
  }}
  const reload = {json.dumps(reload)};
  if (!reload) return;
  if (reload === "system-dark" && !window.matchMedia("(prefers-color-scheme: dark)").matches) return;
  const url = new URL(window.location.href);
  for (const [name, value] of Object.entries({json.dumps(carried)})) {{
    if (value) url.searchParams.set(name, value);
  }}
  window.location.replace(url.toString());
}})();
</script>
"""
    with st.container(key="theme_script"):  # hidden by CSS; the script still runs
        st.html(script, unsafe_allow_javascript=True)
