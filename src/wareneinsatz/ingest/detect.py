"""Recognise a file's type from its first bytes (its "magic number"), not from its file name.

File names lie: a supplier portal may send "rechnung.pdf" that is really XML,
or a phone may save a photo without an extension.
"""

from dataclasses import dataclass
from enum import StrEnum


class FileKind(StrEnum):
    XML = "xml"
    PDF = "pdf"
    IMAGE = "image"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FileType:
    kind: FileKind
    media_type: str  # MIME type stored with the document
    suffix: str  # file extension used in the document store


_UTF8_BOM = b"\xef\xbb\xbf"


def detect_file_type(content: bytes) -> FileType:
    """Return what kind of file this is, judged by its content."""
    head = content[:1024]
    if b"%PDF-" in head:  # PDFs may have a few junk bytes before the header
        return FileType(FileKind.PDF, "application/pdf", ".pdf")
    if head.startswith(b"\xff\xd8\xff"):
        return FileType(FileKind.IMAGE, "image/jpeg", ".jpg")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileType(FileKind.IMAGE, "image/png", ".png")
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):  # iPhone photos
        return FileType(FileKind.IMAGE, "image/heic", ".heic")
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return FileType(FileKind.IMAGE, "image/webp", ".webp")
    if head.startswith((b"II*\x00", b"MM\x00*")):  # scanners often produce TIFF
        return FileType(FileKind.IMAGE, "image/tiff", ".tiff")
    if head.removeprefix(_UTF8_BOM).lstrip().startswith(b"<"):
        return FileType(FileKind.XML, "application/xml", ".xml")
    return FileType(FileKind.UNKNOWN, "application/octet-stream", ".bin")
