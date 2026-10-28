"""Extractors that the harness can evaluate. Each turns document bytes into InvoiceData, or None.

    oracle             returns the correct answer itself: checks that the harness scores 100 %
    einvoice           the exact step 2 reader: perfect on XML/ZUGFeRD, nothing on other PDFs
    pdf-text-baseline  hand-written rules on the PDF text layer (no OCR, no AI)

The OCR + LLM extractor of step 5 is added here, so all of them are measured the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from wareneinsatz.evaluation.baseline import extract_with_rules
from wareneinsatz.ingest import ExtractionError, NeedsVisualExtraction, extract_invoice
from wareneinsatz.schemas import InvoiceData


@dataclass(frozen=True)
class Extractor:
    name: str
    description: str
    run: Callable[[bytes], InvoiceData | None]
    uses_truth: bool = False  # only the oracle: the harness hands it the correct answer


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
