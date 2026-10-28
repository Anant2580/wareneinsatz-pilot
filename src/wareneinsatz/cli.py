"""Command line interface.

uv run wareneinsatz ingest invoice.xml           read one file
uv run wareneinsatz ingest inbox/ --verbose      a folder (searched recursively), also showing warnings
uv run wareneinsatz extract                      read stored PDFs and photos with OCR + a language model
uv run wareneinsatz review                       invoices waiting for a person, with the reasons
uv run wareneinsatz eval generate                build the evaluation set in eval/dataset
uv run wareneinsatz eval run --extractor NAME    score an extractor, report in eval/results
uv run wareneinsatz eval run --extractor llm-hybrid --model qwen3-vl:4b-instruct --only photo
uv run wareneinsatz --version
"""

import argparse
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

from psycopg.errors import UndefinedTable
from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from wareneinsatz import __version__
from wareneinsatz.config import get_settings
from wareneinsatz.db import make_engine, session_scope
from wareneinsatz.ingest.pipeline import IngestResult, Outcome, extract_document, ingest_file
from wareneinsatz.ingest.store import DocumentStore
from wareneinsatz.models import Document, Invoice
from wareneinsatz.reading import DocumentReader, LlmUnavailable, OcrUnavailable, make_reader
from wareneinsatz.repository import received_documents

LABELS = {
    Outcome.APPROVED: "approved",
    Outcome.NEEDS_REVIEW: "review",
    Outcome.NEEDS_OCR: "needs OCR",
    Outcome.DUPLICATE: "duplicate",
    Outcome.FAILED: "failed",
}
INDENT = " " * 11  # lines up issue lists under the file names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wareneinsatz", description="Food-cost copilot for restaurants.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser(
        "ingest",
        help="read supplier invoices into the database",
        description="Reads XRechnung (UBL/CII) and ZUGFeRD/Factur-X invoices exactly and checks them. "
        "PDFs without XML and photos are stored for OCR (step 5).",
    )
    ingest.add_argument("paths", nargs="+", type=Path, help="files or folders (folders are searched recursively)")
    ingest.add_argument("-v", "--verbose", action="store_true", help="also list warnings of approved invoices")

    extract = commands.add_parser(
        "extract",
        help="read stored PDFs and photos with OCR + a language model (step 5)",
        description="Reads every document waiting with status 'received' (scans, photos, PDFs without XML) "
        "with OCR and a language model (Ollama by default, see .env). Results always go to review.",
    )
    extract.add_argument("--limit", type=int, help="read at most this many documents")
    _add_reader_options(extract)
    extract.add_argument("-v", "--verbose", action="store_true", help="also list warnings")

    commands.add_parser("review", help="list invoices waiting for review, with the reasons")

    evaluation = commands.add_parser("eval", help="evaluation set and accuracy measurement (step 4)")
    eval_commands = evaluation.add_subparsers(dest="eval_command", required=True)
    generate = eval_commands.add_parser("generate", help="build the evaluation set (seeded, reproducible)")
    generate.add_argument("--out", type=Path, default=Path("eval/dataset"))
    generate.add_argument("--seed", type=int, default=2026)
    generate.add_argument("--photos", type=int, default=20, help="how many photo versions to create")
    run = eval_commands.add_parser("run", help="score an extractor on a dataset and write a report")
    run.add_argument(
        "--extractor",
        default="pdf-text-baseline",
        help="oracle, einvoice, pdf-text-baseline, llm-text, llm-vision or llm-hybrid",
    )
    run.add_argument("--dataset", type=Path, default=Path("eval/dataset"))
    run.add_argument("--out", type=Path, default=Path("eval/results"))
    run.add_argument("--only", help="a subset: photo, pdf, synthetic, kosit, wholesaler, cash_carry or receipt")
    run.add_argument("--limit", type=int, help="only the first n documents (of the subset)")
    _add_reader_options(run, with_mode=False)  # the mode is part of the extractor name
    template = eval_commands.add_parser("template", help="create a truth file to fill in for your own receipt")
    template.add_argument("document", type=Path)

    args = parser.parse_args(argv)
    if args.command == "ingest":
        return run_ingest(args.paths, verbose=args.verbose)
    if args.command == "extract":
        return run_extract(
            limit=args.limit,
            overrides={"mode": args.mode, "model": args.model, "ocr": args.ocr, "base_url": args.base_url},
            verbose=args.verbose,
        )
    if args.command == "review":
        return run_review()
    if args.command == "eval":
        return run_eval(args)
    return 2  # argparse already rejects unknown commands; kept for completeness


