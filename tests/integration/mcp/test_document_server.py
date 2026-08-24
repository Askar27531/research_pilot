from pathlib import Path

import pymupdf
import pytest
from fastmcp import Client

from app.documents import DocumentService, PDFParser, WorkspaceManager
from app.schemas import DocumentFigure, DocumentPage, ParsedDocument
from mcp_servers.document import create_document_server


def create_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Introduction\nDocument MCP test")
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 160), False)
    pixmap.clear_with(0x336699)
    page.insert_image(pymupdf.Rect(100, 150, 400, 390), stream=pixmap.tobytes("png"))
    page.insert_text((100, 420), "Figure 1. Network architecture.")
    document.save(path)
    document.close()


@pytest.mark.asyncio
async def test_document_tools_cross_mcp_boundary(tmp_path) -> None:
    source = tmp_path / "paper.pdf"
    create_pdf(source)
    workspace = WorkspaceManager(tmp_path / "workspaces")
    entry = workspace.import_pdf("project-1", source)
    service = DocumentService(workspace, PDFParser(workspace, render_dpi=72))
    server = create_document_server(service)

    async with Client(server) as client:
        parsed_result = await client.call_tool(
            "parse_document",
            {"project_id": "project-1", "document_id": entry.document_id},
        )
        page_result = await client.call_tool(
            "get_page",
            {
                "project_id": "project-1",
                "document_id": entry.document_id,
                "page_number": 1,
            },
        )
        structure_result = await client.call_tool(
            "get_document_structure",
            {"project_id": "project-1", "document_id": entry.document_id},
        )
        figures_result = await client.call_tool(
            "extract_figures",
            {"project_id": "project-1", "document_id": entry.document_id},
        )
        figure_data = figures_result.structured_content["result"][0]
        figure_result = await client.call_tool(
            "get_figure",
            {
                "project_id": "project-1",
                "document_id": entry.document_id,
                "figure_id": figure_data["figure_id"],
            },
        )

    parsed = ParsedDocument.model_validate(parsed_result.structured_content)
    page = DocumentPage.model_validate(page_result.structured_content)
    structure = ParsedDocument.model_validate(structure_result.structured_content)
    assert parsed.page_count == 1
    assert page.page_number == 1
    assert structure.document_id == entry.document_id
    figure = DocumentFigure.model_validate(figure_result.structured_content)
    assert len(figures_result.structured_content["result"]) == 1
    assert figure.figure_id == figure_data["figure_id"]
