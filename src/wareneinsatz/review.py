"""The review step: a person checks an invoice, corrects values, then approves or rejects it (step 8).

    data = invoice_to_data(invoice)                 # what is stored, as InvoiceData (the review form)
    issues = correct_invoice(session, invoice, fixed, by="Anant")   # save corrections, check again
    approve_invoice(session, invoice, by="Anant")   # now it counts in prices and costing
    reject_invoice(session, invoice, by="Anant", reason="Not our delivery")

Corrections replace the stored values, but the document keeps the extractor's raw output, so
the accuracy of the extraction on real invoices can be measured by comparing the two.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from wareneinsatz.models import Invoice, InvoiceLine, InvoiceTaxTotal
from wareneinsatz.repository import DuplicateInvoiceError, get_or_create_supplier, record_validation
from wareneinsatz.schemas import InvoiceData
from wareneinsatz.validation import Issue, check_known_supplier, validate_invoice


class ReviewError(ValueError):
    """The invoice can't be changed this way (e.g. approving without a date, changing a rejected one)."""


LINE_FIELDS = [
    "line_no", "description", "seller_article_no", "gtin", "quantity", "unit_code", "unit_raw", "unit_price",
    "price_base_quantity", "allowance_amount", "charge_amount", "line_amount", "vat_rate", "confidence",
]  # fmt: skip


def invoice_to_data(invoice: Invoice) -> InvoiceData:
    """The stored invoice in canonical form: what the review form shows and edits."""
    supplier = invoice.supplier
    return InvoiceData.model_validate(
        {
            "document_type": invoice.document_type,
            "invoice_number": invoice.invoice_number,
            "issue_date": invoice.issue_date,
            "delivery_date": invoice.delivery_date,
            "currency": invoice.currency,
            "prices_include_vat": invoice.prices_include_vat,
            "supplier": {
                "name": supplier.name,
                "vat_id": supplier.vat_id,
                "tax_number": supplier.tax_number,
                "street": supplier.street,
                "postcode": supplier.postcode,
                "city": supplier.city,
                "country_code": supplier.country_code,
            },
            "lines": [{name: getattr(line, name) for name in LINE_FIELDS} for line in invoice.lines],
            "vat_breakdown": [
                {"vat_rate": t.vat_rate, "taxable_amount": t.taxable_amount, "tax_amount": t.tax_amount}
                for t in invoice.tax_totals
            ],
            "allowance_total": invoice.allowance_total,
            "charge_total": invoice.charge_total,
            "net_total": invoice.net_total,
            "vat_total": invoice.vat_total,
            "gross_total": invoice.gross_total,
        }
    )


def correct_invoice(
    session: Session, invoice: Invoice, data: InvoiceData, *, by: str, today: date | None = None
) -> list[Issue]:
    """Replace the invoice's values with the reviewer's, check them again, keep it in review.

    Lines are replaced, so their ingredient matches are cleared and matched again (`match`).
    """
    if invoice.status == "rejected":
        raise ReviewError("A rejected invoice can't be corrected")
    try:
        with session.begin_nested():
            invoice.supplier = get_or_create_supplier(session, data.supplier)
            for name in ("invoice_number", "issue_date", "delivery_date", "currency", "prices_include_vat",
                         "allowance_total", "charge_total", "net_total", "vat_total", "gross_total"):  # fmt: skip
                setattr(invoice, name, getattr(data, name))
            invoice.document_type = data.document_type.value
            invoice.lines.clear()
            invoice.tax_totals.clear()
            session.flush()  # delete the old rows first: (invoice_id, line_no) is unique
            invoice.lines.extend(InvoiceLine(**line.model_dump()) for line in data.lines)
            invoice.tax_totals.extend(InvoiceTaxTotal(**entry.model_dump()) for entry in data.vat_breakdown)
            invoice.corrected_by, invoice.corrected_at = by, datetime.now(UTC)
            session.flush()
    except IntegrityError as error:
        if "uq_invoices_supplier_id_invoice_number" in str(error.orig):
            raise DuplicateInvoiceError(
                f"Invoice {data.invoice_number!r} from {data.supplier.name!r} is already stored"
            ) from error
        raise
    supplier = invoice.supplier
    issues = validate_invoice(data, today=today).issues + check_known_supplier(supplier.name, supplier.vat_id, data)
    record_validation(session, invoice, [issue.as_dict() for issue in issues], approve=False)
    return issues


def approve_invoice(session: Session, invoice: Invoice, *, by: str) -> None:
    if invoice.status == "rejected":
        raise ReviewError("A rejected invoice can't be approved")
    if invoice.issue_date is None:
        raise ReviewError("The invoice has no date; add it before approving")
    invoice.rejected_reason = None
    record_validation(session, invoice, invoice.review_issues or [], approve=True, approved_by=by)


def reject_invoice(session: Session, invoice: Invoice, *, by: str, reason: str) -> None:
    """Rejected invoices stay stored (with the reason) but never count in prices or purchases."""
    if not reason.strip():
        raise ReviewError("Say why the invoice is rejected")
    invoice.status = "rejected"
    invoice.approved_at = None
    invoice.approved_by = None
    invoice.rejected_reason = f"{reason.strip()} (by {by})"
    session.flush()