def _add_reader_options(parser: argparse.ArgumentParser, *, with_mode: bool = True) -> None:
    """Overrides for the step 5 reader; without them the values from .env (or the defaults) are used."""
    if with_mode:
        parser.add_argument("--mode", choices=["text", "vision", "hybrid"], help="what the model gets (default hybrid)")
    parser.add_argument("--model", help="e.g. qwen3-vl:8b-instruct (must be pulled in Ollama)")
    parser.add_argument("--ocr", choices=["tesseract", "none"], help="OCR engine for photos and scans")
    parser.add_argument("--base-url", help="model server, default http://localhost:11434 (Ollama)")


def run_eval(args: argparse.Namespace, out: TextIO | None = None) -> int:
    """The evaluation commands. Imported here: they need the development dependencies."""
    out = out or sys.stdout  # looked up now, not when the function was defined (pytest capture)
    try:
        from wareneinsatz.evaluation.dataset import generate_dataset, write_truth_template
        from wareneinsatz.evaluation.harness import evaluate, write_report
    except ImportError as error:
        print(f"Evaluation needs the development dependencies ({error.name}). Run: uv sync", file=out)
        return 2

    if args.eval_command == "generate":
        dataset = generate_dataset(args.out, seed=args.seed, photos=args.photos, kosit_dir=Path("tests/samples/kosit"))
        kinds = Counter(f"{item.source} {item.kind}" for item in dataset.items)
        print(
            f"{len(dataset.items)} documents in {args.out}: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())),
            file=out,
        )
        return 0
    if args.eval_command == "template":
        print(f"Fill in {write_truth_template(args.document)} with what the document shows.", file=out)
        return 0

    if not args.dataset.exists():
        print(f"No dataset in {args.dataset}. Create it with: uv run wareneinsatz eval generate", file=out)
        return 2
    from wareneinsatz.evaluation.extractors import get_extractor

    overrides = {"model": args.model, "ocr": args.ocr, "base_url": args.base_url}
    try:
        extractor = get_extractor(args.extractor, **{k: v for k, v in overrides.items() if v})
        if extractor.settings:
            print("Settings: " + ", ".join(f"{k} {v}" for k, v in extractor.settings.items() if v), file=out)
        progress = _print_progress(out) if args.extractor.startswith("llm-") else None
        report = evaluate(args.dataset, extractor, only=args.only, limit=args.limit, progress=progress)
    except (ValueError, LlmUnavailable, OcrUnavailable) as error:
        print(error, file=out)
        return 2
    markdown, data = write_report(report, args.out)
    scores = report.summary()["scores"]
    docs = report.summary()["documents"]
    print(f"{args.extractor} on {len(report.results)} documents:", file=out)
    for label, key in [("all fields", "all_fields"), ("header", "header_fields"), ("lines", "line_fields")]:
        sc = scores[key]
        print(
            f"  {label:<11} precision {_pct(sc['precision'])}  recall {_pct(sc['recall'])}  F1 {_pct(sc['f1'])}",
            file=out,
        )
    print(
        f"  with result {docs['with_result']}, completely correct {docs['fully_correct']}, "
        f"would need review {docs['needs_review']}",
        file=out,
    )
    print(f"Report: {markdown}", file=out)
    return 0


