from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.api.dependencies import (
    get_artifact_repository,
    get_document_service,
    get_project_repository,
)
from app.artifacts import ArtifactService
from app.db import ArtifactRepository, ProjectRepository
from app.documents import DocumentService
from app.schemas import (
    ArtifactRecord,
    CSVArtifactRequest,
    MarkdownArtifactRequest,
    MermaidArtifactRequest,
)

router = APIRouter(prefix="/projects/{project_id}/artifacts", tags=["artifacts"])


def service(documents: DocumentService, repository: ArtifactRepository) -> ArtifactService:
    return ArtifactService(documents.workspace, repository)


@router.post("/markdown", response_model=ArtifactRecord, status_code=201)
async def create_markdown(
    project_id: str,
    request: MarkdownArtifactRequest,
    documents: Annotated[DocumentService, Depends(get_document_service)],
    artifacts: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> ArtifactRecord:
    return await service(documents, artifacts).markdown(
        project_id, request.name, request.title, request.sections
    )


@router.post("/csv", response_model=ArtifactRecord, status_code=201)
async def create_csv(
    project_id: str,
    request: CSVArtifactRequest,
    documents: Annotated[DocumentService, Depends(get_document_service)],
    artifacts: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> ArtifactRecord:
    return await service(documents, artifacts).csv(
        project_id, request.name, request.columns, request.rows
    )


@router.post("/mermaid", response_model=ArtifactRecord, status_code=201)
async def create_mermaid(
    project_id: str,
    request: MermaidArtifactRequest,
    documents: Annotated[DocumentService, Depends(get_document_service)],
    artifacts: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> ArtifactRecord:
    return await service(documents, artifacts).mermaid(project_id, request.name, request.diagram)


@router.get("", response_model=list[ArtifactRecord])
async def list_artifacts(
    project_id: str,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    artifacts: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> list[ArtifactRecord]:
    await projects.get(project_id)
    return await artifacts.list_for_project(project_id)


@router.get("/{artifact_id}/download")
async def download_artifact(
    project_id: str,
    artifact_id: str,
    documents: Annotated[DocumentService, Depends(get_document_service)],
    artifacts: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> Response:
    record = await artifacts.get(project_id, artifact_id)
    content = await service(documents, artifacts).read(record)
    media = {"markdown": "text/markdown", "csv": "text/csv", "mermaid": "text/plain"}[
        record.artifact_type
    ]
    return Response(
        content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{record.name}"'},
    )
