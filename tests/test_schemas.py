"""Unit tests for the canonical invoice schema (no database needed)."""

import copy
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from wareneinsatz.schemas import DocumentType, InvoiceData


def test_example_invoice_is_valid(invoice):
    assert invoice.document_type is DocumentType.INVOICE
    assert invoice.issue_date == date(2026, 10, 20)
    assert len(invoice.lines) == 7
    # Amounts become exact Decimals, never floats
    assert invoice.gross_total == Decimal("302.66")
    assert isinstance(invoice.lines[0].unit_price, Decimal)


def test_example_invoice_totals_add_up(invoice):
    """Sanity check of the test data itself: line sums, VAT breakdown and totals agree."""
    assert sum(line.line_amount for line in invoice.lines) == invoice.net_total
    for entry in invoice.vat_breakdown:
        lines_at_rate = sum(line.line_amount for line in invoice.lines if line.vat_rate == entry.vat_rate)
        assert lines_at_rate == entry.taxable_amount
    assert invoice.net_total + invoice.vat_total == invoice.gross_total


def test_vat_id_is_normalised(invoice_dict):
    invoice_dict["supplier"]["vat_id"] = "de 123.456-789"
    assert InvoiceData.model_validate(invoice_dict).supplier.vat_id == "DE123456789"


def test_blank_strings_become_none(invoice_dict):
    invoice_dict["invoice_number"] = "   "
    invoice_dict["lines"][0]["seller_article_no"] = ""
    invoice = InvoiceData.model_validate(invoice_dict)
    assert invoice.invoice_number is None
    assert invoice.lines[0].seller_article_no is None


def test_strings_are_trimmed(invoice_dict):
    invoice_dict["lines"][0]["description"] = "  Rinderhackfleisch  "
    assert InvoiceData.model_validate(invoice_dict).lines[0].description == "Rinderhackfleisch"


def test_unknown_field_is_rejected(invoice_dict):
    """An LLM that invents a key ("discount_reason") must fail loudly, not be silently ignored."""
    invoice_dict["lines"][0]["discount_reason"] = "loyalty"
    with pytest.raises(ValidationError, match="discount_reason"):
        InvoiceData.model_validate(invoice_dict)


def test_invoice_needs_at_least_one_line(invoice_dict):
    invoice_dict["lines"] = []
    with pytest.raises(ValidationError):
        InvoiceData.model_validate(invoice_dict)


def test_supplier_name_is_required(invoice_dict):
    invoice_dict["supplier"]["name"] = "  "
    with pytest.raises(ValidationError):
        InvoiceData.model_validate(invoice_dict)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("line_amount", "98.905"),  # amounts have at most 2 decimals; the database would round silently
        ("unit_price", "9.8912345"),  # unit prices at most 6
        ("quantity", "10.00001"),  # quantities at most 4
    ],
)
def test_too_many_decimals_are_rejected(invoice_dict, field, value):
    invoice_dict["lines"][0][field] = value
    with pytest.raises(ValidationError):
        InvoiceData.model_validate(invoice_dict)


def test_trailing_zeros_are_fine(invoice_dict):
    invoice_dict["lines"][0]["unit_price"] = "9.89000000"
    assert InvoiceData.model_validate(invoice_dict).lines[0].unit_price == Decimal("9.89")


def test_negative_price_is_rejected_but_negative_quantity_is_allowed(invoice_dict):
    """EN 16931 rule BR-27: the item price is never negative. Returns are negative quantities."""
    returned = copy.deepcopy(invoice_dict)
    returned["lines"][6].update(quantity="-2", line_amount="-6.84")
    assert InvoiceData.model_validate(returned).lines[6].quantity == Decimal("-2")

    invoice_dict["lines"][0]["unit_price"] = "-9.89"
    with pytest.raises(ValidationError):
        InvoiceData.model_validate(invoice_dict)


def test_duplicate_line_numbers_are_rejected(invoice_dict):
    invoice_dict["lines"][1]["line_no"] = 1
    with pytest.raises(ValidationError, match="line_no must be unique"):
        InvoiceData.model_validate(invoice_dict)


def test_duplicate_vat_rates_are_rejected(invoice_dict):
    invoice_dict["vat_breakdown"][1]["vat_rate"] = "7.00"  # same rate as the first entry, written differently
    with pytest.raises(ValidationError, match="one entry per VAT rate"):
        InvoiceData.model_validate(invoice_dict)


@pytest.mark.parametrize(
    ("gtin", "valid"), [("4006381333931", True), ("12345678", True), ("12345", False), ("ABC", False)]
)
def test_gtin_format(invoice_dict, gtin, valid):
    invoice_dict["lines"][0]["gtin"] = gtin
    if valid:
        assert InvoiceData.model_validate(invoice_dict).lines[0].gtin == gtin
    else:
        with pytest.raises(ValidationError):
            InvoiceData.model_validate(invoice_dict)


@pytest.mark.parametrize("currency", ["eur", "€", "EURO"])
def test_currency_must_be_iso_code(invoice_dict, currency):
    invoice_dict["currency"] = currency
    with pytest.raises(ValidationError):
        InvoiceData.model_validate(invoice_dict)


def test_suspicious_but_well_formed_values_are_accepted(invoice_dict):
    """Business rules are the validation step's job (step 3), not the schema's.

    A 16 % VAT rate (Germany, second half of 2020) or totals that don't add up must
    be stored and flagged for review, not rejected on arrival.
    """
    invoice_dict["lines"][0]["vat_rate"] = "16"
    invoice_dict["gross_total"] = "999.99"
    invoice = InvoiceData.model_validate(invoice_dict)
    assert invoice.lines[0].vat_rate == Decimal("16")
    assert invoice.gross_total == Decimal("999.99")


def test_minimal_till_receipt_is_valid():
    """A receipt may lack invoice number, VAT ID, net and VAT totals: only gross total and lines are required."""
    receipt = InvoiceData.model_validate(
        {
            "issue_date": "2026-10-18",
            "prices_include_vat": True,
            "supplier": {"name": "Getränkemarkt Beispiel"},
            "lines": [
                {
                    "line_no": 1,
                    "description": "Zitronen 1 kg",
                    "quantity": "1",
                    "unit_price": "2.49",
                    "line_amount": "2.49",
                }
            ],
            "gross_total": "2.49",
        }
    )
    assert receipt.invoice_number is None
    assert receipt.net_total is None
    assert receipt.lines[0].vat_rate is None  # not printed: the validation step will ask for review


def test_json_round_trip_is_lossless(invoice):
    """Decimals are written as strings in JSON mode, so nothing is lost when the raw extraction is stored."""
    as_json = invoice.model_dump(mode="json")
    assert as_json["gross_total"] == "302.66"
    assert InvoiceData.model_validate(as_json) == invoice
