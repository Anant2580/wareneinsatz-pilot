"""From printed text to InvoiceData: numbers, dates, VAT rates and units are parsed here, in code.

The language model copies values as printed (printed.py). This module turns them into typed
values with the German conventions of the documents, and does every calculation:

- "1.234,56" -> 1234.56, "4,35-" -> -4.35, "18.10.26" -> 2026-10-18, "A" -> 7 % via the VAT table
- the amount on a product row minus its discount row = the line amount (BT-131)
- gross or net prices: decided by which printed total the lines add up to
- receipts without net totals: net and VAT totals are the sums of the printed VAT table

If a value can be read two ways ("1.554" is 1554 in German notation, but 1.554 if the model
wrote a decimal point), the reading that makes quantity x price = amount wins.

The result is a valid InvoiceData if possible. If not (no lines, no total), a partial object is
returned with the problems listed, so the evaluation can still score the fields that were read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import product
from typing import Any

from pydantic import ValidationError

from wareneinsatz.reading.printed import PrintedInvoice, PrintedLine
from wareneinsatz.schemas import InvoiceData, InvoiceLineData, SupplierData, VatBreakdownData
from wareneinsatz.units import code_for_printed_unit

CENT = Decimal("0.01")
# Two amounts are "the same" within this tolerance when deciding gross vs. net (rounding per line).
TOTAL_TOLERANCE = Decimal("0.05")
DERIVED_CONFIDENCE = 0.5  # a line amount that was not printed but calculated: flagged by validation

_NUMBER = re.compile(r"(?P<minus>[-−–]\s*)?(?P<digits>\d[\d.,']*)(?P<trailing_minus>-)?")


def number_candidates(text: str | None) -> list[Decimal]:
    """Every plausible reading of a printed number, the most likely first (German conventions).

    '1.234,56' -> [1234.56]      '4,887' -> [4.887]       '12.50' -> [12.50]
    '1.554'    -> [1554, 1.554]  German thousands separator first, then a decimal point
    '-4,35', '4,35-', '−4,35' -> [-4.35]                  '8,89 EUR/kg' -> [8.89]
    """
    if not text:
        return []
    match = _NUMBER.search(text)
    if match is None:
        return []
    digits = match["digits"].rstrip(".,").replace("'", "")
    negative = bool(match["minus"] or match["trailing_minus"])
    if "," in digits and "." in digits:
        # Both present: the one further right is the decimal separator.
        decimal_sep = "," if digits.rfind(",") > digits.rfind(".") else "."
        group_sep = "." if decimal_sep == "," else ","
        readings = [digits.replace(group_sep, "").replace(decimal_sep, ".")]
    elif "," in digits:
        readings = [digits.replace(",", "")] if digits.count(",") > 1 else [digits.replace(",", ".")]
    elif "." in digits:
        if digits.count(".") > 1:
            readings = [digits.replace(".", "")]  # 1.234.567
        else:
            whole, fraction = digits.split(".")
            readings = [whole + fraction, digits] if len(fraction) == 3 else [digits]
    else:
        readings = [digits]
    values = []
    for reading in readings:
        try:
            value = Decimal(reading)
        except InvalidOperation:
            continue
        values.append(-value if negative else value)
    return values


def parse_number(text: str | None) -> Decimal | None:
    candidates = number_candidates(text)
    return candidates[0] if candidates else None


def parse_amount(text: str | None) -> Decimal | None:
    """Money: at most 2 decimals. '43,450' (a trailing zero) becomes 43.45; '43,456' stays and fails later."""
    value = parse_number(text)
    if value is not None and value == value.quantize(CENT):
        return value.quantize(CENT)
    return value


def choose_line_numbers(
    quantity_text: str | None, price_text: str | None, amount: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    """Quantity and unit price, using the reading that makes quantity x price = amount where readings differ."""
    quantities: list[Decimal | None] = list(number_candidates(quantity_text)) or [None]
    prices: list[Decimal | None] = list(number_candidates(price_text)) or [None]
    if amount is None or len(quantities) * len(prices) == 1:
        return quantities[0], prices[0]

    def badness(choice: tuple[tuple[int, Decimal | None], tuple[int, Decimal | None]]) -> tuple[Decimal, int]:
        (q_index, quantity), (p_index, price) = choice
        if quantity is None or price is None:
            return Decimal("Infinity"), q_index + p_index
        return abs(abs(quantity * price) - abs(amount)), q_index + p_index  # ties: the more likely readings

    (_, quantity), (_, price) = min(product(enumerate(quantities), enumerate(prices)), key=badness)
    return quantity, price


_MONTHS = {
    "jan": 1, "januar": 1, "feb": 2, "februar": 2, "mär": 3, "märz": 3, "mrz": 3, "maerz": 3, "apr": 4, "april": 4,
    "mai": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10, "nov": 11, "november": 11, "dez": 12, "dezember": 12,
}  # fmt: skip


def parse_date(text: str | None) -> date | None:
    """'18.10.2026', '18.10.26', '2026-10-18', '18/10/2026', '18. Oktober 2026' -> date(2026, 10, 18)."""
    if not text:
        return None
    try:
        if m := re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", text):
            year = int(m[3])
            return date(year + 2000 if year < 100 else year, int(m[2]), int(m[1]))
        if m := re.search(r"(\d{1,2})\.?\s*([A-Za-zÄäÖöÜü]+)\.?\s*(\d{4})", text):
            month = _MONTHS.get(m[2].lower())
            return date(int(m[3]), month, int(m[1])) if month else None
    except ValueError:  # 31.02.2026 and similar
        return None
    return None


def parse_rate(text: str | None, codes: dict[str, Decimal]) -> Decimal | None:
    """'7 %' -> 7, '19,00%' -> 19, 'A' -> the rate of code A in the VAT table."""
    if not text:
        return None
    key = text.strip().upper().rstrip(".")
    if key in codes:
        return codes[key]
    value = parse_number(text)
    if value is None or not 0 <= value < 100:
        return None
    return value


def split_quantity_unit(quantity_text: str | None, unit_text: str | None) -> str | None:
    """The unit as printed. Models sometimes leave it inside the quantity ('4,887 kg')."""
    if unit_text and unit_text.strip():
        return unit_text.strip()
    if quantity_text and (m := re.search(r"\d\s*([A-Za-zÄÖÜäöü][A-Za-zÄÖÜäöü.]*)", quantity_text)):
        return m[1]
    return None


def split_address(text: str | None) -> tuple[str | None, str | None, str | None]:
    """'Am Bahnhof 2, 39104 Magdeburg' -> ('Am Bahnhof 2', '39104', 'Magdeburg')."""
    if not text:
        return None, None, None
    m = re.search(r"^(?P<street>.*?)[\s,·|-]*\b(?P<postcode>\d{5})\s+(?P<city>[^,·|]+?)\s*$", text.strip())
    if m is None:
        return text.strip() or None, None, None
    return m["street"].strip(" ,·|-") or None, m["postcode"], m["city"].strip()


def clean_vat_id(text: str | None) -> str | None:
    """'USt-IdNr.: DE 713 803 048' -> 'DE 713 803 048' (the schema removes the spaces).

    A VAT ID is a country code and then digits (Austria: 'ATU' + digits; some countries add letters at the end).
    """
    if not text:
        return None
    m = re.search(r"\b([A-Z]{2}[\s.]?U?\d[0-9A-Z\s.]{5,14}[0-9A-Z])\b", text.upper())
    return m[1].strip() if m else None


TAX_NUMBER = re.compile(r"\d{2,3}/\d{3,4}/\d{4,5}")  # Steuernummer as printed, e.g. 105/279/36294


def clean_tax_number(text: str | None) -> str | None:
    """'St.-Nr. 105/279/36294' -> '105/279/36294'."""
    if not text:
        return None
    m = TAX_NUMBER.search(text)
    return m[0] if m else re.sub(r"^\D*", "", text).strip() or None


def clean_invoice_number(text: str | None) -> str | None:
    """Drops a label the model copied along: 'Rechnungs-Nr.: RE-2026-1' -> 'RE-2026-1'."""
    if not text:
        return None
    cleaned = re.sub(
        r"^\s*(rechnungs?-?\s*(nr\.?|nummer)|rechnung|invoice\s*(no\.?|number)?|nr\.?)\s*[:.]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or text.strip() or None  # nothing left: the "label" was the number itself


def is_credit_note(title: str | None) -> bool:
    return bool(title and re.search(r"gutschrift|credit\s*note", title, flags=re.IGNORECASE))


@dataclass
class Interpretation:
    data: InvoiceData  # valid, or built without validation if `valid` is False
    valid: bool
    problems: list[str] = field(default_factory=list)


def interpret(printed: PrintedInvoice) -> Interpretation:
    """Turn the model's printed values into InvoiceData (valid if at all possible)."""
    breakdown, codes = _vat_breakdown(printed)
    only_rate = breakdown[0]["vat_rate"] if len(breakdown) == 1 else None
    lines = [line for index, p in enumerate(printed.lines) if (line := _line(index, p, codes, only_rate))]
    for number, line in enumerate(lines, start=1):
        line["line_no"] = number  # numbered after dropping empty entries

    allowance_total = abs(parse_amount(printed.discount_total) or Decimal("0.00"))
    charge_total = abs(parse_amount(printed.surcharge_total) or Decimal("0.00"))
    net_total = parse_amount(printed.net_total)
    vat_total = parse_amount(printed.vat_total)
    gross_total = parse_amount(printed.gross_total)
    prices_include_vat = _prices_include_vat(lines, net_total, gross_total, allowance_total, charge_total)
    # Till receipts print no net or VAT total, only the VAT table: the totals are its sums.
    if net_total is None and breakdown:
        net_total = sum((entry["taxable_amount"] for entry in breakdown), Decimal("0.00"))
    if vat_total is None and breakdown:
        vat_total = sum((entry["tax_amount"] for entry in breakdown), Decimal("0.00"))

    street, postcode, city = split_address(printed.supplier.address)
    tax_number = clean_tax_number(printed.supplier.tax_number)
    if tax_number is None and printed.supplier.vat_id and TAX_NUMBER.search(printed.supplier.vat_id):
        tax_number = clean_tax_number(printed.supplier.vat_id)  # "St.-Nr. ..." put into the VAT ID field
    supplier = {
        "name": (printed.supplier.name or "").strip() or None,
        "vat_id": clean_vat_id(printed.supplier.vat_id),
        "tax_number": tax_number,
        "street": street,
        "postcode": postcode,
        "city": city,
    }
    currency = (printed.currency or "").strip().upper().replace("€", "EUR")
    data: dict[str, Any] = {
        "document_type": "credit_note" if is_credit_note(printed.document_title) else "invoice",
        "invoice_number": clean_invoice_number(printed.invoice_number),
        "issue_date": parse_date(printed.issue_date),
        "delivery_date": parse_date(printed.delivery_date),
        "currency": currency if re.fullmatch(r"[A-Z]{3}", currency) else "EUR",
        "prices_include_vat": prices_include_vat,
        "supplier": supplier,
        "lines": lines,
        "vat_breakdown": breakdown,
        "allowance_total": allowance_total,
        "charge_total": charge_total,
        "net_total": net_total,
        "vat_total": vat_total,
        "gross_total": gross_total,
    }
    try:
        return Interpretation(InvoiceData.model_validate(data), valid=True)
    except ValidationError as error:
        problems = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()]
        return Interpretation(_construct(data), valid=False, problems=problems)


