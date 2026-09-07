"""V16 event-ized HITL (minimal): waiting_for_human is the derived semantic of
``status='waiting'`` + one open ``hitl_events`` row; the worker guard reads the
same rows so only a human action (resolving the event) can move the project on."""

import pytest

from app.db import Database, HitlEventRepository, ProjectRepository
from app.schemas import ResearchRequest


async def _columns(database: Database, table: str) -> set[str]:
    async with database.connect() as connection:
        rows = await (await connection.execute(
            f"PRAGMA table_info({table})"
        )).fetchall()
    return {row["name"] for row in rows}


@pytest.mark.asyncio
async def test_v16_adds_hitl_events_table(tmp_path) -> None:
    database = Database(tmp_path / "v16.db")
    await database.initialize()

    columns = await _columns(database, "hitl_events")
    assert {
        "project_id", "type", "title", "reason", "options_json", "level",
        "status", "created_by", "default_action", "resolved_by", "created_at",
    } <= columns


@pytest.mark.asyncio
async def test_v17_adds_analysis_review_decision_column(tmp_path) -> None:
    database = Database(tmp_path / "v17.db")
    await database.initialize()

    columns = await _columns(database, "projects")
    assert "analysis_review_decision" in columns


@pytest.mark.asyncio
async def test_analysis_review_decision_roundtrip(tmp_path) -> None:
    database = Database(tmp_path / "decision.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Decision", ResearchRequest(research_question="Review gate decision")
    )
    projects = ProjectRepository(database)

    assert (await projects.get(project.id)).analysis_review_decision is None
    await projects.set_analysis_review_decision(project.id, "continue")
    assert (await projects.get(project.id)).analysis_review_decision == "continue"
    await projects.set_analysis_review_decision(project.id, None)
    assert (await projects.get(project.id)).analysis_review_decision is None


@pytest.mark.asyncio
async def test_wait_for_human_parks_project_with_open_event(tmp_path) -> None:
    database = Database(tmp_path / "wait.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Wait", ResearchRequest(research_question="Pick papers")
    )
    projects = ProjectRepository(database)
    hitl = HitlEventRepository(database)

    record = await projects.wait_for_human(
        project.id, "paper_selection",
        event_type="paper_selection",
        title="请选择要精读的论文",
        reason="检索完成，候选 12 篇。",
        scope={"papers": ["p-1"]},
        options=[{"action": "select_papers", "label": "选择论文"}],
        created_by="system",
    )
    assert record.status == "waiting"
    assert record.current_stage == "paper_selection"
    assert await hitl.has_open(project.id) is True

    events = await hitl.open_events(project.id)
    assert len(events) == 1
    assert events[0]["type"] == "paper_selection"
    assert events[0]["title"] == "请选择要精读的论文"
    assert events[0]["scope"] == {"papers": ["p-1"]}
    assert events[0]["options"] == [{"action": "select_papers", "label": "选择论文"}]
    assert events[0]["id"]


@pytest.mark.asyncio
async def test_resolve_all_closes_events_and_can_filter_by_type(tmp_path) -> None:
    database = Database(tmp_path / "resolve.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Resolve", ResearchRequest(research_question="Human decides")
    )
    projects = ProjectRepository(database)
    hitl = HitlEventRepository(database)

    await projects.wait_for_human(project.id, "paper_selection",
                                  event_type="paper_selection", title="选文")
    await projects.wait_for_human(project.id, "awaiting_documents",
                                  event_type="document_unavailable", title="缺 PDF")

    # Closing only the document wait leaves the selection event open.
    closed = await hitl.resolve_all(
        project.id, event_type="document_unavailable",
        resolved_by="action:upload", resolution={"action": "upload"},
    )
    assert closed == 1
    remaining = [event["type"] for event in await hitl.open_events(project.id)]
    assert remaining == ["paper_selection"]

    # The human action consumes the rest; the resolution is recorded.
    closed = await hitl.resolve_all(
        project.id, resolved_by="action:select_papers",
        resolution={"action": "select_papers"},
    )
    assert closed == 1
    assert await hitl.has_open(project.id) is False

    async with database.connect() as connection:
        row = await (await connection.execute(
            "SELECT resolved_by,resolution_json,status FROM hitl_events "
            "WHERE project_id=? AND type='paper_selection'", (project.id,)
        )).fetchone()
    assert row["status"] == "resolved"
    assert row["resolved_by"] == "action:select_papers"
    assert '"select_papers"' in row["resolution_json"]


@pytest.mark.asyncio
async def test_supersede_marks_obsolete_waits(tmp_path) -> None:
    database = Database(tmp_path / "supersede.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Supersede", ResearchRequest(research_question="Regenerate search")
    )
    projects = ProjectRepository(database)
    hitl = HitlEventRepository(database)

    await projects.wait_for_human(project.id, "paper_selection",
                                  event_type="paper_selection", title="选文")
    await hitl.resolve_all(
        project.id, status="superseded", resolved_by="action:regenerate_search",
        resolution={"action": "regenerate_search"},
    )
    assert await hitl.has_open(project.id) is False
    async with database.connect() as connection:
        row = await (await connection.execute(
            "SELECT status FROM hitl_events WHERE project_id=?", (project.id,)
        )).fetchone()
    assert row["status"] == "superseded"


@pytest.mark.asyncio
async def test_events_cascade_on_project_delete(tmp_path) -> None:
    database = Database(tmp_path / "cascade.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Cascade", ResearchRequest(research_question="Delete cleans events")
    )
    projects = ProjectRepository(database)
    hitl = HitlEventRepository(database)

    await projects.wait_for_human(project.id, "paper_selection",
                                  event_type="paper_selection", title="选文")
    await projects.delete(project.id)
    assert await hitl.has_open(project.id) is False
