"""
Central configuration  -  loads from .env, falls back to defaults.
100% local: Ollama only, no cloud APIs.
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

_SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_.\-]{1,100}$')
BASE_DIR = Path(__file__).parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass


def validate_safe_id(value: str, label: str = "ID") -> str:
    """Raise ValueError if value contains path traversal characters."""
    if ".." in value:
        raise ValueError(f"Invalid {label}: path traversal not allowed: {value!r}")
    if not _SAFE_ID_RE.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _env_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.getenv(name, "")
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(name: str, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = os.getenv(name, "")
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _ollama_host(default: str = "http://127.0.0.1:11434") -> str:
    raw = os.getenv("OLLAMA_HOST", default).strip()
    if not raw:
        return default
    if raw.isdigit():
        return f"http://127.0.0.1:{raw}"
    if "://" not in raw:
        return f"http://{raw}"
    return raw

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.getenv("RAG_DATA_DIR", str(BASE_DIR / "data")))
AUDIT_LOG = Path(os.getenv("RAG_AUDIT_LOG", str(BASE_DIR / "audit.log")))

# Legacy paths (kept for migration from pre-isolation layout)
_LEGACY_UPLOADS  = BASE_DIR / "uploads"
_LEGACY_DB       = BASE_DIR / "rag_db"
_LEGACY_HISTORY  = BASE_DIR / "chat_history"
_LEGACY_OVERVIEW = BASE_DIR / "overviews"
_LEGACY_SUMMARY  = BASE_DIR / "doc_summaries"
_LEGACY_GROUPS   = BASE_DIR / "groups.json"

# Global settings file (admin defaults). Env-overridable like DATA_DIR/AUDIT_LOG so the
# container images can persist it on the /app/data volume instead of the read-only app dir.
SETTINGS_F = Path(os.getenv("RAG_SETTINGS_FILE", str(BASE_DIR / "settings.json")))

# Instance config (written by setup wizard)
INSTANCE_JSON = DATA_DIR / "instance.json"
EDITION_JSON  = DATA_DIR / "edition.json"

# ── HTTPS ─────────────────────────────────────────────────────────────────────
HTTPS_ENABLED = os.getenv("RAG_HTTPS_ENABLED", "false").lower() == "true"
CERT_FILE = os.getenv("RAG_CERT_FILE", str(BASE_DIR / "certs" / "cert.pem"))
KEY_FILE  = os.getenv("RAG_KEY_FILE", str(BASE_DIR / "certs" / "key.pem"))

# ── Limits ────────────────────────────────────────────────────────────────────
MAX_UPLOAD_MB      = _env_int("RAG_MAX_UPLOAD_MB", 50, 1)
MAX_UPLOAD_BYTES   = MAX_UPLOAD_MB * 1024 * 1024
MAX_QUERY_CHARS    = _env_int("RAG_MAX_QUERY_CHARS", 4000, 100)
RATE_LIMIT_PER_MIN = _env_int("RAG_RATE_LIMIT", 30, 1)
MAX_OLLAMA_NUM_CTX = _env_int("RAG_MAX_OLLAMA_NUM_CTX", 8192, 512, 32768)
OLLAMA_KEEP_ALIVE  = os.getenv("RAG_OLLAMA_KEEP_ALIVE", "30s")
OLLAMA_HOST        = _ollama_host()
OLLAMA_TIMEOUT     = _env_int("RAG_OLLAMA_TIMEOUT", 180, 5)
OLLAMA_CHAT_CONCURRENCY = _env_int("RAG_OLLAMA_CHAT_CONCURRENCY", 1, 1, 8)
EMBED_BATCH_SIZE   = _env_int("RAG_EMBED_BATCH_SIZE", 32, 1, 128)
os.environ["OLLAMA_HOST"] = OLLAMA_HOST

# ── Job workers ───────────────────────────────────────────────────────────────
CRITICAL_WORKERS = _env_int("RAG_CRITICAL_WORKERS", 1, 1, 16)
LOW_WORKERS      = _env_int("RAG_LOW_WORKERS", 1, 1, 8)

# ── Backup ────────────────────────────────────────────────────────────────────
BACKUP_INTERVAL_HOURS = _env_int("RAG_BACKUP_INTERVAL_HOURS", 24, 1)
BACKUP_KEEP_COUNT     = _env_int("RAG_BACKUP_KEEP_COUNT", 7, 1)

# ── Server ────────────────────────────────────────────────────────────────────
# Loopback by default: client files must not be reachable from the network unless
# explicitly opted in (RAG_HOST=0.0.0.0 or `./run.sh start --lan`).
HOST = os.getenv("RAG_HOST", "127.0.0.1")
PORT = _env_int("RAG_PORT", 8000, 1, 65535)

# ── Branding ──────────────────────────────────────────────────────────────────
# Fallback values. Effective branding = fallback ← edition defaults ←
# edition.json overrides ← env vars (env always wins). Use get_branding().
BRANDING = {
    "app_name":      "pdm Legal Workspace",
    "tagline":       "Local-first document intelligence for law firms",
    "logo_url":      "",
    # Empty: the accent comes from the pdm.ai palette preset the user picks
    # (static/css/palettes.css). Set it only to brand a firm's own install.
    "primary_color": "",
    "favicon_url":   "",
}

_BRANDING_ENV = {
    "app_name":      "RAG_APP_NAME",
    "tagline":       "RAG_TAGLINE",
    "logo_url":      "RAG_LOGO_URL",
    "primary_color": "RAG_PRIMARY_COLOR",
    "favicon_url":   "RAG_FAVICON_URL",
}


def get_branding() -> dict:
    """Effective branding for the active edition (env vars win)."""
    merged = dict(BRANDING)
    try:
        from local_ai_law.services.editions import EDITIONS
        ed = EDITIONS.get(get_active_edition())
        if ed and ed.branding:
            merged.update({k: v for k, v in ed.branding.items() if v})
    except Exception:
        pass
    try:
        merged.update({k: v for k, v in load_edition_config().get("branding", {}).items() if v})
    except Exception:
        pass
    for key, env in _BRANDING_ENV.items():
        val = os.getenv(env, "")
        if val:
            merged[key] = val
    return merged


def get_disclaimer() -> str:
    """User-facing disclaimer line for the active edition."""
    default = ("AI responses may be inaccurate. Always verify important "
               "information from original sources.")
    try:
        d = load_edition_config().get("disclaimer", "")
        if d:
            return d
        from local_ai_law.services.editions import EDITIONS
        ed = EDITIONS.get(get_active_edition())
        if ed and ed.disclaimer:
            return ed.disclaimer
    except Exception:
        pass
    return os.getenv("RAG_DISCLAIMER", default)

# ── File types ────────────────────────────────────────────────────────────────
SUPPORTED_EXTS = {
    ".pdf", ".ipynb", ".txt", ".md", ".docx",
    ".html", ".htm", ".csv", ".xlsx", ".pptx",
    ".epub", ".json", ".xml",
}
SKIP_FILES: set[str] = set()

# ── Languages ─────────────────────────────────────────────────────────────────
SUPPORTED_LANGUAGES = {
    "en": "English",
    "nl": "Dutch",
}

LANGUAGE_PROMPTS = {
    "en": "You MUST answer entirely in English.",
    "nl": "You MUST answer entirely in Dutch (Nederlands).",
}

DEFAULT_LANGUAGE = "en"

# ── Difficulty levels ─────────────────────────────────────────────────────────
DIFFICULTY_PROMPTS = {
    "beginner": (
        "Explain concepts as if the reader is completely new to this topic. "
        "Use simple language, analogies, and avoid jargon. "
        "Define any technical terms you must use."
    ),
    "intermediate": (
        "Give balanced explanations using proper terminology. "
        "Assume basic familiarity with the subject area."
    ),
    "expert": (
        "Be concise and technical. Assume deep domain knowledge. "
        "Skip basic explanations and focus on nuances and edge cases."
    ),
}

# ── Defaults (including new wiki/graph/agent keys) ────────────────────────────
DEFAULT_SETTINGS: dict = {
    "app_language": DEFAULT_LANGUAGE,
    # Models. Blank means the user must choose; environment variables can preselect.
    "chat_model":     os.getenv("RAG_CHAT_MODEL",     ""),
    # A chat model per answer language ("" -> chat_model): see services/lang.py.
    "chat_model_nl":  os.getenv("RAG_CHAT_MODEL_NL",  ""),
    "chat_model_en":  os.getenv("RAG_CHAT_MODEL_EN",  ""),
    "embed_model":    os.getenv("RAG_EMBED_MODEL",    ""),
    "wiki_model":     os.getenv("RAG_WIKI_MODEL",     ""),
    "relation_model": os.getenv("RAG_RELATION_MODEL", ""),
    "vision_model":   os.getenv("RAG_VISION_MODEL",   ""),
    # Context
    "top_k":         _env_int("RAG_TOP_K",         10,    1,   50),
    "wiki_top_k":    _env_int("RAG_WIKI_TOP_K",    3,     1,   20),
    "chunk_size":    _env_int("RAG_CHUNK_SIZE",     400,   50,  2000),
    "chunk_overlap": _env_int("RAG_CHUNK_OVERLAP",  60,    0,   500),
    "num_ctx":       _env_int("RAG_NUM_CTX",        8192,  512, 32768),
    # Document router: LLM picks the relevant doc(s), then fetch a small,
    # focused set of chunks ONLY from those docs (avoids context dilution).
    "retrieval_router":   _env_bool("RAG_RETRIEVAL_ROUTER", True),
    "router_max_docs":    _env_int("RAG_ROUTER_MAX_DOCS",    3,  1,  10),
    "router_max_chunks":  _env_int("RAG_ROUTER_MAX_CHUNKS",  5,  1,  20),
    "router_candidates":  _env_int("RAG_ROUTER_CANDIDATES",  12, 3,  50),
    # Card retrieval: cross-encoder ranks cheaply, so the candidate pool can be
    # wide. Pool size fed to the cross-encoder in retrieve_carded.
    "card_candidates":    _env_int("RAG_CARD_CANDIDATES",     50, 5, 200),
    # Document selection fuses the RRF and cross-encoder rankings (reciprocal-rank
    # fusion). Weight on the CE ranking; the rest on RRF. 0.5 measured best on
    # EN+NL (lifts statutes without regressing parties/privilege).
    "ce_fuse_weight":     _env_float("RAG_CE_FUSE_WEIGHT",   0.5, 0.0, 1.0),
    # Multi-doc connection: after the fused pick, append up to `card_expand` docs
    # that share the top picked doc's rarest keyword (1-hop keyword-graph hop), so
    # "what links X and Y" questions surface BOTH docs. 0 disables. `expand_chunks`
    # caps the extra chunks fetched per appended doc (kept small so single-gold
    # answers are not diluted — primary chunks are fetched unchanged).
    "card_expand":        _env_int("RAG_CARD_EXPAND",         2, 0, 5),
    "expand_chunks":      _env_int("RAG_EXPAND_CHUNKS",       2, 1, 10),
    # Multi-doc synthesis support (cross-document reasoning: compare / connection /
    # contradiction). `router_per_doc_floor` guarantees at least this many chunks from
    # EACH routed doc before the remaining slots are filled by global score — so a doc
    # that was picked can never contribute 0 chunks (the failure that left compare/
    # connection answers seeing only one of several needed docs). `answer_inject_summaries`
    # prepends each routed doc's one-line focus/summary to the answer context (whole-doc
    # gist, not just a chunk sliver). `reasoning_model` ("" = off) routes only multi-doc
    # questions, and single-doc turns with same-kind siblings, to a stronger local model
    # (e.g. qwen3.5:35b) while other single-doc turns stay on the fast chat model. For multi-doc questions breadth is widened to `multidoc_max_docs`
    # routed docs (vs router_max_docs) so all comparands surface.
    "router_per_doc_floor": _env_int("RAG_ROUTER_PER_DOC_FLOOR", 1, 0, 5),
    "answer_inject_summaries": _env_bool("RAG_ANSWER_INJECT_SUMMARIES", True),
    "multidoc_max_docs":  _env_int("RAG_MULTIDOC_MAX_DOCS",   5, 1, 10),
    # Single-doc questions: up to this many same-kind documents of the top pick's matter
    # (two memos on one point) get a chunk each and are named to the model as possibly
    # disagreeing, so it cannot answer from the wrong sibling unawares. 0 = off.
    "sibling_max_docs":   _env_int("RAG_SIBLING_MAX_DOCS",    2, 0, 5),
    # Every question: the documents behind the top chunks over the whole collection join
    # the picked ones (one chunk each), since a card can miss the document whose exact
    # wording the question uses. 0 = off.
    "chunk_vote_docs":    _env_int("RAG_CHUNK_VOTE_DOCS",     2, 0, 5),
    "reasoning_model":    os.getenv("RAG_REASONING_MODEL", ""),  # "" -> chat_model
    # Doc-SELECTION widening for multi-doc questions (the real cross-doc bottleneck: card
    # ranking can't tell same-matter siblings apart). `matter_anchor` pulls the whole matter
    # of the top picks (≤`matter_max_docs` per matter) so the per-doc floor can surface the
    # right sibling; `entity_join` adds cross-matter docs sharing a party/statute named in
    # the query (≤`entity_max_docs`), via the frontmatter-derived entity index. Both scoped
    # to multi-doc only (single-doc retrieval is untouched) and bounded so context can't
    # blow up. Off ⇒ pre-matter-anchor behaviour (keyword-graph expansion only).
    "matter_anchor":      _env_bool("RAG_MATTER_ANCHOR", True),
    "matter_max_docs":    _env_int("RAG_MATTER_MAX_DOCS",    8, 1, 20),
    "entity_join":        _env_bool("RAG_ENTITY_JOIN", True),
    "entity_max_docs":    _env_int("RAG_ENTITY_MAX_DOCS",    6, 1, 20),
    # Reranker: one bounded LLM-as-judge pass reorders the candidate pool before
    # routing so the router sees the strongest candidates first. Fail-open.
    "reranker":           _env_bool("RAG_RERANKER", True),
    "reranker_model":     os.getenv("RAG_RERANKER_MODEL", ""),  # "" -> chat_model
    "temperature":   _env_float("RAG_TEMPERATURE",  0.3,   0.0, 2.0),
    "top_p":         _env_float("RAG_TOP_P",        0.9,   0.0, 1.0),
    "repeat_penalty":_env_float("RAG_REPEAT_PENALTY", 1.1, 0.5, 2.0),
    "difficulty":    os.getenv("RAG_DIFFICULTY", "intermediate"),
    # Wiki / graph
    "wiki_first_retrieval":   True,
    "wiki_enabled":           True,
    "graph_enabled":          True,
    "graph_depth":            1,
    "save_back_enabled":      False,
    "extraction_mode":        "markdown",   # markdown | plain
    "table_format":           "linearized", # markdown | linearized (row-based, headers repeated)
    "query_expansion":        "auto",       # off | auto (short queries only) | always
    "retrieval_second_pass":  True,         # retry with expanded query when results are weak
    "answer_templates":       True,         # inject structured answer formats per question type
    # Expensive module automation. Defaults are conservative for one local GPU.
    "auto_wiki_build":        _env_bool("RAG_AUTO_WIKI_BUILD", False),
    "auto_graph_build":       _env_bool("RAG_AUTO_GRAPH_BUILD", True),
    # "keyword" (default): build the graph from shared card keywords (instant,
    # deterministic, free). "llm": legacy per-document relation extraction.
    "graph_mode":             os.environ.get("RAG_GRAPH_MODE", "keyword"),
    "auto_contradictions":    _env_bool("RAG_AUTO_CONTRADICTIONS", False),
    "ollama_chat_concurrency": OLLAMA_CHAT_CONCURRENCY,
    "embed_batch_size":       EMBED_BATCH_SIZE,
    # Agent routing
    # Security
    "sensitivity_enforcement": True,
    # Scheduler
    # System prompt
    "system_prompt": (
        "You are a legal document assistant for a law firm. "
        "For document questions, use only the supplied context. "
        "Cite document and page for every factual claim. "
        "Never merge values from different contracts, parties, categories, or table rows. "
        "If information is missing, say exactly what is missing and do not fill gaps with general knowledge. "
        "Do not give legal advice or predict outcomes."
    ),
}

# Technical settings live in the owner's .env (every key is listed in .env.example).
# A key whose variable is set there wins over the global and the user settings.json, so
# no account can change it from the app; GET /api/settings shows only USER_SETTING_KEYS
# to anyone who is not an admin.
TECH_ENV_KEYS: tuple[tuple[str, str], ...] = (
    ("chat_model", "RAG_CHAT_MODEL"),
    ("chat_model_nl", "RAG_CHAT_MODEL_NL"),
    ("chat_model_en", "RAG_CHAT_MODEL_EN"),
    ("embed_model", "RAG_EMBED_MODEL"),
    ("wiki_model", "RAG_WIKI_MODEL"),
    ("relation_model", "RAG_RELATION_MODEL"),
    ("vision_model", "RAG_VISION_MODEL"),
    ("reasoning_model", "RAG_REASONING_MODEL"),
    ("reranker_model", "RAG_RERANKER_MODEL"),
    ("top_k", "RAG_TOP_K"),
    ("wiki_top_k", "RAG_WIKI_TOP_K"),
    ("chunk_size", "RAG_CHUNK_SIZE"),
    ("chunk_overlap", "RAG_CHUNK_OVERLAP"),
    ("num_ctx", "RAG_NUM_CTX"),
    ("retrieval_router", "RAG_RETRIEVAL_ROUTER"),
    ("router_max_docs", "RAG_ROUTER_MAX_DOCS"),
    ("router_max_chunks", "RAG_ROUTER_MAX_CHUNKS"),
    ("router_candidates", "RAG_ROUTER_CANDIDATES"),
    ("card_candidates", "RAG_CARD_CANDIDATES"),
    ("ce_fuse_weight", "RAG_CE_FUSE_WEIGHT"),
    ("card_expand", "RAG_CARD_EXPAND"),
    ("expand_chunks", "RAG_EXPAND_CHUNKS"),
    ("router_per_doc_floor", "RAG_ROUTER_PER_DOC_FLOOR"),
    ("answer_inject_summaries", "RAG_ANSWER_INJECT_SUMMARIES"),
    ("multidoc_max_docs", "RAG_MULTIDOC_MAX_DOCS"),
    ("sibling_max_docs", "RAG_SIBLING_MAX_DOCS"),
    ("chunk_vote_docs", "RAG_CHUNK_VOTE_DOCS"),
    ("matter_anchor", "RAG_MATTER_ANCHOR"),
    ("matter_max_docs", "RAG_MATTER_MAX_DOCS"),
    ("entity_join", "RAG_ENTITY_JOIN"),
    ("entity_max_docs", "RAG_ENTITY_MAX_DOCS"),
    ("reranker", "RAG_RERANKER"),
    ("temperature", "RAG_TEMPERATURE"),
    ("top_p", "RAG_TOP_P"),
    ("repeat_penalty", "RAG_REPEAT_PENALTY"),
    ("difficulty", "RAG_DIFFICULTY"),
    ("auto_wiki_build", "RAG_AUTO_WIKI_BUILD"),
    ("auto_graph_build", "RAG_AUTO_GRAPH_BUILD"),
    ("graph_mode", "RAG_GRAPH_MODE"),
    ("auto_contradictions", "RAG_AUTO_CONTRADICTIONS"),
)

# The only settings a user who is not an admin reads or changes.
USER_SETTING_KEYS: tuple[str, ...] = ("app_language", "edition")

DEFAULT_GROUP_COLORS = [
    "#6366f1", "#8b5cf6", "#0ea5e9", "#10b981",
    "#f59e0b", "#f43f5e", "#14b8a6", "#f97316",
]

DEFAULT_GROUP_EMOJIS = [
    "\U0001f4da", "\U0001f52c", "\U0001f4d0", "\U0001f4bb",
    "\U0001f30d", "\U0001f9ec", "\U0001f4dc", "\U0001f3af",
    "\u2697\ufe0f", "\U0001f3db\ufe0f", "\U0001f4a1", "\U0001f5c2\ufe0f",
]

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("RAG_LOG_LEVEL", "INFO").upper()


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


log = logging.getLogger("rag")


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8"):
    """Write a text file atomically in the target directory with restricted permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)  # restrict before any data is written
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def atomic_write_json(path: Path, data, indent: int = 2):
    atomic_write_text(path, json.dumps(data, indent=indent, ensure_ascii=False))


