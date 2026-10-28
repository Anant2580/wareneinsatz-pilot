# Evaluation: pdf-text-baseline

Dataset **default** (seed 2026), 82 documents · run 2026-10-28T18:42:59+00:00 · wareneinsatz 0.1.0

| Fields | Precision | Recall | F1 |
|---|---:|---:|---:|
| All fields | 97.3% | 65.2% | 78.1% |
| Header fields | 91.4% | 65.6% | 76.4% |
| Line fields | 98.4% | 65.1% | 78.4% |

- **Documents with a result:** 62 of 82
- **Completely correct:** 0 of 82
- **Would need review** (validation error in the result): 37 of 62
- **Time per document:** median 5 ms, 95th percentile 9 ms

## By document group

| Group | Documents | With result | Completely correct | F1 |
|---|---:|---:|---:|---:|
| kosit · pdf · wholesaler | 22 | 22 | 0 | 91.3% |
| synthetic · pdf · cash_carry | 13 | 13 | 0 | 90.3% |
| synthetic · pdf · receipt | 12 | 12 | 0 | 89.3% |
| synthetic · pdf · wholesaler | 15 | 15 | 0 | 96.8% |
| synthetic · photo · receipt | 12 | 0 | 0 | 0.0% |
| synthetic · photo · wholesaler | 8 | 0 | 0 | 0.0% |

## By field

| Field | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| invoice_number | 50 | 0 | 8 | 100.0% | 86.2% | 92.6% |
| issue_date | 62 | 0 | 20 | 100.0% | 75.6% | 86.1% |
| delivery_date | 33 | 0 | 8 | 100.0% | 80.5% | 89.2% |
| supplier_name | 25 | 37 | 57 | 40.3% | 30.5% | 34.7% |
| supplier_vat_id | 46 | 0 | 11 | 100.0% | 80.7% | 89.3% |
| supplier_tax_number | 13 | 0 | 17 | 100.0% | 43.3% | 60.5% |
| net_total | 50 | 0 | 32 | 100.0% | 61.0% | 75.8% |
| vat_total | 50 | 0 | 32 | 100.0% | 61.0% | 75.8% |
| gross_total | 62 | 0 | 20 | 100.0% | 75.6% | 86.1% |
| description | 369 | 7 | 158 | 98.1% | 70.0% | 81.7% |
| seller_article_no | 266 | 0 | 93 | 100.0% | 74.1% | 85.1% |
| quantity | 376 | 0 | 151 | 100.0% | 71.3% | 83.3% |
| unit_code | 376 | 0 | 151 | 100.0% | 71.3% | 83.3% |
| unit_price | 376 | 0 | 151 | 100.0% | 71.3% | 83.3% |
| allowance_amount | 0 | 0 | 42 | – | 0.0% | 0.0% |
| line_amount | 345 | 31 | 182 | 91.8% | 65.5% | 76.4% |
| vat_rate | 212 | 0 | 315 | 100.0% | 40.2% | 57.4% |

TP = correct value, FP = value given but wrong or not on the document, FN = value on the document but missing or wrong. A wrong value counts as FP and FN. Lines are matched by content before comparing.
