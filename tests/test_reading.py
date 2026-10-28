"""Tests for step 5: reading PDFs and photos with OCR and a language model.

No model server is needed: the model is replaced by FakeLlm (conftest.py) or by httpx's MockTransport.
Tesseract tests are skipped where Tesseract with German is not installed.
"""

import io
import json
import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont

from wareneinsatz.config import Settings
from wareneinsatz.evaluation.metrics import score_document
from wareneinsatz.evaluation.photo import photo_of
from wareneinsatz.evaluation.render import render_pdf
from wareneinsatz.evaluation.synthetic import InvoiceGenerator, Layout
from wareneinsatz.ingest.errors import ExtractionError
from wareneinsatz.ingest.extract import parse_einvoice_xml
from wareneinsatz.reading import Mode, OcrUnavailable, make_reader
from wareneinsatz.reading.interpret import (
    choose_line_numbers,
    clean_invoice_number,
    clean_tax_number,
    clean_vat_id,
    implied_rate,
    interpret,
    number_candidates,
    parse_amount,
    parse_date,
    parse_rate,
    split_address,
)
from wareneinsatz.reading.llm import LlmError, LlmUnavailable, OllamaClient, OpenAICompatibleClient
from wareneinsatz.reading.ocr import TesseractOcr, prepare_for_ocr, skew_angle
from wareneinsatz.reading.pages import compact_text, encode_for_model, load_pages
from wareneinsatz.reading.printed import SYSTEM_PROMPT, PrintedInvoice, PrintedLine, PrintedSupplier, user_message
from wareneinsatz.reading.reader import SCHEMA, DocumentReader

KOSIT = Path(__file__).parent / "samples" / "kosit"


def _tesseract_with_german() -> bool:
    if not shutil.which("tesseract"):
        return False
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()
    return "deu" in langs


needs_tesseract = pytest.mark.skipif(not _tesseract_with_german(), reason="Tesseract with German is not installed")


# --- printed text -> values ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.234,56", ["1234.56"]),
        ("4,887", ["4.887"]),
        ("12.50", ["12.50"]),
        ("1.554", ["1554", "1.554"]),  # German thousands separator first, then a decimal point
        ("-4,35", ["-4.35"]),
        ("4,35-", ["-4.35"]),  # till receipts put the minus behind the number
        ("−4,35", ["-4.35"]),  # a real minus sign
        ("8,89 EUR/kg", ["8.89"]),
        ("1,234,567", ["1234567"]),
        ("13,59.", ["13.59"]),
        ("abc", []),
        (None, []),
    ],
)
def test_number_candidates(text, expected):
    assert number_candidates(text) == [Decimal(e) for e in expected]


def test_amounts_have_two_decimals():
    assert parse_amount("43,450") == Decimal("43.45")
    assert str(parse_amount("7")) == "7.00"
    assert parse_amount("43,456") == Decimal("43.456")  # kept as read: the schema rejects it later


