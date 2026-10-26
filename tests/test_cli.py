"""Tests for the `wareneinsatz` command line."""

import io
import shutil

import pytest
from sqlalchemy import text

from wareneinsatz import __version__
from wareneinsatz.cli import find_files, main, run_ingest, run_review
from wareneinsatz.ingest.store import DocumentStore


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_find_files_expands_folders_and_skips_hidden_files(tmp_path):
    (tmp_path / "inbox" / "oktober").mkdir(parents=True)
    (tmp_path / "inbox" / "a.xml").write_text("<a/>")
    (tmp_path / "inbox" / "oktober" / "b.pdf").write_bytes(b"%PDF-1.7")
    (tmp_path / "inbox" / ".DS_Store").write_bytes(b"\x00")
    single = tmp_path / "inbox" / "a.xml"

    files, missing = find_files([tmp_path / "inbox", single, tmp_path / "gibt-es-nicht.xml"])

    assert [f.name for f in files] == ["a.xml", "b.pdf"]  # a.xml only once, .DS_Store skipped
    assert [m.name for m in missing] == ["gibt-es-nicht.xml"]


@pytest.fixture
def clean_database(engine):
    """The CLI commits for real, so this test empties the tables again afterwards."""
    yield engine
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE invoice_lines, invoice_tax_totals, invoices, documents, suppliers CASCADE"))


def test_ingest_end_to_end(clean_database, samples, tmp_path, make_pdf):
    """A folder with an XRechnung, the same invoice in the other syntax, a scan and a broken file."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(samples / "einvoices" / "musterfrisch_xrechnung_cii.xml", inbox / "1_rechnung_cii.xml")
    shutil.copy(samples / "einvoices" / "musterfrisch_xrechnung_ubl.xml", inbox / "2_rechnung_ubl.xml")
    (inbox / "3_scan.pdf").write_bytes(make_pdf())
    (inbox / "4_kaputt.xml").write_text("<oops>")
    out = io.StringIO()

    exit_code = run_ingest([inbox], engine=clean_database, store=DocumentStore(tmp_path / "store"), out=out)

    lines = out.getvalue().splitlines()
    assert lines[0].startswith("approved") and "RE-2026-104233" in lines[0] and "[1 warning(s)]" in lines[0]
    assert lines[1].startswith("duplicate")
    assert lines[2].startswith("needs OCR")
    assert lines[3].startswith("failed")
    assert lines[-1] == "4 file(s): 1 approved, 0 review, 1 needs OCR, 1 duplicate, 1 failed"
    assert exit_code == 1  # one file failed


def test_review_lists_invoices_with_their_reasons(clean_database, samples, tmp_path):
    xml = (samples / "einvoices" / "musterfrisch_xrechnung_cii.xml").read_bytes()
    wrong = xml.replace(
        b"<ram:LineTotalAmount>39.80</ram:LineTotalAmount>", b"<ram:LineTotalAmount>39.90</ram:LineTotalAmount>"
    )
    (tmp_path / "falsch.xml").write_bytes(wrong)
    store = DocumentStore(tmp_path / "store")
    ingest_out = io.StringIO()
    run_ingest([tmp_path / "falsch.xml"], engine=clean_database, store=store, out=ingest_out)
    assert ingest_out.getvalue().startswith("review")
    assert "error    Line 4: 2 × 19.90 = 39.80, but the line says 39.90" in ingest_out.getvalue()

    out = io.StringIO()
    assert run_review(engine=clean_database, out=out) == 0
    text_out = out.getvalue()
    assert "Musterfrisch Großhandel GmbH | invoice RE-2026-104233" in text_out
    assert "Line 4: 2 × 19.90 = 39.80, but the line says 39.90" in text_out
    assert text_out.rstrip().endswith("1 invoice(s) waiting for review")


def test_review_with_nothing_to_do(clean_database):
    out = io.StringIO()
    assert run_review(engine=clean_database, out=out) == 0
    assert out.getvalue() == "No invoices waiting for review.\n"
