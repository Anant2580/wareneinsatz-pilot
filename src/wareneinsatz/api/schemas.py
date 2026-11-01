"""Request and response models of the REST API. Decimals travel as strings, so no cent is lost."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from wareneinsatz.schemas import InvoiceData


class IssueOut(BaseModel):
    code: str
    severity: str  # error or warning
    message: str
    field: str | None = None


class IngestOut(BaseModel):
    filename: str
    outcome: str  # approved, needs_review, needs_ocr, duplicate, failed
    message: str
    invoice_id: int | None = None
    document_id: int | None = None
    method: str | None = None
    extraction_ms: int | None = None
    issues: list[IssueOut] = []


class DocumentOut(BaseModel):
    id: int
    filename: str
    media_type: str
    status: str  # received, extracted, failed
    error: str | None
    extraction_method: str | None
    received_at: datetime
    invoice_id: int | None


class ExtractionStatus(BaseModel):
    running: bool
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total: int = 0
    done: int = 0
    outcomes: dict[str, int] = {}
    error: str | None = None
    waiting: int = 0  # documents still waiting for OCR


class InvoiceSummary(BaseModel):
    id: int
    document_id: int
    filename: str
    supplier: str
    invoice_number: str | None
    issue_date: date | None
    gross_total: Decimal
    currency: str
    status: str
    errors: int
    warnings: int
    extraction_method: str | None
    corrected_by: str | None
    approved_by: str | None
    rejected_reason: str | None


class LineOut(BaseModel):
    id: int
    invoice_id: int
    line_no: int
    supplier: str
    description: str
    quantity: Decimal
    unit: str | None
    line_amount: Decimal
    match_status: str | None
    match_method: str | None
    match_score: float | None
    ingredient_key: str | None
    base_quantity: Decimal | None
    base_unit: str | None


class InvoiceDetail(InvoiceSummary):
    data: InvoiceData  # the values, in the canonical schema: the review form
    issues: list[IssueOut]
    lines: list[LineOut]
    raw_extraction: dict[str, Any] | None  # what the extractor read, before any correction


class ReviewIn(BaseModel):
    data: InvoiceData
    by: str = Field(min_length=1)


class ByIn(BaseModel):
    by: str = Field(min_length=1)


class RejectIn(ByIn):
    reason: str = Field(min_length=1)


class ConfirmIn(BaseModel):
    ingredient_key: str | None = None  # None: confirm the suggestion as it is
    per_unit: Decimal | None = Field(default=None, gt=0)  # amount of ingredient in one invoice unit
    by: str = Field(default="reviewer", min_length=1)


class ConfirmOut(BaseModel):
    ingredient_key: str
    base_qty_per_unit: Decimal | None
    confirmed_line_ids: list[int]


class IngredientOut(BaseModel):
    key: str
    name: str
    base_unit: str
    category: str | None
    is_food: bool


class ItemCostOut(BaseModel):
    label: str
    source: str  # ingredient or recipe
    key: str
    quantity: Decimal
    base_unit: str
    waste_pct: Decimal
    price_per_base: Decimal | None
    cost: Decimal | None


class DishOut(BaseModel):
    key: str
    name: str
    kind: str  # dish or preparation
    menu_price_gross: Decimal | None
    net_price: Decimal | None
    cost: Decimal | None  # None while an ingredient has no price
    food_cost_pct: Decimal | None
    target_food_cost_pct: Decimal | None
    margin: Decimal | None
    over_target: bool
    missing: list[str]


class DishDetail(DishOut):
    items: list[ItemCostOut]
    yield_quantity: Decimal
    yield_unit: str


class PricePointOut(BaseModel):
    issue_date: date
    supplier: str
    ingredient_key: str
    ingredient_name: str
    price: Decimal  # net, per display unit
    display_unit: str  # kg, l or piece


class AlertOut(BaseModel):
    id: int
    kind: str
    subject: str
    message: str
    happened_on: date
    acknowledged_by: str | None
    details: dict[str, Any]


class AlertCheckOut(BaseModel):
    new: int
    open: int


class MonthOut(BaseModel):
    month: date
    food: Decimal
    non_food: Decimal
    unmatched: Decimal
    by_category: dict[str, Decimal]


class SummaryOut(BaseModel):
    """The numbers at the top of the dashboard."""

    invoices_waiting_review: int
    documents_waiting_ocr: int
    suggestions_open: int
    open_alerts: int
    dishes_over_target: int
    dishes_incomplete: int
    wareneinsatz_this_month: Decimal | None
    wareneinsatz_last_month: Decimal | None
    latest_month: date | None
