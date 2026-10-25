"""Tests for the `wareneinsatz` command line."""

import io
import shutil

import pytest
from sqlalchemy import text

from wareneinsatz import __version__
from wareneinsatz.cli import find_files, main, run_ingest
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
    assert lines[0].startswith("stored") and "RE-2026-104233" in lines[0]
    assert lines[1].startswith("duplicate")
    assert lines[2].startswith("needs OCR")
    assert lines[3].startswith("failed")
    assert lines[-1] == "4 file(s): 1 stored, 1 needs OCR, 1 duplicate, 1 failed"
    assert exit_code == 1  # one file failed
