#!/usr/bin/env python3
"""
selfcheck.py — one-command health audit for the local_ai_law tree.

Run it from the repo root; it points you at problems and possible problems across
the whole codebase WITHOUT touching Ollama, the GPU, or any live user data:

    PYTHONPATH=<repo-parent> venv/bin/python scripts/selfcheck.py
    ...                                                scripts/selfcheck.py --strict   # WARN counts as failure
    ...                                                scripts/selfcheck.py --json      # machine-readable

What it verifies (each finding is FAIL = real defect, or WARN = smell):
  1. Static / structural — every module compiles, `from app import app` works, and an
     AST sweep reports docstring ("notation") coverage + coding-skill smells.
  2. Settings triangle — config.DEFAULT_SETTINGS <-> models.SettingsIn must agree
     (a CLAUDE.md "never violate" invariant; drift ships broken settings).
  3. Endpoint coverage — enumerate EVERY route from the OpenAPI schema and prove each
     /api endpoint is auth-gated (401 without a token) and none returns 5xx. The
     middleware rejects before the handler runs, so this never calls a model.
  4. Contracts — re-runs the existing security_smoke.py checks (settings/backup/path/
     contracts) so they live behind the same single command.

Exit code is 1 if any FAIL (or, with --strict, any WARN); else 0. Offline, GPU-free,
Ollama-free by construction — safe to run anytime, including in CI.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys
import tempfile
import threading
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]                 # .../local_ai_law
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_PKG_PARENT = str(Path(__file__).resolve().parents[2])     # parent so local_ai_law.* resolves
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"


class Report:
    """Collects (level, section, code, msg) findings and renders them grouped."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, str]] = []

    def add(self, level: str, section: str, code: str, msg: str) -> None:
        self.items.append((level, section, code, msg))

    def fail(self, section: str, code: str, msg: str) -> None:
        self.add(FAIL, section, code, msg)

    def warn(self, section: str, code: str, msg: str) -> None:
        self.add(WARN, section, code, msg)

    def info(self, section: str, code: str, msg: str) -> None:
        self.add(INFO, section, code, msg)

    def count(self, level: str) -> int:
        return sum(1 for it in self.items if it[0] == level)

    def render_text(self) -> str:
        order = {FAIL: 0, WARN: 1, INFO: 2}
        icon = {FAIL: "✗", WARN: "!", INFO: "·"}
        lines: list[str] = []
        for sec in dict.fromkeys(s for _, s, _, _ in self.items):
            rows = [it for it in self.items if it[1] == sec]
            rows.sort(key=lambda it: order[it[0]])
            lines.append(f"\n── {sec} ──")
            for level, _, code, msg in rows:
                lines.append(f"  {icon[level]} {level:4} [{code}] {msg}")
        lines.append("\n" + "=" * 60)
        lines.append(f"  FAIL={self.count(FAIL)}  WARN={self.count(WARN)}  INFO={self.count(INFO)}")
        return "\n".join(lines)

    def render_json(self) -> str:
        return json.dumps({
            "summary": {"fail": self.count(FAIL), "warn": self.count(WARN), "info": self.count(INFO)},
            "findings": [{"level": l, "section": s, "code": c, "message": m} for l, s, c, m in self.items],
        }, indent=2)


# ── 1. Static / structural ──────────────────────────────────────────────────────

_MODEL_LITERAL = re.compile(r"\b(gemma|qwen|llama|mistral|deepseek|nomic|bge|mxbai)[\w.\-]*:[\w.\-]+")
_TODO = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")


def _public_funcs(tree: ast.AST):
    """Yield top-level public functions/classes (name not starting with '_')."""
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            yield node


