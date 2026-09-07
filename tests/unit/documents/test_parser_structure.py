"""Tests for the stage-1 structured reading base (P1: blocks, sections, mentions)."""

from app.documents.parser import (
    _assign_visual_mentions,
    _collect_mention_hits,
    _core_key,
    _is_caption,
    _is_page_number,
    _section_type_for_title,
    analysis_pages,
    classify_block_roles,
    is_reference_page,
    read_order,
)
from app.schemas import (
    BoundingBox,
    DocumentFigure,
    DocumentPage,
    DocumentSection,
    ParsedDocument,
    TableCandidate,
)


def _record(x0, y0, x1, y1, text, size=10.0):
    return (x0, y0, x1, y1, text, size)


def test_section_type_classification() -> None:
    assert _section_type_for_title("Abstract") == "abstract"
    assert _section_type_for_title("3 Method") == "method"
    assert _section_type_for_title("Experiments and Results") == "experiment"
    assert _section_type_for_title("Related Work") == "background"
    assert _section_type_for_title("Discussion and Conclusion") == "discussion"
    assert _section_type_for_title("References") == "references"
    assert _section_type_for_title("Some Odd Heading") == "other"


def test_is_page_number_and_caption_helpers() -> None:
    assert _is_page_number("12")
    assert _is_page_number("- 34 -")
    assert _is_page_number("xii")
    assert not _is_page_number("12 things to know")
    assert _is_caption("Fig. 3 Comparison of methods.")
    assert _is_caption("Table 2 Per-dataset results.")
    assert not _is_caption("This sentence mentions Fig. 3 in the middle.")


def test_single_column_read_order_is_y_then_x() -> None:
    records = [
        _record(40, 700, 540, 760, "last paragraph"),
        _record(40, 100, 540, 160, "first paragraph"),
    ]
    ordered = read_order(records, page_width=600)
    assert [record[4] for record in ordered] == ["first paragraph", "last paragraph"]


def test_two_column_read_order_is_column_major() -> None:
    records = [
        _record(30, 300, 280, 380, "left lower"),
        _record(30, 100, 280, 180, "left upper"),
        _record(330, 100, 570, 180, "right upper"),
        _record(330, 300, 570, 380, "right lower"),
    ]
    ordered = read_order(records, page_width=600)
    assert [record[4] for record in ordered] == [
        "left upper", "left lower", "right upper", "right lower",
    ]


def test_roles_filter_margins_and_keep_body() -> None:
    records = [
        _record(30, 8, 560, 30, "Journal of Stuff", size=8.0),        # header
        _record(30, 770, 560, 790, "42", size=8.0),                   # page number
        _record(30, 100, 560, 130, "3 Method", size=16.0),            # heading (big font)
        _record(30, 150, 560, 200, "Fig. 3 Comparison of methods.", size=9.0),  # caption
        _record(30, 220, 560, 300, "This is a long body paragraph that keeps going.", size=10.0),
    ]
    labelled = classify_block_roles(records, page_height=800, median_size=10.0)
    roles = [role for _, role in labelled]
    assert roles == ["header", "page_number", "heading", "caption", "body"]


def test_reference_pages_are_excluded_from_analysis_text() -> None:
    sections = [
        DocumentSection(title="Conclusion", start_page=8, end_page=9, type="discussion",
                        confidence=0.9),
        DocumentSection(title="References", start_page=10, end_page=12, type="references",
                        confidence=0.9),
    ]
    pages = [
        DocumentPage(document_id="d", page_number=n, text=f"content {n}",
                     width=1, height=1, rotation=0)
        for n in range(8, 13)
    ]
    parsed = ParsedDocument(
        document_id="d", project_id="p", page_count=12,
        pages=pages, sections=sections, figures=[], tables=[],
    )

    assert is_reference_page(parsed, 11)
    assert not is_reference_page(parsed, 9)
    assert [page.page_number for page in analysis_pages(parsed)] == [8, 9]


def _figure(identifier: str, label: str) -> DocumentFigure:
    return DocumentFigure(
        figure_id=identifier, document_id="d", page_number=5, label=label,
        caption=None, figure_type="result",
        bbox=BoundingBox(x0=0, y0=0, x1=10, y1=10),
        source_path=f"figures/{identifier}.png", sha256="a" * 64,
    )


def test_compound_subfigure_and_supplementary_mentions() -> None:
    pages = [
        DocumentPage(document_id="d", page_number=2, width=1, height=1, rotation=0, text=(
            "Figs. 3 and 4 show the trend; Figure S1 reports the appendix detail; "
            "the zoom is in Fig. 3(a)."
        )),
    ]
    hits = _collect_mention_hits(pages)
    keys = {(kind, key) for kind, key, _, _ in hits}
    assert ("figure", "3") in keys
    assert ("figure", "4") in keys
    assert ("figure", "s1") in keys
    assert ("figure", "3a") in keys
    assert len(hits) == 4


def test_subfigure_mention_falls_back_to_parent_figure() -> None:
    figures = [_figure("f1", "Fig. 3")]
    pages = [
        DocumentPage(document_id="d", page_number=6, width=1, height=1, rotation=0, text=(
            "The left panel of Fig. 3(a) is the most informative."
        )),
    ]
    _assign_visual_mentions(figures, [], _collect_mention_hits(pages))
    assert len(figures[0].mentions) == 1
    assert figures[0].mentions[0].page_number == 6


def test_core_key_strips_subfigure_letter() -> None:
    assert _core_key("3a") == "3"
    assert _core_key("3") == "3"
    assert _core_key("s1") is None


def test_table_mentions_are_collected() -> None:
    tables = [TableCandidate(table_id="t1", document_id="d", page_number=5, label="Table 2")]
    pages = [
        DocumentPage(document_id="d", page_number=3, width=1, height=1, rotation=0, text=(
            "Table 2 reports the per-dataset numbers."
        )),
    ]
    _assign_visual_mentions([], tables, _collect_mention_hits(pages))
    assert len(tables[0].mentions) == 1
    assert "Table 2" in tables[0].mentions[0].sentence


def test_unmatched_figure_references_report_repair_targets() -> None:
    from app.documents.parser import unmatched_figure_references

    figures = [_figure("f1", "Fig. 1")]
    pages = [
        DocumentPage(document_id="d", page_number=2, width=1, height=1, rotation=0, text=(
            "We compare against Fig. 1 and the missing Fig. 7 here; "
            "Fig. 3(a) also refers to the same missing figure."
        )),
        DocumentPage(document_id="d", page_number=5, width=1, height=1, rotation=0, text=(
            "The appendix repeats the reference to Fig. 7."
        )),
    ]

    targets = unmatched_figure_references(pages, figures)

    # Fig. 1 is localized; Fig. 7 (repeated on p2 and p5) collapses to its first
    # mentioning page, and Fig. 3(a) has no parent figure so it is its own target.
    assert set(targets) == {("Fig. 7", 2), ("Fig. 3a", 2)}
