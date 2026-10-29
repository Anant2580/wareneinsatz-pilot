# Evaluation: llm-hybrid

Dataset **default** (seed 2026), 20 documents · run 2026-10-29T11:13:28+00:00 · wareneinsatz 0.1.0

Settings: method `llm-hybrid` · model `ollama:qwen3-vl:4b-instruct` · ocr `tesseract-5.5.4` · image_side `1600` · prompt_version `2026-10-29.1`

Subset: only photo

| Fields | Precision | Recall | F1 |
|---|---:|---:|---:|
| All fields | 95.2% | 95.2% | 95.2% |
| Header fields | 94.0% | 92.6% | 93.3% |
| Line fields | 95.3% | 95.5% | 95.4% |

- **Documents with a result:** 20 of 20
- **Completely correct:** 7 of 20
- **Would need review** (validation error in the result): 9 of 20
- **Time per document:** median 50.6 s, 95th percentile 91.1 s
- **Language model:** 20 of 20 replies gave a complete, valid invoice · median 508.0 output tokens at 17.55 tokens/s · median 2613.0 input tokens

## By document group

| Group | Documents | With result | Completely correct | F1 |
|---|---:|---:|---:|---:|
| synthetic · photo · receipt | 12 | 12 | 7 | 97.6% |
| synthetic · photo · wholesaler | 8 | 8 | 0 | 93.6% |

## By field

| Field | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| invoice_number | 8 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| issue_date | 20 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| delivery_date | 7 | 1 | 1 | 87.5% | 87.5% | 87.5% |
| supplier_name | 20 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| supplier_vat_id | 8 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| supplier_tax_number | 10 | 0 | 2 | 100.0% | 83.3% | 90.9% |
| net_total | 18 | 2 | 2 | 90.0% | 90.0% | 90.0% |
| vat_total | 16 | 4 | 4 | 80.0% | 80.0% | 80.0% |
| gross_total | 19 | 1 | 1 | 95.0% | 95.0% | 95.0% |
| description | 139 | 12 | 8 | 92.1% | 94.6% | 93.3% |
| seller_article_no | 81 | 5 | 5 | 94.2% | 94.2% | 94.2% |
| quantity | 146 | 5 | 1 | 96.7% | 99.3% | 98.0% |
| unit_code | 140 | 2 | 7 | 98.6% | 95.2% | 96.9% |
| unit_price | 146 | 5 | 1 | 96.7% | 99.3% | 98.0% |
| allowance_amount | 3 | 0 | 8 | 100.0% | 27.3% | 42.9% |
| line_amount | 138 | 13 | 9 | 91.4% | 93.9% | 92.6% |
| vat_rate | 142 | 4 | 5 | 97.3% | 96.6% | 96.9% |

TP = correct value, FP = value given but wrong or not on the document, FN = value on the document but missing or wrong. A wrong value counts as FP and FN. Lines are matched by content before comparing.
