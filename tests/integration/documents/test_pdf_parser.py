from pathlib import Path

import pymupdf
import pytest

from app.documents import PDFParser, WorkspaceManager


def create_pdf(path: Path, kind: str) -> None:
    document = pymupdf.open()
    page_count = 3 if kind == "multipage" else 1
    for index in range(page_count):
        page = document.new_page()
        if kind == "two-column":
            page.insert_textbox(
                pymupdf.Rect(40, 80, 280, 700), "Introduction\nLeft column research text"
            )
            page.insert_textbox(
                pymupdf.Rect(320, 80, 560, 700), "Method\nRight column research text"
            )
        elif kind == "scanned":
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 500, 700), False)
            pixmap.clear_with(0xEEEEEE)
            page.insert_image(page.rect, stream=pixmap.tobytes("png"))
        else:
            page.insert_text((72, 72), f"Introduction\nPage {index + 1} research text")
            if kind == "vector":
                page.draw_rect(pymupdf.Rect(100, 150, 400, 350), color=(0, 0, 1), width=3)
                page.draw_line((100, 150), (400, 350), color=(1, 0, 0), width=2)
        if kind == "figure":
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 160), False)
            pixmap.clear_with(0x336699)
            page.insert_image(pymupdf.Rect(100, 150, 400, 390), stream=pixmap.tobytes("png"))
            page.insert_text((100, 420), "Figure 1. Network architecture and pipeline.")
            page.insert_text((100, 470), "Table 1. Registration results.")
    document.set_metadata({"title": f"{kind} sample", "author": "ResearchPilot"})
    document.save(path)
    document.close()


@pytest.mark.parametrize("kind", ["two-column", "multipage", "no-image", "vector", "scanned"])
def test_five_pdf_layout_samples_produce_pages_and_screenshots(tmp_path, kind) -> None:
    source = tmp_path / f"{kind}.pdf"
    create_pdf(source, kind)
    workspace = WorkspaceManager(tmp_path / "workspaces")
    entry = workspace.import_pdf("project-1", source)

    parsed = PDFParser(workspace, render_dpi=72).parse("project-1", entry.document_id)

    expected_pages = 3 if kind == "multipage" else 1
    assert parsed.page_count == expected_pages
    assert len(parsed.pages) == expected_pages
    assert all(page.screenshot_path for page in parsed.pages)
    assert all(
        workspace.resolve_safe_path("project-1", page.screenshot_path).is_file()
        for page in parsed.pages
    )
    if kind != "scanned":
        assert any(page.text for page in parsed.pages)


def test_parser_extracts_figure_caption_type_and_table_candidate(tmp_path) -> None:
    source = tmp_path / "figure.pdf"
    create_pdf(source, "figure")
    workspace = WorkspaceManager(tmp_path / "workspaces")
    entry = workspace.import_pdf("project-1", source)

    parsed = PDFParser(workspace, render_dpi=72).parse("project-1", entry.document_id)

    assert len(parsed.figures) == 1
    figure = parsed.figures[0]
    assert figure.document_id == entry.document_id
    assert figure.page_number == 1
    assert figure.label.lower().startswith("figure 1")
    assert figure.figure_type == "architecture"
    assert workspace.resolve_safe_path("project-1", figure.source_path).is_file()
    assert parsed.tables[0].label.lower().startswith("table 1")
    assert parsed.sections


def test_page_failure_is_recorded_without_stopping_remaining_pages(tmp_path) -> None:
    source = tmp_path / "multipage.pdf"
    create_pdf(source, "multipage")
    workspace = WorkspaceManager(tmp_path / "workspaces")
    entry = workspace.import_pdf("project-1", source)

    class FailingFirstPageParser(PDFParser):
        def _render_page(self, project_id, document_id, page, page_number):
            if page_number == 1:
                raise RuntimeError("injected render failure")
            return super()._render_page(project_id, document_id, page, page_number)

    parsed = FailingFirstPageParser(workspace, render_dpi=72).parse("project-1", entry.document_id)

    assert parsed.pages[0].error == "injected render failure"
    assert parsed.pages[1].screenshot_path is not None
    assert parsed.pages[2].screenshot_path is not None
    assert len(parsed.warnings) == 1
