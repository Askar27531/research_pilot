import aiosqlite
import pytest

from app.db import Database, PaperRepository, ProjectRepository, TraceRepository
from app.db.database import MIGRATION_V1
from app.db.errors import ProjectConflictError, RecordNotFoundError
from app.schemas import PaperMetadata, RankedPaper, ResearchRequest


def request() -> ResearchRequest:
    return ResearchRequest(research_question="RGB-LWIR image registration")


@pytest.mark.asyncio
async def test_existing_v1_database_migrates_to_latest(tmp_path) -> None:
    path = tmp_path / "v1.db"
    async with aiosqlite.connect(path) as connection:
        await connection.executescript(MIGRATION_V1)
        await connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01')"
        )
        await connection.commit()

    await Database(path).initialize()

    async with aiosqlite.connect(path) as connection:
        versions = await (
            await connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ).fetchall()
        tables = await (
            await connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    assert [row[0] for row in versions] == [1, 2, 3, 4, 5]
    assert {"documents", "evidence", "paper_summaries", "summary_evidence_refs"} <= {
        row[0] for row in tables
    }


@pytest.mark.asyncio
async def test_database_migration_and_project_survive_new_database_instance(tmp_path) -> None:
    path = tmp_path / "research.db"
    database = Database(path)
    await database.initialize()
    await database.initialize()

    created = await ProjectRepository(database).create("Registration", request())
    reopened = Database(path)
    loaded = await ProjectRepository(reopened).get(created.id)

    assert loaded == created
    async with reopened.connect() as connection:
        versions = await (
            await connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ).fetchall()
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_project_run_transitions_are_atomic_and_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "research.db")
    await database.initialize()
    repository = ProjectRepository(database)
    project = await repository.create("Registration", request())

    running, claimed = await repository.start_run(project.id, "run-1")
    duplicate, duplicate_claimed = await repository.start_run(project.id, "run-1")
    assert running.status == "running"
    assert claimed is True
    assert duplicate.status == "running"
    assert duplicate_claimed is False

    with pytest.raises(ProjectConflictError):
        await repository.start_run(project.id, "run-2")

    completed = await repository.complete(project.id, "papers_selected")
    _, completed_claimed = await repository.start_run(project.id, "run-3")
    assert completed.status == "completed"
    assert completed_claimed is False


@pytest.mark.asyncio
async def test_failed_project_requires_resume(tmp_path) -> None:
    database = Database(tmp_path / "research.db")
    await database.initialize()
    repository = ProjectRepository(database)
    project = await repository.create("Registration", request())
    await repository.start_run(project.id, "run-1")
    failed = await repository.fail(project.id, "search_papers", {"type": "Timeout"})

    assert failed.error == {"type": "Timeout"}
    with pytest.raises(ProjectConflictError):
        await repository.start_run(project.id, "run-2")
    resumed, claimed = await repository.start_run(project.id, "run-2", resume=True)
    assert resumed.status == "running"
    assert claimed is True


@pytest.mark.asyncio
async def test_paper_upsert_and_trace_are_persistent(tmp_path) -> None:
    path = tmp_path / "research.db"
    database = Database(path)
    await database.initialize()
    project = await ProjectRepository(database).create("Registration", request())
    papers = PaperRepository(database)
    ranked = RankedPaper(
        paper=PaperMetadata(
            stable_id="10.1/example",
            source_id="W1",
            title="RGB-LWIR Registration",
            doi="10.1/example",
        ),
        lexical_score=0.8,
        llm_score=0.9,
        final_score=0.85,
        selection_reason="Relevant",
    )
    first = await papers.upsert_ranked(project.id, ranked)
    updated = await papers.upsert_ranked(
        project.id, ranked.model_copy(update={"final_score": 0.95})
    )
    await TraceRepository(database).append(project.id, "run-1", "research_completed", success=True)

    reopened = Database(path)
    stored = await PaperRepository(reopened).list_for_project(project.id)
    traces = await TraceRepository(reopened).list_for_project(project.id)
    assert len(stored) == 1
    assert stored[0].id == first.id == updated.id
    assert stored[0].relevance_score == 0.95
    assert [trace.event_type for trace in traces] == ["research_completed"]


@pytest.mark.asyncio
async def test_unknown_project_raises_not_found(tmp_path) -> None:
    database = Database(tmp_path / "research.db")
    await database.initialize()
    with pytest.raises(RecordNotFoundError):
        await ProjectRepository(database).get("missing")
