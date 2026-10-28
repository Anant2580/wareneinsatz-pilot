# Design decisions

Short records of the important decisions: what was decided and why, so the reasoning
is still clear later. Newest at the bottom.

## 1. One canonical invoice schema, modelled on EN 16931

*25 Oct 2026*

**Context.** Invoices arrive as XRechnung (two XML syntaxes, UBL and CII), as ZUGFeRD (a PDF
with embedded CII XML), as normal PDFs and as phone photos of paper receipts.

**Decision.** Every extractor returns the same pydantic model, `InvoiceData`
(`src/wareneinsatz/schemas.py`). Its fields follow the business terms of EN 16931, the standard
behind XRechnung and ZUGFeRD; each field names its BT/BG number.

**Consequences.** Validation, storage, matching and costing are written once. The XML mapping
is 1:1 and checkable against the standard. The LLM's structured output targets the same JSON
schema. A new source only needs a new extractor.

## 2. Store what is printed, calculate in code

**Context.** Till receipts print gross prices, wholesaler invoices net prices. LLMs are good at
reading and unreliable at arithmetic.

**Decision.** Amounts are stored as printed, with a `prices_include_vat` flag. Gross-to-net,
unit conversions and totals are calculated in Python, never by the LLM.

**Consequences.** Reading errors and calculation errors can be told apart, and the calculations
are tested like any other code.

## 3. Strict structure, lenient business rules

