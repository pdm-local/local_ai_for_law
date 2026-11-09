"""Law-firm-only edition and module registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


ALL_MODULES: dict[str, str] = {
    "ingestion": "Document ingestion and extraction",
    "wiki": "Wiki page generation and browser",
    "graph": "Knowledge graph and relation extraction",
    "hybrid_search": "Hybrid BM25 + vector search",
    "citations": "Source citations in answers",
    "user_management": "User accounts and roles",
    "audit_logs": "Structured audit trail",
    "backups": "Backup and restore",
    "reports": "PDF reports and export",
    "settings": "Settings panel",
    "annotations": "Document annotations",
    "search": "Cross-document and wiki search",
    "contradiction_tracking": "Contradiction and change detection",
    "save_back": "Save generated answers back to wiki",
    "matter_workspace": "Matter-centric legal workspace",
    "case_authority_linking": "Case and authority linking",
    "chronology_builder": "Chronology and timeline builder",
    "clause_comparison": "Clause and contract comparison",
    "citation_extraction": "Legal citation extraction",
    "privileged_notes": "Privileged and internal note handling",
    "document_comparison": "Side-by-side document comparison",
}

CORE_MODULES: frozenset[str] = frozenset({
    "ingestion", "wiki", "graph", "hybrid_search", "citations",
    "user_management", "audit_logs", "backups", "reports",
    "settings", "annotations", "search", "contradiction_tracking",
})


@dataclass
class EditionDef:
    key: str
    name: str
    description: str
    default_modules: frozenset[str] = field(default_factory=frozenset)
    optional_modules: frozenset[str] = field(default_factory=frozenset)
    wiki_page_types: tuple[str, ...] = field(default_factory=tuple)
    graph_node_types: tuple[str, ...] = field(default_factory=tuple)
    answer_hint: str = ""
    default_system_prompt: str = ""
    answer_hint_nl: str = ""
    default_system_prompt_nl: str = ""
    default_settings: dict = field(default_factory=dict)
    branding: dict = field(default_factory=dict)
    disclaimer: str = ""
    sensitivity_enforcement: bool = False
    presets: dict[str, "EditionDef"] = field(default_factory=dict)


LAW_FIRM = EditionDef(
    key="law_firm",
    name="Law Firm",
    description="Matter-centric legal knowledge assistant for contracts, cases, and research.",
    default_modules=frozenset({
        "matter_workspace", "case_authority_linking", "chronology_builder",
        "clause_comparison", "citation_extraction", "privileged_notes",
        "document_comparison", "save_back",
    }),
    wiki_page_types=(
        "matters", "authorities", "comparisons", "topics", "maps", "MatterOverview",
    ),
    graph_node_types=(
        "Document", "WikiPage", "Matter", "Case", "Clause",
        "Authority", "Party", "Topic",
    ),
    answer_hint=(
        "Legal document assistant rules: use only retrieved context for document questions; "
        "cite document and page for every factual claim; never merge categories, contract "
        "types, parties, clauses, or table rows; explicitly state what is missing from the "
        "context; do not give legal advice or predict outcomes; mark privileged content. "
        "When a question concerns confidentiality or privilege, classify EACH relevant "
        "document explicitly into two groups: (1) PRIVILEGED — attorney-client communications "
        "and internal advice (memos, notes, legal opinions), which you mark [PRIVILEGED] and "
        "which may never be shown to the opposing party; and (2) OPEN/EXTERNAL — documents "
        "addressed to or exchanged with the opposing party (demand letters, correspondence to "
        "the counterparty), which may be shown. Always name both groups; never stop after "
        "listing only the privileged documents. "
        "Do not accept a question's premise as fact: if the question assumes a document, agreement, "
        "transaction, event, ruling, party, role, amount, or date that is not actually present in the "
        "context, say the context contains no such thing and do not answer as if it existed — never "
        "invent details to satisfy the question."
    ),
    default_system_prompt=(
        "You are a legal document assistant for a law firm.\n"
        "Use only the supplied context for document questions. Cite document and page for "
        "every factual claim. Keep contracts, parties, clauses, and table rows strictly "
        "separate. If information is missing, say exactly what is missing. Do not give "
        "legal advice or predict outcomes."
    ),
    answer_hint_nl=(
        "Regels voor de juridische documentassistent: gebruik uitsluitend de opgehaalde "
        "context bij documentvragen; citeer document en pagina bij elke feitelijke bewering; "
        "combineer nooit categorieën, contracttypen, partijen, clausules of tabelrijen; "
        "vermeld expliciet wat in de context ontbreekt; geef geen juridisch advies en "
        "voorspel geen uitkomsten; markeer bevoorrechte inhoud. "
        "Bij een vraag over vertrouwelijkheid of verschoningsrecht: classificeer ELK relevant "
        "stuk uitdrukkelijk in twee groepen: (1) VERTROUWELIJK — advocaat-cliëntcommunicatie en "
        "interne advisering (memo's, notities, juridische adviezen), die je markeert met "
        "[VERTROUWELIJK] en die nooit aan de wederpartij mogen worden getoond; en (2) "
        "OPEN/EXTERN — stukken gericht aan of uitgewisseld met de wederpartij (sommatiebrieven, "
        "correspondentie aan de wederpartij), die wél getoond mogen worden. Noem altijd beide "
        "groepen; stop nooit nadat je alleen de vertrouwelijke stukken hebt opgesomd. "
        "Aanvaard de premisse van een vraag niet als feit: gaat de vraag uit van een document, "
        "overeenkomst, transactie, gebeurtenis, uitspraak, partij, rol, bedrag of datum die niet "
        "daadwerkelijk in de context staat, zeg dan dat de context dat niet bevat en beantwoord de "
        "vraag niet alsof het bestaat — verzin nooit details om de vraag te beantwoorden."
    ),
    default_system_prompt_nl=(
        "Je bent een juridische documentassistent voor een advocatenkantoor.\n"
        "Gebruik uitsluitend de aangeleverde context bij documentvragen. Citeer document en "
        "pagina bij elke feitelijke bewering. Houd contracten, partijen, clausules en "
        "tabelrijen strikt gescheiden. Als informatie ontbreekt, zeg precies wat ontbreekt. "
        "Geef geen juridisch advies en voorspel geen uitkomsten."
    ),
    default_settings={
        "table_format": "linearized",
        "query_expansion": "auto",
        "retrieval_second_pass": True,
        "answer_templates": True,
        "temperature": 0.3,
    },
    # Written in English; a Dutch account sees them through the shell's EN to NL map
    # (MAP_EN_NL in static/index.html), so the two must change together.
    branding={
        "app_name": "pdm Legal Workspace",
        "tagline": "Local document intelligence for law firms",
    },
    disclaimer=(
        "Not legal advice. AI-generated document intelligence: always check it against "
        "the source documents."
    ),
    sensitivity_enforcement=True,
)

EDITIONS: dict[str, EditionDef] = {"law_firm": LAW_FIRM}


def get_edition(key: str) -> EditionDef | None:
    """Return the EditionDef for a key, or None if unknown."""
    return EDITIONS.get(key)


def resolve_edition(edition_key: str, preset_key: Optional[str] = None) -> EditionDef:
    """Resolve to the active edition. This build ships only the Law Firm edition."""
    return LAW_FIRM


def enabled_modules(
    edition_key: str,
    preset_key: Optional[str] = None,
    extra_enabled: Optional[list[str]] = None,
) -> frozenset[str]:
    """The set of enabled module keys: core + Law Firm defaults, plus any valid extras."""
    mods = CORE_MODULES | LAW_FIRM.default_modules
    if extra_enabled:
        allowed = set(ALL_MODULES)
        mods = mods | frozenset(m for m in extra_enabled if m in allowed)
    return mods


def wiki_page_types(edition_key: str, preset_key: Optional[str] = None) -> tuple[str, ...]:
    """The wiki page types available in the active edition."""
    return LAW_FIRM.wiki_page_types


def is_module_enabled(module_key: str, enabled: frozenset[str]) -> bool:
    """True if a module key is in the enabled set."""
    return module_key in enabled


def edition_list() -> list[dict]:
    """List editions (key/name/description/modules) for the setup wizard."""
    return [{
        "key": LAW_FIRM.key,
        "name": LAW_FIRM.name,
        "description": LAW_FIRM.description,
        "presets": [],
    }]
