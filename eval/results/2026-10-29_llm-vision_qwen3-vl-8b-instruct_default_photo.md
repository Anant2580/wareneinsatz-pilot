# Evaluation: llm-vision

Dataset **default** (seed 2026), 20 documents · run 2026-10-29T10:54:23+00:00 · wareneinsatz 0.1.0

Settings: method `llm-vision` · model `ollama:qwen3-vl:8b-instruct` · image_side `1600` · prompt_version `2026-10-29.1`

Subset: only photo

| Fields | Precision | Recall | F1 |
|---|---:|---:|---:|
| All fields | 98.9% | 98.2% | 98.6% |
| Header fields | 94.5% | 89.0% | 91.7% |
| Line fields | 99.5% | 99.5% | 99.5% |

- **Documents with a result:** 20 of 20
- **Completely correct:** 9 of 20
- **Would need review** (validation error in the result): 8 of 20
- **Time per document:** median 74.9 s, 95th percentile 128.4 s
- **Language model:** 20 of 20 replies gave a complete, valid invoice · median 507.5 output tokens at 11.35 tokens/s · median 2175.0 input tokens

## By document group

| Group | Documents | With result | Completely correct | F1 |
|---|---:|---:|---:|---:|
| synthetic · photo · receipt | 12 | 12 | 4 | 97.9% |
| synthetic · photo · wholesaler | 8 | 8 | 5 | 99.0% |

## By field

| Field | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| invoice_number | 8 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| issue_date | 20 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| delivery_date | 8 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| supplier_name | 20 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| supplier_vat_id | 8 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| supplier_tax_number | 4 | 0 | 8 | 100.0% | 33.3% | 50.0% |
| net_total | 18 | 2 | 2 | 90.0% | 90.0% | 90.0% |
| vat_total | 16 | 4 | 4 | 80.0% | 80.0% | 80.0% |
| gross_total | 19 | 1 | 1 | 95.0% | 95.0% | 95.0% |
| description | 142 | 5 | 5 | 96.6% | 96.6% | 96.6% |
| seller_article_no | 86 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| quantity | 147 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| unit_code | 147 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| unit_price | 147 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| allowance_amount | 11 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| line_amount | 147 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| vat_rate | 147 | 0 | 0 | 100.0% | 100.0% | 100.0% |

TP = correct value, FP = value given but wrong or not on the document, FN = value on the document but missing or wrong. A wrong value counts as FP and FN. Lines are matched by content before comparing.