**Decision.** The schema rejects wrong types, unknown fields and too many decimals. Values that
are well-formed but suspicious (totals that don't add up, a 16 % VAT rate, an unknown unit) are
accepted, stored and flagged for review by the validation step.

**Consequences.** Nothing the supplier printed is lost; a person decides. The share of documents
that need review becomes a measurable number.

## 4. Money as NUMERIC and Decimal, never float

**Decision.** NUMERIC(14,2) for amounts, NUMERIC(18,6) for unit prices, NUMERIC(18,4) for
quantities; `Decimal` in Python. The schema's digit limits equal the column limits.

**Consequences.** Sums are exact, nothing is rounded silently on insert, and JSON keeps decimals
as strings.

## 5. The database enforces the invariants

**Decision.** Duplicate files (SHA-256), duplicate invoices (supplier + invoice number), allowed
status values and "approved invoices have a date" are PostgreSQL constraints. The repository
inserts inside a savepoint and turns a violation into `DuplicateDocumentError` or
`DuplicateInvoiceError`, identified by the constraint's name.

**Consequences.** No gap between "check" and "insert" in which a second upload slips through, and
the rules hold for every client: API, scripts and manual SQL. A naming convention makes the
constraint names predictable.

## 6. Status columns as text with CHECK constraints, not ENUM types

**Decision.** Columns such as `status` are `varchar` with a CHECK constraint.

**Consequences.** Adding a value is a small, reversible migration; PostgreSQL ENUM values are
harder to change or remove.

## 7. One restaurant per installation (MVP)

**Context.** Serving several restaurants means a `restaurant_id` in every table and every query.

**Decision.** The MVP serves one restaurant. If needed later: `restaurant_id` plus PostgreSQL
row-level security.

## 8. Migrations with Alembic, tested in both directions

**Decision.** The schema only changes through Alembic migrations with readable IDs (`0001`,
`0002`, ...). Every test run migrates an empty database to the newest version, checks that
`models.py` and the migrations describe the same schema (`alembic check`) and migrates back down.

**Consequences.** A forgotten migration or a broken downgrade fails in CI, not on the server.

## 9. A separate Compose project and database

**Decision.** Locally the database runs as its own Compose project on port 5433. On the shared
cloud VM the app gets its own stack and database and never touches another project's data.

## 10. uv with a lockfile

**Decision.** uv installs Python 3.12 and the dependencies; `uv.lock` pins exact versions and CI
installs with `--locked`.

**Consequences.** The same environment on a laptop, in CI and on the server.

## 11. No real supplier or customer data in git

**Decision.** Tests use synthetic invoices from fictional suppliers, plus official sample files
whose licence allows it. Real receipts, and restaurant data only with the owner's written
permission and anonymised, stay in `data/private/`, which git ignores.

## 12. Untrusted XML is parsed with defusedxml, without DTDs

*26 Oct 2026*

**Context.** Invoices come from outside. Standard XML parsers can be tricked into reading local
files or calling URLs (XXE) or into expanding a few bytes into gigabytes ("billion laughs").

**Decision.** All XML goes through `defusedxml` with DTDs forbidden; e-invoices never need one.

**Consequences.** Both attacks are refused with a clear error, tested in `tests/test_extract.py`.

## 13. Recognise files by content; route what can't be read exactly to step 5

**Decision.** The file type comes from the first bytes, not the file name. XML and ZUGFeRD PDFs are
read exactly. PDFs without XML, photos and ZUGFeRD MINIMUM / BASIC WL (totals only, no lines) are
stored with status `received` for the OCR + LLM extractor. A ZUGFeRD PDF whose XML can't be used
(e.g. ZUGFeRD 1.0) also falls back to its page.

**Consequences.** Every file gets one of four outcomes: stored, needs OCR, duplicate, failed.
Nothing is dropped silently.

## 14. Content-addressed file store, database first

**Decision.** Originals are kept under the SHA-256 of their content (`data/documents/ab/abcd....pdf`).
The database row is written first and the file afterwards, each file in its own transaction.

**Consequences.** The same file always has the same path, so re-saving is harmless and stored files
can't be replaced by different content. A failed insert leaves no orphan file. One bad file in a
batch never undoes the others.

## 15. Official test invoices as test data; schema changed by what they showed

**Decision.** 22 pairs of official XRechnung test invoices (KoSIT, Apache 2.0) are part of the
tests. Each case exists in UBL and CII, so both must give the same invoice, and all totals must add up.

**Consequences.** Two schema assumptions turned out wrong and were fixed in migration `0002`:
unit prices may have 9 decimals (column widened to NUMERIC(20,10)), and correction invoices have
negative allowance totals (CHECK removed). Three pairs differ in the source files themselves;
they are documented in `tests/samples/kosit/README.md`.

## 16. Errors block, warnings inform; only exact e-invoices are approved automatically

*26 Oct 2026*

**Context.** A review queue only helps if it is short. Flagging every oddity would bury the real
problems; approving everything would let misread prices into the cost calculation.

**Decision.** Each rule has a severity. Errors (amounts that don't add up, missing or impossible
VAT rates, missing or future dates, unknown units, low extraction confidence) send the invoice
to review. Warnings (invalid VAT ID check digit, missing tax ID above 250 EUR, supplier name or
VAT ID differs from the stored supplier, old dates, foreign currency) are stored with the invoice
but don't block. An invoice is approved automatically only if it was read exactly from XML and has
no errors; `approved_by` records 'auto' or the reviewer.

**Consequences.** All 44 official KoSIT test invoices pass without errors (a test guards this), so
valid e-invoices don't create review work. Anything read by OCR + LLM is always seen by a person.
The share of invoices that need review becomes a measurable KPI.

## 17. Rules follow EN 16931, with tolerances only where rounding needs them

**Decision.** The checks mirror the standard's calculation rules (BR-CO-10/13/15/17). Totals must
match exactly; a line amount and a VAT amount per rate may differ by one cent from the recalculated
value, because senders round differently. Messages show the calculation, e.g.
"Line 4: 20 × 2.77 = 55.40, but the line says 55.46".

**Consequences.** Line discounts and surcharges (BG-27/BG-28) had to be stored (migration `0003`),
otherwise valid lines with a discount would fail. A wider run on 144 official samples flagged only
4 invoices, each for a real reason.

## 18. Free tools only

*28 Oct 2026*

**Decision.** Every tool and service is free; a paid service is only acceptable at up to 5 EUR per
month. Step 4 uses ReportLab, Pillow and pypdfium2, all open source.

**Consequences.** The OCR and LLM choice in step 5 is limited to open-source OCR, local models or
free API tiers, and the README will say which one was used and what it costs (nothing, or the
limits of a free tier).

## 19. Build the measurement before the extractor

**Context.** "The AI reads invoices well" is not a result. A number is, if the data and the
method are clear.

**Decision.** Step 4 builds an evaluation set and a scoring harness before the OCR + LLM extractor
exists. The set mixes synthetic invoices in three realistic layouts, phone-photo versions and the
official KoSIT invoices rendered as PDFs. Scores are field-level precision, recall and F1 with
lines matched by content, plus documents fully correct, share needing review and time per document.
A rule-based baseline gives the bar to beat.

**Consequences.** Every extractor variant in step 5 (OCR engine, model, prompt) can be compared on
the same 82 documents. The truth contains only what the document shows, so 100 % is possible.

## 20. Reproducible test data instead of committed files

**Decision.** The evaluation set is generated, not stored in git: seeded generators, PDFs written
without timestamps, seeded photo effects. `dataset.json` records a SHA-256 for every document and
reports refer to it. A test checks that generation is byte-identical.

**Consequences.** The repository stays small (the set is about 4 MB), anyone can rebuild exactly
the same documents, and results can be traced to the exact files. Private test data (own
receipts) lives in `data/private/`, outside git.

## 21. Open models on the own machine (Ollama), not a paid API

*29 Oct 2026*

**Context.** The cost rule (decision 18) excludes paid APIs. Invoices are business data: prices
and suppliers of a real restaurant should not leave the machine without a reason.

**Decision.** The language model runs locally with Ollama, an open-source model server.
Default model: Qwen3-VL in the instruct variant (no hidden reasoning, so faster), 8B or 4B
parameters, quantised to 4 bits (6.2 GB / 3.3 GB). It reads images and text, and Ollama's
structured output forces its reply into a JSON schema. The client also speaks the
OpenAI-compatible API (llama.cpp's llama-server, LM Studio, cloud APIs) as an escape hatch.

**Consequences.** Costs nothing and nothing leaves the computer. It needs a machine with about
16 GB of RAM; an Apple Silicon laptop uses its GPU. A free cloud tier would be faster but may
use the data to improve its models (Google's unpaid Gemini terms, for example, allow human
review) and limits requests per day, so it is only an option for the synthetic test data.

## 22. The model transcribes, code interprets

**Context.** Language models read well but make mistakes with German number formats and
arithmetic, and a wrong price is worse than a missing one.

**Decision.** The model returns every value as a string exactly as printed ("1.234,56", "A",
"Krt") in a separate schema (`reading/printed.py`). `reading/interpret.py` turns that into
`InvoiceData`: German numbers and dates, VAT codes via the VAT table, printed units to UN/ECE
codes, the product row minus its discount row as line amount, gross or net by which printed
total the lines add up to. Where a number can be read two ways ("1.554"), the reading that makes
quantity × price = amount wins. Two repairs are checked against the printed amounts: swapped
net/VAT columns and a missing rate that tax ÷ net shows.

**Consequences.** Every rule is a unit test, not a prompt. A test proves the step is lossless:
for 180 synthetic invoices in three layouts and the 22 KoSIT invoices, a perfect transcription
gives back every scored field. Any error left in the evaluation is a reading error of the model.

## 23. Three reading modes, compared on the same documents

**Decision.** `text` (the PDF's own text layer, or Tesseract OCR for photos), `vision` (only the
page images) and `hybrid` (both: the image for the layout, the text for exact digits). Photos are
cleaned before OCR: median filter, contrast stretch, upscaling, and deskewing by a projection
profile (the rotation at which the rows of edges line up best), all in Pillow.

**Consequences.** The evaluation shows which mode is worth its cost: images make the model slower,
OCR text alone loses the table layout of photos. All settings of a run (mode, model, OCR engine,
prompt version) are stored with every result and printed in every report.

## 24. Extraction is a separate step, and its results are always reviewed

**Decision.** `ingest` stays fast and exact; documents it can't read exactly get status
`received`. `wareneinsatz extract` works through them like a job queue, one transaction per
document. The model's untouched reply and the settings are stored in `raw_extraction`. A reply
that isn't a complete invoice marks the document `failed` with the reason. If the model server
is down, the batch stops and the documents stay `received` for the next run. A photo of an
invoice that already arrived as XML is caught by the duplicate check.

**Consequences.** A slow model never blocks uploads, and in step 8 the same function runs in a
background worker. Nothing read by a model is approved automatically (decision 16), and the
stored raw reply makes it possible to measure the model's accuracy on real invoices later, by
comparing it with what the reviewer approved.

## 25. Units that print the same count as the same in scoring

**Context.** The KoSIT XML files use the codes XPP and C62 for pieces; the rendered PDF shows
"St" for both, which every reader maps to H87. No reader could score these lines.

**Decision.** The metric treats H87, C62, EA and XPP as equal (all are "one piece" in
`units.py`). The rule-based baseline was re-run: all-fields F1 76.5 % → 78.1 %, with "Box" added
to the printed units at the same time.
