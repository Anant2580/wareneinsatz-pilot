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
