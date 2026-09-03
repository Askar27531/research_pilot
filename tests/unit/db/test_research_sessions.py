import pytest

from app.db import (
    Database,
    PaperRepository,
    ProjectRepository,
    ResearchSessionRepository,
    WorkflowJobRepository,
)
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.schemas import PaperMetadata, RankedPaper, ResearchRequest


def ranked(identifier: str) -> RankedPaper:
    return RankedPaper(
        paper=PaperMetadata(stable_id=identifier, source_id=identifier, title=identifier),
        lexical_score=0.5, final_score=0.5, selection_reason="Relevant",
    )


@pytest.mark.asyncio
async def test_selection_accepts_only_current_search_papers(tmp_path) -> None:
    database = Database(tmp_path / "research.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Selection", ResearchRequest(research_question="Select papers")
    )
    papers = PaperRepository(database)
    sessions = ResearchSessionRepository(database)
    revision = await sessions.begin_search(project.id)
    first = await papers.upsert_ranked(project.id, ranked("paper-one"))
    second = await papers.upsert_ranked(project.id, ranked("paper-two"))
    outsider = await papers.upsert_ranked(project.id, ranked("not-in-results"))
    await sessions.complete_search(project.id, revision, {"queries": []}, [first.id, second.id])

    await sessions.select(project.id, revision, [first.id, second.id], "compare methods")
    selection = await sessions.selection(project.id)
    assert selection["paper_ids"] == [first.id, second.id]
    assert selection["requirements"] == "compare methods"

    with pytest.raises(ProjectConflictError, match="not in the current search"):
        await sessions.select(project.id, revision, [outsider.id], None)


@pytest.mark.asyncio
async def test_new_search_invalidates_selection_and_preserves_history(tmp_path) -> None:
    database = Database(tmp_path / "research.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Revision", ResearchRequest(research_question="Revise search")
    )
    papers = PaperRepository(database)
    sessions = ResearchSessionRepository(database)
    revision = await sessions.begin_search(project.id)
    paper = await papers.upsert_ranked(project.id, ranked("paper-one"))
    await sessions.complete_search(project.id, revision, {"queries": ["first"]}, [paper.id])
    await sessions.select(project.id, revision, [paper.id], None)

    next_revision = await sessions.begin_search(project.id, "focus on evidence")
    assert next_revision == 2
    assert await sessions.selection(project.id) is None
    with pytest.raises(ProjectConflictError, match="outdated"):
        await sessions.select(project.id, revision, [paper.id], None)


@pytest.mark.asyncio
async def test_reset_selection_keeps_current_search_results(tmp_path) -> None:
    database = Database(tmp_path / "research.db")
    await database.initialize()
    projects = ProjectRepository(database)
    project = await projects.create(
        "Reselect", ResearchRequest(research_question="Choose different papers")
    )
    papers = PaperRepository(database)
    paper = await papers.upsert_ranked(project.id, ranked("current"))
    sessions = ResearchSessionRepository(database)
    revision = await sessions.begin_search(project.id)
    await sessions.complete_search(project.id, revision, {"queries": []}, [paper.id])
    await sessions.select(project.id, revision, [paper.id], "first choice")

    await sessions.reset_selection(project.id)

    assert await sessions.selection(project.id) is None
    assert await sessions.current_paper_ids(project.id, revision) == [paper.id]


@pytest.mark.asyncio
async def test_project_delete_cascades_related_records(tmp_path) -> None:
    database = Database(tmp_path / "research.db")
    await database.initialize()
    projects = ProjectRepository(database)
    project = await projects.create(
        "Delete", ResearchRequest(research_question="Delete this research project")
    )
    sessions = ResearchSessionRepository(database)
    await sessions.begin_search(project.id)

    await projects.delete(project.id)

    with pytest.raises(RecordNotFoundError):
        await projects.get(project.id)
    assert await sessions.current_search(project.id) is None


@pytest.mark.asyncio
async def test_failed_job_reconciliation_updates_project_status(tmp_path) -> None:
    database = Database(tmp_path / "research.db")
    await database.initialize()
    projects = ProjectRepository(database)
    project = await projects.create(
        "Failure", ResearchRequest(research_question="Recover failed project status")
    )
    jobs = WorkflowJobRepository(database)
    job = await jobs.enqueue(project.id, "document_analysis")
    await projects.start_run(project.id, job.run_id)
    await jobs.fail(job.job_id, {"type": "LLMError", "message": "timed out"})

    assert await jobs.reconcile_failed_projects() == 1
    repaired = await projects.get(project.id)
    assert repaired.status == "failed"
    assert repaired.error == {"type": "LLMError", "message": "timed out"}
