"""The chat's citation post-check accepts the forms the models actually write."""
import pytest

from local_ai_law.services.prompting import has_citation


@pytest.mark.parametrize("text", [
    "as agreed (contract.pdf, p. 3).",
    "see [doc: meridiaan-0116-memo-001.md | Page: 2]",
    "[doc: meridiaan-0108-contract-001.md p.2 | score 0.74]",
    "zie (brief.pdf, pagina 2)",
    "Memo.DOCX page 12",
])
def test_cites(text):
    assert has_citation(text)


@pytest.mark.parametrize("text", [
    "The documents do not say.",
    "contract.pdf",
    "page 3 of the contract",
])
def test_no_citation(text):
    assert not has_citation(text)
