"""Tests for step 7: recipe costing, prices, alerts, monthly Wareneinsatz and the demo data.

The costing numbers are worked out by hand in the comments, so every formula is checked
against arithmetic, not against itself.
"""

import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

from wareneinsatz.alerts import cheaper_suppliers, over_target, price_jumps
from wareneinsatz.cli_costing import run_alerts, run_cost, run_demo, run_purchases, run_recipes
from wareneinsatz.costing import (
    IngredientPrice,
    RecipeSpec,
    check_recipes,
    cost_recipes,
    ingredient_usage,
)
from wareneinsatz.demo import DemoGenerator, load_demo, truth_for
from wareneinsatz.matching.service import load_ingredients
from wareneinsatz.models import Alert, Invoice
from wareneinsatz.pricing import Purchase, average_prices, latest_prices
from wareneinsatz.recipes import RecipeError, check_alerts, cost_menu, load_recipes, monthly_purchases, read_recipe_file

ROOT = Path(__file__).resolve().parents[1]
INGREDIENTS = ROOT / "data" / "ingredients.json"
RECIPES = ROOT / "data" / "recipes.json"


def price(key: str, base: str, per_base: str) -> IngredientPrice:
    return IngredientPrice(key, key.title(), base, Decimal(per_base))


SAUCE = {"key": "sauce", "name": "Sauce", "kind": "preparation", "yield_quantity": 1000, "yield_unit": "g",
         "items": [{"ingredient": "mayo", "quantity": 800, "unit": "ml"}, {"ingredient": "ketchup", "quantity": 0.2,
                                                                          "unit": "kg"}]}  # fmt: skip
BURGER = {"key": "burger", "name": "Burger", "kind": "dish", "menu_price_gross": "10.70", "vat_rate": 7,
          "target_food_cost_pct": 30, "items": [
              {"ingredient": "bun", "quantity": 1, "unit": "piece"},
              {"ingredient": "patty", "quantity": 1, "unit": "piece"},
              {"ingredient": "tomato", "quantity": 36, "unit": "g", "waste_pct": 10},
              {"recipe": "sauce", "quantity": 25, "unit": "g"}]}  # fmt: skip
PRICES = {
    "bun": price("bun", "piece", "0.50"),
    "patty": price("patty", "piece", "1.50"),
    "tomato": price("tomato", "g", "0.004"),  # 4 EUR/kg
    "mayo": price("mayo", "ml", "0.003"),  # 3 EUR/l
    "ketchup": price("ketchup", "g", "0.002"),  # 2 EUR/kg
}


def test_costing_by_hand():
    recipes = [RecipeSpec.model_validate(SAUCE), RecipeSpec.model_validate(BURGER)]
    costs = cost_recipes(recipes, PRICES)
    sauce, burger = costs["sauce"], costs["burger"]
    # Sauce: 800 ml x 0.003 + 200 g x 0.002 = 2.40 + 0.40 = 2.80 EUR for 1000 g
    assert sauce.cost == Decimal("2.80") and sauce.cost_per_yield_unit == Decimal("0.0028")
    # Tomato: 36 g served, 10 % waste -> 40 g bought x 0.004 = 0.16; sauce: 25 g x 0.0028 = 0.07
    # Burger: 0.50 + 1.50 + 0.16 + 0.07 = 2.23 EUR
    assert burger.cost == Decimal("2.23")
    assert burger.net_price == Decimal("10.00")  # 10.70 / 1.07
    assert burger.food_cost_pct == Decimal("22.3")  # 2.23 / 10.00
    assert burger.margin == Decimal("7.77")
    assert not burger.over_target


def test_a_missing_price_is_never_guessed():
    recipes = [RecipeSpec.model_validate(SAUCE), RecipeSpec.model_validate(BURGER)]
    costs = cost_recipes(recipes, {k: v for k, v in PRICES.items() if k != "ketchup"})
    burger = costs["burger"]
    assert burger.missing == ["ketchup"]  # via the sauce
    assert burger.cost is None and burger.food_cost_pct is None and not burger.complete
    assert burger.known_cost == Decimal("2.16")  # what is known: bun, patty, tomato


def test_over_target_and_usage():
    expensive = {**PRICES, "patty": price("patty", "piece", "3.00")}
    recipes = [RecipeSpec.model_validate(SAUCE), RecipeSpec.model_validate(BURGER)]
    burger = cost_recipes(recipes, expensive)["burger"]
    assert burger.food_cost_pct == Decimal("37.3") and burger.over_target
    usage = ingredient_usage(recipes)["burger"]
    assert usage["tomato"] == Decimal(40)  # waste included
    assert usage["mayo"] == Decimal(20)  # 25 g of 1000 g sauce = 2.5 % of 800 ml
    assert usage["ketchup"] == Decimal(5)


