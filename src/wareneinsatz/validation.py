"""Business-rule checks on a canonical invoice (step 3).

The schema (schemas.py) only checks structure. This module checks whether the content makes
sense: do the numbers add up, are VAT rates, units and dates plausible, is the supplier's tax ID
valid? Every finding is an Issue with a stable code and a specific, human-readable message.

    error    the invoice goes to review (no automatic approval)
    warning  shown on the review screen, but does not block approval

Most rules mirror the calculation rules of EN 16931 (BR-CO-*), so an e-invoice that passes
the official validator also passes here, and an OCR result with a misread number does not.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from stdnum import ean
from stdnum.eu import vat as eu_vat

from wareneinsatz import units
from wareneinsatz.names import same_company
from wareneinsatz.schemas import InvoiceData

CENT = Decimal("0.01")
GERMAN_VAT_RATES = {Decimal("0"), Decimal("7"), Decimal("19")}
# Germany lowered VAT to 16 % and 5 % from 1 July to 31 December 2020.
REDUCED_2020_RATES = {Decimal("5"), Decimal("16")}
REDUCED_2020 = (date(2020, 7, 1), date(2020, 12, 31))
# Up to this gross amount an invoice may omit the supplier's tax ID (Kleinbetragsrechnung, § 33 UStDV).
SMALL_INVOICE_LIMIT = Decimal("250")
LOW_CONFIDENCE = 0.8  # extractor confidence below this needs a human look (OCR + LLM, step 5)
OLD_INVOICE = timedelta(days=730)


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    code: str  # stable name for code and statistics, e.g. "line_amount_mismatch"
    severity: Severity
    message: str  # specific text for the review screen
    field: str | None = None  # where the problem is, e.g. "lines[4].line_amount"

    def as_dict(self) -> dict[str, str | None]:
        """JSON-ready form, stored in invoices.review_issues."""
        return {**asdict(self), "severity": self.severity.value}


@dataclass(frozen=True)
class ValidationResult:
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        """True when nothing blocks approval."""
        return not self.errors


def validate_invoice(data: InvoiceData, *, today: date | None = None) -> ValidationResult:
    """Run every rule and collect all findings (it doesn't stop at the first one)."""
    today = today or date.today()
    issues = [
        *_check_lines(data),
        *_check_totals(data),
        *_check_vat(data),
        *_check_dates(data, today),
        *_check_supplier(data),
    ]
    if data.currency != "EUR":
        issues.append(
            warning("currency_not_eur", f"Amounts are in {data.currency}, prices can't be compared", "currency")
        )
    return ValidationResult(issues)


def check_known_supplier(stored_name: str, stored_vat_id: str | None, data: InvoiceData) -> list[Issue]:
    """Compare with the supplier the invoice was filed under (found by VAT ID or by name).

    Same name but a different VAT ID, or the same VAT ID under a different name, can be a typo,
    an OCR error or a placeholder ID, so a person should look at it.
    """
    issues = []
    if stored_vat_id and data.supplier.vat_id and stored_vat_id != data.supplier.vat_id:
        issues.append(
            warning(
                "supplier_vat_id_differs",
                f"VAT ID {data.supplier.vat_id} differs from {stored_vat_id} stored for {stored_name}",
                "supplier.vat_id",
            )
        )
    if not same_company(stored_name, data.supplier.name):
        issues.append(
            warning(
                "supplier_name_differs",
                f"Supplier {data.supplier.name!r} was filed under {stored_name!r} (same VAT ID)",
                "supplier.name",
            )
        )
    return issues


# --- the rules ------------------------------------------------------------------------------------


def _check_lines(data: InvoiceData) -> list[Issue]:
    issues = []
    for line in data.lines:
        where = f"lines[{line.line_no}]"
        # BT-131 = quantity x (price / base quantity) + line charges - line allowances, rounded to cents
        expected = money(
            line.quantity * line.unit_price / line.price_base_quantity + line.charge_amount - line.allowance_amount
        )
        if abs(expected - line.line_amount) > CENT:
            issues.append(
                error(
                    "line_amount_mismatch",
                    f"Line {line.line_no}: {_line_formula(line)} = {fmt(expected)}, "
                    f"but the line says {fmt(line.line_amount)}",
                    f"{where}.line_amount",
                )
            )
        if line.unit_code is None:
            issues.append(
                error(
                    "unit_unknown",
                    f"Line {line.line_no}: unit {line.unit_raw!r} not recognised, so no price per kg/l/piece",
                    f"{where}.unit_code",
                )
            )
        elif not units.is_known(line.unit_code):
            issues.append(
                warning(
                    "unit_unlisted",
                    f"Line {line.line_no}: unit code {line.unit_code} is not in the unit list",
                    f"{where}.unit_code",
                )
            )
        if line.gtin and not ean.is_valid(line.gtin):
            issues.append(
                warning(
                    "gtin_invalid", f"Line {line.line_no}: EAN {line.gtin} has a wrong check digit", f"{where}.gtin"
                )
            )
        if line.confidence is not None and line.confidence < LOW_CONFIDENCE:
            issues.append(
                error(
                    "low_confidence",
                    f"Line {line.line_no}: extraction confidence {line.confidence:.0%} is too low to trust",
                    where,
                )
            )
    return issues


def _check_totals(data: InvoiceData) -> list[Issue]:
    issues = []
    lines_total = sum((line.line_amount for line in data.lines), Decimal("0"))
    # BR-CO-13: total without VAT = sum of lines + document charges - document allowances
    items_total = lines_total + data.charge_total - data.allowance_total

    if data.prices_include_vat:
        # Till receipt with gross prices: the items must add up to the amount paid.
        if items_total != data.gross_total:
            issues.append(
                error(
                    "gross_total_mismatch",
                    f"Items add up to {fmt(items_total)}, but the total says {fmt(data.gross_total)}",
                    "gross_total",
                )
            )
    elif data.net_total is None:
        issues.append(warning("net_total_missing", "No total without VAT on the document", "net_total"))
    elif items_total != data.net_total:
        issues.append(
            error(
                "net_total_mismatch",
                f"Lines add up to {fmt(items_total)} net, but the total without VAT says {fmt(data.net_total)}",
                "net_total",
            )
        )

    # BR-CO-15: total with VAT = total without VAT + VAT
    if data.net_total is not None and data.vat_total is not None:
        expected_gross = data.net_total + data.vat_total
        if expected_gross != data.gross_total:
            issues.append(
                error(
                    "gross_total_mismatch",
                    f"{fmt(data.net_total)} net + {fmt(data.vat_total)} VAT = {fmt(expected_gross)}, "
                    f"but the total says {fmt(data.gross_total)}",
                    "gross_total",
                )
            )
    return issues


def _check_vat(data: InvoiceData) -> list[Issue]:
    issues = []
    reference_date = data.delivery_date or data.issue_date

    for line in data.lines:
        if line.vat_rate is None:
            issues.append(
                error("vat_rate_missing", f"Line {line.line_no}: no VAT rate", f"lines[{line.line_no}].vat_rate")
            )
        elif data.supplier.country_code == "DE":
            problem = _german_rate_problem(line.vat_rate, reference_date)
            if problem:
                issues.append(
                    error("vat_rate_implausible", f"Line {line.line_no}: {problem}", f"lines[{line.line_no}].vat_rate")
                )

    if not data.vat_breakdown:
        if not data.prices_include_vat:
            issues.append(warning("vat_breakdown_missing", "No VAT breakdown on the document", "vat_breakdown"))
        return issues

    for entry in data.vat_breakdown:
        # BR-CO-17: VAT amount = taxable amount x rate, rounded to cents
        expected_tax = money(entry.taxable_amount * entry.vat_rate / 100)
        if abs(expected_tax - entry.tax_amount) > CENT:
            issues.append(
                error(
                    "vat_amount_mismatch",
                    f"{fmt(entry.vat_rate)} % of {fmt(entry.taxable_amount)} is {fmt(expected_tax)}, "
                    f"but the VAT breakdown says {fmt(entry.tax_amount)}",
                    "vat_breakdown",
                )
            )

    if data.prices_include_vat:
        return issues  # receipts print gross lines; the per-rate net check below doesn't apply

    # Per rate, the lines must add up to the taxable amount. Skipped when the document has
    # allowances or charges, because those carry their own VAT rate, which isn't stored.
    if data.allowance_total == 0 and data.charge_total == 0:
        rates = {line.vat_rate for line in data.lines if line.vat_rate is not None} | {
            entry.vat_rate for entry in data.vat_breakdown
        }
        breakdown = {entry.vat_rate: entry.taxable_amount for entry in data.vat_breakdown}
        for rate in sorted(rates):
            lines_at_rate = sum((line.line_amount for line in data.lines if line.vat_rate == rate), Decimal("0"))
            if lines_at_rate != breakdown.get(rate, Decimal("0")):
                issues.append(
                    error(
                        "vat_breakdown_mismatch",
                        f"Lines at {fmt(rate)} % add up to {fmt(lines_at_rate)}, "
                        f"but the VAT breakdown says {fmt(breakdown.get(rate, Decimal('0')))}",
                        "vat_breakdown",
                    )
                )

    taxable_total = sum((entry.taxable_amount for entry in data.vat_breakdown), Decimal("0"))
    if data.net_total is not None and taxable_total != data.net_total:
        issues.append(
            error(
                "vat_breakdown_mismatch",
                f"Taxable amounts add up to {fmt(taxable_total)}, but the total without VAT says {fmt(data.net_total)}",
                "vat_breakdown",
            )
        )
    tax_sum = sum((entry.tax_amount for entry in data.vat_breakdown), Decimal("0"))
    if data.vat_total is not None and tax_sum != data.vat_total:
        issues.append(
            error(
                "vat_total_mismatch",
                f"VAT per rate adds up to {fmt(tax_sum)}, but the VAT total says {fmt(data.vat_total)}",
                "vat_total",
            )
        )
    return issues


def _check_dates(data: InvoiceData, today: date) -> list[Issue]:
    if data.issue_date is None:
        return [error("issue_date_missing", "No invoice date: it is needed for the price history", "issue_date")]
    issues = []
    if data.issue_date > today:
        issues.append(error("issue_date_in_future", f"Invoice date {data.issue_date} is in the future", "issue_date"))
    elif today - data.issue_date > OLD_INVOICE:
        issues.append(
            warning("issue_date_old", f"Invoice date {data.issue_date} is more than two years ago", "issue_date")
        )
    if data.delivery_date and data.delivery_date > today + timedelta(days=30):
        issues.append(
            warning(
                "delivery_date_far_future", f"Delivery date {data.delivery_date} is far in the future", "delivery_date"
            )
        )
    return issues


def _check_supplier(data: InvoiceData) -> list[Issue]:
    issues = []
    supplier = data.supplier
    is_eu_vat_id = bool(supplier.vat_id) and supplier.vat_id[:2].lower() in eu_vat.MEMBER_STATES | {"el"}
    if is_eu_vat_id and not eu_vat.is_valid(supplier.vat_id):
        issues.append(
            warning(
                "vat_id_invalid",
                f"VAT ID {supplier.vat_id} fails the official format or check-digit test",
                "supplier.vat_id",
            )
        )
    if not supplier.vat_id and not supplier.tax_number and abs(data.gross_total) > SMALL_INVOICE_LIMIT:
        issues.append(
            warning(
                "supplier_tax_id_missing",
                "Invoices over 250 EUR must show the supplier's VAT ID or tax number (§ 14 UStG)",
                "supplier",
            )
        )
    return issues


# --- helpers ----------------------------------------------------------------------------------------


def _german_rate_problem(rate: Decimal, reference_date: date | None) -> str | None:
    if rate in GERMAN_VAT_RATES:
        return None
    if rate in REDUCED_2020_RATES:
        if reference_date and REDUCED_2020[0] <= reference_date <= REDUCED_2020[1]:
            return None
        return f"{fmt(rate)} % VAT was only valid from July to December 2020"
    return f"{fmt(rate)} % is not a German VAT rate (19, 7 or 0)"


def _line_formula(line) -> str:
    formula = f"{fmt(line.quantity)} × {fmt(line.unit_price)}"
    if line.price_base_quantity != 1:
        formula += f" / {fmt(line.price_base_quantity)}"
    if line.charge_amount:
        formula += f" + {fmt(line.charge_amount)}"
    if line.allowance_amount:
        formula += f" - {fmt(line.allowance_amount)}"
    return formula


def money(value: Decimal) -> Decimal:
    """Round to cents the commercial way (0.005 -> 0.01), as invoices do."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value: Decimal) -> str:
    """Readable numbers in messages: Decimal('20.0000') -> '20', Decimal('55.4000') -> '55.40'."""
    text = format(value.normalize(), "f")
    if "." in text and len(text.split(".")[1]) == 1:
        text += "0"  # 55.4 -> 55.40, so amounts look like amounts
    return text


def error(code: str, message: str, field: str | None = None) -> Issue:
    return Issue(code, Severity.ERROR, message, field)


def warning(code: str, message: str, field: str | None = None) -> Issue:
    return Issue(code, Severity.WARNING, message, field)
