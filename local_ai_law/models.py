"""Pydantic request/response models for the API.

One model per request body the routes accept; field constraints here are the first
validation gate (a bad payload is rejected before any handler runs). When you add a
setting to `SettingsIn` you must also add it to `config.py:DEFAULT_SETTINGS` (the
invariant called out in CLAUDE.md).
"""

from typing import Optional
from pydantic import BaseModel, Field


# ── Existing models ───────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    """User-settable app settings (PATCH /api/settings). Every field is optional so a
    partial update only overrides the keys sent; bounds mirror `DEFAULT_SETTINGS`."""
    app_language:  Optional[str] = Field(None, pattern=r"^(en|nl)$")
    chat_model:    Optional[str] = None
    chat_model_nl: Optional[str] = None
    chat_model_en: Optional[str] = None
    embed_model:   Optional[str] = None
    wiki_model:    Optional[str] = None
    relation_model: Optional[str] = None
    vision_model:  Optional[str] = None
    top_k:         Optional[int] = Field(None, ge=1, le=50)
    wiki_top_k:    Optional[int] = Field(None, ge=1, le=20)
    chunk_size:    Optional[int] = Field(None, ge=50, le=2000)
    chunk_overlap: Optional[int] = Field(None, ge=0, le=500)
    num_ctx:        Optional[int]   = Field(None, ge=512, le=32768)
    retrieval_router:   Optional[bool] = None
    router_max_docs:    Optional[int]  = Field(None, ge=1, le=10)
    router_max_chunks:  Optional[int]  = Field(None, ge=1, le=20)
    router_candidates:  Optional[int]  = Field(None, ge=3, le=50)
    card_candidates:    Optional[int]  = Field(None, ge=5, le=200)
    ce_fuse_weight:     Optional[float] = Field(None, ge=0.0, le=1.0)
    card_expand:        Optional[int]  = Field(None, ge=0, le=5)
    expand_chunks:      Optional[int]  = Field(None, ge=1, le=10)
    # Multi-doc synthesis support (see config.py DEFAULT_SETTINGS).
    router_per_doc_floor:    Optional[int]  = Field(None, ge=0, le=5)
    answer_inject_summaries: Optional[bool] = None
    multidoc_max_docs:       Optional[int]  = Field(None, ge=1, le=10)
    sibling_max_docs:        Optional[int]  = Field(None, ge=0, le=5)
    chunk_vote_docs:         Optional[int]  = Field(None, ge=0, le=5)
    reasoning_model:         Optional[str]  = None
    matter_anchor:           Optional[bool] = None
    matter_max_docs:         Optional[int]  = Field(None, ge=1, le=20)
    entity_join:             Optional[bool] = None
    entity_max_docs:         Optional[int]  = Field(None, ge=1, le=20)
    reranker:           Optional[bool] = None
    reranker_model:     Optional[str]  = None
    temperature:    Optional[float] = Field(None, ge=0.0, le=2.0)
    top_p:          Optional[float] = Field(None, ge=0.0, le=1.0)
    repeat_penalty: Optional[float] = Field(None, ge=0.5, le=2.0)
    system_prompt:  Optional[str]   = None
    difficulty:    Optional[str] = Field(None, pattern=r"^(beginner|intermediate|expert)$")
    wiki_first_retrieval: Optional[bool] = None
    wiki_enabled:  Optional[bool] = None
    graph_enabled: Optional[bool] = None
    graph_depth:   Optional[int] = Field(None, ge=1, le=3)
    save_back_enabled: Optional[bool] = None
    sensitivity_enforcement: Optional[bool] = None
    auto_wiki_build: Optional[bool] = None
    auto_graph_build: Optional[bool] = None
    graph_mode: Optional[str] = None
    auto_contradictions: Optional[bool] = None
    ollama_chat_concurrency: Optional[int] = Field(None, ge=1, le=8)
    embed_batch_size: Optional[int] = Field(None, ge=1, le=128)
    extraction_mode: Optional[str] = Field(None, pattern=r"^(markdown|plain)$")
    table_format: Optional[str] = Field(None, pattern=r"^(markdown|linearized)$")
    query_expansion: Optional[str] = Field(None, pattern=r"^(off|auto|always)$")
    retrieval_second_pass: Optional[bool] = None
    answer_templates: Optional[bool] = None


class TokenEstimateIn(BaseModel):
    """Request to estimate the token budget of a pending chat turn (for the context meter)."""
    group_id: Optional[str] = None
    model: str = Field(..., min_length=1)
    messages: list[dict] = []
    pending_input: str = ""
    num_ctx: Optional[int] = Field(None, ge=1, le=32768)


class FindIn(BaseModel):
    """Finder request: 'where do I look?' — runs retrieval only, returns ranked pointers
    (no LLM answer). See routes/finder.py + services.retrieval.find_pointers."""
    query:    str = Field(..., min_length=1, max_length=2000)
    group_id: str = Field(..., min_length=1)
    max_docs: Optional[int] = Field(8, ge=1, le=25)


class GroupIn(BaseModel):
    """Create/update a document group (a knowledge collection of uploaded docs)."""
    name:            str = Field(..., min_length=1, max_length=100)
    description:     Optional[str] = Field("", max_length=500)
    color:           Optional[str] = "cat-1"
    emoji:           Optional[str] = "\U0001f4da"
    language:        Optional[str] = Field(None, pattern=r"^(en|nl)$")
    group_type:      Optional[str] = Field("knowledge", pattern=r"^knowledge$")



class AnnotationIn(BaseModel):
    """A user highlight/note anchored to a character span of a document."""
    doc_name:      str = Field(..., min_length=1)
    start_offset:  int = Field(..., ge=0)
    end_offset:    int = Field(..., ge=0)
    selected_text: str = ""
    note:          str = ""
    color:         str = "yellow"


# ── Setup wizard models ───────────────────────────────────────────────────────

class SetupStepEdition(BaseModel):
    """Setup wizard step 1 — organisation identity, edition, and default language/jurisdiction."""
    org_name:    str = Field(..., min_length=1, max_length=200)
    edition:     str = Field("law_firm", pattern=r"^law_firm$")
    preset:      Optional[str] = None
    language:    str = Field("nl", pattern=r"^(en|nl)$")
    jurisdiction: Optional[str] = Field(None, max_length=100)


class SetupStepModels(BaseModel):
    """Setup wizard step 2 — the Ollama models to use for chat/embed/wiki/relations."""
    chat_model:     str = Field(..., min_length=1)
    embed_model:    str = Field(..., min_length=1)
    wiki_model:     Optional[str] = None
    relation_model: Optional[str] = None


class SetupStepAdmin(BaseModel):
    """Setup wizard step 3 — the first admin account credentials."""
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=8, max_length=128)


class SetupStepSecurity(BaseModel):
    """Setup wizard step 4 — security policy (sensitivity enforcement, lockout, machine binding)."""
    sensitivity_enforcement: bool = False
    lock_attempts:           int = Field(3, ge=1, le=10)
    machine_binding:         bool = False


class SetupComplete(BaseModel):
    """Full setup wizard payload — all steps submitted together to finalise install."""
    edition:     SetupStepEdition
    models:      SetupStepModels
    admin:       SetupStepAdmin
    security:    Optional[SetupStepSecurity] = None


# ── Wiki models ───────────────────────────────────────────────────────────────

class WikiGenerateIn(BaseModel):
    """Request to generate a wiki page of a given type from named source docs."""
    group_id:    str = Field(..., min_length=1)
    page_type:   str = Field(..., min_length=1)
    topic:       str = Field(..., min_length=1, max_length=200)
    doc_names:   list[str] = Field(..., min_length=1)
    tags:        list[str] = []
    sensitivity: str = Field("internal", pattern=r"^(normal|internal|privileged|sensitive)$")
    language:    str = Field("nl", pattern=r"^(en|nl)$")


class WikiPageUpdateIn(BaseModel):
    """Edit an existing wiki page's body / sensitivity / tags."""
    body:        str = Field(..., min_length=1)
    sensitivity: Optional[str] = Field(None, pattern=r"^(normal|internal|privileged|sensitive)$")
    tags:        Optional[list[str]] = None


