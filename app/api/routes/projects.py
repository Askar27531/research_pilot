from time import perf_counter
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request

from app.agents import Coordinator, LiteratureResearcher
from app.api.dependencies import (
    get_checkpointer,
    get_literature_client,
    get_llm_provider,
    get_paper_repository,
    get_project_repository,
    get_skill_registry,
    get_trace_repository,
    get_work_item_repository,
)
from app.db import PaperRepository, ProjectRepository, TraceRepository, WorkItemRepository
from app.literature import LiteratureMCPClient
from app.llm import LLMProvider
from app.schemas import (
    AgentTask,
    ProgressMetrics,
    ProjectCreateRequest,
    ProjectRecord,
    ProjectRunRequest,
    ProjectRunResponse,
    RankedPaper,
    StoredPaper,
    TraceMetrics,
    TraceRecord,
    WorkItemProgress,
)
from app.skills import SkillRegistry

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectRecord, status_code=201)
async def create_project(
    request: ProjectCreateRequest,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
) -> ProjectRecord:
    return await projects.create(request.name, request.request)


@router.get("/{project_id}", response_model=ProjectRecord)
async def get_project(
    project_id: str,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
) -> ProjectRecord:
    return await projects.get(project_id)


@router.get("/{project_id}/papers", response_model=list[StoredPaper])
async def get_project_papers(
    project_id: str,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    papers: Annotated[PaperRepository, Depends(get_paper_repository)],
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[StoredPaper]:
    await projects.get(project_id)
    return await papers.list_for_project(project_id, limit=limit, offset=offset)


@router.get("/{project_id}/trace", response_model=list[TraceRecord])
async def get_project_trace(
    project_id: str,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    traces: Annotated[TraceRepository, Depends(get_trace_repository)],
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    event_type: Annotated[str | None, Query(max_length=100)] = None,
    success: bool | None = None,
) -> list[TraceRecord]:
    await projects.get(project_id)
    return await traces.list_for_project(
        project_id,
        limit=limit,
        offset=offset,
        event_type=event_type,
        success=success,
    )


@router.get("/{project_id}/trace/metrics", response_model=TraceMetrics)
async def get_trace_metrics(
    project_id: str,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    traces: Annotated[TraceRepository, Depends(get_trace_repository)],
) -> TraceMetrics:
    await projects.get(project_id)
    return await traces.metrics(project_id)


@router.get("/{project_id}/progress", response_model=list[WorkItemProgress])
async def get_work_item_progress(
    project_id: str,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    items: Annotated[WorkItemRepository, Depends(get_work_item_repository)],
) -> list[WorkItemProgress]:
    await projects.get(project_id)
    return await items.list_for_project(project_id)


@router.get("/{project_id}/progress/metrics", response_model=ProgressMetrics)
async def get_progress_metrics(
    project_id: str,
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    items: Annotated[WorkItemRepository, Depends(get_work_item_repository)],
) -> ProgressMetrics:
    await projects.get(project_id)
    return await items.metrics(project_id)


@router.post("/{project_id}/research", response_model=ProjectRunResponse)
async def run_project_research(
    project_id: str,
    run_request: ProjectRunRequest,
    http_request: Request,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    literature: Annotated[LiteratureMCPClient, Depends(get_literature_client)],
    checkpointer: Annotated[Any, Depends(get_checkpointer)],
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    papers: Annotated[PaperRepository, Depends(get_paper_repository)],
    traces: Annotated[TraceRepository, Depends(get_trace_repository)],
    skills: Annotated[SkillRegistry, Depends(get_skill_registry)],
    items: Annotated[WorkItemRepository, Depends(get_work_item_repository)],
) -> ProjectRunResponse:
    run_id = run_request.run_id or str(uuid4())
    return await _execute(
        project_id,
        run_id,
        provider,
        literature,
        checkpointer,
        projects,
        papers,
        traces,
        skills,
        items,
        http_request.state.request_id,
        resume=False,
    )


@router.post("/{project_id}/resume", response_model=ProjectRunResponse)
async def resume_project_research(
    project_id: str,
    run_request: ProjectRunRequest,
    http_request: Request,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    literature: Annotated[LiteratureMCPClient, Depends(get_literature_client)],
    checkpointer: Annotated[Any, Depends(get_checkpointer)],
    projects: Annotated[ProjectRepository, Depends(get_project_repository)],
    papers: Annotated[PaperRepository, Depends(get_paper_repository)],
    traces: Annotated[TraceRepository, Depends(get_trace_repository)],
    skills: Annotated[SkillRegistry, Depends(get_skill_registry)],
    items: Annotated[WorkItemRepository, Depends(get_work_item_repository)],
) -> ProjectRunResponse:
    run_id = run_request.run_id or str(uuid4())
    return await _execute(
        project_id,
        run_id,
        provider,
        literature,
        checkpointer,
        projects,
        papers,
        traces,
        skills,
        items,
        http_request.state.request_id,
        resume=True,
    )


async def _execute(
    project_id: str,
    run_id: str,
    provider: LLMProvider,
    literature: LiteratureMCPClient,
    checkpointer: Any,
    projects: ProjectRepository,
    papers: PaperRepository,
    traces: TraceRepository,
    skills: SkillRegistry,
    items: WorkItemRepository,
    trace_id: str,
    *,
    resume: bool,
) -> ProjectRunResponse:
    project, claimed = await projects.start_run(project_id, run_id, resume=resume)
    existing_papers = await papers.list_for_project(project_id)
    if not claimed:
        return ProjectRunResponse(
            project_id=project_id,
            run_id=project.last_run_id or run_id,
            status=project.status,
            current_stage=project.current_stage,
            selected_paper_count=len(existing_papers),
            resumed=resume,
        )

    started = perf_counter()
    await traces.append(
        project_id,
        trace_id,
        "research_started" if not resume else "research_resumed",
        success=True,
        agent="research_workflow",
        summary={"resume": resume},
    )
    researcher = LiteratureResearcher(provider, literature, checkpointer, skills, traces, items)
    coordinator = Coordinator(researcher, traces)
    task = AgentTask(
        task_id=run_id,
        project_id=project_id,
        task_type="literature_search",
        objective=project.goal,
        context={"request": project.request.model_dump(mode="json"), "resume": resume},
    )
    try:
        agent_result = await coordinator.run(task, trace_id)
        result = agent_result.output
        selected = [RankedPaper.model_validate(item) for item in result["selected_papers"]]
        for ranked in selected:
            await papers.upsert_ranked(project_id, ranked)
        completed = await projects.complete(project_id, result["current_stage"])
        await traces.append(
            project_id,
            trace_id,
            "research_completed",
            success=True,
            agent="research_workflow",
            latency_ms=round((perf_counter() - started) * 1000),
            summary={"selected_paper_count": len(selected)},
        )
        return ProjectRunResponse(
            project_id=project_id,
            run_id=run_id,
            status=completed.status,
            current_stage=completed.current_stage,
            selected_paper_count=len(selected),
            resumed=resume,
        )
    except Exception as exc:
        current_stage = "failed"
        await projects.fail(
            project_id,
            current_stage,
            {"type": type(exc).__name__, "message": str(exc)[:1_000]},
        )
        await traces.append(
            project_id,
            trace_id,
            "research_failed",
            success=False,
            agent="research_workflow",
            latency_ms=round((perf_counter() - started) * 1000),
            error={"type": type(exc).__name__, "message": str(exc)[:1_000]},
        )
        raise
