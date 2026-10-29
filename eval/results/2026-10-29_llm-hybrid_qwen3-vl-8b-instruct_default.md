# Evaluation: llm-hybrid

Dataset **default** (seed 2026), 82 documents · run 2026-10-29T09:32:44+00:00 · wareneinsatz 0.1.0

Settings: method `llm-hybrid` · model `ollama:qwen3-vl:8b-instruct` · ocr `tesseract-5.5.4` · image_side `1600` · prompt_version `2026-10-29.1`

| Fields | Precision | Recall | F1 |
|---|---:|---:|---:|
| All fields | 98.6% | 99.2% | 98.9% |
| Header fields | 98.5% | 98.3% | 98.4% |
| Line fields | 98.7% | 99.3% | 99.0% |

- **Documents with a result:** 82 of 82
- **Completely correct:** 67 of 82
- **Would need review** (validation error in the result): 13 of 82
- **Time per document:** median 70.0 s, 95th percentile 134.7 s
- **Language model:** 82 of 82 replies gave a complete, valid invoice · median 492.0 output tokens at 12.4 tokens/s · median 2960.5 input tokens

## By document group

| Group | Documents | With result | Completely correct | F1 |
|---|---:|---:|---:|---:|
| kosit · pdf · wholesaler | 22 | 22 | 21 | 99.4% |
| synthetic · pdf · cash_carry | 13 | 13 | 9 | 97.6% |
| synthetic · pdf · receipt | 12 | 12 | 10 | 99.3% |
| synthetic · pdf · wholesaler | 15 | 15 | 13 | 99.7% |
| synthetic · photo · receipt | 12 | 12 | 9 | 99.0% |
| synthetic · photo · wholesaler | 8 | 8 | 5 | 98.2% |

## By field

| Field | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| invoice_number | 58 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| issue_date | 82 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| delivery_date | 41 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| supplier_name | 82 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| supplier_vat_id | 57 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| supplier_tax_number | 29 | 0 | 1 | 100.0% | 96.7% | 98.3% |
| net_total | 78 | 4 | 4 | 95.1% | 95.1% | 95.1% |
| vat_total | 77 | 5 | 5 | 93.9% | 93.9% | 93.9% |
| gross_total | 82 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| description | 515 | 17 | 12 | 96.8% | 97.7% | 97.3% |
| seller_article_no | 358 | 8 | 1 | 97.8% | 99.7% | 98.8% |
| quantity | 527 | 5 | 0 | 99.1% | 100.0% | 99.5% |
| unit_code | 527 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| unit_price | 527 | 5 | 0 | 99.1% | 100.0% | 99.5% |
| allowance_amount | 36 | 0 | 6 | 100.0% | 85.7% | 92.3% |
| line_amount | 521 | 11 | 6 | 97.9% | 98.9% | 98.4% |
| vat_rate | 527 | 2 | 0 | 99.6% | 100.0% | 99.8% |

TP = correct value, FP = value given but wrong or not on the document, FN = value on the document but missing or wrong. A wrong value counts as FP and FN. Lines are matched by content before comparing.
