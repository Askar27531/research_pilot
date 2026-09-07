"""Tests for parser-side prose-mention indexing of figures and tables (B1)."""

from app.documents.parser import (
    MAX_MENTIONS_PER_VISUAL,
    _assign_visual_mentions,
    _collect_mention_hits,
    _visual_key,
)
from app.schemas import BoundingBox, DocumentFigure, DocumentPage, TableCandidate


def _figure(identifier: str, label: str, page: int,
            caption: str | None = None) -> DocumentFigure:
    return DocumentFigure(
        figure_id=identifier, document_id="doc", page_number=page, label=label,
        caption=caption, figure_type="result",
        bbox=BoundingBox(x0=0, y0=0, x1=10, y1=10),
        source_path=f"figures/{identifier}.png", sha256="a" * 64,
    )


def _table(identifier: str, label: str, page: int) -> TableCandidate:
    return TableCandidate(
        table_id=identifier, document_id="doc", page_number=page, label=label, cells=[],
    )


def _page(number: int, text: str) -> DocumentPage:
    return DocumentPage(
        document_id="doc", page_number=number, text=text,
        width=600, height=800, rotation=0,
    )


def test_visual_key_normalizes_labels() -> None:
    assert _visual_key("Fig. 3") == "3"
    assert _visual_key("Figure 12b") == "12b"
    assert _visual_key("table 2") == "2"
    assert _visual_key(None) is None
    assert _visual_key("Some random text") is None


def test_prose_mentions_are_attached_by_label_key() -> None:
    figures = [_figure("f1", "Fig. 3", page=5)]
    tables = [_table("t1", "Table 2", page=8)]
    pages = [
        _page(2, "Our method improves accuracy, as shown in Fig. 3 against baselines."),
        _page(7, "Table 2 reports the per-dataset results."),
        _page(9, "No visual is referenced in this sentence."),
    ]

    hits = _collect_mention_hits(pages)
    _assign_visual_mentions(figures, tables, hits)

    assert len(hits) == 2
    assert len(figures[0].mentions) == 1
    assert figures[0].mentions[0].page_number == 2
    assert "Fig. 3" in figures[0].mentions[0].sentence
    assert len(tables[0].mentions) == 1
    assert tables[0].mentions[0].page_number == 7
    assert "Table 2" in tables[0].mentions[0].sentence


def test_caption_self_reference_is_not_kept_as_mention() -> None:
    caption = "Fig. 3 Comparison of error rates across methods."
    figures = [_figure("f1", "Fig. 3", page=5, caption=caption)]

    _assign_visual_mentions(figures, [], _collect_mention_hits([_page(5, caption)]))

    assert figures[0].mentions == []


def test_mentions_are_capped_and_deduplicated() -> None:
    figures = [_figure("f1", "Fig. 4", page=1)]
    pages = [
        _page(index, f"Page {index} context: results are shown in Fig. 4 here.")
        for index in range(1, MAX_MENTIONS_PER_VISUAL + 4)
    ]

    _assign_visual_mentions(figures, [], _collect_mention_hits(pages))

    assert len(figures[0].mentions) == MAX_MENTIONS_PER_VISUAL
    sentences = {item.sentence for item in figures[0].mentions}
    assert len(sentences) == MAX_MENTIONS_PER_VISUAL
