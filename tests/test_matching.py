"""Tests for step 6: pack sizes, matching, the matching memory and the price history.

Embeddings are replaced by FakeEmbedder (letter trigrams), so no model server is needed.
"""

import hashlib
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

from wareneinsatz.cli import main
from wareneinsatz.cli_matching import run_approve, run_confirm, run_ingredients, run_match, run_prices
from wareneinsatz.evaluation.matching_eval import load_matching_set, score_method, score_packs, simulate_memory
from wareneinsatz.matching.embeddings import cosine, normalise
from wareneinsatz.matching.ingredients import read_ingredient_file, to_info
from wareneinsatz.matching.matcher import Matcher, Method
from wareneinsatz.matching.normalize import description_key, matching_text
from wareneinsatz.matching.packs import base_quantity_per_unit, parse_pack
from wareneinsatz.matching.service import (
    UnknownIngredientError,
    build_matcher,
    confirm_line,
    load_ingredients,
    match_open_lines,
    price_history,
)
from wareneinsatz.models import InvoiceLine, SupplierProduct
from wareneinsatz.repository import DocumentMeta, record_validation, save_invoice
from wareneinsatz.schemas import InvoiceData

INGREDIENTS = Path(__file__).resolve().parents[1] / "data" / "ingredients.json"


class FakeEmbedder:
    """Letter-trigram vectors: similar spelling, similar vector. Enough to test the plumbing."""

    name = "fake-embedder"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        vectors = []
        for value in texts:
            vector = [0.0] * 64
            padded = f"  {value} "
            for i in range(len(padded) - 2):
                vector[int(hashlib.md5(padded[i : i + 3].encode()).hexdigest(), 16) % 64] += 1
            vectors.append(normalise(vector))
        return vectors


# --- pack sizes ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "count", "content", "base"),
    [
        ("Rapsöl 10 l Kanister", None, "10000", "ml"),
        ("Pommes Frites TK 10 mm, 4 x 2,5 kg", "4", "2500", "g"),  # 10 mm is the cut
        ("Cola 0,33 l Glas, 24 Fl.", "24", "330", "ml"),
        ("Burger Patty Rind 180 g, TK, 40 Stk", "40", "180", "g"),
        ("Schlagsahne 30 % 1 l", None, "1000", "ml"),  # 30 % is the fat content
        ("Freilandeier M 10er", "10", None, None),
        ("Servietten 2lg. 40x40cm 6x250St", "1500", None, None),  # 40x40 cm is the size
        ("Chicken breast fillet IQF 2.5 kg", None, "2500.0", "g"),
        ("Eisbergsalat", None, None, None),
    ],
)
def test_parse_pack(description, count, content, base):
    pack = parse_pack(description)
    assert pack.count == (Decimal(count) if count else None)
    assert pack.content == (Decimal(content) if content else None)
    assert pack.content_base == base


def test_base_quantity_per_unit():
    assert base_quantity_per_unit("Tomaten rund", "KGM", "g") == Decimal(1000)  # weighed: 1 kg = 1000 g
    assert base_quantity_per_unit("Pommes 4 x 2,5 kg", "XCT", "g") == Decimal(10000)
    assert base_quantity_per_unit("Burger Patty Rind 180 g, TK, 40 Stk", "XCT", "piece") == Decimal(40)
    assert base_quantity_per_unit("Eisbergsalat", "H87", "piece") == Decimal(1)
    assert base_quantity_per_unit("Mayo 80 % 10 kg", "XBJ", "ml") is None  # kg for a sauce in ml: needs a person
    assert base_quantity_per_unit("Tomaten", "KGM", "piece") is None


def test_pack_sizes_of_the_matching_set_are_all_right():
    ingredients = {i.key: i for i in to_info(read_ingredient_file(INGREDIENTS))}
    result = score_packs(load_matching_set(), ingredients)
    assert result["items"] == 162 and result["wrong"] == []


# --- texts and the matcher -----------------------------------------------------------------------------


def test_texts():
    assert description_key("Rapsöl  10 l Kanister ") == "rapsöl 10 l kanister"
    assert matching_text("HAEHNCHENBRUST TK 4x2,5kg") == "haehnchenbrust"
    assert matching_text("Hähnchenbrust") == "haehnchenbrust"  # umlauts as suppliers spell them out
    assert matching_text("Schlagsahne 30 % 1 l") == "schlagsahne"