def test_recipe_problems_are_found():
    units = {"bun": "piece", "patty": "piece", "tomato": "g", "mayo": "ml", "ketchup": "g"}
    good = [RecipeSpec.model_validate(SAUCE), RecipeSpec.model_validate(BURGER)]
    assert check_recipes(good, units) == []
    wrong_unit = RecipeSpec.model_validate({**SAUCE, "items": [{"ingredient": "mayo", "quantity": 1, "unit": "kg"}]})
    loop = RecipeSpec.model_validate({**SAUCE, "items": [{"recipe": "sauce", "quantity": 1, "unit": "g"}]})
    problems = check_recipes([wrong_unit, RecipeSpec.model_validate(BURGER)], units) + check_recipes([loop], units)
    assert "sauce: mayo is counted in ml, not kg" in problems
    assert "sauce: uses itself (cycle)" in problems
    assert check_recipes([RecipeSpec.model_validate(BURGER)], units) == ["burger: unknown recipe 'sauce'"]
    with pytest.raises(ValueError, match="needs menu_price_gross"):
        RecipeSpec.model_validate({**BURGER, "menu_price_gross": None})
    with pytest.raises(ValueError, match="either an ingredient or a recipe"):
        RecipeSpec.model_validate({**SAUCE, "items": [{"quantity": 1, "unit": "g"}]})


def test_the_example_menu_is_consistent():
    recipes = read_recipe_file(RECIPES)
    units = {i["key"]: i["base_unit"] for i in json.loads(INGREDIENTS.read_text())["ingredients"]}
    assert check_recipes(recipes, units) == []
    assert sum(r.kind == "dish" for r in recipes) == 12


# --- prices and alerts ---------------------------------------------------------------------------------


def purchase(line: int, day: str, supplier: int, key: str, quantity: str, amount: str, *, food=True) -> Purchase:
    return Purchase(line, date.fromisoformat(day), supplier, f"Supplier {supplier}", key, key.title(), "piece", food,
                    Decimal(quantity), Decimal(amount))  # fmt: skip


HISTORY = [
    purchase(1, "2026-08-01", 1, "patty", "40", "56.00"),  # 1.40 per piece
    purchase(2, "2026-08-20", 2, "patty", "80", "104.00"),  # 1.30 at the butcher
    purchase(3, "2026-09-15", 1, "patty", "40", "64.00"),  # 1.60: +14.3 % at supplier 1
    purchase(4, "2026-09-16", 9, "pfand", "10", "300.00", food=False),
]


def test_latest_and_average_prices():
    assert latest_prices(HISTORY)["patty"].price_per_base == Decimal("1.60")
    # 90-day quantity-weighted average: (56 + 104 + 64) / (40 + 80 + 40) = 1.40
    average = average_prices(HISTORY)["patty"]
    assert average.price_per_base == Decimal("1.40") and average.supplier == "average of 3 purchase(s)"


def test_price_jump_alert_names_the_dishes():
    usage = {"burger": {"patty": Decimal(1)}, "fries": {"potato": Decimal(200)}}
    [alert] = price_jumps(HISTORY, usage, {"burger": "Classic Burger"})
    assert alert.kind == "price_jump" and alert.subject == "patty"
    assert "1.40 EUR/piece -> 1.60 EUR/piece (+14.3 %) at Supplier 1" in alert.message
    assert "Classic Burger +0.20 EUR" in alert.message
    assert alert.dedupe_key == "price_jump:patty:3"
    assert price_jumps(HISTORY, usage, {}, threshold_pct=Decimal(20)) == []


def test_cheaper_supplier_alert():
    [alert] = cheaper_suppliers(HISTORY)
    assert "Supplier 2 sold it for 1.30 EUR/piece" in alert.message and "18.8 % less" in alert.message


def test_over_target_alert():
    recipes = [RecipeSpec.model_validate(SAUCE), RecipeSpec.model_validate(BURGER)]
    costs = cost_recipes(recipes, {**PRICES, "patty": price("patty", "piece", "3.00")})
    [alert] = over_target(costs, as_of=date(2026, 10, 30))
    assert alert.dedupe_key == "over_target:burger:2026-10" and "37.3 %" in alert.message


# --- demo data -----------------------------------------------------------------------------------------


def test_demo_data_is_reproducible_and_tells_the_story():
    first, again = DemoGenerator(2026).invoices(26), DemoGenerator(2026).invoices(26)
    assert first == again and len(first) == 117
    patties = [
        (inv.issue_date, line.unit_price)
        for inv in first
        if inv.supplier.name.startswith("Musterfrisch")
        for line in inv.lines
        if line.description.startswith("Burger Patty")
    ]
    before = [p for d, p in patties if d < date(2026, 9, 14)][-1]
    after = [p for d, p in patties if d >= date(2026, 9, 14)][0]
    assert after / before > Decimal("1.10")  # the beef price jump
    assert truth_for("Pfand Cola 0,33 l Glas") == "pfand" and truth_for("Rapsöl 10 l Kanister") == "rapsoel"