def check_static(rep: Report) -> None:
    section = "static"
    import compileall
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        ok = compileall.compile_dir(str(ROOT / "services"), quiet=2, force=False)
        ok &= compileall.compile_dir(str(ROOT / "routes"), quiet=2, force=False)
        ok &= compileall.compile_file(str(ROOT / "app.py"), quiet=2, force=False)
    if ok:
        rep.info(section, "compile", "services/ routes/ app.py compile clean")
    else:
        rep.fail(section, "compile", "syntax error — see: python -m compileall services routes app.py")

    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            import app as _app  # noqa: F401
        rep.info(section, "import", "from app import app  → OK")
    except Exception as e:
        rep.fail(section, "import", f"`from app import app` failed: {e!r}")

    # AST sweep over services/ + routes/ — docstrings ("notations") + coding smells.
    for sub in ("services", "routes"):
        for py in sorted((ROOT / sub).glob("*.py")):
            if py.name == "__init__.py":
                continue
            rel = py.relative_to(ROOT).as_posix()
            try:
                src = py.read_text(encoding="utf-8")
                tree = ast.parse(src, filename=str(py))
            except Exception as e:
                rep.fail(section, "parse", f"{rel}: {e!r}")
                continue

            # Docstring coverage.
            if ast.get_docstring(tree) is None:
                rep.warn(section, "doc.module", f"{rel}: no module docstring")
            pubs = list(_public_funcs(tree))
            undoc = [n.name for n in pubs if ast.get_docstring(n) is None]
            if pubs:
                ratio = 1 - len(undoc) / len(pubs)
                if ratio < 0.5:
                    rep.warn(section, "doc.func",
                             f"{rel}: {len(undoc)}/{len(pubs)} public defs undocumented ({ratio:.0%} covered)")

            # Coding-skill smells.
            for node in ast.walk(tree):
                # broad `except Exception: pass`
                if isinstance(node, ast.ExceptHandler):
                    is_broad = node.type is None or (isinstance(node.type, ast.Name) and node.type.id == "Exception")
                    if is_broad and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                        rep.warn(section, "lint.except",
                                 f"{rel}:{node.lineno}: silent `except: pass` (log or narrow it)")
                # print() in library code
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "print"):
                    rep.warn(section, "lint.print", f"{rel}:{node.lineno}: print() in library code (use log)")
                # wildcard import
                if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                    rep.warn(section, "lint.import", f"{rel}:{node.lineno}: wildcard import")
                # mutable default arg
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in node.args.defaults + node.args.kw_defaults:
                        if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                            rep.warn(section, "lint.mutdefault",
                                     f"{rel}:{node.lineno}: mutable default arg in {node.name}()")
                # hardcoded model tag (CLAUDE.md: never hardcode model names outside config)
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if _MODEL_LITERAL.search(node.value):
                        rep.warn(section, "lint.model",
                                 f"{rel}: hardcoded model tag {node.value!r} (load from settings)")

            # raw-text scans (comments aren't in the AST). Only flag TODO/FIXME in a
            # comment, not inside strings/regexes (those can be legitimate content).
            for i, line in enumerate(src.splitlines(), 1):
                comment = line.split("#", 1)[1] if "#" in line else ""
                if comment and _TODO.search(comment):
                    rep.warn(section, "lint.todo", f"{rel}:{i}: {line.strip()[:70]}")


# ── 2. Settings triangle ────────────────────────────────────────────────────────

def check_settings_triangle(rep: Report) -> None:
    section = "settings"
    try:
        from local_ai_law.config import DEFAULT_SETTINGS
        from local_ai_law.models import SettingsIn
    except Exception as e:
        rep.fail(section, "import", f"could not import DEFAULT_SETTINGS/SettingsIn: {e!r}")
        return
    default_keys = set(DEFAULT_SETTINGS)
    model_keys = set(SettingsIn.model_fields)
    missing_in_model = default_keys - model_keys
    missing_in_default = model_keys - default_keys
    if missing_in_model:
        rep.fail(section, "triangle",
                 f"in DEFAULT_SETTINGS but not SettingsIn (unsettable): {sorted(missing_in_model)}")
    if missing_in_default:
        rep.fail(section, "triangle",
                 f"in SettingsIn but not DEFAULT_SETTINGS (no default): {sorted(missing_in_default)}")
    if not missing_in_model and not missing_in_default:
        rep.info(section, "triangle", f"DEFAULT_SETTINGS ↔ SettingsIn agree ({len(default_keys)} keys)")

    # Edition presets may only override settings that actually exist. An edition
    # default aimed at a renamed/removed key silently never applies → real bug.
    try:
        from local_ai_law.services.editions import EDITIONS
        for ekey, ed in EDITIONS.items():
            unknown = set(getattr(ed, "default_settings", {})) - default_keys
            if unknown:
                rep.fail(section, "edition-default",
                         f"edition '{ekey}' default_settings target unknown keys: {sorted(unknown)}")
    except Exception as e:
        rep.warn(section, "edition-default", f"could not check edition defaults: {e!r}")

    # Docs-and-settings contract: every setting wired into the settings UI
    # (saveSettings body) should be mentioned in README.md. WARN-only — advanced /
    # env-only knobs may be omitted deliberately, but a user-facing setting with no
    # docs is a smell the "code + docs travel together" rule is meant to catch.
    try:
        readme = (ROOT / "README.md").read_text(encoding="utf-8", errors="ignore")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"function\s+saveSettings\s*\([^)]*\)\s*\{.*?body\s*=\s*\{(.*?)\};",
                      html, re.S)
        if m:
            body_keys = set(re.findall(r"(\w+)\s*:", m.group(1)))
            ui_settings = body_keys & (default_keys | model_keys)  # drop UI-only prefs
            undocumented = sorted(k for k in ui_settings if k not in readme)
            if undocumented:
                rep.warn(section, "readme",
                         f"UI settings not mentioned in README.md: {undocumented}")
            else:
                rep.info(section, "readme",
                         f"all {len(ui_settings)} UI settings documented in README")
    except Exception as e:
        rep.warn(section, "readme", f"could not check README settings coverage: {e!r}")


