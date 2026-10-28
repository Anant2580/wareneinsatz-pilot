"""Storing canonical invoices in the database.

Duplicates are detected by the database itself: the insert runs inside a
savepoint, and a unique-constraint violation is translated into a clear error.
Checking with a SELECT first would leave a gap between check and insert in
which a second upload could slip through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from wareneinsatz.models import Document, Invoice, InvoiceLine, InvoiceTaxTotal, Supplier
from wareneinsatz.names import normalise_name
from wareneinsatz.schemas import InvoiceData, SupplierData


class DuplicateDocumentError(Exception):
    """The exact same file was stored before (same SHA-256 hash)."""


class DuplicateInvoiceError(Exception):
    """This supplier's invoice number is already stored, e.g. the PDF copy of an XML invoice."""


@dataclass(frozen=True)
class DocumentMeta:
    """Facts about the uploaded file that are known before extraction."""

    sha256: str
    filename: str
    media_type: str
    size_bytes: int
    storage_path: str | None = None
    extraction_method: str | None = None
    extraction_ms: int | None = None


def get_or_create_supplier(session: Session, data: SupplierData) -> Supplier:
    """Find the supplier by VAT ID, then by normalised name, and create it if it is new."""
    name_key = normalise_name(data.name)

    supplier = None
    if data.vat_id:
        supplier = session.scalar(select(Supplier).where(Supplier.vat_id == data.vat_id))
    if supplier is None:
        supplier = session.scalar(select(Supplier).where(Supplier.name_key == name_key))
        if supplier is not None and supplier.vat_id is None and data.vat_id:
            # Known supplier, first document that shows its VAT ID: remember it.
            # (A known supplier with a *different* VAT ID is flagged by the validation step.)
            supplier.vat_id = data.vat_id
    if supplier is None:
        supplier = Supplier(
            name=data.name,
            name_key=name_key,
            vat_id=data.vat_id,
            tax_number=data.tax_number,
            street=data.street,
            postcode=data.postcode,
            city=data.city,
            country_code=data.country_code,
        )
        session.add(supplier)
        session.flush()  # sends the INSERT now, so supplier.id is known
    return supplier


def save_invoice(
    session: Session,
    meta: DocumentMeta,
    data: InvoiceData,
    *,
    raw_extraction: dict[str, Any] | None = None,
) -> Invoice:
    """Store one extracted invoice together with its document, supplier, lines and VAT breakdown.

    Everything happens in a savepoint: if the file or the invoice number is a duplicate,
    nothing is stored, DuplicateDocumentError or DuplicateInvoiceError is raised, and the
    caller's transaction is still usable. Committing is up to the caller (unit of work).
    """
    try:
        with session.begin_nested():
            supplier = get_or_create_supplier(session, data.supplier)
            document = Document(
                sha256=meta.sha256,
                filename=meta.filename,
                media_type=meta.media_type,
                size_bytes=meta.size_bytes,
                storage_path=meta.storage_path,
                status="extracted",
                extraction_method=meta.extraction_method,
                extraction_ms=meta.extraction_ms,
                # Keep the extractor's untouched output (JSON: Decimals as strings, dates as ISO text).
                raw_extraction=raw_extraction if raw_extraction is not None else data.model_dump(mode="json"),
            )
            invoice = _new_invoice(document, supplier, data)
            session.add(invoice)
            session.flush()  # run the INSERTs inside the savepoint, so violations surface here
    except IntegrityError as error:
        constraint = _violated_constraint(error)
        if constraint == "uq_documents_sha256":
            raise DuplicateDocumentError(f"This file is already stored (sha256 {meta.sha256[:12]}...)") from error
        if constraint == "uq_invoices_supplier_id_invoice_number":
            raise _duplicate_invoice(data) from error
        raise  # any other violation is a bug, not a duplicate: let it surface
    return invoice