# --- database ------------------------------------------------------------------------------------------


@pytest.fixture
def kitchen(session):
    load_ingredients(session, INGREDIENTS)
    load_recipes(session, RECIPES)
    return session


def test_recipes_load_and_reload(kitchen):
    assert load_recipes(kitchen, RECIPES) == (0, 14)
    costs = cost_menu(kitchen)
    assert costs["classic-burger"].missing  # no purchases yet: no prices


def test_recipes_with_problems_are_not_loaded(kitchen, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"recipes": [{**SAUCE, "key": "x", "items": [
        {"ingredient": "kaviar", "quantity": 1, "unit": "g"}]}]}))  # fmt: skip
    with pytest.raises(RecipeError, match="unknown ingredient 'kaviar'"):
        load_recipes(kitchen, broken)


def test_demo_costing_alerts_and_purchases(kitchen):
    result = load_demo(kitchen)
    assert (result.invoices, result.lines) == (117, 1013)
    costs = cost_menu(kitchen)
    classic = costs["classic-burger"]
    assert classic.complete and Decimal(15) < classic.food_cost_pct < Decimal(35)
    # before the beef price jump the classic burger was cheaper
    assert cost_menu(kitchen, until=date(2026, 9, 1))["classic-burger"].cost < classic.cost

    check = check_alerts(kitchen)
    kinds = {a.kind for a in kitchen.scalars(select(Alert))}
    assert check.new > 0 and {"price_jump", "cheaper_supplier"} <= kinds
    assert any(a.subject == "patty-rind-180" and a.kind == "price_jump" for a in kitchen.scalars(select(Alert)))
    assert not any(a.subject == "pfand" for a in kitchen.scalars(select(Alert)))  # deposits are not food cost
    assert check_alerts(kitchen).new == 0  # never twice

    months = monthly_purchases(kitchen)
    assert [m.month for m in months][0] == date(2026, 5, 1) and len(months) == 6
    assert all(m.food > 0 and m.unmatched == 0 for m in months)
    assert load_demo(kitchen).skipped == 117  # loading again changes nothing


def test_credit_notes_are_subtracted(kitchen):
    load_demo(kitchen, weeks=1)
    before = monthly_purchases(kitchen)[0].food
    invoice = kitchen.scalars(select(Invoice)).first()
    kitchen.execute(text(f"UPDATE invoices SET document_type = 'credit_note' WHERE id = {invoice.id}"))
    food_lines = sum(line.line_amount for line in invoice.lines if line.ingredient and line.ingredient.is_food)
    assert monthly_purchases(kitchen)[0].food == before - 2 * food_lines


# --- command line --------------------------------------------------------------------------------------


@pytest.fixture
def clean_database(engine):
    yield engine
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE alerts, recipe_items, recipes, invoice_lines, invoice_tax_totals, invoices, documents, "
                "supplier_products, ingredients, suppliers CASCADE"
            )  # fmt: skip
        )


def test_cli_step_7(clean_database):
    from wareneinsatz.cli_matching import run_ingredients

    engine = clean_database
    out = io.StringIO()
    assert run_recipes("load", RECIPES, engine=engine, out=out) == 2  # ingredients first
    assert "ingredients load" in out.getvalue()
    run_ingredients("load", INGREDIENTS, engine=engine, out=io.StringIO())
    assert run_recipes("load", RECIPES, engine=engine, out=io.StringIO()) == 0
    out = io.StringIO()
    assert run_cost(None, engine=engine, out=out) == 0
    assert "no price yet" in out.getvalue()

    out = io.StringIO()
    assert run_demo(weeks=26, engine=engine, out=out) == 0
    assert out.getvalue().startswith("Demo data: 117 invoice(s)")
    out = io.StringIO()
    assert run_cost(None, engine=engine, out=out) == 0
    assert "Classic Burger" in out.getvalue() and "no price yet" not in out.getvalue()
    out = io.StringIO()
    assert run_cost("classic-burger", engine=engine, out=out) == 0
    assert "Burger-Sauce (house)" in out.getvalue() and "= cost" in out.getvalue()
    assert run_cost("pizza", engine=engine, out=io.StringIO()) == 2

    out = io.StringIO()
    assert run_alerts(engine=engine, out=out) == 0
    first_line = out.getvalue().splitlines()[0]
    assert first_line.endswith("open.") and not first_line.startswith("0 new")
    alert_id = int(out.getvalue().splitlines()[1].split()[0].lstrip("#"))
    out = io.StringIO()
    assert run_alerts(ack=[alert_id], by="Anant", engine=engine, out=out) == 0
    assert out.getvalue() == "1 alert(s) marked as seen by Anant.\n"

    out = io.StringIO()
    assert run_purchases(engine=engine, out=out) == 0
    assert "2026-05" in out.getvalue() and "2026-10" in out.getvalue()
