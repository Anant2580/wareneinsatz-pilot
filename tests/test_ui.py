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
    labels = [metric.label for metric in app.metric]
    assert labels[0].startswith("Wareneinsatz 2026-") and "Invoices to review" in labels
    assert app.metric[1].value == "1"  # the faulty invoice
    assert any("Classic Burger" in str(frame.value) for frame in app.dataframe)


def test_upload_page(api):
    app = run_page(api, "upload")
    assert "Upload invoices" in texts(app)


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
    assert "food cost" in texts(app)
