"""Turning an invoice (InvoiceData + Layout) into a PDF that looks like a real German supplier document.

Three layouts cover what a restaurant actually receives:

    wholesaler   A4 invoice with a product table and net prices (Großhandel, Fleischerei)
    cash_carry   dense A4 cash & carry invoice with VAT codes A/B per line
    receipt      80 mm till receipt with gross prices (Getränkemarkt, supermarket)

Numbers use German formatting (1.234,56; 20.10.2026), as on real documents. Only the standard PDF
fonts are used and the PDF is written in "invariant" mode (no timestamps), so the same input
always gives byte-identical output.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen.canvas import Canvas

from wareneinsatz.evaluation.synthetic import BUYER, TEST_IBAN, Layout
from wareneinsatz.schemas import InvoiceData, InvoiceLineData

# How units are printed when an invoice comes from XML (KoSIT samples) rather than the catalogue.
PRINTED_UNIT_NAMES = {
    "KGM": "kg",
    "GRM": "g",
    "LTR": "l",
    "H87": "St",
    "C62": "St",
    "XPP": "St",
    "EA": "St",
    "DAY": "Tag",
    "HUR": "Std",
    "MON": "Monat",
    "ANN": "Jahr",
    "LS": "pausch.",
    "KWH": "kWh",
}


# --- German number and date formats -------------------------------------------------------------------


def de_money(value: Decimal) -> str:
    """Decimal('1234.5') -> '1.234,50'."""
    text = f"{value:,.2f}"  # 1,234.50
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def de_number(value: Decimal) -> str:
    """Quantities and prices without forced decimals: 4.255 -> '4,255', 3 -> '3', 0.0612 -> '0,0612'."""
    text = format(value.normalize(), "f")
    integer, _, fraction = text.partition(".")
    integer = f"{int(integer):,}".replace(",", ".") if integer.lstrip("-").isdigit() else integer
    return f"{integer},{fraction}" if fraction else integer


def de_price(value: Decimal) -> str:
    """Unit prices: at least 2 decimals, more if the price has them (0,0612)."""
    return de_money(value) if value == value.quantize(Decimal("0.01")) else de_number(value)


def de_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def row_amount(line: InvoiceLineData) -> Decimal:
    """The amount on the product row: quantity x price. Discounts and surcharges follow as their own rows,
    as on real invoices, so the line amount in the data (BT-131) is this minus discounts plus surcharges."""
    return line.line_amount + line.allowance_amount - line.charge_amount


def printed_unit(line: InvoiceLineData, layout: Layout) -> str:
    return (
        layout.printed_units.get(line.line_no)
        or PRINTED_UNIT_NAMES.get(line.unit_code or "")  # XML data only has the code: print "St", not "XPP"
        or line.unit_raw
        or ""
    )


# --- entry point ------------------------------------------------------------------------------------------


def render_pdf(data: InvoiceData, layout: Layout) -> bytes:
    buffer = io.BytesIO()
    if layout.template == "receipt":
        _receipt(buffer, data, layout)
    elif layout.template == "cash_carry":
        _cash_carry(buffer, data, layout)
    else:
        _wholesaler(buffer, data, layout)
    return buffer.getvalue()


# --- wholesaler: classic A4 invoice -----------------------------------------------------------------------


def _wholesaler(buffer: io.BytesIO, data: InvoiceData, layout: Layout) -> None:
    c = Canvas(buffer, pagesize=A4, invariant=1)
    width, height = A4
    s = data.supplier
    left, right = 20 * mm, width - 20 * mm

    # Letterhead
    c.setFont("Helvetica-Bold", 16)
    c.drawString(left, height - 25 * mm, s.name)
    c.setFont("Helvetica", 8)
    c.drawString(left, height - 30 * mm, f"{s.street} · {s.postcode} {s.city}")
    c.drawRightString(right, height - 25 * mm, "Tel. 0391 000000 · buchhaltung@beispiel.example")
    if s.vat_id:
        c.drawRightString(right, height - 29 * mm, f"USt-IdNr.: {s.vat_id}")
    if s.tax_number:
        c.drawRightString(right, height - 33 * mm, f"Steuernummer: {s.tax_number}")

    # Address window: sender line, then the buyer
    c.setFont("Helvetica", 6.5)
    c.drawString(left, height - 48 * mm, f"{s.name} · {s.street} · {s.postcode} {s.city}")
    c.setFont("Helvetica", 10)
    for i, text in enumerate([BUYER["name"], BUYER["street"], f"{BUYER['postcode']} {BUYER['city']}"]):
        c.drawString(left, height - (54 + i * 5) * mm, text)

    # Invoice facts on the right
    facts = [
        ("Rechnungs-Nr.", data.invoice_number or "-"),
        ("Rechnungsdatum", de_date(data.issue_date)),
        ("Lieferdatum", de_date(data.delivery_date)),
        ("Kunden-Nr.", layout.customer_number),
    ]
    c.setFont("Helvetica", 9)
    for i, (label, value) in enumerate(facts):
        y = height - (54 + i * 5) * mm
        c.drawString(125 * mm, y, label)
        c.drawRightString(right, y, value)

    c.setFont("Helvetica-Bold", 14)
    c.drawString(left, height - 82 * mm, "Gutschrift" if data.document_type == "credit_note" else "Rechnung")

    # Product table
    columns = [  # (title, x position, alignment)
        ("Pos", left, "l"),
        ("Art.-Nr.", left + 9 * mm, "l"),
        ("Bezeichnung", left + 27 * mm, "l"),
        ("Menge", left + 110 * mm, "r"),
        ("Einh.", left + 112 * mm, "l"),
        ("Einzelpreis", left + 139 * mm, "r"),
        ("MwSt", left + 153 * mm, "r"),
        ("Gesamt €", right, "r"),
    ]
    y = height - 92 * mm
    c.setFont("Helvetica-Bold", 8)
    for title, x, align in columns:
        (c.drawRightString if align == "r" else c.drawString)(x, y, title)
    c.line(left, y - 1.8 * mm, right, y - 1.8 * mm)
    y -= 6 * mm

    c.setFont("Helvetica", 8)
    for line in data.lines:
        description = simpleSplit(line.description, "Helvetica", 8, 72 * mm)
        values = [
            str(line.line_no),
            line.seller_article_no or "",
            description[0],
            de_number(line.quantity),
            printed_unit(line, layout),
            de_price(line.unit_price),
            f"{de_number(line.vat_rate or Decimal(0))} %",
            de_money(row_amount(line)),
        ]
        for (_title, x, align), value in zip(columns, values, strict=True):
            (c.drawRightString if align == "r" else c.drawString)(x, y, value)
        for extra in description[1:]:  # long descriptions wrap onto further lines
            y -= 4 * mm
            c.drawString(left + 27 * mm, y, extra)
        if line.allowance_amount:
            y -= 4 * mm
            percent = layout.discounts.get(line.line_no)
            label = f"abzgl. {percent} % Rabatt" if percent else "abzgl. Rabatt"
            c.drawString(left + 31 * mm, y, label)
            c.drawRightString(right, y, f"-{de_money(line.allowance_amount)}")
        if line.charge_amount:
            y -= 4 * mm
            c.drawString(left + 31 * mm, y, "zzgl. Zuschlag")
            c.drawRightString(right, y, de_money(line.charge_amount))
        y -= 5 * mm
        if y < 60 * mm:  # next page; totals always fit on the last one
            c.showPage()
            c.setFont("Helvetica", 8)
            y = height - 25 * mm

    # Totals
    c.line(115 * mm, y + 2 * mm, right, y + 2 * mm)
    y -= 3 * mm
    rows = []
    if data.allowance_total:
        rows.append(("abzgl. Nachlass", f"-{de_money(data.allowance_total)}"))
    if data.charge_total:
        rows.append(("zzgl. Fracht / Zuschläge", de_money(data.charge_total)))
    rows.append(("Nettobetrag", de_money(data.net_total or Decimal(0))))
    for entry in data.vat_breakdown:
        rows.append(
            (
                f"zzgl. {de_number(entry.vat_rate)} % MwSt auf {de_money(entry.taxable_amount)}",
                de_money(entry.tax_amount),
            )
        )
    c.setFont("Helvetica", 9)
    for label, value in rows:
        c.drawString(115 * mm, y, label)
        c.drawRightString(right, y, value)
        y -= 5 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(115 * mm, y - 1 * mm, "Rechnungsbetrag EUR")
    c.drawRightString(right, y - 1 * mm, de_money(data.gross_total))

    # Footer
    c.setFont("Helvetica", 7.5)
    c.drawString(left, 25 * mm, "Zahlbar innerhalb von 14 Tagen ohne Abzug.")
    c.drawString(left, 21 * mm, f"Bankverbindung: Beispielbank · IBAN {TEST_IBAN} · BIC BEISDEXXXXX")
    c.save()


# --- cash & carry: dense A4 with VAT codes --------------------------------------------------------------


def _cash_carry(buffer: io.BytesIO, data: InvoiceData, layout: Layout) -> None:
    c = Canvas(buffer, pagesize=A4, invariant=1)
    width, height = A4
    s = data.supplier
    left, right = 15 * mm, width - 15 * mm
    codes = {entry.vat_rate: chr(ord("A") + i) for i, entry in enumerate(data.vat_breakdown)}

    c.setFont("Helvetica-Bold", 18)
    c.drawString(left, height - 22 * mm, s.name.upper())
    c.setFont("Helvetica", 8)
    c.drawString(left, height - 27 * mm, f"{s.street}, {s.postcode} {s.city}")
    tax_id = f"USt-IdNr. {s.vat_id}" if s.vat_id else f"St.-Nr. {s.tax_number}"
    c.drawString(left, height - 31 * mm, tax_id)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, height - 42 * mm, "RECHNUNG")
    c.setFont("Helvetica", 8.5)
    meta = (
        f"Rechnung {data.invoice_number or '-'}   Datum {de_date(data.issue_date)}   "
        f"Lieferung {de_date(data.delivery_date)}   Kunde {layout.customer_number} {BUYER['name']}"
    )
    c.drawString(left, height - 47 * mm, meta)

    columns = [
        ("Art.-Nr.", left, "l"),
        ("Bezeichnung", left + 20 * mm, "l"),
        ("Menge", left + 118 * mm, "r"),
        ("Einh.", left + 120 * mm, "l"),
        ("Preis", left + 150 * mm, "r"),
        ("Betrag", left + 170 * mm, "r"),
        ("MK", right, "r"),
    ]
    y = height - 57 * mm
    c.setFont("Helvetica-Bold", 7.5)
    for title, x, align in columns:
        (c.drawRightString if align == "r" else c.drawString)(x, y, title)
    c.line(left, y - 1.5 * mm, right, y - 1.5 * mm)
    y -= 5 * mm
    c.setFont("Helvetica", 7.5)
    for line in data.lines:
        values = [
            line.seller_article_no or "",
            line.description,
            de_number(line.quantity),
            printed_unit(line, layout),
            de_price(line.unit_price),
            de_money(row_amount(line)),
            codes.get(line.vat_rate, ""),
        ]
        for (_title, x, align), value in zip(columns, values, strict=True):
            (c.drawRightString if align == "r" else c.drawString)(x, y, value)
        if line.allowance_amount:
            y -= 3.6 * mm
            percent = layout.discounts.get(line.line_no)
            c.drawString(left + 24 * mm, y, f"Rabatt {percent} %" if percent else "Rabatt")
            c.drawRightString(left + 170 * mm, y, f"-{de_money(line.allowance_amount)}")
        y -= 4.2 * mm
        if y < 55 * mm:
            c.showPage()
            c.setFont("Helvetica", 7.5)
            y = height - 20 * mm

    # VAT table and total
    y -= 3 * mm
    c.setFont("Helvetica-Bold", 7.5)
    for title, x in [
        ("MK", left),
        ("MwSt %", left + 15 * mm),
        ("Netto", left + 50 * mm),
        ("MwSt", left + 75 * mm),
        ("Brutto", left + 100 * mm),
    ]:
        c.drawRightString(x + 10 * mm, y, title)
    c.setFont("Helvetica", 7.5)
    for entry in data.vat_breakdown:
        y -= 4 * mm
        values = [
            codes[entry.vat_rate],
            de_number(entry.vat_rate),
            de_money(entry.taxable_amount),
            de_money(entry.tax_amount),
            de_money(entry.taxable_amount + entry.tax_amount),
        ]
        for value, x in zip(
            values, [left, left + 15 * mm, left + 50 * mm, left + 75 * mm, left + 100 * mm], strict=True
        ):
            c.drawRightString(x + 10 * mm, y, value)
    y -= 9 * mm
    c.setFont("Helvetica", 9)
    c.drawString(left + 120 * mm, y + 4.5 * mm, f"Summe netto {de_money(data.net_total or Decimal(0))}")
    c.drawString(left + 120 * mm, y, f"MwSt {de_money(data.vat_total or Decimal(0))}")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left + 120 * mm, y - 6 * mm, f"Gesamt EUR {de_money(data.gross_total)}")
    c.setFont("Helvetica", 7)
    c.drawString(left, 20 * mm, f"Zahlung per Lastschrift. IBAN {TEST_IBAN}")
    c.save()


# --- till receipt: 80 mm, gross prices ----------------------------------------------------------------------


def _receipt(buffer: io.BytesIO, data: InvoiceData, layout: Layout) -> None:
    width = 80 * mm
    rows = 18 + 2 * len(data.lines) + len(data.vat_breakdown)
    height = max(120 * mm, rows * 4.2 * mm + 20 * mm)
    c = Canvas(buffer, pagesize=(width, height), invariant=1)
    s = data.supplier
    left, right = 5 * mm, width - 5 * mm
    codes = {entry.vat_rate: chr(ord("A") + i) for i, entry in enumerate(data.vat_breakdown)}
    y = height - 10 * mm

    def center(text: str, font: str = "Courier", size: float = 8) -> None:
        nonlocal y
        c.setFont(font, size)
        c.drawCentredString(width / 2, y, text)
        y -= 4.2 * mm

    def row(text_left: str, text_right: str = "", font: str = "Courier", size: float = 8) -> None:
        nonlocal y
        c.setFont(font, size)
        c.drawString(left, y, text_left)
        if text_right:
            c.drawRightString(right, y, text_right)
        y -= 4.2 * mm

    center(s.name, "Courier-Bold", 9)
    center(s.street)
    center(f"{s.postcode} {s.city}")
    if s.tax_number:
        center(f"St.-Nr. {s.tax_number}")
    y -= 2 * mm
    row(f"{de_date(data.issue_date)} {layout.receipt_time}", f"Bon {layout.receipt_number}")
    row("-" * 40)
    for line in data.lines:
        code = codes.get(line.vat_rate, "")
        row(line.description[:34])
        unit = printed_unit(line, layout)
        if unit in ("kg", "g", "l"):
            row(
                f"  {de_number(line.quantity)} {unit} x {de_price(line.unit_price)} EUR/{unit}",
                f"{de_money(line.line_amount)} {code}",
            )
        else:
            # The unit is printed ("4 Ki x 16,59"), so the truth's unit can be read from the document
            row(
                f"  {de_number(line.quantity)} {unit} x {de_price(line.unit_price)}",
                f"{de_money(line.line_amount)} {code}",
            )
    row("-" * 40)
    row("SUMME EUR", de_money(data.gross_total), "Courier-Bold", 10)
    row("Gegeben Karte", de_money(data.gross_total))
    y -= 2 * mm
    row("MwSt     Netto    Steuer    Brutto")
    for entry in data.vat_breakdown:
        gross = entry.taxable_amount + entry.tax_amount
        row(
            f"{codes[entry.vat_rate]} {de_number(entry.vat_rate):>2}%  {de_money(entry.taxable_amount):>8}  "
            f"{de_money(entry.tax_amount):>7}  {de_money(gross):>8}"
        )
    y -= 2 * mm
    center("Vielen Dank für Ihren Einkauf!")
    c.save()
