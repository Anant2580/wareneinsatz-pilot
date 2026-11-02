"""Smoke tests for the web app (step 8): every page renders against the real API and test database.

Streamlit's AppTest runs a page without a browser. The page's API client is FastAPI's TestClient,
so requests go straight to the app; no server, no network.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

pytest.importorskip("streamlit")
from fastapi.testclient import TestClient  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from wareneinsatz.api import create_app  # noqa: E402
from wareneinsatz.config import Settings  # noqa: E402
from wareneinsatz.db import session_scope  # noqa: E402
from wareneinsatz.demo import load_demo  # noqa: E402
from wareneinsatz.ingest.store import DocumentStore  # noqa: E402
from wareneinsatz.matching.service import load_ingredients  # noqa: E402
from wareneinsatz.recipes import load_recipes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))  # the web app's package `webui`
from webui.client import Api  # noqa: E402


@pytest.fixture(scope="module")
def api(engine, tmp_path_factory, samples_dir=ROOT / "tests" / "samples"):
    """Demo data plus one invoice waiting for review, behind the API."""
    with session_scope(engine) as session:
        load_ingredients(session, ROOT / "data" / "ingredients.json")
        load_recipes(session, ROOT / "data" / "recipes.json")
        load_demo(session, weeks=26)
    app = create_app(engine=engine, store=DocumentStore(tmp_path_factory.mktemp("store")), settings=Settings(),
                     embedder_factory=lambda: None)  # fmt: skip
    client = TestClient(app)
    xml = (samples_dir / "einvoices" / "musterfrisch_xrechnung_cii.xml").read_bytes()
    wrong = xml.replace(b"<ram:LineTotalAmount>39.80</ram:LineTotalAmount>",
                        b"<ram:LineTotalAmount>39.90</ram:LineTotalAmount>")  # fmt: skip
    client.post("/documents", files=[("files", ("falsch.xml", wrong, "application/xml"))])
    minimum = (samples_dir / "einvoices" / "musterfrisch_facturx_minimum.xml").read_bytes()
    client.post("/documents", files=[("files", ("minimum.xml", minimum, "application/xml"))])  # fails: no lines
    client.post("/matching", params={"embeddings": False})
    yield Api(client=client)
    with engine.begin() as connection:
        connection.execute(text(
            "TRUNCATE alerts, recipe_items, recipes, invoice_lines, invoice_tax_totals, invoices, documents, "
            "supplier_products, ingredients, suppliers CASCADE"))  # fmt: skip


def run_page(api: Api, page: str) -> AppTest:
    script = f"import sys\nsys.path.insert(0, {str(ROOT / 'app')!r})\nfrom webui import views\nviews.{page}()\n"
    app = AppTest.from_string(script, default_timeout=30)
    app.session_state["api"] = api
    app.run()
    assert not app.exception, app.exception
    return app


def texts(app: AppTest) -> str:
    parts = [m.value for m in app.markdown] + [m.value for m in app.title] + [str(m.value) for m in app.metric]
    parts += [e.value for e in app.error] + [w.value for w in app.warning] + [i.value for i in app.info]
    return "\n".join(parts)


def test_dashboard(api):
    app = run_page(api, "dashboard")
    assert "€ of food bought in October" in texts(app)  # the month as one sentence
    labels = [button.label for button in app.button]
    assert "1 invoice to review" in labels and "Check prices" in labels  # to-do buttons, then the alerts
    assert app.get("vega_lite_chart")  # food cost per dish


def test_upload_page(api):
    app = run_page(api, "upload")
    assert "Upload invoices" in texts(app)
    assert app.expander[0].label == "Failed documents (1)"  # with the reason, so nothing fails silently
    assert "upload the PDF version" in app.dataframe[0].value["Reason"][0]


def test_review_page_shows_the_errors(api):
    app = run_page(api, "review")
    assert "Line 4: 2 × 19.90 = 39.80, but the line says 39.90" in texts(app)
    assert any(button.label == "Approve" for button in app.button)


def test_ingredients_page_lists_suggestions(api):
    app = run_page(api, "matching")
    assert "Ingredients" in texts(app)
    assert any(button.label == "Confirm 0 line(s)" for button in app.button)  # suggestions listed, none ticked


def test_menu_page(api):
    app = run_page(api, "menu")
    assert "Menu costing" in texts(app)
    assert [metric.label for metric in app.metric] == ["Cost", "Net price", "Food cost", "Margin"]


def test_whole_app_has_the_theme_switch(api):
    """The full app (navigation, sidebar, theme switch) starts light, with the sun button at the top."""
    app = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=30)
    app.session_state["api"] = api
    app.run()
    assert not app.exception, app.exception
    switch = app.button(key="theme_toggle")
    assert switch.help == "Switch to dark mode"  # light by default
    assert any("stActiveTheme-" in str(element.proto) for element in app.get("html"))  # the script that stores it


def test_public_demo_locks_changes_until_the_owner_signs_in(api, monkeypatch):
    from wareneinsatz.config import Settings
    from webui import client

    monkeypatch.setattr(client, "get_settings", lambda: Settings(ui_password="owner-pw", api_key=None))
    app = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=30)
    app.session_state["api"] = api
    app.run()
    assert "Public demo" in " ".join(c.value for c in app.sidebar.caption)

    review = run_page(api, "review")
    assert next(b for b in review.button if b.label == "Approve").disabled  # visitors can only look

    app.sidebar.text_input[1].input("wrong")  # [0] is "Your name"
    app.sidebar.button[-1].click().run()  # the form sends the password with the click
    assert "signed_in" not in app.session_state
    app.sidebar.text_input[1].input("owner-pw")
    app.sidebar.button[-1].click().run()
    assert app.session_state["signed_in"] is True
