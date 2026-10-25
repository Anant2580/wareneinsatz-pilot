"""Entry point of extraction: bytes in, InvoiceData out (or a clear reason why not).

XML (UBL or CII)            -> parsed exactly
PDF with embedded XML       -> ZUGFeRD / Factur-X, parsed exactly
PDF without XML, images     -> NeedsVisualExtraction (OCR + LLM, step 5)
anything else / broken      -> ExtractionError
"""

from dataclasses import dataclass

from wareneinsatz.ingest import cii, ubl
from wareneinsatz.ingest.detect import FileKind, FileType, detect_file_type
from wareneinsatz.ingest.errors import ExtractionError, NeedsVisualExtraction
from wareneinsatz.ingest.xmlutil import local_name, namespace, parse_xml
from wareneinsatz.ingest.zugferd import find_invoice_xml
from wareneinsatz.schemas import InvoiceData


@dataclass(frozen=True)
class Extraction:
    data: InvoiceData
    method: str  # how it was read: "xml-ubl", "xml-cii" or "pdf-zugferd"
    profile: str | None  # specification identifier from the XML, e.g. XRechnung 3.0 or Factur-X EN 16931


def extract_invoice(content: bytes, file_type: FileType | None = None) -> Extraction:
    """Read an invoice from a file's bytes. Raises ExtractionError or NeedsVisualExtraction."""
    file_type = file_type or detect_file_type(content)

    if file_type.kind is FileKind.XML:
        data, syntax, profile = parse_einvoice_xml(content)
        return Extraction(data, f"xml-{syntax}", profile)

    if file_type.kind is FileKind.PDF:
        embedded = find_invoice_xml(content)
        if embedded is None:
            raise NeedsVisualExtraction("PDF without embedded e-invoice XML")
        name, xml = embedded
        try:
            data, _syntax, profile = parse_einvoice_xml(xml)
        except ExtractionError as error:
            # The attachment is unusable (e.g. ZUGFeRD 1.0), but the PDF page is still readable.
            raise NeedsVisualExtraction(f"Embedded {name} could not be used: {error}") from error
        return Extraction(data, "pdf-zugferd", profile)

    if file_type.kind is FileKind.IMAGE:
        raise NeedsVisualExtraction(f"Image ({file_type.media_type})")

    raise ExtractionError("Unsupported file type (expected XML, PDF or an image)")


def parse_einvoice_xml(content: bytes) -> tuple[InvoiceData, str, str | None]:
    """Parse XRechnung / ZUGFeRD XML. Returns (invoice, syntax 'ubl' or 'cii', profile)."""
    root = parse_xml(content)
    root_namespace = namespace(root)

    if root_namespace == cii.ROOT_NAMESPACE and local_name(root) == "CrossIndustryInvoice":
        return cii.parse_cii(root), "cii", cii.profile(root)
    if root_namespace in (ubl.INVOICE_NAMESPACE, ubl.CREDIT_NOTE_NAMESPACE):
        return ubl.parse_ubl(root), "ubl", ubl.profile(root)
    raise ExtractionError(f"Not an EN 16931 e-invoice (root element <{local_name(root)}> in '{root_namespace}')")
