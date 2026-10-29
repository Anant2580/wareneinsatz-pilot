"""Pack sizes: how much of an ingredient one invoice unit holds.

Prices are only comparable per kilogram, litre or piece. An invoice says "3 Krt" of
"Burger Patty Rind 180 g, TK, 40 Stk"; the cost of a burger needs the price of one patty.
The pack size is in the description, written in many ways:

    "Rapsöl 10 l Kanister"                 one unit = 10 l
    "Pommes Frites TK 10 mm, 4 x 2,5 kg"   one unit = 4 x 2.5 kg = 10 kg (the 10 mm is a cut, not a size)
    "Cola 0,33 l Glas, 24 Fl."             one unit = 24 x 0.33 l = 7.92 l
    "Burger Patty Rind 180 g, 40 Stk"      one unit = 40 pieces of 180 g
    "Freilandeier M 10er"                  one unit = 10 pieces
    "Schlagsahne 30 % 1 l"                 one unit = 1 l (30 % is the fat content)

parse_pack() reads that; base_quantity_per_unit() turns it into the ingredient's base unit
(g, ml or piece), or None if the description doesn't say (then a person enters it once).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from wareneinsatz.units import to_base

# Content units in the descriptions and their size in base units
CONTENT_UNITS: dict[str, tuple[Decimal, str]] = {
    "kg": (Decimal(1000), "g"),
    "g": (Decimal(1), "g"),
    "gr": (Decimal(1), "g"),
    "l": (Decimal(1000), "ml"),
    "ltr": (Decimal(1000), "ml"),
    "liter": (Decimal(1000), "ml"),
    "ml": (Decimal(1), "ml"),
    "cl": (Decimal(10), "ml"),
}
COUNT_WORDS = r"(?:stk|stück|stueck|st|fl|fla|flaschen|pcs|pc|blatt|bl)"
_NUMBER = r"\d+(?:[.,]\d+)?"
_CONTENT = rf"(?P<amount>{_NUMBER})\s*(?P<unit>kg|gr|g|ltr|liter|ml|cl|l)\b"

# Things that look like amounts but aren't: fat or alcohol content, and dimensions (cut, sheet size).
_NOISE = [
    re.compile(rf"{_NUMBER}\s*%"),  # 20 % Fett
    re.compile(rf"{_NUMBER}\s*(?:x\s*{_NUMBER}\s*)?(?:mm|cm|m)\b"),  # 10 mm, 40 x 60 cm, 150 m
]
_MULTI_CONTENT = re.compile(rf"(?P<count>\d+)\s*x\s*{_CONTENT}")  # 4 x 2,5 kg
_MULTI_COUNT = re.compile(rf"(?P<a>\d+)\s*x\s*(?P<b>\d+)\s*{COUNT_WORDS}\b")  # 6x250 St
_CONTENT_RE = re.compile(_CONTENT)
_COUNT_RE = re.compile(rf"(?P<count>\d+)\s*{COUNT_WORDS}\b|(?P<er>\d+)er\b")  # 40 Stk, 24 Fl., 10er


@dataclass(frozen=True)
class Pack:
    count: Decimal | None  # pieces per invoice unit (None: not printed)
    content: Decimal | None  # size of one piece in g or ml (None: not printed)
    content_base: str | None  # "g" or "ml"


def _number(text: str) -> Decimal | None:
    cleaned = text.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _content(amount: str, unit: str) -> tuple[Decimal, str] | None:
    value = _number(amount)
    factor, base = CONTENT_UNITS[unit]
    return (value * factor, base) if value is not None else None


def parse_pack(description: str) -> Pack:
    text = description.lower()
    for noise in _NOISE:
        text = noise.sub(" ", text)

    count: Decimal | None = None
    content: tuple[Decimal, str] | None = None
    if m := _MULTI_CONTENT.search(text):  # "4 x 2,5 kg" / "24x0,33l"
        count = Decimal(m["count"])
        content = _content(m["amount"], m["unit"])
        text = text[: m.start()] + " " + text[m.end() :]
    if count is None and (m := _MULTI_COUNT.search(text)):  # "6x250 St" / "4X10ST"
        count = Decimal(m["a"]) * Decimal(m["b"])
        text = text[: m.start()] + " " + text[m.end() :]
    if content is None and (m := _CONTENT_RE.search(text)):  # "180 g", "0,33 l", "2.5 kg"
        content = _content(m["amount"], m["unit"])
    if count is None and (m := _COUNT_RE.search(text)):  # "40 Stk", "24 Fl.", "10er"
        count = Decimal(m["count"] or m["er"])
    return Pack(count=count, content=content[0] if content else None, content_base=content[1] if content else None)


def base_quantity_per_unit(description: str, unit_code: str | None, base_unit: str) -> Decimal | None:
    """Ingredient base units (g, ml or piece) in one invoice unit, or None if it can't be told.

    Weighed or measured lines (kg, l) need no pack size: 1 kg = 1000 g. For pieces and packages
    the description decides: "4 x 2,5 kg" in a carton is 10,000 g. If the description gives the
    size in another kind of unit (a sauce in l, the recipe in g), it stays None: converting
    would need the density, so a person enters the number once.
    """
    measured = to_base(Decimal(1), unit_code)
    if measured is not None and measured[1] in ("g", "ml"):
        factor, base = measured
        return factor if base == base_unit else None
    pack = parse_pack(description)
    if base_unit == "piece":
        return pack.count if pack.count is not None else Decimal(1)
    if pack.content is None or pack.content_base != base_unit:
        return None
    return (pack.count or Decimal(1)) * pack.content
