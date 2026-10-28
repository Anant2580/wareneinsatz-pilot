"""Units of measure as used on invoices: UN/ECE Recommendation 20 and 21 codes (EN 16931 BT-130).

Three jobs:
1. Validation (step 3): is a unit code one we know?
2. Normalising (step 5): map units printed on receipts ("kg", "Krt", "St") to codes.
3. Price comparison (step 6): convert quantities to base units (g, ml, piece), so that
   "10 KGM" and "10000 GRM" mean the same amount of beef.

Package units (carton, crate, bottle ...) have no fixed size: how many grams are in a carton
depends on the product, so their base amount comes from the supplier product mapping (step 6).
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Unit:
    code: str
    name: str
    base: str | None = None  # "g", "ml" or "piece"; None if the size depends on the product
    factor: Decimal | None = None  # base units in one of this unit (KGM -> 1000 g)


def _units(*units: Unit) -> dict[str, Unit]:
    return {unit.code: unit for unit in units}


UNITS = _units(
    # Mass
    Unit("KGM", "kilogram", "g", Decimal("1000")),
    Unit("GRM", "gram", "g", Decimal("1")),
    Unit("MGM", "milligram", "g", Decimal("0.001")),
    Unit("TNE", "tonne", "g", Decimal("1000000")),
    # Volume
    Unit("LTR", "litre", "ml", Decimal("1000")),
    Unit("DLT", "decilitre", "ml", Decimal("100")),
    Unit("CLT", "centilitre", "ml", Decimal("10")),
    Unit("MLT", "millilitre", "ml", Decimal("1")),
    Unit("MTQ", "cubic metre", "ml", Decimal("1000000")),
    # Counted items
    Unit("H87", "piece", "piece", Decimal("1")),
    Unit("C62", "one", "piece", Decimal("1")),
    Unit("EA", "each", "piece", Decimal("1")),
    Unit("XPP", "piece", "piece", Decimal("1")),
    Unit("DZN", "dozen", "piece", Decimal("12")),
    # Packages (Rec 21 codes, prefixed with X): size depends on the product
    Unit("XBX", "box"),
    Unit("XCT", "carton"),
    Unit("XCR", "crate"),
    Unit("XBC", "bottle crate"),
    Unit("XCS", "case"),
    Unit("XPK", "package"),
    Unit("XPA", "packet"),
    Unit("XBO", "bottle"),
    Unit("XCA", "can, rectangular"),
    Unit("XCX", "can, cylindrical"),
    Unit("XTN", "tin"),
    Unit("XBG", "bag"),
    Unit("XSA", "sack"),
    Unit("XNT", "net"),
    Unit("XJR", "jar"),
    Unit("XJC", "jerrican"),
    Unit("XBJ", "bucket"),
    Unit("XTU", "tube"),
    Unit("XKG", "keg"),
    Unit("XBA", "barrel"),
    Unit("XPX", "pallet"),
    # Services and other units that appear on supplier invoices (delivery, rent, energy ...)
    Unit("LS", "lump sum"),
    Unit("SET", "set"),
    Unit("HUR", "hour"),
    Unit("DAY", "day"),
    Unit("WEE", "week"),
    Unit("MON", "month"),
    Unit("ANN", "year"),
    Unit("KWH", "kilowatt hour"),
    Unit("KWT", "kilowatt"),
    Unit("MTR", "metre"),
    Unit("KMT", "kilometre"),
    Unit("MTK", "square metre"),
)

# Units as printed on German receipts and wholesaler invoices (compared in lower case, dots removed).
PRINTED_UNITS = {
    **dict.fromkeys(["kg", "kilo", "kilogramm"], "KGM"),
    **dict.fromkeys(["g", "gr", "gramm"], "GRM"),
    **dict.fromkeys(["l", "ltr", "liter", "litre"], "LTR"),
    **dict.fromkeys(["cl"], "CLT"),
    **dict.fromkeys(["ml"], "MLT"),
    **dict.fromkeys(["st", "stk", "stck", "stück", "stueck", "pc", "pcs", "piece", "ea"], "H87"),
    **dict.fromkeys(["dz", "dtzd", "dutzend"], "DZN"),
    **dict.fromkeys(["krt", "kt", "ktn", "karton", "crt"], "XCT"),
    **dict.fromkeys(["ki", "kis", "kiste", "kst", "ka", "kasten"], "XCR"),
    **dict.fromkeys(["fl", "fla", "flasche"], "XBO"),
    **dict.fromkeys(["ds", "dose"], "XCX"),
    **dict.fromkeys(["pck", "pack", "pkg", "packung", "pkt", "paket"], "XPK"),
    **dict.fromkeys(["btl", "beutel"], "XBG"),
    **dict.fromkeys(["box", "schachtel"], "XBX"),
    **dict.fromkeys(["sack"], "XSA"),
    **dict.fromkeys(["netz"], "XNT"),
    **dict.fromkeys(["gl", "glas"], "XJR"),
    **dict.fromkeys(["kan", "kanister"], "XJC"),
    **dict.fromkeys(["eim", "eimer"], "XBJ"),
    **dict.fromkeys(["tb", "tube"], "XTU"),
    **dict.fromkeys(["fass"], "XKG"),
    **dict.fromkeys(["pal", "palette"], "XPX"),
    # Services (delivery, rent, energy), as printed on service invoices
    **dict.fromkeys(["t", "to", "tonne"], "TNE"),
    **dict.fromkeys(["pausch", "pauschal", "pauschale", "psch"], "LS"),
    **dict.fromkeys(["std", "stunde", "stunden", "h"], "HUR"),
    **dict.fromkeys(["tag", "tage"], "DAY"),
    **dict.fromkeys(["woche", "wochen"], "WEE"),
    **dict.fromkeys(["monat", "monate", "mon"], "MON"),
    **dict.fromkeys(["jahr", "jahre"], "ANN"),
    **dict.fromkeys(["kwh"], "KWH"),
    **dict.fromkeys(["kw"], "KWT"),
    **dict.fromkeys(["m", "meter"], "MTR"),
    **dict.fromkeys(["km"], "KMT"),
    **dict.fromkeys(["m2", "m²", "qm"], "MTK"),
    **dict.fromkeys(["set"], "SET"),
}


def is_known(code: str | None) -> bool:
    return code in UNITS


def code_for_printed_unit(printed: str | None) -> str | None:
    """'Krt' -> 'XCT', 'kg' -> 'KGM', 'KGM' -> 'KGM'; None if the unit is not recognised."""
    if not printed:
        return None
    cleaned = printed.strip()
    if cleaned.upper() in UNITS:
        return cleaned.upper()
    return PRINTED_UNITS.get(cleaned.lower().rstrip("."))


def to_base(quantity: Decimal, code: str | None) -> tuple[Decimal, str] | None:
    """Quantity in base units: (Decimal('2.5'), 'KGM') -> (Decimal('2500.0'), 'g').

    None for package units and services, whose size depends on the product.
    """
    unit = UNITS.get(code or "")
    if unit is None or unit.base is None or unit.factor is None:
        return None
    return quantity * unit.factor, unit.base
