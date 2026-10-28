"""A rule-based baseline extractor: PDF text layer + regular expressions, no OCR, no AI.

It shows what hand-written rules achieve, so the OCR + LLM extractor of step 5 has an honest
number to beat. The rules use common German invoice wording (Rechnungs-Nr., Nettobetrag,
SUMME EUR ...) and German number formats. Photos have no text layer, so it finds nothing there.

Partial results are kept: a header without lines still scores the fields it found. That is why
the result is built with model_construct (no validation) instead of model_validate.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from wareneinsatz.ingest.detect import FileKind, detect_file_type
from wareneinsatz.schemas import InvoiceData, InvoiceLineData, SupplierData
from wareneinsatz.units import code_for_printed_unit

AMOUNT = r"-?\d{1,3}(?:\.\d{3})*,\d{2,}"  # 1.234,56 (and unit prices with more decimals)
NUMBER = r"\d+(?:,\d+)?"
DATE = r"\d{2}\.\d{2}\.\d{4}"


def de_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def de_date(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return None


def pdf_text(content: bytes) -> str | None:
    try:
        reader = PdfReader(io.BytesIO(content))
        # layout mode keeps the visual rows of a table together (plain mode returns one cell per line)
        return "\n".join(page.extract_text(extraction_mode="layout") or "" for page in reader.pages)
    except PdfReadError:
        return None


def extract_with_rules(content: bytes) -> InvoiceData | None:
    if detect_file_type(content).kind is not FileKind.PDF:
        return None  # photos have no text layer
    text = pdf_text(content)
    if not text or not text.strip():
        return None
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    is_receipt = bool(re.search(r"\bSUMME EUR\b|\bBon\b", text))

    def first(pattern: str) -> str | None:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        return match.group(1) if match else None

    gross = first(rf"(?:Rechnungsbetrag|Gesamtbetrag|Gesamt|SUMME|Endbetrag|Zu zahlen)\s*(?:EUR|€)?\s*({AMOUNT})")
    net = first(rf"(?:Nettobetrag|Summe netto|Netto gesamt)\s*(?:EUR|€)?\s*({AMOUNT})")
    vat = first(rf"(?:^|\n)\s*MwSt\s+({AMOUNT})")
    vat_rates = [
        (de_decimal(r), de_decimal(a)) for r, a in re.findall(rf"(\d{{1,2}})\s*% MwSt auf {AMOUNT}\s+({AMOUNT})", text)
    ]
    if vat is None and vat_rates:
        vat = None
        vat_total = sum((a for _r, a in vat_rates if a is not None), Decimal("0"))
    else:
        vat_total = de_decimal(vat) if vat else None

    issue = first(rf"(?:Rechnungsdatum|Datum|Belegdatum)\s*:?\s*({DATE})") or first(rf"({DATE})")
    supplier = SupplierData.model_construct(
        name=rows[0] if rows else None,
        vat_id=(first(r"\b(DE\s?\d{9})\b") or "").replace(" ", "") or None,
        tax_number=first(r"St(?:euer)?(?:nummer|\.?-?\s*Nr\.?)\s*:?\s*(\d{2,3}/\d{3}/\d{4,5})"),
    )
    return InvoiceData.model_construct(
        invoice_number=first(r"Rechnungs?-?\s*(?:Nr\.?|nummer)\s*:?\s*([A-Z0-9][A-Z0-9/\-]{2,})")
        or first(r"\bRechnung\s+([A-Z]{0,3}-?\d[\w/\-]{2,})"),
        issue_date=de_date(issue) if issue else None,
        delivery_date=de_date(d) if (d := first(rf"Liefer(?:datum|ung)\s*:?\s*({DATE})")) else None,
        currency="EUR",
        prices_include_vat=is_receipt,
        supplier=supplier,
        lines=_lines(rows),
        vat_breakdown=[],
        net_total=de_decimal(net) if net else None,
        vat_total=vat_total,
        gross_total=de_decimal(gross) if gross else None,
    )


# A table row: [pos] [article no] description quantity unit price [rate %] amount [VAT code]
TABLE_ROW = re.compile(
    rf"^(?:\d{{1,3}}\s+)?(?:(?P<article>[A-Z]{{0,2}}\d{{4,8}})\s+)?(?P<desc>.+?)\s+(?P<qty>{NUMBER})\s+"
    rf"(?P<unit>[A-Za-zÄÖÜäöü.]{{1,7}})\s+(?P<price>{AMOUNT})\s+(?:(?P<rate>\d{{1,2}})\s*%\s+)?(?P<amount>{AMOUNT})(?:\s+[A-Z])?$"
)
# A receipt item: description on one row, then "  3 Ki x 9,29   27,87 A" or "1,554 kg x 3,19 EUR/kg  4,96 A"
RECEIPT_ROW = re.compile(
    rf"^(?P<qty>{NUMBER})\s*(?:(?P<unit>[A-Za-zÄÖÜäöü.]{{1,6}})\s+)?x\s*(?P<price>{AMOUNT})(?:\s*EUR/\w+)?\s+(?P<amount>{AMOUNT})(?:\s+[A-Z])?$"
)


def _lines(rows: list[str]) -> list[InvoiceLineData]:
    lines: list[InvoiceLineData] = []
    for index, row in enumerate(rows):
        if match := TABLE_ROW.match(row):
            unit = match["unit"]
            description, article = match["desc"], match["article"]
        elif (match := RECEIPT_ROW.match(row)) and index > 0:
            unit = match["unit"] or "St"
            description, article = rows[index - 1], None
        else:
            continue
        lines.append(
            InvoiceLineData.model_construct(
                line_no=len(lines) + 1,
                description=description,
                seller_article_no=article,
                quantity=de_decimal(match["qty"]),
                unit_code=code_for_printed_unit(unit),
                unit_raw=unit,
                unit_price=de_decimal(match["price"]),
                allowance_amount=Decimal("0"),
                charge_amount=Decimal("0"),
                line_amount=de_decimal(match["amount"]),
                vat_rate=Decimal(match["rate"]) if match.groupdict().get("rate") else None,
            )
        )
    return lines
