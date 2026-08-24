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

    def get_parsed(self, project_id: str, document_id: str) -> ParsedDocument:
        self.workspace.get_document(project_id, document_id)
        path = self.workspace.resolve_safe_path(project_id, f"parsed/{document_id}/document.json")
        if not path.is_file():
            raise DocumentNotFoundError(f"Document has not been parsed: {document_id}")
        return ParsedDocument.model_validate_json(path.read_text(encoding="utf-8"))

    def get_page(self, project_id: str, document_id: str, page_number: int) -> DocumentPage:
        parsed = self.get_parsed(project_id, document_id)
        for page in parsed.pages:
            if page.page_number == page_number:
                return page
        raise DocumentNotFoundError(f"Unknown page {page_number} in document {document_id}")

    def get_structure(self, project_id: str, document_id: str) -> ParsedDocument:
        return self.get_parsed(project_id, document_id)

    def extract_figures(self, project_id: str, document_id: str) -> list[DocumentFigure]:
        return self.get_parsed(project_id, document_id).figures

    def get_figure(self, project_id: str, document_id: str, figure_id: str) -> DocumentFigure:
        for figure in self.extract_figures(project_id, document_id):
            if figure.figure_id == figure_id:
                return figure
        raise DocumentNotFoundError(f"Unknown figure: {figure_id}")
