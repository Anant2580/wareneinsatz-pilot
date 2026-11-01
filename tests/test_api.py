"""Tests for the REST API (step 8), against the test database. The language model is a fake."""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text

from wareneinsatz.api import create_app
from wareneinsatz.config import Settings
from wareneinsatz.demo import load_demo
from wareneinsatz.evaluation.photo import photo_of
from wareneinsatz.evaluation.render import render_pdf
from wareneinsatz.evaluation.synthetic import InvoiceGenerator
from wareneinsatz.ingest.store import DocumentStore
from wareneinsatz.matching.service import load_ingredients
from wareneinsatz.reading import DocumentReader, LlmUnavailable, Mode
from wareneinsatz.recipes import load_recipes

ROOT = Path(__file__).resolve().parents[1]
ALL_TABLES = (
    "alerts, recipe_items, recipes, invoice_lines, invoice_tax_totals, invoices, documents, supplier_products, "
    "ingredients, suppliers"
)


@pytest.fixture
def clean_database(engine):
    """The API commits for real, so the tables are emptied again afterwards."""
    yield engine
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {ALL_TABLES} CASCADE"))


@pytest.fixture
def receipt():
    return InvoiceGenerator(21).generate("receipt")


@pytest.fixture
def client(clean_database, tmp_path, fake_llm, printed_of, receipt):
    """The API with a fake model that always reads the `receipt` perfectly."""
    reply = printed_of(receipt.data, receipt.layout).model_dump_json()
    app = create_app(
        engine=clean_database,
        store=DocumentStore(tmp_path / "store"),
        settings=Settings(api_key=None),
        reader_factory=lambda: DocumentReader(fake_llm(reply), Mode.VISION),
        embedder_factory=lambda: None,
    )
    return TestClient(app)


def _upload(client, name: str, content: bytes) -> dict:
    response = client.post("/documents", files=[("files", (name, content, "application/octet-stream"))])
    assert response.status_code == 200, response.text
    return response.json()[0]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "database": "ok", "version": "0.1.0"}


def test_upload_outcomes(client, samples):
    xml = (samples / "einvoices" / "musterfrisch_xrechnung_cii.xml").read_bytes()
    first = _upload(client, "rechnung.xml", xml)
    assert first["outcome"] == "approved" and first["method"] == "xml-cii"
    assert first["issues"][0]["severity"] == "warning"
    assert _upload(client, "rechnung.xml", xml)["outcome"] == "duplicate"
    wrong = xml.replace(b"<ram:LineTotalAmount>39.80</ram:LineTotalAmount>",
                        b"<ram:LineTotalAmount>39.90</ram:LineTotalAmount>").replace(b"104233", b"104299")  # fmt: skip
    assert _upload(client, "falsch.xml", wrong)["outcome"] == "needs_review"
    assert _upload(client, "kaputt.xml", b"<oops>")["outcome"] == "failed"
    documents = client.get("/documents").json()
    assert {d["status"] for d in documents} == {"extracted", "failed"}


def test_uploads_have_a_size_limit(clean_database, tmp_path):
    small = TestClient(
        create_app(engine=clean_database, store=DocumentStore(tmp_path), settings=Settings(max_upload_mb=0))
    )
    response = small.post("/documents", files=[("files", ("big.pdf", b"%PDF-1.7 ...", "application/pdf"))])
    assert response.status_code == 413


def test_extraction_runs_in_the_background_and_ends_in_review(client, receipt):
    photo = photo_of(render_pdf(receipt.data, receipt.layout), seed=4)
    assert _upload(client, "bon.jpg", photo)["outcome"] == "needs_ocr"
    assert client.get("/extraction").json()["waiting"] == 1

    started = client.post("/extraction")  # TestClient runs the background task before returning
    assert started.status_code == 202
    status = client.get("/extraction").json()
    assert (status["running"], status["done"], status["outcomes"], status["waiting"]) == (
        False,
        1,
        {"needs_review": 1},
        0,
    )

    [waiting] = client.get("/invoices", params={"status": "needs_review"}).json()
    assert waiting["extraction_method"] == "llm-vision" and waiting["supplier"] == receipt.data.supplier.name
    detail = client.get(f"/invoices/{waiting['id']}").json()
    assert detail["raw_extraction"]["printed"]["gross_total"]  # the model's untouched reply
    assert len(detail["data"]["lines"]) == len(receipt.data.lines)

    preview = client.get(f"/documents/{waiting['document_id']}/preview", params={"width": 400})
    assert preview.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(preview.content)).width <= 400


def test_extraction_needs_the_model_server(clean_database, tmp_path):
    def unavailable():
        raise LlmUnavailable("Ollama is not reachable at http://localhost:11434")

    app = create_app(engine=clean_database, store=DocumentStore(tmp_path), reader_factory=unavailable)
    response = TestClient(app).post("/extraction")
    assert response.status_code == 503 and "Ollama" in response.json()["detail"]


