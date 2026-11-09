"""
Text extraction from all supported file types.
Each extractor returns list[dict] with keys: text, source, page, [no_split]
"""

import csv
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from local_ai_law.config import log


def _is_real_table(table: list[list]) -> bool:
    """Filter out pdfplumber false-positive tables (single-column text blocks)."""
    if len(table) < 3:
        return False
    sample = table[:10]
    col_counts = [sum(1 for c in row if c and str(c).strip()) for row in sample]
    avg_filled = sum(col_counts) / max(len(col_counts), 1)
    if avg_filled < 2.0:
        return False
    has_numbers = any(
        any(c and any(ch.isdigit() for ch in str(c)) for c in row)
        for row in sample
    )
    return has_numbers


def _clean_table(table: list[list]) -> list[list]:
    """Normalize cells, drop empty rows, pad ragged rows, collapse empty columns."""
    cleaned = []
    for row in table:
        cells = []
        for c in row:
            val = str(c).strip().replace("\n", " ") if c else ""
            cells.append(val)
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return []

    max_cols = max(len(r) for r in cleaned)
    for r in cleaned:
        while len(r) < max_cols:
            r.append("")
    keep = [i for i in range(max_cols) if any(r[i] for r in cleaned)]
    if not keep:
        return []
    return [[r[i] for i in keep] for r in cleaned]


def _format_table_md(table: list[list]) -> str:
    """Convert a pdfplumber table to a readable markdown table."""
    cleaned = _clean_table(table)
    if not cleaned:
        return ""
    lines = []
    header = cleaned[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


_TABLE_TITLE_RE = None  # compiled lazily below


def _detect_table_title(page_text: str) -> str:
    """Best-effort: a 'Table N: ...' / 'Schedule ...' style line near the end of the
    page text usually labels the table that follows it."""
    global _TABLE_TITLE_RE
    if _TABLE_TITLE_RE is None:
        import re
        _TABLE_TITLE_RE = re.compile(
            r"^(Table|Annex|Schedule|Exhibit|Appendix|Tabel|Bijlage)\b.{0,80}$",
            re.IGNORECASE,
        )
    for line in reversed(page_text.splitlines()[-8:]):
        line = line.strip()
        if line and _TABLE_TITLE_RE.match(line):
            return line
    return ""


def _linearize_table(table: list[list], title: str = "") -> str:
    """Convert a table to row-based text with headers repeated on every row, so
    a comparison table never loses which column a value belongs to — even if the
    table is later split across chunks."""
    cleaned = _clean_table(table)
    if not cleaned:
        return ""
    header = cleaned[0]
    lines = []
    if title:
        lines.append(f"Table: {title}")
    lines.append("Headers: " + " | ".join(h or f"col{i}" for i, h in enumerate(header)))
    for row in cleaned[1:]:
        row_label = row[0] or "(unlabelled)"
        cells = [f"{header[j] or f'col{j}'}: {row[j]}" for j in range(1, len(row)) if row[j]]
        lines.append(f"Row: {row_label}; " + "; ".join(cells) if cells else f"Row: {row_label}")
    return "\n".join(lines)


def extract_pdf(path: Path, table_format: str = "markdown") -> list[dict]:
    """Extract a PDF page-by-page (text + real tables). Pages with almost no text are
    rendered to an image (b64) so the vision model can OCR them downstream."""
    out = []
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages):
                parts = []

                tables = page.extract_tables() or []
                real_tables = [t for t in tables if _is_real_table(t)]

                text = (page.extract_text() or "").strip()
                if text:
                    parts.append(text)

                table_title = _detect_table_title(text) if real_tables else ""
                for t in real_tables:
                    if table_format == "linearized":
                        md = _linearize_table(t, table_title)
                    else:
                        md = _format_table_md(t)
                    if md:
                        parts.append("\n[TABLE]\n" + md + "\n[/TABLE]")

                combined = "\n\n".join(parts).strip()
                # If page has very little extractable text, try to render it as an image for OCR
                if len((combined or "").strip()) < 80:
                    img_b64 = _render_pdf_page_as_b64(path, i + 1)
                    if img_b64:
                        out.append({
                            "text": f"[IMAGE PAGE {i+1} — requires vision analysis]",
                            "source": path.name,
                            "page": i + 1,
                            "image_b64": img_b64,
                        })
                    continue
                if combined:
                    out.append({"text": combined, "source": path.name, "page": i + 1})
    except Exception as e:
        log.warning("pdf %s: %s", path.name, e, exc_info=True)
    return out


