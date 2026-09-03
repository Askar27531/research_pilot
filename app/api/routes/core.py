from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response

from app.api.dependencies import get_workspace_service
from app.api.routes.health import health
from app.schemas import (
    ProjectSummary,
    ProjectWorkspace,
    WorkspaceActionRequest,
    WorkspaceMutationResult,
    WorkspaceProjectCreate,
    WorkspaceProjectUpdate,
)
from app.services import WorkspaceService

router = APIRouter(tags=["workspace"])
Service = Annotated[WorkspaceService, Depends(get_workspace_service)]


@router.get("/projects", response_model=list[ProjectSummary])
async def list_projects(service: Service) -> list[ProjectSummary]:
    return await service.list_projects()


@router.post(
    "/projects",
    response_model=WorkspaceMutationResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_project(
    body: WorkspaceProjectCreate, request: Request, service: Service
) -> WorkspaceMutationResult:
    capability = await health(request)
    if not capability.paper_analysis.ready:
        raise HTTPException(status_code=409, detail={
            "message": "本地论文分析模型尚未就绪",
            "blockers": capability.paper_analysis.blockers,
        })
    return await service.create(body)


@router.get("/projects/{project_id}/workspace", response_model=ProjectWorkspace)
async def get_workspace(
    project_id: str,
    service: Service,
    paper: Annotated[str | None, Query()] = None,
) -> ProjectWorkspace:
    return await service.workspace(project_id, paper)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, service: Service) -> Response:
    await service.delete(project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/projects/{project_id}", response_model=WorkspaceMutationResult)
async def update_project(
    project_id: str, body: WorkspaceProjectUpdate, service: Service
) -> WorkspaceMutationResult:
    return await service.update(project_id, body)


@router.post("/projects/{project_id}/actions", response_model=WorkspaceMutationResult)
async def project_action(
    project_id: str, action: WorkspaceActionRequest, service: Service
) -> WorkspaceMutationResult:
    return await service.action(project_id, action)


@router.post(
    "/projects/{project_id}/documents",
    response_model=WorkspaceMutationResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_documents(
    project_id: str,
    service: Service,
    upload_token: Annotated[str, Form(min_length=10)],
    files: Annotated[list[UploadFile], File()],
) -> WorkspaceMutationResult:
    if not files:
        raise HTTPException(status_code=422, detail="请至少上传一个 PDF")
    first = files[0]
    content = await first.read(service.max_upload_bytes + 1)
    return await service.upload(
        project_id, upload_token, first.filename or "paper.pdf", content
    )


@router.get("/projects/{project_id}/resources/{token}")
async def get_resource(project_id: str, token: str, service: Service) -> Response:
    resource = await service.resource(project_id, token)
    headers = ({"Content-Disposition": f'attachment; filename="{resource.filename}"'}
               if resource.filename else None)
    return Response(resource.content, media_type=resource.media_type, headers=headers)
