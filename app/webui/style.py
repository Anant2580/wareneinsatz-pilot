"""Small layout helpers and the few CSS rules the theme in .streamlit/config.toml cannot express.

Colours and fonts live in config.toml, so light and dark mode both work; the CSS here only uses
Streamlit's own CSS variables or `inherit`; the background drawings (decor.py) pick their colour from
the active theme.
"""

from __future__ import annotations

import html

import streamlit as st

CSS = """
<style>
/* Numbers line up in columns: tabular figures for metrics, tables and the headline. */
[data-testid="stMetricValue"], [data-testid="stDataFrame"], .wp-headline { font-variant-numeric: tabular-nums; }

/* Less empty space above the page title. */
.block-container { padding-top: 2.2rem; }

/* Page header: title plus one line that says what the page is for. */
[data-testid="stMarkdownContainer"] p.wp-lede { margin: -0.4rem 0 1.4rem; opacity: 0.72; max-width: 70ch; }

/* Dashboard opener: the month, written as a sentence in the heading face. */
[data-testid="stMarkdownContainer"] p.wp-month { margin: 0; font-weight: 600; opacity: 0.65; }
[data-testid="stMarkdownContainer"] p.wp-headline {
  font-family: "Bricolage Grotesque", sans-serif; font-weight: 650; letter-spacing: -0.02em;
  font-size: clamp(1.7rem, 2.9vw, 2.55rem); line-height: 1.12; max-width: 26ch; margin: 0.2rem 0 1.1rem;
}
.wp-muted { opacity: 0.65; }

/* Theme switch: a small round button at the top right, over the first line of the page. */
.st-key-theme_bar { margin-bottom: -2.9rem; position: relative; z-index: 2; pointer-events: none; }
.st-key-theme_bar button {
  pointer-events: auto; width: 2.6rem; height: 2.6rem; padding: 0; border-radius: 50%;
  border: 1px solid rgba(127, 127, 127, 0.35); background: rgba(127, 127, 127, 0.06);
}
.st-key-theme_bar button:hover { background: rgba(127, 127, 127, 0.16); }
.st-key-theme_bar button [data-testid="stIconMaterial"] { font-size: 1.4rem !important; }
.st-key-theme_script { display: none; }

@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
</style>
"""


def apply(*, decor: bool = True) -> None:
    """The page CSS, plus the floating food drawings in the background unless they are switched off."""
    st.html(CSS)
    if decor:
        from webui.decor import layer

        theme = getattr(getattr(st.context, "theme", None), "type", None)
        st.html(layer(dark=theme == "dark"))


def page_header(title: str, lede: str) -> None:
    st.title(title)
    st.markdown(f'<p class="wp-lede">{html.escape(lede)}</p>', unsafe_allow_html=True)


def opener(month: str, sentence: str) -> None:
    """The dashboard's first line: e.g. 'October 2026' over '12,340 € of food bought ...'."""
    st.markdown(
        f'<p class="wp-month">{html.escape(month)}</p><p class="wp-headline">{html.escape(sentence)}</p>',
        unsafe_allow_html=True,
    )