def loads_lenient(raw: str):
    """json.loads with a best-effort repair pass for common LLM JSON glitches.

    Local models (especially small ones) routinely emit JSON with stray
    backslashes, raw control chars inside strings, or trailing commas. Strict
    json.loads drops the whole object on any of these. We try strict first, then
    apply targeted repairs and retry. Returns None if it still cannot parse.
    """
    try:
        return json.loads(raw)
    except Exception:
        pass
    s = re.sub(r"[\x00-\x1f]+", " ", raw)              # raw control chars in strings
    s = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", s)       # stray (invalid) backslash escapes
    s = re.sub(r",\s*([}\]])", r"\1", s)                # trailing commas
    try:
        return json.loads(s)
    except Exception:
        return None


# ── Instance config I/O ───────────────────────────────────────────────────────

def load_instance() -> dict:
    """Load instance.json. Returns {} if not yet set up."""
    if INSTANCE_JSON.exists():
        try:
            return json.loads(INSTANCE_JSON.read_text())
        except Exception:
            pass
    return {}


def save_instance(data: dict):
    atomic_write_json(INSTANCE_JSON, data)


def load_edition_config() -> dict:
    """Load edition.json (enabled modules, wiki types, etc.)."""
    if EDITION_JSON.exists():
        try:
            return json.loads(EDITION_JSON.read_text())
        except Exception:
            pass
    return {}


