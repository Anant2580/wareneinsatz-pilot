"""Unit tests for reading e-invoices: XRechnung (UBL and CII), ZUGFeRD PDFs, routing and XML safety."""

from decimal import Decimal
from pathlib import Path

import pytest

from wareneinsatz.ingest import ExtractionError, NeedsVisualExtraction, extract_invoice
from wareneinsatz.ingest.detect import FileKind, detect_file_type
from wareneinsatz.ingest.en16931 import merged_vat_breakdown
from wareneinsatz.ingest.extract import parse_einvoice_xml
from wareneinsatz.schemas import DocumentType

KOSIT = Path(__file__).parent / "samples" / "kosit"
KOSIT_FILES = sorted(KOSIT.glob("*.xml"))
KOSIT_PAIRS = sorted(path.name.removesuffix("_ubl.xml") for path in KOSIT.glob("*_ubl.xml"))
# These pairs differ in the official files themselves (see tests/samples/kosit/README.md).
SUPPLIER_DIFFERS_IN_SOURCE = {"01.20a-INVOICE", "01.21a-INVOICE", "03.06a-INVOICE"}

# The step 1 JSON invoice prints units as "kg"/"Krt"; e-invoices only carry the code, so unit_raw differs.
WITHOUT_UNIT_RAW = {"lines": {"__all__": {"unit_raw"}}}


# --- XRechnung: both syntaxes give the same canonical invoice -----------------------------------


@pytest.mark.parametrize(
    ("filename", "syntax"), [("musterfrisch_xrechnung_cii.xml", "cii"), ("musterfrisch_xrechnung_ubl.xml", "ubl")]
)
def test_xrechnung_gives_exactly_the_expected_invoice(samples, invoice, filename, syntax):
    """The same invoice written as CII and as UBL must produce the step 1 JSON invoice, field by field."""
    result = extract_invoice((samples / "einvoices" / filename).read_bytes())
    assert result.method == f"xml-{syntax}"
    assert "xrechnung_3.0" in result.profile
    assert result.data.model_dump(exclude=WITHOUT_UNIT_RAW) == invoice.model_dump(exclude=WITHOUT_UNIT_RAW)
    assert result.data.lines[0].unit_raw == "KGM"


@pytest.mark.parametrize("path", KOSIT_FILES, ids=lambda p: p.name)
def test_official_samples_parse_and_add_up(path):
    """Every official KoSIT test invoice parses, and its totals are consistent (EN 16931 rules BR-CO-13/15)."""
    data, _syntax, _profile = parse_einvoice_xml(path.read_bytes())
    assert data.lines
    assert data.net_total == sum(line.line_amount for line in data.lines) + data.charge_total - data.allowance_total
    assert data.gross_total == data.net_total + data.vat_total


@pytest.mark.parametrize("case", KOSIT_PAIRS)
def test_official_ubl_and_cii_versions_agree(case):
    ubl, _, _ = parse_einvoice_xml((KOSIT / f"{case}_ubl.xml").read_bytes())
    cii, _, _ = parse_einvoice_xml((KOSIT / f"{case}_uncefact.xml").read_bytes())
    assert ubl.model_dump(exclude={"supplier"}) == cii.model_dump(exclude={"supplier"})
    if case not in SUPPLIER_DIFFERS_IN_SOURCE:
        assert ubl.supplier == cii.supplier


def test_unit_price_with_nine_decimals_is_kept_exactly():
    """Official case 03.05a: the reason unit prices got 10 decimals in migration 0002."""
    data, _, _ = parse_einvoice_xml((KOSIT / "03.05a-INVOICE_ubl.xml").read_bytes())
    assert Decimal("0.061232374") in [line.unit_price for line in data.lines]


def test_credit_notes_are_recognised(samples):
    cii = (samples / "einvoices" / "musterfrisch_xrechnung_cii.xml").read_text(encoding="utf-8")
    data, _, _ = parse_einvoice_xml(
        cii.replace("<ram:TypeCode>380</ram:TypeCode>", "<ram:TypeCode>381</ram:TypeCode>").encode()
    )
    assert data.document_type is DocumentType.CREDIT_NOTE

    ubl = (samples / "einvoices" / "musterfrisch_xrechnung_ubl.xml").read_text(encoding="utf-8")
    credit_note = (
        ubl.replace("ubl:Invoice", "ubl:CreditNote")
        .replace("xsd:Invoice-2", "xsd:CreditNote-2")
        .replace("cbc:InvoiceTypeCode>380<", "cbc:CreditNoteTypeCode>381<")
        .replace("</cbc:InvoiceTypeCode>", "</cbc:CreditNoteTypeCode>")
        .replace("cac:InvoiceLine", "cac:CreditNoteLine")
        .replace("cbc:InvoicedQuantity", "cbc:CreditedQuantity")
    )
    data, syntax, _ = parse_einvoice_xml(credit_note.encode())
    assert (syntax, data.document_type, len(data.lines)) == ("ubl", DocumentType.CREDIT_NOTE, 7)


