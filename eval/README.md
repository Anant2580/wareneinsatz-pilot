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
reader), `pdf-text-baseline` (rules on the PDF text layer), and the step 5 readers `llm-text`,
`llm-vision` and `llm-hybrid` (OCR + a language model, see below). Each run writes
`results/<date>_<extractor>_<dataset>.md` and a `.json` with every number and every document's result.

### Language model runs (step 5)

They need [Ollama](https://ollama.com) with a vision model, and Tesseract for the OCR text
(macOS: `brew install tesseract tesseract-lang`). Everything is free and runs on the own computer.

```bash
ollama pull qwen3-vl:8b-instruct
uv run wareneinsatz eval run --extractor llm-hybrid --limit 3              # quick check: 3 documents
uv run wareneinsatz eval run --extractor llm-hybrid                        # all 82
uv run wareneinsatz eval run --extractor llm-vision --only photo           # one mode, only the photos
uv run wareneinsatz eval run --extractor llm-hybrid --model qwen3-vl:4b-instruct
```

`--only` picks a subset (`photo`, `pdf`, `synthetic`, `kosit`, `wholesaler`, `cash_carry`,
`receipt`), `--limit` the first n documents. The report names the model, mode, OCR engine and
prompt version, and adds what the model did: how many replies were complete, valid invoices,
and the median number of tokens read and written, and tokens per second.

A test guarantees that everything after the model is lossless: a perfect transcription of any of
202 test invoices (all three layouts and all KoSIT files) scores 100 %. So every error in a
language model run is a reading error of the model, not of the code around it.

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
matched by content before comparing, so one missed line doesn't shift all the others. Unit codes
that print the same ("St" for H87, C62, EA and XPP) count as equal.

Also reported: documents with any result, documents read completely correctly, documents the
step 3 validation would send to review, and the time per document.

## Results

| Extractor | All fields F1 | Precision | Recall | With result | Completely correct | Report |
|---|---:|---:|---:|---:|---:|---|
| pdf-text-baseline (rules, no OCR, no AI) | 78.1 % | 97.3 % | 65.2 % | 62 of 82 | 0 of 82 | [2026-10-28](results/2026-10-28_pdf-text-baseline_default.md) |

| llm-hybrid, Qwen3-VL 8B (image + OCR/PDF text) | **98.9 %** | 98.6 % | 99.2 % | 82 of 82 | **67 of 82** | [2026-10-29](results/2026-10-29_llm-hybrid_qwen3-vl-8b-instruct_default.md) |

The baseline was first scored at 76.5 %; after the unit comparison above was corrected (decision 25
in `docs/decisions.md`) it is 78.1 %.

Language model runs: MacBook Air, Apple M4, 16 GB, Ollama, models quantised to 4 bits. The 8B
hybrid run took a median of 70 s per document (492 output tokens at 12.4 tokens/s); all 82
replies were complete, valid invoices, and the step 3 validation would send 13 to review.

### Photos: which mode and which model?

The 20 phone photos (12 till receipts, 8 A4 invoices) are the hard case, so the modes and the
smaller model were compared there. F1 per photo group, and documents completely correct:

| Run on the 20 photos | Receipt photos F1 | A4 photos F1 | Completely correct | Median time | Report |
|---|---:|---:|---:|---:|---|
| text: OCR text only, 8B | 90.6 % | 74.2 % | 2 of 20 | 57 s | [md](results/2026-10-29_llm-text_qwen3-vl-8b-instruct_default_photo.md) |
| vision: image only, 8B | 97.9 % | 99.0 % | 9 of 20 | 75 s | [md](results/2026-10-29_llm-vision_qwen3-vl-8b-instruct_default_photo.md) |
| hybrid: image + OCR text, 8B | **99.0 %** | 98.2 % | **14 of 20** | 65 s | [md](results/2026-10-29_llm-hybrid_qwen3-vl-8b-instruct_default.md) (photo groups) |
| hybrid, 4B model | 97.6 % | 93.6 % | 7 of 20 | 51 s | [md](results/2026-10-29_llm-hybrid_qwen3-vl-4b-instruct_default_photo.md) |

What this shows:

- **The image is what makes photos work.** OCR text alone loses the table layout of a tilted A4
  photo, so the model can't tell which number belongs to which column (74 % on A4 photos).
- **Image + OCR text is best overall:** the most documents completely correct (14 of 20). The OCR
  text helps with exact digits; where it is wrong, the image wins. Hybrid is the default mode.
- **8B beats 4B** clearly on A4 photos (98 % vs 94 %) for about 30 % more time per document.
- The rule baseline gets 0 % on photos: it needs a text layer.
- Limits: synthetic photos with simulated tilt, blur and noise, and one seed. Real receipts
  (crumpled thermal paper) are harder; they go into `data/private/eval/` next.

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

## Ingredient matching (step 6)

```bash
uv run wareneinsatz eval matching          # needs Ollama with qwen3-embedding:0.6b; --no-embeddings for fuzzy only
```

**The matching set** (`src/wareneinsatz/evaluation/matching_set.json`): 179 hand-written invoice
line texts, about three per ingredient, as different suppliers might print them: abbreviations,
capitals, English, umlauts spelled out, pack sizes in many formats ("HAEHNCHENBRUST TK
4x2,5kg", "Onion Rings Beer Batter 1kg", "Servietten 2lg. 40x40cm 6x250St"). 14 products are
not on the master list (e.g. "Hähnchenkeulen", "Salatgurke"): there the right answer is no
suggestion. Each item also has the correct amount per invoice unit in the ingredient's base unit.
Every second item is "dev": thresholds and the embedding model were chosen there. The table shows
the **test** half (89 items, 82 known, 7 unknown).

| Method | Threshold | Right ingredient first (top-1) | Suggestions right (precision) | Known products with a right suggestion (recall) | Unknown without suggestion | Report |
|---|---:|---:|---:|---:|---:|---|
| fuzzy (rapidfuzz WRatio) | 0.68 | 87.8 % | 87.8 % | 87.8 % | 4 of 7 | [md](results/2026-10-29_matching_qwen3-embedding-0.6b.md) |
| embedding (qwen3-embedding:0.6b) | 0.76 | 93.9 % | 92.6 % | 91.5 % | 2 of 7 | same |
| hybrid (average of both) | 0.74 | **93.9 %** | **94.9 %** | 91.5 % | **6 of 7** | same |

- **Hybrid is the default:** letters catch abbreviations and typos, meaning catches synonyms and
  English ("Espresso ganze Bohne" -> Kaffeebohnen), and together they reject unknown products best.
- **Where it fails:** close products that differ in one word ("Cola zuckerfrei" -> Zucker instead
  of Cola Zero, "PILS KEG" -> bottled instead of draught beer). These are suggestions, so a
  person still confirms; the memory then keeps the right answer.
- On a MacBook Air M4 the same run gave identical hybrid and fuzzy numbers; embeddings alone
  scored 91.5 % instead of 93.9 %, because the same model computes slightly different vectors
  on a GPU. Hybrid, the default, was not affected.
- On dev, three free multilingual embedding models were compared in hybrid mode. All three put
  the right ingredient first for 98.8 %; counting the unknown products too, qwen3-embedding:0.6b
  and bge-m3 decided 88 of 90 items right and embeddinggemma 87. qwen3-embedding is about twice
  as fast as bge-m3, so it is the default.
- **Pack sizes:** 162 of 162 amounts per invoice unit right (the rules were written alongside the
  set, so this mainly shows that the formats in it are covered).
- **Memory over time:** the 40 synthetic invoices in date order, each new supplier product
  confirmed once: 314 lines, 122 confirmations, 192 matched automatically from memory. The
  suggestion was right for 117 of the 122. (The generator draws random article numbers, so the
  simulation remembers by text.)
- Limits: a small, hand-written set for one restaurant type. Real supplier texts will show new
  formats; the confirmed lines in the database (`match_method`, `match_score`) make it possible to
  measure the accuracy on real invoices later.
