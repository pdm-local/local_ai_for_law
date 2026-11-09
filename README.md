# Local AI Law

A legal document workspace that runs on your own computer. Upload the files of a case, ask
questions about them and get answers that cite the document and page they come from. The
language model runs locally through [Ollama](https://ollama.com), so no document, question
or answer leaves the machine.

Made by Daniel Mihai Pocatilu at [pdm.ai](https://www.pdmzerotrustbusiness.com), 21 October
2026.

## Who it is for

Lawyers, legal clinics, students and anyone who reads many legal documents and may not send
them to an online AI service. It is also a starting point for researchers who want to test
retrieval over legal text without touching real client files.

## What it does

- Keeps each case (a "matter") in its own space, with walls between users.
- Reads PDF, Word, Excel, PowerPoint, Markdown and plain text files.
- Answers questions with a citation to the document and page for every claim, and says so
  when the files do not hold the answer.
- Writes a summary of each document, a wiki for the case and a graph of the people,
  companies and statutes in it.
- Finder: points to the documents and sections to read, without writing an answer.
- Works in English and Dutch, on a desktop and on a phone.

## Try it

You need Python 3.10 or newer and Ollama.

```bash
ollama pull gemma4:e4b
ollama pull qwen3-embedding:4b
ollama pull qwen3.5:9b

git clone https://github.com/pdm-local/local_ai_for_law.git
cd local_ai_for_law/local_ai_law
./run.sh
```

The app opens on http://localhost:8000. The first account you create is the administrator.

To load the demo cases from the invented law firm in `legal_corpus/`, run this from the
repository root while nothing else uses the graphics card:

```bash
PYTHONPATH=. local_ai_law/venv/bin/python local_ai_law/scripts/seed_demo.py --variant en-fulllaw
```

It creates two lawyer accounts, `j.havelaar_en` and `m.terhorst_en`, with the password
`meridiaan-demo-2025`, and three cases. Use `--variant nl-fulllaw` for the Dutch version.

## Demo and paper

- Demo: https://www.pdmzerotrustbusiness.com/projects/law/
- Paper: https://www.pdmzerotrustbusiness.com/papers/2026-10-local-ai-law.pdf

## Repository

| Folder | What is in it |
|---|---|
| `local_ai_law/` | The app (Python, FastAPI, ChromaDB) with its tests and scripts. Its own README explains each part. |
| `legal_corpus/` | An invented law firm with 40 cases in English and Dutch, and the benchmark questions. |

Checks, from `local_ai_law/`: `scripts/verify.sh` runs the health audit, the security
checks, the tests and the linter.

## Licence

The code is free software under the GNU Affero General Public License 3.0 (`LICENSE`). If
you run a changed version as a service for others, you share your changes under the same
licence. The legal corpus is CC BY 4.0 (`LICENSE-DATA`).

## Cite

Pocatilu, D. M. (2026). Local AI Law: a private legal document workspace with local
language models. pdm.ai. https://github.com/pdm-local/local_ai_for_law
