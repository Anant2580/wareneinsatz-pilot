"""Commands for step 6: ingredients, matching, confirmation, approval and price history.

uv run wareneinsatz ingredients load              master list from data/ingredients.json
uv run wareneinsatz ingredients list
uv run wareneinsatz match                         suggest an ingredient for every new invoice line
uv run wareneinsatz confirm 41                    line 41 is what was suggested
uv run wareneinsatz confirm 41 mayonnaise --per-unit 5000   ... or another ingredient / amount
uv run wareneinsatz approve 7 --by Anant          a reviewed invoice counts in the price history
uv run wareneinsatz prices pommes                 net price per kg / l / piece over time
uv run wareneinsatz eval matching                 accuracy on the matching set (eval/README.md)
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from psycopg.errors import UndefinedTable
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from wareneinsatz.config import Settings, get_settings
from wareneinsatz.db import make_engine, session_scope
from wareneinsatz.matching.embeddings import Embedder, OllamaEmbedder
from wareneinsatz.matching.ingredients import DEFAULT_FILE
from wareneinsatz.matching.service import (
    UnknownIngredientError,
    build_matcher,
    confirm_line,
    load_ingredients,
    match_open_lines,
    price_history,
)
from wareneinsatz.models import Ingredient, Invoice, InvoiceLine
from wareneinsatz.reading.llm import LlmUnavailable
from wareneinsatz.repository import record_validation


def add_commands(commands: argparse._SubParsersAction) -> None:
    ingredients = commands.add_parser("ingredients", help="the ingredient master list (step 6)")
    sub = ingredients.add_subparsers(dest="ingredients_command", required=True)
    load = sub.add_parser("load", help="insert or update ingredients from a JSON file")
    load.add_argument("file", nargs="?", type=Path, default=DEFAULT_FILE)
    sub.add_parser("list", help="show the ingredients")

    match = commands.add_parser("match", help="suggest an ingredient for every new invoice line (step 6)")
    match.add_argument("--invoice", type=int, help="only this invoice")
    match.add_argument("--no-embeddings", action="store_true", help="fuzzy matching only (no Ollama needed)")

    confirm = commands.add_parser("confirm", help="confirm or correct the ingredient of an invoice line")
    confirm.add_argument("line", type=int, nargs="?", help="line id, as shown by `match`")
    confirm.add_argument("ingredient", nargs="?", help="ingredient key, if not the suggested one")
    confirm.add_argument("--per-unit", help="amount of the ingredient in one invoice unit (g, ml or pieces)")
    confirm.add_argument("--all", action="store_true", help="confirm every open suggestion as it is")
    confirm.add_argument("--by", default="reviewer", help="who confirms (stored with the memory entry)")

    approve = commands.add_parser("approve", help="approve a reviewed invoice, so it counts in the price history")
    approve.add_argument("invoice", type=int)
    approve.add_argument("--by", default="reviewer", help="who approves")

    prices = commands.add_parser("prices", help="net prices per kg / l / piece from approved invoices")
    prices.add_argument("ingredient", nargs="?", help="ingredient key; all ingredients if left out")


def dispatch(args: argparse.Namespace) -> int | None:
    """Run a step 6 command; None if the command is not one of them."""
    if args.command == "ingredients":
        return run_ingredients(args.ingredients_command, getattr(args, "file", DEFAULT_FILE))
    if args.command == "match":
        return run_match(invoice_id=args.invoice, embeddings=not args.no_embeddings)
    if args.command == "confirm":
        return run_confirm(args.line, args.ingredient, per_unit=args.per_unit, confirm_all=args.all, by=args.by)
    if args.command == "approve":
        return run_approve(args.invoice, by=args.by)
    if args.command == "prices":
        return run_prices(args.ingredient)
    return None


def _guarded(engine: Engine | None, out: TextIO, work: Callable[[Session], int]) -> int:
    """Run `work` in one transaction; database problems become the usual one-line messages."""
    from wareneinsatz.cli import _database_unreachable, _tables_missing  # here: cli imports this module

    try:
        with session_scope(engine or make_engine()) as session:
            return work(session)
    except OperationalError as error:
        return _database_unreachable(error, out)
    except ProgrammingError as error:
        if isinstance(error.orig, UndefinedTable):
            return _tables_missing(out)
        raise


def make_embedder(settings: Settings, out: TextIO) -> Embedder | None:
    """The embedding model if Ollama answers; otherwise fuzzy matching only, with a note."""
    base_url = settings.llm_base_url if settings.llm_api == "ollama" else "http://localhost:11434"
    embedder = OllamaEmbedder(base_url, settings.embedding_model)
    try:
        embedder.embed(["test"])
    except LlmUnavailable as error:
        print(f"Embeddings not available ({error}); matching with fuzzy text similarity only.", file=out)
        return None
    return embedder


def run_ingredients(command: str, file: Path, *, engine: Engine | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout

    def work(session: Session) -> int:
        if command == "load":
            added, updated = load_ingredients(session, file)
            print(f"{added} ingredient(s) added, {updated} updated from {file}", file=out)
            return 0
        for i in session.scalars(select(Ingredient).order_by(Ingredient.category, Ingredient.key)):
            food = "" if i.is_food else "  (not food)"
            print(f"{i.key:<24} {i.name:<28} per {i.base_unit:<6} {i.category or '-'}{food}", file=out)
        return 0

    return _guarded(engine, out, work)


def run_match(
    *,
    invoice_id: int | None = None,
    embeddings: bool = True,
    embedder: Embedder | None = None,
    engine: Engine | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    if embedder is None and embeddings:
        embedder = make_embedder(get_settings(), out)

    def work(session: Session) -> int:
        try:
            matcher = build_matcher(session, embedder=embedder)
        except UnknownIngredientError as error:
            print(error, file=out)
            return 2
        results = match_open_lines(session, matcher, invoice_id=invoice_id)
        if not results:
            print("No new invoice lines to match.", file=out)
            return 0
        for r in results:
            score = f"{r.score:.2f}" if r.score is not None else "    "
            amount = f"{r.base_quantity.normalize():f} {r.base_unit}" if r.base_quantity is not None else ""
            if r.ingredient_key and r.base_quantity is None:
                amount = "amount per unit unknown: --per-unit"
            print(
                f"{r.line_id:>6}  {r.status:<9} {r.method:<9} {score}  {r.description[:40]:<40} "
                f"-> {r.ingredient_key or '-':<22} {amount}",
                file=out,
            )
        counts = {status: sum(r.status == status for r in results) for status in ("confirmed", "suggested", "no_match")}
        print(
            f"\n{len(results)} line(s): {counts['confirmed']} confirmed from memory, {counts['suggested']} suggested, "
            f"{counts['no_match']} without a match",
            file=out,
        )
        if counts["suggested"] or counts["no_match"]:
            print(
                "Confirm: uv run wareneinsatz confirm LINE  ·  correct: uv run wareneinsatz confirm LINE KEY", file=out
            )
        return 0

    return _guarded(engine, out, work)


def run_confirm(
    line_id: int | None,
    ingredient_key: str | None,
    *,
    per_unit: str | None = None,
    confirm_all: bool = False,
    by: str = "reviewer",
    engine: Engine | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    try:
        amount = Decimal(per_unit.replace(",", ".")) if per_unit else None
    except InvalidOperation:
        print(f"--per-unit must be a number, not {per_unit!r}", file=out)
        return 2
    if line_id is None and not confirm_all:
        print("Name a line (uv run wareneinsatz confirm 41) or use --all.", file=out)
        return 2

    def work(session: Session) -> int:
        if confirm_all:
            ids = list(session.scalars(select(InvoiceLine.id).where(InvoiceLine.match_status == "suggested")))
        else:
            ids = [line_id]
        done: set[int] = set()
        for current in ids:
            if current in done:
                continue  # confirmed already together with an earlier line of the same product
            if session.get(InvoiceLine, current) is None:
                print(f"There is no invoice line {current}.", file=out)
                return 2
            try:
                product, confirmed = confirm_line(
                    session,
                    current,
                    ingredient_key=ingredient_key,
                    base_qty_per_unit=amount,
                    confirmed_by=by,
                )
            except UnknownIngredientError as error:
                print(f"{error}. See: uv run wareneinsatz ingredients list", file=out)
                return 2
            done.update(confirmed)
            per = f"{product.base_qty_per_unit.normalize():f} {product.ingredient.base_unit} per unit" if (
                product.base_qty_per_unit) else "amount per unit unknown (add --per-unit)"  # fmt: skip
            print(
                f"Line {current}: {product.description} = {product.ingredient.key} ({per}); "
                f"{len(confirmed)} line(s) confirmed",
                file=out,
            )
        if not ids:
            print("No open suggestions.", file=out)
        return 0

    return _guarded(engine, out, work)


def run_approve(
    invoice_id: int, *, by: str = "reviewer", engine: Engine | None = None, out: TextIO | None = None
) -> int:
    out = out or sys.stdout

    def work(session: Session) -> int:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            print(f"There is no invoice {invoice_id}.", file=out)
            return 2
        if invoice.status == "approved":
            print(f"Invoice {invoice_id} is already approved (by {invoice.approved_by}).", file=out)
            return 0
        try:
            record_validation(session, invoice, invoice.review_issues or [], approve=True, approved_by=by)
        except IntegrityError:
            print(f"Invoice {invoice_id} has no invoice date; it can't be approved without one.", file=out)
            return 2
        errors = sum(issue.get("severity") == "error" for issue in invoice.review_issues or [])
        note = f" despite {errors} validation error(s)" if errors else ""
        print(f"Invoice {invoice_id} approved by {by}{note}.", file=out)
        return 0

    return _guarded(engine, out, work)


def run_prices(ingredient_key: str | None, *, engine: Engine | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout

    def work(session: Session) -> int:
        points = price_history(session, ingredient_key)
        if not points:
            print("No prices yet: approved invoices with confirmed lines are needed (match, confirm, approve).",
                  file=out)  # fmt: skip
            return 0
        previous: dict[str, Decimal] = {}
        current = None
        for p in points:
            if p.ingredient_key != current:
                current = p.ingredient_key
                print(f"\n{p.ingredient_name} ({p.ingredient_key}), net price per {p.display_unit}", file=out)
            change = ""
            if p.ingredient_key in previous and previous[p.ingredient_key]:
                pct = (p.price - previous[p.ingredient_key]) / previous[p.ingredient_key] * 100
                change = f"  {pct:+.1f} %"
            previous[p.ingredient_key] = p.price
            print(f"  {p.issue_date}  {p.supplier_name[:30]:<30} {p.price:>10.2f} EUR{change}", file=out)
        return 0

    return _guarded(engine, out, work)
