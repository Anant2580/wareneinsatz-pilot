# Evaluation: ingredient matching

Run 2026-10-29T17:22:36+00:00 · wareneinsatz 0.1.0 · 54 ingredients · embedding model `ollama:qwen3-embedding:0.6b`

Matching set: 89 test items (82 known products, 7 not on the master list). Thresholds were chosen on the dev half; the numbers below are the test half.

| Method | Threshold | Top-1 accuracy | Suggestions | Precision | Recall | Unknown without suggestion |
|---|---:|---:|---:|---:|---:|---:|
| fuzzy | 0.68 | 87.8% | 82 | 87.8% | 87.8% | 57.1% |
| embedding | 0.76 | 93.9% | 81 | 92.6% | 91.5% | 28.6% |
| hybrid | 0.74 | 93.9% | 79 | 94.9% | 91.5% | 85.7% |

**Pack sizes:** 162 of 162 amounts per invoice unit right.

**Memory over time:** 314 lines on 40 invoices needed 122 confirmations; 192 were matched automatically from memory. The suggestion was right for 117 of the 122 confirmations.

Wrong best candidate (hybrid, test half):

- Cola zuckerfrei 24x0,33L: zucker instead of cola-zero (0.90)
- PILS KEG 30L: pils-flasche instead of pils-fass (0.85)
- Pilsener 20 x 0,5 l Mehrweg: eier instead of pils-flasche (0.74)
- Orange Juice 12x0,2l: sahne instead of orangensaft (0.64)
- Dishwasher Tabs 100 pcs: mineralwasser instead of spuelmaschinentabs (0.69)
