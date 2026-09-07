import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from app.db import DocumentRepository, EvidenceRepository
from app.documents import DocumentNotFoundError, DocumentService
from app.schemas import (
    EvidenceNode,
    FigureEvidenceCreate,
    TableEvidenceCreate,
    TextEvidenceCreate,
)


class EvidenceBuilder:
    def __init__(
        self,
        documents: DocumentService,
        document_repository: DocumentRepository,
        evidence_repository: EvidenceRepository,
    ) -> None:
        self.documents = documents
        self.document_repository = document_repository
        self.evidence_repository = evidence_repository

    async def build_text(self, project_id: str, request: TextEvidenceCreate) -> EvidenceNode:
        await self._validate_link(project_id, request.paper_id, request.document_id)
        page = self.documents.get_page(project_id, request.document_id, request.page_number)
        if (
            not page.screenshot_path
            or not self.documents.workspace.resolve_safe_path(
                project_id, page.screenshot_path
            ).is_file()
        ):
            raise DocumentNotFoundError("Page screenshot is unavailable for source verification")
        start = page.text.find(request.quote)
        if start < 0:
            raise DocumentNotFoundError("Quoted text does not exist on the requested page")
        end = start + len(request.quote)
        section = self._section_for_page(
            self.documents.get_structure(project_id, request.document_id), request.page_number
        )
        source_hash = hashlib.sha256(request.quote.encode("utf-8")).hexdigest()
        node = EvidenceNode(
            evidence_id=str(uuid4()),
            project_id=project_id,
            paper_id=request.paper_id,
            document_id=request.document_id,
            evidence_type="text",
            claim=request.claim,
            confidence=request.confidence,
            page_number=request.page_number,
            section=section,
            excerpt=request.quote,
            span_start=start,
            span_end=end,
            source_path=page.screenshot_path,
            source_hash=source_hash,
            created_at=datetime.now(UTC).isoformat(),
        )
        locator = f"text:{request.page_number}:{start}:{end}:{source_hash}"
        return await self.evidence_repository.create(node, locator)

    async def build_figure(self, project_id: str, request: FigureEvidenceCreate) -> EvidenceNode:
        await self._validate_link(project_id, request.paper_id, request.document_id)
        parsed = self.documents.get_structure(project_id, request.document_id)
        figure = next(
            (item for item in parsed.figures if item.figure_id == request.figure_id), None
        )
        if figure is None:
            raise DocumentNotFoundError(f"Unknown figure: {request.figure_id}")
        if not self.documents.workspace.resolve_safe_path(project_id, figure.source_path).is_file():
            raise DocumentNotFoundError("Figure artifact is missing")
        node = EvidenceNode(
            evidence_id=str(uuid4()),
            project_id=project_id,
            paper_id=request.paper_id,
            document_id=request.document_id,
            evidence_type="figure",
            claim=request.claim,
            confidence=request.confidence,
            page_number=figure.page_number,
            section=self._section_for_page(parsed, figure.page_number),
            label=figure.label or figure.figure_id,
            excerpt=figure.caption,
            bbox=figure.bbox,
            source_path=figure.source_path,
            source_hash=figure.sha256,
            created_at=datetime.now(UTC).isoformat(),
        )
        locator = f"figure:{figure.page_number}:{figure.figure_id}:{figure.sha256}"
        return await self.evidence_repository.create(node, locator)

    async def build_table(self, project_id: str, request: TableEvidenceCreate) -> EvidenceNode:
        await self._validate_link(project_id, request.paper_id, request.document_id)
        parsed = self.documents.get_structure(project_id, request.document_id)
        table = next((item for item in parsed.tables if item.table_id == request.table_id), None)
        if table is None:
            raise DocumentNotFoundError(f"Unknown table: {request.table_id}")
        page = self.documents.get_page(project_id, request.document_id, table.page_number)
        caption = table.caption or table.label or table.table_id
        source_hash = hashlib.sha256(caption.encode("utf-8")).hexdigest()
        node = EvidenceNode(
            evidence_id=str(uuid4()),
            project_id=project_id,
            paper_id=request.paper_id,
            document_id=request.document_id,
            evidence_type="table",
            claim=request.claim,
            confidence=request.confidence,
            page_number=table.page_number,
            section=self._section_for_page(parsed, table.page_number),
            label=table.label or table.table_id,
            excerpt=table.caption,
            bbox=table.bbox,
            source_path=page.screenshot_path or f"pages/{table.page_number}",
            source_hash=source_hash,
            created_at=datetime.now(UTC).isoformat(),
        )
        locator = f"table:{table.page_number}:{table.table_id}:{source_hash}"
        return await self.evidence_repository.create(node, locator)

    async def _validate_link(self, project_id: str, paper_id: str, document_id: str) -> None:
        linked = await self.document_repository.get(project_id, document_id)
        if linked.paper_id != paper_id:
            raise DocumentNotFoundError("Document is not linked to the requested paper")
        entry = self.documents.workspace.get_document(project_id, document_id)
        if entry.sha256 != linked.sha256:
            raise DocumentNotFoundError("Workspace document hash differs from database link")

    @staticmethod
    def _section_for_page(parsed, page_number: int) -> str | None:
        matches = [
            section
            for section in parsed.sections
            if section.start_page <= page_number <= section.end_page
        ]
        return matches[-1].title if matches else None
