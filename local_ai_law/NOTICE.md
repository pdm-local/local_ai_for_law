# Third party notices

Local AI Law is free software under the GNU Affero General Public License 3.0 (see
`LICENSE` at the repository root). It is built on open source components, listed here with
the licence each one is used under. Every component below is under a permissive licence
(MIT, BSD, Apache 2.0 or MIT-CMU), or, for a few files inside one library, MPL 2.0 (see
below). All of these may be combined with AGPL 3.0 code.

## Dependencies

WeasyPrint (server-side PDF reports) is optional: `requirements-pdf.txt` installs it. It is
BSD-3-Clause itself, but it pulls in `pyphen`, whose hyphenation dictionaries are GPL or
LGPL, and it links Pango (LGPL). Without it the matter report is served as HTML and printed
to PDF in the browser.

Before adding a dependency, check its licence in the installed wheel, including what it pulls
in, and that it may be combined with AGPL 3.0.

## Python components

| Component | Licence |
|---|---|
| fastapi | MIT |
| starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| gunicorn | MIT |
| python-dotenv | BSD-3-Clause |
| chromadb | Apache-2.0 |
| ollama (client) | MIT |
| pdfplumber | MIT |
| nbformat | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| python-docx | MIT |
| openpyxl | MIT |
| python-pptx | MIT |
| pyjwt | MIT |
| bcrypt | Apache-2.0 |
| psutil | BSD-3-Clause |
| cryptography | Apache-2.0 or BSD-3-Clause |
| argon2-cffi | MIT |
| pypdfium2 | BSD-3-Clause and Apache-2.0 |
| beautifulsoup4 | MIT |
| lxml | BSD-3-Clause |
| markdown | BSD-3-Clause |
| Pillow | MIT-CMU |

Pulled in by the above, with a licence that needs a word:

| Component | Pulled in by | Licence |
|---|---|---|
| orjson | chromadb | `MPL-2.0 AND (Apache-2.0 OR MIT)`: a few vendored source files are MPL 2.0. MPL is file-level: it covers those files only, unmodified here, and their source is public at github.com/ijl/orjson. It gives no right to the rest of the product. |

## Browser components

Served from the appliance itself (`static/vendor`, `static/fonts`, the chat bundle), never
from a CDN.

| Component | Licence |
|---|---|
| DM Sans, DM Mono (typefaces) | SIL Open Font License 1.1 (text in `static/fonts/OFL-*.txt`) |
| React, React DOM (chat panel) | MIT |
| marked | MIT |
| DOMPurify | Apache-2.0 or MPL-2.0 (used under Apache-2.0) |
| highlight.js | BSD-3-Clause |
| KaTeX | MIT |
| D3 | ISC |
| PDF.js | Apache-2.0 |

The OFL lets the fonts ship inside the product; it only forbids selling the fonts on their
own, and asks that the licence travel with them.

## The models

The appliance runs models through Ollama (MIT). The model weights carry their own terms,
which are separate from the licences above and travel with the appliance image:

- **Gemma 3** is under the Google Gemma Terms of Use. Commercial use is allowed, and the
  terms and use restrictions must be passed on to anyone who receives the weights, so the
  pilot agreement and the appliance both have to carry them.
- **Qwen3 Embedding** is under Apache-2.0.

Check the terms of any model before baking it into an image for a client, and say in the
licence and support agreement which models the firm is receiving.
