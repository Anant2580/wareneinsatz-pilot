"""Unit tests for the business-rule checks (step 3). No database needed."""

import copy
from datetime import date
from pathlib import Path

import pytest

from wareneinsatz.ingest.extract import parse_einvoice_xml
from wareneinsatz.names import same_company
from wareneinsatz.schemas import InvoiceData
from wareneinsatz.validation import Severity, check_known_supplier, validate_invoice

TODAY = date(2026, 10, 26)


def codes(data: InvoiceData, severity: Severity | None = None) -> list[str]:
    result = validate_invoice(data, today=TODAY)
    return [issue.code for issue in result.issues if severity is None or issue.severity is severity]


def changed(invoice_dict, **changes) -> InvoiceData:
    """A copy of the example invoice with some top-level fields changed."""
    data = copy.deepcopy(invoice_dict)
    data.update(changes)
    return InvoiceData.model_validate(data)


# --- a correct invoice ----------------------------------------------------------------------------


def test_correct_invoice_passes(invoice):
    result = validate_invoice(invoice, today=TODAY)
    assert result.ok
    # Only the placeholder VAT ID is reported, as a warning: DE123456789 fails the official check digit.
    assert [(issue.code, issue.severity) for issue in result.issues] == [("vat_id_invalid", Severity.WARNING)]


# --- lines --------------------------------------------------------------------------------------------


def test_line_that_does_not_add_up_is_an_error(invoice_dict):
    invoice_dict["lines"][3]["line_amount"] = "39.90"  # 2 x 19.90 = 39.80
    result = validate_invoice(InvoiceData.model_validate(invoice_dict), today=TODAY)
    line_errors = [issue for issue in result.errors if issue.code == "line_amount_mismatch"]
    assert line_errors[0].message == "Line 4: 2 × 19.90 = 39.80, but the line says 39.90"
    assert line_errors[0].field == "lines[4].line_amount"
    assert not result.ok


def test_line_discounts_and_surcharges_are_part_of_the_line_check(invoice_dict):
    # 10 kg x 9.89 = 98.90, minus a 9.89 discount = 89.01; totals adjusted to match
    invoice_dict["lines"][0].update(allowance_amount="9.89", line_amount="89.01")
    invoice_dict["vat_breakdown"][0].update(taxable_amount="232.22", tax_amount="16.26")
    invoice_dict.update(net_total="268.86", vat_total="23.22", gross_total="292.08")
    assert codes(InvoiceData.model_validate(invoice_dict), Severity.ERROR) == []


def test_price_base_quantity_is_respected(invoice_dict):
    """BT-149: a price per 100 pieces. 300 pieces x (11.49 / 100) = 34.47."""
    invoice_dict["lines"][1].update(quantity="300", price_base_quantity="100", unit_code="H87")
    assert "line_amount_mismatch" not in codes(InvoiceData.model_validate(invoice_dict))


def test_unrecognised_unit_is_an_error_and_unlisted_code_a_warning(invoice_dict):
    invoice_dict["lines"][0].update(unit_code=None, unit_raw="Schale")
    invoice_dict["lines"][1].update(unit_code="XYZ")
    result = validate_invoice(InvoiceData.model_validate(invoice_dict), today=TODAY)
    assert ("unit_unknown", Severity.ERROR) in [(i.code, i.severity) for i in result.issues]
    assert ("unit_unlisted", Severity.WARNING) in [(i.code, i.severity) for i in result.issues]


def test_ean_check_digit(invoice_dict):
    invoice_dict["lines"][0]["gtin"] = "4006381333931"  # valid
    invoice_dict["lines"][1]["gtin"] = "4006381333932"  # last digit wrong
    assert codes(InvoiceData.model_validate(invoice_dict)).count("gtin_invalid") == 1


def test_low_extraction_confidence_needs_review(invoice_dict):
    invoice_dict["lines"][2]["confidence"] = 0.55
    assert "low_confidence" in codes(InvoiceData.model_validate(invoice_dict), Severity.ERROR)


# --- totals -------------------------------------------------------------------------------------------


def test_net_total_must_match_the_lines(invoice_dict):
    assert "net_total_mismatch" in codes(changed(invoice_dict, net_total="278.85"), Severity.ERROR)


def test_gross_total_must_be_net_plus_vat(invoice_dict):
    assert "gross_total_mismatch" in codes(changed(invoice_dict, gross_total="302.76"), Severity.ERROR)


def test_till_receipt_items_must_add_up_to_the_amount_paid():
    receipt = {
        "issue_date": "2026-10-24",
        "prices_include_vat": True,
        "supplier": {"name": "Getränkemarkt Beispiel", "tax_number": "102/123/45678"},
        "lines": [
            {
                "line_no": 1,
                "description": "Zitronen 1 kg",
                "quantity": "2",
                "unit_code": "KGM",
                "unit_price": "2.49",
                "line_amount": "4.98",
                "vat_rate": "7",
            },
            {
                "line_no": 2,
                "description": "Mineralwasser 0,75 l",
                "quantity": "6",
                "unit_code": "XBO",
                "unit_price": "0.89",
                "line_amount": "5.34",
                "vat_rate": "19",
            },
        ],
        "gross_total": "10.32",
    }
    assert codes(InvoiceData.model_validate(receipt), Severity.ERROR) == []
    receipt["gross_total"] = "10.42"
    assert codes(InvoiceData.model_validate(receipt), Severity.ERROR) == ["gross_total_mismatch"]


# --- VAT ----------------------------------------------------------------------------------------------


