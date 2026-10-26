"""Unit tests for unit codes, printed units and base-unit conversion."""

from decimal import Decimal

import pytest

from wareneinsatz.units import code_for_printed_unit, is_known, to_base


@pytest.mark.parametrize(
    ("printed", "code"),
    [
        ("kg", "KGM"),
        ("KG", "KGM"),
        ("St", "H87"),
        ("Stk.", "H87"),
        ("Krt", "XCT"),
        ("Kiste", "XCR"),
        ("Fl", "XBO"),
        ("Kanister", "XJC"),
        ("KGM", "KGM"),  # already a code
        ("Schale", None),  # not recognised
        ("", None),
    ],
)
def test_printed_units_map_to_codes(printed, code):
    assert code_for_printed_unit(printed) == code


def test_known_codes():
    assert is_known("KGM") and is_known("XCT") and is_known("LS")
    assert not is_known("ABC") and not is_known(None)


def test_quantities_convert_to_base_units():
    assert to_base(Decimal("2.5"), "KGM") == (Decimal("2500.0"), "g")
    assert to_base(Decimal("6"), "CLT") == (Decimal("60"), "ml")
    assert to_base(Decimal("2"), "DZN") == (Decimal("24"), "piece")


def test_package_units_have_no_fixed_size():
    """How many grams are in a carton depends on the product (supplier product mapping, step 6)."""
    assert to_base(Decimal("3"), "XCT") is None
    assert to_base(Decimal("1"), "LS") is None