def test_master_list_is_valid(tmp_path):
    entries = read_ingredient_file(INGREDIENTS)
    assert len(entries) == 54
    assert {e.base_unit for e in entries} == {"g", "ml", "piece"}
    assert not next(e for e in entries if e.key == "pfand").is_food
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"ingredients": [entries[0].model_dump(), entries[0].model_dump()]}))
    with pytest.raises(ValueError, match="Duplicate ingredient keys"):
        read_ingredient_file(broken)


@pytest.fixture(scope="module")
def ingredients():
    return to_info(read_ingredient_file(INGREDIENTS))


def test_fuzzy_matching(ingredients):
    matcher = Matcher(ingredients)  # no embedder: fuzzy
    assert matcher.method is Method.FUZZY
    match = matcher.match("Hähnchenbrustfilet 2,5 kg TK")
    assert (match.key, match.method) == ("haehnchenbrust", Method.FUZZY)
    assert match.candidates[0].key == "haehnchenbrust" and len(match.candidates) == 3
    nothing = matcher.match("Alufolie 30 cm x 150 m")
    assert nothing.key is None and nothing.method is Method.NONE and nothing.candidates  # candidates still shown


def test_hybrid_matching_and_learning(ingredients):
    embedder = FakeEmbedder()
    matcher = Matcher(ingredients, embedder=embedder)
    assert matcher.method is Method.HYBRID
    results = matcher.match_many(["Rapsöl 10 l Kanister", "Ketchup 5 kg Eimer"])
    assert [r.key for r in results] == ["rapsoel", "ketchup"]
    assert embedder.calls == 2  # ingredient texts once, both lines in one batch

    unknown = "Frittenöl Spezial XL"
    before = matcher.match(unknown)
    matcher.learn("frittierfett", unknown)  # a person confirmed it once
    after = matcher.match(unknown)
    assert after.key == "frittierfett" and after.score > before.score


def test_matchers_need_an_embedder_for_embeddings(ingredients):
    with pytest.raises(ValueError, match="needs an embedder"):
        Matcher(ingredients, method="embedding")


def test_cosine():
    a, b = normalise([3.0, 4.0]), normalise([6.0, 8.0])
    assert cosine(a, b) == pytest.approx(1.0)


def test_scores_per_split(ingredients):
    items = load_matching_set()
    scores = score_method(Matcher(ingredients), items)
    assert {"dev", "test", "all"} == set(scores.splits)
    assert scores.splits["all"]["items"] == 179 and scores.splits["all"]["unknown"] == 14
    assert 0.8 < scores.splits["test"]["top1_accuracy"] <= 1.0


def test_memory_simulation(ingredients):
    lines = [
        {"line_no": 1, "description": "Zucker 5 kg", "quantity": "1", "unit_price": "4", "line_amount": "4.00"},
        {"line_no": 2, "description": "Ketchup 5 kg Eimer", "quantity": "1", "unit_price": "9", "line_amount": "9.00"},
    ]
    invoice = {"supplier": {"name": "Muster GmbH"}, "lines": lines, "gross_total": "13.91"}
    invoices = [
        InvoiceData.model_validate({**invoice, "issue_date": day, "invoice_number": n})
        for n, day in [("1", "2026-01-02"), ("2", "2026-02-02")]
    ]
    truth = {"Zucker 5 kg": "zucker", "Ketchup 5 kg Eimer": "ketchup"}
    result = simulate_memory(invoices, truth, Matcher(ingredients))
    assert (result["lines"], result["confirmations"], result["automatic"], result["right_suggestions"]) == (4, 2, 2, 2)


# --- database: memory, confirmation, price history -----------------------------------------------------


def _invoice(supplier: str, number: str, day: str, lines: list[tuple], *, gross: bool = False) -> InvoiceData:
    """lines: (article_no, description, quantity, unit_code, unit_price, line_amount, vat_rate)."""
    data_lines = [
        {"line_no": n, "seller_article_no": a, "description": d, "quantity": q, "unit_code": u, "unit_price": p,
         "line_amount": amount, "vat_rate": v}
        for n, (a, d, q, u, p, amount, v) in enumerate(lines, start=1)
    ]  # fmt: skip
    total = sum(Decimal(line[5]) for line in lines)
    return InvoiceData.model_validate(
        {
            "invoice_number": number,
            "issue_date": day,
            "supplier": {"name": supplier},
            "lines": data_lines,
            "prices_include_vat": gross,
            "gross_total": str(total),
        }  # fmt: skip
    )