def _pct(value: float | None) -> str:
    return "  –  " if value is None else f"{value:6.1%}"


def _print_progress(out: TextIO):
    """One row per document for slow (language model) runs, so a long run shows it is alive."""

    def progress(number: int, total: int, result) -> None:
        from wareneinsatz.evaluation.metrics import HEADER_FIELDS, LINE_FIELDS, micro

        f1 = micro(result.score.fields, [*HEADER_FIELDS, *LINE_FIELDS]).f1
        status = f"F1 {_pct(f1)}" if result.predicted else f"no result: {result.error or '-'}"[:90]
        print(f"[{number:>3}/{total}] {result.item.id:<32} {result.seconds:6.1f} s  {status}", file=out, flush=True)

    return progress


def find_files(paths: Iterable[Path]) -> tuple[list[Path], list[Path]]:
    """Expand folders into their files. Returns (files, paths that don't exist).

    Hidden files such as macOS .DS_Store are skipped.
    """
    files: list[Path] = []
    missing: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(
                sorted(
                    p
                    for p in path.rglob("*")
                    if p.is_file() and not any(part.startswith(".") for part in p.relative_to(path).parts)
                )
            )
        elif path.is_file():
            files.append(path)
        else:
            missing.append(path)
    unique = list(dict.fromkeys(files))  # same file named twice -> process once, keep the order
    return unique, missing


def run_ingest(
    paths: Iterable[Path],
    *,
    verbose: bool = False,
    engine: Engine | None = None,
    store: DocumentStore | None = None,
    out: TextIO | None = None,
) -> int:
    """Ingest every file in its own transaction, so one bad file never undoes the others.

    Exit code: 0 all fine, 1 some files failed, 2 database not reachable or not migrated.
    """
    out = out or sys.stdout  # looked up now, not when the function was defined (pytest capture)
    files, missing = find_files(paths)
    for path in missing:
        print(f"{'missing':<10} {path}  (no such file or folder)", file=out)
    if not files:
        print("No files to ingest.", file=out)
        return 1 if missing else 0

    engine = engine or make_engine()
    store = store or DocumentStore(get_settings().document_dir)
    counts: Counter[Outcome] = Counter()

    for path in files:
        try:
            with session_scope(engine) as session:
                result = ingest_file(session, store, path)
        except OperationalError as error:
            return _database_unreachable(error, out)
        except ProgrammingError as error:
            if isinstance(error.orig, UndefinedTable):
                return _tables_missing(out)
            result = IngestResult(path.name, Outcome.FAILED, f"Unexpected error: {type(error).__name__}: {error}")
        except Exception as error:  # report it and carry on with the next file
            result = IngestResult(path.name, Outcome.FAILED, f"Unexpected error: {type(error).__name__}: {error}")
        counts[result.outcome] += 1
        _print_result(result, verbose, out)

    summary = ", ".join(f"{counts[outcome]} {LABELS[outcome]}" for outcome in Outcome)
    print(f"\n{len(files)} file(s): {summary}", file=out)
    if counts[Outcome.NEEDS_OCR]:
        print("Read the scans and photos with OCR + a language model: uv run wareneinsatz extract", file=out)
    return 1 if counts[Outcome.FAILED] or missing else 0