def extract_notebook(path: Path) -> list[dict]:
    """Extract a Jupyter .ipynb cell-by-cell; code cells are fenced and marked no_split."""
    out = []
    try:
        import nbformat
        nb = nbformat.read(str(path), as_version=4)
        for i, cell in enumerate(nb.cells):
            src = cell.get("source", "").strip()
            if not src:
                continue
            if cell.cell_type == "code":
                lang = "python"
                try:
                    lang = nb.metadata.kernelspec.language or "python"
                except Exception:
                    log.debug("Notebook kernelspec language missing; defaulting to python", exc_info=True)
                text = f"```{lang}\n{src}\n```"
            else:
                text = src
            out.append({"text": text, "source": path.name, "page": i + 1, "no_split": True})
    except Exception as e:
        log.warning("ipynb %s: %s", path.name, e, exc_info=True)
    return out


def extract_docx(path: Path) -> list[dict]:
    """Extract a .docx, grouping paragraphs into ~600-word page-like blocks."""
    out = []
    try:
        from docx import Document
        doc = Document(str(path))
        page, words, page_num = [], 0, 1
        for para in doc.paragraphs:
            t = para.text.strip()
            if not t:
                continue
            w = len(t.split())
            if words + w > 600 and page:
                out.append({"text": "\n\n".join(page), "source": path.name, "page": page_num})
                page_num += 1
                page, words = [], 0
            page.append(t)
            words += w
        if page:
            out.append({"text": "\n\n".join(page), "source": path.name, "page": page_num})
    except Exception as e:
        log.warning("docx %s: %s", path.name, e, exc_info=True)
    return out


def extract_text(path: Path) -> list[dict]:
    """Extract .txt and .md  -  now returns multiple page-like chunks instead of one giant blob."""
    try:
        t = path.read_text(errors="ignore").strip()
        if not t:
            return []
        # Split into ~600 word page-like blocks so chunker gets reasonable input
        words = t.split()
        out = []
        page_num = 1
        PAGE_WORDS = 600
        for i in range(0, len(words), PAGE_WORDS):
            block = " ".join(words[i:i + PAGE_WORDS])
            if block.strip():
                out.append({"text": block, "source": path.name, "page": page_num})
                page_num += 1
        return out if out else [{"text": t, "source": path.name, "page": 1}]
    except Exception as e:
        log.warning("text %s: %s", path.name, e, exc_info=True)
    return []


def extract_html(path: Path) -> list[dict]:
    """Extract visible text from HTML (drops script/style/nav/header/footer), in ~600-word blocks."""
    try:
        from html.parser import HTMLParser

        class _Strip(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
                self._skip = False
            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "nav", "footer", "header"):
                    self._skip = True
            def handle_endtag(self, tag):
                if tag in ("script", "style", "nav", "footer", "header"):
                    self._skip = False
            def handle_data(self, data):
                if not self._skip:
                    t = data.strip()
                    if t:
                        self.parts.append(t)

        raw = path.read_text(errors="ignore")
        parser = _Strip()
        parser.feed(raw)
        text = "\n".join(parser.parts).strip()
        if not text:
            return []
        words = text.split()
        out = []
        page_num = 1
        for i in range(0, len(words), 600):
            block = " ".join(words[i:i + 600])
            out.append({"text": block, "source": path.name, "page": page_num})
            page_num += 1
        return out
    except Exception as e:
        log.warning("html %s: %s", path.name, e, exc_info=True)
    return []


