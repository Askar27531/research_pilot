from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies import get_checkpointer, get_literature_client, get_llm_provider
from app.graph import build_literature_search_graph, build_research_graph, create_research_state
from app.literature import LiteratureMCPClient
from app.llm import LLMProvider
from app.schemas import RankedPaper, ResearchRequest, ResearchUnderstanding, SearchQuery

router = APIRouter(prefix="/research", tags=["research"])


class ResearchTestResponse(BaseModel):
    project_id: str
    status: Literal["completed"]
    current_stage: str
    understanding: ResearchUnderstanding


class LiteratureSearchResponse(BaseModel):
    project_id: str
    status: Literal["completed"]
    current_stage: str
    queries: list[SearchQuery]
    candidate_count: int
    selected_papers: list[RankedPaper]
    deduplication: dict[str, Any]
    search_strategy: dict[str, Any]
    concept_filter: dict[str, Any]
    warnings: list[str]


@router.post("/test", response_model=ResearchTestResponse)
async def test_research_graph(
    request: ResearchRequest,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    checkpointer: Annotated[Any, Depends(get_checkpointer)],
) -> ResearchTestResponse:
    initial_state = create_research_state(request)
    project_id = initial_state["project_id"]
    graph = build_research_graph(provider, checkpointer=checkpointer)
    config: dict[str, Any] = {"configurable": {"thread_id": project_id}}
    result = await graph.ainvoke(initial_state, config=config)
    understanding = ResearchUnderstanding.model_validate(result["understanding"])
    return ResearchTestResponse(
        project_id=project_id,
        status="completed",
        current_stage=result["current_stage"],
        understanding=understanding,
    )


@router.post("/search", response_model=LiteratureSearchResponse)
async def search_literature(
    request: ResearchRequest,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    literature: Annotated[LiteratureMCPClient, Depends(get_literature_client)],
    checkpointer: Annotated[Any, Depends(get_checkpointer)],
) -> LiteratureSearchResponse:
    initial_state = create_research_state(request)
    project_id = initial_state["project_id"]
    graph = build_literature_search_graph(provider, literature, checkpointer)
    config: dict[str, Any] = {"configurable": {"thread_id": project_id}}
    result = await graph.ainvoke(initial_state, config=config)
    return LiteratureSearchResponse(
        project_id=project_id,
        status="completed",
        current_stage=result["current_stage"],
        queries=[SearchQuery.model_validate(item) for item in result["search_queries"]],
        candidate_count=len(result["candidate_papers"]),
        selected_papers=[RankedPaper.model_validate(item) for item in result["selected_papers"]],
        deduplication=result["plan"].get("deduplication", {}),
        search_strategy=result["plan"].get("search_strategy", {}),
        concept_filter=result["plan"].get("concept_filter", {}),
        warnings=result["warnings"],
    )