def test_review_correct_approve_reject(client, samples):
    xml = (samples / "einvoices" / "musterfrisch_xrechnung_cii.xml").read_bytes()
    wrong = xml.replace(b"<ram:LineTotalAmount>39.80</ram:LineTotalAmount>",
                        b"<ram:LineTotalAmount>39.90</ram:LineTotalAmount>")  # fmt: skip
    invoice_id = _upload(client, "falsch.xml", wrong)["invoice_id"]
    detail = client.get(f"/invoices/{invoice_id}").json()
    assert detail["errors"] == 3 and detail["status"] == "needs_review"  # the line, net total and VAT base

    data = detail["data"]
    line = next(line for line in data["lines"] if line["line_amount"] == "39.90")
    line["line_amount"] = "39.80"  # the reviewer fixes the misprint
    corrected = client.put(f"/invoices/{invoice_id}", json={"data": data, "by": "Anant"}).json()
    assert corrected["errors"] == 0 and corrected["corrected_by"] == "Anant" and corrected["status"] == "needs_review"
    assert corrected["raw_extraction"]["lines"][3]["line_amount"] == "39.90"  # what was read is kept

    approved = client.post(f"/invoices/{invoice_id}/approve", json={"by": "Anant"}).json()
    assert (approved["status"], approved["approved_by"]) == ("approved", "Anant")

    rejected = client.post(f"/invoices/{invoice_id}/reject", json={"by": "Anant", "reason": "Not our delivery"})
    assert rejected.json()["rejected_reason"] == "Not our delivery (by Anant)"
    assert client.post(f"/invoices/{invoice_id}/approve", json={"by": "Anant"}).status_code == 409
    assert client.post(f"/invoices/{invoice_id}/reject", json={"by": "Anant", "reason": ""}).status_code == 422
    assert client.get("/invoices/999999").status_code == 404


def test_xml_has_no_preview_but_its_file(client, samples):
    document_id = _upload(client, "r.xml", (samples / "einvoices" / "musterfrisch_xrechnung_ubl.xml").read_bytes())[
        "document_id"
    ]
    assert client.get(f"/documents/{document_id}/preview").status_code == 415
    assert client.get(f"/documents/{document_id}/file").content.startswith(b"<?xml")
    assert client.get("/documents/999999/file").status_code == 404


def test_matching_and_kitchen(client, clean_database, samples):
    from wareneinsatz.db import session_scope

    assert client.post("/matching", params={"embeddings": False}).status_code == 409  # no ingredients yet
    with session_scope(clean_database) as session:
        load_ingredients(session, ROOT / "data" / "ingredients.json")
        load_recipes(session, ROOT / "data" / "recipes.json")
    _upload(client, "r.xml", (samples / "einvoices" / "musterfrisch_xrechnung_cii.xml").read_bytes())

    matched = client.post("/matching", params={"embeddings": False}).json()
    assert len(matched) == 7 and {m["match_status"] for m in matched} == {"suggested"}
    pommes = next(m for m in matched if m["ingredient_key"] == "pommes")
    confirmed = client.post(f"/lines/{pommes['id']}/confirm", json={"by": "Anant"}).json()
    assert float(confirmed["base_qty_per_unit"]) == 10000 and confirmed["confirmed_line_ids"] == [pommes["id"]]
    assert len(client.get("/lines", params={"status": "suggested"}).json()) == 6
    bad = client.post(f"/lines/{pommes['id']}/confirm", json={"ingredient_key": "kaviar"})
    assert bad.status_code == 422
    assert len(client.get("/ingredients").json()) == 54

    with session_scope(clean_database) as session:
        load_demo(session, weeks=26)
    menu = client.get("/menu").json()
    classic = next(d for d in menu if d["key"] == "classic-burger")
    assert classic["cost"] and classic["food_cost_pct"] and classic["missing"] == []
    detail = client.get("/menu/classic-burger", params={"pricing": "average"}).json()
    assert detail["items"][0]["label"] == "Brioche-Bun"
    assert client.get("/menu/pizza").status_code == 404
    assert client.get("/prices", params={"ingredient": "patty-rind-180"}).json()[0]["display_unit"] == "piece"

    check = client.post("/alerts/check").json()
    assert check["new"] > 0 and check["open"] == check["new"]
    first = client.get("/alerts").json()[0]
    assert client.post(f"/alerts/{first['id']}/ack", json={"by": "Anant"}).json()["acknowledged_by"] == "Anant"
    assert len(client.get("/alerts").json()) == check["open"] - 1

    months = client.get("/purchases/monthly").json()
    assert months[0]["month"] == "2026-05-01" and float(months[0]["food"]) > 0
    summary = client.get("/summary").json()
    assert summary["latest_month"] == "2026-10-01" and summary["suggestions_open"] == 6


def test_api_key(clean_database, tmp_path):
    secured = TestClient(create_app(engine=clean_database, store=DocumentStore(tmp_path),
                                    settings=Settings(api_key="s3cret")))  # fmt: skip
    assert secured.get("/health").status_code == 200  # monitoring needs no key
    assert secured.get("/summary").status_code == 401
    assert secured.get("/summary", headers={"X-API-Key": "wrong"}).status_code == 401
    assert secured.get("/summary", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_database_down(tmp_path):
    engine = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:9/none")
    down = TestClient(create_app(engine=engine, store=DocumentStore(tmp_path), settings=Settings()))
    assert down.get("/health").json()["database"] == "unreachable"
    assert down.get("/summary").status_code == 503