def save_invoice_for_document(
    session: Session,
    document: Document,
    data: InvoiceData,
    *,
    extraction_method: str,
    extraction_ms: int,
    raw_extraction: dict[str, Any],
) -> Invoice:
    """Attach an invoice to a document that was stored earlier with status 'received' (step 5: OCR + LLM).

    In a savepoint like save_invoice: if the supplier's invoice number is already stored (e.g. the
    photo of a paper copy of an e-invoice), nothing changes and DuplicateInvoiceError is raised.
    """
    try:
        with session.begin_nested():
            supplier = get_or_create_supplier(session, data.supplier)
            invoice = _new_invoice(document, supplier, data)
            session.add(invoice)
            document.status = "extracted"
            document.extraction_method = extraction_method
            document.extraction_ms = extraction_ms
            document.raw_extraction = raw_extraction
            document.error = None
            session.flush()
    except IntegrityError as error:
        if _violated_constraint(error) == "uq_invoices_supplier_id_invoice_number":
            raise _duplicate_invoice(data) from error
        raise
    return invoice


def received_documents(session: Session, *, limit: int | None = None) -> list[Document]:
    """Documents waiting for OCR + LLM extraction, oldest first."""
    query = select(Document).where(Document.status == "received").order_by(Document.received_at, Document.id)
    return list(session.scalars(query.limit(limit) if limit else query))


def mark_document_failed(
    session: Session,
    document: Document,
    error: str,
    *,
    method: str | None = None,
    raw_extraction: dict[str, Any] | None = None,
) -> None:
    """The document could not become an invoice; `error` says why, `raw_extraction` keeps what was read."""
    document.status = "failed"
    document.error = error
    if method:
        document.extraction_method = method
    if raw_extraction is not None:
        document.raw_extraction = raw_extraction
    session.flush()


def _new_invoice(document: Document, supplier: Supplier, data: InvoiceData) -> Invoice:
    return Invoice(
        document=document,
        supplier=supplier,
        document_type=data.document_type.value,
        invoice_number=data.invoice_number,
        issue_date=data.issue_date,
        delivery_date=data.delivery_date,
        currency=data.currency,
        prices_include_vat=data.prices_include_vat,
        allowance_total=data.allowance_total,
        charge_total=data.charge_total,
        net_total=data.net_total,
        vat_total=data.vat_total,
        gross_total=data.gross_total,
        # Schema field names are the column names, so each line maps 1:1 onto a row.
        lines=[InvoiceLine(**line.model_dump()) for line in data.lines],
        tax_totals=[InvoiceTaxTotal(**entry.model_dump()) for entry in data.vat_breakdown],
    )


def _duplicate_invoice(data: InvoiceData) -> DuplicateInvoiceError:
    return DuplicateInvoiceError(f"Invoice {data.invoice_number!r} from {data.supplier.name!r} is already stored")


def save_document(session: Session, meta: DocumentMeta, *, status: str, error: str | None = None) -> Document:
    """Store a file that did not (yet) become an invoice.

    status 'received': waiting for OCR + LLM extraction (scans, photos, plain PDFs).
    status 'failed': broken or not an invoice; `error` says why.
    Raises DuplicateDocumentError if the same file was stored before.
    """
    try:
        with session.begin_nested():
            document = Document(
                sha256=meta.sha256,
                filename=meta.filename,
                media_type=meta.media_type,
                size_bytes=meta.size_bytes,
                storage_path=meta.storage_path,
                status=status,
                extraction_method=meta.extraction_method,
                extraction_ms=meta.extraction_ms,
                error=error,
            )
            session.add(document)
            session.flush()
    except IntegrityError as integrity_error:
        if _violated_constraint(integrity_error) == "uq_documents_sha256":
            raise DuplicateDocumentError(
                f"This file is already stored (sha256 {meta.sha256[:12]}...)"
            ) from integrity_error
        raise
    return document


def record_validation(
    session: Session,
    invoice: Invoice,
    issues: list[dict[str, Any]],
    *,
    approve: bool,
    approved_by: str = "auto",
    now: datetime | None = None,
) -> None:
    """Store the validation findings on the invoice and approve it or send it to review."""
    invoice.review_issues = issues
    if approve:
        invoice.status = "approved"
        invoice.approved_at = now or datetime.now(UTC)
        invoice.approved_by = approved_by
    else:
        invoice.status = "needs_review"
        invoice.approved_at = None
        invoice.approved_by = None
    session.flush()  # the database's CHECK (approved => date, time and approver) is enforced here


def _violated_constraint(error: IntegrityError) -> str | None:
    """Name of the violated constraint as reported by PostgreSQL (psycopg exposes it in .diag)."""
    diag = getattr(error.orig, "diag", None)
    return getattr(diag, "constraint_name", None)
