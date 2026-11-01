"""Wareneinsatz-Pilot web app: upload, review, ingredients, menu costing and a dashboard.

    uv run wareneinsatz serve     # the API first, in one terminal
    uv run wareneinsatz ui        # then this, in another: http://localhost:8501

The app only talks to the REST API (API_URL in .env), never to the database directly.
"""

import streamlit as st

from webui import views

st.set_page_config(page_title="Wareneinsatz-Pilot", page_icon="🍔", layout="wide")

with st.sidebar:
    st.text_input("Your name (stored with reviews)", key="reviewer", placeholder="reviewer")

page = st.navigation(
    [
        st.Page(views.dashboard, title="Dashboard", icon="📊", url_path="dashboard", default=True),
        st.Page(views.upload, title="Upload", icon="📥", url_path="upload"),
        st.Page(views.review, title="Review", icon="✅", url_path="review"),
        st.Page(views.matching, title="Ingredients", icon="🔗", url_path="ingredients"),
        st.Page(views.menu, title="Menu costing", icon="🍔", url_path="menu"),
    ]
)
page.run()
