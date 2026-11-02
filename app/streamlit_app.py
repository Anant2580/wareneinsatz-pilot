"""Wareneinsatz-Pilot web app: upload, review, ingredients, menu costing and a dashboard.

    uv run wareneinsatz serve     # the API first, in one terminal
    uv run wareneinsatz ui        # then this, in another: http://localhost:8501

The app only talks to the REST API (API_URL in .env), never to the database directly.
Colours and fonts: .streamlit/config.toml.
"""

from pathlib import Path

import streamlit as st

from webui import nav, style

STATIC = Path(__file__).parent / "static"

st.set_page_config(page_title="Wareneinsatz-Pilot", page_icon=str(STATIC / "icon.svg"), layout="wide")
st.logo(str(STATIC / "logo.svg"), icon_image=str(STATIC / "icon.svg"), size="large")
page = st.navigation(nav.sections())  # pages grouped by the job they do

with st.sidebar:
    st.text_input("Your name", key="reviewer", placeholder="Saved with reviews and approvals")
    decor = st.toggle("Food drawings in the background", value=True, key="decor")

style.apply(decor=decor)

page.run()
