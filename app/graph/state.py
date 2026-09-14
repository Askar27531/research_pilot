from typing import Any, TypedDict
from uuid import uuid4

from app.schemas import ResearchRequest


class ResearchError(TypedDict):
    stage: str
    message: str
    retryable: bool


class ResearchState(TypedDict):
    """Durable, project-level state shared by LangGraph nodes."""

    project_id: str
    user_goal: str
    request: dict[str, Any]
    understanding: dict[str, Any] | None

    plan: dict[str, Any]
    current_stage: str

    search_queries: list[str]
    candidate_papers: list[dict[str, Any]]
    selected_papers: list[dict[str, Any]]
    ranked_papers: list[dict[str, Any]]
    warnings: list[str]

    errors: list[ResearchError]


def create_research_state(
    request: ResearchRequest,
    project_id: str | None = None,
) -> ResearchState:
    """Create a complete initial state for a new research project."""

    return ResearchState(
        project_id=project_id or str(uuid4()),
        user_goal=request.research_question,
        request=request.model_dump(mode="json"),
        understanding=None,
        plan={},
        current_stage="initialized",
        search_queries=[],
        candidate_papers=[],
        selected_papers=[],
        ranked_papers=[],
        warnings=[],
        errors=[],
    )