def save_edition_config(data: dict):
    atomic_write_json(EDITION_JSON, data)


def is_setup_complete() -> bool:
    inst = load_instance()
    return bool(inst.get("setup_complete"))


def get_active_edition() -> str:
    """Return the active edition key. This branch is the law-firm product,
    so pre-setup installs default to law_firm."""
    env_override = os.getenv("RAG_EDITION", "")
    if env_override:
        return env_override
    return load_instance().get("edition", "law_firm")


# ── Per-user data paths ───────────────────────────────────────────────────────

def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_data_dir(username: str) -> Path:
    validate_safe_id(username, "username")
    return _ensure(DATA_DIR / username)


def user_groups_file(username: str) -> Path:
    return user_data_dir(username) / "groups.json"


def user_uploads_dir(username: str) -> Path:
    return _ensure(user_data_dir(username) / "uploads")


def user_db_path(username: str) -> Path:
    return _ensure(user_data_dir(username) / "rag_db")


def user_history_dir(username: str) -> Path:
    return _ensure(user_data_dir(username) / "chat_history")


def user_overviews_dir(username: str) -> Path:
    return _ensure(user_data_dir(username) / "overviews")


def user_summaries_dir(username: str) -> Path:
    return _ensure(user_data_dir(username) / "doc_summaries")