def test_vat_breakdowns_with_the_same_rate_are_merged():
    """EN 16931 has one breakdown per VAT category, so 'exempt' and 'zero rated' can both be 0 %."""
    merged = merged_vat_breakdown(
        [
            (Decimal("0"), Decimal("10.00"), Decimal("0")),
            (Decimal("19"), Decimal("100.00"), Decimal("19.00")),
            (Decimal("0"), Decimal("5.00"), Decimal("0")),
        ]
    )
    assert merged == [
        {"vat_rate": Decimal("0"), "taxable_amount": Decimal("15.00"), "tax_amount": Decimal("0")},
        {"vat_rate": Decimal("19"), "taxable_amount": Decimal("100.00"), "tax_amount": Decimal("19.00")},
    ]


# --- ZUGFeRD / Factur-X: PDF with the XML inside --------------------------------------------------


@pytest.mark.parametrize("attachment_name", ["factur-x.xml", "ZUGFeRD-invoice.xml", "xrechnung.xml", "rechnung.xml"])
def test_zugferd_pdf_is_read_from_its_embedded_xml(samples, make_pdf, attachment_name):
    xml = (samples / "einvoices" / "musterfrisch_xrechnung_cii.xml").read_bytes()
    result = extract_invoice(make_pdf({attachment_name: xml}))
    assert result.method == "pdf-zugferd"
    assert result.data == parse_einvoice_xml(xml)[0]


def test_plain_pdf_goes_to_visual_extraction(make_pdf):
    with pytest.raises(NeedsVisualExtraction, match="without embedded"):
        extract_invoice(make_pdf())


def test_zugferd_with_unusable_xml_falls_back_to_the_pdf_page(make_pdf):
    """E.g. an old ZUGFeRD 1.0 attachment: the page is still readable, so OCR can take over."""
    old_format = b'<rsm:CrossIndustryDocument xmlns:rsm="urn:ferd:CrossIndustryDocument:invoice:1p0"/>'
    with pytest.raises(NeedsVisualExtraction, match="could not be used"):
        extract_invoice(make_pdf({"ZUGFeRD-invoice.xml": old_format}))


def test_minimum_profile_without_lines_goes_to_visual_extraction(samples):
    """ZUGFeRD MINIMUM / BASIC WL carry only totals; item prices must come from the PDF page."""
    with pytest.raises(NeedsVisualExtraction, match="minimum"):
        extract_invoice((samples / "einvoices" / "musterfrisch_facturx_minimum.xml").read_bytes())


# --- Routing by file content ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "kind", "suffix"),
    [
        (b"%PDF-1.7\n...", FileKind.PDF, ".pdf"),
        (b"\xef\xbb\xbf<?xml version='1.0'?><a/>", FileKind.XML, ".xml"),  # with UTF-8 byte order mark
        (b"\xff\xd8\xff\xe0 JFIF", FileKind.IMAGE, ".jpg"),
        (b"\x89PNG\r\n\x1a\n....", FileKind.IMAGE, ".png"),
        (b"\x00\x00\x00\x18ftypheic....", FileKind.IMAGE, ".heic"),
        (b"just some text", FileKind.UNKNOWN, ".bin"),
    ],
)
def test_file_type_is_detected_from_content(content, kind, suffix):
    file_type = detect_file_type(content)
    assert (file_type.kind, file_type.suffix) == (kind, suffix)


def test_photo_goes_to_visual_extraction():
    with pytest.raises(NeedsVisualExtraction, match="image/jpeg"):
        extract_invoice(b"\xff\xd8\xff\xe0 fake jpeg")


def test_unknown_file_type_fails():
    with pytest.raises(ExtractionError, match="Unsupported file type"):
        extract_invoice(b"Hallo, hier ist keine Rechnung.")


# --- Untrusted XML ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "xml",
    [
        # XXE: an external entity that would read a local file into the document
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>',
        # "Billion laughs": nested entities that expand to gigabytes in memory
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><x>&b;</x>',
    ],
    ids=["external-entity", "entity-expansion"],
)
def test_dangerous_xml_is_refused(xml):
    with pytest.raises(ExtractionError, match="Unsafe XML"):
        extract_invoice(xml)


def test_broken_xml_fails_clearly():
    with pytest.raises(ExtractionError, match="Not well-formed"):
        extract_invoice(b"<rsm:CrossIndustryInvoice><unclosed>")


def test_xml_that_is_not_an_invoice_fails():
    with pytest.raises(ExtractionError, match="Not an EN 16931 e-invoice"):
        extract_invoice(b"<html><body>Bestellung</body></html>")