def extract_csv(path: Path) -> list[dict]:
    """Extract a CSV as 'header: value' lines, 50 rows per page-like block."""
    try:
        text = path.read_text(errors="ignore")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return []
        header = rows[0] if rows else []
        out = []
        page_num = 1
        ROWS_PER_PAGE = 50
        for i in range(1, len(rows), ROWS_PER_PAGE):
            batch = rows[i:i + ROWS_PER_PAGE]
            lines = [", ".join(f"{h}: {v}" for h, v in zip(header, row) if v.strip()) for row in batch]
            block = "\n".join(lines)
            if block.strip():
                out.append({"text": block, "source": path.name, "page": page_num})
                page_num += 1
        return out
    except Exception as e:
        log.warning("csv %s: %s", path.name, e, exc_info=True)
    return []


def extract_xlsx(path: Path) -> list[dict]:
    """Extract an .xlsx sheet-by-sheet as 'header: value' lines, 50 rows per block."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        out = []
        page_num = 1
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            header = [str(c) if c else f"col{i}" for i, c in enumerate(rows[0])]
            ROWS_PER_PAGE = 50
            for i in range(1, len(rows), ROWS_PER_PAGE):
                batch = rows[i:i + ROWS_PER_PAGE]
                lines = []
                for row in batch:
                    vals = [f"{h}: {v}" for h, v in zip(header, row) if v is not None and str(v).strip()]
                    if vals:
                        lines.append(", ".join(vals))
                block = f"[Sheet: {sheet}]\n" + "\n".join(lines)
                if block.strip():
                    out.append({"text": block, "source": path.name, "page": page_num})
                    page_num += 1
        wb.close()
        return out
    except Exception as e:
        log.warning("xlsx %s: %s", path.name, e, exc_info=True)
    return []


def extract_pptx(path: Path) -> list[dict]:
    """Extract a .pptx one page per slide (concatenated shape text)."""
    try:
        from pptx import Presentation
        prs = Presentation(str(path))
        out = []
        for i, slide in enumerate(prs.slides):
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        t = para.text.strip()
                        if t:
                            texts.append(t)
            if texts:
                out.append({"text": "\n".join(texts), "source": path.name, "page": i + 1})
        return out
    except Exception as e:
        log.warning("pptx %s: %s", path.name, e, exc_info=True)
    return []


def _epub_documents(zf) -> list[str]:
    """The reading order of an epub: the spine from its package file, and if that cannot be
    read, every XHTML member sorted by name. An epub is a zip of XHTML and is read as one
    here, so no AGPL library is linked into a product we license to a client."""
    import posixpath
    import xml.etree.ElementTree as ET

    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        opf_name = rootfile.get("full-path")
        opf = ET.fromstring(zf.read(opf_name))
        base = posixpath.dirname(opf_name)
        ids = {
            item.get("id"): item.get("href")
            for item in opf.findall(".//{*}manifest/{*}item")
            if (item.get("media-type") or "").endswith("xhtml+xml")
        }
        order = [
            posixpath.normpath(posixpath.join(base, ids[ref.get("idref")]))
            for ref in opf.findall(".//{*}spine/{*}itemref")
            if ref.get("idref") in ids
        ]
        if order:
            return order
    except Exception as e:
        log.debug("epub spine unreadable, falling back to member order: %s", e)
    return sorted(n for n in zf.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))


def extract_epub(path: Path) -> list[dict]:
    """Extract an .epub: strip each document item's HTML into ~600-word blocks."""
    try:
        from html.parser import HTMLParser
        from zipfile import ZipFile

        class _Strip(HTMLParser):
            SKIP = {"script", "style", "head"}

            def __init__(self):
                super().__init__()
                self.parts = []
                self.skipping = 0
            def handle_starttag(self, tag, attrs):
                if tag in self.SKIP:
                    self.skipping += 1
            def handle_endtag(self, tag):
                if tag in self.SKIP and self.skipping:
                    self.skipping -= 1
            def handle_data(self, data):
                t = data.strip()
                if t and not self.skipping:
                    self.parts.append(t)

        out = []
        page_num = 1
        with ZipFile(path) as zf:
            for member in _epub_documents(zf):
                try:
                    content = zf.read(member).decode("utf-8", errors="ignore")
                except KeyError:
                    continue
                parser = _Strip()
                parser.feed(content)
                text = "\n".join(parser.parts).strip()
                if text and len(text) > 30:
                    words = text.split()
                    for i in range(0, len(words), 600):
                        block = " ".join(words[i:i + 600])
                        out.append({"text": block, "source": path.name, "page": page_num})
                        page_num += 1
        return out
    except Exception as e:
        log.warning("epub %s: %s", path.name, e, exc_info=True)
    return []


