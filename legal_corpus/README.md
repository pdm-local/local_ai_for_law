# Legal corpus: Meridiaan Advocaten

An invented Dutch law firm, Meridiaan Advocaten, with 40 client matters in 14 practice
areas: ICT, privacy, intellectual property, internet, employment, family, criminal, tax,
tenancy and real estate, corporate, administrative, insolvency, personal injury and
migration. Every client, person, company, address and amount is fictional. Any match with a
real name, address or phone number is chance.

Real client files are confidential, so nobody can share them to test a legal AI tool. This
corpus can be shared, and every answer in it is known in advance.

| Folder | English | Dutch |
|---|---|---|
| `variants/*/documents/` (contracts, letters, memos, notes, opinions) | 218 files | 208 files |
| `variants/*/qa/gold.jsonl` (questions checked against their source) | 260 | 278 |
| `variants/*/qa/testbase.jsonl` (the benchmark input) | 600 | 618 |
| `variants/*/world/kantoor.md` (the firm, its lawyers and the statutes it cites) | 1 | 1 |

Each matter has planted facts (amounts, dates, penalties and one contradiction between two
documents), so a question about it has one correct answer.

The documents were written by a local model (Qwen 3.6, 35B parameters) and checked by a
second pass. The generator is not part of this release.

Used by `local_ai_law/scripts/seed_demo.py` (the demo) and
`local_ai_law/scripts/selfheal_bench.py` (the benchmark).

Licence: CC BY 4.0, see `LICENSE-DATA` at the repository root.