def _store(session, data: InvoiceData, *, approve: bool = True):
    meta = DocumentMeta(sha256=hashlib.sha256(data.model_dump_json().encode()).hexdigest(), filename="x.xml",
                        media_type="application/xml", size_bytes=1)  # fmt: skip
    invoice = save_invoice(session, meta, data)
    record_validation(session, invoice, [], approve=approve)
    return invoice


@pytest.fixture
def with_ingredients(session):
    load_ingredients(session, INGREDIENTS)
    return session


def test_loading_the_master_list_twice_updates(with_ingredients):
    assert load_ingredients(with_ingredients, INGREDIENTS) == (0, 54)


def test_match_confirm_memory_and_price_history(with_ingredients):
    session = with_ingredients
    first = _store(
        session,
        _invoice("Muster Großhandel GmbH", "R1", "2026-09-01", [
            ("4711", "Pommes Frites TK 10 mm, 4 x 2,5 kg", "2", "XCT", "18.50", "37.00", "7"),
            (None, "Mayonnaise 80 % 5 l", "1", "XBJ", "14.00", "14.00", "7"),
        ]),
    )  # fmt: skip
    matcher = build_matcher(session, embedder=FakeEmbedder())
    results = match_open_lines(session, matcher)
    assert [(r.status, r.ingredient_key) for r in results] == [("suggested", "pommes"), ("suggested", "mayonnaise")]
    assert results[0].base_quantity == Decimal("20000")  # 2 cartons x 4 x 2.5 kg

    product, confirmed = confirm_line(session, results[0].line_id, confirmed_by="Anant")
    assert (product.article_no, product.base_qty_per_unit, product.confirmed_by) == ("4711", Decimal(10000), "Anant")
    assert confirmed == [results[0].line_id]
    confirm_line(session, results[1].line_id)

    # The next invoice: same products, matched from memory without asking.
    second = _store(
        session,
        _invoice("Muster Großhandel GmbH", "R2", "2026-10-01", [
            ("4711", "Pommes Frites 4x2,5kg", "1", "XCT", "19.90", "19.90", "7"),  # new text, same article no.
            (None, "Mayonnaise 80 % 5 l", "2", "XBJ", "14.50", "29.00", "7"),
        ]),
    )  # fmt: skip
    again = match_open_lines(session, build_matcher(session), invoice_id=second.id)
    assert [(r.status, r.method) for r in again] == [("confirmed", "memory"), ("confirmed", "memory")]

    prices = price_history(session, "pommes")
    assert [(p.issue_date, p.price, p.display_unit) for p in prices] == [
        (date(2026, 9, 1), Decimal("1.8500"), "kg"),  # 37.00 EUR / 20 kg
        (date(2026, 10, 1), Decimal("1.9900"), "kg"),
    ]
    assert first.id != second.id


def test_receipts_count_net_and_unapproved_invoices_dont(with_ingredients):
    session = with_ingredients
    receipt = _invoice("Getränkemarkt", None, "2026-09-05", [(None, "Cola 0,33 l Glas, 24 Fl.", "1", "XCR", "16.66",
                       "16.66", "19")], gross=True)  # fmt: skip
    _store(session, receipt)
    pending = _invoice("Getränkemarkt", "X9", "2026-09-06", [(None, "Cola 0,33 l Glas, 24 Fl.", "1", "XCR", "20.00",
                       "20.00", "19")])  # fmt: skip
    _store(session, pending, approve=False)
    results = match_open_lines(session, build_matcher(session))
    confirm_line(session, results[0].line_id, ingredient_key="cola")
    [price] = price_history(session, "cola")
    assert price.price == Decimal("1.7677")  # 16.66 / 1.19 = 14.00 net for 7.92 l
    assert session.scalar(select(InvoiceLine.match_status).where(InvoiceLine.id == results[1].line_id)) == "confirmed"


