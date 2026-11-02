"""Background illustrations: faint line drawings of what a burger grill buys, drifting slowly.

Drawn here as SVG paths (own work, no image licences), in the theme's green at a few percent
opacity, so tables and charts stay easy to read. Motion stops for people who ask their system
for reduced motion, and the sidebar has a switch to turn the drawings off.
"""

from __future__ import annotations

import base64

# 64 x 64 line drawings: stroke only, round ends.
ICONS = {
    "burger": "M10 30c0-11 10-18 22-18s22 7 22 18z M25 20l2 1 M32 17v2 M39 20l-2 1 "
    "M8 36c4-3 6 3 10 0s6 3 10 0 6 3 10 0 6 3 10 0 6 3 10 0 M10 40h44a3 3 0 0 1 0 6H10a3 3 0 0 1 0-6z "
    "M10 50h44c0 4-3 6-7 6H17c-4 0-7-2-7-6z",
    "fries": "M18 30h28l-4 26H22z M18 30c5 5 23 5 28 0 M23 31l-3-21 M29 31l-1-23 M35 31l1-22 M41 31l3-18",
    "tomato": "M32 12a20 20 0 1 0 0.01 0z M32 18a14 14 0 1 0 0.01 0z M25 23a2.5 2.5 0 1 0 0.01 0z "
    "M39 23a2.5 2.5 0 1 0 0.01 0z M25 37a2.5 2.5 0 1 0 0.01 0z M39 37a2.5 2.5 0 1 0 0.01 0z M32 18v28 M18 32h28",
    "leaf": "M12 52C12 26 30 12 54 10c-2 24-16 42-42 42z M12 52L42 22 M24 40h9 M30 34v-9",
    "cheese": "M8 44L44 20l12 8v20H8z M8 44h48 M22 40a3 3 0 1 0 0.01 0z M40 37a2.5 2.5 0 1 0 0.01 0z "
    "M48 31a2 2 0 1 0 0.01 0z",
    "beer": "M18 18h24l-3 38H21z M16 18c0-5 6-7 9-5 2-4 9-4 11 0 3-2 9 0 8 5 M42 25h6a4 4 0 0 1 4 4v9a4 4 0 0 1-4 4h-5"
    " M26 28v20 M33 28v20",
    "chili": "M14 50c10 4 26-2 34-18 3-6 2-12-2-14-4 0-6 4-8 8-6 12-14 18-24 24z M46 18c1-5 4-8 8-8",
    "onion": "M32 14a18 18 0 1 0 0.01 0z M32 22a10 10 0 1 0 0.01 0z",
    "egg": "M14 34c-4-10 6-20 16-16 6-8 22-2 20 8 8 6 2 20-10 18-6 6-20 4-22-4-6-1-6-4-4-6z M32 25a7 7 0 1 0 0.01 0z",
    "cutlery": "M20 8v14a4 4 0 0 0 8 0V8 M24 8v48 M42 8c6 4 8 14 4 22v26",
}

# Where each drawing floats: (icon, left %, top %, size px, start angle, seconds per cycle, delay s).
# Left values start at about 25 %, so nothing hides under the sidebar.
PLACES = [
    ("burger", 76, 5, 150, -8, 26, 0),
    ("beer", 95, 3, 92, 6, 20, -14),
    ("fries", 92, 36, 112, 10, 22, -6),
    ("onion", 64, 26, 72, 0, 18, -11),
    ("chili", 47, 44, 86, -20, 32, -5),
    ("egg", 27, 62, 98, 8, 27, -2),
    ("tomato", 60, 66, 100, 0, 30, -12),
    ("leaf", 84, 72, 124, 18, 24, -3),
    ("cheese", 40, 86, 106, -6, 28, -9),
    ("cutlery", 31, 22, 82, 14, 25, -16),
    ("tomato", 98, 60, 66, 0, 21, -4),
    ("burger", 70, 92, 92, 6, 29, -8),
]

CSS = """
<style>
/* The content forms its own layer, so the drawings (z-index -1) sit behind it but above the page colour. */
[data-testid="stMainBlockContainer"] { position: relative; z-index: 0; }
[data-testid="stElementContainer"]:has(.wp-food) { margin-bottom: -1rem; }
.wp-food { position: fixed; inset: 0; z-index: -1; pointer-events: none; overflow: hidden; }
.wp-food img { position: absolute; animation: wp-drift var(--t) ease-in-out var(--d) infinite alternate; }
@keyframes wp-drift {
  from { transform: translate(0, 0) rotate(var(--r)); }
  to   { transform: translate(-14px, -26px) rotate(calc(var(--r) + 9deg)); }
}
@media (prefers-reduced-motion: reduce) { .wp-food img { animation: none; } }
</style>
"""


def layer(dark: bool) -> str:
    """The HTML for the drawings, in the theme's colour (mint on dark, bay-leaf green on light)."""
    colour, opacity = ("#9FD3B4", 0.1) if dark else ("#2F6B4F", 0.12)
    drawings = "".join(
        f'<img src="{_data_uri(ICONS[icon], colour)}" width="{size}" height="{size}" alt="" '
        f'style="opacity:{opacity};left:calc({left}% - {size // 2}px);top:{top}%;'
        f'--r:{angle}deg;--t:{seconds}s;--d:{delay}s">'
        for icon, left, top, size, angle, seconds, delay in PLACES
    )  # images, because Streamlit's HTML cleaner removes inline SVG
    return f'{CSS}<div class="wp-food" aria-hidden="true">{drawings}</div>'


def _data_uri(path: str, colour: str) -> str:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="{colour}" fill-opacity="0.16" stroke="{colour}" '
        f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="{path}"/></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