def user_annotations_dir(username: str) -> Path:
    return _ensure(user_data_dir(username) / "annotations")


def user_backups_dir(username: str) -> Path:
    return _ensure(user_data_dir(username) / "backups")


# ── Settings I/O ──────────────────────────────────────────────────────────────

def load_settings(username: str = "") -> dict:
    """Load settings. User-specific overrides merge on top of global defaults, then every
    TECH_ENV_KEYS key whose variable is set in the environment (.env) is put back, so
    the owner's .env wins over any settings.json."""
    settings = DEFAULT_SETTINGS.copy()
    if SETTINGS_F.exists():
        try:
            settings.update(json.loads(SETTINGS_F.read_text()))
        except Exception:
            pass
    if username:
        user_sf = user_data_dir(username) / "settings.json"
        if user_sf.exists():
            try:
                settings.update(json.loads(user_sf.read_text()))
            except Exception:
                pass
    for key, env in TECH_ENV_KEYS:
        if os.getenv(env, "") != "":
            settings[key] = DEFAULT_SETTINGS[key]
    # Inject active edition into settings so services can read it
    settings.setdefault("edition", get_active_edition())
    return settings


def save_settings_file(s: dict, username: str = ""):
    path = (user_data_dir(username) / "settings.json") if username else SETTINGS_F
    atomic_write_json(path, s)


