"""The REST API (FastAPI): everything the web app does goes through here.

    uv run wareneinsatz serve        # http://localhost:8000, interactive docs at /docs

Each request runs in its own transaction (`ctx.transaction()`): committed when the handler
returns, rolled back on an error. Reading scans and photos with the language model is slow, so
POST /extraction starts it in the background and GET /extraction reports the progress.

If API_KEY is set in .env, every request except /health needs the header `X-API-Key`.
"""

from __future__ import annotations

import io
import secrets
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from wareneinsatz import __version__
from wareneinsatz.alerts import open_alerts
from wareneinsatz.api.schemas import (
    AlertCheckOut,
    AlertOut,
    ByIn,
    ConfirmIn,
    ConfirmOut,
    DishDetail,
    DishOut,
    DocumentOut,
    ExtractionStatus,
    IngestOut,
    IngredientOut,
    InvoiceDetail,
    InvoiceSummary,
    IssueOut,
    ItemCostOut,
    LineOut,
    MonthOut,
    PricePointOut,
    RejectIn,
    ReviewIn,
    SummaryOut,
)
from wareneinsatz.config import Settings, get_settings
from wareneinsatz.costing import RecipeCost
from wareneinsatz.db import make_engine, session_scope
from wareneinsatz.ingest.pipeline import extract_document, ingest_bytes
from wareneinsatz.ingest.store import DocumentStore
from wareneinsatz.matching.embeddings import Embedder
from wareneinsatz.matching.service import (
    UnknownIngredientError,
    build_matcher,
    confirm_line,
    match_open_lines,
    price_history,
)
from wareneinsatz.models import Alert, Document, Ingredient, Invoice, InvoiceLine
from wareneinsatz.reading import DocumentReader, LlmUnavailable, OcrUnavailable
from wareneinsatz.recipes import check_alerts, cost_menu, monthly_purchases
from wareneinsatz.repository import DuplicateInvoiceError, received_documents
from wareneinsatz.review import ReviewError, approve_invoice, correct_invoice, invoice_to_data, reject_invoice

MAX_PREVIEW_WIDTH = 2000


@dataclass
class ExtractionJob:
    """Progress of the background extraction run (one at a time)."""

    running: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total: int = 0
    done: int = 0
    outcomes: Counter = field(default_factory=Counter)
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class Context:
    engine: Engine
    store: DocumentStore
    settings: Settings
    reader_factory: Callable[[], DocumentReader]  # builds the OCR + LLM reader (raises if unavailable)
    embedder_factory: Callable[[], Embedder | None]  # embeddings for matching, or None (fuzzy only)
    job: ExtractionJob = field(default_factory=ExtractionJob)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with session_scope(self.engine) as session:
            yield session


def _default_reader(settings: Settings) -> Callable[[], DocumentReader]:
    def build() -> DocumentReader:
        from wareneinsatz.reading import make_reader

        reader = make_reader(settings)
        reader.llm.check()
        return reader

    return build


def _default_embedder(settings: Settings) -> Callable[[], Embedder | None]:
    def build() -> Embedder | None:
        from wareneinsatz.matching.embeddings import OllamaEmbedder

        embedder = OllamaEmbedder(settings.llm_base_url, settings.embedding_model)
        try:
            embedder.embed(["test"])
        except LlmUnavailable:
            return None
        return embedder

    return build


