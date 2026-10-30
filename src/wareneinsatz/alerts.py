"""Alerts: the things a restaurant owner should hear about without looking for them (step 7).

    price_jump        an ingredient got at least 10 % more expensive at the same supplier,
                      with the dishes it affects and by how much per portion
    cheaper_supplier  another supplier sold the same ingredient at least 5 % cheaper recently
    over_target       a dish's food-cost percentage is above its target

Only food ingredients are checked: deposits (Pfand) differ per crate and keg, and napkins are not
food cost. The rules are pure functions over purchases and recipe costs. Each alert has a dedupe key, so
running the check again (after every import) never repeats an alert; the table keeps whether
someone has acknowledged it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from wareneinsatz.costing import DISPLAY, RecipeCost
from wareneinsatz.models import Alert
from wareneinsatz.pricing import Purchase

PRICE_JUMP_PCT = Decimal(10)
CHEAPER_SUPPLIER_PCT = Decimal(5)
LOOKBACK_DAYS = 90  # older price changes are history, not news


@dataclass(frozen=True)
class AlertDraft:
    kind: str
    subject: str  # ingredient or recipe key
    message: str
    dedupe_key: str
    happened_on: date
    details: dict[str, Any] = field(default_factory=dict)


def _display(price_per_base: Decimal, base_unit: str) -> str:
    factor, unit = DISPLAY[base_unit]
    return f"{price_per_base * factor:.2f} EUR/{unit}"


def price_jumps(
    purchases: list[Purchase],
    usage: dict[str, dict[str, Decimal]],
    dish_names: dict[str, str],
    *,
    threshold_pct: Decimal = PRICE_JUMP_PCT,
    as_of: date | None = None,
) -> list[AlertDraft]:
    """Each purchase compared with the previous one of the same ingredient at the same supplier."""
    if not purchases:
        return []
    as_of = as_of or purchases[-1].issue_date
    purchases = [p for p in purchases if p.is_food]
    previous: dict[tuple[str, int], Purchase] = {}
    drafts = []
    for p in purchases:  # in date order
        key = (p.ingredient_key, p.supplier_id)
        before = previous.get(key)
        previous[key] = p
        if before is None or p.issue_date < as_of - timedelta(days=LOOKBACK_DAYS) or p.issue_date > as_of:
            continue
        old, new = before.price_per_base, p.price_per_base
        change = (new - old) / old * 100
        if change < threshold_pct:
            continue
        affected = sorted(
            ((dish, quantities[p.ingredient_key] * (new - old)) for dish, quantities in usage.items()
             if p.ingredient_key in quantities),
            key=lambda item: -item[1],
        )  # fmt: skip
        impact = ", ".join(f"{dish_names.get(dish, dish)} +{delta:.2f} EUR" for dish, delta in affected[:4])
        drafts.append(
            AlertDraft(
                kind="price_jump",
                subject=p.ingredient_key,
                message=(
                    f"{p.ingredient_name}: {_display(old, p.base_unit)} -> {_display(new, p.base_unit)} "
                    f"(+{change:.1f} %) at {p.supplier_name} on {p.issue_date}"
                    + (f"; per portion: {impact}" if impact else "")
                ),
                dedupe_key=f"price_jump:{p.ingredient_key}:{p.line_id}",
                happened_on=p.issue_date,
                details={
                    "supplier": p.supplier_name,
                    "old_price_per_base": str(old),
                    "new_price_per_base": str(new),
                    "change_pct": str(round(change, 2)),
                    "dishes": {dish: str(round(delta, 4)) for dish, delta in affected},
                },
            )
        )
    return drafts


def cheaper_suppliers(
    purchases: list[Purchase],
    *,
    threshold_pct: Decimal = CHEAPER_SUPPLIER_PCT,
    window_days: int = 60,
    as_of: date | None = None,
) -> list[AlertDraft]:
    """For each ingredient: is the supplier of the latest purchase clearly dearer than another one's latest price?"""
    if not purchases:
        return []
    as_of = as_of or purchases[-1].issue_date
    recent = [p for p in purchases if p.is_food and as_of - timedelta(days=window_days) < p.issue_date <= as_of]
    latest_per_supplier: dict[str, dict[int, Purchase]] = {}
    last_bought: dict[str, Purchase] = {}
    for p in recent:
        latest_per_supplier.setdefault(p.ingredient_key, {})[p.supplier_id] = p
        last_bought[p.ingredient_key] = p
    drafts = []
    for key, bought in last_bought.items():
        others = [p for sid, p in latest_per_supplier[key].items() if sid != bought.supplier_id]
        if not others:
            continue
        cheapest = min(others, key=lambda p: p.price_per_base)
        saving = (bought.price_per_base - cheapest.price_per_base) / bought.price_per_base * 100
        if saving < threshold_pct:
            continue
        drafts.append(
            AlertDraft(
                kind="cheaper_supplier",
                subject=key,
                message=(
                    f"{bought.ingredient_name}: {cheapest.supplier_name} sold it for "
                    f"{_display(cheapest.price_per_base, bought.base_unit)} on {cheapest.issue_date}, "
                    f"{saving:.1f} % less than the last purchase at {bought.supplier_name} "
                    f"({_display(bought.price_per_base, bought.base_unit)})"
                ),
                dedupe_key=f"cheaper_supplier:{key}:{bought.line_id}:{cheapest.supplier_id}",
                happened_on=bought.issue_date,
                details={
                    "bought_from": bought.supplier_name,
                    "cheaper": cheapest.supplier_name,
                    "saving_pct": str(round(saving, 2)),
                },  # fmt: skip
            )
        )
    return drafts


def over_target(costs: dict[str, RecipeCost], *, as_of: date) -> list[AlertDraft]:
    """Dishes above their food-cost target; one alert per dish and month."""
    drafts = []
    for cost in costs.values():
        if cost.kind != "dish" or not cost.over_target:
            continue
        drafts.append(
            AlertDraft(
                kind="over_target",
                subject=cost.key,
                message=(
                    f"{cost.name}: food cost {cost.food_cost_pct:.1f} % is above the target of "
                    f"{cost.target_food_cost_pct:.0f} % (cost {cost.cost:.2f} EUR, net price {cost.net_price:.2f} EUR)"
                ),
                dedupe_key=f"over_target:{cost.key}:{as_of:%Y-%m}",
                happened_on=as_of,
                details={
                    "food_cost_pct": str(round(cost.food_cost_pct or 0, 2)),
                    "cost": str(round(cost.cost or 0, 4)),
                },  # fmt: skip
            )
        )
    return drafts


def record_alerts(session: Session, drafts: list[AlertDraft]) -> int:
    """Store new alerts; ones seen before (same dedupe key) are skipped. Returns how many are new."""
    new = 0
    for draft in drafts:
        statement = (
            insert(Alert)
            .values(
                kind=draft.kind,
                subject=draft.subject,
                message=draft.message,
                dedupe_key=draft.dedupe_key,
                happened_on=draft.happened_on,
                details=draft.details,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(Alert.id)  # a row comes back only if it was really inserted
        )
        new += len(session.execute(statement).all())
    session.flush()
    return new


def open_alerts(session: Session) -> list[Alert]:
    query = select(Alert).where(Alert.acknowledged_at.is_(None)).order_by(Alert.happened_on.desc(), Alert.id.desc())
    return list(session.scalars(query))
