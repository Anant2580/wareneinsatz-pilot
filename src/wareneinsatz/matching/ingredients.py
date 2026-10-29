"""The ingredient master list: what the restaurant costs its recipes in (data/ingredients.json).

    {"key": "pommes", "name": "Pommes Frites", "base_unit": "g", "category": "frozen",
     "aliases": ["Pommes", "French fries"], "is_food": true}

base_unit is the unit prices are compared in: g, ml or piece. is_food false marks deposits
(Pfand) and consumables, which are bought and matched but are not part of the food cost.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from wareneinsatz.matching.matcher import IngredientInfo

DEFAULT_FILE = Path("data/ingredients.json")


class IngredientEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")  # stable id used in recipes, e.g. "patty-rind-180"
    name: str = Field(min_length=1)
    base_unit: str = Field(pattern=r"^(g|ml|piece)$")
    category: str | None = None
    is_food: bool = True
    aliases: list[str] = Field(default_factory=list)


def read_ingredient_file(path: Path = DEFAULT_FILE) -> list[IngredientEntry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = [IngredientEntry.model_validate(item) for item in data["ingredients"]]
    keys = [entry.key for entry in entries]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"Duplicate ingredient keys in {path}: {', '.join(duplicates)}")
    return entries


def to_info(entries: list[IngredientEntry]) -> list[IngredientInfo]:
    return [IngredientInfo(e.key, e.name, e.base_unit, e.is_food, tuple(e.aliases)) for e in entries]
