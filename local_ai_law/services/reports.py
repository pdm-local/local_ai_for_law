"""
Auto-generated matter reports.
Assembles the matter overview and document summaries into a formatted HTML report.
The default build ships that HTML (the reader prints it to PDF from the browser);
with the optional WeasyPrint extra (requirements-pdf.txt) it is converted to PDF here.

Every value in the report (matter and document names, LLM summaries of a firm's
documents) is someone else's words: it is escaped, never inserted as HTML, and
the PDF renderer may not fetch any URL, so a crafted summary cannot pull a local
file or a remote image into the report.
"""

import json
from html import escape
from datetime import datetime

from local_ai_law.config import user_overviews_dir, user_summaries_dir, log


def _load_overview(username: str, group_id: str) -> dict:
    p = user_overviews_dir(username) / f"{group_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            log.debug("Unreadable overview JSON for %s; report omits the overview", group_id, exc_info=True)
    return {}


def _load_summaries(username: str, group_id: str) -> dict:
    p = user_summaries_dir(username) / f"{group_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            log.debug("Unreadable summaries JSON for %s; report omits summaries", group_id, exc_info=True)
    return {}


_REPORT_LABELS = {
    "nl": {
        "no_overview": "Nog geen overzicht gegenereerd.", "none": "Geen",
        "summary": "Samenvatting", "report": "Dossierrapport",
        "generated_on": "Gegenereerd op", "for": "voor", "overview": "Overzicht",
        "topics": "Onderwerpen", "documents": "Documenten",
        "no_docs": "Nog geen documenten geïndexeerd.",
        "footer": "pdm Juridische Werkplek — automatisch gegenereerd rapport",
    },
    "en": {
        "no_overview": "No overview generated yet.", "none": "None",
        "summary": "Summary", "report": "Matter report",
        "generated_on": "Generated on", "for": "for", "overview": "Overview",
        "topics": "Topics", "documents": "Documents",
        "no_docs": "No documents indexed yet.",
        "footer": "pdm Legal Workspace — automatically generated report",
    },
}


def generate_report_html(username: str, group_id: str, group_name: str, lang: str = "nl") -> str:
    """Build an HTML report string for a matter."""
    L = _REPORT_LABELS.get(lang, _REPORT_LABELS["nl"])
    overview = _load_overview(username, group_id)
    summaries = _load_summaries(username, group_id)
    now = datetime.now().strftime("%d-%m-%Y %H:%M")

    def e(v) -> str:
        return escape(str(v or ""))

    summary_text = e(overview.get("summary") or L["no_overview"])
    topics = overview.get("topics") or []
    docs = overview.get("documents") or []

    topics_html = "".join(f'<span class="tag">{e(t)}</span>' for t in topics) or f"<em>{L['none']}</em>"

    docs_html = ""
    for d in docs:
        name = d.get("name", "")
        desc = e(d.get("description"))
        doc_summary = summaries.get(name, {})
        tldr = e(doc_summary.get("tldr"))
        key_pts = doc_summary.get("key_points") or []
        pts_html = ""
        if key_pts:
            pts_html = "<ul>" + "".join(f"<li>{e(p)}</li>" for p in key_pts) + "</ul>"
        name = e(name)
        docs_html += f"""
        <div class="doc-card">
            <h3>{name}</h3>
            <p class="desc">{desc}</p>
            {"<p><strong>" + L['summary'] + ":</strong> " + tldr + "</p>" if tldr else ""}
            {pts_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{e(group_name)} — {L['report']}</title>
<style>
  @page {{ margin: 2cm; size: A4; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; color: #1e293b; line-height: 1.6; }}
  h1 {{ color: #C9A227; border-bottom: 2px solid #C9A227; padding-bottom: 8px; }}
  h2 {{ color: #334155; margin-top: 28px; }}
  .meta {{ color: #64748b; font-size: 0.9em; }}
  .tag {{ display: inline-block; background: #f3eccb; color: #8a6d1b; padding: 2px 10px;
          border-radius: 12px; margin: 2px 4px; font-size: 0.85em; }}
  .doc-card {{ background: #f8fafc; border-left: 3px solid #C9A227; padding: 12px 16px;
               margin: 10px 0; border-radius: 4px; }}
  .doc-card h3 {{ margin: 0 0 4px; font-size: 1em; }}
  .desc {{ color: #64748b; margin: 2px 0; font-size: 0.9em; }}
</style></head><body>
<h1>{e(group_name)} — {L['report']}</h1>
<p class="meta">{L['generated_on']} {now} {L['for']} <strong>{e(username)}</strong></p>

<h2>{L['overview']}</h2>
<p>{summary_text}</p>
<p><strong>{L['topics']}:</strong> {topics_html}</p>

<h2>{L['documents']} ({len(docs)})</h2>
{docs_html or f"<p><em>{L['no_docs']}</em></p>"}

<div style="margin-top: 40px; padding-top: 12px; border-top: 1px solid #e2e8f0; color: #94a3b8; font-size: 0.8em;">
    {L['footer']}
</div>
</body></html>"""


def _no_fetch(url: str, *args, **kwargs):
    """WeasyPrint url_fetcher that refuses every URL: file:, http:, data: alike."""
    raise ValueError(f"report renderer may not fetch {url[:60]!r}")


def generate_report_pdf(username: str, group_id: str, group_name: str, lang: str = "nl") -> bytes:
    """Generate a PDF report and return its bytes.

    Raises ImportError when the optional WeasyPrint extra is not installed (the sold
    build leaves it out: its hyphenation dictionaries are GPL/LGPL)."""
    html = generate_report_html(username, group_id, group_name, lang)
    try:
        from weasyprint import HTML
        pdf_bytes = HTML(string=html, url_fetcher=_no_fetch).write_pdf()
        log.info("PDF report generated for %s/%s (%.1f KB)",
                 username, group_id, len(pdf_bytes) / 1024)
        return pdf_bytes
    except ImportError:
        log.info("WeasyPrint not installed; the report is served as HTML")
        raise
    except Exception as e:
        log.error("PDF generation failed: %s", e)
        raise
