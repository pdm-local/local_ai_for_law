"""Deprecated education endpoints removed from the pure legal edition.

These are 410-Gone stubs kept only so old cached clients get a clear status instead
of a 404. They are excluded from the OpenAPI schema (`include_in_schema=False`): they
are not part of the real API, and listing the multi-method `api_route` stubs in the
schema produced duplicate operation-id warnings on every startup.
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api", tags=["deprecated"], include_in_schema=False)


def _gone():
    raise HTTPException(
        status_code=410,
        detail="Study, quiz, flashcard, progress, and study-bank features were removed from the pure legal edition.",
    )


@router.api_route("/study-bank", methods=["GET", "POST", "DELETE"])
async def study_bank_root():
    """410 — study-bank was removed from the pure legal edition."""
    _gone()


@router.api_route("/study-bank/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def study_bank_removed(path: str):
    """410 — study-bank sub-paths were removed."""
    _gone()


@router.api_route("/wiki/generate-from-bank", methods=["POST"])
async def wiki_generate_from_bank_removed():
    """410 — wiki generate-from-bank was removed."""
    _gone()


@router.api_route("/graph/build-from-bank", methods=["POST"])
async def graph_build_from_bank_removed():
    """410 — graph build-from-bank was removed."""
    _gone()


@router.api_route("/study/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def study_removed(path: str):
    """410 — study features were removed."""
    _gone()


@router.api_route("/progress/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def progress_removed(path: str):
    """410 — progress tracking was removed."""
    _gone()


@router.api_route("/flashcards/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def flashcards_removed(path: str):
    """410 — flashcards were removed."""
    _gone()


@router.api_route("/groups/{group_id}/flashcards", methods=["GET", "POST", "DELETE"])
async def group_flashcards_root(group_id: str):
    """410 — per-group flashcards were removed."""
    _gone()


@router.api_route("/groups/{group_id}/flashcards/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def group_flashcards_removed(group_id: str, path: str):
    """410 — per-group flashcard sub-paths were removed."""
    _gone()


@router.api_route("/groups/{group_id}/quiz", methods=["GET", "POST", "DELETE"])
async def group_quiz_root(group_id: str):
    """410 — per-group quiz was removed."""
    _gone()


@router.api_route("/groups/{group_id}/quiz/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def group_quiz_removed(group_id: str, path: str):
    """410 — per-group quiz sub-paths were removed."""
    _gone()


@router.api_route("/groups/{group_id}/bank-quiz", methods=["GET", "POST", "DELETE"])
async def group_bank_quiz_removed(group_id: str):
    """410 — per-group bank-quiz was removed."""
    _gone()
