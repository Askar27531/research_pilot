"""Tests for block-driven evidence chunking (P1-a blocks put to use)."""

from app.agents.paper_analysis import _chunks_for_page
from app.schemas import DocumentBlock, DocumentPage


def _block(text: str, role: str) -> DocumentBlock:
    return DocumentBlock(index=0, role=role, text=text)


def _page(blocks: list[DocumentBlock]) -> DocumentPage:
    # page.text mirrors the parser contract: kept blocks joined with "\n",
    # margin roles excluded.
    kept = [block for block in blocks if block.role not in {"header", "footer", "page_number"}]
    return DocumentPage(
        document_id="d", page_number=1,
        text="\n".join(block.text for block in kept),
        width=600, height=800, rotation=0, blocks=blocks,
    )


def test_chunks_follow_block_boundaries_and_drop_noise() -> None:
    page = _page([
        _block("Journal of Synthetic Stuff", "header"),
        _block("The first body paragraph explains the approach in detail.", "body"),
        _block("5 Method", "heading"),
        _block("The second body paragraph reports the main experiment result.", "body"),
        _block("Fig. 2 Caption for the result figure.", "caption"),
        _block("42", "page_number"),
    ])

    chunks = _chunks_for_page(page)

    assert chunks, "expected at least one chunk"
    assert "Journal of Synthetic Stuff" not in "\n".join(chunks)
    assert "5 Method" not in "\n".join(chunks)
    assert "42" not in "\n".join(chunks)
    assert "The first body paragraph" in chunks[0]
    assert "Fig. 2 Caption" in "\n".join(chunks)
    # Every chunk must stay a contiguous substring of page.text so evidence
    # span locators keep resolving.
    for chunk in chunks:
        assert chunk in page.text


def test_long_body_is_split_and_headings_delimit_runs() -> None:
    paragraph_a = "Sentence one in the first paragraph. " * 60
    paragraph_b = "Sentence one in the second paragraph. " * 60
    page = _page([
        _block(paragraph_a, "body"),
        _block("7 Conclusion", "heading"),
        _block(paragraph_b, "body"),
    ])

    chunks = _chunks_for_page(page)

    assert len(chunks) >= 2
    assert "7 Conclusion" not in "\n".join(chunks)
    assert all(chunk in page.text for chunk in chunks)
    # Paragraphs on different sides of a heading never share a chunk.
    for chunk in chunks:
        assert not ("first paragraph" in chunk and "second paragraph" in chunk)


def test_legacy_page_without_blocks_falls_back_to_text_chunking() -> None:
    page = DocumentPage(
        document_id="d", page_number=1,
        text="A legacy page with no layout blocks yet. " * 80,
        width=600, height=800, rotation=0,
    )

    chunks = _chunks_for_page(page)

    assert chunks
    assert all(chunk in page.text for chunk in chunks)