class SaveBackIn(BaseModel):
    """Persist an AI-drafted answer back into the wiki as a sourced page."""
    group_id:  str = Field(..., min_length=1)
    page_type: str = Field(..., min_length=1)
    topic:     str = Field(..., min_length=1, max_length=200)
    content:   str = Field(..., min_length=1)
    source_docs: list[str] = []
    tags:      list[str] = []


# ── License models ────────────────────────────────────────────────────────────

class LicenseImportIn(BaseModel):
    """Import a signed license file ({payload, signature}) for a user."""
    username:     str = Field(..., min_length=1)
    license_json: dict  # full {payload, signature} dict


class UnlockIn(BaseModel):
    """Unlock a locked install with a signed operator recovery token."""
    token_json: dict  # signed operator recovery token


# ── Job models ────────────────────────────────────────────────────────────────

class IngestionJobIn(BaseModel):
    """Kick off a (re)ingestion job — normalize/wiki/graph stages over some or all docs."""
    group_id:   str = Field(..., min_length=1)
    doc_names:  list[str] = []   # empty = all docs in group
    run_normalize: bool = True
    run_wiki:      bool = True
    run_graph:     bool = True
    force_rebuild: bool = False


# ── File/Folder creation models ──────────────────────────────────────────────

class FileCreateIn(BaseModel):
    """Create a new document file inside a group (optionally within a folder)."""
    name:    str = Field(..., min_length=1, max_length=200)
    folder:  str = Field("", max_length=400)
    content: str = ""


class FolderCreateIn(BaseModel):
    """Create a new folder within a group's document tree."""
    name:   str = Field(..., min_length=1, max_length=200)
    parent: str = Field("", max_length=400)


class FileContentIn(BaseModel):
    """Overwrite the content of an existing document file."""
    content: str


# ── Graph models ──────────────────────────────────────────────────────────────

class GraphNodeIn(BaseModel):
    """Manually add a knowledge-graph node (concept/document/entity) to a group."""
    type:        str = Field(..., min_length=1)
    label:       str = Field(..., min_length=1, max_length=200)
    group_id:    str = Field(..., min_length=1)
    source_path: Optional[str] = None
    metadata:    dict = {}


class GraphEdgeIn(BaseModel):
    """Manually add a directed, weighted relation between two graph nodes."""
    source_id: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    relation:  str = Field(..., min_length=1)
    weight:    float = Field(1.0, ge=0.0, le=10.0)
    metadata:  dict = {}