# ── Groups I/O (per-user) ─────────────────────────────────────────────────────

def _default_groups() -> list[dict]:
    return [{
        "id":              "default",
        "name":            "Algemeen dossier",
        "description":     "Algemeen dossier",
        "color":           "#6366f1",
        "emoji":           "\U0001f4da",
        "created":         datetime.now().isoformat(),
    }]


def load_groups(username: str = "default") -> list[dict]:
    gf = user_groups_file(username)
    if gf.exists():
        try:
            return json.loads(gf.read_text())
        except Exception:
            pass
    # The legacy single-user groups.json is not read here: it holds the first
    # install's matter names, and only the admin's migrate_legacy_data may take them.
    groups = _default_groups()
    save_groups(groups, username)
    return groups


def save_groups(groups: list[dict], username: str = "default"):
    atomic_write_json(user_groups_file(username), groups)


def get_group(group_id: str, username: str = "default") -> dict | None:
    return next((g for g in load_groups(username) if g["id"] == group_id), None)


def group_upload_dir(group_id: str, username: str = "default") -> Path:
    validate_safe_id(group_id, "group_id")
    d = user_uploads_dir(username) / group_id
    d.mkdir(exist_ok=True)
    return d


def group_manifest_path(group_id: str, username: str = "default") -> Path:
    return group_upload_dir(group_id, username) / "_manifest.json"