# ── 3. Endpoint coverage (auth-gate only, GPU-free) ─────────────────────────────

def _audit_endpoints(rep: Report, client, paths: dict, public_paths: set, public_prefixes: tuple) -> None:
    """Hit every parameter-free endpoint WITHOUT a token. Non-public /api must 401;
    public must not; nothing may 5xx. The middleware rejects before the handler, so
    no model/GPU code runs here."""
    section = "endpoints"

    def is_public(p: str) -> bool:
        return p in public_paths or any(p.startswith(pre) for pre in public_prefixes)

    tested = gated = skipped = 0
    for path, methods in sorted(paths.items()):
        if "{" in path:  # path params — skip (would need fixtures); covered by the introspection pass
            continue
        for method in methods:
            m = method.upper()
            if m not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                continue
            # Skip public endpoints entirely: executing their handlers can do external
            # I/O (e.g. /api/health reaches Ollama) and this auditor must stay offline
            # and never hang. Auth-gate coverage comes from the non-public probes —
            # which the middleware rejects with 401 BEFORE the handler runs, so they
            # touch no model and no I/O.
            if is_public(path):
                skipped += 1
                continue
            try:
                resp = client.request(m, path)
            except Exception as e:
                rep.fail(section, "5xx", f"{m} {path}: raised {e!r}")
                continue
            tested += 1
            if resp.status_code >= 500:
                rep.fail(section, "5xx", f"{m} {path}: server error {resp.status_code} (reachable pre-auth)")
                continue
            if path.startswith("/api/"):
                if resp.status_code in (401, 423):  # 423 = instance locked, also a gate
                    gated += 1
                else:
                    rep.fail(section, "ungated",
                             f"{m} {path}: expected 401 without token, got {resp.status_code} "
                             f"(endpoint not auth-gated)")
    rep.info(section, "coverage",
             f"probed {tested} non-public endpoints ({gated} auth-gated); skipped {skipped} public (offline-safe)")


def _introspect_auth(rep: Report) -> None:
    """Every /api route handler should declare an auth dependency. Belt-and-braces
    next to the live 401 check (catches param'd routes the live probe skips)."""
    section = "endpoints"
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from app import app
        from fastapi.routing import APIRoute
    except Exception as e:
        rep.warn(section, "introspect", f"could not introspect routes: {e!r}")
        return
    AUTH_DEPS = {"get_current_username", "require_auth", "get_current_user", "require_admin"}
    public = {"/", "/api/auth/login", "/api/auth/register", "/api/auth/status",
              "/api/health", "/api/setup/status", "/api/setup/editions",
              "/api/setup/complete", "/api/setup/unlock", "/api/meta"}
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if not path.startswith("/api/") or path in public or path.startswith("/api/auth/"):
            continue
        src = ""
        try:
            import inspect
            src = inspect.getsource(route.endpoint)
        except Exception:
            pass
        if not any(dep in src for dep in AUTH_DEPS):
            missing.append(f"{sorted(route.methods)} {path}")
    if missing:
        for m in missing:
            rep.warn(section, "noauth-dep", f"{m}: handler declares no auth dependency (verify gating)")
    else:
        rep.info(section, "introspect", "all /api handlers declare an auth dependency")


