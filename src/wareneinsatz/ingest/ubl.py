"""OASIS UBL 2.1 Invoice and CreditNote: the other XML syntax of XRechnung (and of Peppol).

Same business terms as CII, different element names. Each value notes its EN 16931 term.
"""

from decimal import Decimal
from xml.etree.ElementTree import Element

from wareneinsatz.ingest.en16931 import document_type, gtin, merged_vat_breakdown, unit_fields, vat_rate
from wareneinsatz.ingest.errors import ExtractionError
from wareneinsatz.ingest.xmlutil import Node, local_name
from wareneinsatz.schemas import InvoiceData

INVOICE_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CREDIT_NOTE_NAMESPACE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
NAMESPACES = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


def profile(element: Element) -> str | None:
    """BT-24 specification identifier (CustomizationID)."""
    return Node(element, NAMESPACES).text("cbc:CustomizationID")


def parse_ubl(element: Element) -> InvoiceData:
    root = Node(element, NAMESPACES)
    is_credit_note = local_name(element) == "CreditNote"
    # Credit notes use different element names for their lines and quantities.
    line_path, quantity_tag = (
        ("cac:CreditNoteLine", "cbc:CreditedQuantity")
        if is_credit_note
        else ("cac:InvoiceLine", "cbc:InvoicedQuantity")
    )

    party = root.find("cac:AccountingSupplierParty/cac:Party")
    totals = root.find("cac:LegalMonetaryTotal")
    if party is None or totals is None:
        raise ExtractionError("UBL file without supplier party or monetary totals")
    currency = root.text("cbc:DocumentCurrencyCode") or "EUR"  # BT-5
    tax_totals = root.findall("cac:TaxTotal")

    data = {
        "document_type": document_type(  # BT-3
            root.text("cbc:CreditNoteTypeCode" if is_credit_note else "cbc:InvoiceTypeCode"),
            is_credit_note_root=is_credit_note,
        ),
        "invoice_number": root.text("cbc:ID"),  # BT-1
        "issue_date": root.date("cbc:IssueDate"),  # BT-2
        "delivery_date": root.date("cac:Delivery/cbc:ActualDeliveryDate"),  # BT-72
        "currency": currency,
        "prices_include_vat": False,  # e-invoice amounts are always net
        "supplier": _seller(party),
        "lines": [_line(line, line_no, quantity_tag) for line_no, line in enumerate(root.findall(line_path), start=1)],
        "vat_breakdown": merged_vat_breakdown(  # BG-23
            [
                (
                    vat_rate(sub.decimal("cac:TaxCategory/cbc:Percent"), sub.text("cac:TaxCategory/cbc:ID"))
                    or Decimal("0"),
                    sub.decimal("cbc:TaxableAmount") or Decimal("0"),  # BT-116
                    sub.decimal("cbc:TaxAmount") or Decimal("0"),  # BT-117
                )
                for tax_total in tax_totals
                for sub in tax_total.findall("cac:TaxSubtotal")
            ]
        ),
        "allowance_total": totals.decimal("cbc:AllowanceTotalAmount") or Decimal("0"),  # BT-107
        "charge_total": totals.decimal("cbc:ChargeTotalAmount") or Decimal("0"),  # BT-108
        "net_total": totals.decimal("cbc:TaxExclusiveAmount"),  # BT-109
        "vat_total": _vat_total(tax_totals, currency),  # BT-110
        "gross_total": totals.decimal("cbc:TaxInclusiveAmount"),  # BT-112
    }
    return InvoiceData.model_validate(data)


def _seller(party: Node) -> dict:
    """BG-4 Seller. The VAT ID sits in the PartyTaxScheme whose scheme is 'VAT'; any other scheme is BT-32."""
    vat_id = tax_number = None
    for scheme in party.findall("cac:PartyTaxScheme"):
        if scheme.text("cac:TaxScheme/cbc:ID") == "VAT":
            vat_id = scheme.text("cbc:CompanyID")  # BT-31
        else:
            tax_number = scheme.text("cbc:CompanyID")  # BT-32
    return {
        "name": party.text("cac:PartyLegalEntity/cbc:RegistrationName")  # BT-27
        or party.text("cac:PartyName/cbc:Name"),  # BT-28 trading name as fallback
        "vat_id": vat_id,
        "tax_number": tax_number,
        "street": party.text("cac:PostalAddress/cbc:StreetName"),  # BT-35
        "postcode": party.text("cac:PostalAddress/cbc:PostalZone"),  # BT-38
        "city": party.text("cac:PostalAddress/cbc:CityName"),  # BT-37
        "country_code": party.text("cac:PostalAddress/cac:Country/cbc:IdentificationCode") or "DE",  # BT-40
    }


def _line(line: Node, line_no: int, quantity_tag: str) -> dict:
    """BG-25 Invoice line, numbered by position (BT-126 is free text)."""
    item = "cac:Item"
    base_quantity = line.decimal("cac:Price/cbc:BaseQuantity")  # BT-149
    return {
        "line_no": line_no,
        "description": line.text(f"{item}/cbc:Name")  # BT-153
        or line.text(f"{item}/cbc:Description")  # BT-154
        or f"Position {line_no}",
        "seller_article_no": line.text(f"{item}/cac:SellersItemIdentification/cbc:ID"),  # BT-155
        "gtin": gtin(  # BT-157
            line.text(f"{item}/cac:StandardItemIdentification/cbc:ID"),
            line.attr(f"{item}/cac:StandardItemIdentification/cbc:ID", "schemeID"),
        ),
        "quantity": line.decimal(quantity_tag),  # BT-129
        **unit_fields(line.attr(quantity_tag, "unitCode")),  # BT-130
        "unit_price": line.decimal("cac:Price/cbc:PriceAmount"),  # BT-146
        "price_base_quantity": base_quantity if base_quantity is not None else Decimal("1"),
        "line_amount": line.decimal("cbc:LineExtensionAmount"),  # BT-131
        "vat_rate": vat_rate(
            line.decimal(f"{item}/cac:ClassifiedTaxCategory/cbc:Percent"),  # BT-152
            line.text(f"{item}/cac:ClassifiedTaxCategory/cbc:ID"),  # BT-151
        ),
    }


def _vat_total(tax_totals: list[Node], currency: str) -> Decimal | None:
    """BT-110: the TaxAmount in the document currency (a second TaxTotal may be in the accounting currency)."""
    for tax_total in tax_totals:
        if tax_total.attr("cbc:TaxAmount", "currencyID") in (None, currency):
            return tax_total.decimal("cbc:TaxAmount")
    return tax_totals[0].decimal("cbc:TaxAmount") if tax_totals else None
