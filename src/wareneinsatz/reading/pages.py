"""Pages of a document: as images (for the vision model and OCR) and as text (the PDF's text layer).

    pages = load_pages(content)        # PDF, JPEG, PNG, WebP, TIFF or iPhone HEIC
    pages[0].image                     # a Pillow image, upright (EXIF rotation applied)
    pages[0].text                      # text layer of a digital PDF; None for photos and scans

PDF pages are rendered with pypdfium2 (Google's PDFium, the engine of Chrome's PDF viewer).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import pypdfium2 as pdfium
from PIL import Image, ImageOps
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from wareneinsatz.ingest.detect import FileKind, detect_file_type
from wareneinsatz.ingest.errors import ExtractionError

MAX_PAGES = 4  # supplier invoices for a restaurant are 1-2 pages; more pages cost model time
RENDER_DPI = 150  # enough for the model and for OCR after upscaling; 300 dpi images are 4x larger
MODEL_IMAGE_SIDE = 1600  # longest side of an image sent to the model, in pixels


@dataclass
class Page:
    image: Image.Image  # RGB
    text: str | None  # the PDF's own text layer; None for photos and scanned PDFs
    is_photo: bool  # a camera image: OCR straightens and denoises it first


def load_pages(content: bytes, *, max_pages: int = MAX_PAGES, dpi: int = RENDER_DPI) -> list[Page]:
    file_type = detect_file_type(content)
    if file_type.kind is FileKind.PDF:
        return _pdf_pages(content, max_pages, dpi)
    if file_type.kind is FileKind.IMAGE:
        return [_photo(content, file_type.media_type)]
    raise ExtractionError(f"Cannot read a {file_type.media_type} file as pages")


def compact_text(text: str) -> str:
    """Layout text has long runs of spaces to keep columns apart. Three spaces keep them apart just as well
    and cost the model far fewer tokens. Trailing spaces and repeated blank lines go too."""
    rows = [re.sub(r" {3,}", "   ", row).rstrip() for row in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(rows)).strip()


def pdf_text_pages(content: bytes) -> list[str]:
    """Text layer per page; layout mode keeps the rows of a table together."""
    try:
        return [page.extract_text(extraction_mode="layout") or "" for page in PdfReader(io.BytesIO(content)).pages]
    except (PdfReadError, ValueError):
        return []


def encode_for_model(image: Image.Image, max_side: int = MODEL_IMAGE_SIDE) -> bytes:
    """JPEG, longest side at most `max_side`: small enough to be fast, large enough to read prices."""
    copy = image.copy()
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    copy.convert("RGB").save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def _pdf_pages(content: bytes, max_pages: int, dpi: int) -> list[Page]:
    try:
        pdf = pdfium.PdfDocument(content)
    except pdfium.PdfiumError as error:
        raise ExtractionError(f"Cannot open the PDF: {error}") from error
    try:
        texts = pdf_text_pages(content)
        pages = []
        for index in range(min(len(pdf), max_pages)):
            image = pdf[index].render(scale=dpi / 72).to_pil().convert("RGB")
            text = texts[index] if index < len(texts) else ""
            pages.append(Page(image=image, text=compact_text(text) if text.strip() else None, is_photo=False))
    finally:
        pdf.close()
    if not pages:
        raise ExtractionError("The PDF has no pages")
    return pages


def _photo(content: bytes, media_type: str) -> Page:
    if media_type == "image/heic":
        try:
            from pillow_heif import register_heif_opener
        except ImportError as error:  # pragma: no cover - pillow-heif is a dependency
            raise ExtractionError("HEIC photos need the pillow-heif package") from error
        register_heif_opener()
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except (OSError, Image.DecompressionBombError) as error:  # broken file, or a pixel bomb
        raise ExtractionError(f"Cannot open the image: {error}") from error
    # Phones store "rotate by 90°" in the EXIF data instead of rotating the pixels.
    upright = ImageOps.exif_transpose(image)
    return Page(image=upright.convert("RGB"), text=None, is_photo=True)