def load_group_manifest(group_id: str, username: str = "default") -> dict:
    p = group_manifest_path(group_id, username)
    if not p.exists():
        return {"disabled": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"disabled": []}


def save_group_manifest(group_id: str, username: str, manifest: dict):
    atomic_write_json(group_manifest_path(group_id, username), manifest)


def group_doc_paths(group_id: str, group: dict, username: str = "default") -> list[Path]:
    base = group_upload_dir(group_id, username)
    disabled = set(load_group_manifest(group_id, username).get("disabled", []))
    paths: list[Path] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        if p.name == "_manifest.json":
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        rel = p.relative_to(base).as_posix()
        if rel not in disabled:
            paths.append(p)
    return paths


# ── Legacy migration ──────────────────────────────────────────────────────────

def migrate_legacy_data(username: str):
    """Move pre-isolation global data into a user's directory (run once)."""
    import shutil
    marker = user_data_dir(username) / ".migrated"
    if marker.exists():
        return

    pairs = [
        (_LEGACY_UPLOADS,  user_uploads_dir(username)),
        (_LEGACY_DB,       user_db_path(username)),
        (_LEGACY_HISTORY,  user_history_dir(username)),
        (_LEGACY_OVERVIEW, user_overviews_dir(username)),
        (_LEGACY_SUMMARY,  user_summaries_dir(username)),
    ]
    for src, dst in pairs:
        if src.exists() and src.is_dir():
            for item in src.iterdir():
                target = dst / item.name
                if not target.exists():
                    try:
                        if item.is_dir():
                            shutil.copytree(item, target)
                        else:
                            shutil.copy2(item, target)
                    except Exception as e:
                        log.warning("Migration copy failed %s → %s: %s", item, target, e)

    if _LEGACY_GROUPS.exists() and not user_groups_file(username).exists():
        try:
            save_groups(json.loads(_LEGACY_GROUPS.read_text()), username)
        except Exception as e:
            log.warning("Legacy groups migration failed: %s", e)

    marker.write_text("1")
    log.info("Legacy data migrated for user %s", username)


# ── Startup ───────────────────────────────────────────────────────────────────

def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "license").mkdir(exist_ok=True)


def check_ollama() -> bool:
    try:
        import urllib.request
        host = OLLAMA_HOST.rstrip("/")
        with urllib.request.urlopen(f"{host}/api/tags", timeout=3):
            pass
        return True
    except Exception as e:
        log.warning("Ollama not reachable: %s", e)
        return False


def safe_num_ctx(value=None, default: int = 8192) -> int:
    """Clamp Ollama context to avoid accidental VRAM exhaustion."""
    try:
        ctx = int(value if value is not None else default)
    except (TypeError, ValueError):
        ctx = default
    return max(512, min(ctx, MAX_OLLAMA_NUM_CTX))


def ollama_options(num_ctx=None, **extra) -> dict:
    opts = {"num_ctx": safe_num_ctx(num_ctx)}
    opts.update({k: v for k, v in extra.items() if v is not None})
    return opts


def ollama_keep_alive() -> str:
    return OLLAMA_KEEP_ALIVE
