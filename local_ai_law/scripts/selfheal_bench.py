#!/usr/bin/env python3
"""Bilingual (NL + EN) self-healing benchmark for the legal RAG.

Runs the 100-question testbase for BOTH the Dutch (nl-realistic) and English
(en-realistic) groups against the live /ws/chat pipeline, auto-scores the
factual question types, and records per-item retrieval-hit / generation results
so failures are diagnosable. Scorers are language-generalized (NL + EN keywords)
so a correct English answer is not counted as a failure.

Run (in tmux, app venv, PYTHONPATH=repository root):
  PYTHONPATH=<repo-parent> \
  OLLAMA_HOST=$OLLAMA_HOST \
  ./venv/bin/python scripts/selfheal_bench.py --tag baseline --top-k 5

Outputs (under legal_corpus/_demo_upload/):
  selfheal_<tag>.json     full per-item results for both languages
  selfheal_<tag>.txt      human summary (per language x type, retrieval hit)
"""
from __future__ import annotations
import sys, re, time, json, glob, os, asyncio, argparse, pathlib, unicodedata
import requests, websockets

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from local_ai_law.services.auth import create_token

BASE = "http://localhost:8000"; API = BASE + "/api"
USER = "pdm95"
CORPUS = pathlib.Path(os.getenv("LEGAL_CORPUS_DIR",
                                str(pathlib.Path(__file__).resolve().parents[2] / "legal_corpus")))
OUTDIR = CORPUS / "_demo_upload"

# Each group: (variant, group_name, language). The NL group is the already-indexed
# bakeoff group; the EN group is created/indexed on first run.
GROUPS = [
    {"variant": "nl-realistic", "name": "nl-realistic FULL (bakeoff)",  "lang": "nl"},
    {"variant": "en-realistic", "name": "en-realistic NATIVE (selfheal)", "lang": "en"},
    # General full-law bilingual corpus (gen_lawfox.py firm=fulllaw, 14 practice areas) — the product.
    {"variant": "nl-fulllaw", "name": "nl-fulllaw FULL (general)", "lang": "nl"},
    {"variant": "en-fulllaw", "name": "en-fulllaw FULL (general)", "lang": "en"},
    # The former LAWFOX demo variants (nl/en-lawfox-demo) are archived under
    # legal_corpus/variants/_archive/. Re-add an entry here to bench them.
]

token = create_token(USER, "user")
H = {"Authorization": f"Bearer {token}"}
def log(*a): print(*a, flush=True)


# ───────────────────────── setup helpers ─────────────────────────
def get_or_create_group(name, lang):
    for g in requests.get(f"{API}/groups", headers=H, timeout=30).json().get("groups", []):
        if g.get("name") == name:
            return g["id"]
    r = requests.post(f"{API}/groups", headers=H, timeout=30,
                      json={"name": name, "language": lang, "emoji": "⚖️"})
    r.raise_for_status(); return r.json()["id"]


def doc_count(gid):
    docs = requests.get(f"{API}/groups/{gid}/documents", headers=H, timeout=30).json()
    return len(docs.get("documents", [])) if isinstance(docs, dict) else len(docs)


def upload_all(gid, docroot):
    files = sorted(glob.glob(str(docroot / "**" / "*.md"), recursive=True))
    ok = 0
    for i, fp in enumerate(files, 1):
        with open(fp, "rb") as fh:
            r = requests.post(f"{API}/groups/{gid}/upload", headers=H,
                              files={"file": (os.path.basename(fp), fh, "text/markdown")}, timeout=60)
        ok += r.status_code == 200
        if i % 50 == 0: log(f"  upload {i}/{len(files)}")
    log(f"  uploaded {ok}/{len(files)}")


