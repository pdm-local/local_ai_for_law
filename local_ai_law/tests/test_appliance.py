"""Container packaging guarantees — no important file may ship, and both images
must lay the code out so the app actually imports.

The appliance is `docker save`-ed and handed to clients, so `COPY . .` must never
bake in a secret, a credential store, prior tenant data, or an internal-only
document. Both images must also mirror the `<root>/local_ai_law` package layout
(app.py imports itself as that package) and drop root. These are static checks over
the build-context filters and the two Dockerfiles — no Docker daemon needed, so they
run in the normal offline suite and FAIL LOUD if a future edit reopens a leak or the
import-breaking `COPY . .` layout.

The end-to-end proof (build the image, list /app, assert none of these exist) is
scripts/verify.sh --image; this test is the fast regression guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEV_IGNORE = ROOT / ".dockerignore"
APP_IGNORE = ROOT / "Dockerfile.appliance.dockerignore"
APP_DOCKERFILE = ROOT / "Dockerfile.appliance"
DEV_DOCKERFILE = ROOT / "Dockerfile"
ENTRYPOINT = ROOT / "deploy" / "appliance-entrypoint.sh"

# Anything on this list must be excluded from BOTH build contexts. A trailing "/"
# is normalised away before comparison so "certs/" and "certs" both match.
MUST_BE_IGNORED = {
    # secrets / per-instance credentials
    ".env", ".jwt_secret", "users.json", "groups.json", "settings.json",
    "secrets.json", "audit.log", "certs", "backup_bundle",
    # runtime tenant data
    "data", "rag_db", "uploads", "chat_history", "overviews",
    "doc_summaries", "flashcards",
    # internal-only documents
    "businessplan.md", "CLAUDE.md", "IMPROVEMENTS.md",
}


def _patterns(path: Path) -> set[str]:
    """Non-comment, non-negation patterns from a .dockerignore, trailing / stripped."""
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        out.add(line.rstrip("/"))
    return out


def test_both_ignore_files_exist():
    assert DEV_IGNORE.is_file()
    assert APP_IGNORE.is_file()


@pytest.mark.parametrize("ignore_file", [DEV_IGNORE, APP_IGNORE], ids=["dev", "appliance"])
def test_no_important_file_ships(ignore_file):
    patterns = _patterns(ignore_file)
    missing = sorted(p for p in MUST_BE_IGNORED if p not in patterns)
    assert not missing, f"{ignore_file.name} would ship: {missing}"


def test_git_is_never_in_the_context():
    # .git holds the full history (incl. the committed encrypted secrets bundle);
    # it must never be copied into an image.
    assert ".git" in _patterns(DEV_IGNORE)
    assert ".git" in _patterns(APP_IGNORE)


def test_model_bundle_policy_differs_by_image():
    # Dev image talks to an external Ollama → exclude the heavy bundle.
    assert "deploy/ollama-bundle" in _patterns(DEV_IGNORE)
    # Appliance bakes the weights in → the bundle MUST be present in the context.
    assert "deploy/ollama-bundle" not in _patterns(APP_IGNORE)


def test_appliance_runs_non_root():
    text = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert "USER appuser" in text, "web process must drop root"
    # The last USER directive before ENTRYPOINT must be the unprivileged one.
    assert text.rindex("USER appuser") < text.rindex("ENTRYPOINT")


def test_entrypoint_starts_ollama_then_app():
    assert ENTRYPOINT.is_file()
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "ollama serve" in text
    assert "gunicorn" in text
    # secret is generated at runtime into the data volume, never baked in
    assert ".jwt_secret" in text


def _dockerfile_instructions(path: Path) -> str:
    """Dockerfile with comment-only lines stripped (so prose can't match a check)."""
    return "\n".join(
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )


def test_appliance_uses_explicit_copies_not_copy_dot():
    # `COPY . .` would drag the 16 GB model bundle into /app (baking the weights
    # TWICE) AND re-introduce every internal/secret file the dockerignore misses.
    # The appliance must copy runtime dirs explicitly instead.
    instr = _dockerfile_instructions(APP_DOCKERFILE)
    assert "COPY . ." not in instr, "COPY . . re-opens the dup + leak; list runtime dirs explicitly"
    text = APP_DOCKERFILE.read_text(encoding="utf-8")
    for needed in ("app.py", "routes/", "services/", "static/"):
        assert needed in text, f"runtime path {needed} not copied into the image"
    # frontend/ is just the Vite source (the built UI ships in static/) — never bake it.
    assert "COPY --chown=appuser:appuser frontend/" not in text


def test_appliance_layout_resolves_package_imports():
    # app.py imports `local_ai_law.*` after putting its parent on sys.path,
    # so the code MUST live at <root>/local_ai_law or the app won't import.
    text = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert "WORKDIR /app/local_ai_law" in text


def test_model_store_copied_once_no_metadata_rewrite():
    # A `chmod -R`/`chown -R` over /models rewrites every blob into a NEW layer,
    # duplicating the multi-GB store. Ownership must be set inline at COPY time.
    instr = _dockerfile_instructions(APP_DOCKERFILE)
    assert "COPY --chown=appuser:appuser deploy/ollama-bundle/ /models/" in instr
    assert "chmod -R" not in instr, "recursive chmod duplicates the model store into a new layer"
    assert "chown -R appuser:appuser /models" not in instr


def test_appliance_does_not_ship_scripts():
    # scripts/ is operator tooling (build/verify/bench/eval) that the running app never
    # imports — baking it into a CLIENT image is bloat + internal-tooling leakage.
    instr = _dockerfile_instructions(APP_DOCKERFILE)
    assert "scripts/" not in instr, "appliance must not COPY the internal scripts/ dir"


# ── The DEV image (Dockerfile) shares the same import-layout + hardening contract ────
def test_dev_dockerfile_layout_resolves_package_imports():
    # Same trap as the appliance: app.py imports `local_ai_law.*` off its
    # parent, so the dev image must lay the code at /app/local_ai_law too —
    # a plain `WORKDIR /app` + `COPY . .` makes every package import fail at runtime.
    assert DEV_DOCKERFILE.is_file()
    text = DEV_DOCKERFILE.read_text(encoding="utf-8")
    assert "WORKDIR /app/local_ai_law" in text


def test_dev_dockerfile_uses_explicit_copies_not_copy_dot():
    instr = _dockerfile_instructions(DEV_DOCKERFILE)
    assert "COPY . ." not in instr, "dev image must list runtime dirs, not COPY . . (leak risk)"
    for needed in ("app.py", "routes/", "services/", "static/"):
        assert needed in instr, f"runtime path {needed} not copied into the dev image"


def test_dev_dockerfile_runs_non_root():
    text = DEV_DOCKERFILE.read_text(encoding="utf-8")
    assert "USER appuser" in text, "dev web process must drop root"
    assert text.rindex("USER appuser") < text.rindex("CMD")
