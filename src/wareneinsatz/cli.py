"""Command line interface.

uv run wareneinsatz ingest invoice.xml           read one file
uv run wareneinsatz ingest inbox/ --verbose      a folder (searched recursively), also showing warnings
uv run wareneinsatz review                       invoices waiting for a person, with the reasons
uv run wareneinsatz eval generate                build the evaluation set in eval/dataset
uv run wareneinsatz eval run --extractor NAME    score an extractor, report in eval/results
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
from wareneinsatz.ingest.pipeline import IngestResult, Outcome, ingest_file
from wareneinsatz.ingest.store import DocumentStore
from wareneinsatz.models import Invoice

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

    commands.add_parser("review", help="list invoices waiting for review, with the reasons")

    evaluation = commands.add_parser("eval", help="evaluation set and accuracy measurement (step 4)")
    eval_commands = evaluation.add_subparsers(dest="eval_command", required=True)
    generate = eval_commands.add_parser("generate", help="build the evaluation set (seeded, reproducible)")
    generate.add_argument("--out", type=Path, default=Path("eval/dataset"))
    generate.add_argument("--seed", type=int, default=2026)
    generate.add_argument("--photos", type=int, default=20, help="how many photo versions to create")
    run = eval_commands.add_parser("run", help="score an extractor on a dataset and write a report")
    run.add_argument("--extractor", default="pdf-text-baseline", help="oracle, einvoice or pdf-text-baseline")
    run.add_argument("--dataset", type=Path, default=Path("eval/dataset"))
    run.add_argument("--out", type=Path, default=Path("eval/results"))
    template = eval_commands.add_parser("template", help="create a truth file to fill in for your own receipt")
    template.add_argument("document", type=Path)

    args = parser.parse_args(argv)
    if args.command == "ingest":
        return run_ingest(args.paths, verbose=args.verbose)
    if args.command == "review":
        return run_review()
    if args.command == "eval":
        return run_eval(args)
    return 2  # argparse already rejects unknown commands; kept for completeness


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
    try:
        report = evaluate(args.dataset, args.extractor)
    except ValueError as error:
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
    return 1 if counts[Outcome.FAILED] or missing else 0


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
