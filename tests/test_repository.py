"""Database tests for storing invoices (need TEST_DATABASE_URL, see conftest.py)."""

import copy
import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from wareneinsatz.models import Document, Invoice, InvoiceLine, Supplier
from wareneinsatz.names import normalise_name
from wareneinsatz.repository import (
    DocumentMeta,
    DuplicateDocumentError,
    DuplicateInvoiceError,
    save_invoice,
)
from wareneinsatz.schemas import InvoiceData


def meta_for(content: bytes, filename: str = "rechnung.xml") -> DocumentMeta:
    """Document metadata for a fake file with the given content."""
    return DocumentMeta(
        sha256=hashlib.sha256(content).hexdigest(),
        filename=filename,
        media_type="application/xml",
        size_bytes=len(content),
        extraction_method="test",
    )


def test_normalise_name():
    assert normalise_name("METRO Deutschland GmbH.") == "metro deutschland gmbh"
    assert normalise_name("Musterfrisch  Großhandel GmbH") == normalise_name("musterfrisch grosshandel gmbh")


def test_save_invoice_round_trip(session, invoice):
    saved = save_invoice(session, meta_for(b"file-1"), invoice)
    session.expire_all()  # force a real reload from the database

    stored = session.get(Invoice, saved.id)
    assert stored.invoice_number == "RE-2026-104233"
    assert stored.gross_total == Decimal("302.66")
    assert stored.status == "needs_review"  # nothing is approved without validation (step 3)
    assert stored.supplier.vat_id == "DE123456789"
    assert [line.line_no for line in stored.lines] == [1, 2, 3, 4, 5, 6, 7]
    assert stored.lines[0].unit_price == Decimal("9.89")
    assert {entry.vat_rate: entry.tax_amount for entry in stored.tax_totals} == {
        Decimal("7"): Decimal("16.95"),
        Decimal("19"): Decimal("6.96"),
    }
    assert stored.document.status == "extracted"
    # The extractor's output is kept unchanged, as JSON
    assert stored.document.raw_extraction == invoice.model_dump(mode="json")


def test_supplier_is_reused_by_vat_id(session, invoice_dict):
    first = InvoiceData.model_validate(invoice_dict)
    invoice_dict["invoice_number"] = "RE-2026-104999"
    invoice_dict["supplier"]["name"] = "MUSTERFRISCH Grosshandel"  # different spelling, same VAT ID
    second = InvoiceData.model_validate(invoice_dict)

    a = save_invoice(session, meta_for(b"file-1"), first)
    b = save_invoice(session, meta_for(b"file-2"), second)
    assert a.supplier_id == b.supplier_id
    assert session.scalar(select(func.count()).select_from(Supplier)) == 1


def test_supplier_is_found_by_name_and_learns_its_vat_id(session, invoice_dict):
    without_vat = copy.deepcopy(invoice_dict)
    without_vat["supplier"]["vat_id"] = None
    a = save_invoice(session, meta_for(b"receipt"), InvoiceData.model_validate(without_vat))
    assert a.supplier.vat_id is None

    invoice_dict["invoice_number"] = "RE-2026-105000"
    b = save_invoice(session, meta_for(b"invoice"), InvoiceData.model_validate(invoice_dict))
    assert b.supplier_id == a.supplier_id
    assert b.supplier.vat_id == "DE123456789"


def test_same_file_twice_is_rejected(session, invoice):
    save_invoice(session, meta_for(b"same-file"), invoice)
    with pytest.raises(DuplicateDocumentError):
        save_invoice(session, meta_for(b"same-file"), invoice)
    # The savepoint kept the session usable and nothing extra was stored
    assert session.scalar(select(func.count()).select_from(Document)) == 1


def test_same_invoice_number_from_same_supplier_is_rejected(session, invoice):
    """E.g. the PDF copy of an invoice that already arrived as XML: different file, same invoice."""
    save_invoice(session, meta_for(b"invoice.xml"), invoice)
    with pytest.raises(DuplicateInvoiceError, match="RE-2026-104233"):
        save_invoice(session, meta_for(b"invoice.pdf", "rechnung.pdf"), invoice)
    assert session.scalar(select(func.count()).select_from(Invoice)) == 1
    assert session.scalar(select(func.count()).select_from(InvoiceLine)) == 7


def test_same_invoice_number_from_another_supplier_is_fine(session, invoice_dict):
    save_invoice(session, meta_for(b"a"), InvoiceData.model_validate(invoice_dict))
    invoice_dict["supplier"] = {"name": "Beispiel Getränke KG", "vat_id": "DE999999999"}
    save_invoice(session, meta_for(b"b"), InvoiceData.model_validate(invoice_dict))
    assert session.scalar(select(func.count()).select_from(Invoice)) == 2


def test_receipts_without_invoice_number_do_not_collide(session, invoice_dict):
    """NULL invoice numbers are distinct in a PostgreSQL unique constraint."""
    invoice_dict["invoice_number"] = None
    save_invoice(session, meta_for(b"receipt-1"), InvoiceData.model_validate(invoice_dict))
    save_invoice(session, meta_for(b"receipt-2"), InvoiceData.model_validate(invoice_dict))
    assert session.scalar(select(func.count()).select_from(Invoice)) == 2


def test_database_refuses_approval_without_date(session, invoice_dict):
    """Invariant in the database itself: price history needs a date, so no approval without one."""
    invoice_dict["issue_date"] = None
    saved = save_invoice(session, meta_for(b"no-date"), InvoiceData.model_validate(invoice_dict))
    saved.status = "approved"
    saved.approved_at = datetime.now(UTC)
    with pytest.raises(IntegrityError, match="ck_invoices_approved_has_date"), session.begin_nested():
        session.flush()


def test_deleting_a_document_deletes_its_invoice(session, invoice):
    saved = save_invoice(session, meta_for(b"to-delete"), invoice)
    session.delete(saved.document)
    session.flush()
    assert session.scalar(select(func.count()).select_from(Invoice)) == 0
    assert session.scalar(select(func.count()).select_from(InvoiceLine)) == 0
    assert session.scalar(select(func.count()).select_from(Supplier)) == 1  # suppliers stay