def check_endpoints(rep: Report) -> None:
    section = "endpoints"
    # Isolate data + seed a user so the auth middleware is ACTIVE (it skips auth while
    # user_count()==0). Without this the 401 probe is meaningless.
    tmp = tempfile.mkdtemp(prefix="selfcheck_rag_")
    os.environ["RAG_DATA_DIR"] = tmp
    try:
        from fastapi.testclient import TestClient
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from app import app, _PUBLIC_PATHS, _PUBLIC_PREFIXES
            from local_ai_law.services.auth import register_user, user_count
            register_user("selfcheck_probe", "Probe-pass-1", role="admin")
    except Exception as e:
        rep.fail(section, "setup", f"could not build TestClient / seed user: {e!r}")
        return
    if user_count() == 0:
        rep.fail(section, "setup", "user seed failed — auth gate would be inactive, 401 probe invalid")
        return

    paths = app.openapi().get("paths", {})
    rep.info(section, "count", f"OpenAPI exposes {len(paths)} route paths")
    if len(paths) < 80:
        rep.warn(section, "count", f"only {len(paths)} paths — expected ~96; routers may be unregistered")

    # 127.0.0.1: the front door (services/guard.py) refuses TestClient's default "testserver".
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    _audit_endpoints(rep, client, paths, set(_PUBLIC_PATHS), tuple(_PUBLIC_PREFIXES))
    _introspect_auth(rep)


# ── UI integrity (offline: handlers defined, API calls resolve, orphans) ────────

def check_ui(rep: Report) -> None:
    """Static UI audit of static/index.html: every on*-handler is defined (no dead
    buttons), every /api path the UI calls maps to a real route (no 404 buttons like
    the /report bug), and functions wired to nothing are WARNed. Cross-bundle window.*
    calls from the React chat (static/chat/chat.js, frontend/src) count as references,
    so a global like showSrc isn't a false orphan."""
    section = "ui"
    static_dir = ROOT / "static"
    index = static_dir / "index.html"
    if not index.exists():
        rep.warn(section, "missing", "static/index.html not found")
        return
    html = index.read_text(encoding="utf-8", errors="ignore")

    # cross-bundle reference text: the built chat bundle, its React source, AND the
    # extracted static/js/*.js modules — so a global defined in index.html but called
    # only from a module (or the reverse) is not flagged as a false orphan / dead button.
    cross = ""
    chatjs = static_dir / "chat" / "chat.js"
    if chatjs.exists():
        cross += chatjs.read_text(encoding="utf-8", errors="ignore")
    fe = ROOT / "frontend" / "src"
    if fe.exists():
        for p in fe.rglob("*.ts*"):
            cross += p.read_text(encoding="utf-8", errors="ignore")
    jssrc = ""
    for p in sorted((static_dir / "js").glob("*.js")):
        jssrc += "\n" + p.read_text(encoding="utf-8", errors="ignore")
    cross += jssrc

    # 1. every on*-handler name resolves to a definition — in index.html OR in a
    # static/js/*.js module the SPA has been split into.
    refs = set(re.findall(r'on(?:click|change|input|submit|keyup|keydown|focus|blur)='
                          r'"([a-zA-Z_$][\w$]*)\s*\(', html))
    defs = set(re.findall(r'function\s+([a-zA-Z_$][\w$]*)\s*\(', html))
    defs |= set(re.findall(r'(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()', html))
    jsdefs = set(re.findall(r'function\s+([a-zA-Z_$][\w$]*)\s*\(', jssrc))
    jsdefs |= set(re.findall(r'(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()', jssrc))
    keywords = {"if", "for", "while", "switch", "return", "event", "document", "window", "this"}
    for r in sorted(refs):
        if r not in defs and r not in jsdefs and r not in keywords:
            rep.fail(section, "handler", f"on*-handler '{r}()' is not defined — dead button")

    # 2. every /api path the UI calls maps to a real backend route.
    # Scan index.html PLUS the other hand-written UI sources — the finder page, the
    # service worker, and the static/js/*.js modules the SPA is being split into — so
    # an extracted module's calls stay covered. The minified chat bundle and frontend/src
    # are excluded here (dynamic path building would cause false 404 findings); they
    # still count as references for the orphan check above via `cross`.
    ui_src = html
    for extra in [static_dir / "finder.html", static_dir / "sw.js",
                  *sorted((static_dir / "js").glob("*.js"))]:
        if extra.exists():
            ui_src += "\n" + extra.read_text(encoding="utf-8", errors="ignore")
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from app import app
        routes = {re.sub(r"\{[^}]+\}", "{}", p).rstrip("/") for p in app.openapi().get("paths", {})}
    except Exception as e:
        rep.warn(section, "routes", f"could not load route set: {e!r}")
        routes = None
    if routes is not None:
        # collapse single-level ${...} template interpolations to {}, drop query strings
        flat = re.sub(r"\$\{[^{}]*\}", "{}", ui_src)
        ui_paths = set()
        for m in re.finditer(r"/api/[^\s'\"`)]+", flat):
            p = m.group(0).split("?")[0].rstrip("/")
            p = re.sub(r"\{\}(?:\.\w+)*", "{}", p)         # tidy leftover member access
            ui_paths.add(p)
        for p in sorted(ui_paths):
            if p not in routes and not any(p == r or p.startswith(r + "/") for r in routes):
                rep.fail(section, "deadcall", f"UI calls {p} but no backend route matches (404 button)")

    # 3. functions defined but referenced nowhere (here or cross-bundle) → WARN
    orphans = []
    for name in sorted(defs):
        if len(re.findall(r"\b" + re.escape(name) + r"\b", html)) <= 1 and name not in cross:
            orphans.append(name)
    for o in orphans:
        rep.warn(section, "orphan", f"function {o}() is defined but never referenced (dead or unwired)")
    if not orphans:
        rep.info(section, "orphan", "no orphaned UI functions")
    rep.info(section, "coverage", f"{len(refs)} handlers, {len(defs)} defs checked in index.html")


