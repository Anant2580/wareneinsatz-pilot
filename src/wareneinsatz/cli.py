"""Command line interface.

uv run wareneinsatz ingest invoice.xml           one file
uv run wareneinsatz ingest inbox/                a folder (searched recursively)
uv run wareneinsatz --version
"""

import argparse
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from wareneinsatz import __version__
from wareneinsatz.config import get_settings
from wareneinsatz.db import make_engine, session_scope
from wareneinsatz.ingest.pipeline import IngestResult, Outcome, ingest_file
from wareneinsatz.ingest.store import DocumentStore

LABELS = {
    Outcome.STORED: "stored",
    Outcome.NEEDS_OCR: "needs OCR",
    Outcome.DUPLICATE: "duplicate",
    Outcome.FAILED: "failed",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wareneinsatz", description="Food-cost copilot for restaurants.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser(
        "ingest",
        help="read supplier invoices into the database",
        description="Reads XRechnung (UBL/CII) and ZUGFeRD/Factur-X invoices exactly. "
        "PDFs without XML and photos are stored for OCR (step 5).",
    )
    ingest.add_argument("paths", nargs="+", type=Path, help="files or folders (folders are searched recursively)")
    args = parser.parse_args(argv)

    if args.command == "ingest":
        return run_ingest(args.paths)
    return 2  # argparse already rejects unknown commands; kept for completeness


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
    engine: Engine | None = None,
    store: DocumentStore | None = None,
    out: TextIO = sys.stdout,
) -> int:
    """Ingest every file in its own transaction, so one bad file never undoes the others.

    Exit code: 0 all fine, 1 some files failed, 2 database not reachable.
    """
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
            print(f"Cannot reach the database: {error.orig}", file=out)
            print("Is it running? Start it with: docker compose up -d --wait db", file=out)
            return 2
        except Exception as error:  # report it and carry on with the next file
            result = IngestResult(path.name, Outcome.FAILED, f"Unexpected error: {type(error).__name__}: {error}")
        counts[result.outcome] += 1
        timing = f"  ({result.method}, {result.extraction_ms} ms)" if result.method else ""
        print(f"{LABELS[result.outcome]:<10} {path.name}  {result.message}{timing}", file=out)

    summary = ", ".join(f"{counts[outcome]} {LABELS[outcome]}" for outcome in Outcome)
    print(f"\n{len(files)} file(s): {summary}", file=out)
    return 1 if counts[Outcome.FAILED] or missing else 0
