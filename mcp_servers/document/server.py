from pathlib import Path

from fastmcp import FastMCP

from app.core.config import get_settings
from app.documents import (
    DocumentService,
    PaperIntelligenceService,
    PDFParser,
    WorkspaceManager,
)
from app.schemas import DocumentFigure, DocumentPage, PaperAnalysisJob, PaperHandle, ParsedDocument


def create_document_service() -> DocumentService:
    settings = get_settings()
    workspace = WorkspaceManager(
        settings.workspace_root, max_document_bytes=settings.document_max_bytes
    )
    return DocumentService(
        workspace,
        PDFParser(workspace, render_dpi=settings.document_render_dpi),
    )


def create_document_server(
    service: DocumentService | None = None,
    intelligence: PaperIntelligenceService | None = None,
    *,
    allow_local_files: bool = True,
    auth: object | None = None,
) -> FastMCP:
    documents = service or create_document_service()
    settings = get_settings()
    papers = intelligence or PaperIntelligenceService(
        documents,
        Path(settings.workspace_root) / "mcp-paper-intelligence.db",
        allowed_roots=settings.mcp_allowed_roots,
        allow_local_files=allow_local_files,
    )
    server = FastMCP(
        "ResearchPilot Document",
        version="0.1.0",
        auth=auth,
        instructions=(
            "ResearchPilot Paper Intelligence: safely ingest papers, run persistent multimodal "
            "analysis, and retrieve traceable structure, evidence, method cards, and summaries."
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

    @server.tool
    async def submit_paper(source_uri: str) -> PaperHandle:
        """Copy a safe file:// or https:// PDF into this server's workspace."""
        return await papers.submit(source_uri)

    @server.tool
    async def start_paper_analysis(
        paper_handle: str, objectives: list[str] | None = None
    ) -> PaperAnalysisJob:
        """Start a persistent multimodal analysis job and return immediately."""
        return await papers.start(paper_handle, objectives)

    @server.tool
    def get_analysis_status(analysis_job_id: str) -> PaperAnalysisJob:
        """Return durable status, progress, and a sanitized error for an analysis job."""
        return papers.job(analysis_job_id)

    @server.tool
    def get_paper_structure(paper_handle: str) -> ParsedDocument:
        """Return parsed pages, sections, figures, and tables."""
        return papers.structure(paper_handle)

    @server.tool
    def get_paper_evidence(
        paper_handle: str, evidence_types: list[str] | None = None
    ) -> list[dict]:
        """Return traceable text, figure, and table evidence from completed analysis."""
        values = papers.result_part(paper_handle, "evidence")
        allowed = set(evidence_types or [])
        return [value for value in values if not allowed or value["evidence_type"] in allowed]

    @server.tool
    def get_method_card(paper_handle: str) -> dict:
        """Return the evidence-linked domain-neutral method card."""
        return papers.result_part(paper_handle, "method_card")

    @server.tool
    def get_paper_summary(paper_handle: str) -> dict:
        """Return the evidence-linked paper summary."""
        return papers.result_part(paper_handle, "summary")

    return server


mcp = create_document_server()


if __name__ == "__main__":
    mcp.run()
