"""Ingredient prices for costing, from the purchase history (view v_ingredient_prices).

Two ways to price an ingredient:

    latest   the most recent purchase: what the next portion will really cost (default)
    average  quantity-weighted average over the last 90 days: smooths out single offers

Both only use confirmed lines of approved invoices (see migration 0004).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from wareneinsatz.costing import IngredientPrice


@dataclass(frozen=True)
class Purchase:
    line_id: int
    issue_date: date
    supplier_id: int
    supplier_name: str
    ingredient_key: str
    ingredient_name: str
    base_unit: str
    is_food: bool
    base_quantity: Decimal
    net_amount: Decimal

    @property
    def price_per_base(self) -> Decimal:
        return self.net_amount / self.base_quantity


def load_purchases(session: Session, *, until: date | None = None) -> list[Purchase]:
    sql = (
        "SELECT invoice_line_id, issue_date, supplier_id, supplier_name, ingredient_key, ingredient_name, base_unit, "
        "is_food, base_quantity, net_amount FROM v_ingredient_prices"
    )
    params = {}
    if until is not None:
        sql += " WHERE issue_date <= :until"
        params["until"] = until
    sql += " ORDER BY issue_date, invoice_line_id"
    return [Purchase(*row) for row in session.execute(text(sql), params)]


def latest_prices(purchases: Iterable[Purchase]) -> dict[str, IngredientPrice]:
    """The most recent purchase of each ingredient (the list is in date order, so the last one wins)."""
    prices: dict[str, IngredientPrice] = {}
    for p in purchases:
        prices[p.ingredient_key] = IngredientPrice(
            p.ingredient_key, p.ingredient_name, p.base_unit, p.price_per_base, p.supplier_name, p.issue_date
        )
    return prices


def average_prices(purchases: Iterable[Purchase], *, days: int = 90) -> dict[str, IngredientPrice]:
    """Quantity-weighted average over the `days` before each ingredient's latest purchase."""
    by_ingredient: dict[str, list[Purchase]] = {}
    for p in purchases:
        by_ingredient.setdefault(p.ingredient_key, []).append(p)
    prices = {}
    for key, rows in by_ingredient.items():
        last = rows[-1]
        window = [p for p in rows if p.issue_date > last.issue_date - timedelta(days=days)]
        amount = sum((p.net_amount for p in window), Decimal(0))
        quantity = sum((p.base_quantity for p in window), Decimal(0))
        prices[key] = IngredientPrice(key, last.ingredient_name, last.base_unit, amount / quantity,
                                      f"average of {len(window)} purchase(s)", last.issue_date)  # fmt: skip
    return prices


def prices_for(purchases: list[Purchase], method: str = "latest") -> dict[str, IngredientPrice]:
    if method == "latest":
        return latest_prices(purchases)
    if method == "average":
        return average_prices(purchases)
    raise ValueError(f"Unknown pricing method {method!r}: latest or average")
