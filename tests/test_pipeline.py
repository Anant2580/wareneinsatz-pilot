"""Database tests for the ingest pipeline: file in, outcome + database rows + stored file out."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from wareneinsatz.ingest.pipeline import Outcome, ingest_bytes
from wareneinsatz.ingest.store import DocumentStore
from wareneinsatz.models import Document, Invoice, InvoiceLine


@pytest.fixture
def store(tmp_path):
    return DocumentStore(tmp_path / "documents")


@pytest.fixture
def cii_xml(samples):
    return (samples / "einvoices" / "musterfrisch_xrechnung_cii.xml").read_bytes()


def test_correct_xrechnung_is_approved_automatically(session, store, cii_xml):
    result = ingest_bytes(session, store, cii_xml, "rechnung.xml")

    assert result.outcome is Outcome.APPROVED
    assert result.method == "xml-cii"
    assert "302.66 EUR" in result.message
    invoice = session.get(Invoice, result.invoice_id)
    assert invoice.invoice_number == "RE-2026-104233"
    assert len(invoice.lines) == 7
    assert invoice.document.extraction_method == "xml-cii"
    assert invoice.document.extraction_ms is not None
    # The original file is kept, under the path saved in the database
    assert store.open(invoice.document.storage_path) == cii_xml
    # Approved by the validation step; the placeholder VAT ID is kept as a warning for the record
    assert (invoice.status, invoice.approved_by) == ("approved", "auto")
    assert invoice.approved_at is not None
    assert [issue["code"] for issue in invoice.review_issues] == ["vat_id_invalid"]


def test_invoice_with_an_error_goes_to_review(session, store, cii_xml):
    wrong = cii_xml.replace(
        b"<ram:LineTotalAmount>39.80</ram:LineTotalAmount>", b"<ram:LineTotalAmount>39.90</ram:LineTotalAmount>"
    )
    result = ingest_bytes(session, store, wrong, "rechnung.xml")

    assert result.outcome is Outcome.NEEDS_REVIEW
    assert "line_amount_mismatch" in [issue.code for issue in result.issues]
    invoice = session.get(Invoice, result.invoice_id)
    assert (invoice.status, invoice.approved_at, invoice.approved_by) == ("needs_review", None, None)
    assert "Line 4: 2 × 19.90 = 39.80, but the line says 39.90" in [i["message"] for i in invoice.review_issues]


def test_same_vat_id_under_another_name_is_flagged(session, store, cii_xml):
    """Placeholder or mistyped VAT IDs would otherwise file an invoice under the wrong supplier."""
    ingest_bytes(session, store, cii_xml, "musterfrisch.xml")
    other = cii_xml.replace(b"Musterfrisch Gro\xc3\x9fhandel GmbH", b"Lieferant GmbH").replace(
        b"RE-2026-104233", b"L-1"
    )
    result = ingest_bytes(session, store, other, "lieferant.xml")
    assert "supplier_name_differs" in [issue.code for issue in result.issues]


def test_same_file_twice_is_a_duplicate(session, store, cii_xml):
    ingest_bytes(session, store, cii_xml, "rechnung.xml")
    result = ingest_bytes(session, store, cii_xml, "rechnung-kopie.xml")
    assert result.outcome is Outcome.DUPLICATE
    assert session.scalar(select(func.count()).select_from(Document)) == 1


def test_same_invoice_in_the_other_syntax_is_a_duplicate(session, store, cii_xml, samples):
    """Different file, same supplier and invoice number: it must not be booked twice."""
    ingest_bytes(session, store, cii_xml, "rechnung_cii.xml")
    ubl = (samples / "einvoices" / "musterfrisch_xrechnung_ubl.xml").read_bytes()
    result = ingest_bytes(session, store, ubl, "rechnung_ubl.xml")
    assert result.outcome is Outcome.DUPLICATE
    assert "RE-2026-104233" in result.message
    assert session.scalar(select(func.count()).select_from(Invoice)) == 1


def test_zugferd_pdf_is_stored(session, store, cii_xml, make_pdf):
    result = ingest_bytes(session, store, make_pdf({"factur-x.xml": cii_xml}), "rechnung.pdf")
    assert result.outcome is Outcome.APPROVED
    assert result.method == "pdf-zugferd"
    assert session.get(Invoice, result.invoice_id).document.media_type == "application/pdf"


def test_plain_pdf_waits_for_ocr(session, store, make_pdf):
    pdf = make_pdf()
    result = ingest_bytes(session, store, pdf, "scan.pdf")

    assert result.outcome is Outcome.NEEDS_OCR
    document = session.get(Document, result.document_id)
    assert (document.status, document.invoice) == ("received", None)
    assert store.open(document.storage_path) == pdf  # kept for step 5


def test_broken_file_is_recorded_as_failed(session, store):
    result = ingest_bytes(session, store, b"<rsm:CrossIndustryInvoice><unclosed>", "kaputt.xml")

    assert result.outcome is Outcome.FAILED
    document = session.get(Document, result.document_id)
    assert document.status == "failed"
    assert "Not well-formed" in document.error


def test_unit_price_with_nine_decimals_survives_the_database(session, store, samples):
    """Regression test for migration 0002 (NUMERIC(20,10) unit prices)."""
    content = (samples / "kosit" / "03.05a-INVOICE_uncefact.xml").read_bytes()
    result = ingest_bytes(session, store, content, "03.05a.xml")
    assert result.outcome is Outcome.APPROVED
    prices = session.scalars(select(InvoiceLine.unit_price).where(InvoiceLine.invoice_id == result.invoice_id))
    assert Decimal("0.061232374") in list(prices)


def test_negative_allowance_total_is_stored(session, store, cii_xml):
    """Regression test for migration 0002: correction invoices have negative document totals."""
    correction = cii_xml.replace(
        b"<ram:TaxBasisTotalAmount>",
        b"<ram:AllowanceTotalAmount>-0.23</ram:AllowanceTotalAmount>\n                <ram:TaxBasisTotalAmount>",
    )
    result = ingest_bytes(session, store, correction, "korrektur.xml")
    # Stored; it goes to review because this hand-edited total no longer adds up
    assert result.outcome is Outcome.NEEDS_REVIEW
    assert session.get(Invoice, result.invoice_id).allowance_total == Decimal("-0.23")
