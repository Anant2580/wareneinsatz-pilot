# Evaluation: llm-text

Dataset **default** (seed 2026), 20 documents · run 2026-10-29T10:23:07+00:00 · wareneinsatz 0.1.0

Settings: method `llm-text` · model `ollama:qwen3-vl:8b-instruct` · ocr `tesseract-5.5.4` · prompt_version `2026-10-29.1`

Subset: only photo

| Fields | Precision | Recall | F1 |
|---|---:|---:|---:|
| All fields | 82.7% | 78.8% | 80.7% |
| Header fields | 89.6% | 88.2% | 88.9% |
| Line fields | 81.7% | 77.5% | 79.6% |

- **Documents with a result:** 20 of 20
- **Completely correct:** 2 of 20
- **Would need review** (validation error in the result): 18 of 20
- **Time per document:** median 56.6 s, 95th percentile 92.5 s
- **Language model:** 20 of 20 replies gave a complete, valid invoice · median 524.0 output tokens at 13.15 tokens/s · median 1101.5 input tokens

## By document group

| Group | Documents | With result | Completely correct | F1 |
|---|---:|---:|---:|---:|
| synthetic · photo · receipt | 12 | 12 | 2 | 90.6% |
| synthetic · photo · wholesaler | 8 | 8 | 0 | 74.2% |

## By field

| Field | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| invoice_number | 6 | 2 | 2 | 75.0% | 75.0% | 75.0% |
| issue_date | 20 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| delivery_date | 7 | 1 | 1 | 87.5% | 87.5% | 87.5% |
| supplier_name | 20 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| supplier_vat_id | 5 | 1 | 3 | 83.3% | 62.5% | 71.4% |
| supplier_tax_number | 12 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| net_total | 17 | 3 | 3 | 85.0% | 85.0% | 85.0% |
| vat_total | 14 | 6 | 6 | 70.0% | 70.0% | 70.0% |
| gross_total | 19 | 1 | 1 | 95.0% | 95.0% | 95.0% |
| description | 99 | 48 | 48 | 67.3% | 67.3% | 67.3% |
| seller_article_no | 55 | 31 | 31 | 64.0% | 64.0% | 64.0% |
| quantity | 130 | 17 | 17 | 88.4% | 88.4% | 88.4% |
| unit_code | 87 | 21 | 60 | 80.6% | 59.2% | 68.2% |
| unit_price | 128 | 19 | 19 | 87.1% | 87.1% | 87.1% |
| allowance_amount | 5 | 0 | 6 | 100.0% | 45.5% | 62.5% |
| line_amount | 114 | 33 | 33 | 77.6% | 77.6% | 77.6% |
| vat_rate | 141 | 1 | 6 | 99.3% | 95.9% | 97.6% |

TP = correct value, FP = value given but wrong or not on the document, FN = value on the document but missing or wrong. A wrong value counts as FP and FN. Lines are matched by content before comparing.
