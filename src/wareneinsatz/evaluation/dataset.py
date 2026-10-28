"""The evaluation set: documents plus their correct answer (ground truth) in canonical form.

Layout of a dataset folder:

    dataset.json            list of items: id, document, truth, kind, source, template, sha256
    documents/<id>.pdf|jpg  what the extractor sees
    truth/<id>.json         the correct InvoiceData

The default set is generated (seeded, so everyone gets byte-identical files):

    synthetic  invoices from the catalogue in three layouts (wholesaler, cash & carry, till receipt)
    photo      phone-photo versions of some of them (no text layer: needs OCR)
    kosit      official KoSIT test invoices (tests/samples/kosit), rendered as PDFs; their truth is
               the XML, read exactly by the step 2 parser

A private set (own receipts, kept outside git) can skip dataset.json: any `name.jpg` / `name.pdf`
with a `name.json` truth next to it counts as one item.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from pydantic import BaseModel

from wareneinsatz.evaluation.photo import photo_of
from wareneinsatz.evaluation.render import render_pdf
from wareneinsatz.evaluation.synthetic import InvoiceGenerator, Layout
from wareneinsatz.ingest.extract import parse_einvoice_xml
from wareneinsatz.schemas import InvoiceData

# Default composition: 40 synthetic PDFs, 20 photos, every KoSIT pair in tests/samples/kosit (UBL version).
TEMPLATE_MIX = {"wholesaler": 15, "cash_carry": 13, "receipt": 12}


class DatasetItem(BaseModel):
    id: str
    document: str  # path relative to the dataset folder
    truth: str  # path relative to the dataset folder
    kind: str  # "pdf" (has a text layer) or "photo" (image only)
    source: str  # "synthetic", "kosit" or "private"
    template: str  # layout used, e.g. "wholesaler"; "unknown" for private items
    sha256: str  # of the document, so a report always says which exact files it used


class Dataset(BaseModel):
    name: str
    seed: int | None
    items: list[DatasetItem]

    def load_truth(self, root: Path, item: DatasetItem) -> InvoiceData:
        return InvoiceData.model_validate_json((root / item.truth).read_text(encoding="utf-8"))


def generate_dataset(out: Path, *, seed: int = 2026, photos: int = 20, kosit_dir: Path | None = None) -> Dataset:
    """Build the default evaluation set in `out` (replacing what is there)."""
    for sub in ("documents", "truth"):
        shutil.rmtree(out / sub, ignore_errors=True)
        (out / sub).mkdir(parents=True)

    generator = InvoiceGenerator(seed)
    items: list[DatasetItem] = []
    synthetic: list[tuple[str, InvoiceData, Layout, bytes]] = []
    number = 0
    for template, count in TEMPLATE_MIX.items():
        for _ in range(count):
            number += 1
            invoice = generator.generate(template)
            pdf = render_pdf(invoice.data, invoice.layout)
            item_id = f"syn-{number:03d}-{template}"
            items.append(_write(out, item_id, pdf, ".pdf", invoice.data, "pdf", "synthetic", template))
            synthetic.append((item_id, invoice.data, invoice.layout, pdf))

    # Photos: every receipt first (that is what gets photographed), then A4 invoices
    by_template = sorted(synthetic, key=lambda entry: entry[2].template != "receipt")
    for index, (item_id, data, layout, pdf) in enumerate(by_template[:photos]):
        jpg = photo_of(pdf, seed=seed * 1000 + index)
        items.append(_write(out, f"{item_id}-photo", jpg, ".jpg", data, "photo", "synthetic", layout.template))

    if kosit_dir and kosit_dir.exists():
        for xml_path in sorted(kosit_dir.glob("*_ubl.xml")):
            data, _syntax, _profile = parse_einvoice_xml(xml_path.read_bytes())
            pdf = render_pdf(data, Layout(template="wholesaler", printed_units={}))
            item_id = f"kosit-{xml_path.name.removesuffix('-INVOICE_ubl.xml')}"
            items.append(_write(out, item_id, pdf, ".pdf", data, "pdf", "kosit", "wholesaler"))

    dataset = Dataset(name="default", seed=seed, items=items)
    (out / "dataset.json").write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
    return dataset


def load_dataset(root: Path) -> Dataset:
    """Read dataset.json, or pair up `name.<image|pdf>` with `name.json` in a folder without one."""
    manifest = root / "dataset.json"
    if manifest.exists():
        return Dataset.model_validate_json(manifest.read_text(encoding="utf-8"))
    items = []
    for document in sorted(
        p for p in root.rglob("*") if p.suffix.lower() in {".pdf", ".jpg", ".jpeg", ".png", ".heic"}
    ):
        truth = document.with_suffix(".json")
        if truth.exists():
            items.append(
                DatasetItem(
                    id=str(document.relative_to(root).with_suffix("")),
                    document=str(document.relative_to(root)),
                    truth=str(truth.relative_to(root)),
                    kind="pdf" if document.suffix.lower() == ".pdf" else "photo",
                    source="private",
                    template="unknown",
                    sha256=hashlib.sha256(document.read_bytes()).hexdigest(),
                )
            )
    return Dataset(name=root.name, seed=None, items=items)


def _write(
    out: Path, item_id: str, content: bytes, suffix: str, truth: InvoiceData, kind: str, source: str, template: str
) -> DatasetItem:
    document = f"documents/{item_id}{suffix}"
    truth_file = f"truth/{item_id}.json"
    (out / document).write_bytes(content)
    (out / truth_file).write_text(truth.model_dump_json(indent=2), encoding="utf-8")
    return DatasetItem(
        id=item_id,
        document=document,
        truth=truth_file,
        kind=kind,
        source=source,
        template=template,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def write_truth_template(path: Path) -> Path:
    """Create `name.json` next to a private document: a truth file to fill in by hand."""
    template = {
        "document_type": "invoice",
        "invoice_number": None,
        "issue_date": "2026-01-31",
        "currency": "EUR",
        "prices_include_vat": True,
        "supplier": {"name": "Name as printed", "vat_id": None, "tax_number": None},
        "lines": [
            {
                "line_no": 1,
                "description": "Text as printed",
                "quantity": "1",
                "unit_code": "H87",
                "unit_raw": "St",
                "unit_price": "0.00",
                "line_amount": "0.00",
                "vat_rate": "7",
            }
        ],
        "vat_breakdown": [],
        "gross_total": "0.00",
    }
    target = path.with_suffix(".json")
    target.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")
    return target