def test_missing_vat_rate_is_an_error(invoice_dict):
    invoice_dict["lines"][0]["vat_rate"] = None
    assert "vat_rate_missing" in codes(InvoiceData.model_validate(invoice_dict), Severity.ERROR)


@pytest.mark.parametrize(
    ("rate", "issue_date", "plausible"),
    [
        ("19", "2026-10-20", True),
        ("7", "2026-10-20", True),
        ("16", "2026-10-20", False),  # only valid in the second half of 2020
        ("16", "2020-10-15", True),
        ("5", "2020-08-01", True),
        ("12", "2026-10-20", False),  # not a German rate at all
    ],
)
def test_german_vat_rates(invoice_dict, rate, issue_date, plausible):
    invoice_dict["lines"][0]["vat_rate"] = rate
    invoice_dict.update(issue_date=issue_date, delivery_date=issue_date)
    assert ("vat_rate_implausible" not in codes(InvoiceData.model_validate(invoice_dict))) is plausible


def test_foreign_suppliers_may_use_other_rates(invoice_dict):
    invoice_dict["supplier"].update(country_code="AT", vat_id=None)
    invoice_dict["lines"][5]["vat_rate"] = "20"  # Austrian standard rate
    assert "vat_rate_implausible" not in codes(InvoiceData.model_validate(invoice_dict))


def test_vat_amount_must_match_rate_times_taxable_amount(invoice_dict):
    invoice_dict["vat_breakdown"][0]["tax_amount"] = "16.85"  # 7 % of 242.11 = 16.95
    assert "vat_amount_mismatch" in codes(InvoiceData.model_validate(invoice_dict), Severity.ERROR)


def test_vat_breakdown_must_match_the_lines_per_rate(invoice_dict):
    invoice_dict["lines"][5]["vat_rate"] = "7"  # cola booked at 7 % instead of 19 %
    assert "vat_breakdown_mismatch" in codes(InvoiceData.model_validate(invoice_dict), Severity.ERROR)


def test_vat_total_must_match_the_breakdown(invoice_dict):
    invoice_dict.update(vat_total="23.81", gross_total="302.56")
    assert "vat_total_mismatch" in codes(InvoiceData.model_validate(invoice_dict), Severity.ERROR)


# --- dates, supplier, currency --------------------------------------------------------------------------


def test_missing_date_is_an_error(invoice_dict):
    assert codes(changed(invoice_dict, issue_date=None), Severity.ERROR) == ["issue_date_missing"]


def test_future_date_is_an_error_and_old_date_a_warning(invoice_dict):
    assert "issue_date_in_future" in codes(changed(invoice_dict, issue_date="2026-11-30"), Severity.ERROR)
    assert "issue_date_old" in codes(changed(invoice_dict, issue_date="2023-01-10"), Severity.WARNING)


def test_supplier_tax_id_is_required_above_250_eur(invoice_dict):
    invoice_dict["supplier"]["vat_id"] = None
    assert "supplier_tax_id_missing" in codes(InvoiceData.model_validate(invoice_dict), Severity.WARNING)
    invoice_dict["supplier"]["tax_number"] = "102/123/45678"  # a tax number is enough
    assert "supplier_tax_id_missing" not in codes(InvoiceData.model_validate(invoice_dict))


def test_foreign_currency_is_a_warning(invoice_dict):
    assert "currency_not_eur" in codes(changed(invoice_dict, currency="CHF"), Severity.WARNING)


def test_issues_are_json_ready(invoice):
    issue = validate_invoice(invoice, today=TODAY).issues[0]
    assert issue.as_dict() == {
        "code": "vat_id_invalid",
        "severity": "warning",
        "message": "VAT ID DE123456789 fails the official format or check-digit test",
        "field": "supplier.vat_id",
    }


# --- comparison with the stored supplier ------------------------------------------------------------------


def test_known_supplier_with_other_vat_id_or_other_name_is_flagged(invoice):
    assert check_known_supplier("Musterfrisch Großhandel GmbH", "DE123456789", invoice) == []
    assert [i.code for i in check_known_supplier("Musterfrisch Großhandel GmbH", "DE111111111", invoice)] == [
        "supplier_vat_id_differs"
    ]
    assert [i.code for i in check_known_supplier("Lieferant GmbH", "DE123456789", invoice)] == ["supplier_name_differs"]


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Musterfrisch Großhandel GmbH", "MUSTERFRISCH Grosshandel", True),  # legal form, ß, case
        ("METRO Deutschland GmbH", "Metro", True),  # short form
        ("Müller & Söhne KG", "Müller und Söhne", True),
        ("Lieferant GmbH", "Musterfrisch Großhandel GmbH", False),
        ("GmbH", "Lieferant GmbH", False),  # nothing left after removing the legal form
    ],
)
def test_same_company(a, b, same):
    assert same_company(a, b) is same


# --- no false alarms on official data ------------------------------------------------------------------

KOSIT_FILES = sorted((Path(__file__).parent / "samples" / "kosit").glob("*.xml"))


@pytest.mark.parametrize("path", KOSIT_FILES, ids=lambda p: p.name)
def test_official_samples_have_no_errors(path):
    """The KoSIT test invoices are valid XRechnungen, so a correct validator finds no errors in them.

    Warnings are expected: their dates are years old and their VAT IDs are placeholders.
    """
    data, _, _ = parse_einvoice_xml(path.read_bytes())
    result = validate_invoice(data, today=TODAY)
    assert result.errors == []
    assert {issue.code for issue in result.warnings} <= {"issue_date_old", "vat_id_invalid", "supplier_tax_id_missing"}
