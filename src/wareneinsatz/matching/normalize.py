"""Text normalisation for matching invoice lines to ingredients.

Two keys:
- description_key(): the line as the supplier writes it, only case and spacing normalised. The
  memory uses it: the same supplier prints the same text every time.
- matching_text(): what the line *is*, without pack sizes and packaging words, for fuzzy and
  semantic matching: "HAEHNCHENBRUST TK 4x2,5kg" -> "haehnchenbrust".
"""

from __future__ import annotations

import re

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "é": "e", "ñ": "n"})

# Words about packaging, quality class or handling that say nothing about the product itself.
# fmt: off
STOP_WORDS = frozenset({
    "tk", "ca", "kl", "i", "ii", "iii", "gr", "st", "stk", "stueck", "fl", "fla", "er", "x", "vak",
    "mw", "mehrweg", "einweg", "pet", "glas", "dose", "ds", "eimer", "eim", "sack", "netz", "kiste",
    "ki", "karton", "krt", "beutel", "btl", "packung", "pck", "pack", "kanister", "kan", "pcs", "bl",
    "blatt", "m", "l", "g", "kg", "ml", "lose", "stueckig", "a", "b", "und", "mit", "in", "im"
})
# fmt: on


def description_key(description: str) -> str:
    """'Rapsöl  10 l Kanister ' -> 'rapsöl 10 l kanister'."""
    return re.sub(r"\s+", " ", description).strip().casefold()


def matching_text(text: str) -> str:
    """Lower case, umlauts spelled out (suppliers write both), numbers, units and packaging words removed."""
    cleaned = text.casefold().translate(_UMLAUTS)
    cleaned = re.sub(r"\d+(?:[.,]\d+)?\s*(?:%|x|er\b)?", " ", cleaned)  # sizes, counts, percentages
    cleaned = re.sub(r"[^a-z]+", " ", cleaned)  # punctuation, slashes, hyphens
    words = [word for word in cleaned.split() if word not in STOP_WORDS and len(word) > 1]
    return " ".join(words)