def _line(index: int, printed: PrintedLine, codes: dict[str, Decimal], only_rate: Decimal | None) -> dict | None:
    description = (printed.description or "").strip() or None
    amount = parse_amount(printed.amount)
    if description is None and amount is None:
        return None  # an empty entry: nothing to keep
    quantity, price = choose_line_numbers(printed.quantity, printed.unit_price, amount)
    confidence = None
    if quantity is None and amount is not None:
        quantity = Decimal(1)  # receipts print single items without "1 x"
    if price is None and amount is not None and quantity:
        price = abs(amount / quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if amount is None and quantity is not None and price is not None:
        amount = (quantity * price).quantize(CENT, rounding=ROUND_HALF_UP)
        confidence = DERIVED_CONFIDENCE  # not printed: validation asks a person to check it
    if price is not None and price < 0:
        price = -price  # unit prices are never negative (BR-27); the quantity carries the sign
        if quantity is not None and quantity > 0:
            quantity = -quantity
    if amount is not None and amount < 0 and quantity is not None and quantity > 0:
        quantity = -quantity  # returned goods / deposit refunds
    discount = abs(parse_amount(printed.discount) or Decimal("0.00"))
    surcharge = abs(parse_amount(printed.surcharge) or Decimal("0.00"))
    unit = split_quantity_unit(printed.quantity, printed.unit)
    vat_rate = parse_rate(printed.vat, codes)
    if vat_rate is None:
        vat_rate = only_rate  # a document with a single VAT rate: every line has that rate
    return {
        "line_no": index + 1,
        "description": description,
        "seller_article_no": (printed.article_no or "").strip() or None,
        "quantity": quantity,
        "unit_code": code_for_printed_unit(unit),
        "unit_raw": unit,
        "unit_price": price,
        "allowance_amount": discount,
        "charge_amount": surcharge,
        "line_amount": amount - discount + surcharge if amount is not None else None,
        "vat_rate": vat_rate,
        "confidence": confidence,
    }


def _vat_breakdown(printed: PrintedInvoice) -> tuple[list[dict[str, Decimal]], dict[str, Decimal]]:
    """One entry per rate with both amounts printed; the first wins if a rate appears twice.

    Two repairs for column mix-ups in VAT tables, both checkable from the printed amounts:
    VAT can't exceed the net amount (below 100 %), so swapped columns are swapped back; and a
    missing rate is taken from tax / net if that is one of the German rates.
    Returns the breakdown and the VAT codes it defines ("A" -> 7).
    """
    entries: dict[Decimal, dict[str, Decimal]] = {}
    codes: dict[str, Decimal] = {}
    for row in printed.vat_rates:
        rate, net, tax = parse_rate(row.rate, {}), parse_amount(row.net), parse_amount(row.tax)
        if net is not None and tax is not None:
            if abs(tax) > abs(net):
                net, tax = tax, net
            if rate is None:
                rate = implied_rate(net, tax)
            if rate is not None and rate not in entries:
                entries[rate] = {"vat_rate": rate, "taxable_amount": net, "tax_amount": tax}
        if rate is not None and row.code and row.code.strip():
            codes.setdefault(row.code.strip().upper(), rate)
    return list(entries.values()), codes


GERMAN_RATES = [Decimal(r) for r in ("0", "5", "7", "16", "19")]  # current 0/7/19; 5/16 only in late 2020


def implied_rate(net: Decimal, tax: Decimal) -> Decimal | None:
    """The VAT rate that tax / net shows, if it is a German rate (within rounding): 155.21 / 10.87 -> 7."""
    if not net:
        return None
    percent = abs(tax / net) * 100
    for rate in GERMAN_RATES:
        if abs(percent - rate) <= Decimal("0.2"):
            return rate
    return None


def _prices_include_vat(
    lines: list[dict], net: Decimal | None, gross: Decimal | None, allowance: Decimal, charge: Decimal
) -> bool:
    """Gross or net prices? The lines add up to one of the printed totals; receipts print only gross."""
    amounts = [line["line_amount"] for line in lines if line["line_amount"] is not None]
    if amounts:
        expected = sum(amounts, Decimal("0.00")) - allowance + charge
        if net is not None and abs(expected - net) <= TOTAL_TOLERANCE:
            return False
        if gross is not None and abs(expected - gross) <= TOTAL_TOLERANCE:
            return True
    return net is None  # no net total printed: a till receipt with gross prices


def _construct(data: dict[str, Any]) -> InvoiceData:
    """A partial InvoiceData without validation, for scoring what was read (like the rule baseline)."""
    return InvoiceData.model_construct(
        **{key: value for key, value in data.items() if key not in ("supplier", "lines", "vat_breakdown")},
        supplier=SupplierData.model_construct(**data["supplier"]),
        lines=[InvoiceLineData.model_construct(**line) for line in data["lines"]],
        vat_breakdown=[VatBreakdownData.model_construct(**entry) for entry in data["vat_breakdown"]],
    )
