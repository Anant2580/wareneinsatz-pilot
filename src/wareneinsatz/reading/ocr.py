"""OCR: text from page images, for the text and hybrid reading modes.

Tesseract 5 (open source, Apache 2.0) with the German and English models. Photos are cleaned
up first, because Tesseract expects straight, clean scans:

1. grayscale and a median filter against sensor noise
2. contrast stretch (shadows and grey paper become white)
3. deskew: a phone photo is rarely straight, and tilted text breaks the rows of a table apart
4. upscaling: Tesseract reads best at about 30 pixels per text line

Deskewing uses a projection profile. The edges of the image (text strokes and the paper's
border) are rotated in small steps; at the right angle the rows line up, so the row sums
vary most. Pure Pillow, no OpenCV needed.
"""

from __future__ import annotations

import csv
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageFilter, ImageOps

from wareneinsatz.reading.pages import compact_text

MIN_OCR_WIDTH = 1800  # pixels; smaller images are upscaled before OCR


class OcrUnavailable(Exception):
    """The OCR engine is not installed or misses a language."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float | None  # mean word confidence 0..1 as reported by the engine


class OcrEngine(Protocol):
    name: str

    def read(self, image: Image.Image, *, is_photo: bool) -> OcrResult: ...


class TesseractOcr:
    """Runs the `tesseract` program (macOS: brew install tesseract tesseract-lang)."""

    def __init__(self, languages: str = "deu+eng", *, psm: int = 6, binary: str | None = None, timeout: float = 120):
        found = binary or shutil.which("tesseract")
        if not found:
            raise OcrUnavailable(
                "Tesseract is not installed. macOS: brew install tesseract tesseract-lang · "
                "Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-deu"
            )
        self.binary = found
        self.languages = languages
        self.psm = psm  # 6 = one uniform block of text: keeps the columns of a row on one line
        self.timeout = timeout
        installed = set(self._run("--list-langs").split()[1:])  # first word is a heading
        missing = [lang for lang in languages.split("+") if lang not in installed]
        if missing:
            raise OcrUnavailable(
                f"Tesseract has no language data for {', '.join(missing)}. macOS: brew install tesseract-lang · "
                "Ubuntu: sudo apt install tesseract-ocr-deu"
            )
        words = self._run("--version").split()  # "tesseract 5.3.4 ..."
        self.name = f"tesseract-{words[1]}" if len(words) > 1 else "tesseract"

    def _run(self, *args: str) -> str:
        result = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=self.timeout, check=False)
        return result.stdout + result.stderr  # older versions print some lists to stderr

    def read(self, image: Image.Image, *, is_photo: bool) -> OcrResult:
        prepared = prepare_for_ocr(image, is_photo=is_photo)
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "page.png", Path(tmp) / "out"
            prepared.save(source)
            subprocess.run(
                [self.binary, str(source), str(out), "-l", self.languages, "--psm", str(self.psm),
                 "-c", "preserve_interword_spaces=1", "txt", "tsv"],
                capture_output=True, timeout=self.timeout, check=True,
            )  # fmt: skip
            text = (Path(tmp) / "out.txt").read_text(encoding="utf-8")
            confidence = _mean_confidence(Path(tmp) / "out.tsv")
        return OcrResult(text=compact_text(text), confidence=confidence)


def _mean_confidence(tsv: Path) -> float | None:
    """Tesseract's TSV has one row per word with a confidence 0-100 (-1 for layout rows)."""
    with tsv.open(encoding="utf-8", newline="") as handle:
        values = [float(row["conf"]) for row in csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)]
    words = [value for value in values if value >= 0]
    return round(statistics.fmean(words) / 100, 3) if words else None


def prepare_for_ocr(image: Image.Image, *, is_photo: bool) -> Image.Image:
    gray = ImageOps.grayscale(image)
    if is_photo:
        gray = gray.filter(ImageFilter.MedianFilter(3))
        gray = ImageOps.autocontrast(gray, cutoff=1)
        angle = skew_angle(gray)
        if angle:
            gray = gray.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
    if gray.width < MIN_OCR_WIDTH:
        factor = min(MIN_OCR_WIDTH / gray.width, 4.0)
        gray = gray.resize((round(gray.width * factor), round(gray.height * factor)), Image.Resampling.LANCZOS)
    return gray


def skew_angle(gray: Image.Image, *, max_angle: float = 7.5, step: float = 0.25) -> float:
    """The rotation (degrees, counter-clockwise) that makes the text rows horizontal."""
    small = gray.copy()
    small.thumbnail((500, 500))
    edges = small.filter(ImageFilter.FIND_EDGES)  # text strokes and the paper's border light up
    edges = ImageOps.autocontrast(edges.crop((2, 2, edges.width - 2, edges.height - 2)))  # the filter's own frame
    best_angle, best_score = 0.0, -1.0
    steps = round(max_angle / step)
    for i in range(-steps, steps + 1):
        angle = i * step
        rotated = edges.rotate(angle, resample=Image.Resampling.BILINEAR)
        rows = rotated.resize((1, rotated.height), Image.Resampling.BOX)  # one pixel per row = row average
        score = statistics.pvariance(list(rows.get_flattened_data()))
        if score > best_score:
            best_angle, best_score = angle, score
    return best_angle
