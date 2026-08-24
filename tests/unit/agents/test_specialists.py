import pytest

from app.agents import MultimodalAnalyst, ResearchBuilder
from app.schemas import AgentTask


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("specialist", "task_type"),
    [
        (MultimodalAnalyst(), "multimodal_analysis"),
        (ResearchBuilder(), "research_build"),
    ],
)
async def test_future_specialists_return_explicit_unsupported(specialist, task_type) -> None:
    task = AgentTask(
        task_id="task-1",
        project_id="project-1",
        task_type=task_type,
        objective="Perform a future-stage task",
    )

    result = await specialist.run(task)

    assert result.status == "unsupported"
    assert specialist.capability().available is False
    assert result.output == {}
