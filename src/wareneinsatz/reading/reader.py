"""The document reader: a PDF or photo in, InvoiceData out, by OCR and a language model.

Three modes, compared on the evaluation set (eval/README.md):

    text    the model gets text only: the PDF's text layer, or OCR text for photos and scans
    vision  the model gets the page images only and reads them itself
    hybrid  the model gets both: images for the layout, text for exact digits

    reader = DocumentReader(OllamaClient(model="qwen3-vl:8b-instruct"), Mode.HYBRID, TesseractOcr())
    reading = reader.read(content)      # reading.data is InvoiceData; reading.printed is the raw reply

Anything read this way is shown to a person before it counts (decision 16): the pipeline never
approves it automatically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from wareneinsatz.ingest.errors import ExtractionError
from wareneinsatz.reading.interpret import interpret
from wareneinsatz.reading.llm import LlmClient
from wareneinsatz.reading.ocr import OcrEngine
from wareneinsatz.reading.pages import MODEL_IMAGE_SIDE, encode_for_model, load_pages
from wareneinsatz.reading.printed import PROMPT_VERSION, SYSTEM_PROMPT, PrintedInvoice, user_message
from wareneinsatz.schemas import InvoiceData

SCHEMA = PrintedInvoice.model_json_schema()  # the structured-output schema sent with every request


class Mode(StrEnum):
    TEXT = "text"
    VISION = "vision"
    HYBRID = "hybrid"


@dataclass
class Reading:
    data: InvoiceData  # valid if `valid`, otherwise a partial object (scored, never stored)
    valid: bool
    problems: list[str]  # why the result is not a valid invoice
    printed: dict[str, Any]  # the model's reply, untouched: stored as documents.raw_extraction
    method: str  # e.g. "llm-hybrid"
    details: dict[str, Any] = field(default_factory=dict)  # model, OCR engine, timings, tokens


class DocumentReader:
    def __init__(
        self,
        llm: LlmClient,
        mode: Mode = Mode.HYBRID,
        ocr: OcrEngine | None = None,
        *,
        image_side: int = MODEL_IMAGE_SIDE,
    ):
        self.llm = llm
        self.mode = Mode(mode)
        self.ocr = ocr
        self.image_side = image_side

    @property
    def method(self) -> str:
        return f"llm-{self.mode.value}"

    def describe(self) -> dict[str, Any]:
        """The settings that produced a result: stored with it, printed in reports."""
        return {
            "method": self.method,
            "model": self.llm.name,
            "ocr": self.ocr.name if self.ocr and self.mode is not Mode.VISION else None,
            "image_side": self.image_side if self.mode is not Mode.TEXT else None,
            "prompt_version": PROMPT_VERSION,
        }

    def read(self, content: bytes) -> Reading:
        started = time.perf_counter()
        pages = load_pages(content)

        texts: list[tuple[str, str | None]] = []
        ocr_seconds, ocr_confidences = 0.0, []
        if self.mode in (Mode.TEXT, Mode.HYBRID):
            for page in pages:
                if page.text:
                    texts.append(("pdf", page.text))
                elif self.ocr is not None:
                    ocr_started = time.perf_counter()
                    result = self.ocr.read(page.image, is_photo=page.is_photo)
                    ocr_seconds += time.perf_counter() - ocr_started
                    texts.append(("ocr", result.text))
                    if result.confidence is not None:
                        ocr_confidences.append(result.confidence)
        if self.mode is Mode.TEXT and not any(text for _, text in texts):
            raise ExtractionError("No text to read: the file has no text layer and no OCR engine is set up")
        images = [encode_for_model(page.image, self.image_side) for page in pages] if self.mode is not Mode.TEXT else []

        reply = self.llm.complete(
            system=SYSTEM_PROMPT,
            user=user_message(texts, with_images=bool(images)),
            images=images,
            schema=SCHEMA,
        )
        try:
            printed = PrintedInvoice.model_validate_json(reply.content)
        except ValidationError as error:
            first = error.errors()[0]
            raise ExtractionError(
                f"The model's reply does not match the schema ({'.'.join(map(str, first['loc']))}: {first['msg']})"
            ) from error
        interpretation = interpret(printed)
        return Reading(
            data=interpretation.data,
            valid=interpretation.valid,
            problems=interpretation.problems,
            printed=printed.model_dump(),
            method=self.method,
            details={
                **self.describe(),
                "pages": len(pages),
                "text_sources": [source for source, text in texts if text],
                "ocr_confidence": min(ocr_confidences) if ocr_confidences else None,
                "seconds": round(time.perf_counter() - started, 3),
                "ocr_seconds": round(ocr_seconds, 3),
                "llm_seconds": round(reply.seconds, 3),
                "prompt_tokens": reply.prompt_tokens,
                "output_tokens": reply.output_tokens,
                "tokens_per_second": round(reply.tokens_per_second, 1) if reply.tokens_per_second else None,
            },
        )
