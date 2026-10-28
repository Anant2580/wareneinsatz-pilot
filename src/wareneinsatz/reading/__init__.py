"""Step 5: reading PDFs and photos that are not e-invoices, with OCR and a language model.

pages.py      PDF / photo -> page images and text layer
ocr.py        Tesseract with photo clean-up (deskew, denoise)
llm.py        Ollama or any OpenAI-compatible server, structured JSON output
printed.py    what the model returns (text as printed) and the prompt
interpret.py  printed text -> InvoiceData; all parsing and arithmetic in code
reader.py     the pipeline: text / vision / hybrid mode
"""

from __future__ import annotations

from wareneinsatz.config import Settings
from wareneinsatz.reading.llm import LlmClient, LlmError, LlmUnavailable, OllamaClient, OpenAICompatibleClient
from wareneinsatz.reading.ocr import OcrEngine, OcrUnavailable, TesseractOcr
from wareneinsatz.reading.reader import DocumentReader, Mode, Reading

__all__ = [
    "DocumentReader",
    "LlmError",
    "LlmUnavailable",
    "Mode",
    "OcrUnavailable",
    "Reading",
    "make_reader",
]


def make_reader(
    settings: Settings,
    *,
    mode: str | None = None,
    model: str | None = None,
    ocr: str | None = None,
    base_url: str | None = None,
) -> DocumentReader:
    """A reader from the settings (.env), with optional overrides from the command line.

    Raises OcrUnavailable if the mode needs OCR and Tesseract is missing (use ocr="none" to go without).
    """
    reading_mode = Mode(mode or settings.reading_mode)
    model_name = model or settings.llm_model
    url = base_url or settings.llm_base_url
    llm: LlmClient
    if settings.llm_api == "openai":
        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
        llm = OpenAICompatibleClient(url, model_name, api_key=key, timeout=settings.llm_timeout_seconds)
    else:
        llm = OllamaClient(url, model_name, timeout=settings.llm_timeout_seconds)
    engine: OcrEngine | None = None
    if reading_mode is not Mode.VISION and (ocr or settings.ocr_engine) == "tesseract":
        engine = TesseractOcr(settings.ocr_languages)
    return DocumentReader(llm, reading_mode, engine)
