"""Field-level scores: how many fields did an extractor get right?

For every field of every document, the predicted value is compared with the truth:

    TP  both present and equal
    FP  predicted, but wrong or not on the document
    FN  on the document, but missing or wrong in the prediction

A wrong value counts as both FP and FN (the usual convention for key information extraction).
Fields that are empty in truth and prediction are not counted.

    precision = TP / (TP + FP)   of the values the extractor gave, how many were right?
    recall    = TP / (TP + FN)   of the values on the documents, how many were found?
    F1        = harmonic mean of the two

Invoice lines are matched by content (description, then amount), not by position, so one missed
line doesn't make every following line count as wrong.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wareneinsatz.names import normalise_name
from wareneinsatz.units import UNITS

DESCRIPTION_SIMILARITY = 0.9  # descriptions count as equal from this similarity on (OCR noise, spacing)
MATCH_THRESHOLD = 0.5  # below this similarity two lines are not considered the same line


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _compact(value: Any) -> str:
    """Identifiers: spaces don't matter ('DE 123 456 789' = 'DE123456789')."""
    return re.sub(r"\s+", "", str(value)).casefold()


def similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, _text(a), _text(b)).ratio()


def _zero_is_empty(value: Decimal | None) -> Decimal | None:
    """A line discount of 0 means 'no discount': don't count it as a found value."""
    return None if value in (None, Decimal("0")) else value


# Field name -> (how to read it, how to compare it)
Getter = Callable[[Any], Any]
Comparer = Callable[[Any, Any], bool]


def exact(truth: Any, pred: Any) -> bool:
    """Decimals and dates: Decimal('7') == Decimal('7.00')."""
    return truth == pred


def identifier(truth: Any, pred: Any) -> bool:
    return _compact(truth) == _compact(pred)


def same_name(truth: Any, pred: Any) -> bool:
    return normalise_name(str(truth)) == normalise_name(str(pred))


def similar_description(truth: Any, pred: Any) -> bool:
    return similarity(truth, pred) >= DESCRIPTION_SIMILARITY


def same_unit(truth: Any, pred: Any) -> bool:
    """Unit codes: equal, or both "one piece". H87, C62, EA and XPP are all printed as "St", so the
    document can't tell them apart; the KoSIT XML uses XPP and C62 where a reader sees "St" (H87)."""
    if identifier(truth, pred):
        return True
    a, b = UNITS.get(str(truth).upper()), UNITS.get(str(pred).upper())
    return bool(a and b and a.base == b.base == "piece" and a.factor == b.factor == 1)


HEADER_FIELDS: dict[str, tuple[Getter, Comparer]] = {
    "invoice_number": (lambda d: d.invoice_number, identifier),
    "issue_date": (lambda d: d.issue_date, exact),
    "delivery_date": (lambda d: d.delivery_date, exact),
    "supplier_name": (lambda d: d.supplier.name if d.supplier else None, same_name),
    "supplier_vat_id": (lambda d: d.supplier.vat_id if d.supplier else None, identifier),
    "supplier_tax_number": (lambda d: d.supplier.tax_number if d.supplier else None, identifier),
    "net_total": (lambda d: d.net_total, exact),
    "vat_total": (lambda d: d.vat_total, exact),
    "gross_total": (lambda d: d.gross_total, exact),
}
LINE_FIELDS: dict[str, tuple[Getter, Comparer]] = {
    "description": (lambda line: line.description, similar_description),
    "seller_article_no": (lambda line: line.seller_article_no, identifier),
    "quantity": (lambda line: line.quantity, exact),
    "unit_code": (lambda line: line.unit_code, same_unit),
    "unit_price": (lambda line: line.unit_price, exact),
    "allowance_amount": (lambda line: _zero_is_empty(line.allowance_amount), exact),
    "line_amount": (lambda line: line.line_amount, exact),
    "vat_rate": (lambda line: line.vat_rate, exact),
}


@dataclass
class FieldCounts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, other: FieldCounts) -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return 0.0 if (self.fp or self.fn) else None
        return 2 * p * r / (p + r)


@dataclass
class DocumentScore:
    """Counts per field for one document, plus whether every field was right."""

    fields: dict[str, FieldCounts] = field(default_factory=dict)

    @property
    def all_correct(self) -> bool:
        return all(c.fp == 0 and c.fn == 0 for c in self.fields.values())

    def header_correct(self) -> bool:
        return all(
            self.fields[name].fp == 0 and self.fields[name].fn == 0 for name in HEADER_FIELDS if name in self.fields
        )


def compare_value(truth: Any, pred: Any, comparer: Comparer) -> FieldCounts:
    if truth is None and pred is None:
        return FieldCounts()
    if truth is None:
        return FieldCounts(fp=1)
    if pred is None:
        return FieldCounts(fn=1)
    return FieldCounts(tp=1) if comparer(truth, pred) else FieldCounts(fp=1, fn=1)


def align_lines(truth_lines: list, pred_lines: list) -> list[tuple[Any | None, Any | None]]:
    """Pair truth and predicted lines by content. Greedy: best-matching pairs first."""
    scored = []
    for i, t in enumerate(truth_lines):
        for j, p in enumerate(pred_lines):
            score = similarity(t.description, p.description) + (0.5 if t.line_amount == p.line_amount else 0.0)
            if score >= MATCH_THRESHOLD:
                scored.append((score, i, j))
    pairs: list[tuple[Any | None, Any | None]] = []
    used_truth: set[int] = set()
    used_pred: set[int] = set()
    for _score, i, j in sorted(scored, key=lambda s: (-s[0], s[1], s[2])):
        if i not in used_truth and j not in used_pred:
            used_truth.add(i)
            used_pred.add(j)
            pairs.append((truth_lines[i], pred_lines[j]))
    pairs += [(t, None) for i, t in enumerate(truth_lines) if i not in used_truth]
    pairs += [(None, p) for j, p in enumerate(pred_lines) if j not in used_pred]
    return pairs


def score_document(truth: Any, pred: Any | None) -> DocumentScore:
    """Compare a prediction (InvoiceData, possibly partial, or None) with the truth."""
    score = DocumentScore({name: FieldCounts() for name in [*HEADER_FIELDS, *LINE_FIELDS]})
    for name, (get, comparer) in HEADER_FIELDS.items():
        score.fields[name].add(compare_value(get(truth), get(pred) if pred is not None else None, comparer))
    pred_lines = list(getattr(pred, "lines", None) or []) if pred is not None else []
    for t_line, p_line in align_lines(list(truth.lines), pred_lines):
        for name, (get, comparer) in LINE_FIELDS.items():
            t_value = get(t_line) if t_line is not None else None
            p_value = get(p_line) if p_line is not None else None
            score.fields[name].add(compare_value(t_value, p_value, comparer))
    return score


def total(counts: list[FieldCounts]) -> FieldCounts:
    result = FieldCounts()
    for c in counts:
        result.add(c)
    return result


def summarise(scores: list[DocumentScore]) -> dict[str, FieldCounts]:
    """Sum per field over many documents."""
    sums: dict[str, FieldCounts] = {name: FieldCounts() for name in [*HEADER_FIELDS, *LINE_FIELDS]}
    for score in scores:
        for name, counts in score.fields.items():
            sums[name].add(counts)
    return sums


def micro(sums: dict[str, FieldCounts], names: list[str] | dict) -> FieldCounts:
    """Micro-average: add up TP/FP/FN of several fields, then compute the scores once."""
    return total([sums[name] for name in names])