def index_and_wait(gid):
    """Force a CLEAN, COMPLETE index of the group, then wait for the whole job.

    Why reset + drain (this bit us on a fresh bulk upload): uploading N docs
    auto-submits a per-file index job (routes/documents.py), and those jobs dedupe
    per group — only the first actually runs, indexing whatever happened to be on
    disk at that instant (often a single file). Polling `is_indexing` right after
    the upload burst can therefore observe that tiny 1-doc job finish and return a
    1-document index, so every later query collapses to that one doc (100% retr=miss).
    Guard: (1) drain any leftover auto-index, (2) trigger a full reset re-embed,
    (3) wait for it to actually START before (4) waiting for it to COMPLETE.
    Retrieval is card-routed (`retrieve_carded`), and card/summary generation is the
    tail of the same job — so we must wait for the FULL job, not just chunk embedding,
    or retrieval can only reach the early docs whose cards exist yet."""
    def status():
        return requests.get(f"{API}/groups/{gid}/index/status", headers=H, timeout=30).json()
    # 1. drain any auto-index still running from the upload burst
    t0 = time.time()
    while status().get("is_indexing") and time.time() - t0 < 1800:
        time.sleep(5)
    # 2. full reset re-embed — the only way to guarantee EVERY doc is embedded + carded
    r = requests.post(f"{API}/groups/{gid}/index", headers=H, params={"reset": "true"}, timeout=30)
    if r.status_code != 409: r.raise_for_status()
    # 3. wait for the job to actually start (avoid the not-yet-started false "done")
    t0 = time.time()
    while time.time() - t0 < 60 and not status().get("is_indexing"):
        time.sleep(2)
    # 4. wait for completion (chunk embedding AND card/summary generation)
    t0 = time.time()
    while True:
        time.sleep(5)
        s = status()
        if int(time.time() - t0) % 30 < 6:
            log(f"  index [{int(time.time()-t0)}s] busy={s.get('is_indexing')} "
                f"files {s.get('done_files')}/{s.get('total_files')} "
                f"chunks {s.get('done_chunks')}/{s.get('total_chunks')} {s.get('phase','')}")
        if not s.get("is_indexing"): break


def ensure_group(grp, skip_index=False):
    docroot = CORPUS / "variants" / grp["variant"] / "documents"
    gid = get_or_create_group(grp["name"], grp["lang"])
    have = doc_count(gid)
    expected = len(glob.glob(str(docroot / "**" / "*.md"), recursive=True))
    log(f"[{grp['variant']}] group {gid}: {have}/{expected} docs")
    if have < max(1, int(expected * 0.95)):
        log(f"  uploading {grp['variant']} corpus...")
        upload_all(gid, docroot)
    if skip_index:
        log("  skip-index: using existing index")
        return gid
    index_and_wait(gid)
    return gid


