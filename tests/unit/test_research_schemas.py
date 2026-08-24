import pytest
from pydantic import ValidationError

from app.graph import create_research_state
from app.schemas import ResearchRequest


def test_research_request_normalizes_user_input() -> None:
    request = ResearchRequest(
        research_question="  RGB-LWIR image registration  ",
        keywords=["UAV", " uav ", "thermal", ""],
        year_from=2024,
        year_to=2026,
        constraints=["Open access", " open access "],
    )

    assert request.research_question == "RGB-LWIR image registration"
    assert request.keywords == ["UAV", "thermal"]
    assert request.constraints == ["Open access"]
    assert request.maximum_papers == 15


def test_research_request_rejects_invalid_year_range() -> None:
    with pytest.raises(ValidationError, match="year_from must be less"):
        ResearchRequest(research_question="valid question", year_from=2026, year_to=2024)


def test_create_research_state_is_complete_and_isolated() -> None:
    request = ResearchRequest(research_question="RGB-LWIR registration")
    first = create_research_state(request, project_id="project-1")
    second = create_research_state(request, project_id="project-2")

    first["search_queries"].append("thermal registration")

    assert first["project_id"] == "project-1"
    assert first["current_stage"] == "initialized"
    assert first["user_goal"] == request.research_question
    assert first["experiment_plan"] is None
    assert second["search_queries"] == []
