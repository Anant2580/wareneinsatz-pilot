"""What the language model returns: the document's content as printed text, plus the prompt.

The model only transcribes and labels. Every value stays a string exactly as printed
("1.234,56", "18.10.2026", "A", "Krt"). Turning text into numbers, dates, VAT rates and unit
codes, and every calculation, happens afterwards in Python (interpret.py), where it is tested.
This keeps reading errors and calculation errors apart (decision 2 in docs/decisions.md).

The schema is passed to the model as a JSON schema ("structured output"), so the reply is
always valid JSON with exactly these keys. All fields are required but nullable: the model
must write null for anything that is not printed instead of leaving a key out.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

PROMPT_VERSION = "2026-10-29.1"  # stored with every extraction, so results stay traceable to the prompt


class _Printed(BaseModel):
    # The reply is checked strictly: an invented key means the model ignored the schema.
    model_config = ConfigDict(extra="forbid")


class PrintedSupplier(_Printed):
    name: str | None  # company in the letterhead, not the customer
    address: str | None  # one line as printed, e.g. "Am Bahnhof 2, 39104 Magdeburg"
    vat_id: str | None  # USt-IdNr., e.g. "DE 123 456 789"
    tax_number: str | None  # Steuernummer, e.g. "105/279/36294"


class PrintedLine(_Printed):
    article_no: str | None  # Art.-Nr. as printed
    description: str | None  # product text as printed, all of it
    quantity: str | None  # e.g. "4,887" or "3"
    unit: str | None  # e.g. "kg", "Krt", "St"
    unit_price: str | None  # e.g. "8,89"
    amount: str | None  # amount on the product row itself, before a discount row
    discount: str | None  # amount of a discount row (Rabatt) printed under this product
    surcharge: str | None  # amount of a surcharge row (Zuschlag) printed under this product
    vat: str | None  # what the row shows about VAT: a rate ("7 %") or a code ("A")


class PrintedVatRate(_Printed):
    code: str | None  # VAT code if the document uses codes ("A"), else null
    rate: str | None  # e.g. "7 %"
    net: str | None  # net amount at this rate
    tax: str | None  # VAT amount at this rate


class PrintedInvoice(_Printed):
    document_title: str | None  # heading as printed: "Rechnung", "Gutschrift", "Kassenbon"
    supplier: PrintedSupplier
    invoice_number: str | None
    receipt_number: str | None  # "Bon 1007" on till receipts; not an invoice number
    issue_date: str | None
    delivery_date: str | None
    currency: str | None
    lines: list[PrintedLine]
    vat_rates: list[PrintedVatRate]
    discount_total: str | None  # discount on the whole document (Nachlass)
    surcharge_total: str | None  # charges on the whole document (Fracht, Zuschläge)
    net_total: str | None
    vat_total: str | None
    gross_total: str | None


# Compact on purpose: every token the model writes costs time on a laptop.
EXAMPLE_REPLY = (
    '{"document_title":"Rechnung","supplier":{"name":"Beispiel Frischdienst GmbH",'
    '"address":"Lindenweg 4, 12345 Musterstadt","vat_id":"DE 999 999 999","tax_number":null},'
    '"invoice_number":"R-2001","receipt_number":null,"issue_date":"02.03.2026","delivery_date":"01.03.2026",'
    '"currency":"EUR","lines":[{"article_no":"4711","description":"Tomaten rund 6 kg","quantity":"2",'
    '"unit":"Krt","unit_price":"12,90","amount":"25,80","discount":"-2,58","surcharge":null,"vat":"7 %"},'
    '{"article_no":null,"description":"Pfand Kiste","quantity":"1","unit":"St","unit_price":"3,10",'
    '"amount":"3,10","discount":null,"surcharge":null,"vat":"19 %"}],'
    '"vat_rates":[{"code":null,"rate":"7 %","net":"23,22","tax":"1,63"},'
    '{"code":null,"rate":"19 %","net":"3,10","tax":"0,59"}],"discount_total":null,"surcharge_total":null,'
    '"net_total":"26,32","vat_total":"2,22","gross_total":"28,54"}'
)

RULES = [
    "Copy every value exactly as printed, character for character: numbers in German format (1.234,56), "
    "dates as printed (18.10.2026), units as printed (kg, Krt, St, Ki). Never convert, round or calculate anything.",
    "Use null for anything that is not printed. Never guess a value.",
    "lines: one entry per product or service, in the order printed. Deposit rows (Pfand) with their own amount "
    "are lines. Not lines: subtotals, totals, VAT rows, payment rows (Gegeben, Karte, Bar, Rückgeld).",
    "A discount row under a product (Rabatt, abzgl. Rabatt) is not a line: put its amount into that product's "
    '"discount". A surcharge row (Zuschlag) goes into "surcharge".',
    'On till receipts the product name is on one row and "quantity unit x price" with the amount on the next '
    "row. Together they are one line: the name is the description.",
    "amount: the amount printed on the product row itself.",
    'vat: what the product row shows about VAT, a rate ("7 %") or a code ("A", "B"). vat_rates: the VAT table '
    "at the bottom, one entry per rate, with the code if codes are used.",
    "invoice_number: only a number labelled as invoice number (Rechnungs-Nr., Rechnungsnummer, Rechnung). "
    "A receipt number (Bon) goes into receipt_number.",
    "supplier: the company that issued the document (letterhead, sender line), never the customer "
    "(Kunde, Rechnungsempfänger).",
]

SYSTEM_PROMPT = "\n".join(
    [
        "You read German supplier invoices and till receipts for a restaurant and return their content as JSON.",
        "",
        "Rules:",
        *(f"{number}. {rule}" for number, rule in enumerate(RULES, start=1)),
        "",
        f"Example of the format (a different document): {EXAMPLE_REPLY}",
        "",
        "Answer with compact JSON on one line, without indentation.",
    ]
)


def user_message(texts: list[tuple[str, str | None]], *, with_images: bool) -> str:
    """The request for one document: its page images and/or its text.

    texts: (source, text) per page, where source is "pdf" (the PDF's own text layer, exact) or
    "ocr" (recognised from the image, may contain reading errors).
    """
    parts = []
    if with_images:
        parts.append("The image(s) show the document, one image per page.")
    readable = [(source, text) for source, text in texts if text and text.strip()]
    if readable:
        if any(source == "ocr" for source, _ in readable):
            note = "Text recognised by OCR, page by page. It may contain reading errors"
            note += "; where it disagrees with the image, trust the image." if with_images else "."
        else:
            note = "Text of the document from the PDF, page by page."
        parts.append(note)
        for number, (_source, text) in enumerate(readable, start=1):
            parts.append(f"--- page {number} ---\n{text.strip()}")
    parts.append("Return the document's content as JSON.")
    return "\n\n".join(parts)
