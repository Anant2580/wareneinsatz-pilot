"""Tests for the `wareneinsatz` command line."""

import io
import shutil

import pytest
from sqlalchemy import text

from wareneinsatz import __version__
from wareneinsatz.cli import find_files, main, run_extract, run_ingest, run_review
from wareneinsatz.evaluation.photo import photo_of
from wareneinsatz.evaluation.render import render_pdf
from wareneinsatz.evaluation.synthetic import InvoiceGenerator, Layout
from wareneinsatz.ingest.extract import parse_einvoice_xml
from wareneinsatz.ingest.store import DocumentStore
from wareneinsatz.reading import DocumentReader, LlmUnavailable, Mode


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
    assert lines[-2] == "4 file(s): 1 approved, 0 review, 1 needs OCR, 1 duplicate, 1 failed"
    assert lines[-1].endswith("uv run wareneinsatz extract")  # what to do with the scan
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


# --- step 5: extract ------------------------------------------------------------------------------------


def _store_photo(engine, tmp_path, content: bytes) -> DocumentStore:
    """Ingest a photo: it is stored with status 'received' and waits for extract."""
    (tmp_path / "bon.jpg").write_bytes(content)
    store = DocumentStore(tmp_path / "store")
    out = io.StringIO()
    run_ingest([tmp_path / "bon.jpg"], engine=engine, store=store, out=out)
    assert out.getvalue().startswith("needs OCR")
    return store


def _rows(engine, sql: str):
    with engine.connect() as connection:
        return connection.execute(text(sql)).all()


def test_extract_reads_waiting_photos_and_sends_them_to_review(clean_database, tmp_path, fake_llm, printed_of):
    receipt = InvoiceGenerator(5).generate("receipt")
    store = _store_photo(clean_database, tmp_path, photo_of(render_pdf(receipt.data, receipt.layout), seed=3))
    reader = DocumentReader(fake_llm(printed_of(receipt.data, receipt.layout).model_dump_json()), Mode.VISION)

    out = io.StringIO()
    assert run_extract(engine=clean_database, store=store, reader=reader, out=out) == 0
    printed = out.getvalue()
    assert "Reading 1 document(s) with fake:fake-vl:1b, vision mode" in printed
    assert f"review     bon.jpg  {receipt.data.supplier.name}" in printed
    assert printed.rstrip().endswith("1 document(s): 1 review")

    [(status, method, raw)] = _rows(clean_database, "SELECT status, extraction_method, raw_extraction FROM documents")
    assert (status, method) == ("extracted", "llm-vision")
    assert raw["printed"]["gross_total"] and raw["reader"]["prompt_version"]  # the untouched reply + settings
    # Read by a model, so never approved automatically, even without validation errors.
    assert _rows(clean_database, "SELECT status, approved_by FROM invoices") == [("needs_review", None)]

    out = io.StringIO()
    assert run_extract(engine=clean_database, store=store, reader=reader, out=out) == 0
    assert out.getvalue() == "No documents waiting for OCR.\n"


def test_extract_marks_unreadable_documents_as_failed(clean_database, tmp_path, fake_llm, printed_of):
    receipt = InvoiceGenerator(6).generate("receipt")
    store = _store_photo(clean_database, tmp_path, photo_of(render_pdf(receipt.data, receipt.layout), seed=4))
    printed = printed_of(receipt.data, receipt.layout).model_copy(update={"lines": [], "gross_total": None})
    reader = DocumentReader(fake_llm(printed.model_dump_json()), Mode.VISION)

    out = io.StringIO()
    assert run_extract(engine=clean_database, store=store, reader=reader, out=out) == 1
    assert "failed     bon.jpg  Not a complete invoice: lines" in out.getvalue()
    [(status, error, raw)] = _rows(clean_database, "SELECT status, error, raw_extraction FROM documents")
    assert status == "failed" and error.startswith("Not a complete invoice") and raw["printed"]


def test_extract_finds_the_photo_of_an_invoice_that_is_already_stored(
    clean_database, samples, tmp_path, fake_llm, printed_of
):
    xml = samples / "einvoices" / "musterfrisch_xrechnung_cii.xml"
    store = DocumentStore(tmp_path / "store")
    run_ingest([xml], engine=clean_database, store=store, out=io.StringIO())
    data, _syntax, _profile = parse_einvoice_xml(xml.read_bytes())
    invoice = InvoiceGenerator(1).generate("wholesaler")
    (tmp_path / "kopie.jpg").write_bytes(photo_of(render_pdf(invoice.data, invoice.layout), seed=1))
    run_ingest([tmp_path / "kopie.jpg"], engine=clean_database, store=store, out=io.StringIO())
    # The model reads the paper copy of the e-invoice: same supplier, same invoice number.
    reader = DocumentReader(fake_llm(printed_of(data, Layout("wholesaler", {})).model_dump_json()), Mode.VISION)

    out = io.StringIO()
    run_extract(engine=clean_database, store=store, reader=reader, out=out)
    assert "duplicate  kopie.jpg  Invoice 'RE-2026-104233'" in out.getvalue()
    statuses = _rows(clean_database, "SELECT filename, status FROM documents ORDER BY id")
    assert statuses == [("musterfrisch_xrechnung_cii.xml", "extracted"), ("kopie.jpg", "failed")]
    assert len(_rows(clean_database, "SELECT id FROM invoices")) == 1


def test_extract_stops_when_the_model_server_is_gone(clean_database, tmp_path, fake_llm):
    receipt = InvoiceGenerator(7).generate("receipt")
    store = _store_photo(clean_database, tmp_path, photo_of(render_pdf(receipt.data, receipt.layout), seed=5))

    class GoneLlm(fake_llm):
        def complete(self, **request):
            raise LlmUnavailable("Ollama is not reachable at http://localhost:11434")

    out = io.StringIO()
    assert (
        run_extract(engine=clean_database, store=store, reader=DocumentReader(GoneLlm("{}"), Mode.VISION), out=out) == 2
    )
    assert "Stopped: Ollama is not reachable" in out.getvalue()
    assert _rows(clean_database, "SELECT status FROM documents") == [("received",)]  # tried again next time