def test_corrections_and_unknown_amounts(with_ingredients):
    session = with_ingredients
    _store(session, _invoice("Muster", "R5", "2026-09-01", [(None, "Mayo 80 % 10 kg", "1", "XBJ", "30", "30.00", "7")]))
    [line] = match_open_lines(session, build_matcher(session))
    assert line.ingredient_key == "mayonnaise" and line.base_quantity is None  # kg for a sauce counted in ml
    assert price_history(session, "mayonnaise") == []  # no amount, no price

    product, _ = confirm_line(session, line.line_id, ingredient_key="mayonnaise", base_qty_per_unit=Decimal(10500))
    assert product.base_qty_per_unit == Decimal(10500)
    stored = session.get(InvoiceLine, line.line_id)
    assert (stored.match_status, stored.base_quantity) == ("confirmed", Decimal(10500))

    confirm_line(session, line.line_id, ingredient_key="ketchup")  # a correction
    assert session.get(InvoiceLine, line.line_id).ingredient.key == "ketchup"
    assert session.scalar(select(SupplierProduct.ingredient_id)) == session.get(InvoiceLine, line.line_id).ingredient_id
    with pytest.raises(UnknownIngredientError):
        confirm_line(session, line.line_id, ingredient_key="kaviar")


def test_no_ingredients_loaded(session):
    with pytest.raises(UnknownIngredientError, match="ingredients load"):
        build_matcher(session)


def test_the_database_rejects_a_confirmed_line_without_ingredient(with_ingredients):
    session = with_ingredients
    invoice = _store(
        session, _invoice("Muster", "R7", "2026-09-01", [(None, "Zucker 5 kg", "1", "XBG", "4", "4.00", "7")])
    )
    with pytest.raises(Exception, match="confirmed_has_ingredient"), session.begin_nested():
        session.execute(text(f"UPDATE invoice_lines SET match_status = 'confirmed' WHERE invoice_id = {invoice.id}"))


# --- command line --------------------------------------------------------------------------------------


@pytest.fixture
def clean_database(engine):
    yield engine
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE invoice_lines, invoice_tax_totals, invoices, documents, supplier_products, ingredients, "
                "suppliers CASCADE"
            )  # fmt: skip
        )


def test_cli_from_master_list_to_prices(clean_database, samples, tmp_path):
    from wareneinsatz.cli import run_ingest
    from wareneinsatz.ingest.store import DocumentStore

    engine = clean_database
    out = io.StringIO()
    assert run_ingredients("load", INGREDIENTS, engine=engine, out=out) == 0
    assert out.getvalue().startswith("54 ingredient(s) added")
    run_ingest([samples / "einvoices" / "musterfrisch_xrechnung_cii.xml"], engine=engine,
               store=DocumentStore(tmp_path), out=io.StringIO())  # fmt: skip

    out = io.StringIO()
    assert run_match(embedder=FakeEmbedder(), engine=engine, out=out) == 0
    printed = out.getvalue()
    assert "7 line(s): 0 confirmed from memory" in printed and "uv run wareneinsatz confirm LINE" in printed

    out = io.StringIO()
    assert run_confirm(None, None, confirm_all=True, by="Anant", engine=engine, out=out) == 0
    assert "line(s) confirmed" in out.getvalue()

    out = io.StringIO()
    assert run_prices(None, engine=engine, out=out) == 0
    assert "net price per" in out.getvalue()


def test_cli_messages(clean_database):
    out = io.StringIO()
    assert run_match(embeddings=False, engine=clean_database, out=out) == 2
    assert "ingredients load" in out.getvalue()
    out = io.StringIO()
    assert run_confirm(999, None, engine=clean_database, out=out) == 2
    assert "There is no invoice line 999" in out.getvalue()
    out = io.StringIO()
    assert run_confirm(1, None, per_unit="viel", engine=clean_database, out=out) == 2
    assert run_approve(999, engine=clean_database, out=io.StringIO()) == 2
    out = io.StringIO()
    assert run_prices(None, engine=clean_database, out=out) == 0
    assert out.getvalue().startswith("No prices yet")


def test_cli_eval_matching_without_embeddings(tmp_path, capsys):
    args = ["eval", "matching", "--no-embeddings", "--dataset", str(tmp_path / "none"), "--out", str(tmp_path)]
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "fuzzy      test half" in output and "pack sizes: 162 of 162 right" in output
    assert list(tmp_path.glob("*_matching_fuzzy-only.md"))
