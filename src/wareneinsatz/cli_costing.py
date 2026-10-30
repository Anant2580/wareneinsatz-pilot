"""Commands for step 7: recipes, menu costing, alerts, monthly Wareneinsatz and demo data.

uv run wareneinsatz recipes load               menu and preparations from data/recipes.json
uv run wareneinsatz cost                       every dish: cost, food-cost %, margin
uv run wareneinsatz cost classic-burger        one dish, item by item
uv run wareneinsatz alerts                     check for price jumps, cheaper suppliers, dishes over target
uv run wareneinsatz alerts --ack 3 --by Anant  mark an alert as seen
uv run wareneinsatz purchases                  net food purchases (Wareneinsatz) per month
uv run wareneinsatz demo load                  six months of fictional purchases, for trying it out
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from wareneinsatz.alerts import open_alerts
from wareneinsatz.costing import RecipeCost
from wareneinsatz.models import Alert, Ingredient, Recipe
from wareneinsatz.recipes import DEFAULT_RECIPES, RecipeError, check_alerts, cost_menu, load_recipes, monthly_purchases


def add_commands(commands: argparse._SubParsersAction) -> None:
    recipes = commands.add_parser("recipes", help="dishes and preparations (step 7)")
    sub = recipes.add_subparsers(dest="recipes_command", required=True)
    load = sub.add_parser("load", help="insert or update recipes from a JSON file")
    load.add_argument("file", nargs="?", type=Path, default=DEFAULT_RECIPES)
    sub.add_parser("list", help="show the recipes")

    cost = commands.add_parser("cost", help="dish costs, food-cost % and margins with current prices")
    cost.add_argument("dish", nargs="?", help="a recipe key for the item-by-item breakdown")
    cost.add_argument("--pricing", choices=["latest", "average"], default="latest",
                      help="latest purchase (default) or 90-day average")  # fmt: skip
    cost.add_argument("--until", type=date.fromisoformat, help="prices as they were on this date (YYYY-MM-DD)")

    alerts = commands.add_parser("alerts", help="check for and list price jumps, cheaper suppliers, dishes over target")
    alerts.add_argument("--ack", type=int, nargs="+", help="mark these alerts as seen")
    alerts.add_argument("--by", default="reviewer")

    purchases = commands.add_parser("purchases", help="net purchases per month (Wareneinsatz)")
    purchases.add_argument("--months", type=int, default=12)

    demo = commands.add_parser("demo", help="demo data for trying it out (never in a real restaurant's database)")
    demo_sub = demo.add_subparsers(dest="demo_command", required=True)
    demo_load = demo_sub.add_parser("load", help="26 weeks of purchases from five fictional suppliers")
    demo_load.add_argument("--weeks", type=int, default=26)


def dispatch(args: argparse.Namespace) -> int | None:
    if args.command == "recipes":
        return run_recipes(args.recipes_command, getattr(args, "file", DEFAULT_RECIPES))
    if args.command == "cost":
        return run_cost(args.dish, pricing=args.pricing, until=args.until)
    if args.command == "alerts":
        return run_alerts(ack=args.ack, by=args.by)
    if args.command == "purchases":
        return run_purchases(months=args.months)
    if args.command == "demo":
        return run_demo(weeks=args.weeks)
    return None


def _guarded(engine: Engine | None, out: TextIO, work: Callable[[Session], int]) -> int:
    from wareneinsatz.cli_matching import _guarded as guarded

    return guarded(engine, out, work)


def _eur(value: Decimal | None) -> str:
    return "–" if value is None else f"{value:,.2f}"


def run_recipes(command: str, file: Path, *, engine: Engine | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout

    def work(session: Session) -> int:
        if command == "load":
            if session.scalar(select(Ingredient.id).limit(1)) is None:
                print("Load the ingredients first: uv run wareneinsatz ingredients load", file=out)
                return 2
            try:
                added, updated = load_recipes(session, file)
            except RecipeError as error:
                print(f"Recipes not loaded: {error}", file=out)
                return 2
            print(f"{added} recipe(s) added, {updated} updated from {file}", file=out)
            return 0
        for recipe in session.scalars(select(Recipe).order_by(Recipe.kind, Recipe.key)):
            if recipe.menu_price_gross:
                price = f"{_eur(recipe.menu_price_gross)} EUR, VAT {recipe.vat_rate:.0f} %"
            else:
                price = f"yields {recipe.yield_quantity.normalize():f} {recipe.yield_unit}"
            print(f"{recipe.key:<24} {recipe.kind:<12} {recipe.name:<36} {price}", file=out)
        return 0

    return _guarded(engine, out, work)


def run_cost(
    dish: str | None,
    *,
    pricing: str = "latest",
    until: date | None = None,
    engine: Engine | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout

    def work(session: Session) -> int:
        costs = cost_menu(session, method=pricing, until=until)
        if not costs:
            print("No recipes yet. Load them: uv run wareneinsatz recipes load", file=out)
            return 2
        if dish:
            if dish not in costs:
                print(f"Unknown recipe {dish!r}. See: uv run wareneinsatz recipes list", file=out)
                return 2
            _print_breakdown(costs[dish], out)
            return 0
        print(
            f"{'Dish':<28} {'Menu':>7} {'Net':>7} {'Cost':>7} {'Food cost':>10} {'Target':>7} {'Margin':>7}", file=out
        )
        for cost in sorted((c for c in costs.values() if c.kind == "dish"), key=lambda c: c.name):
            pct = f"{cost.food_cost_pct:.1f} %" if cost.food_cost_pct is not None else "–"
            flag = "  over target" if cost.over_target else ""
            if not cost.complete:
                flag = f"  no price yet: {', '.join(cost.missing)}"
            target = f"{cost.target_food_cost_pct:.0f} %" if cost.target_food_cost_pct else "–"
            print(
                f"{cost.name[:28]:<28} {_eur(cost.menu_price_gross):>7} {_eur(cost.net_price):>7} "
                f"{_eur(cost.cost):>7} {pct:>10} {target:>7} {_eur(cost.margin):>7}{flag}",
                file=out,
            )
        print(f"\nPrices: {pricing} purchase{' (90-day average)' if pricing == 'average' else ''}, net, EUR", file=out)
        return 0

    return _guarded(engine, out, work)


def _print_breakdown(cost: RecipeCost, out: TextIO) -> None:
    print(f"{cost.name} ({cost.kind})", file=out)
    for item in cost.items:
        waste = f", {item.waste_pct.normalize():f} % waste" if item.waste_pct else ""
        unit_price = (
            "no price yet" if item.price_per_base is None else f"{_eur(item.price_per_base * _per(item))}/{_unit(item)}"
        )
        amount = f"{item.quantity.normalize():f} {item.base_unit}{waste}"
        print(f"  {item.label[:36]:<36} {amount:<22} {unit_price:>14} {_eur(item.cost):>7} EUR", file=out)
    if cost.kind == "preparation":
        print(f"  = {_eur(cost.cost)} EUR for {cost.yield_quantity.normalize():f} {cost.yield_unit}", file=out)
        return
    pct = f"{cost.food_cost_pct:.1f} %" if cost.food_cost_pct is not None else "–"
    print(f"  = cost {_eur(cost.cost)} EUR · net price {_eur(cost.net_price)} EUR · food cost {pct} · "
          f"margin {_eur(cost.margin)} EUR", file=out)  # fmt: skip


def _per(item) -> Decimal:
    return Decimal(1) if item.base_unit == "piece" else Decimal(1000)


def _unit(item) -> str:
    return {"g": "kg", "ml": "l", "piece": "piece"}[item.base_unit]


def run_alerts(
    *, ack: list[int] | None = None, by: str = "reviewer", engine: Engine | None = None, out: TextIO | None = None
) -> int:
    out = out or sys.stdout

    def work(session: Session) -> int:
        if ack:
            for alert_id in ack:
                alert = session.get(Alert, alert_id)
                if alert is None:
                    print(f"There is no alert {alert_id}.", file=out)
                    return 2
                alert.acknowledged_at, alert.acknowledged_by = datetime.now(UTC), by
            print(f"{len(ack)} alert(s) marked as seen by {by}.", file=out)
            return 0
        if session.scalar(select(Ingredient.id).limit(1)) is None:
            print("No ingredients yet. Load them: uv run wareneinsatz ingredients load", file=out)
            return 2
        result = check_alerts(session)
        alerts = open_alerts(session)
        print(f"{result.new} new alert(s); {len(alerts)} open.", file=out)
        for alert in alerts:
            print(f"#{alert.id:<4} {alert.happened_on}  {alert.kind:<16} {alert.message}", file=out)
        if alerts:
            print("Mark as seen: uv run wareneinsatz alerts --ack ID [ID ...]", file=out)
        return 0

    return _guarded(engine, out, work)


def run_purchases(*, months: int = 12, engine: Engine | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout

    def work(session: Session) -> int:
        rows = monthly_purchases(session, months=months)
        if not rows:
            print("No approved purchases yet.", file=out)
            return 0
        print(
            f"{'Month':<8} {'Food (Wareneinsatz)':>20} {'Non-food':>10} {'Not matched':>12}  Biggest categories",
            file=out,
        )
        for row in rows:
            top = sorted(row.by_category.items(), key=lambda kv: -kv[1])[:3]
            categories = ", ".join(f"{name} {_eur(amount)}" for name, amount in top)
            amounts = f"{_eur(row.food):>20} {_eur(row.non_food):>10} {_eur(row.unmatched):>12}"
            print(f"{row.month:%Y-%m}  {amounts}  {categories}", file=out)
        print(
            "\nNet EUR from approved invoices; credit notes subtracted; lines not yet confirmed are shown apart.",
            file=out,
        )
        return 0

    return _guarded(engine, out, work)


def run_demo(*, weeks: int = 26, engine: Engine | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    from wareneinsatz.demo import load_demo

    def work(session: Session) -> int:
        if session.scalar(select(Ingredient.id).limit(1)) is None:
            print("Load the ingredients first: uv run wareneinsatz ingredients load", file=out)
            return 2
        result = load_demo(session, weeks=weeks)
        print(
            f"Demo data: {result.invoices} invoice(s) stored as approved ({result.skipped} loaded before), "
            f"{result.lines} line(s) confirmed with {result.confirmations} memory entries.",
            file=out,
        )
        return 0

    return _guarded(engine, out, work)
