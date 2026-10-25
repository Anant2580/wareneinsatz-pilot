"""Canonical invoice schema: the one structure every extractor must produce.

The XRechnung/ZUGFeRD parser (step 2) and the OCR + LLM extractor (step 5) both
return an ``InvoiceData`` object. Everything after that (validation, storage,
matching, recipe costing) only knows this schema, never the source format.

Field names follow the European e-invoicing standard EN 16931, which XRechnung
and ZUGFeRD are based on. The business-term numbers (BT-x for a field, BG-x for
a group) are noted next to each field, so the XML mapping is 1:1 and checkable.

Two rules shape the schema:

1. Store what is printed. Amounts are taken exactly as they appear on the
   document, net or gross (see ``prices_include_vat``). Conversions such as
   gross -> net happen later in Python, never inside the LLM.
2. Strict structure, lenient business rules. A wrong type, an unknown field or
   too many decimals is rejected here. A value that is well-formed but
   suspicious (totals that don't add up, an unusual VAT rate) is accepted and
   flagged by the validation step (step 3), so a person can review it.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator


def _blank_to_none(value: object) -> object:
    """LLMs and some XML files write '' for 'not given'. Treat that as missing (NULL)."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


# Reusable field types. The digit limits match the NUMERIC columns in the database,
# so a value that passes here is never silently rounded when it is stored.
OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]
Amount = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]  # EN 16931 amounts have at most 2 decimals
UnitPrice = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=6)]  # rule BR-27: price is never negative
Quantity = Annotated[Decimal, Field(max_digits=18, decimal_places=4)]  # negative for returned goods
VatRate = Annotated[Decimal, Field(ge=0, lt=100, max_digits=5, decimal_places=2)]  # in percent: 19, 7, 0


class DocumentType(StrEnum):
    """BT-3 invoice type, reduced to what matters for food cost."""

    INVOICE = "invoice"  # UNTDID 1001 code 380 and similar
    CREDIT_NOTE = "credit_note"  # code 381, e.g. for returned goods or empties (Leergut)


class _Strict(BaseModel):
    """Base class: unknown fields are an error (catches invented LLM keys), strings are trimmed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SupplierData(_Strict):
    """BG-4 Seller: the supplier who issued the invoice."""

    name: str = Field(min_length=1)  # BT-27
    vat_id: OptionalText = None  # BT-31 USt-IdNr., e.g. DE123456789
    tax_number: OptionalText = None  # BT-32 Steuernummer (some small suppliers print only this)
    street: OptionalText = None  # BT-35
    postcode: OptionalText = None  # BT-38
    city: OptionalText = None  # BT-37
    country_code: str = Field(default="DE", pattern=r"^[A-Z]{2}$")  # BT-40, ISO 3166-1 alpha-2

    @field_validator("vat_id")
    @classmethod
    def normalise_vat_id(cls, value: str | None) -> str | None:
        """'de 123.456.789' -> 'DE123456789', so the same supplier is always found again."""
        if value is None:
            return None
        return re.sub(r"[\s.\-]", "", value).upper() or None


class InvoiceLineData(_Strict):
    """BG-25 Invoice line: one product or service on the invoice."""

    line_no: int = Field(ge=1)  # position on the document, numbered 1..n
    description: str = Field(min_length=1)  # BT-153 item name as printed, e.g. "Hähnchenbrustfilet 2,5 kg TK"
    seller_article_no: OptionalText = None  # BT-155 supplier's article number: the best key for matching later
    gtin: OptionalText = Field(default=None, pattern=r"^(\d{8}|\d{12,14})$")  # BT-157 EAN/GTIN barcode number
    quantity: Quantity  # BT-129
    unit_code: OptionalText = Field(default=None, pattern=r"^[A-Z0-9]{2,3}$")  # BT-130 UN/ECE Rec 20/21: KGM, H87, XCT
    unit_raw: OptionalText = None  # unit as printed ("kg", "Krt", "St"); OCR documents are mapped to unit_code in code
    unit_price: UnitPrice  # BT-146 price per price_base_quantity units
    price_base_quantity: Decimal = Field(default=Decimal(1), gt=0, max_digits=18, decimal_places=4)  # BT-149
    line_amount: Amount  # BT-131 quantity x price, net (or gross if the document prints gross prices)
    vat_rate: VatRate | None = None  # BT-152; None if the document doesn't show it (flagged for review)
    confidence: float | None = Field(default=None, ge=0, le=1)  # extractor's confidence; None for exact XML


class VatBreakdownData(_Strict):
    """BG-23 VAT breakdown: one entry per VAT rate on the invoice."""

    vat_rate: VatRate  # BT-119
    taxable_amount: Amount  # BT-116 net amount taxed at this rate
    tax_amount: Amount  # BT-117 VAT at this rate


class InvoiceData(_Strict):
    """A supplier invoice or till receipt in canonical form."""

    document_type: DocumentType = DocumentType.INVOICE  # BT-3
    invoice_number: OptionalText = None  # BT-1; missing on some till receipts
    issue_date: date | None = None  # BT-2; required before approval (enforced in the database)
    delivery_date: date | None = None  # BT-72 Leistungsdatum
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")  # BT-5, ISO 4217
    prices_include_vat: bool = False  # True for till receipts that print gross prices; e-invoices are always net
    supplier: SupplierData  # BG-4
    lines: list[InvoiceLineData] = Field(min_length=1)  # BG-25
    vat_breakdown: list[VatBreakdownData] = Field(default_factory=list)  # BG-23
    allowance_total: Amount = Field(default=Decimal("0.00"), ge=0)  # BT-107 discounts on the whole document
    charge_total: Amount = Field(default=Decimal("0.00"), ge=0)  # BT-108 e.g. delivery fee
    net_total: Amount | None = None  # BT-109 total without VAT (not printed on every receipt)
    vat_total: Amount | None = None  # BT-110
    gross_total: Amount  # BT-112 total with VAT: printed on every invoice and receipt

    @model_validator(mode="after")
    def check_unique_keys(self) -> InvoiceData:
        """Line numbers and VAT rates are keys in the database, so they must be unique."""
        line_numbers = [line.line_no for line in self.lines]
        if len(line_numbers) != len(set(line_numbers)):
            raise ValueError("line_no must be unique within an invoice")
        rates = [entry.vat_rate for entry in self.vat_breakdown]
        if len(rates) != len(set(rates)):
            raise ValueError("vat_breakdown must have exactly one entry per VAT rate")
        return self
