"""Tests for the evaluation set and harness (step 4). No database needed."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfReader
from stdnum.eu import vat as eu_vat

from wareneinsatz.cli import main
from wareneinsatz.evaluation.baseline import extract_with_rules
from wareneinsatz.evaluation.dataset import generate_dataset, load_dataset, write_truth_template
from wareneinsatz.evaluation.extractors import reader_extractor
from wareneinsatz.evaluation.harness import evaluate, select_items, write_report
from wareneinsatz.evaluation.metrics import align_lines, same_unit, score_document, similarity
from wareneinsatz.evaluation.photo import photo_of
from wareneinsatz.evaluation.render import de_money, de_number, render_pdf
from wareneinsatz.evaluation.synthetic import InvoiceGenerator, Layout
from wareneinsatz.reading import DocumentReader, LlmUnavailable, Mode
from wareneinsatz.validation import validate_invoice

KOSIT = Path(__file__).parent / "samples" / "kosit"


# --- the generator ------------------------------------------------------------------------------------


def test_generator_is_reproducible():
    first = [InvoiceGenerator(42).generate().data for _ in range(1)]
    again = [InvoiceGenerator(42).generate().data for _ in range(1)]
    assert first == again


@pytest.mark.parametrize("template", ["wholesaler", "cash_carry", "receipt"])
def test_generated_invoices_are_valid(template):
    """The truth must be a correct invoice: the step 3 validator finds no errors in 40 of each layout."""
    generator = InvoiceGenerator(7)
    for _ in range(40):
        invoice = generator.generate(template)
        result = validate_invoice(invoice.data, today=date(2026, 10, 28))
        assert result.errors == [], [e.message for e in result.errors]


def test_receipts_are_small_invoices_with_gross_prices():
    generator = InvoiceGenerator(3)
    for _ in range(30):
        receipt = generator.generate("receipt").data
        assert receipt.prices_include_vat
        assert receipt.gross_total <= 250  # Kleinbetragsrechnung
        assert receipt.invoice_number is None


def test_invented_vat_ids_can_never_be_real():
    """They are built to fail the official check digit, so no real company has them."""
    generator = InvoiceGenerator(1)
    for vat_id, _tax_number in generator.tax_ids.values():
        assert not eu_vat.is_valid(vat_id)


# --- rendering and photos -----------------------------------------------------------------------------


def test_german_number_formats():
    assert de_money(Decimal("1234.5")) == "1.234,50"
    assert de_number(Decimal("4.255")) == "4,255"
    assert de_number(Decimal("3")) == "3"
    assert de_number(Decimal("12000")) == "12.000"


@pytest.mark.parametrize("template", ["wholesaler", "cash_carry", "receipt"])
def test_rendered_pdf_shows_the_invoice_and_is_reproducible(template):
    invoice = InvoiceGenerator(5).generate(template)
    pdf = render_pdf(invoice.data, invoice.layout)
    assert pdf == render_pdf(invoice.data, invoice.layout)  # byte-identical
    text = PdfReader(__import__("io").BytesIO(pdf)).pages[0].extract_text()
    assert de_money(invoice.data.gross_total) in text
    assert invoice.data.lines[0].description[:20] in text


def test_photo_is_a_reproducible_jpeg():
    invoice = InvoiceGenerator(5).generate("receipt")
    pdf = render_pdf(invoice.data, invoice.layout)
    photo = photo_of(pdf, seed=1)
    assert photo[:3] == b"\xff\xd8\xff"
    assert photo == photo_of(pdf, seed=1)
    assert photo != photo_of(pdf, seed=2)


# --- metrics --------------------------------------------------------------------------------------------


def test_perfect_prediction_scores_100_percent(invoice):
    score = score_document(invoice, invoice)
    assert score.all_correct
    assert all(c.fp == 0 and c.fn == 0 for c in score.fields.values())


def test_missing_and_wrong_values(invoice_dict, invoice):
    from wareneinsatz.schemas import InvoiceData

    invoice_dict["invoice_number"] = None  # missing -> FN
    invoice_dict["gross_total"] = "999.99"  # wrong -> FP and FN
    score = score_document(invoice, InvoiceData.model_validate(invoice_dict))
    assert (score.fields["invoice_number"].fn, score.fields["invoice_number"].fp) == (1, 0)
    assert (score.fields["gross_total"].fn, score.fields["gross_total"].fp) == (1, 1)
    assert not score.all_correct


def test_lines_are_matched_by_content_not_position(invoice_dict, invoice):
    from wareneinsatz.schemas import InvoiceData

    invoice_dict["lines"] = list(reversed(invoice_dict["lines"]))[1:]  # other order, one line missing
    for number, line in enumerate(invoice_dict["lines"], start=1):
        line["line_no"] = number
    score = score_document(invoice, InvoiceData.model_validate(invoice_dict))
    assert score.fields["line_amount"].tp == 6
    assert score.fields["line_amount"].fn == 1  # only the missing line counts as missing
    assert score.fields["line_amount"].fp == 0


def test_small_ocr_typos_in_descriptions_still_match():
    assert similarity("Rinderhackfleisch frisch, 20 % Fett", "Rinderhackfleisch frisch 20 % Fett") > 0.9
    assert similarity("Rinderhackfleisch", "Mineralwasser still") < 0.5


def test_unmatched_lines_count_as_missing_and_extra(invoice):
    pairs = align_lines(list(invoice.lines[:2]), [])
    assert pairs == [(invoice.lines[0], None), (invoice.lines[1], None)]


def test_no_prediction_counts_every_field_as_missing(invoice):
    score = score_document(invoice, None)
    assert score.fields["gross_total"].fn == 1
    assert score.fields["description"].fn == len(invoice.lines)


# --- dataset and harness --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def small_dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("dataset")
    kosit = tmp_path_factory.mktemp("kosit")
    for name in ["01.01a-INVOICE_ubl.xml", "01.13a-INVOICE_ubl.xml"]:
        (kosit / name).write_bytes((KOSIT / name).read_bytes())
    generate_dataset(out, seed=11, photos=2, kosit_dir=kosit)
    return out


def test_generated_dataset_is_complete_and_checksummed(small_dataset):
    import hashlib

    dataset = load_dataset(small_dataset)
    assert len(dataset.items) == 40 + 2 + 2
    assert {item.kind for item in dataset.items} == {"pdf", "photo"}
    for item in dataset.items:
        content = (small_dataset / item.document).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item.sha256
        assert dataset.load_truth(small_dataset, item).lines


def test_generation_is_byte_identical(small_dataset, tmp_path):
    again = generate_dataset(tmp_path, seed=11, photos=2, kosit_dir=None)
    original = {item.id: item.sha256 for item in load_dataset(small_dataset).items}
    assert all(original[item.id] == item.sha256 for item in again.items)


def test_oracle_scores_100_percent(small_dataset):
    summary = evaluate(small_dataset, "oracle").summary()
    assert summary["scores"]["all_fields"]["f1"] == 1.0
    assert summary["documents"]["fully_correct"] == 44


def test_exact_reader_finds_nothing_without_embedded_xml(small_dataset):
    summary = evaluate(small_dataset, "einvoice").summary()
    assert summary["documents"]["with_result"] == 0
    assert summary["scores"]["all_fields"]["recall"] == 0.0


def test_baseline_reads_text_pdfs_but_not_photos(small_dataset):
    report = evaluate(small_dataset, "pdf-text-baseline")
    summary = report.summary()
    assert summary["groups"]["synthetic · photo · receipt"]["with_result"] == 0
    assert summary["fields"]["gross_total"]["precision"] == 1.0  # whatever it finds there is right
    assert summary["scores"]["all_fields"]["f1"] > 0.5


def test_report_files(small_dataset, tmp_path):
    markdown, data = write_report(evaluate(small_dataset, "oracle"), tmp_path)
    assert "| All fields | 100.0% | 100.0% | 100.0% |" in markdown.read_text()
    assert json.loads(data.read_text())["dataset"]["documents"] == 44


def test_private_folder_without_manifest(tmp_path):
    invoice = InvoiceGenerator(2).generate("receipt")
    (tmp_path / "bon.pdf").write_bytes(render_pdf(invoice.data, invoice.layout))
    (tmp_path / "bon.json").write_text(invoice.data.model_dump_json())
    (tmp_path / "ohne_wahrheit.pdf").write_bytes(b"%PDF-1.7")  # no truth file: ignored
    dataset = load_dataset(tmp_path)
    assert [(item.id, item.source) for item in dataset.items] == [("bon", "private")]
    assert extract_with_rules((tmp_path / "bon.pdf").read_bytes()).gross_total == invoice.data.gross_total


def test_truth_template_for_own_receipts(tmp_path):
    target = write_truth_template(tmp_path / "metro_bon.jpg")
    assert target.name == "metro_bon.json"
    assert json.loads(target.read_text())["prices_include_vat"] is True


def test_cli_generate_and_run(tmp_path, capsys):
    assert main(["eval", "generate", "--out", str(tmp_path / "ds"), "--seed", "4", "--photos", "1"]) == 0
    assert (
        main(
            ["eval", "run", "--extractor", "oracle", "--dataset", str(tmp_path / "ds"), "--out", str(tmp_path / "res")]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "F1 100.0%" in output
    assert list((tmp_path / "res").glob("*_oracle_default.md"))
    assert main(["eval", "run", "--extractor", "nope", "--dataset", str(tmp_path / "ds")]) == 2


# --- step 5: language model extractors ------------------------------------------------------------------


def test_units_printed_the_same_count_as_equal():
    assert same_unit("XPP", "H87") and same_unit("C62", "H87")  # all printed as "St"
    assert same_unit("KGM", "kgm")
    assert not same_unit("XCT", "H87") and not same_unit("DZN", "H87")


def test_subsets(small_dataset):
    items = load_dataset(small_dataset).items
    assert {item.kind for item in select_items(items, "photo", None)} == {"photo"}
    assert len(select_items(items, "kosit", 1)) == 1
    assert len(select_items(items, None, None)) == 44


def test_language_model_extractor_on_a_subset(small_dataset, fake_llm, printed_of, tmp_path):
    dataset = load_dataset(small_dataset)
    first = next(item for item in dataset.items if item.template == "receipt")
    truth = dataset.load_truth(small_dataset, first)
    # The fake model always answers with a perfect transcription of the first receipt.
    llm = fake_llm(printed_of(truth, Layout(template="receipt", printed_units={})).model_dump_json())
    seen = []
    report = evaluate(
        small_dataset,
        reader_extractor(DocumentReader(llm, Mode.VISION)),
        only="receipt",
        limit=3,
        progress=lambda number, total, result: seen.append((number, total, result.item.id)),
    )
    summary = report.summary()
    assert [(n, t) for n, t, _ in seen] == [(1, 3), (2, 3), (3, 3)]
    assert summary["subset"] == {"only": "receipt", "limit": 3}
    assert summary["settings"]["model"] == "fake:fake-vl:1b"
    assert summary["items"][0]["fully_correct"] is True
    assert summary["items"][0]["details"]["valid"] is True
    assert summary["language_model"]["replies"] == 3
    assert summary["language_model"]["median_output_tokens"] == 300

    markdown, _data = write_report(report, tmp_path)
    assert markdown.name.endswith("_llm-vision_fake-vl-1b_default_receipt_first3.md")
    assert "Language model:" in markdown.read_text() and "fake:fake-vl:1b" in markdown.read_text()


def test_evaluation_stops_when_the_model_server_is_gone(small_dataset, fake_llm):
    class GoneLlm(fake_llm):
        def complete(self, **request):
            raise LlmUnavailable("Ollama is not reachable")

    with pytest.raises(LlmUnavailable):
        evaluate(small_dataset, reader_extractor(DocumentReader(GoneLlm("{}"), Mode.VISION)), limit=2)


def test_cli_language_model_run_without_a_server(small_dataset, capsys):
    args = ["eval", "run", "--extractor", "llm-vision", "--base-url", "http://127.0.0.1:9"]
    assert main([*args, "--dataset", str(small_dataset)]) == 2
    assert "not reachable" in capsys.readouterr().out
