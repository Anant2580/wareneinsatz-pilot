"""Rules shared by the two XML syntaxes of EN 16931 (UBL and CII).

Both syntaxes carry the same business terms, so the code that turns them into
InvoiceData-ready values lives here once.
"""

import re
from collections import defaultdict
from decimal import Decimal

# UNTDID 1001 type codes that mean "credit note" (the amounts reduce what is owed).
CREDIT_NOTE_TYPE_CODES = {"81", "83", "261", "262", "296", "308", "381", "396", "420", "458", "532"}

# VAT categories that have no rate on the document because the rate is 0 by definition:
# Z zero rated, E exempt, AE reverse charge, K intra-EU supply, G export, O not subject to VAT.
ZERO_RATE_CATEGORIES = {"Z", "E", "AE", "K", "G", "O"}

_UNIT_CODE = re.compile(r"^[A-Z0-9]{2,3}$")
_GTIN = re.compile(r"^(\d{8}|\d{12,14})$")
GTIN_SCHEME = "0160"  # ISO 6523 scheme ID for GS1 GTIN/EAN


def document_type(type_code: str | None, *, is_credit_note_root: bool = False) -> str:
    """Map BT-3 to the schema's 'invoice' / 'credit_note'."""
    if is_credit_note_root or (type_code or "") in CREDIT_NOTE_TYPE_CODES:
        return "credit_note"
    return "invoice"


def vat_rate(percent: Decimal | None, category: str | None) -> Decimal | None:
    """Rate in percent; categories that are 0 % by definition often omit the number."""
    if percent is not None:
        return percent
    if (category or "").upper() in ZERO_RATE_CATEGORIES:
        return Decimal("0")
    return None


def unit_fields(code: str | None) -> dict[str, str | None]:
    """BT-130 is a UN/ECE Rec 20/21 code. Keep the raw value too, and drop codes that aren't valid."""
    return {"unit_code": code if code and _UNIT_CODE.match(code) else None, "unit_raw": code}


def gtin(value: str | None, scheme: str | None) -> str | None:
    """BT-157 counts as a GTIN only with scheme 0160 and a valid length."""
    if value and scheme == GTIN_SCHEME and _GTIN.match(value):
        return value
    return None


def merged_vat_breakdown(entries: list[tuple[Decimal, Decimal, Decimal]]) -> list[dict[str, Decimal]]:
    """Combine (rate, taxable amount, tax amount) entries that share a rate.

    EN 16931 has one breakdown per VAT *category*, so e.g. "exempt" and "zero rated"
    can both appear at 0 %. The schema keys the breakdown by rate, so they are summed.
    """
    sums: dict[Decimal, list[Decimal]] = defaultdict(lambda: [Decimal("0"), Decimal("0")])
    for rate, taxable, tax in entries:
        sums[rate][0] += taxable
        sums[rate][1] += tax
    return [
        {"vat_rate": rate, "taxable_amount": taxable, "tax_amount": tax}
        for rate, (taxable, tax) in sorted(sums.items())
    ]
