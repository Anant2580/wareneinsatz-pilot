"""The ingest pipeline: one file in, one clear outcome out.

stored      invoice extracted exactly and saved (status needs_review until step 3 validates it)
needs_ocr   file saved with status 'received'; the OCR + LLM extractor (step 5) picks it up
duplicate   the same file, or the same supplier's invoice number, is already stored
failed      file saved with status 'failed' and the reason, so nothing disappears silently
"""

import hashlib
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from wareneinsatz.ingest.detect import FileType, detect_file_type
from wareneinsatz.ingest.errors import ExtractionError, NeedsVisualExtraction
from wareneinsatz.ingest.extract import extract_invoice
from wareneinsatz.ingest.store import DocumentStore
from wareneinsatz.repository import (
    DocumentMeta,
    DuplicateDocumentError,
    DuplicateInvoiceError,
    save_document,
    save_invoice,
)


class Outcome(StrEnum):
    STORED = "stored"
    NEEDS_OCR = "needs_ocr"
    DUPLICATE = "duplicate"
    FAILED = "failed"


@dataclass(frozen=True)
class IngestResult:
    filename: str
    outcome: Outcome
    message: str
    invoice_id: int | None = None
    document_id: int | None = None
    method: str | None = None
    extraction_ms: int | None = None


def ingest_file(session: Session, store: DocumentStore, path: Path) -> IngestResult:
    return ingest_bytes(session, store, path.read_bytes(), path.name)


def ingest_bytes(session: Session, store: DocumentStore, content: bytes, filename: str) -> IngestResult:
    """Extract, save and store one file. The caller commits (one transaction per file)."""
    sha256 = hashlib.sha256(content).hexdigest()
    file_type = detect_file_type(content)
    base = {
        "sha256": sha256,
        "filename": filename,
        "media_type": file_type.media_type,
        "size_bytes": len(content),
        "storage_path": store.relative_path(sha256, file_type.suffix),
    }

    started = time.perf_counter()
    try:
        extraction = extract_invoice(content, file_type)
    except NeedsVisualExtraction as reason:
        return _save_without_invoice(
            session, store, content, file_type, DocumentMeta(**base), Outcome.NEEDS_OCR, reason
        )
    except (ExtractionError, ValidationError) as problem:
        return _save_without_invoice(session, store, content, file_type, DocumentMeta(**base), Outcome.FAILED, problem)
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    meta = DocumentMeta(**base, extraction_method=extraction.method, extraction_ms=elapsed_ms)
    try:
        invoice = save_invoice(session, meta, extraction.data)
    except (DuplicateDocumentError, DuplicateInvoiceError) as duplicate:
        return IngestResult(filename, Outcome.DUPLICATE, str(duplicate))

    # Database first, file second: if the insert fails, no orphan file is written.
    # Same content always maps to the same path, so writing it again is harmless.
    store.save(content, sha256, file_type.suffix)

    data = extraction.data
    return IngestResult(
        filename,
        Outcome.STORED,
        f"{data.supplier.name} | invoice {data.invoice_number or '-'} | {len(data.lines)} lines"
        f" | {data.gross_total} {data.currency}",
        invoice_id=invoice.id,
        document_id=invoice.document_id,
        method=extraction.method,
        extraction_ms=elapsed_ms,
    )


def _save_without_invoice(
    session: Session,
    store: DocumentStore,
    content: bytes,
    file_type: FileType,
    meta: DocumentMeta,
    outcome: Outcome,
    reason: Exception,
) -> IngestResult:
    message = describe(reason)
    status = "received" if outcome is Outcome.NEEDS_OCR else "failed"
    try:
        document = save_document(session, meta, status=status, error=None if status == "received" else message)
    except DuplicateDocumentError as duplicate:
        return IngestResult(meta.filename, Outcome.DUPLICATE, str(duplicate))
    store.save(content, meta.sha256, file_type.suffix)
    return IngestResult(meta.filename, outcome, message, document_id=document.id)


def describe(problem: Exception) -> str:
    """A one-line reason. Schema errors list the first fields that broke, e.g. 'lines.2.unit_price: ...'."""
    if isinstance(problem, ValidationError):
        details = [f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in problem.errors()[:3]]
        return "Schema check failed: " + "; ".join(details)
    return str(problem)