def extract_json_file(path: Path) -> list[dict]:
    """Extract a .json file as pretty-printed text in ~600-word blocks."""
    try:
        raw = path.read_text(errors="ignore")
        data = json.loads(raw)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        words = text.split()
        out = []
        page_num = 1
        for i in range(0, len(words), 600):
            block = " ".join(words[i:i + 600])
            out.append({"text": block, "source": path.name, "page": page_num})
            page_num += 1
        return out
    except Exception as e:
        log.warning("json %s: %s", path.name, e, exc_info=True)
    return []


def extract_xml(path: Path) -> list[dict]:
    """Extract all element/tail text from an XML tree, in ~600-word blocks."""
    try:
        tree = ET.parse(str(path))
        root = tree.getroot()

        def _texts(el):
            parts = []
            if el.text and el.text.strip():
                parts.append(el.text.strip())
            for child in el:
                parts.extend(_texts(child))
                if child.tail and child.tail.strip():
                    parts.append(child.tail.strip())
            return parts

        text = "\n".join(_texts(root))
        if not text.strip():
            return []
        words = text.split()
        out = []
        page_num = 1
        for i in range(0, len(words), 600):
            block = " ".join(words[i:i + 600])
            out.append({"text": block, "source": path.name, "page": page_num})
            page_num += 1
        return out
    except Exception as e:
        log.warning("xml %s: %s", path.name, e, exc_info=True)
    return []


def _render_pdf_page_as_b64(pdf_path: Path, page_num: int) -> str:
    """Render a PDF page as a PNG and return base64. Uses pypdfium2, the same PDFium engine
    a browser draws PDFs with, under a BSD licence: the AGPL renderer that used to be here
    would have entitled every firm we ship the appliance to the source of the whole product."""
    try:
        import base64
        import io

        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            if page_num < 1 or page_num > len(doc):
                return ""
            page = doc[page_num - 1]
            image = page.render(scale=2.0).to_pil()  # 2x resolution for readability
            buf = io.BytesIO()
            image.convert("RGB").save(buf, format="PNG")
        finally:
            doc.close()
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.debug("pdf page render failed p%d: %s", page_num, e)
        return ""


def extract_file(path: Path, table_format: str = "markdown") -> list[dict]:
    """Dispatch to the right extractor by file extension; returns [] for unsupported types."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_pdf(path, table_format)
    extractors = {
        ".ipynb": extract_notebook,
        ".docx":  extract_docx,
        ".txt":   extract_text,
        ".md":    extract_text,
        ".html":  extract_html,
        ".htm":   extract_html,
        ".csv":   extract_csv,
        ".xlsx":  extract_xlsx,
        ".pptx":  extract_pptx,
        ".epub":  extract_epub,
        ".json":  extract_json_file,
        ".xml":   extract_xml,
    }
    fn = extractors.get(ext)
    if fn:
        return fn(path)
    return []
