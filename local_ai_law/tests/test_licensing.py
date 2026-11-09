"""Dependency regression: the dependencies stay permissive.

NOTICE.md lists every dependency under a permissive licence, which keeps the author free to
offer the code under other terms next to AGPL 3.0. `pymupdf` and `ebooklib` (both AGPL v3)
were removed on 2026-09-22; this test keeps them from coming back, and it also checks that
the permissive replacements read the same documents the AGPL ones did.

Hermetic: temp RAG_DATA_DIR (see conftest); no Ollama/GPU needed.
"""
from __future__ import annotations

import base64
import re
import zipfile
from pathlib import Path

from local_ai_law.services.extraction import _render_pdf_page_as_b64, extract_epub

ROOT = Path(__file__).resolve().parents[1]

#: Packages whose licence would follow the appliance to the client. AGPL is the whole
#: reason this list exists; GPL and LGPL are here so the rule is not relearned per case.
COPYLEFT = ("pymupdf", "fitz", "ebooklib", "unstructured", "camelot-py", "pdf2image")

#: Code that ends up in the image. Tests and scripts stay out: they are not shipped.
SHIPPED = ("services", "routes", "app.py", "config.py", "models.py")


def test_no_copyleft_dependency_is_declared():
    declared = [
        re.split(r"[<>=\[]", line.strip())[0].lower()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    bad = sorted(set(declared) & set(COPYLEFT))
    assert not bad, (
        f"REGRESSION: copyleft dependency back in requirements.txt: {bad}. "
        "An AGPL library gives the firm the source of the whole product. See NOTICE.md."
    )


def test_no_shipped_module_imports_a_copyleft_library():
    offenders = []
    for target in SHIPPED:
        path = ROOT / target
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for f in files:
            body = f.read_text(errors="ignore")
            for name in COPYLEFT:
                if re.search(rf"^\s*(?:import|from)\s+{re.escape(name)}\b", body, re.M):
                    offenders.append(f"{f.relative_to(ROOT)}: {name}")
    assert not offenders, f"REGRESSION: copyleft import in shipped code: {offenders}"


def test_notice_lists_every_direct_dependency():
    notice = (ROOT / "NOTICE.md").read_text().lower()
    missing = [
        name for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
        for name in [re.split(r"[<>=\[]", line.strip())[0]]
        if name.lower() not in notice
    ]
    assert not missing, (
        f"NOTICE.md does not list: {missing}. A firm's intake asks for this list; "
        "it is only useful if it is complete."
    )


def _epub(path: Path) -> Path:
    """An epub whose spine deliberately disagrees with the manifest and the file names, so a
    reader that ignores the spine reads the chapters in the wrong order and fails here."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<manifest>'
            '<item id="b" href="b_second.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="a" href="a_first.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest>'
            '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>',
        )
        z.writestr(
            "OEBPS/a_first.xhtml",
            "<html><head><style>p{color:red}</style></head><body><p>"
            + "artikel zeven van de overeenkomst " * 10
            + "</p></body></html>",
        )
        z.writestr(
            "OEBPS/b_second.xhtml",
            "<html><body><p>" + "tweede hoofdstuk over aansprakelijkheid " * 10 + "</p></body></html>",
        )
    return path


def test_an_epub_is_read_in_spine_order_without_ebooklib(tmp_path):
    blocks = extract_epub(_epub(tmp_path / "book.epub"))

    assert blocks, "epub produced no text at all"
    assert blocks[0]["text"].startswith("artikel"), "chapters came back out of spine order"
    assert any(b["text"].startswith("tweede") for b in blocks), "second chapter never read"
    assert all("color:red" not in b["text"] for b in blocks), "stylesheet text leaked into the document"
    assert all(b["source"] == "book.epub" for b in blocks)


def _pdf(path: Path) -> Path:
    """One page of real PDF, written by hand so building the fixture needs no PDF library."""
    content = b"BT /F1 24 Tf 72 700 Td (Artikel 7 aansprakelijkheid) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
        b"<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1) + b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, start)
    path.write_bytes(bytes(out))
    return path


def test_a_page_renders_to_a_png_without_pymupdf(tmp_path):
    png = base64.b64decode(_render_pdf_page_as_b64(_pdf(tmp_path / "one.pdf"), 1))

    assert png.startswith(b"\x89PNG\r\n"), "the vision model would be handed something that is not a PNG"
    assert len(png) > 1000, "the page rendered blank"


def test_a_page_that_is_not_there_renders_to_nothing(tmp_path):
    pdf = _pdf(tmp_path / "one.pdf")

    assert _render_pdf_page_as_b64(pdf, 9) == ""
    assert _render_pdf_page_as_b64(pdf, 0) == ""
