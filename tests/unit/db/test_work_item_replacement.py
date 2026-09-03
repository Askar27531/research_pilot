import pytest

from app.db import Database, ProjectRepository, WorkItemRepository
from app.db.errors import ProjectConflictError
from app.schemas import ResearchRequest


@pytest.mark.asyncio
async def test_changed_failed_analysis_step_can_be_replaced_without_losing_item(tmp_path) -> None:
    database = Database(tmp_path / "progress.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Analysis", ResearchRequest(research_question="Analyze the selected paper")
    )
    items = WorkItemRepository(database)
    old_hash = "a" * 64
    new_hash = "b" * 64
    original, claimed = await items.claim(
        project.id, "paper-analysis:1", "experiment", "paper_analysis_section", old_hash
    )
    assert claimed is True
    await items.fail(project.id, "paper-analysis:1", "experiment", {"message": "EOF"}, 10)

    replacement, claimed = await items.claim(
        project.id,
        "paper-analysis:1",
        "experiment",
        "paper_analysis_section",
        new_hash,
        replace_changed=True,
    )

    assert claimed is True
    assert replacement.item_id == original.item_id
    assert replacement.input_hash == new_hash
    assert replacement.status == "running"
    assert replacement.error is None
    assert replacement.attempts == 2


@pytest.mark.asyncio
async def test_changed_generic_work_item_still_conflicts_by_default(tmp_path) -> None:
    database = Database(tmp_path / "progress.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Analysis", ResearchRequest(research_question="Analyze the selected paper")
    )
    items = WorkItemRepository(database)
    await items.claim(project.id, "scope", "item", "generic", "a" * 64)

    with pytest.raises(ProjectConflictError):
        await items.claim(project.id, "scope", "item", "generic", "b" * 64)
