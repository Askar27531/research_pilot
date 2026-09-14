from fastmcp import FastMCP

from app.core.config import get_settings
from app.documents import DocumentService
from app.documents.service import create_document_service as _build_document_service
from app.schemas import DocumentFigure, DocumentPage, ParsedDocument


def create_document_service() -> DocumentService:
    """Assemble the document subsystem from shared settings (one DPI knob)."""
    return _build_document_service(get_settings())


def create_document_server(
    service: DocumentService | None = None,
    *,
    auth: object | None = None,
) -> FastMCP:
    documents = service or create_document_service()
    server = FastMCP(
        "ResearchPilot Document",
        version="0.1.0",
        auth=auth,
        instructions=(
            "ResearchPilot Document: parse imported PDFs and retrieve persisted page, "
            "structure, and figure artifacts scoped to a project."
        ),
    )

    @server.tool
    def parse_document(project_id: str, document_id: str) -> ParsedDocument:
        """Parse an imported PDF and persist page, structure, and figure artifacts."""
        return documents.parse_document(project_id, document_id)

    @server.tool
    def get_page(project_id: str, document_id: str, page_number: int) -> DocumentPage:
        """Return one parsed page and its project-relative screenshot path."""
        return documents.get_page(project_id, document_id, page_number)

    @server.tool
    def get_document_structure(project_id: str, document_id: str) -> ParsedDocument:
        """Return persisted document metadata, pages, sections, figures, and tables."""
        return documents.get_structure(project_id, document_id)

    @server.tool
    def extract_figures(project_id: str, document_id: str) -> list[DocumentFigure]:
        """Return extracted raster figures and matched captions."""
        return documents.extract_figures(project_id, document_id)

    @server.tool
    def get_figure(
        project_id: str, document_id: str, figure_id: str
    ) -> DocumentFigure:
        """Return one figure scoped to its project and document."""
        return documents.get_figure(project_id, document_id, figure_id)

    return server


mcp = create_document_server()


if __name__ == "__main__":
    mcp.run()
