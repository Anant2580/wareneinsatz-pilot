"""The navigation: one place that knows every page, so pages can link to each other.

Streamlit wants fresh page objects on every run, so they are built each time, not cached.
"""

from __future__ import annotations

import streamlit as st

from webui import views


def pages() -> dict[str, st.Page]:
    return {
        "dashboard": st.Page(
            views.dashboard, title="Dashboard", icon=":material/space_dashboard:", url_path="dashboard", default=True
        ),  # fmt: skip
        "upload": st.Page(views.upload, title="Upload", icon=":material/upload_file:", url_path="upload"),
        "review": st.Page(views.review, title="Review", icon=":material/fact_check:", url_path="review"),
        "ingredients": st.Page(
            views.matching, title="Ingredients", icon=":material/nutrition:", url_path="ingredients"
        ),  # fmt: skip
        "menu": st.Page(views.menu, title="Menu costing", icon=":material/restaurant_menu:", url_path="menu"),
    }


def sections() -> dict[str, list[st.Page]]:
    p = pages()
    return {"": [p["dashboard"]], "Invoices": [p["upload"], p["review"]], "Kitchen": [p["ingredients"], p["menu"]]}


def go(name: str) -> None:
    """Open another page (used by the dashboard's to-do buttons)."""
    st.switch_page(pages()[name])
