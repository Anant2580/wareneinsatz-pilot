# Data model

PostgreSQL 16. Tables are added step by step through Alembic migrations; this page shows the
whole MVP model and which step adds each part. Steps 1–3 (migrations `0001`–`0003`) exist
today, the rest is the plan and may change as the project learns.

```mermaid
erDiagram
    suppliers ||--o{ invoices : sends
    documents ||--o| invoices : contains
    invoices ||--|{ invoice_lines : has
    invoices ||--o{ invoice_tax_totals : "VAT per rate"
    suppliers ||--o{ supplier_products : sells
    supplier_products }o--|| ingredients : "is the same as"
    invoice_lines }o--o| supplier_products : "matched to"
    recipes ||--|{ recipe_items : contains
    recipe_items }o--o| ingredients : uses
    recipe_items }o--o| recipes : "uses sub-recipe"

    suppliers {
        bigint id PK
        text name
        text name_key UK "normalised name"
        text vat_id UK "USt-IdNr."
    }
    documents {
        bigint id PK
        varchar sha256 UK "duplicate files"
        text filename
        text status "received, extracted, failed"
        text extraction_method
        int extraction_ms
        jsonb raw_extraction "extractor output before review"
    }
    invoices {
        bigint id PK
        bigint document_id FK, UK
        bigint supplier_id FK
        text invoice_number "unique per supplier"
        date issue_date
        bool prices_include_vat
        numeric net_total
        numeric vat_total
        numeric gross_total
        text status "needs_review, approved, rejected"
        jsonb review_issues
    }
    invoice_tax_totals {
        bigint invoice_id FK
        numeric vat_rate
        numeric taxable_amount
        numeric tax_amount
    }
    invoice_lines {
        bigint id PK
        bigint invoice_id FK
        int line_no
        text description "as printed"
        text seller_article_no
        numeric quantity
        text unit_code "UN/ECE Rec 20/21"
        numeric unit_price
        numeric line_amount
        numeric vat_rate
        bigint ingredient_id FK "step 6"
        bigint supplier_product_id FK "step 6"
        text match_status "suggested, confirmed, no_match"
        text match_method "memory, fuzzy, embedding, hybrid, manual"
        float match_score
        numeric base_quantity "quantity in g, ml or pieces"
    }
    ingredients {
        bigint id PK
        text key UK "e.g. patty-rind-180"
        text name
        text category
        text base_unit "g, ml or piece"
        bool is_food "false for deposits, packaging"
        jsonb aliases
    }
    supplier_products {
        bigint id PK
        bigint supplier_id FK
        text article_no "unique per supplier"
        text description_key "unique per supplier without article no."
        bigint ingredient_id FK
        numeric base_qty_per_unit "e.g. 10000 g per carton"
        text confirmed_by "the person who confirmed it"
    }
    recipes {
        bigint id PK
        text name UK
        text kind "dish or preparation"
        numeric menu_price_gross
        numeric vat_rate "7 food, 19 drinks"
        numeric target_food_cost_pct
        numeric yield_quantity
    }
    recipe_items {
        bigint id PK
        bigint recipe_id FK
        bigint ingredient_id FK
        bigint sub_recipe_id FK
        numeric quantity
        text unit
        numeric waste_pct
    }
```

## Steps 1–3: invoices (migrations `0001`–`0003`, exist)

| Table | Purpose |
|---|---|
| `suppliers` | Who sent the invoice. Found again by VAT ID, or by normalised name when a receipt has none. |
| `documents` | One row per uploaded file. SHA-256 rejects the same file twice; `raw_extraction` keeps the extractor's untouched output, so accuracy can be measured against the reviewed version. |
| `invoices` | Header and totals in EN 16931 terms. `(supplier_id, invoice_number)` is unique, so an invoice that arrives as XML and as PDF is booked once. An invoice can only be `approved` with a date. |
| `invoice_tax_totals` | VAT breakdown per rate (BG-23), used to check that totals add up. |
| `invoice_lines` | Lines exactly as printed: text, article number, quantity, unit, price, VAT rate. |

Migration `0002` (step 2) widened `unit_price` to NUMERIC(20,10) and allowed negative allowance and charge totals, after the official test invoices showed both.
Migration `0003` (step 3) added line discounts and surcharges (`allowance_amount`, `charge_amount`) and `approved_by` ('auto' or the reviewer); `review_issues` holds the validation findings.

## Step 6: matching and prices (migration `0004`, exists)

- `ingredients`: the kitchen's master list (`data/ingredients.json`) with one base unit each
  (g, ml or piece). Deposits (Pfand) and packaging are `is_food = false`, so they never count as
  food cost.
- `supplier_products`: the matching memory. A supplier's product confirmed by a person as an
  ingredient, with the pack size (e.g. "Pommes Frites TK, 4 x 2,5 kg" = 10,000 g per carton).
  The next invoice with the same article number (or, without one, the same text) matches
  automatically. Two partial unique indexes keep one entry per article number or text.
- `invoice_lines` got `ingredient_id`, `supplier_product_id`, `match_status`, `match_method`,
  `match_score` and `base_quantity`. A confirmed suggestion keeps the method and score that
  suggested it, so matching accuracy can be measured per method on real invoices too. A CHECK
  makes sure a confirmed line always has an ingredient.
- View `v_ingredient_prices`: net price per base unit and per kg / l / piece for every confirmed
  line of an approved invoice, by date and supplier. Credit notes and returns are left out. It is a
  view, so it can never go stale.

## Step 7: recipes and alerts (migration `0005`, exists)

- `recipes`: dishes (menu price, VAT rate: 7 % for food in restaurants since 1 Jan 2026, 19 % for
  drinks, food-cost target) and preparations such as a house sauce, with a yield, which other
  recipes use. A CHECK makes sure every dish has a price and a VAT rate.
- `recipe_items`: an ingredient or a preparation (exactly one of the two, enforced by a CHECK),
  with quantity, unit (g, kg, ml, cl, l, piece) and waste percentage.
- `alerts`: price jumps, cheaper supplier available, dishes above their food-cost target.
  `dedupe_key` is unique, so a finding is stored once; `acknowledged_at` / `_by` record who saw it.
- View `v_purchase_lines`: every line of an approved invoice with its month, net amount (gross
  receipts converted, credit notes negative) and, if confirmed, ingredient, category and
  `is_food`. The monthly Wareneinsatz is the sum of its food lines.

## Calculations

| Value | Formula |
|---|---|
| Net line amount | `line_amount`, or `line_amount / (1 + vat_rate / 100)` when the document prints gross prices |
| Quantity in base units | `quantity` x unit factor (KGM = 1,000 g), or x `base_qty_per_unit` for cartons and crates |
| Price per base unit | net line amount / quantity in base units (view `v_ingredient_prices`) |
| Dish cost | sum over items of `quantity / (1 - waste_pct / 100)` x latest price per base unit |
| Food-cost % | dish cost / net menu price, net = `menu_price_gross / (1 + vat_rate / 100)` |
| Gross margin per dish | net menu price - dish cost |
| Monthly Wareneinsatz (MVP) | net purchases of food ingredients per month, credit notes subtracted |

The accounting definition of Wareneinsatz is opening stock + purchases - closing stock. Without
stock counts the MVP uses purchases; stock counts are a possible extension.
