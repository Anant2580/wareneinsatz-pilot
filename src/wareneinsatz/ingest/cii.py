"""UN/CEFACT Cross Industry Invoice (CII): the XML syntax of ZUGFeRD 2, Factur-X and XRechnung-CII.

Each value is read from the element that EN 16931 assigns to the business term
(BT-x), noted in the comments, and returned as InvoiceData.
"""

from decimal import Decimal
from xml.etree.ElementTree import Element

from wareneinsatz.ingest.en16931 import document_type, gtin, merged_vat_breakdown, unit_fields, vat_rate
from wareneinsatz.ingest.errors import ExtractionError, NeedsVisualExtraction
from wareneinsatz.ingest.xmlutil import Node
from wareneinsatz.schemas import InvoiceData

NAMESPACES = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
ROOT_NAMESPACE = NAMESPACES["rsm"]


def profile(element: Element) -> str | None:
    """Specification identifier, e.g. 'urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0'."""
    return Node(element, NAMESPACES).text(
        "rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID"
    )


def parse_cii(element: Element) -> InvoiceData:
    root = Node(element, NAMESPACES)
    document = root.find("rsm:ExchangedDocument")
    trade = root.find("rsm:SupplyChainTradeTransaction")
    if document is None or trade is None:
        raise ExtractionError("CII file without ExchangedDocument or SupplyChainTradeTransaction")

    items = trade.findall("ram:IncludedSupplyChainTradeLineItem")
    if not items:
        # The ZUGFeRD profiles MINIMUM and BASIC WL carry only totals, no products or prices.
        # The PDF itself still shows the lines, so it goes to visual extraction instead.
        raise NeedsVisualExtraction(f"XML has no invoice lines (profile: {profile(element) or 'unknown'})")

    seller = trade.find("ram:ApplicableHeaderTradeAgreement/ram:SellerTradeParty")
    settlement = trade.find("ram:ApplicableHeaderTradeSettlement")
    totals = settlement.find("ram:SpecifiedTradeSettlementHeaderMonetarySummation") if settlement is not None else None
    if seller is None or settlement is None or totals is None:
        raise ExtractionError("CII file without seller, settlement or totals")
    currency = settlement.text("ram:InvoiceCurrencyCode") or "EUR"  # BT-5

    data = {
        "document_type": document_type(document.text("ram:TypeCode")),  # BT-3
        "invoice_number": document.text("ram:ID"),  # BT-1
        "issue_date": document.date("ram:IssueDateTime/udt:DateTimeString"),  # BT-2
        "delivery_date": trade.date(  # BT-72
            "ram:ApplicableHeaderTradeDelivery/ram:ActualDeliverySupplyChainEvent/ram:OccurrenceDateTime/udt:DateTimeString"
        ),
        "currency": currency,
        "prices_include_vat": False,  # e-invoice amounts are always net
        "supplier": _seller(seller),
        "lines": [_line(item, line_no) for line_no, item in enumerate(items, start=1)],
        "vat_breakdown": merged_vat_breakdown(  # BG-23
            [
                (
                    vat_rate(tax.decimal("ram:RateApplicablePercent"), tax.text("ram:CategoryCode")) or Decimal("0"),
                    tax.decimal("ram:BasisAmount") or Decimal("0"),  # BT-116
                    tax.decimal("ram:CalculatedAmount") or Decimal("0"),  # BT-117
                )
                for tax in settlement.findall("ram:ApplicableTradeTax")
            ]
        ),
        "allowance_total": totals.decimal("ram:AllowanceTotalAmount") or Decimal("0"),  # BT-107
        "charge_total": totals.decimal("ram:ChargeTotalAmount") or Decimal("0"),  # BT-108
        "net_total": totals.decimal("ram:TaxBasisTotalAmount"),  # BT-109
        "vat_total": _vat_total(totals, currency),  # BT-110
        "gross_total": totals.decimal("ram:GrandTotalAmount"),  # BT-112
    }
    return InvoiceData.model_validate(data)


def _seller(seller: Node) -> dict:
    """BG-4 Seller."""
    return {
        "name": seller.text("ram:Name"),  # BT-27
        "vat_id": seller.text("ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']"),  # BT-31
        "tax_number": seller.text("ram:SpecifiedTaxRegistration/ram:ID[@schemeID='FC']"),  # BT-32
        "street": seller.text("ram:PostalTradeAddress/ram:LineOne"),  # BT-35
        "postcode": seller.text("ram:PostalTradeAddress/ram:PostcodeCode"),  # BT-38
        "city": seller.text("ram:PostalTradeAddress/ram:CityName"),  # BT-37
        "country_code": seller.text("ram:PostalTradeAddress/ram:CountryID") or "DE",  # BT-40
    }


def _line(item: Node, line_no: int) -> dict:
    """BG-25 Invoice line. Lines are numbered by position; BT-126 (line ID) is free text."""
    product = "ram:SpecifiedTradeProduct"
    price = "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice"
    quantity = "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity"
    tax = "ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax"
    base_quantity = item.decimal(f"{price}/ram:BasisQuantity")  # BT-149
    # BG-27 / BG-28 line allowances and charges; ChargeIndicator true = charge, false = allowance
    allowances = charges = Decimal("0")
    for entry in item.findall("ram:SpecifiedLineTradeSettlement/ram:SpecifiedTradeAllowanceCharge"):
        amount = entry.decimal("ram:ActualAmount") or Decimal("0")  # BT-136 / BT-141
        if entry.text("ram:ChargeIndicator/udt:Indicator") == "true":
            charges += amount
        else:
            allowances += amount
    return {
        "line_no": line_no,
        "description": item.text(f"{product}/ram:Name")  # BT-153
        or item.text(f"{product}/ram:Description")  # BT-154
        or f"Position {line_no}",
        "seller_article_no": item.text(f"{product}/ram:SellerAssignedID"),  # BT-155
        "gtin": gtin(  # BT-157
            item.text(f"{product}/ram:GlobalID[@schemeID='0160']"),
            item.attr(f"{product}/ram:GlobalID[@schemeID='0160']", "schemeID"),
        ),
        "quantity": item.decimal(quantity),  # BT-129
        **unit_fields(item.attr(quantity, "unitCode")),  # BT-130
        "unit_price": item.decimal(f"{price}/ram:ChargeAmount"),  # BT-146
        "price_base_quantity": base_quantity if base_quantity is not None else Decimal("1"),
        "allowance_amount": allowances,
        "charge_amount": charges,
        "line_amount": item.decimal(  # BT-131
            "ram:SpecifiedLineTradeSettlement/ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount"
        ),
        "vat_rate": vat_rate(item.decimal(f"{tax}/ram:RateApplicablePercent"), item.text(f"{tax}/ram:CategoryCode")),
    }


def _vat_total(totals: Node, currency: str) -> Decimal | None:
    """BT-110. There can be a second TaxTotalAmount in the accounting currency; take the invoice currency's."""
    candidates = totals.findall("ram:TaxTotalAmount")
    for candidate in candidates:
        if candidate.attr(".", "currencyID") in (None, currency):
            return candidate.decimal(".")
    return candidates[0].decimal(".") if candidates else None
