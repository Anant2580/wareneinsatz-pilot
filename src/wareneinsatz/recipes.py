"""Recipes in the database, menu costing, alert checks and the monthly Wareneinsatz (step 7).

load_recipes(session, Path("data/recipes.json"))     insert or replace recipes by key
cost_menu(session, method="latest")                  RecipeCost per recipe, with today's prices
check_alerts(session)                                store new price/supplier/target alerts
monthly_purchases(session)                           net food purchases per month
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from wareneinsatz.alerts import cheaper_suppliers, over_target, price_jumps, record_alerts
from wareneinsatz.costing import RecipeCost, RecipeItemSpec, RecipeSpec, check_recipes, cost_recipes, ingredient_usage
from wareneinsatz.models import Ingredient, Recipe, RecipeItem
from wareneinsatz.pricing import load_purchases, prices_for

DEFAULT_RECIPES = Path("data/recipes.json")


class RecipeError(ValueError):
    pass


def read_recipe_file(path: Path = DEFAULT_RECIPES) -> list[RecipeSpec]:
    return [RecipeSpec.model_validate(raw) for raw in json.loads(path.read_text(encoding="utf-8"))["recipes"]]


def load_recipes(session: Session, path: Path = DEFAULT_RECIPES) -> tuple[int, int]:
    """Insert or update recipes by key; their items are replaced. Returns (added, updated)."""
    specs = read_recipe_file(path)
    ingredients = {i.key: i for i in session.scalars(select(Ingredient))}
    existing_specs = {spec.key: spec for spec in read_recipes(session)}
    all_specs = list({**existing_specs, **{spec.key: spec for spec in specs}}.values())
    problems = check_recipes(all_specs, {key: i.base_unit for key, i in ingredients.items()})
    if problems:
        raise RecipeError("; ".join(problems))

    rows = {r.key: r for r in session.scalars(select(Recipe))}
    added = updated = 0
    for spec in specs:  # first the recipes themselves, so preparations exist before items point to them
        fields = spec.model_dump(exclude={"items"})
        row = rows.get(spec.key)
        if row is None:
            row = Recipe(**fields)
            session.add(row)
            rows[spec.key] = row
            added += 1
        else:
            for name, value in fields.items():
                setattr(row, name, value)
            row.items.clear()
            updated += 1
    session.flush()
    for spec in specs:
        row = rows[spec.key]
        for position, item in enumerate(spec.items, start=1):
            row.items.append(
                RecipeItem(
                    position=position,
                    ingredient=ingredients[item.ingredient] if item.ingredient else None,
                    sub_recipe=rows[item.recipe] if item.recipe else None,
                    quantity=item.quantity,
                    unit=item.unit,
                    waste_pct=item.waste_pct,
                )
            )
    session.flush()
    return added, updated


def read_recipes(session: Session) -> list[RecipeSpec]:
    specs = []
    for recipe in session.scalars(select(Recipe).order_by(Recipe.kind.desc(), Recipe.key)):
        items = [
            RecipeItemSpec(
                ingredient=item.ingredient.key if item.ingredient else None,
                recipe=item.sub_recipe.key if item.sub_recipe else None,
                quantity=item.quantity,
                unit=item.unit,
                waste_pct=item.waste_pct,
            )
            for item in recipe.items
        ]
        specs.append(
            RecipeSpec(
                key=recipe.key,
                name=recipe.name,
                kind=recipe.kind,
                menu_price_gross=recipe.menu_price_gross,
                vat_rate=recipe.vat_rate,
                target_food_cost_pct=recipe.target_food_cost_pct,
                yield_quantity=recipe.yield_quantity,
                yield_unit=recipe.yield_unit,
                items=items,
            )
        )
    return specs


def cost_menu(session: Session, *, method: str = "latest", until: date | None = None) -> dict[str, RecipeCost]:
    """Every recipe costed with the prices from approved purchases up to `until` (default: all)."""
    recipes = read_recipes(session)
    prices = prices_for(load_purchases(session, until=until), method)
    names = {i.key: i.name for i in session.scalars(select(Ingredient))}
    return cost_recipes(recipes, prices, names)


@dataclass(frozen=True)
class AlertCheck:
    new: int
    found: int


def check_alerts(
    session: Session,
    *,
    price_jump_pct: Decimal = Decimal(10),
    cheaper_supplier_pct: Decimal = Decimal(5),
    as_of: date | None = None,
) -> AlertCheck:
    purchases = load_purchases(session, until=as_of)
    if not purchases:
        return AlertCheck(0, 0)
    as_of = as_of or purchases[-1].issue_date
    recipes = read_recipes(session)
    names = {spec.key: spec.name for spec in recipes}
    drafts = price_jumps(purchases, ingredient_usage(recipes), names, threshold_pct=price_jump_pct, as_of=as_of)
    drafts += cheaper_suppliers(purchases, threshold_pct=cheaper_supplier_pct, as_of=as_of)
    drafts += over_target(cost_menu(session, until=as_of), as_of=as_of)
    return AlertCheck(new=record_alerts(session, drafts), found=len(drafts))


@dataclass(frozen=True)
class MonthlyPurchases:
    month: date
    food: Decimal  # Wareneinsatz: net purchases of food ingredients (credit notes subtracted)
    non_food: Decimal  # deposits, napkins, cleaning
    unmatched: Decimal  # lines not (yet) confirmed: not counted above
    by_category: dict[str, Decimal]


def monthly_purchases(session: Session, *, months: int = 12) -> list[MonthlyPurchases]:
    """Net purchases per month from approved invoices (view v_purchase_lines), newest month last."""
    rows = session.execute(
        text(
            "SELECT month, is_food, category, sum(net_amount) FROM v_purchase_lines "
            "GROUP BY month, is_food, category ORDER BY month"
        )
    ).all()
    result: dict[date, MonthlyPurchases] = {}
    for month, is_food, category, amount in rows:
        entry = result.setdefault(month, MonthlyPurchases(month, Decimal(0), Decimal(0), Decimal(0), {}))
        amount = Decimal(amount)
        if is_food is None:
            entry = MonthlyPurchases(month, entry.food, entry.non_food, entry.unmatched + amount, entry.by_category)
        elif is_food:
            categories = {**entry.by_category, category or "-": entry.by_category.get(category or "-", 0) + amount}
            entry = MonthlyPurchases(month, entry.food + amount, entry.non_food, entry.unmatched, categories)
        else:
            entry = MonthlyPurchases(month, entry.food, entry.non_food + amount, entry.unmatched, entry.by_category)
        result[month] = entry
    return list(result.values())[-months:]
