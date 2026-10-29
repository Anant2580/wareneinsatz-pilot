"""Matching in the database: load the master list, match invoice lines, confirm, read price history.

The flow for one invoice line:

    memory hit (supplier + article no., or supplier + exact description)
        -> confirmed automatically, amount per unit from the memory
    otherwise the matcher suggests an ingredient (or none)
        -> a person confirms or corrects it once: confirm_line()
        -> that creates the memory entry, and every other open line of the same supplier
           product is confirmed with it

Only confirmed lines of approved invoices count in the price history (v_ingredient_prices).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from wareneinsatz.matching.embeddings import Embedder
from wareneinsatz.matching.ingredients import DEFAULT_FILE, read_ingredient_file
from wareneinsatz.matching.matcher import IngredientInfo, Matcher, Method
from wareneinsatz.matching.normalize import description_key
from wareneinsatz.matching.packs import base_quantity_per_unit
from wareneinsatz.models import Ingredient, Invoice, InvoiceLine, SupplierProduct


class UnknownIngredientError(ValueError):
    pass


def load_ingredients(session: Session, path: Path = DEFAULT_FILE) -> tuple[int, int]:
    """Insert or update ingredients by key. Returns (added, updated). Ingredients are never deleted here."""
    added = updated = 0
    existing = {ingredient.key: ingredient for ingredient in session.scalars(select(Ingredient))}
    for entry in read_ingredient_file(path):
        row = existing.get(entry.key)
        if row is None:
            session.add(Ingredient(**entry.model_dump()))
            added += 1
        else:
            for name, value in entry.model_dump().items():
                setattr(row, name, value)
            updated += 1
    session.flush()
    return added, updated


def build_matcher(session: Session, *, embedder: Embedder | None = None) -> Matcher:
    """A matcher for the ingredients in the database. Confirmed descriptions count as extra aliases."""
    learned: dict[int, list[str]] = {}
    for ingredient_id, description in session.execute(
        select(SupplierProduct.ingredient_id, SupplierProduct.description)
    ):
        learned.setdefault(ingredient_id, []).append(description)
    infos = [
        IngredientInfo(i.key, i.name, i.base_unit, i.is_food, (*i.aliases, *learned.get(i.id, [])))
        for i in session.scalars(select(Ingredient).order_by(Ingredient.key))
    ]
    if not infos:
        raise UnknownIngredientError("No ingredients yet. Load the master list: uv run wareneinsatz ingredients load")
    return Matcher(infos, embedder=embedder)


@dataclass(frozen=True)
class LineMatch:
    line_id: int
    invoice_id: int
    description: str
    status: str  # confirmed, suggested, no_match
    method: str
    score: float | None
    ingredient_key: str | None
    base_quantity: Decimal | None
    base_unit: str | None


def find_memory(session: Session, supplier_id: int, line: InvoiceLine) -> SupplierProduct | None:
    if line.seller_article_no:
        query = select(SupplierProduct).where(
            SupplierProduct.supplier_id == supplier_id, SupplierProduct.article_no == line.seller_article_no
        )
    else:
        query = select(SupplierProduct).where(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.article_no.is_(None),
            SupplierProduct.description_key == description_key(line.description),
        )
    return session.scalar(query)


def match_open_lines(session: Session, matcher: Matcher, *, invoice_id: int | None = None) -> list[LineMatch]:
    """Match every line that has not been matched yet (match_status NULL)."""
    query = (
        select(InvoiceLine)
        .join(Invoice)
        .where(InvoiceLine.match_status.is_(None), Invoice.status != "rejected")
        .order_by(InvoiceLine.invoice_id, InvoiceLine.line_no)
    )
    if invoice_id is not None:
        query = query.where(InvoiceLine.invoice_id == invoice_id)
    lines = list(session.scalars(query))
    ingredients = {i.key: i for i in session.scalars(select(Ingredient))}

    results: list[LineMatch] = []
    to_match: list[InvoiceLine] = []
    for line in lines:
        product = find_memory(session, line.invoice.supplier_id, line)
        if product is not None:
            _apply_memory(line, product)
            results.append(_result(line))
        else:
            to_match.append(line)
    for line, match in zip(to_match, matcher.match_many([line.description for line in to_match]), strict=True):
        if match.key is None:
            line.match_status, line.match_method, line.ingredient_id = "no_match", None, None
        else:
            ingredient = ingredients[match.key]
            line.match_status, line.match_method, line.ingredient_id = "suggested", match.method.value, ingredient.id
            line.base_quantity = _base_quantity(line, ingredient, None)
        line.match_score = round(match.score, 4)
        results.append(_result(line))
    session.flush()
    return sorted(results, key=lambda r: (r.invoice_id, r.line_id))


def confirm_line(
    session: Session,
    line_id: int,
    *,
    ingredient_key: str | None = None,
    base_qty_per_unit: Decimal | None = None,
    confirmed_by: str = "reviewer",
    matcher: Matcher | None = None,
) -> tuple[SupplierProduct, list[int]]:
    """A person confirms (or corrects) the ingredient of a line. Creates or updates the memory entry.

    Every other open line of the same supplier product is confirmed with it. Returns the memory
    entry and the ids of all lines it confirmed.
    """
    line = session.get_one(InvoiceLine, line_id)
    if ingredient_key is not None:
        ingredient = session.scalar(select(Ingredient).where(Ingredient.key == ingredient_key))
        if ingredient is None:
            raise UnknownIngredientError(f"Unknown ingredient {ingredient_key!r}")
    elif line.ingredient is not None:
        ingredient = line.ingredient
    else:
        raise UnknownIngredientError(f"Line {line_id} has no suggestion: name the ingredient")

    supplier_id = line.invoice.supplier_id
    per_unit = base_qty_per_unit or base_quantity_per_unit(line.description, line.unit_code, ingredient.base_unit)
    product = find_memory(session, supplier_id, line)
    if product is None:
        product = SupplierProduct(
            supplier_id=supplier_id,
            article_no=line.seller_article_no,
            description_key=description_key(line.description),
            description=line.description,
            unit_code=line.unit_code,
            ingredient=ingredient,
            base_qty_per_unit=per_unit,
            confirmed_by=confirmed_by,
        )
        session.add(product)
    else:  # a correction: the person knows better than the earlier confirmation
        product.ingredient = ingredient
        product.base_qty_per_unit = per_unit
        product.confirmed_by = confirmed_by
    session.flush()

    manual = ingredient_key is not None and (line.ingredient is None or line.ingredient.key != ingredient_key)
    _apply_memory(line, product, method="manual" if manual else None)
    confirmed = [line.id]
    for other in _open_lines_of(session, supplier_id, product, exclude=line.id):
        _apply_memory(other, product, method="memory")
        other.match_score = 1.0
    # A correction also changes the lines confirmed earlier with this memory entry.
    for earlier in session.scalars(
        select(InvoiceLine).where(InvoiceLine.supplier_product_id == product.id, InvoiceLine.id.not_in(confirmed))
    ):
        _apply_memory(earlier, product, method=earlier.match_method)
        confirmed.append(earlier.id)
        confirmed.append(other.id)
    if matcher is not None:
        matcher.learn(ingredient.key, line.description)
    session.flush()
    return product, confirmed


def _open_lines_of(session: Session, supplier_id: int, product: SupplierProduct, *, exclude: int) -> list[InvoiceLine]:
    query = (
        select(InvoiceLine)
        .join(Invoice)
        .where(
            Invoice.supplier_id == supplier_id,
            InvoiceLine.id != exclude,
            or_(InvoiceLine.match_status.is_(None), InvoiceLine.match_status != "confirmed"),
        )
    )
    if product.article_no:
        query = query.where(InvoiceLine.seller_article_no == product.article_no)
    else:
        query = query.where(InvoiceLine.seller_article_no.is_(None))
    return [line for line in session.scalars(query) if product.article_no or
            description_key(line.description) == product.description_key]  # fmt: skip


def _apply_memory(line: InvoiceLine, product: SupplierProduct, *, method: str | None = None) -> None:
    """Confirm a line with a memory entry. A confirmed suggestion keeps its method and score, so it
    stays measurable which method suggested what; everything else is a memory match."""
    was_suggested = line.match_status == "suggested"
    line.supplier_product = product
    line.ingredient = product.ingredient
    line.match_status = "confirmed"
    if method is not None:
        line.match_method = method
    elif not was_suggested:
        line.match_method, line.match_score = "memory", 1.0
    line.base_quantity = _base_quantity(line, product.ingredient, product.base_qty_per_unit)


def _base_quantity(line: InvoiceLine, ingredient: Ingredient, per_unit: Decimal | None) -> Decimal | None:
    per_unit = per_unit or base_quantity_per_unit(line.description, line.unit_code, ingredient.base_unit)
    if per_unit is None:
        return None
    return (line.quantity * per_unit).quantize(Decimal("0.0001"))


def _result(line: InvoiceLine) -> LineMatch:
    return LineMatch(
        line_id=line.id,
        invoice_id=line.invoice_id,
        description=line.description,
        status=line.match_status or "",
        method=line.match_method or Method.NONE.value,
        score=line.match_score,
        ingredient_key=line.ingredient.key if line.ingredient else None,
        base_quantity=line.base_quantity,
        base_unit=line.ingredient.base_unit if line.ingredient else None,
    )


@dataclass(frozen=True)
class PricePoint:
    issue_date: date
    supplier_name: str
    ingredient_key: str
    ingredient_name: str
    price: Decimal  # net, per display unit
    display_unit: str  # kg, l or piece
    base_quantity: Decimal
    base_unit: str


def price_history(session: Session, ingredient_key: str | None = None) -> list[PricePoint]:
    """Net prices per kg / l / piece from v_ingredient_prices, oldest first."""
    sql = (
        "SELECT issue_date, supplier_name, ingredient_key, ingredient_name, net_price_per_display_unit, "
        "display_unit, base_quantity, base_unit FROM v_ingredient_prices"
    )
    params = {}
    if ingredient_key:
        sql += " WHERE ingredient_key = :key"
        params["key"] = ingredient_key
    sql += " ORDER BY ingredient_key, issue_date, invoice_line_id"
    return [
        PricePoint(row[0], row[1], row[2], row[3], Decimal(row[4]).quantize(Decimal("0.0001")), row[5], row[6], row[7])
        for row in session.execute(text(sql), params)
    ]
