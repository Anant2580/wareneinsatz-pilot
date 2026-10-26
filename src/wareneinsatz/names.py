"""Normalising company names, so spelling variants of the same supplier compare equal."""

import re
import unicodedata


def normalise_name(name: str) -> str:
    """'METRO Deutschland GmbH.' and 'Metro  Deutschland GmbH' both become 'metro deutschland gmbh'.

    casefold() also maps 'ß' to 'ss', so 'Großhandel' and 'Grosshandel' match.
    """
    folded = unicodedata.normalize("NFKC", name).casefold()
    return re.sub(r"[\W_]+", " ", folded).strip()


# Legal forms that vary between documents of the same company ("Musterfrisch GmbH" vs "Musterfrisch").
LEGAL_FORMS = {
    "gmbh",
    "mbh",
    "ag",
    "kg",
    "kgaa",
    "ohg",
    "gbr",
    "ug",
    "haftungsbeschränkt",
    "e",
    "k",
    "v",
    "co",
    "se",
    "und",
    "u",
}


def same_company(a: str, b: str) -> bool:
    """True if two names probably mean the same company.

    Legal forms are ignored, and one name may be a shorter form of the other:
    'MUSTERFRISCH Grosshandel' ~ 'Musterfrisch Großhandel GmbH', 'Metro' ~ 'METRO Deutschland GmbH'.
    """
    core_a = [word for word in normalise_name(a).split() if word not in LEGAL_FORMS]
    core_b = [word for word in normalise_name(b).split() if word not in LEGAL_FORMS]
    shorter, longer = sorted((core_a, core_b), key=len)
    return bool(shorter) and longer[: len(shorter)] == shorter
