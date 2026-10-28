# Evaluation

How accurately does an extractor read invoices? This folder holds the results; the code is in
`src/wareneinsatz/evaluation/`.

## The evaluation set

```bash
uv run wareneinsatz eval generate          # 82 documents in eval/dataset (not in git, ~4 MB)
```

| Group | Documents | What it tests |
|---|---:|---|
| Synthetic invoices, A4 wholesaler layout | 15 | product table with article numbers, net prices, discounts |
| Synthetic invoices, cash & carry layout | 13 | dense table, VAT codes A/B per line, deposit (Pfand) lines |
| Synthetic till receipts | 12 | 80 mm receipt, gross prices, weighed goods (1,554 kg x 3,19) |
| Phone-photo versions of 20 of them | 20 | tilt, shadow, blur, noise, JPEG: no text layer, needs OCR |
| Official KoSIT test invoices, rendered as PDF | 22 | real-world variety; the truth is the official XML |

- **Synthetic invoices** come from `catalogue.json`: items a burger restaurant buys, with realistic
  units, pack sizes and prices, from clearly fictional suppliers. Every generated invoice passes
  the step 3 validation, so the truth itself is a correct invoice (a test checks 120 of them).
- **The truth only contains what the document shows.** For example, receipts print the unit
  ("4 Ki x 16,59"), so a perfect reader could get every field right.
- **Reproducible:** generation is seeded (default 2026), PDFs are written without timestamps and
  photos use seeded randomness, so the same seed gives byte-identical files on any machine.
  `dataset.json` stores a SHA-256 for every document, and each report names the files it used.
- **Invented VAT IDs fail the official check digit on purpose**, so they can never be a real
  company's VAT ID.

## Running an evaluation

```bash
uv run wareneinsatz eval run --extractor pdf-text-baseline
```

Extractors: `oracle` (returns the truth; checks the harness itself), `einvoice` (the exact step 2
reader), `pdf-text-baseline` (rules on the PDF text layer). The OCR + LLM extractor of step 5 will
be added and measured the same way. Each run writes `results/<date>_<extractor>_<dataset>.md` and a
`.json` with every number and every document's result.

## What is measured

Every field of every document is compared with the truth: a correct value is a true positive
(TP), a value that is wrong or not on the document a false positive (FP), a value on the document
that is missing or wrong a false negative (FN). A wrong value counts as both FP and FN.

- **Precision** = TP / (TP + FP): of the values the extractor gave, how many were right?
- **Recall** = TP / (TP + FN): of the values on the documents, how many were found?
- **F1** = harmonic mean of both, per field and for header fields, line fields and all fields.

Header fields: invoice number, issue and delivery date, supplier name, VAT ID and tax number, net,
VAT and gross total. Line fields: description (counts as correct from 90 % text similarity),
article number, quantity, unit, unit price, line discount, line amount, VAT rate. Lines are
matched by content before comparing, so one missed line doesn't shift all the others.

Also reported: documents with any result, documents read completely correctly, documents the
step 3 validation would send to review, and the time per document.

## Results

| Extractor | All fields F1 | Precision | Recall | With result | Completely correct | Report |
|---|---:|---:|---:|---:|---:|---|
| pdf-text-baseline (rules, no OCR, no AI) | 76.5 % | 95.7 % | 63.7 % | 62 of 82 | 0 of 82 | [2026-10-28](results/2026-10-28_pdf-text-baseline_default.md) |

What the baseline shows:

- On clean text PDFs, rules do well (F1 82–97 % per group): what they find is almost always right.
- Photos: 0 %. Without a text layer there is nothing to match, so OCR is required.
- No document is completely correct: rules miss discount rows, VAT codes (A/B) and supplier names
  in letterheads. These are the gaps the OCR + LLM extractor (step 5) has to close.
- The synthetic layouts are clean, so these numbers are optimistic for rules. Real supplier PDFs
  vary more, which favours an approach that understands the document instead of matching patterns.

## Your own receipts (kept out of git)

Real receipts are the hardest test. Put photos in `data/private/eval/` (ignored by git), then:

```bash
uv run wareneinsatz eval template data/private/eval/metro_bon.jpg     # creates metro_bon.json
uv run wareneinsatz eval run --extractor pdf-text-baseline --dataset data/private/eval
```

Fill in the JSON with exactly what the receipt shows. A folder of `name.jpg` + `name.json` pairs
is a dataset; no `dataset.json` is needed.