# ── 4. Contracts (reuse security_smoke) ─────────────────────────────────────────

def check_contracts(rep: Report) -> None:
    section = "contracts"
    os.environ.setdefault("RAG_DATA_DIR", tempfile.mkdtemp(prefix="selfcheck_contracts_"))
    try:
        from local_ai_law.scripts import security_smoke as ss
    except Exception as e:
        rep.fail(section, "import", f"could not import security_smoke: {e!r}")
        return
    checks = [
        ("settings_contract", ss.check_settings_contract),
        ("backup_validation", ss.check_backup_validation),
        ("path_validation", ss.check_path_validation),
        ("app_import", ss.check_app_import),
        ("api_contracts", ss.check_api_contracts),
    ]
    for name, fn in checks:
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                fn()
            rep.info(section, name, "ok")
        except Exception as e:
            rep.fail(section, name, f"{e!r}")


# ── self-test: prove the endpoint auditor actually CATCHES problems ──────────────

def run_self_test() -> int:
    """Build a throwaway app with a deliberately ungated /api route and a 5xx route;
    assert the auditor flags both. Confirms the gate logic isn't a no-op."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    bad = FastAPI()

    @bad.get("/api/leak")           # ungated — must be caught
    async def _leak():
        return {"secret": 1}

    @bad.get("/api/boom")           # 5xx — must be caught
    async def _boom():
        raise RuntimeError("kaboom")

    @bad.get("/api/auth/login")     # public — must NOT be flagged
    async def _login():
        return {"ok": True}

    rep = Report()
    client = TestClient(bad, raise_server_exceptions=False)
    paths = bad.openapi().get("paths", {})
    _audit_endpoints(rep, client, paths, {"/api/auth/login"}, ("/api/auth/",))
    codes = {c for lvl, _, c, _ in rep.items if lvl == FAIL}
    ok = "ungated" in codes and "5xx" in codes
    print("self-test:", "PASS" if ok else "FAIL", "— caught:", sorted(codes))
    return 0 if ok else 1


def _timeboxed(rep: Report, section: str, fn, seconds: int) -> None:
    """Run fn in a daemon thread; if it overruns, record a WARN and move on rather
    than hanging the whole audit (the abandoned thread dies with the process)."""
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        rep.warn(section, "timeout",
                 f"{section} exceeded {seconds}s and was skipped — app or Ollama slow/unreachable?")


def main() -> int:
    ap = argparse.ArgumentParser(description="Health audit for the local_ai_law tree (offline, GPU-free).")
    ap.add_argument("--strict", action="store_true", help="treat WARN as failure (nonzero exit)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--timeout", type=int, default=45, help="per-section seconds before skipping (default 45)")
    ap.add_argument("--self-test", action="store_true", help="prove the endpoint auditor catches problems, then exit")
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()

    rep = Report()
    check_static(rep)
    check_settings_triangle(rep)
    check_ui(rep)
    # The app-touching sections are timeboxed: if the app or Ollama is unreachable a
    # check could otherwise block, but this auditor must never hang. A daemon thread
    # that overruns is abandoned (it dies with the process) and reported as a WARN.
    _timeboxed(rep, "endpoints", lambda: check_endpoints(rep), args.timeout)
    _timeboxed(rep, "contracts", lambda: check_contracts(rep), args.timeout)

    print(rep.render_json() if args.json else rep.render_text())
    if rep.count(FAIL) or (args.strict and rep.count(WARN)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
