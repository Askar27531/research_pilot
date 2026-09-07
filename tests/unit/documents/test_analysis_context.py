"""Tests for the multimodal context used by the vision pipeline (A1)."""

from app.documents.analysis import _observation_context
from app.schemas import FigureMention


def test_context_carries_caption_and_prose_mentions() -> None:
    mentions = [
        FigureMention(page_number=2, sentence="We outperform baselines, as shown in Fig. 3."),
        FigureMention(page_number=2, sentence="Fig. 3 again supports the same trend."),
    ]

    content = _observation_context("figure", "Fig. 3 Results.", "Fig. 3", mentions)

    assert "Region: figure" in content
    assert "Fig. 3 Results." in content
    assert "(p2)" in content
    assert "as shown in Fig. 3" in content
    assert "Prose mentions of this visual" in content


def test_context_falls_back_without_mentions() -> None:
    content = _observation_context("table", "Table 2 Experimental results.", "Table 2", None)

    assert "Table 2 Experimental results." in content
    assert "(p" not in content
    assert "Region: table" in content
    assert "Prose mentions" not in content
