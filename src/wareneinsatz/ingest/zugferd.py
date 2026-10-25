"""ZUGFeRD / Factur-X: a normal-looking PDF (PDF/A-3) with the invoice XML attached inside it.

People read the PDF, software reads the attached CII XML. This module finds that attachment.
"""

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from wareneinsatz.ingest.errors import ExtractionError

# Attachment names defined by the standards, in order of preference: Factur-X / ZUGFeRD 2.1+,
# the XRechnung profile of ZUGFeRD, and ZUGFeRD 1.0 / 2.0 (compared in lower case).
KNOWN_XML_NAMES = ("factur-x.xml", "xrechnung.xml", "zugferd-invoice.xml")


def find_invoice_xml(content: bytes) -> tuple[str, bytes] | None:
    """Return (attachment name, XML bytes) of the embedded invoice, or None for a plain PDF."""
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):  # empty user password is common for "protected" PDFs
            raise ExtractionError("PDF is password-protected")
        attachments = reader.attachments  # {file name: [file contents, ...]} from the PDF's EmbeddedFiles
    except PdfReadError as error:
        raise ExtractionError(f"Unreadable PDF: {error}") from error

    by_lower_name = {name.lower(): (name, files) for name, files in attachments.items() if files}
    for known in KNOWN_XML_NAMES:
        if known in by_lower_name:
            name, files = by_lower_name[known]
            return name, files[0]
    # Some generators use other names: accept any attached XML file.
    for name, files in attachments.items():
        if files and name.lower().endswith(".xml"):
            return name, files[0]
    return None