def create_app(
    *,
    engine: Engine | None = None,
    store: DocumentStore | None = None,
    settings: Settings | None = None,
    reader_factory: Callable[[], DocumentReader] | None = None,
    embedder_factory: Callable[[], Embedder | None] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    ctx = Context(
        engine=engine or make_engine(settings.database_url),
        store=store or DocumentStore(settings.document_dir),
        settings=settings,
        reader_factory=reader_factory or _default_reader(settings),
        embedder_factory=embedder_factory or _default_embedder(settings),
    )

    def check_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
        expected = settings.api_key.get_secret_value() if settings.api_key else None
        # compare_digest takes the same time for every wrong key, so the key can't be guessed by timing
        if expected and not (x_api_key and secrets.compare_digest(x_api_key, expected)):
            raise HTTPException(401, "Missing or wrong X-API-Key")

    app = FastAPI(
        title="Wareneinsatz-Pilot API",
        version=__version__,
        description="Supplier invoices in, ingredient prices and dish margins out.",
    )
    app.state.ctx = ctx
    secured = [Depends(check_key)]

    @app.exception_handler(OperationalError)
    def database_down(request: Request, error: OperationalError) -> JSONResponse:
        return JSONResponse({"detail": "The database is not reachable"}, status_code=503)

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        try:
            with ctx.transaction() as session:
                session.execute(text("SELECT 1"))
            database = "ok"
        except OperationalError:
            database = "unreachable"
        return {"status": "ok", "database": database, "version": __version__}

    # --- documents ------------------------------------------------------------------------------------

    @app.post("/documents", tags=["documents"], dependencies=secured)
    async def upload(files: Annotated[list[UploadFile], File()]) -> list[IngestOut]:
        """Store and read uploaded files: e-invoices exactly, scans and photos wait for extraction."""
        limit = settings.max_upload_mb * 1024 * 1024
        results = []
        for upload_file in files:
            content = await upload_file.read(limit + 1)
            if len(content) > limit:
                raise HTTPException(413, f"{upload_file.filename} is larger than {settings.max_upload_mb} MB")
            with ctx.transaction() as session:
                result = ingest_bytes(session, ctx.store, content, upload_file.filename or "upload")
            results.append(
                IngestOut(
                    filename=result.filename,
                    outcome=result.outcome.value,
                    message=result.message,
                    invoice_id=result.invoice_id,
                    document_id=result.document_id,
                    method=result.method,
                    extraction_ms=result.extraction_ms,
                    issues=[IssueOut(**issue.as_dict()) for issue in result.issues],
                )
            )
        return results

    @app.get("/documents", tags=["documents"], dependencies=secured)
    def documents(status: str | None = None, limit: int = Query(100, le=1000)) -> list[DocumentOut]:
        with ctx.transaction() as session:
            query = select(Document).order_by(Document.received_at.desc(), Document.id.desc()).limit(limit)
            if status:
                query = query.where(Document.status == status)
            return [
                DocumentOut(
                    id=d.id, filename=d.filename, media_type=d.media_type, status=d.status, error=d.error,
                    extraction_method=d.extraction_method, received_at=d.received_at,
                    invoice_id=d.invoice.id if d.invoice else None,
                )
                for d in session.scalars(query)
            ]  # fmt: skip

    def _document(session: Session, document_id: int) -> Document:
        document = session.get(Document, document_id)
        if document is None or not document.storage_path:
            raise HTTPException(404, "No such document")
        return document

    @app.get("/documents/{document_id}/file", tags=["documents"], dependencies=secured)
    def document_file(document_id: int) -> Response:
        with ctx.transaction() as session:
            document = _document(session, document_id)
            content, media_type = ctx.store.open(document.storage_path or ""), document.media_type
        return Response(content, media_type=media_type)

    @app.get("/documents/{document_id}/preview", tags=["documents"], dependencies=secured)
    def document_preview(
        document_id: int, page: int = Query(1, ge=1), width: int = Query(1000, ge=100, le=MAX_PREVIEW_WIDTH)
    ) -> Response:
        """A page as PNG, for the review screen (PDFs rendered, photos turned upright)."""
        from wareneinsatz.ingest.errors import ExtractionError
        from wareneinsatz.reading.pages import load_pages

        with ctx.transaction() as session:
            document = _document(session, document_id)
            content = ctx.store.open(document.storage_path or "")
        try:
            pages = load_pages(content, max_pages=page)
        except ExtractionError as error:
            raise HTTPException(415, f"No preview: {error}") from error
        if page > len(pages):
            raise HTTPException(404, f"The document has {len(pages)} page(s)")
        image = pages[page - 1].image
        image.thumbnail((width, width * 3))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return Response(buffer.getvalue(), media_type="image/png")

    # --- extraction (OCR + language model) ------------------------------------------------------------

    def _run_extraction(reader: DocumentReader) -> None:
        job = ctx.job
        try:
            with ctx.transaction() as session:
                ids = [d.id for d in received_documents(session)]
            job.total = len(ids)
            for document_id in ids:
                with ctx.transaction() as session:
                    result = extract_document(session, ctx.store, session.get_one(Document, document_id), reader)
                job.outcomes[result.outcome.value] += 1
                job.done += 1
        except LlmUnavailable as error:
            job.error = str(error)
        except Exception as error:  # the job must always finish, or it could never be started again
            job.error = f"{type(error).__name__}: {error}"
        finally:
            job.running, job.finished_at = False, datetime.now(UTC)

    def _status() -> ExtractionStatus:
        job = ctx.job
        with ctx.transaction() as session:
            waiting = session.scalar(select(func.count()).select_from(Document).where(Document.status == "received"))
        return ExtractionStatus(
            running=job.running, started_at=job.started_at, finished_at=job.finished_at, total=job.total,
            done=job.done, outcomes=dict(job.outcomes), error=job.error, waiting=waiting or 0,
        )  # fmt: skip

    @app.post("/extraction", tags=["documents"], dependencies=secured, status_code=202)
    def start_extraction(background: BackgroundTasks) -> ExtractionStatus:
        """Read every document waiting for OCR in the background. Poll GET /extraction for progress."""
        job = ctx.job
        with job.lock:
            if job.running:
                return _status()
            try:
                reader = ctx.reader_factory()
            except (LlmUnavailable, OcrUnavailable) as error:
                raise HTTPException(503, str(error)) from error
            job.running, job.started_at, job.finished_at = True, datetime.now(UTC), None
            job.total, job.done, job.outcomes, job.error = 0, 0, Counter(), None
        background.add_task(_run_extraction, reader)
        return _status()

    @app.get("/extraction", tags=["documents"], dependencies=secured)
    def extraction_status() -> ExtractionStatus:
        return _status()

    # --- review ---------------------------------------------------------------------------------------

    def _invoice(session: Session, invoice_id: int) -> Invoice:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            raise HTTPException(404, "No such invoice")
        return invoice

    def _summary(invoice: Invoice) -> InvoiceSummary:
        issues = invoice.review_issues or []
        return InvoiceSummary(
            id=invoice.id, document_id=invoice.document_id, filename=invoice.document.filename,
            supplier=invoice.supplier.name, invoice_number=invoice.invoice_number, issue_date=invoice.issue_date,
            gross_total=invoice.gross_total, currency=invoice.currency, status=invoice.status,
            errors=sum(i.get("severity") == "error" for i in issues),
            warnings=sum(i.get("severity") == "warning" for i in issues),
            extraction_method=invoice.document.extraction_method, corrected_by=invoice.corrected_by,
            approved_by=invoice.approved_by, rejected_reason=invoice.rejected_reason,
        )  # fmt: skip

    def _line(line: InvoiceLine) -> LineOut:
        return LineOut(
            id=line.id, invoice_id=line.invoice_id, line_no=line.line_no, supplier=line.invoice.supplier.name,
            description=line.description, quantity=line.quantity, unit=line.unit_raw or line.unit_code,
            line_amount=line.line_amount, match_status=line.match_status, match_method=line.match_method,
            match_score=line.match_score, ingredient_key=line.ingredient.key if line.ingredient else None,
            base_quantity=line.base_quantity, base_unit=line.ingredient.base_unit if line.ingredient else None,
        )  # fmt: skip

    def _detail(invoice: Invoice) -> InvoiceDetail:
        return InvoiceDetail(
            **_summary(invoice).model_dump(),
            data=invoice_to_data(invoice),
            issues=[IssueOut(**issue) for issue in invoice.review_issues or []],
            lines=[_line(line) for line in invoice.lines],
            raw_extraction=invoice.document.raw_extraction,
        )

    @app.get("/invoices", tags=["review"], dependencies=secured)
    def invoices(status: str | None = None, limit: int = Query(200, le=1000)) -> list[InvoiceSummary]:
        with ctx.transaction() as session:
            query = select(Invoice).order_by(Invoice.issue_date.desc().nulls_first(), Invoice.id.desc()).limit(limit)
            if status:
                query = query.where(Invoice.status == status)
            return [_summary(invoice) for invoice in session.scalars(query)]

    @app.get("/invoices/{invoice_id}", tags=["review"], dependencies=secured)
    def invoice(invoice_id: int) -> InvoiceDetail:
        with ctx.transaction() as session:
            return _detail(_invoice(session, invoice_id))

    @app.put("/invoices/{invoice_id}", tags=["review"], dependencies=secured)
    def correct(invoice_id: int, body: ReviewIn) -> InvoiceDetail:
        """Save the reviewer's corrections and run the checks again. The invoice stays in review."""
        with ctx.transaction() as session:
            invoice = _invoice(session, invoice_id)
            try:
                correct_invoice(session, invoice, body.data, by=body.by)
            except (ReviewError, DuplicateInvoiceError) as error:
                raise HTTPException(409, str(error)) from error
            session.refresh(invoice)
            return _detail(invoice)

    @app.post("/invoices/{invoice_id}/approve", tags=["review"], dependencies=secured)
    def approve(invoice_id: int, body: ByIn) -> InvoiceSummary:
        with ctx.transaction() as session:
            invoice = _invoice(session, invoice_id)
            try:
                approve_invoice(session, invoice, by=body.by)
            except ReviewError as error:
                raise HTTPException(409, str(error)) from error
            return _summary(invoice)

    @app.post("/invoices/{invoice_id}/reject", tags=["review"], dependencies=secured)
    def reject(invoice_id: int, body: RejectIn) -> InvoiceSummary:
        with ctx.transaction() as session:
            invoice = _invoice(session, invoice_id)
            try:
                reject_invoice(session, invoice, by=body.by, reason=body.reason)
            except ReviewError as error:
                raise HTTPException(409, str(error)) from error
            return _summary(invoice)

    # --- matching -------------------------------------------------------------------------------------

    @app.post("/matching", tags=["matching"], dependencies=secured)
    def run_matching(embeddings: bool = True) -> list[LineOut]:
        """Suggest an ingredient for every line that has none yet (memory first)."""
        embedder = ctx.embedder_factory() if embeddings else None
        with ctx.transaction() as session:
            try:
                matcher = build_matcher(session, embedder=embedder)
            except UnknownIngredientError as error:
                raise HTTPException(409, str(error)) from error
            results = match_open_lines(session, matcher)
            lines = [session.get_one(InvoiceLine, r.line_id) for r in results]
            return [_line(line) for line in lines]

    @app.get("/lines", tags=["matching"], dependencies=secured)
    def lines(status: str = "suggested", limit: int = Query(500, le=5000)) -> list[LineOut]:
        with ctx.transaction() as session:
            query = (
                select(InvoiceLine)
                .join(Invoice)
                .where(InvoiceLine.match_status == status, Invoice.status != "rejected")
                .order_by(InvoiceLine.invoice_id, InvoiceLine.line_no)
                .limit(limit)
            )
            return [_line(line) for line in session.scalars(query)]

    @app.post("/lines/{line_id}/confirm", tags=["matching"], dependencies=secured)
    def confirm(line_id: int, body: ConfirmIn) -> ConfirmOut:
        with ctx.transaction() as session:
            if session.get(InvoiceLine, line_id) is None:
                raise HTTPException(404, "No such invoice line")
            try:
                product, confirmed = confirm_line(
                    session, line_id, ingredient_key=body.ingredient_key, base_qty_per_unit=body.per_unit,
                    confirmed_by=body.by,
                )  # fmt: skip
            except UnknownIngredientError as error:
                raise HTTPException(422, str(error)) from error
            return ConfirmOut(
                ingredient_key=product.ingredient.key,
                base_qty_per_unit=product.base_qty_per_unit,
                confirmed_line_ids=confirmed,
            )

    @app.get("/ingredients", tags=["matching"], dependencies=secured)
    def ingredients() -> list[IngredientOut]:
        with ctx.transaction() as session:
            return [
                IngredientOut(key=i.key, name=i.name, base_unit=i.base_unit, category=i.category, is_food=i.is_food)
                for i in session.scalars(select(Ingredient).order_by(Ingredient.name))
            ]

    # --- kitchen: menu, prices, alerts, purchases ----------------------------------------------------

    def _dish(cost: RecipeCost) -> DishOut:
        return DishOut(
            key=cost.key, name=cost.name, kind=cost.kind, menu_price_gross=cost.menu_price_gross,
            net_price=_round(cost.net_price), cost=_round(cost.cost), food_cost_pct=_round(cost.food_cost_pct, 1),
            target_food_cost_pct=cost.target_food_cost_pct, margin=_round(cost.margin),
            over_target=cost.over_target, missing=cost.missing,
        )  # fmt: skip

    @app.get("/menu", tags=["kitchen"], dependencies=secured)
    def menu(pricing: str = Query("latest", pattern="^(latest|average)$"), until: date | None = None) -> list[DishOut]:
        with ctx.transaction() as session:
            costs = cost_menu(session, method=pricing, until=until)
        return sorted((_dish(c) for c in costs.values() if c.kind == "dish"), key=lambda d: d.name)

    @app.get("/menu/{key}", tags=["kitchen"], dependencies=secured)
    def dish(key: str, pricing: str = Query("latest", pattern="^(latest|average)$"), until: date | None = None
             ) -> DishDetail:  # fmt: skip
        with ctx.transaction() as session:
            costs = cost_menu(session, method=pricing, until=until)
        if key not in costs:
            raise HTTPException(404, "No such recipe")
        cost = costs[key]
        items = [
            ItemCostOut(
                label=i.label, source=i.source, key=i.key, quantity=i.quantity, base_unit=i.base_unit,
                waste_pct=i.waste_pct, price_per_base=_round(i.price_per_base, 6), cost=_round(i.cost, 4),
            )
            for i in cost.items
        ]  # fmt: skip
        return DishDetail(**_dish(cost).model_dump(), items=items, yield_quantity=cost.yield_quantity,
                          yield_unit=cost.yield_unit)  # fmt: skip

    @app.get("/prices", tags=["kitchen"], dependencies=secured)
    def prices(ingredient: str | None = None) -> list[PricePointOut]:
        with ctx.transaction() as session:
            return [
                PricePointOut(issue_date=p.issue_date, supplier=p.supplier_name, ingredient_key=p.ingredient_key,
                              ingredient_name=p.ingredient_name, price=p.price, display_unit=p.display_unit)
                for p in price_history(session, ingredient)
            ]  # fmt: skip

    def _alert(alert: Alert) -> AlertOut:
        return AlertOut(id=alert.id, kind=alert.kind, subject=alert.subject, message=alert.message,
                        happened_on=alert.happened_on, acknowledged_by=alert.acknowledged_by,
                        details=alert.details)  # fmt: skip

    @app.get("/alerts", tags=["kitchen"], dependencies=secured)
    def alerts() -> list[AlertOut]:
        """Open alerts, newest first."""
        with ctx.transaction() as session:
            return [_alert(alert) for alert in open_alerts(session)]

    @app.post("/alerts/check", tags=["kitchen"], dependencies=secured)
    def alerts_check() -> AlertCheckOut:
        with ctx.transaction() as session:
            result = check_alerts(session)
            return AlertCheckOut(new=result.new, open=len(open_alerts(session)))

    @app.post("/alerts/{alert_id}/ack", tags=["kitchen"], dependencies=secured)
    def alert_ack(alert_id: int, body: ByIn) -> AlertOut:
        with ctx.transaction() as session:
            alert = session.get(Alert, alert_id)
            if alert is None:
                raise HTTPException(404, "No such alert")
            alert.acknowledged_at, alert.acknowledged_by = datetime.now(UTC), body.by
            return _alert(alert)

    @app.get("/purchases/monthly", tags=["kitchen"], dependencies=secured)
    def purchases(months: int = Query(12, ge=1, le=60)) -> list[MonthOut]:
        with ctx.transaction() as session:
            return [
                MonthOut(month=m.month, food=_round(m.food), non_food=_round(m.non_food),
                         unmatched=_round(m.unmatched), by_category={k: _round(v) for k, v in m.by_category.items()})
                for m in monthly_purchases(session, months=months)
            ]  # fmt: skip

    @app.get("/summary", tags=["kitchen"], dependencies=secured)
    def summary() -> SummaryOut:
        with ctx.transaction() as session:
            count = lambda query: session.scalar(select(func.count()).select_from(query.subquery())) or 0  # noqa: E731
            months = monthly_purchases(session, months=2)
            costs = [c for c in cost_menu(session).values() if c.kind == "dish"]
            return SummaryOut(
                invoices_waiting_review=count(select(Invoice.id).where(Invoice.status == "needs_review")),
                documents_waiting_ocr=count(select(Document.id).where(Document.status == "received")),
                suggestions_open=count(select(InvoiceLine.id).where(InvoiceLine.match_status == "suggested")),
                open_alerts=count(select(Alert.id).where(Alert.acknowledged_at.is_(None))),
                dishes_over_target=sum(c.over_target for c in costs),
                dishes_incomplete=sum(not c.complete for c in costs),
                wareneinsatz_this_month=_round(months[-1].food) if months else None,
                wareneinsatz_last_month=_round(months[-2].food) if len(months) > 1 else None,
                latest_month=months[-1].month if months else None,
            )

    return app


def _round(value: Decimal | None, places: int = 2) -> Decimal | None:
    return None if value is None else round(value, places)
