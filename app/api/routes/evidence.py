from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.api.dependencies import (
    get_document_repository,
    get_document_service,
    get_evidence_repository,
    get_project_repository,
    get_summary_repository,
)
from app.db import (
    DocumentRepository,
    EvidenceRepository,
    ProjectRepository,
    SummaryRepository,
)
from app.documents import DocumentService
from app.evidence import CrossPaperSynthesizer, EvidenceBuilder, EvidenceVerifier
from app.schemas import (
    CrossPaperComparison,
    EvidenceNode,
    EvidenceSourcePreview,
    FigureEvidenceCreate,
    LinkedDocument,
    PaperSummary,
    TableEvidenceCreate,
    TextEvidenceCreate,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["evidence"])


@router.post("/documents/import", response_model=LinkedDocument, status_code=201)
async def import_document(
    project_id: str,
    paper_id: Annotated[str, Form(min_length=1)],
    file: Annotated[UploadFile, File()],
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    service: Annotated[DocumentService, Depends(get_document_service)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
) -> LinkedDocument:
    await projects.get(project_id)
    content = await file.read(service.workspace.max_document_bytes + 1)
    entry = service.workspace.import_pdf_bytes(project_id, file.filename or "upload.pdf", content)
    service.parse_document(project_id, entry.document_id)
    return await documents.register(project_id, paper_id, entry)


def builder(
    service: DocumentService,
    documents: DocumentRepository,
    evidence: EvidenceRepository,
) -> EvidenceBuilder:
    return EvidenceBuilder(service, documents, evidence)


@router.post("/evidence/text", response_model=EvidenceNode, status_code=201)
async def create_text_evidence(
    project_id: str,
    request: TextEvidenceCreate,
    service: Annotated[DocumentService, Depends(get_document_service)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    evidence: Annotated[EvidenceRepository, Depends(get_evidence_repository)],
) -> EvidenceNode:
    return await builder(service, documents, evidence).build_text(project_id, request)


@router.post("/evidence/figure", response_model=EvidenceNode, status_code=201)
async def create_figure_evidence(
    project_id: str,
    request: FigureEvidenceCreate,
    service: Annotated[DocumentService, Depends(get_document_service)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    evidence: Annotated[EvidenceRepository, Depends(get_evidence_repository)],
) -> EvidenceNode:
    return await builder(service, documents, evidence).build_figure(project_id, request)


@router.post("/evidence/table", response_model=EvidenceNode, status_code=201)
async def create_table_evidence(
    project_id: str,
    request: TableEvidenceCreate,
    service: Annotated[DocumentService, Depends(get_document_service)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    evidence: Annotated[EvidenceRepository, Depends(get_evidence_repository)],
) -> EvidenceNode:
    return await builder(service, documents, evidence).build_table(project_id, request)


@router.get("/evidence", response_model=list[EvidenceNode])
async def list_evidence(
    project_id: str,
    paper_id: str,
    evidence: Annotated[EvidenceRepository, Depends(get_evidence_repository)],
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EvidenceNode]:
    return await evidence.list_for_paper(project_id, paper_id, limit=limit, offset=offset)


@router.get("/evidence/{evidence_id}", response_model=EvidenceNode)
async def get_evidence(
    project_id: str,
    evidence_id: str,
    evidence: Annotated[EvidenceRepository, Depends(get_evidence_repository)],
) -> EvidenceNode:
    return await evidence.get(project_id, evidence_id)


@router.get("/evidence/{evidence_id}/source", response_model=EvidenceSourcePreview)
async def get_evidence_source(
    project_id: str,
    evidence_id: str,
    service: Annotated[DocumentService, Depends(get_document_service)],
    evidence: Annotated[EvidenceRepository, Depends(get_evidence_repository)],
) -> EvidenceSourcePreview:
    node = await evidence.get(project_id, evidence_id)
    return EvidenceVerifier(service).verify(node)


@router.put("/summaries/{paper_id}", response_model=PaperSummary)
async def save_summary(
    project_id: str,
    paper_id: str,
    summary: PaperSummary,
    summaries: Annotated[SummaryRepository, Depends(get_summary_repository)],
) -> PaperSummary:
    normalized = summary.model_copy(update={"project_id": project_id, "paper_id": paper_id})
    return await summaries.upsert(normalized)


@router.get("/comparison", response_model=CrossPaperComparison)
async def get_comparison(
    project_id: str,
    summaries: Annotated[SummaryRepository, Depends(get_summary_repository)],
) -> CrossPaperComparison:
    records = await summaries.list_for_project(project_id)
    return CrossPaperSynthesizer().build(project_id, records)
