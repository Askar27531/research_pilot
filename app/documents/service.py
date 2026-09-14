"""Facade over the PDF workspace + parser, plus a one-call assembler.

``parse_document`` runs (and persists) a parse; every other method reads the
persisted result back, so a parsed document is only parsed once per source.
``create_document_service`` is the single assembly point shared by the app
lifespan, the document MCP server and probes (one DPI knob, no duplication).
"""

from app.core.config import Settings, get_settings
from app.documents.errors import DocumentNotFoundError
from app.documents.parser import PDFParser
from app.documents.workspace import WorkspaceManager
from app.schemas import DocumentFigure, DocumentPage, ParsedDocument


class DocumentService:
    def __init__(self, workspace: WorkspaceManager, parser: PDFParser) -> None:
        self.workspace = workspace
        self.parser = parser

    def parse_document(self, project_id: str, document_id: str) -> ParsedDocument:
        return self.parser.parse(project_id, document_id)

    def get_structure(self, project_id: str, document_id: str) -> ParsedDocument:
        """Return the persisted parse result for a document (raise if not parsed)."""
        self.workspace.get_document(project_id, document_id)
        path = self.workspace.resolve_safe_path(
            project_id, f"parsed/{document_id}/document.json"
        )
        if not path.is_file():
            raise DocumentNotFoundError(f"Document has not been parsed: {document_id}")
        return ParsedDocument.model_validate_json(path.read_text(encoding="utf-8"))

    def get_page(self, project_id: str, document_id: str, page_number: int) -> DocumentPage:
        for page in self.get_structure(project_id, document_id).pages:
            if page.page_number == page_number:
                return page
        raise DocumentNotFoundError(f"Unknown page {page_number} in document {document_id}")

    def extract_figures(self, project_id: str, document_id: str) -> list[DocumentFigure]:
        return self.get_structure(project_id, document_id).figures

    def get_figure(self, project_id: str, document_id: str, figure_id: str) -> DocumentFigure:
        for figure in self.extract_figures(project_id, document_id):
            if figure.figure_id == figure_id:
                return figure
        raise DocumentNotFoundError(f"Unknown figure: {figure_id}")


def create_document_service(settings: Settings | None = None) -> DocumentService:
    """Assemble the document subsystem: workspace + parser + facade."""
    settings = settings or get_settings()
    workspace = WorkspaceManager(
        settings.workspace_root, max_document_bytes=settings.document_max_bytes
    )
    return DocumentService(
        workspace,
        PDFParser(
            workspace,
            render_dpi=settings.pdf_render_dpi,
            ocr_languages=settings.ocr_languages,
        ),
    )
