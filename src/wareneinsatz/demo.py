"""Demo data: six months of purchases of a fictional burger restaurant (May to October 2026).

Five fictional suppliers deliver on fixed weekdays, with stable article numbers and prices that
drift a little every month, like real list prices. Three events are built in, so the alerts
and the dashboard have something to show:

    from 14 Sep  beef patties +15 % at every supplier (the "beef got expensive" story)
    from  3 Aug  rapeseed oil +9 %
    always       the butcher sells meat about 6 % cheaper than the wholesaler

The invoices are stored as approved (approved_by 'demo') and every line is confirmed with its
correct ingredient, as a careful reviewer would. Generated with a seed, so it is the same
every time. Never mix it with a real restaurant's database.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from wareneinsatz.evaluation.synthetic import InvoiceGenerator, load_catalogue, money
from wareneinsatz.matching.service import confirm_line
from wareneinsatz.models import Document, Invoice, InvoiceLine
from wareneinsatz.repository import (
    DocumentMeta,
    DuplicateDocumentError,
    DuplicateInvoiceError,
    record_validation,
    save_invoice,
)
from wareneinsatz.schemas import InvoiceData

START = date(2026, 5, 4)  # a Monday


@dataclass(frozen=True)
class DemoSupplier:
    name: str
    weekday: int  # 0 = Monday
    every_weeks: int
    groups: tuple[str, ...]
    price_factor: Decimal  # price level compared with the catalogue's middle price


SUPPLIERS = [
    DemoSupplier("Musterfrisch Großhandel GmbH", 1, 1, ("bakery", "dairy", "frozen", "dry", "meat", "veggie"),
                 Decimal("1.00")),
    DemoSupplier("Fleischerei Beispiel e.K.", 4, 1, ("meat",), Decimal("0.94")),
    DemoSupplier("Gemüsehof Muster", 3, 1, ("produce",), Decimal("0.95")),
    DemoSupplier("Muster Cash & Carry GmbH", 0, 2, ("nonfood", "dry"), Decimal("0.98")),
    DemoSupplier("Getränke Beispiel KG", 2, 1, ("drinks",), Decimal("1.00")),
]  # fmt: skip

EVENTS = [  # (ingredient key, from date, price factor)
    ("patty-rind-180", date(2026, 9, 14), Decimal("1.15")),
    ("rapsoel", date(2026, 8, 3), Decimal("1.09")),
]


class DemoGenerator(InvoiceGenerator):
    def __init__(self, seed: int = 2026):
        super().__init__(seed)
        self.suppliers = {s["name"]: s for s in self.catalogue["suppliers"]}
        self.articles: dict[tuple[str, str], str] = {}
        self.drift: dict[tuple[str, str, int], Decimal] = {}

    def invoices(self, weeks: int = 26) -> list[InvoiceData]:
        result = []
        for week in range(weeks):
            for supplier in SUPPLIERS:
                if week % supplier.every_weeks:
                    continue
                day = START + timedelta(weeks=week, days=supplier.weekday)
                result.append(self._delivery(supplier, day))
        return sorted(result, key=lambda invoice: (invoice.issue_date, invoice.supplier.name))

    def _price(self, supplier: DemoSupplier, item: dict, day: date) -> Decimal:
        low, high = item["price"]
        base = Decimal(str((low + high) / 2)) * supplier.price_factor
        month = (day.year - START.year) * 12 + day.month - START.month
        factor = Decimal(1)
        for m in range(month + 1):  # a slow random walk of about 1 % per month, per supplier and item
            key = (supplier.name, item["name"], m)
            if key not in self.drift:
                self.drift[key] = Decimal(str(round(self.rng.gauss(0, 0.01), 4)))
            factor *= 1 + self.drift[key]
        for ingredient, since, jump in EVENTS:
            if item["ingredient"] == ingredient and day >= since:
                factor *= jump
        return self._price_ending_in_9(base * factor)

    def _delivery(self, supplier: DemoSupplier, day: date) -> InvoiceData:
        rng = self.rng
        items = [item for item in self.catalogue["items"] if item["group"] in supplier.groups]
        ordered = [item for item in items if rng.random() < 0.55] or items[:1]
        lines: list[dict] = []
        for item in ordered:
            quantity = self._quantity(item)
            price = self._price(supplier, item, day)
            lines.append(self._demo_line(len(lines) + 1, supplier, item, quantity, price))
            if "deposit" in item:
                deposit_item = {**item, "name": f"Pfand {item['name'].rsplit(', ', 1)[0]}", "ingredient": "pfand"}
                lines.append(
                    self._demo_line(len(lines) + 1, supplier, deposit_item, quantity, Decimal(item["deposit"]))
                )
        catalogue_supplier = self.suppliers[supplier.name]
        vat_id, _tax_number = self.tax_ids[supplier.name]
        return InvoiceData.model_validate(
            {
                "invoice_number": self._number(catalogue_supplier, day),
                "issue_date": day,
                "delivery_date": day,
                "supplier": {"name": supplier.name, "vat_id": vat_id, "street": catalogue_supplier["street"],
                             "postcode": catalogue_supplier["postcode"], "city": catalogue_supplier["city"]},
                "lines": lines,
                **self._totals(lines, False),
            }
        )  # fmt: skip

    def _demo_line(self, line_no: int, supplier: DemoSupplier, item: dict, quantity: Decimal, price: Decimal) -> dict:
        key = (supplier.name, item["name"])
        if key not in self.articles:  # stable article numbers, like a real supplier's
            self.articles[key] = f"{random.Random(f'{key}').randint(100000, 999999)}"
        return {
            "line_no": line_no,
            "description": item["name"],
            "seller_article_no": self.articles[key],
            "quantity": quantity,
            "unit_code": item["unit"],
            "unit_raw": item["printed"],
            "unit_price": price,
            "line_amount": money(quantity * price),
            "vat_rate": Decimal(item["vat"]),
        }


@lru_cache
def _catalogue_ingredients() -> dict[str, str]:
    return {item["name"]: item["ingredient"] for item in load_catalogue()["items"]}


def truth_for(description: str) -> str:
    """The right ingredient of a demo line (from the catalogue)."""
    return "pfand" if description.startswith("Pfand ") else _catalogue_ingredients()[description]


@dataclass(frozen=True)
class DemoResult:
    invoices: int
    skipped: int
    lines: int
    confirmations: int


def load_demo(session: Session, *, weeks: int = 26, seed: int = 2026) -> DemoResult:
    """Store the demo invoices as approved and confirm every line (needs the ingredients loaded)."""
    stored = skipped = 0
    for data in DemoGenerator(seed).invoices(weeks):
        content = data.model_dump_json().encode()
        meta = DocumentMeta(
            sha256=hashlib.sha256(content).hexdigest(),
            filename=f"demo-{data.invoice_number}.json",
            media_type="application/json",
            size_bytes=len(content),
            extraction_method="demo",
        )
        try:
            invoice = save_invoice(session, meta, data)
        except (DuplicateDocumentError, DuplicateInvoiceError):
            skipped += 1  # loaded before
            continue
        record_validation(session, invoice, [], approve=True, approved_by="demo")
        stored += 1

    confirmations = lines = 0
    names = {
        line_id: description
        for line_id, description in session.execute(
            select(InvoiceLine.id, InvoiceLine.description)
            .join(Invoice)
            .join(Document)
            .where(InvoiceLine.match_status.is_(None), Document.extraction_method == "demo")  # only demo lines
        )
    }
    done: set[int] = set()
    for line_id, description in names.items():
        lines += 1
        if line_id in done:
            continue
        _product, confirmed = confirm_line(session, line_id, ingredient_key=truth_for(description), confirmed_by="demo")
        done.update(confirmed)
        confirmations += 1
    return DemoResult(stored, skipped, lines, confirmations)
