"""Make a rendered invoice look like a phone photo of a printed page.

Real photos of receipts are slightly rotated and tilted, unevenly lit, a bit blurry, noisy and
JPEG-compressed. These effects are applied with Pillow (free) in random but seeded amounts, so a
given seed always produces the same image.
"""

from __future__ import annotations

import io
import random

import pypdfium2
from PIL import Image, ImageChops, ImageDraw, ImageFilter


def pdf_to_image(pdf: bytes, dpi: int = 150) -> Image.Image:
    """First page of a PDF as an RGB image."""
    page = pypdfium2.PdfDocument(pdf)[0]
    return page.render(scale=dpi / 72).to_pil().convert("RGB")


def photo_of(pdf: bytes, seed: int) -> bytes:
    """A JPEG that looks like a phone photo of the printed PDF page."""
    rng = random.Random(seed)
    page = pdf_to_image(pdf, dpi=rng.choice([130, 150, 170]))

    # The paper lies on a darker surface, with a margin around it
    margin = int(min(page.size) * 0.08)
    table_colour = tuple(rng.randint(60, 120) for _ in range(3))
    canvas = Image.new("RGB", (page.width + 2 * margin, page.height + 2 * margin), table_colour)
    canvas.paste(page, (margin, margin))

    # Slight rotation and perspective tilt (the phone is never held perfectly straight)
    canvas = canvas.rotate(
        rng.uniform(-3.5, 3.5), resample=Image.Resampling.BICUBIC, expand=False, fillcolor=table_colour
    )
    canvas = _tilt(canvas, rng, strength=rng.uniform(0.01, 0.035), fill=table_colour)

    # Uneven light: a soft shadow from one side
    canvas = _shade(canvas, rng)

    # Camera blur, sensor noise, then compression and a typical phone resolution
    canvas = canvas.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.4, 1.1)))
    canvas = _noise(canvas, rng, amount=rng.randint(6, 14))
    longest = rng.choice([1400, 1600, 1800])
    scale = longest / max(canvas.size)
    canvas = canvas.resize((int(canvas.width * scale), int(canvas.height * scale)), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=rng.randint(55, 80))
    return buffer.getvalue()


def _tilt(image: Image.Image, rng: random.Random, strength: float, fill: tuple[int, int, int]) -> Image.Image:
    """Perspective distortion: move each corner a little and map the image onto the new corners."""
    w, h = image.size
    dx, dy = w * strength, h * strength
    corners = [  # where the source corners come from (top-left, top-right, bottom-right, bottom-left)
        (rng.uniform(0, dx), rng.uniform(0, dy)),
        (w - rng.uniform(0, dx), rng.uniform(0, dy)),
        (w - rng.uniform(0, dx), h - rng.uniform(0, dy)),
        (rng.uniform(0, dx), h - rng.uniform(0, dy)),
    ]
    coefficients = _perspective_coefficients([(0, 0), (w, 0), (w, h), (0, h)], corners)
    return image.transform((w, h), Image.Transform.PERSPECTIVE, coefficients, Image.Resampling.BICUBIC, fillcolor=fill)


def _perspective_coefficients(target: list[tuple[float, float]], source: list[tuple[float, float]]) -> list[float]:
    """Solve the 8 coefficients Pillow needs, mapping target corners to source corners (Gaussian elimination)."""
    rows, values = [], []
    for (x, y), (u, v) in zip(target, source, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        values.extend([u, v])
    n = 8
    matrix = [row + [value] for row, value in zip(rows, values, strict=True)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(matrix[r][col]))
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        for r in range(n):
            if r != col:
                factor = matrix[r][col] / matrix[col][col]
                matrix[r] = [a - factor * b for a, b in zip(matrix[r], matrix[col], strict=True)]
    return [matrix[i][n] / matrix[i][i] for i in range(n)]


def _shade(image: Image.Image, rng: random.Random) -> Image.Image:
    """Darken one side gradually, like a hand or the phone casting a shadow."""
    w, h = image.size
    gradient = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(gradient)
    darkest = rng.randint(150, 215)
    horizontal = rng.random() < 0.5
    steps = w if horizontal else h
    for i in range(0, steps, 4):
        level = int(darkest + (255 - darkest) * (i / steps))
        if horizontal:
            draw.rectangle([i, 0, i + 4, h], fill=level)
        else:
            draw.rectangle([0, i, w, i + 4], fill=level)
    if rng.random() < 0.5:
        gradient = gradient.transpose(
            Image.Transpose.FLIP_LEFT_RIGHT if horizontal else Image.Transpose.FLIP_TOP_BOTTOM
        )
    return ImageChops.multiply(image, Image.merge("RGB", [gradient] * 3))


def _noise(image: Image.Image, rng: random.Random, amount: int) -> Image.Image:
    """Grain: a seeded noise image blended in (Pillow's effect_noise is not seedable, so the noise is drawn here)."""
    small = Image.new("L", (image.width // 2, image.height // 2))
    small.putdata([128 + rng.randint(-amount, amount) for _ in range(small.width * small.height)])
    noise = small.resize(image.size).convert("RGB")
    return ImageChops.add(image, noise, scale=1.0, offset=-128)  # image + (noise - 128), clipped to 0..255
