"""Recipe costing: what a dish costs, its food-cost percentage and its margin (step 7).

    Dish cost       sum over items of quantity / (1 - waste %) x price per base unit
    Net menu price  gross menu price / (1 + VAT rate)    7 % for food, 19 % for drinks
    Food-cost %     dish cost / net menu price
    Gross margin    net menu price - dish cost

Recipes can use preparations (a house sauce, a salad topping), which have a yield: 30 g of a
sauce that yields 1,000 g costs 30/1000 of the sauce. Everything here is pure Python on
Decimals, independent of the database, so every formula is a unit test.

A missing price is never guessed: the dish is marked incomplete and lists what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RECIPE_UNITS: dict[str, tuple[Decimal, str]] = {
    "g": (Decimal(1), "g"),
    "kg": (Decimal(1000), "g"),
    "ml": (Decimal(1), "ml"),
    "cl": (Decimal(10), "ml"),
    "l": (Decimal(1000), "ml"),
    "piece": (Decimal(1), "piece"),
}
DISPLAY = {"g": (Decimal(1000), "kg"), "ml": (Decimal(1000), "l"), "piece": (Decimal(1), "piece")}


class RecipeItemSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient: str | None = None  # an ingredient key ...
    recipe: str | None = None  # ... or a preparation key, exactly one of the two
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    unit: Literal["g", "kg", "ml", "cl", "l", "piece"]
    waste_pct: Decimal = Field(default=Decimal(0), ge=0, lt=100, max_digits=5, decimal_places=2)

    @model_validator(mode="after")
    def one_source(self) -> RecipeItemSpec:
        if (self.ingredient is None) == (self.recipe is None):
            raise ValueError("an item uses either an ingredient or a recipe")
        return self


class RecipeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    kind: Literal["dish", "preparation"]
    menu_price_gross: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    vat_rate: Decimal | None = Field(default=None, ge=0, lt=100)
    target_food_cost_pct: Decimal | None = Field(default=None, gt=0, lt=100)
    yield_quantity: Decimal = Field(default=Decimal(1), gt=0)  # preparations: how much one batch makes
    yield_unit: Literal["g", "ml", "piece"] = "piece"
    items: list[RecipeItemSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def dishes_have_a_price(self) -> RecipeSpec:
        if self.kind == "dish" and (self.menu_price_gross is None or self.vat_rate is None):
            raise ValueError(f"dish {self.key!r} needs menu_price_gross and vat_rate")
        return self


def check_recipes(recipes: list[RecipeSpec], ingredient_units: dict[str, str]) -> list[str]:
    """Problems that make costing impossible: unknown keys, g for an ingredient counted in ml, cycles."""
    problems: list[str] = []
    by_key = {recipe.key: recipe for recipe in recipes}
    if len(by_key) != len(recipes):
        problems.append("duplicate recipe keys")
    for recipe in recipes:
        for item in recipe.items:
            item_base = RECIPE_UNITS[item.unit][1]
            if item.ingredient is not None:
                base = ingredient_units.get(item.ingredient)
                if base is None:
                    problems.append(f"{recipe.key}: unknown ingredient {item.ingredient!r}")
                elif base != item_base:
                    problems.append(f"{recipe.key}: {item.ingredient} is counted in {base}, not {item.unit}")
            else:
                sub = by_key.get(item.recipe or "")
                if sub is None:
                    problems.append(f"{recipe.key}: unknown recipe {item.recipe!r}")
                elif sub.kind != "preparation":
                    problems.append(f"{recipe.key}: {item.recipe} is a dish, only preparations can be used")
                elif sub.yield_unit != item_base:
                    problems.append(f"{recipe.key}: {item.recipe} yields {sub.yield_unit}, not {item.unit}")
    for recipe in recipes:  # a preparation that (indirectly) uses itself can't be costed
        if _uses(recipe.key, recipe.key, by_key, set()):
            problems.append(f"{recipe.key}: uses itself (cycle)")
    return problems


def _uses(target: str, current: str, by_key: dict[str, RecipeSpec], seen: set[str]) -> bool:
    recipe = by_key.get(current)
    if recipe is None or current in seen:
        return False
    seen.add(current)
    for item in recipe.items:
        if item.recipe == target or (item.recipe and _uses(target, item.recipe, by_key, seen)):
            return True
    return False


@dataclass(frozen=True)
class IngredientPrice:
    key: str
    name: str
    base_unit: str  # g, ml or piece
    price_per_base: Decimal  # net EUR per g, ml or piece
    supplier: str | None = None
    issue_date: date | None = None  # of the (latest) purchase it comes from

    @property
    def display(self) -> tuple[Decimal, str]:
        """Price per kg, l or piece."""
        factor, unit = DISPLAY[self.base_unit]
        return self.price_per_base * factor, unit


@dataclass
class ItemCost:
    label: str  # ingredient or preparation name
    source: str  # "ingredient" or "recipe"
    key: str
    quantity: Decimal  # in the base unit, before waste
    base_unit: str
    waste_pct: Decimal
    price_per_base: Decimal | None
    cost: Decimal | None  # None: no price known


@dataclass
class RecipeCost:
    key: str
    name: str
    kind: str
    items: list[ItemCost]
    missing: list[str] = field(default_factory=list)  # ingredients without a price
    yield_quantity: Decimal = Decimal(1)
    yield_unit: str = "piece"
    menu_price_gross: Decimal | None = None
    vat_rate: Decimal | None = None
    target_food_cost_pct: Decimal | None = None

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def cost(self) -> Decimal | None:
        """Total cost of one portion (dish) or one batch (preparation); None if a price is missing."""
        return sum((item.cost for item in self.items if item.cost is not None), Decimal(0)) if self.complete else None

    @property
    def known_cost(self) -> Decimal:
        return sum((item.cost for item in self.items if item.cost is not None), Decimal(0))

    @property
    def cost_per_yield_unit(self) -> Decimal | None:
        return self.cost / self.yield_quantity if self.cost is not None else None

    @property
    def net_price(self) -> Decimal | None:
        if self.menu_price_gross is None or self.vat_rate is None:
            return None
        return self.menu_price_gross / (1 + self.vat_rate / 100)

    @property
    def food_cost_pct(self) -> Decimal | None:
        if self.cost is None or not self.net_price:
            return None
        return self.cost / self.net_price * 100

    @property
    def margin(self) -> Decimal | None:
        if self.cost is None or self.net_price is None:
            return None
        return self.net_price - self.cost

    @property
    def over_target(self) -> bool:
        pct, target = self.food_cost_pct, self.target_food_cost_pct
        return pct is not None and target is not None and pct > target


def cost_recipes(recipes: list[RecipeSpec], prices: dict[str, IngredientPrice], names: dict[str, str] | None = None
                 ) -> dict[str, RecipeCost]:  # fmt: skip
    """Cost every recipe. Preparations are costed first and reused by the dishes that use them."""
    by_key = {recipe.key: recipe for recipe in recipes}
    names = names or {}
    done: dict[str, RecipeCost] = {}

    def cost_of(key: str) -> RecipeCost:
        if key in done:
            return done[key]
        recipe = by_key[key]
        items, missing = [], []
        for item in recipe.items:
            factor, base = RECIPE_UNITS[item.unit]
            quantity = item.quantity * factor
            gross_quantity = quantity / (1 - item.waste_pct / 100)  # what has to be bought to serve `quantity`
            if item.ingredient is not None:
                price = prices.get(item.ingredient)
                per_base = price.price_per_base if price else None
                label = price.name if price else names.get(item.ingredient, item.ingredient)
                if price is None:
                    missing.append(item.ingredient)
                items.append(ItemCost(label, "ingredient", item.ingredient, quantity, base, item.waste_pct, per_base,
                                      gross_quantity * per_base if per_base is not None else None))  # fmt: skip
            else:
                sub = cost_of(item.recipe or "")
                per_base = sub.cost_per_yield_unit
                missing.extend(m for m in sub.missing if m not in missing)
                items.append(ItemCost(sub.name, "recipe", sub.key, quantity, base, item.waste_pct, per_base,
                                      gross_quantity * per_base if per_base is not None else None))  # fmt: skip
        result = RecipeCost(
            key=recipe.key,
            name=recipe.name,
            kind=recipe.kind,
            items=items,
            missing=missing,
            yield_quantity=recipe.yield_quantity * RECIPE_UNITS[recipe.yield_unit][0],
            yield_unit=recipe.yield_unit,
            menu_price_gross=recipe.menu_price_gross,
            vat_rate=recipe.vat_rate,
            target_food_cost_pct=recipe.target_food_cost_pct,
        )
        done[key] = result
        return result

    for recipe in recipes:
        cost_of(recipe.key)
    return done


def ingredient_usage(recipes: list[RecipeSpec]) -> dict[str, dict[str, Decimal]]:
    """Per dish: how much of each ingredient one portion needs, in base units, waste included,
    with preparations broken down. Used to tell which dishes a price change affects, and by how much."""
    by_key = {recipe.key: recipe for recipe in recipes}

    def flatten(key: str, scale: Decimal) -> dict[str, Decimal]:
        usage: dict[str, Decimal] = {}
        recipe = by_key[key]
        for item in recipe.items:
            factor, _base = RECIPE_UNITS[item.unit]
            amount = item.quantity * factor / (1 - item.waste_pct / 100) * scale
            if item.ingredient is not None:
                usage[item.ingredient] = usage.get(item.ingredient, Decimal(0)) + amount
            else:
                sub = by_key[item.recipe or ""]
                share = amount / (sub.yield_quantity * RECIPE_UNITS[sub.yield_unit][0])
                for ingredient, quantity in flatten(sub.key, share).items():
                    usage[ingredient] = usage.get(ingredient, Decimal(0)) + quantity
        return usage

    return {recipe.key: flatten(recipe.key, Decimal(1)) for recipe in recipes if recipe.kind == "dish"}