# ───────────────────────── ask ─────────────────────────
async def _ask(gid, q, model, top_k):
    url = f"{BASE.replace('http','ws')}/ws/chat?token={token}"
    ans, src = [], []
    async with websockets.connect(url, open_timeout=30, max_size=None,
                                  additional_headers={"Origin": BASE}) as ws:
        await ws.send(json.dumps({"type": "query", "query": q, "group_id": gid,
                                  "model": model, "top_k": top_k, "difficulty": "intermediate"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=600))
            t = m.get("type")
            if t == "chunk": ans.append(m.get("content", ""))
            elif t == "sources": src = m.get("sources", m.get("content", []))
            elif t == "error": ans.append(f"[ERROR] {m.get('content')}"); break
            elif t == "done": break
    names = [s.get("doc") or s.get("name") or s.get("source") or "" for s in src] if src else []
    return "".join(ans).strip(), names


def ask(gid, q, model, top_k):
    return asyncio.run(_ask(gid, q, model, top_k))


# ───────────────────────── scorers (NL + EN) ─────────────────────────
def _norm(s): return re.sub(r"\s+", "", s.lower())

def score_statutes(ans, gold):
    a = _norm(ans); cores = []
    for g in gold:
        m = re.search(r"(\d+:\d+[a-z]?|\d+\s*lid\s*\d+|\d+)", g.lower())
        if m: cores.append(_norm(m.group(1)))
    if not cores: return None
    return sum(1 for c in cores if c in a) / len(cores)

# Privilege scoring (NL + EN). The earlier any()-overlap scorer double-counted:
# a correct privileged answer ("VERTROUWELIJK ... mag niet aan de wederpartij
# worden getoond") tripped a NO keyword and scored 0. The real discriminator is
# the disclosure-to-OPPOSING-PARTY verdict: anchor on the opposing-party mention
# and read the words right before it — a negation ("mag NIET aan de wederpartij")
# means privileged, a bare disclosure modal ("kan aan de wederpartij getoond")
# means disclosable. This avoids confusing internal-sharing clauses, and falls
# back to echoed `privileged:` flags and bare confidentiality words.
_PARTY = re.compile(r"(wederpartij|tegenpartij|opposing party|other (?:side|party))")
_NEG = re.compile(r"\b(niet|geen|nooit|not|never|cannot|can ?not|may not)\b")
_DISCLOSE = re.compile(r"\b(mag|kan|mogen|kunnen|may|can|wordt|worden|getoond|gedeeld|"
                       r"disclosed|shared|shown)\b")

_PRIV_CONF = re.compile(r"\[?\s*(confidential|privileged|vertrouwelijk|geprivilegieerd|"
                        r"attorney-client|advocaat-cli)")
# A NEGATED-confidential or disclosable VERDICT about the document (gap-tolerant —
# Dutch/English word order puts the negation several words from the keyword). NB:
# the bare framework labels "OPEN/EXTERNAL" / "PRIVILEGED" are NOT verdicts and are
# deliberately excluded so the two-group answer format can't trip the scorer.
_PRIV_NOT = re.compile(
    r"\b(not|niet|geen)\b[^.]{0,40}?(confidential|privileged|attorney-client|"
    r"vertrouwelijk|advocaat-cli|geprivilegieerd|aangemerkt)"
    r"|not privileged|not confidential|niet[- ]vertrouwelijk|disclosable"
    r"|may be (?:disclosed|shown)|(?:mag|kan)(?: worden)? (?:getoond|gedeeld)")
# A NEGATED disclosure ("may NOT be shown to the opposing party", "mag niet aan de
# wederpartij worden getoond") is a PRIVILEGED signal, not a "not confidential"
# one — must win over _PRIV_NOT, which would otherwise misread the "niet".
_PRIV_NODISCLOSE = re.compile(
    r"\b(not|niet|never|nooit|may not|cannot|mag niet|mogen niet)\b"
    r"[^.]{0,45}?(getoond|gedeeld|verstrekt|shown|disclosed|shared|"
    r"wederpartij|tegenpartij|opposing)")


def score_privilege(ans, gold):
    """Decide whether the answer classifies the NAMED document as privileged.
    The legal answer policy makes the model output a two-group framework
    (PRIVILEGED + OPEN/EXTERNAL labels), so naive token presence is ambiguous —
    read the document's OWN verdict: a negated-confidential / disclosable verdict
    in the opening means "not privileged"; otherwise a confidential marker means
    "privileged". Falls back to the older heuristics only when neither is found."""
    a = re.sub(r"\s+", " ", (ans or "").lower())
    # Strip diacritics so accented negations (e.g. "géén") match the same as plain.
    a = unicodedata.normalize("NFKD", a)
    a = "".join(c for c in a if not unicodedata.combining(c))
    want = "yes" if bool(gold) else "no"
    decided = None

    # 1. Unambiguous machine-readable frontmatter echo (exclusive only).
    has_true = bool(re.search(r"privileged\s*[:=]\s*true", a))
    has_false = bool(re.search(r"privileged\s*[:=]\s*false", a))
    if has_true and not has_false:
        decided = "yes"
    elif has_false and not has_true:
        decided = "no"

    # 2. Leading verdict: a negated/disclosable verdict wins (check it FIRST, since
    #    a confidential keyword appears in both verdicts); else a confidential
    #    marker means privileged. Read the opening, where the verdict is stated.
    if decided is None:
        head = a[:320]
        if _PRIV_NODISCLOSE.search(head):
            decided = "yes"
        elif _PRIV_NOT.search(head):
            decided = "no"
        elif _PRIV_CONF.search(head):
            decided = "yes"

    # 3. Legacy fallbacks (opposing-party proximity, then whole-answer keyword).
    if decided is None:
        m = _PARTY.search(a)
        if m:
            pre = a[max(0, m.start() - 45):m.start()]
            if _NEG.search(pre):
                decided = "yes"
            elif _DISCLOSE.search(pre):
                decided = "no"
    if decided is None:
        if re.search(r"\b(vertrouwelijk|geprivilegieerd|advocaat-cli|confidential|"
                     r"privileged|attorney-client)\b", a):
            decided = "yes"
    if decided is None:
        return 0.0
    return 1.0 if decided == want else 0.0

_SUFFIX_RE = re.compile(r"\b(b\.?v\.?|ltd\.?|llp|n\.?v\.?|gmbh|inc\.?|s\.?a\.?|plc)\.?\s*", re.I)

def score_parties(ans, gold):
    a = ans.lower()
    # Strip corporate suffixes anywhere (EN + NL forms), robust to a trailing
    # "B.V." with no following word char (where a trailing \b never matches).
    cores = [_SUFFIX_RE.sub(" ", p.lower()).strip().strip(".").strip() for p in gold]
    cores = [c for c in cores if len(c) > 2]
    if not cores: return None
    return sum(1 for c in cores if c in a) / len(cores)

DT = {
    "contract": ["overeenkomst", "contract", "agreement"],
    "correspondence": ["brief", "correspondent", "e-mail", "email", "sommatie", "schrijven",
                       "letter", "correspondence", "demand", "notice"],
    "memo": ["memo", "advies", "memorandum", "advice"],
    "pleading": ["pleit", "conclusie", "dagvaard", "processtuk", "akte",
                 "pleading", "statement of claim", "writ", "summons", "brief", "procedural"],
    "opinion": ["advies", "opinie", "legal opinion", "opinion"],
    "attendance_note": ["notitie", "aantekening", "gespreksverslag", "telefoonnotitie", "verslag",
                        "attendance note", "note", "telephone note", "memo of call", "record"],
    "board_minute": ["notulen", "besluit", "vergadering", "aandeelhoudersbesluit",
                     "minutes", "resolution", "board", "meeting"],
}

def score_doctype(ans, gold):
    a = ans.lower()
    keys = DT.get(gold, [str(gold).lower()])
    return 1.0 if any(k in a for k in keys) else 0.0

# ───────────────────────── LLM-as-judge (reasoning) ─────────────────────────
JUDGE_MODEL = os.getenv("RAG_JUDGE_MODEL", "")  # resolved in main() if empty
JUDGE_RUNS = 1  # >1 => median of N judge passes (stabilizes the seeded n=1 subtypes)
_REFUSAL = re.compile(
    r"not (in|present|found|available|contain(s|ed)?|stat(e|es|ed)|mention(s|ed)?|specif|provided?)"
    r"|does not (contain|state|mention|specify|provide|include|exist|appear)"
    r"|niet (in|aanwezig|vermeld|terug te vinden|opgenomen|genoemd|bekend|beschikbaar)"
    r"|geen (informatie|gegevens|bedrag|sprake|document|stuk|vermelding)"
    r"|cannot (be )?(answer|determin|find)|kan (niet|het niet|geen)"
    r"|no (information|data|mention|such|document|record|court|ruling|mention of)"
    r"|there is no|does not (exist|appear|contain)|is not (mentioned|stated|present)"
    r"|geen .* (beschikbaar|gevonden|in de stukken)|bestaat (geen|niet)"
    r"|bevat geen|context (bevat|geeft) geen|er is geen|geen dergelij|is er geen"
    r"|geen (overeenkomst|transactie|fusie|uitspraak|verkoop|vonnis|veroordeling)"
    r"|premise|onjuiste (aanname|premisse)|not provided in", re.I)


def judge_answer(item, ans, model):
    """LLM-as-judge: grade a reasoning answer against the reference gold_answer.
    Returns 0.0 / 0.5 / 1.0. For 'unanswerable' items, a deterministic refusal
    check gates full credit so a fabricated value can never score 1.0."""
    from local_ai_law.services.ollama_client import chat as ollama_chat
    from local_ai_law.config import loads_lenient
    gold = item.get("gold") or item.get("gold_answer") or ""
    q = item["question"]
    # Types whose ONLY correct answer is a refusal: unanswerable facts AND
    # misleading / false-premise / non-existent-entity traps.
    t = item.get("type", "")
    unanswerable = any(k in t for k in ("onbeantwoord", "unanswer", "misleid", "trap", "premise"))
    # Connection questions are under-determined: two same-matter documents share
    # MANY valid concepts, but the reference names only one. Grade on whether the
    # answer identifies a genuine shared concept and each document's role — NOT on
    # echoing the reference's exact wording. (Raw retrieval-hit stays reported
    # separately, so this leniency cannot mask a retrieval miss.)
    connection = "connection" in t
    if connection:
        prompt = (
            "You are a strict legal-answer grader for a DOCUMENT-CONNECTION question. "
            "The candidate must (1) identify a genuine legal concept, issue, or subject "
            "that BOTH named documents share, and (2) explain each document's role with "
            "respect to it. Two documents in one matter usually share SEVERAL valid "
            "concepts; the REFERENCE names ONE acceptable example, but ANY concept truly "
            "common to both documents is correct. Do NOT require the candidate to use the "
            "reference's exact wording, and do NOT penalize a different but valid concept.\n\n"
            f"QUESTION:\n{q}\n\nREFERENCE (one acceptable concept):\n{gold}\n\n"
            f"CANDIDATE ANSWER:\n{ans}\n\n"
            "Scoring: 1.0 = identifies a real shared concept AND explains both documents' "
            "roles; 0.5 = a plausible shared concept but one/both roles missing or vague; "
            "0.0 = no real shared concept, fabricated, or the wrong documents.\n"
            'Respond with ONLY JSON: {"score": 0.0, "reason": "..."} (score one of 0.0,0.5,1.0).'
        )
    else:
        prompt = (
            "You are a strict legal-answer grader. Compare the CANDIDATE answer to the "
            "REFERENCE answer for the QUESTION. Grade only on the key legal points and the "
            "final conclusion, not on style or length.\n\n"
            f"QUESTION:\n{q}\n\nREFERENCE (correct) ANSWER:\n{gold}\n\n"
            f"CANDIDATE ANSWER:\n{ans}\n\n"
            "Scoring: 1.0 = all key points AND the correct conclusion; 0.5 = partially "
            "correct or a key point missing/wrong; 0.0 = wrong, fabricated, or misses the "
            "point. If the reference says the info is NOT in the documents, award 1.0 only "
            "when the candidate also declines to answer and invents no value.\n"
            'Respond with ONLY JSON: {"score": 0.0, "reason": "..."} (score one of 0.0,0.5,1.0).'
        )
    def one_pass():
        resp = ollama_chat(model=model, messages=[{"role": "user", "content": prompt}],
                           num_ctx=8192, options={"temperature": 0.0})
        raw = resp["message"]["content"]
        raw = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.DOTALL).strip()
        a, b = raw.find("{"), raw.rfind("}") + 1
        data = loads_lenient(raw[a:b]) if a >= 0 and b > a else {}
        return min(1.0, max(0.0, float(data.get("score", 0.0))))
    try:
        scores = [one_pass() for _ in range(max(1, JUDGE_RUNS))]
        scores.sort()
        sc = scores[len(scores) // 2]   # median (stabilizes judge non-determinism)
    except Exception as e:
        log(f"  [judge error] {e}")
        return None
    # unanswerable: full credit requires an actual refusal, never a fabricated value.
    if unanswerable and sc >= 1.0 and not _REFUSAL.search(ans or ""):
        sc = 0.0
    return sc


def score(item, ans):
    s = item["scorer"]
    if s == "judge":
        return judge_answer(item, ans, JUDGE_MODEL)
    return {"statutes": score_statutes, "privilege": score_privilege,
            "parties": score_parties, "doctype": score_doctype}.get(s, lambda *_: None)(ans, item.get("gold"))


def retrieval_hit(item, sources):
    """Did the expected document(s) appear in the retrieved sources? Multi-doc
    gold (connection: a comma-joined doc_id) counts as a hit only when EVERY
    listed document is present — the whole point is surfacing both siblings."""
    want = (item.get("doc_id") or "").lower()
    if not want:
        return None
    wants = [w.strip() for w in want.split(",") if w.strip()]
    srcs = [str(s).lower() for s in sources]
    return all(any(w in s for s in srcs) for w in wants)


def _norm_title(t):
    t = re.sub(r"[*`]", "", (t or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def _bare_did(s):
    return re.sub(r"\.md$", "", str(s).strip().lower())


def _norm_party(p):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (p or "").lower())).strip()


def build_doc_info(gid, user):
    """did -> {title (normalized), parties (frozenset), matter}. Source for the
    sibling-aware hit. Keyed by bare doc id; summaries key off `doc_name`."""
    info = {}
    try:
        from local_ai_law.services.doc_summary import load_doc_summaries
        for name, e in load_doc_summaries(gid, user).items():
            if not isinstance(e, dict):
                continue
            did = _bare_did(e.get("doc_name") or e.get("doc_id") or name)
            parties = frozenset(x for x in (_norm_party(p) for p in (e.get("parties") or [])) if x)
            info[did] = {"title": _norm_title(e.get("title") or ""),
                         "parties": parties,
                         "matter": did.split("-")[0] if "-" in did else did}
    except Exception as ex:
        log(f"  [doc_info] {ex}")
    return info


def sibling_hit(item, sources, info):
    """Secondary, sibling-AWARE hit. Reported ALONGSIDE the raw hit, never replacing
    it. Counts a hit when the gold doc OR a sibling that makes the single gold
    *arbitrary* is retrieved:
      * same normalized TITLE (a duplicate-title doc), or
      * for a PARTIES question, a same-matter doc listing the SAME parties set — the
        gold party doc is arbitrary among siblings that list identical parties, so
        retrieving any of them answers the question.
    Generalizes (no corpus-specific ids); the raw hit stays honest beside it."""
    want = _bare_did(item.get("doc_id") or "")
    if not want:
        return None
    if any(want in str(s).lower() for s in sources):
        return True
    g = info.get(want)
    if not g:
        return False
    src_ids = [_bare_did(s) for s in sources]
    if g["title"] and any(info.get(d, {}).get("title") == g["title"] for d in src_ids):
        return True
    if item.get("type") == "parties" and g["parties"]:
        if any(info.get(d, {}).get("parties") == g["parties"]
               and info.get(d, {}).get("matter") == g["matter"] for d in src_ids):
            return True
    return False


# ───────────────────────── main ─────────────────────────
def run_group(grp, model, top_k, filt=None, skip_index=False, limit=0):
    gid = ensure_group(grp, skip_index=skip_index)
    tb_path = CORPUS / "variants" / grp["variant"] / "qa" / "testbase.jsonl"
    tb = [json.loads(l) for l in tb_path.read_text().splitlines() if l.strip()]
    if filt:
        tb = [it for it in tb if filt in it.get("type", "")]
    if limit and limit > 0:
        # Stratified per-type cap: keep the first `limit` of each type so a cheap
        # probe still covers every type (deterministic — testbase order is stable).
        seen: dict = {}
        capped = []
        for it in tb:
            t = it.get("type", "")
            seen[t] = seen.get(t, 0) + 1
            if seen[t] <= limit:
                capped.append(it)
        tb = capped
    # did -> {title, parties, matter}, for the sibling-aware (secondary) hit metric.
    info = build_doc_info(gid, USER)
    log(f"\n{'='*70}\n[{grp['lang'].upper()}] {grp['variant']}: {len(tb)} questions\n{'='*70}")
    rows = []
    for it in tb:
        t0 = time.time()
        try:
            ans, src = ask(gid, it["question"], model, top_k)
        except Exception as e:
            ans, src = f"[EXC] {e}", []
        sc = score(it, ans)
        hit = retrieval_hit(it, src)
        sib = sibling_hit(it, src, info)
        rows.append({**it, "lang": grp["lang"], "answer": ans, "sources": src,
                     "score": sc, "retrieval_hit": hit, "retrieval_hit_sib": sib,
                     "secs": round(time.time()-t0, 1)})
        tag = "JUDGE" if sc is None else f"{sc:.2f}"
        hh = "?" if hit is None else ("H" if hit else "miss")
        log(f"[{grp['lang']}] {it['id']} {it['type']:<20} score={tag:>5} retr={hh:<4} ({round(time.time()-t0,1)}s) {it['question'][:55]}")
    return {"variant": grp["variant"], "lang": grp["lang"], "group_id": gid, "rows": rows}


def summarize(groups_out, model, top_k, tag, elapsed):
    lines = [f"SELF-HEAL BENCH  tag={tag}  model={model}  top_k={top_k}  ({int(elapsed)}s)", "=" * 64]
    for go in groups_out:
        rows = go["rows"]
        auto = [r for r in rows if r["score"] is not None]
        mean = sum(r["score"] for r in auto) / max(1, len(auto))
        rhits = [r["retrieval_hit"] for r in rows if r["retrieval_hit"] is not None]
        rhit = sum(1 for x in rhits if x) / max(1, len(rhits))
        sibs = [r.get("retrieval_hit_sib") for r in rows if r.get("retrieval_hit_sib") is not None]
        sibhit = (sum(1 for x in sibs if x) / len(sibs)) if sibs else None
        avgt = sum(r["secs"] for r in rows) / max(1, len(rows))
        # Family A: keyword-scored factual types. Family B: judge-scored reasoning.
        fa = [r for r in auto if r["scorer"] != "judge"]
        fb = [r for r in rows if r["scorer"] == "judge" and r["score"] is not None]
        fa_mean = sum(r["score"] for r in fa) / max(1, len(fa))
        lines.append(f"\n### {go['lang'].upper()}  ({go['variant']})")
        lines.append(f"  Family A score  : {fa_mean:.3f}  over {len(fa)} q   (auto overall {mean:.3f}/{len(auto)})")
        sibtxt = f"   (sibling-aware {sibhit:.3f})" if sibhit is not None else ""
        lines.append(f"  retrieval hit   : {rhit:.3f}  over {len(rhits)} q   avg {avgt:.1f}s/q{sibtxt}")
        for t in ["statutes", "privilege", "parties", "doctype"]:
            rs = [r["score"] for r in rows if r["scorer"] == t and r["score"] is not None]
            hs = [r["retrieval_hit"] for r in rows if r["scorer"] == t and r["retrieval_hit"] is not None]
            ss = [r.get("retrieval_hit_sib") for r in rows if r["scorer"] == t and r.get("retrieval_hit_sib") is not None]
            if rs:
                hr = (sum(1 for x in hs if x)/len(hs)) if hs else float("nan")
                sr = (sum(1 for x in ss if x)/len(ss)) if ss else None
                st = f" sib {sr:.3f}" if sr is not None else ""
                lines.append(f"    {t:<10} score {sum(rs)/len(rs):.3f}  retr {hr:.3f}{st}  (n={len(rs)})")
        if fb:
            fb_mean = sum(r["score"] for r in fb) / len(fb)
            lines.append(f"  Family B (judge): {fb_mean:.3f}  over {len(fb)} q")
            if JUDGE_RUNS < 3:
                lines.append(f"    ⚠ judged with --judge-runs {JUDGE_RUNS} (skill recommends 3; "
                             f"single-pass judging is noisy for the seeded n=1 subtypes)")
            sub = {}
            for r in fb:
                sub.setdefault(r["type"].replace("reasoning:", ""), []).append(r["score"])
            for st in sorted(sub):
                v = sub[st]
                lines.append(f"    {st:<18} score {sum(v)/len(v):.3f}  (n={len(v)})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--model", default=None)
    ap.add_argument("--only", default=None, help="nl|en to run a single language")
    ap.add_argument("--variant", default=None,
                    help="run only this variant (e.g. nl-fulllaw) — excludes the others")
    ap.add_argument("--judge-model", default=None,
                    help="model for reasoning LLM-as-judge (default: wiki_model)")
    ap.add_argument("--filter", default=None,
                    help="run only questions whose type contains this substring (e.g. reasoning)")
    ap.add_argument("--skip-index", action="store_true",
                    help="skip reindex (use existing index)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap questions PER TYPE (stratified sample) for a cheap probe; 0=all")
    ap.add_argument("--judge-runs", type=int, default=1,
                    help="median of N judge passes for reasoning (stabilizes n=1 subtypes)")
    args = ap.parse_args()

    settings = {}
    try:
        settings = requests.get(f"{API}/settings", headers=H, timeout=30).json()
    except Exception as e:
        log(f"warn: could not fetch /settings ({e}); falling back to defaults")
    model = args.model or settings.get("chat_model") or "gemma4:e4b"
    # Judge with a stronger local model than the answerer (wiki_model), so the
    # grader is not the model being graded. Ollama-only, fully offline.
    global JUDGE_MODEL, JUDGE_RUNS
    JUDGE_RUNS = max(1, args.judge_runs)
    JUDGE_MODEL = (args.judge_model or os.getenv("RAG_JUDGE_MODEL")
                   or settings.get("wiki_model") or model)
    log(f"judge_model={JUDGE_MODEL}")
    OUTDIR.mkdir(parents=True, exist_ok=True)

    groups = [g for g in GROUPS if (not args.only or g["lang"] == args.only)
              and (not args.variant or g["variant"] == args.variant)]
    log(f"model={model} top_k={args.top_k} groups={[g['variant'] for g in groups]}")
    t0 = time.time()
    out = []
    for grp in groups:
        out.append(run_group(grp, model, args.top_k, filt=args.filter,
                              skip_index=args.skip_index, limit=args.limit))
        # write incrementally so a crash still leaves partial data
        (OUTDIR / f"selfheal_{args.tag}.json").write_text(
            json.dumps({"tag": args.tag, "model": model, "top_k": args.top_k, "groups": out},
                       ensure_ascii=False, indent=2))
    summary = summarize(out, model, args.top_k, args.tag, time.time() - t0)
    (OUTDIR / f"selfheal_{args.tag}.txt").write_text(summary)
    log("\n" + summary)
    log(f"\nsaved -> {OUTDIR / f'selfheal_{args.tag}.json'}")
    log(f"summary -> {OUTDIR / f'selfheal_{args.tag}.txt'}")


if __name__ == "__main__":
    main()
