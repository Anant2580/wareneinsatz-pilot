"""The evaluation harness: run one extractor over a dataset, score every field, write a report.

    report = evaluate(Path("eval/dataset"), "pdf-text-baseline")
    write_report(report, Path("eval/results"))   # Markdown to read, JSON to compare runs

Besides field scores it measures what matters in operation: how many documents got a result at
all, how many were completely correct, how many would need a person (the step 3 validation finds
an error), and the time per document.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from wareneinsatz import __version__
from wareneinsatz.evaluation.dataset import Dataset, DatasetItem, load_dataset
from wareneinsatz.evaluation.extractors import EXTRACTORS
from wareneinsatz.evaluation.metrics import (
    HEADER_FIELDS,
    LINE_FIELDS,
    DocumentScore,
    FieldCounts,
    micro,
    score_document,
    summarise,
)
from wareneinsatz.validation import validate_invoice


@dataclass
class ItemResult:
    item: DatasetItem
    predicted: bool  # did the extractor return anything?
    seconds: float
    score: DocumentScore
    needs_review: bool | None  # validation found an error in the prediction (None: no prediction)
    error: str | None = None  # an exception inside the extractor


@dataclass
class EvaluationReport:
    extractor: str
    dataset: Dataset
    results: list[ItemResult]
    created: datetime = field(default_factory=lambda: datetime.now(UTC))

    def groups(self) -> dict[str, list[ItemResult]]:
        """Results per document group, e.g. 'synthetic · photo · receipt'."""
        groups: dict[str, list[ItemResult]] = {}
        for result in self.results:
            item = result.item
            key = f"{item.source} · {item.kind} · {item.template}"
            groups.setdefault(key, []).append(result)
        return dict(sorted(groups.items()))

    def summary(self) -> dict[str, Any]:
        """Everything in the report, JSON-ready."""
        sums = summarise([r.score for r in self.results])
        predicted = [r for r in self.results if r.predicted]
        times = sorted(r.seconds for r in self.results)
        return {
            "extractor": self.extractor,
            "dataset": {"name": self.dataset.name, "seed": self.dataset.seed, "documents": len(self.results)},
            "created": self.created.isoformat(timespec="seconds"),
            "version": __version__,
            "scores": {
                "all_fields": _scores(micro(sums, [*HEADER_FIELDS, *LINE_FIELDS])),
                "header_fields": _scores(micro(sums, HEADER_FIELDS)),
                "line_fields": _scores(micro(sums, LINE_FIELDS)),
            },
            "fields": {name: {"tp": c.tp, "fp": c.fp, "fn": c.fn, **_scores(c)} for name, c in sums.items()},
            "documents": {
                "with_result": len(predicted),
                "fully_correct": sum(r.score.all_correct for r in self.results),
                "needs_review": sum(bool(r.needs_review) for r in predicted),
                "errors": sum(r.error is not None for r in self.results),
            },
            "seconds_per_document": {
                "median": statistics.median(times) if times else None,
                "p95": times[max(0, int(len(times) * 0.95) - 1)] if times else None,
                "mean": statistics.fmean(times) if times else None,
            },
            "groups": {
                name: {
                    "documents": len(results),
                    "with_result": sum(r.predicted for r in results),
                    "fully_correct": sum(r.score.all_correct for r in results),
                    **_scores(micro(summarise([r.score for r in results]), [*HEADER_FIELDS, *LINE_FIELDS])),
                }
                for name, results in self.groups().items()
            },
            "items": [
                {
                    "id": r.item.id,
                    "sha256": r.item.sha256,
                    "predicted": r.predicted,
                    "f1": micro(r.score.fields, [*HEADER_FIELDS, *LINE_FIELDS]).f1,
                    "fully_correct": r.score.all_correct,
                    "needs_review": r.needs_review,
                    "seconds": round(r.seconds, 4),
                    "error": r.error,
                }
                for r in self.results
            ],
        }


def evaluate(dataset_dir: Path, extractor_name: str, *, today: date | None = None) -> EvaluationReport:
    if extractor_name not in EXTRACTORS:
        raise ValueError(f"Unknown extractor {extractor_name!r}; choose one of {', '.join(EXTRACTORS)}")
    extractor = EXTRACTORS[extractor_name]
    dataset = load_dataset(dataset_dir)
    results = []
    for item in dataset.items:
        truth = dataset.load_truth(dataset_dir, item)
        content = (dataset_dir / item.document).read_bytes()
        error = None
        started = time.perf_counter()
        try:
            prediction = truth if extractor.uses_truth else extractor.run(content)
        except Exception as exc:  # a crash on one document is a result, not the end of the run
            prediction, error = None, f"{type(exc).__name__}: {exc}"
        seconds = time.perf_counter() - started
        results.append(
            ItemResult(
                item=item,
                predicted=prediction is not None,
                seconds=seconds,
                score=score_document(truth, prediction),
                needs_review=_needs_review(prediction, today),
                error=error,
            )
        )
    return EvaluationReport(extractor_name, dataset, results)


def _needs_review(prediction: Any, today: date | None) -> bool | None:
    """Would the step 3 validation send this prediction to a person? Partial results always would."""
    if prediction is None:
        return None
    try:
        return not validate_invoice(prediction, today=today).ok
    except Exception:  # an incomplete prediction can't even be checked
        return True


def _scores(counts: FieldCounts) -> dict[str, float | None]:
    return {"precision": counts.precision, "recall": counts.recall, "f1": counts.f1}


# --- report files ---------------------------------------------------------------------------------------


def write_report(report: EvaluationReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report.created:%Y-%m-%d}_{report.extractor}_{report.dataset.name}"
    markdown, data = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    markdown.write_text(render_markdown(report), encoding="utf-8")
    data.write_text(json.dumps(report.summary(), indent=2, ensure_ascii=False), encoding="utf-8")
    return markdown, data


def render_markdown(report: EvaluationReport) -> str:
    s = report.summary()
    n = s["dataset"]["documents"]
    docs = s["documents"]
    timing = s["seconds_per_document"]
    out = [
        f"# Evaluation: {s['extractor']}",
        "",
        f"Dataset **{s['dataset']['name']}** (seed {s['dataset']['seed']}), {n} documents · "
        f"run {s['created']} · wareneinsatz {s['version']}",
        "",
        "| Fields | Precision | Recall | F1 |",
        "|---|---:|---:|---:|",
    ]
    for label, key in [
        ("All fields", "all_fields"),
        ("Header fields", "header_fields"),
        ("Line fields", "line_fields"),
    ]:
        sc = s["scores"][key]
        out.append(f"| {label} | {_pct(sc['precision'])} | {_pct(sc['recall'])} | {_pct(sc['f1'])} |")
    out += [
        "",
        f"- **Documents with a result:** {docs['with_result']} of {n}",
        f"- **Completely correct:** {docs['fully_correct']} of {n}",
        f"- **Would need review** (validation error in the result): {docs['needs_review']} of {docs['with_result']}",
        f"- **Time per document:** median {_ms(timing['median'])}, 95th percentile {_ms(timing['p95'])}",
    ]
    if docs["errors"]:
        out.append(f"- **Extractor crashed** on {docs['errors']} documents (see the JSON file)")
    out += [
        "",
        "## By document group",
        "",
        "| Group | Documents | With result | Completely correct | F1 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, g in s["groups"].items():
        out.append(f"| {name} | {g['documents']} | {g['with_result']} | {g['fully_correct']} | {_pct(g['f1'])} |")
    out += [
        "",
        "## By field",
        "",
        "| Field | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, f in s["fields"].items():
        scores = f"{_pct(f['precision'])} | {_pct(f['recall'])} | {_pct(f['f1'])}"
        out.append(f"| {name} | {f['tp']} | {f['fp']} | {f['fn']} | {scores} |")
    out += [
        "",
        "TP = correct value, FP = value given but wrong or not on the document, FN = value on the document but "
        "missing or wrong. A wrong value counts as FP and FN. Lines are matched by content before comparing.",
        "",
    ]
    return "\n".join(out)


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.1%}"


def _ms(seconds: float | None) -> str:
    return "–" if seconds is None else f"{seconds * 1000:.0f} ms"