def test_line_maths_decides_between_two_readings():
    # "1.554" kg x 3,19 = 4,96 only works as 1.554, not as 1554
    assert choose_line_numbers("1.554", "3,19", Decimal("4.96")) == (Decimal("1.554"), Decimal("3.19"))
    # and as a thousands separator where that is what adds up
    assert choose_line_numbers("1.200", "0,05", Decimal("60.00")) == (Decimal("1200"), Decimal("0.05"))
    assert choose_line_numbers(None, "2,49", Decimal("2.49")) == (None, Decimal("2.49"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("18.10.2026", date(2026, 10, 18)),
        ("18.10.26", date(2026, 10, 18)),
        ("2026-10-18", date(2026, 10, 18)),
        ("18/10/2026", date(2026, 10, 18)),
        ("18. Oktober 2026", date(2026, 10, 18)),
        ("3. März 2026", date(2026, 3, 3)),
        ("Datum 04.04.2026 18:57", date(2026, 4, 4)),
        ("31.02.2026", None),
        ("gestern", None),
        (None, None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_vat_rates_and_codes():
    codes = {"A": Decimal("7"), "B": Decimal("19")}
    assert parse_rate("7 %", codes) == Decimal("7")
    assert parse_rate("19,00%", codes) == Decimal("19")
    assert parse_rate("A", codes) == Decimal("7")
    assert parse_rate("b", codes) == Decimal("19")
    assert parse_rate("C", codes) is None
    assert parse_rate("150 %", codes) is None
    assert implied_rate(Decimal("155.21"), Decimal("10.87")) == Decimal("7")
    assert implied_rate(Decimal("100.00"), Decimal("3.00")) is None  # 3 % is not a German rate


def test_address_and_identifiers():
    assert split_address("Am Bahnhof 2, 39104 Magdeburg") == ("Am Bahnhof 2", "39104", "Magdeburg")
    assert split_address("Brunnenweg 7 · 06108 Halle (Saale)") == ("Brunnenweg 7", "06108", "Halle (Saale)")
    assert split_address("irgendwo") == ("irgendwo", None, None)
    assert clean_vat_id("USt-IdNr. DE713803048") == "DE713803048"  # the label is not part of it
    assert clean_vat_id("USt-IdNr.: DE 713 803 048") == "DE 713 803 048"
    assert clean_vat_id("ATU12345678") == "ATU12345678"
    assert clean_vat_id("St.-Nr. 106/179/10281") is None
    assert clean_tax_number("St.-Nr. 106/179/10281") == "106/179/10281"
    assert clean_invoice_number("Rechnungs-Nr.: RE-2026-1") == "RE-2026-1"
    assert clean_invoice_number("Rechnungsnummer") == "Rechnungsnummer"  # the KoSIT samples use this as the number


def _printed(**changes) -> PrintedInvoice:
    """A small cash & carry invoice as a model would return it."""
    base = {
        "document_title": "RECHNUNG",
        "supplier": {"name": "MUSTER CASH & CARRY GMBH", "address": "Gewerbering 12, 39116 Magdeburg",
                     "vat_id": "USt-IdNr. DE713803048", "tax_number": None},
        "invoice_number": "69355-4380",
        "receipt_number": None,
        "issue_date": "08.05.2026",
        "delivery_date": "06.05.2026",
        "currency": "EUR",
        "lines": [
            {"article_no": "646112", "description": "Gouda jung am Stück", "quantity": "4,887", "unit": "kg",
             "unit_price": "8,89", "amount": "43,45", "discount": "-4,35", "surcharge": None, "vat": "A"},
            {"article_no": "408986", "description": "Spülmaschinentabs 150 Stk", "quantity": "1", "unit": "Box",
             "unit_price": "18,59", "amount": "18,59", "discount": None, "surcharge": None, "vat": "B"},
        ],
        "vat_rates": [{"code": "A", "rate": "7", "net": "39,10", "tax": "2,74"},
                      {"code": "B", "rate": "19", "net": "18,59", "tax": "3,53"}],
        "discount_total": None,
        "surcharge_total": None,
        "net_total": "57,69",
        "vat_total": "6,27",
        "gross_total": "63,96",
    }  # fmt: skip
    base.update(changes)
    return PrintedInvoice.model_validate(base)


def test_interpret_a_net_invoice_with_codes_and_a_discount():
    result = interpret(_printed())
    assert result.valid, result.problems
    data = result.data
    assert data.supplier.name == "MUSTER CASH & CARRY GMBH"
    assert data.supplier.vat_id == "DE713803048"
    assert (data.supplier.postcode, data.supplier.city) == ("39116", "Magdeburg")
    assert data.prices_include_vat is False  # the lines add up to the net total
    gouda, tabs = data.lines
    assert (gouda.quantity, gouda.unit_code, gouda.unit_raw) == (Decimal("4.887"), "KGM", "kg")
    assert gouda.allowance_amount == Decimal("4.35")
    assert gouda.line_amount == Decimal("39.10")  # 43,45 printed on the row, minus the discount row
    assert (gouda.vat_rate, tabs.vat_rate) == (Decimal("7"), Decimal("19"))
    assert tabs.unit_code == "XBX"
    assert data.issue_date == date(2026, 5, 8)


def test_interpret_a_receipt_with_gross_prices_and_only_a_vat_table():
    printed = _printed(
        document_title=None,
        invoice_number=None,
        receipt_number="Bon 1007",
        delivery_date=None,
        lines=[
            PrintedLine(
                article_no=None,
                description="Avocado Hass",
                quantity="30",
                unit="St",
                unit_price="1,19",
                amount="35,70",
                discount=None,
                surcharge=None,
                vat="A",
            ),
            PrintedLine(
                article_no=None,
                description="Brötchen",
                quantity=None,
                unit=None,
                unit_price=None,
                amount="2,49",
                discount=None,
                surcharge=None,
                vat="A",
            ),
        ],  # fmt: skip
        # the model swapped the columns and left out the rate: both repaired from the amounts
        vat_rates=[{"code": "A", "rate": None, "net": "2,51", "tax": "35,68"}],
        net_total=None,
        vat_total=None,
        gross_total="38,19",
    )
    result = interpret(printed)
    assert result.valid, result.problems
    data = result.data
    assert data.prices_include_vat is True  # no net total printed, lines add up to the gross total
    assert data.invoice_number is None  # the Bon number is not an invoice number
    assert [line.vat_rate for line in data.lines] == [Decimal("7"), Decimal("7")]
    assert data.vat_breakdown[0].taxable_amount == Decimal("35.68")
    assert (data.net_total, data.vat_total) == (Decimal("35.68"), Decimal("2.51"))  # sums of the VAT table
    bread = data.lines[1]
    assert (bread.quantity, bread.unit_price) == (Decimal("1"), Decimal("2.49"))  # "1 x" is not printed


def test_tax_number_in_the_vat_id_field_is_moved():
    supplier = PrintedSupplier(name="Getränkemarkt", address=None, vat_id="St.-Nr. 105/279/36294", tax_number=None)
    data = interpret(_printed(supplier=supplier)).data
    assert (data.supplier.vat_id, data.supplier.tax_number) == (None, "105/279/36294")


def test_deposit_refund_keeps_the_unit_price_positive():
    refund = PrintedLine(article_no=None, description="Leergut", quantity="1", unit=None, unit_price="-3,10",
                         amount="-3,10", discount=None, surcharge=None, vat="B")  # fmt: skip
    line = interpret(_printed(lines=[refund])).data.lines[0]
    assert (line.quantity, line.unit_price, line.line_amount) == (Decimal("-1"), Decimal("3.10"), Decimal("-3.10"))


def test_a_missing_amount_is_calculated_and_flagged():
    line = PrintedLine(article_no=None, description="Zwiebeln", quantity="2", unit="kg", unit_price="1,50",
                       amount=None, discount=None, surcharge=None, vat="A")  # fmt: skip
    result = interpret(_printed(lines=[line]))
    assert result.data.lines[0].line_amount == Decimal("3.00")
    assert result.data.lines[0].confidence == 0.5  # validation sends it to a person


def test_unusable_reply_gives_a_partial_result_with_reasons():
    result = interpret(_printed(lines=[], gross_total=None))
    assert not result.valid
    assert any(problem.startswith("lines") for problem in result.problems)
    assert result.data.invoice_number == "69355-4380"  # what was read can still be scored


def test_a_perfect_transcription_gives_back_the_truth(printed_of):
    """Everything after the model is lossless: for 180 synthetic invoices in all three layouts and all
    22 KoSIT invoices, interpreting exactly what the document prints gives back every scored field."""
    generator = InvoiceGenerator(11)
    cases = [generator.generate(t) for t in ("wholesaler", "cash_carry", "receipt") for _ in range(60)]
    pairs = [(case.data, case.layout) for case in cases]
    for xml in sorted(KOSIT.glob("*_ubl.xml")):
        data, _syntax, _profile = parse_einvoice_xml(xml.read_bytes())
        pairs.append((data, Layout(template="wholesaler", printed_units={})))
    assert len(pairs) == 202
    for truth, layout in pairs:
        result = interpret(printed_of(truth, layout))
        assert result.valid, result.problems
        assert score_document(truth, result.data).all_correct, truth.invoice_number
        assert result.data.prices_include_vat == truth.prices_include_vat


# --- pages ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def receipt():
    return InvoiceGenerator(3).generate("receipt")


def test_pdf_pages_have_an_image_and_the_text_layer(receipt):
    pages = load_pages(render_pdf(receipt.data, receipt.layout))
    assert len(pages) == 1
    assert pages[0].image.mode == "RGB" and pages[0].image.width > 400
    assert "SUMME EUR" in pages[0].text
    assert not pages[0].is_photo
    assert "     " not in pages[0].text  # long runs of spaces are compacted


def test_photo_pages_are_upright(receipt):
    photo = load_pages(photo_of(render_pdf(receipt.data, receipt.layout), seed=1))[0]
    assert photo.is_photo and photo.text is None

    # A phone stores "rotate 90°" in EXIF (orientation 6) instead of turning the pixels.
    wide = Image.new("RGB", (200, 100), "white")
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    wide.save(buffer, "JPEG", exif=exif)
    assert load_pages(buffer.getvalue())[0].image.size == (100, 200)


def test_iphone_heic_photos_can_be_read():
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    buffer = io.BytesIO()
    try:
        Image.new("RGB", (64, 48), "white").save(buffer, format="HEIF")
    except (KeyError, OSError, ValueError):
        pytest.skip("this pillow-heif build cannot write HEIC")
    assert load_pages(buffer.getvalue())[0].image.size == (64, 48)


def test_broken_and_unsupported_files_are_refused():
    with pytest.raises(ExtractionError, match="Cannot open the image"):
        load_pages(b"\xff\xd8\xff" + b"not really a jpeg")
    with pytest.raises(ExtractionError, match="Cannot read"):
        load_pages(b"<invoice/>")


def test_images_for_the_model_are_small_jpegs():
    big = Image.new("RGB", (3000, 4000), "white")
    encoded = encode_for_model(big, max_side=1600)
    assert encoded.startswith(b"\xff\xd8\xff")
    assert Image.open(io.BytesIO(encoded)).size == (1200, 1600)


def test_compact_text():
    assert compact_text("A          B\n\n\n\nC   \n") == "A   B\n\nC"


# --- OCR -----------------------------------------------------------------------------------------------


def _text_image(angle: float) -> Image.Image:
    image = Image.new("L", (900, 600), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=28)
    for row in range(8):
        draw.text((60, 50 + row * 60), "Gouda jung 4,887 kg x 8,89  43,45 A", fill=0, font=font)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255, expand=True)


@pytest.mark.parametrize("angle", [-4.0, 0.0, 3.0])
def test_deskew_finds_the_tilt_of_a_photo(angle):
    assert skew_angle(_text_image(angle)) == pytest.approx(-angle, abs=0.5)


def test_small_images_are_upscaled_for_ocr():
    assert prepare_for_ocr(Image.new("RGB", (600, 900), "white"), is_photo=False).size == (1800, 2700)


def test_missing_tesseract_gives_install_instructions(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(OcrUnavailable, match="brew install tesseract"):
        TesseractOcr()


@needs_tesseract
def test_tesseract_reads_a_receipt_photo(receipt):
    page = load_pages(photo_of(render_pdf(receipt.data, receipt.layout), seed=5))[0]
    result = TesseractOcr().read(page.image, is_photo=True)
    assert "SUMME EUR" in result.text
    assert receipt.data.supplier.name.split()[0] in result.text
    assert 0.5 < result.confidence <= 1


@needs_tesseract
def test_unknown_ocr_language_is_reported():
    with pytest.raises(OcrUnavailable, match="xyz"):
        TesseractOcr("deu+xyz")


# --- model clients -------------------------------------------------------------------------------------


def _ollama(handler) -> OllamaClient:
    return OllamaClient("http://ollama.test", "qwen3-vl:8b-instruct", transport=httpx.MockTransport(handler))


def test_ollama_request_and_reply():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        reply = {"message": {"content": '{"a": 1}'}, "done_reason": "stop", "prompt_eval_count": 1200,
                 "eval_count": 400, "eval_duration": 20_000_000_000}  # fmt: skip
        return httpx.Response(200, json=reply)

    reply = _ollama(handler).complete(system="sys", user="read this", images=[b"jpeg"], schema={"type": "object"})
    assert reply.content == '{"a": 1}'
    assert (reply.prompt_tokens, reply.output_tokens, reply.tokens_per_second) == (1200, 400, 20.0)
    assert seen["format"] == {"type": "object"}  # structured output
    assert seen["options"]["temperature"] == 0 and seen["think"] is False
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["messages"][1]["images"] == ["anBlZw=="]  # base64 of b"jpeg"


def test_ollama_errors():
    with pytest.raises(LlmUnavailable, match="ollama pull"):
        _ollama(lambda r: httpx.Response(404, json={"error": "model not found"})).complete(
            system="", user="", images=[], schema={}
        )
    with pytest.raises(LlmError, match="cut off"):
        _ollama(lambda r: httpx.Response(200, json={"message": {"content": "{"}, "done_reason": "length"})).complete(
            system="", user="", images=[], schema={}
        )

    def refuse(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LlmUnavailable, match="not reachable"):
        _ollama(refuse).complete(system="", user="", images=[], schema={})


def test_ollama_check_finds_missing_models():
    tags = {"models": [{"name": "qwen3-vl:4b-instruct"}]}
    with pytest.raises(LlmUnavailable, match="ollama pull qwen3-vl:8b-instruct"):
        _ollama(lambda r: httpx.Response(200, json=tags)).check()
    tags["models"].append({"name": "qwen3-vl:8b-instruct"})
    _ollama(lambda r: httpx.Response(200, json=tags)).check()


def test_openai_compatible_request_and_errors():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen.update(json.loads(request.content))
        reply = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}}  # fmt: skip
        return httpx.Response(200, json=reply)

    client = OpenAICompatibleClient("http://llm.test/v1", "some-model", api_key="secret",
                                    transport=httpx.MockTransport(handler))  # fmt: skip
    reply = client.complete(system="sys", user="read", images=[b"jpeg"], schema={"type": "object"})
    assert (reply.prompt_tokens, reply.output_tokens) == (10, 5)
    assert seen["auth"] == "Bearer secret"
    assert seen["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert seen["messages"][1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,anBlZw=="

    for status, message in [(401, "API key"), (429, "Rate limit")]:
        failing = OpenAICompatibleClient(
            "http://llm.test/v1", "m", transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s))
        )
        with pytest.raises(LlmUnavailable, match=message):
            failing.complete(system="", user="", images=[], schema={})


