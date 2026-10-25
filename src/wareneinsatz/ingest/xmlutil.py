"""Safe XML parsing and a small helper for reading values from namespaced e-invoice XML."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

from wareneinsatz.ingest.errors import ExtractionError


def parse_xml(content: bytes) -> Element:
    """Parse untrusted XML.

    defusedxml blocks the classic XML attacks: external entities that read local files
    or call URLs (XXE) and entity "bombs" that expand to gigabytes. E-invoices never need
    a DTD, so any DOCTYPE is refused outright.
    """
    try:
        return SafeElementTree.fromstring(content, forbid_dtd=True)
    except DefusedXmlException as error:
        raise ExtractionError(f"Unsafe XML refused ({type(error).__name__})") from error
    except ParseError as error:
        raise ExtractionError(f"Not well-formed XML: {error}") from error


def local_name(element: Element) -> str:
    """'{urn:...:CrossIndustryInvoice:100}CrossIndustryInvoice' -> 'CrossIndustryInvoice'."""
    return element.tag.rsplit("}", 1)[-1]


def namespace(element: Element) -> str:
    """'{urn:...:Invoice-2}Invoice' -> 'urn:...:Invoice-2' ('' if the element has no namespace)."""
    return element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""


class Node:
    """An XML element plus its namespace prefixes, with short typed accessors.

    Paths are ElementTree paths with prefixes, e.g. "ram:SpecifiedTradeProduct/ram:Name"
    or "ram:ID[@schemeID='VA']". Missing elements and empty text give None.
    """

    def __init__(self, element: Element, namespaces: dict[str, str]):
        self.element = element
        self.ns = namespaces

    def find(self, path: str) -> Node | None:
        found = self.element.find(path, self.ns)
        return None if found is None else Node(found, self.ns)

    def findall(self, path: str) -> list[Node]:
        return [Node(found, self.ns) for found in self.element.findall(path, self.ns)]

    def text(self, path: str = ".") -> str | None:
        found = self.element.find(path, self.ns)
        if found is None or found.text is None:
            return None
        return found.text.strip() or None

    def attr(self, path: str, name: str) -> str | None:
        found = self.element.find(path, self.ns)
        if found is None:
            return None
        value = found.get(name)
        return value.strip() if value and value.strip() else None

    def decimal(self, path: str) -> Decimal | None:
        """Read a number exactly as written ("288.79" -> Decimal('288.79')), never via float."""
        raw = self.text(path)
        if raw is None:
            return None
        try:
            return Decimal(raw)
        except InvalidOperation as error:
            raise ExtractionError(f"Not a number at {path}: {raw!r}") from error

    def date(self, path: str) -> date | None:
        """Dates come as '20260420' (CII format 102) or '2026-04-20' (UBL, ISO 8601)."""
        raw = self.text(path)
        if raw is None:
            return None
        try:
            if len(raw) == 8 and raw.isdigit():
                return datetime.strptime(raw, "%Y%m%d").date()
            return date.fromisoformat(raw[:10])
        except ValueError as error:
            raise ExtractionError(f"Not a date at {path}: {raw!r}") from error
