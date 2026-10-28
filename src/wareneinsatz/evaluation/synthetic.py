"""Synthetic supplier invoices with a known correct answer (ground truth) for the evaluation set.

Everything is invented: suppliers are clearly fictional ("Muster", "Beispiel"), items and prices
come from catalogue.json, and VAT IDs are generated so that they *fail* the official check digit,
which means they can never be the VAT ID of a real company.

The generator is seeded: the same seed always produces the same invoices, so evaluation results
can be reproduced and compared between extractor versions.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from importlib import resources

from stdnum.iso7064 import mod_11_10

from wareneinsatz.schemas import InvoiceData

CENT = Decimal("0.01")
BUYER = {"name": "Beispiel-Burgerbar GmbH", "street": "Beispielweg 2", "postcode": "39104", "city": "Magdeburg"}
TEST_IBAN = "DE79 0000 0000 1234 5678 90"  # valid format, no real account (also used in the KoSIT samples)
FIRST_DATE, LAST_DATE = date(2026, 1, 5), date(2026, 10, 20)


@dataclass(frozen=True)
class Layout:
    """What the renderer needs beyond the invoice data itself."""

    template: str  # "wholesaler", "cash_carry" or "receipt"
    printed_units: dict[int, str]  # line_no -> unit as printed on the document ("Krt", "kg")
    discounts: dict[int, int] = field(default_factory=dict)  # line_no -> discount in percent
    customer_number: str = ""
    receipt_number: str = ""
    receipt_time: str = ""


@dataclass(frozen=True)
class SyntheticInvoice:
    data: InvoiceData  # the ground truth
    layout: Layout


def load_catalogue() -> dict:
    text = resources.files("wareneinsatz.evaluation").joinpath("catalogue.json").read_text(encoding="utf-8")
    return json.loads(text)


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class InvoiceGenerator:
    """Creates invoices one after another; invoice numbers keep counting up per supplier."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.catalogue = load_catalogue()
        self.counters: dict[str, int] = {}
        # Each supplier keeps one invented VAT ID / tax number across all its invoices.
        self.tax_ids = {s["name"]: self._fake_tax_ids(s["name"]) for s in self.catalogue["suppliers"]}

    def generate(self, template: str | None = None) -> SyntheticInvoice:
        suppliers = [s for s in self.catalogue["suppliers"] if template in (None, s["template"])]
        supplier = self.rng.choice(suppliers)
        return self._invoice(supplier)

    # --- building one invoice ----------------------------------------------------------------------

    def _invoice(self, supplier: dict) -> SyntheticInvoice:
        rng = self.rng
        template = supplier["template"]
        is_receipt = template == "receipt"
        items = [item for item in self.catalogue["items"] if item["group"] in supplier["groups"]]
        line_count = rng.randint(2, 8) if is_receipt else rng.randint(4, 16)

        for _attempt in range(50):  # receipts stay below 250 EUR (Kleinbetragsrechnung)
            lines, printed_units, discounts = self._lines(rng.sample(items, min(line_count, len(items))), is_receipt)
            if not is_receipt or sum(line["line_amount"] for line in lines) <= 250:
                break
            line_count = max(2, line_count - 1)

        issue_date = FIRST_DATE + timedelta(days=rng.randint(0, (LAST_DATE - FIRST_DATE).days))
        vat_id, tax_number = self.tax_ids[supplier["name"]]
        data = {
            "document_type": "invoice",
            "invoice_number": self._number(supplier, issue_date),
            "issue_date": issue_date,
            "delivery_date": None if is_receipt else issue_date - timedelta(days=rng.choice([0, 0, 1, 2])),
            "currency": "EUR",
            "prices_include_vat": is_receipt,
            "supplier": {
                "name": supplier["name"],
                "vat_id": None if is_receipt else vat_id,
                "tax_number": tax_number if is_receipt else None,
                "street": supplier["street"],
                "postcode": supplier["postcode"],
                "city": supplier["city"],
                "country_code": "DE",
            },
            "lines": lines,
            **self._totals(lines, is_receipt),
        }
        layout = Layout(
            template=template,
            printed_units=printed_units,
            discounts=discounts,
            customer_number=f"{rng.randint(10000, 99999)}",
            receipt_number=f"{rng.randint(1000, 9999)}",
            receipt_time=f"{rng.randint(7, 19):02d}:{rng.randint(0, 59):02d}",
        )
        return SyntheticInvoice(InvoiceData.model_validate(data), layout)

    def _lines(self, items: list[dict], is_receipt: bool) -> tuple[list[dict], dict[int, str], dict[int, int]]:
        rng = self.rng
        lines: list[dict] = []
        printed_units: dict[int, str] = {}
        discounts: dict[int, int] = {}
        for item in items:
            quantity = self._quantity(item)
            net_price = self._price(item)
            rate = Decimal(item["vat"])
            # Till receipts print gross prices, invoices net prices.
            price = self._price_ending_in_9(net_price * (1 + rate / 100)) if is_receipt else net_price
            gross_amount = money(quantity * price)
            allowance = Decimal("0.00")
            if not is_receipt and rng.random() < 0.12:
                percent = rng.choice([3, 5, 10])
                allowance = money(gross_amount * percent / 100)
                discounts[len(lines) + 1] = percent
            lines.append(
                self._line(len(lines) + 1, item, quantity, price, gross_amount - allowance, allowance, is_receipt)
            )
            printed_units[len(lines)] = item["printed"]
            if "deposit" in item:  # Pfand: its own line, same VAT rate as the drink
                deposit = Decimal(item["deposit"])
                # "Cola 0,33 l Glas, 24 Fl." -> "Pfand Cola 0,33 l Glas" (split at ", " so "0,33" stays whole)
                deposit_item = {**item, "name": f"Pfand {item['name'].rsplit(', ', 1)[0]}"}
                lines.append(
                    self._line(
                        len(lines) + 1,
                        deposit_item,
                        quantity,
                        deposit,
                        money(quantity * deposit),
                        Decimal("0.00"),
                        is_receipt,
                        deposit=True,
                    )
                )
                printed_units[len(lines)] = item["printed"]
        return lines, printed_units, discounts

    def _line(self, line_no, item, quantity, price, amount, allowance, is_receipt, deposit=False) -> dict:
        article = None
        if not is_receipt:
            article = f"PF{self.rng.randint(1000, 9999)}" if deposit else f"{self.rng.randint(100000, 999999)}"
        return {
            "line_no": line_no,
            "description": item["name"],
            "seller_article_no": article,
            "quantity": quantity,
            "unit_code": item["unit"],
            "unit_raw": item["printed"],
            "unit_price": price,
            "allowance_amount": allowance,
            "line_amount": amount,
            "vat_rate": Decimal(item["vat"]),
        }

    def _totals(self, lines: list[dict], is_receipt: bool) -> dict:
        per_rate: dict[Decimal, Decimal] = {}
        for line in lines:
            per_rate[line["vat_rate"]] = per_rate.get(line["vat_rate"], Decimal("0")) + line["line_amount"]
        breakdown = []
        for rate, amount in sorted(per_rate.items()):
            if is_receipt:  # gross amounts: take the VAT out
                taxable = money(amount / (1 + rate / 100))
                tax = amount - taxable
            else:
                taxable, tax = amount, money(amount * rate / 100)
            breakdown.append({"vat_rate": rate, "taxable_amount": taxable, "tax_amount": tax})
        net = sum((entry["taxable_amount"] for entry in breakdown), Decimal("0"))
        vat = sum((entry["tax_amount"] for entry in breakdown), Decimal("0"))
        return {"vat_breakdown": breakdown, "net_total": net, "vat_total": vat, "gross_total": net + vat}

    # --- small helpers ------------------------------------------------------------------------------

    def _quantity(self, item: dict) -> Decimal:
        low, high = item["qty"]
        if item["step"] == "0.001":  # weighed goods: e.g. 4.255 kg
            return Decimal(str(round(self.rng.uniform(low, high), 3)))
        return Decimal(self.rng.randint(low, high))

    def _price(self, item: dict) -> Decimal:
        low, high = item["price"]
        return self._price_ending_in_9(Decimal(str(round(self.rng.uniform(low, high), 2))))

    @staticmethod
    def _price_ending_in_9(price: Decimal) -> Decimal:
        """9.83 -> 9.89, like most shelf and list prices."""
        return max(Decimal("0.09"), price.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) - CENT)

    def _number(self, supplier: dict, issue_date: date) -> str | None:
        if supplier["number"] is None:  # till receipts carry no invoice number, only a receipt number
            return None
        count = self.counters.get(supplier["name"], self.rng.randint(1000, 90000)) + self.rng.randint(1, 40)
        self.counters[supplier["name"]] = count
        return supplier["number"].format(n=count, m=self.rng.randint(1, 9999), y=issue_date.year)

    def _fake_tax_ids(self, supplier_name: str) -> tuple[str, str]:
        rng = random.Random(supplier_name)  # stable per supplier, independent of the main sequence
        digits = "".join(str(rng.randint(0, 9)) for _ in range(8))
        valid = mod_11_10.calc_check_digit(digits)
        wrong = str((int(valid) + rng.randint(1, 9)) % 10)  # deliberately wrong: can't be a real VAT ID
        tax_number = f"10{rng.randint(1, 9)}/{rng.randint(100, 999)}/{rng.randint(10000, 99999)}"
        return f"DE{digits}{wrong}", tax_number