def run_extract(
    *,
    limit: int | None = None,
    overrides: dict[str, str | None] | None = None,
    verbose: bool = False,
    engine: Engine | None = None,
    store: DocumentStore | None = None,
    reader: DocumentReader | None = None,
    out: TextIO | None = None,
) -> int:
    """Read every document waiting for OCR, each in its own transaction.

    Exit code: 0 all fine, 1 some documents failed, 2 database or model server not available.
    """
    out = out or sys.stdout  # looked up now, not when the function was defined (pytest capture)
    engine = engine or make_engine()
    store = store or DocumentStore(get_settings().document_dir)
    try:
        with session_scope(engine) as session:
            ids = [document.id for document in received_documents(session, limit=limit)]
    except OperationalError as error:
        return _database_unreachable(error, out)
    except ProgrammingError as error:
        if isinstance(error.orig, UndefinedTable):
            return _tables_missing(out)
        raise
    if not ids:
        print("No documents waiting for OCR.", file=out)
        return 0
    if reader is None:
        try:
            options = {key: value for key, value in (overrides or {}).items() if value}
            reader = make_reader(get_settings(), **options)
            reader.llm.check()
        except (LlmUnavailable, OcrUnavailable) as error:
            print(error, file=out)
            return 2
    ocr = f" + {reader.ocr.name}" if reader.ocr else ""
    print(f"Reading {len(ids)} document(s) with {reader.llm.name}{ocr}, {reader.mode.value} mode", file=out)

    counts: Counter[Outcome] = Counter()
    for document_id in ids:
        try:
            with session_scope(engine) as session:
                document = session.get_one(Document, document_id)
                result = extract_document(session, store, document, reader)
        except LlmUnavailable as error:
            print(f"Stopped: {error}", file=out)
            return 2
        counts[result.outcome] += 1
        _print_result(result, verbose, out)

    summary = ", ".join(f"{counts[outcome]} {LABELS[outcome]}" for outcome in Outcome if counts[outcome])
    print(f"\n{len(ids)} document(s): {summary}", file=out)
    return 1 if counts[Outcome.FAILED] else 0


def run_review(*, engine: Engine | None = None, out: TextIO | None = None) -> int:
    """Print every invoice with status needs_review and its findings, oldest first."""
    out = out or sys.stdout  # looked up now, not when the function was defined (pytest capture)
    engine = engine or make_engine()
    try:
        with session_scope(engine) as session:
            invoices = session.scalars(
                select(Invoice).where(Invoice.status == "needs_review").order_by(Invoice.created_at, Invoice.id)
            ).all()
            if not invoices:
                print("No invoices waiting for review.", file=out)
                return 0
            for invoice in invoices:
                print(
                    f"#{invoice.id:<5} {invoice.issue_date or '(no date)'}  {invoice.supplier.name}"
                    f" | invoice {invoice.invoice_number or '-'} | {invoice.gross_total} {invoice.currency}",
                    file=out,
                )
                for issue in invoice.review_issues:
                    print(f"{INDENT}{issue['severity']:<8} {issue['message']}", file=out)
            print(f"\n{len(invoices)} invoice(s) waiting for review", file=out)
    except OperationalError as error:
        return _database_unreachable(error, out)
    except ProgrammingError as error:
        if isinstance(error.orig, UndefinedTable):
            return _tables_missing(out)
        raise
    return 0


def _print_result(result: IngestResult, verbose: bool, out: TextIO) -> None:
    timing = f"  ({result.method}, {result.extraction_ms} ms)" if result.method else ""
    warnings = [issue for issue in result.issues if issue.severity == "warning"]
    errors = [issue for issue in result.issues if issue.severity == "error"]
    note = f"  [{len(warnings)} warning(s)]" if warnings and not (verbose or errors) else ""
    print(f"{LABELS[result.outcome]:<10} {result.filename}  {result.message}{timing}{note}", file=out)
    # Errors are always listed; warnings when the invoice needs review anyway, or with --verbose.
    shown = errors + (warnings if verbose or errors else [])
    for issue in shown:
        print(f"{INDENT}{issue.severity:<8} {issue.message}", file=out)


def _tables_missing(out: TextIO) -> int:
    print("The database has no tables yet. Create them with: uv run alembic upgrade head", file=out)
    return 2


def _database_unreachable(error: OperationalError, out: TextIO) -> int:
    print(f"Cannot reach the database: {error.orig}", file=out)
    print("Is it running? Start it with: docker compose up -d --wait db", file=out)
    return 2
