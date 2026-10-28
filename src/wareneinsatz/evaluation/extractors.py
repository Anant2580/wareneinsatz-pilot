"""Extractors that the harness can evaluate. Each turns document bytes into InvoiceData, or None.

    oracle             returns the correct answer itself: checks that the harness scores 100 %
    einvoice           the exact step 2 reader: perfect on XML/ZUGFeRD, nothing on other PDFs
    pdf-text-baseline  hand-written rules on the PDF text layer (no OCR, no AI)
    llm-text           step 5: PDF text layer or OCR text -> language model
    llm-vision         step 5: page images -> vision language model
    llm-hybrid         step 5: images and text -> vision language model

The llm-* extractors are built from the settings (.env) with optional overrides, e.g. another
model: get_extractor("llm-hybrid", model="qwen3-vl:4b-instruct").
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from wareneinsatz.config import Settings, get_settings
from wareneinsatz.evaluation.baseline import extract_with_rules
from wareneinsatz.ingest import ExtractionError, NeedsVisualExtraction, extract_invoice
from wareneinsatz.schemas import InvoiceData

if TYPE_CHECKING:
    from wareneinsatz.reading import DocumentReader


@dataclass(frozen=True)
class Extractor:
    name: str
    description: str
    run: Callable[[bytes], InvoiceData | None]
    uses_truth: bool = False  # only the oracle: the harness hands it the correct answer
    settings: dict[str, Any] = field(default_factory=dict)  # model, OCR engine ...: printed in the report
    label: str | None = None  # report file name part; defaults to the name
    details: Callable[[], dict[str, Any] | None] | None = None  # facts about the last run (tokens, timings)


def _einvoice(content: bytes) -> InvoiceData | None:
    try:
        return extract_invoice(content).data
    except (ExtractionError, NeedsVisualExtraction, ValidationError):
        return None


def _not_used(content: bytes) -> InvoiceData | None:  # the oracle's answer comes from the harness
    raise NotImplementedError


EXTRACTORS: dict[str, Extractor] = {
    extractor.name: extractor
    for extractor in [
        Extractor("oracle", "returns the correct answer (checks the harness itself)", _not_used, uses_truth=True),
        Extractor("einvoice", "exact XRechnung / ZUGFeRD reader from step 2", _einvoice),
        Extractor("pdf-text-baseline", "rules on the PDF text layer, no OCR, no AI", extract_with_rules),
    ]
}
READER_EXTRACTORS = {"llm-text": "text", "llm-vision": "vision", "llm-hybrid": "hybrid"}
ALL_NAMES = [*EXTRACTORS, *READER_EXTRACTORS]


def reader_extractor(reader: DocumentReader) -> Extractor:
    """Wrap a step 5 reader. Partial results are kept, so the fields that were read still count."""
    last: dict[str, Any] = {}

    def run(content: bytes) -> InvoiceData:
        last.clear()
        reading = reader.read(content)
        last.update(reading.details, valid=reading.valid, problems=reading.problems[:5])
        return reading.data

    model = re.sub(r"[^A-Za-z0-9.]+", "-", reader.llm.model).strip("-")
    return Extractor(
        name=reader.method,
        description=f"OCR + language model, {reader.mode.value} mode",
        run=run,
        settings=reader.describe(),
        label=f"{reader.method}_{model}",
        details=lambda: dict(last) or None,
    )


def get_extractor(name: str, settings: Settings | None = None, **overrides: str | None) -> Extractor:
    """An extractor by name. llm-* ones check first that the model server is up (LlmUnavailable if not)."""
    if name in EXTRACTORS:
        return EXTRACTORS[name]
    if name in READER_EXTRACTORS:
        from wareneinsatz.reading import make_reader  # only needed (and only imported) for step 5 runs

        reader = make_reader(settings or get_settings(), mode=READER_EXTRACTORS[name], **overrides)
        reader.llm.check()
        return reader_extractor(reader)
    raise ValueError(f"Unknown extractor {name!r}; choose one of {', '.join(ALL_NAMES)}")
