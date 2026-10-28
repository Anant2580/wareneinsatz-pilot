"""The ingest pipeline: one file in, one clear outcome out.

approved      invoice extracted exactly, all checks passed, approved automatically
needs_review  invoice saved, but a check found an error (or it wasn't read exactly): a person decides
needs_ocr     file saved with status 'received'; `wareneinsatz extract` reads it later (step 5)
duplicate     the same file, or the same supplier's invoice number, is already stored
failed        file saved with status 'failed' and the reason, so nothing disappears silently

Ingest is fast and exact. Reading scans and photos with a language model takes seconds to a
minute per page, so it is a separate step (extract_document) that works through the documents
waiting with status 'received', like a job queue.
"""

import hashlib
import time
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from wareneinsatz.ingest.detect import FileType, detect_file_type
from wareneinsatz.ingest.errors import ExtractionError, NeedsVisualExtraction
from wareneinsatz.ingest.extract import EXACT_METHODS, extract_invoice
from wareneinsatz.ingest.store import DocumentStore
from wareneinsatz.models import Document
from wareneinsatz.reading.llm import LlmError, LlmUnavailable
from wareneinsatz.reading.reader import DocumentReader
from wareneinsatz.repository import (
    DocumentMeta,
    DuplicateDocumentError,
    DuplicateInvoiceError,
    mark_document_failed,
    record_validation,
    save_document,
    save_invoice,
    save_invoice_for_document,
)
from wareneinsatz.validation import Issue, Severity, check_known_supplier, validate_invoice


class Outcome(StrEnum):
    APPROVED = "approved"
    NEEDS_REVIEW = "needs_review"
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
    issues: tuple[Issue, ...] = ()  # validation findings (errors and warnings)


def ingest_file(session: Session, store: DocumentStore, path: Path, *, today: date | None = None) -> IngestResult:
    return ingest_bytes(session, store, path.read_bytes(), path.name, today=today)


def ingest_bytes(
    session: Session, store: DocumentStore, content: bytes, filename: str, *, today: date | None = None
) -> IngestResult:
    """Extract, save, validate and store one file. The caller commits (one transaction per file)."""
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

    # Step 3: business-rule checks. Only exact e-invoices without errors are approved automatically;
    # anything read by OCR + LLM (step 5) is always shown to a person first.
    data = extraction.data
    supplier = invoice.supplier
    issues = validate_invoice(data, today=today).issues + check_known_supplier(supplier.name, supplier.vat_id, data)
    approve = extraction.method in EXACT_METHODS and not any(issue.severity is Severity.ERROR for issue in issues)
    record_validation(session, invoice, [issue.as_dict() for issue in issues], approve=approve)

    return IngestResult(
        filename,
        Outcome.APPROVED if approve else Outcome.NEEDS_REVIEW,
        f"{data.supplier.name} | invoice {data.invoice_number or '-'} | {len(data.lines)} lines"
        f" | {data.gross_total} {data.currency}",
        invoice_id=invoice.id,
        document_id=invoice.document_id,
        method=extraction.method,
        extraction_ms=elapsed_ms,
        issues=tuple(issues),
    )


def extract_document(
    session: Session, store: DocumentStore, document: Document, reader: DocumentReader, *, today: date | None = None
) -> IngestResult:
    """Step 5: read a stored document (status 'received') with OCR + a language model.

    The result always goes to review: a person checks what the model read before it counts.
    LlmUnavailable is not caught: the batch stops and the document stays 'received' for the next run.
    """
    started = time.perf_counter()
    try:
        reading = reader.read(store.open(document.storage_path or ""))
    except LlmUnavailable:
        raise
    except (ExtractionError, LlmError, OSError) as problem:  # OSError: the stored file is missing
        mark_document_failed(session, document, describe(problem), method=reader.method)
        return IngestResult(document.filename, Outcome.FAILED, describe(problem), document_id=document.id)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    # The model's reply exactly as it came, with the settings that produced it (model, prompt version ...).
    raw = {"reader": reading.details, "printed": reading.printed}

    if not reading.valid:
        message = "Not a complete invoice: " + "; ".join(reading.problems[:3])
        mark_document_failed(session, document, message, method=reader.method, raw_extraction=raw)
        return IngestResult(document.filename, Outcome.FAILED, message, document_id=document.id)
    try:
        invoice = save_invoice_for_document(
            session,
            document,
            reading.data,
            extraction_method=reader.method,
            extraction_ms=elapsed_ms,
            raw_extraction=raw,
        )
    except DuplicateInvoiceError as duplicate:
        mark_document_failed(session, document, str(duplicate), method=reader.method, raw_extraction=raw)
        return IngestResult(document.filename, Outcome.DUPLICATE, str(duplicate), document_id=document.id)

    data = reading.data
    supplier = invoice.supplier
    issues = validate_invoice(data, today=today).issues + check_known_supplier(supplier.name, supplier.vat_id, data)
    record_validation(session, invoice, [issue.as_dict() for issue in issues], approve=False)  # never automatic
    return IngestResult(
        document.filename,
        Outcome.NEEDS_REVIEW,
        f"{data.supplier.name} | invoice {data.invoice_number or '-'} | {len(data.lines)} lines"
        f" | {data.gross_total} {data.currency}",
        invoice_id=invoice.id,
        document_id=document.id,
        method=reader.method,
        extraction_ms=elapsed_ms,
        issues=tuple(issues),
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