# --- the reader ----------------------------------------------------------------------------------------


def test_prompt_and_schema():
    assert "Never convert, round or calculate" in SYSTEM_PROMPT
    assert PrintedInvoice.model_validate_json(SYSTEM_PROMPT.split("(a different document): ")[1].split("\n")[0])
    assert set(SCHEMA["required"]) == set(PrintedInvoice.model_fields)  # every key must be written
    message = user_message([("ocr", "SUMME 12,00")], with_images=True)
    assert "trust the image" in message and "SUMME 12,00" in message
    assert "from the PDF" in user_message([("pdf", "x")], with_images=False)


@pytest.mark.parametrize(
    ("mode", "sends_images", "sends_text"),
    [(Mode.TEXT, False, True), (Mode.VISION, True, False), (Mode.HYBRID, True, True)],
)
def test_reader_modes(receipt, printed_of, fake_llm, mode, sends_images, sends_text):
    llm = fake_llm(printed_of(receipt.data, receipt.layout).model_dump_json())
    reading = DocumentReader(llm, mode).read(render_pdf(receipt.data, receipt.layout))
    request = llm.requests[0]
    assert bool(request["images"]) is sends_images
    assert ("SUMME EUR" in request["user"]) is sends_text
    assert reading.valid and score_document(receipt.data, reading.data).all_correct
    assert reading.method == f"llm-{mode.value}"
    assert reading.details["model"] == "fake:fake-vl:1b" and reading.details["output_tokens"] == 300
    assert reading.printed["gross_total"]  # the untouched reply, for documents.raw_extraction


def test_photo_in_text_mode_needs_ocr(receipt, fake_llm):
    photo = photo_of(render_pdf(receipt.data, receipt.layout), seed=2)
    with pytest.raises(ExtractionError, match="no OCR engine"):
        DocumentReader(fake_llm("{}"), Mode.TEXT, ocr=None).read(photo)


def test_reply_that_breaks_the_schema_is_an_extraction_error(receipt, fake_llm):
    with pytest.raises(ExtractionError, match="does not match the schema"):
        DocumentReader(fake_llm('{"lines": "none"}'), Mode.VISION).read(render_pdf(receipt.data, receipt.layout))


def test_make_reader_from_settings(monkeypatch):
    settings = Settings(llm_model="qwen3-vl:4b-instruct", reading_mode="vision", ocr_engine="none")
    reader = make_reader(settings)
    assert (reader.mode, reader.llm.model, reader.ocr) == (Mode.VISION, "qwen3-vl:4b-instruct", None)
    reader = make_reader(settings, mode="text", model="other:1b", ocr="none")
    assert (reader.mode, reader.llm.name) == (Mode.TEXT, "ollama:other:1b")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(OcrUnavailable):
        make_reader(settings, mode="hybrid", ocr="tesseract")
