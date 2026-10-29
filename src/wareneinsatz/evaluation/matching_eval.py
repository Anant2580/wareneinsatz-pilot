"""How well are invoice lines matched to ingredients? (step 6)

Two measurements:

1. The matching set (matching_set.json): 179 hand-written line texts as different suppliers
   print them ("HAEHNCHENBRUST TK 4x2,5kg", "Onion Rings Beer Batter 1kg"), with the correct
   ingredient, or none for 14 products that are not on the master list. Per method:
   - top-1 accuracy: is the best candidate the right ingredient?
   - at the threshold: suggestions shown, how many of them are right (precision), how many
     products got a right suggestion (recall), how many unknown products got none
   Thresholds were chosen on the 'dev' half, so the 'test' half is an honest number.
   Also the pack sizes: is the amount per invoice unit (10,000 g for "4 x 2,5 kg") right?

2. The memory over time: the synthetic invoices of the evaluation set in date order. A
   supplier product is confirmed by a person the first time it appears; after that the memory
   finds it (here by supplier + description, because the generator's article numbers are
   random). Result: how many confirmations a restaurant needs for how many lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from wareneinsatz import __version__
from wareneinsatz.matching.embeddings import Embedder
from wareneinsatz.matching.matcher import THRESHOLDS, IngredientInfo, Matcher, Method
from wareneinsatz.matching.normalize import description_key
from wareneinsatz.matching.packs import base_quantity_per_unit
from wareneinsatz.names import normalise_name
from wareneinsatz.schemas import InvoiceData
from wareneinsatz.units import code_for_printed_unit


@dataclass(frozen=True)
class MatchItem:
    id: int
    description: str
    unit: str
    ingredient: str | None  # None: not on the master list
    split: str  # "dev" or "test"
    base_qty: Decimal | None = None
    scores_pack: bool = False  # False: the base quantity is not part of the truth


def load_matching_set(path: Path | None = None) -> list[MatchItem]:
    if path is None:
        text = resources.files("wareneinsatz.evaluation").joinpath("matching_set.json").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    items = []
    for raw in json.loads(text)["items"]:
        qty = raw.get("base_qty")
        items.append(
            MatchItem(
                id=raw["id"],
                description=raw["description"],
                unit=raw["unit"],
                ingredient=raw["ingredient"],
                split=raw["split"],
                base_qty=Decimal(qty) if qty is not None else None,
                scores_pack="base_qty" in raw,
            )
        )
    return items


@dataclass
class MethodScores:
    method: str
    threshold: float
    splits: dict[str, dict[str, Any]] = field(default_factory=dict)
    misses: list[dict[str, Any]] = field(default_factory=list)  # test items with a wrong best candidate


def score_method(matcher: Matcher, items: list[MatchItem]) -> MethodScores:
    """Run one matcher over every item; scores per split ('dev', 'test', 'all')."""
    matches = matcher.match_many([item.description for item in items])
    result = MethodScores(method=matcher.method.value, threshold=matcher.threshold)
    for split in ("dev", "test", "all"):
        pairs = [(i, m) for i, m in zip(items, matches, strict=True) if split == "all" or i.split == split]
        known = [(i, m) for i, m in pairs if i.ingredient]
        unknown = [(i, m) for i, m in pairs if not i.ingredient]
        top1 = sum(bool(m.candidates) and m.candidates[0].key == i.ingredient for i, m in known)
        suggested = [(i, m) for i, m in pairs if m.key is not None]
        right = sum(m.key == i.ingredient for i, m in suggested)
        result.splits[split] = {
            "items": len(pairs),
            "known": len(known),
            "unknown": len(unknown),
            "top1_accuracy": _ratio(top1, len(known)),
            "suggestions": len(suggested),
            "precision": _ratio(right, len(suggested)),  # suggestions that are right
            "recall": _ratio(right, len(known)),  # known products with a right suggestion
            "unknown_without_suggestion": _ratio(sum(m.key is None for _, m in unknown), len(unknown)),
        }
    result.misses = [
        {"description": i.description, "expected": i.ingredient, "best": m.candidates[0].key, "score": m.score}
        for i, m in zip(items, matches, strict=True)
        if i.split == "test" and i.ingredient and m.candidates and m.candidates[0].key != i.ingredient
    ]
    return result


def score_packs(items: list[MatchItem], ingredients: dict[str, IngredientInfo]) -> dict[str, Any]:
    scored = [item for item in items if item.scores_pack and item.ingredient]
    wrong = []
    for item in scored:
        got = base_quantity_per_unit(
            item.description, code_for_printed_unit(item.unit), ingredients[item.ingredient].base_unit
        )
        if got != item.base_qty:
            wrong.append({"description": item.description, "expected": item.base_qty, "got": got})
    return {"items": len(scored), "correct": len(scored) - len(wrong), "wrong": wrong}


def simulate_memory(invoices: list[InvoiceData], truth: dict[str, str], matcher: Matcher) -> dict[str, Any]:
    """Process invoices in date order. Unknown supplier products need one confirmation; then the memory knows them.

    truth maps a line description to its ingredient key. The matcher learns every confirmation.
    """
    memory: dict[tuple[str, str], str] = {}
    lines = confirmations = right_suggestions = no_suggestion = 0
    for invoice in sorted(invoices, key=lambda inv: (inv.issue_date, inv.invoice_number or "")):
        supplier = normalise_name(invoice.supplier.name)
        for line in invoice.lines:
            lines += 1
            # By description: the generator draws a new random article number for every line, real suppliers don't.
            key = (supplier, description_key(line.description))
            if key in memory:
                continue  # found automatically
            confirmations += 1
            expected = truth[line.description]
            match = matcher.match(line.description)
            right_suggestions += match.key == expected
            no_suggestion += match.key is None
            memory[key] = expected
            matcher.learn(expected, line.description)
    return {
        "invoices": len(invoices),
        "lines": lines,
        "confirmations": confirmations,
        "automatic": lines - confirmations,
        "right_suggestions": right_suggestions,
        "no_suggestion": no_suggestion,
    }


@dataclass
class MatchingReport:
    methods: list[MethodScores]
    packs: dict[str, Any]
    memory: dict[str, Any] | None
    settings: dict[str, Any]
    created: datetime = field(default_factory=lambda: datetime.now(UTC))

    def summary(self) -> dict[str, Any]:
        return {
            "created": self.created.isoformat(timespec="seconds"),
            "version": __version__,
            "settings": self.settings,
            "methods": [
                {"method": m.method, "threshold": m.threshold, **m.splits, "test_misses": m.misses}
                for m in self.methods
            ],  # fmt: skip
            "packs": {**self.packs, "wrong": [{k: str(v) for k, v in w.items()} for w in self.packs["wrong"]]},
            "memory": self.memory,
        }


def evaluate_matching(
    ingredients: list[IngredientInfo],
    items: list[MatchItem],
    *,
    embedder: Embedder | None = None,
    invoices: list[InvoiceData] | None = None,
    truth: dict[str, str] | None = None,
) -> MatchingReport:
    methods = [Method.FUZZY] + ([Method.EMBEDDING, Method.HYBRID] if embedder else [])
    scores = [score_method(Matcher(ingredients, embedder=embedder, method=m), items) for m in methods]
    memory = None
    if invoices and truth is not None:
        memory = simulate_memory(invoices, truth, Matcher(ingredients, embedder=embedder))
    settings = {
        "ingredients": len(ingredients),
        "embedding_model": embedder.name if embedder else None,
        "thresholds": {m.value: THRESHOLDS[m] for m in methods},
    }
    return MatchingReport(scores, score_packs(items, {i.key: i for i in ingredients}), memory, settings)


def write_matching_report(report: MatchingReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    model = (report.settings["embedding_model"] or "fuzzy-only").replace("ollama:", "").replace(":", "-")
    stem = f"{report.created:%Y-%m-%d}_matching_{model}"
    markdown, data = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    markdown.write_text(render_matching_markdown(report), encoding="utf-8")
    data.write_text(json.dumps(report.summary(), indent=2, ensure_ascii=False), encoding="utf-8")
    return markdown, data


def render_matching_markdown(report: MatchingReport) -> str:
    s = report.summary()
    test = s["methods"][0]["test"]
    out = [
        "# Evaluation: ingredient matching",
        "",
        f"Run {s['created']} · wareneinsatz {s['version']} · {s['settings']['ingredients']} ingredients · "
        f"embedding model `{s['settings']['embedding_model'] or '-'}`",
        "",
        f"Matching set: {test['items']} test items ({test['known']} known products, {test['unknown']} not on the "
        "master list). Thresholds were chosen on the dev half; the numbers below are the test half.",
        "",
        "| Method | Threshold | Top-1 accuracy | Suggestions | Precision | Recall | Unknown without suggestion |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for m in s["methods"]:
        t = m["test"]
        out.append(
            f"| {m['method']} | {m['threshold']:.2f} | {_pct(t['top1_accuracy'])} | {t['suggestions']} | "
            f"{_pct(t['precision'])} | {_pct(t['recall'])} | {_pct(t['unknown_without_suggestion'])} |"
        )
    packs = s["packs"]
    out += ["", f"**Pack sizes:** {packs['correct']} of {packs['items']} amounts per invoice unit right."]
    if memory := s["memory"]:
        out += [
            "",
            f"**Memory over time:** {memory['lines']} lines on {memory['invoices']} invoices needed "
            f"{memory['confirmations']} confirmations; {memory['automatic']} were matched automatically from "
            f"memory. The suggestion was right for {memory['right_suggestions']} of the {memory['confirmations']} "
            "confirmations.",
        ]
    misses = s["methods"][-1]["test_misses"]
    if misses:
        out += ["", f"Wrong best candidate ({s['methods'][-1]['method']}, test half):", ""]
        out += [f"- {m['description']}: {m['best']} instead of {m['expected']} ({m['score']:.2f})" for m in misses]
    out.append("")
    return "\n".join(out)


def _ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.1%}"
