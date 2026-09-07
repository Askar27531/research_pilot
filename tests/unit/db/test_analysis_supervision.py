"""V14 analysis-supervision: pause flag, stage board, part instructions and
scoped deletion backing "interrupt / see where it is / re-analyze one part"."""

import pytest

from app.db import (
    Database,
    ProjectRepository,
    ResearchDataRepository,
    ResearchSessionRepository,
    WorkItemRepository,
)
from app.schemas import (
    AnalysisClaim,
    AnalysisReport,
    PaperAnalysis,
    ResearchRequest,
)


async def _schema_version(database: Database) -> int:
    async with database.connect() as connection:
        row = await (await connection.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations"
        )).fetchone()
    return int(row["n"])


async def _table_names(database: Database) -> set[str]:
    async with database.connect() as connection:
        rows = await (await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()
    return {row["name"] for row in rows}


def _paper(paper_id: str, title: str, evidence_id: str) -> PaperAnalysis:
    return PaperAnalysis(
        paper_id=paper_id, title=title,
        core_problem=[AnalysisClaim(
            value=f"{title} 结论", kind="supported", evidence_ids=[evidence_id],
        )],
        methods=[], mechanisms=[], experimental_setup=[], main_results=[],
        limitations=[], relevance_to_topic=[], overview=None,
    )


@pytest.mark.asyncio
async def test_v14_adds_supervision_tables_and_pause_column(tmp_path) -> None:
    database = Database(tmp_path / "supervision.db")
    await database.initialize()

    assert await _schema_version(database) == 17
    tables = await _table_names(database)
    assert {"analysis_progress", "analysis_part_instructions"} <= tables
    async with database.connect() as connection:
        columns = {
            row["name"]
            for row in await (await connection.execute(
                "PRAGMA table_info(projects)"
            )).fetchall()
        }
    assert "pause_requested" in columns
    assert "analysis_review_decision" in columns


@pytest.mark.asyncio
async def test_pause_flag_roundtrip(tmp_path) -> None:
    database = Database(tmp_path / "pause.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Pause", ResearchRequest(research_question="Interrupt me")
    )
    research = ResearchDataRepository(database)
    assert await research.is_pause_requested(project.id) is False
    await ProjectRepository(database).set_pause_requested(project.id, True)
    assert await research.is_pause_requested(project.id) is True
    await ProjectRepository(database).set_pause_requested(project.id, False)
    assert await research.is_pause_requested(project.id) is False


@pytest.mark.asyncio
async def test_progress_board_upsert_and_reset(tmp_path) -> None:
    database = Database(tmp_path / "board.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Board", ResearchRequest(research_question="Stage board")
    )
    research = ResearchDataRepository(database)

    await research.set_progress(
        project.id, "paper-a", "visuals", "running",
        label="图表视觉观察", done=3, total=12,
    )
    rows = await research.list_progress(project.id)
    assert rows == [{
        "project_id": project.id, "paper_id": "paper-a", "stage_key": "visuals",
        "status": "running", "label": "图表视觉观察", "done": 3, "total": 12,
        "updated_at": rows[0]["updated_at"],
    }]

    await research.set_progress(
        project.id, "paper-a", "visuals", "completed", done=12, total=12,
    )
    rows = await research.list_progress(project.id)
    assert rows[0]["status"] == "completed"
    assert rows[0]["done"] == 12

    await research.reset_progress_paper(project.id, "paper-a", ["visuals", "index"])
    rows = await research.list_progress(project.id)
    assert rows[0]["status"] == "queued"
    assert rows[0]["done"] == 0


@pytest.mark.asyncio
async def test_part_instructions_roundtrip(tmp_path) -> None:
    database = Database(tmp_path / "parts.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Parts", ResearchRequest(research_question="Re-analyze one part")
    )
    sessions = ResearchSessionRepository(database)
    await sessions.begin_search(project.id)

    await sessions.save_part_instruction(project.id, 1, "paper-a", "method", "补充伪代码步骤")
    await sessions.save_part_instruction(project.id, 1, "paper-a", "overview", "概述不超过 200 字")
    instructions = await sessions.part_instructions(project.id, 1)
    assert instructions == {
        "paper-a": {
            "method": "补充伪代码步骤",
            "overview": "概述不超过 200 字",
        },
    }


@pytest.mark.asyncio
async def test_scoped_analysis_delete_and_part_cache_invalidation(tmp_path) -> None:
    database = Database(tmp_path / "scoped.db")
    await database.initialize()
    project = await ProjectRepository(database).create(
        "Scoped", ResearchRequest(research_question="Only one paper reruns")
    )
    sessions = ResearchSessionRepository(database)
    await sessions.begin_search(project.id)

    report = AnalysisReport(
        project_id=project.id, search_revision=1,
        papers=[_paper("paper-a", "A", "e-a"), _paper("paper-b", "B", "e-b")],
        created_at="2026-01-01T00:00:00Z",
    )
    # selected_paper_analyses.paper_id is FK-bound to papers, so seed rows.
    from app.db.repositories import utc_now

    async with database.connect() as connection:
        for paper_id in ("paper-a", "paper-b"):
            await connection.execute(
                "INSERT INTO papers(id,project_id,stable_key,metadata_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (paper_id, project.id, f"stable:{paper_id}", "{}",
                 utc_now(), utc_now()),
            )
        await connection.commit()
    await sessions.save_report(report)
    await sessions.save_paper_analysis(project.id, 1, report.papers[0])
    await sessions.save_paper_analysis(project.id, 1, report.papers[1])

    # Scoped delete drops the report and only the targeted paper's analysis row.
    await sessions.delete_analysis(project.id, 1, paper_id="paper-a")
    assert await sessions.report(project.id, 1) is None
    remaining = await sessions.paper_analyses(project.id, 1)
    assert [item.paper_id for item in remaining] == ["paper-b"]

    # Cache invalidation targets exactly one (paper, part) row.
    items = WorkItemRepository(database)
    for index, part in enumerate(("problem", "method", "overview")):
        await items.claim(
            project.id, "paper-analysis:1:paper-a:abc123", part,
            "paper_analysis_section", f"{index:02x}" + "0" * 62,
            replace_changed=True,
        )
    deleted = await items.delete_part_items(project.id, 1, "paper-a", ["method"])
    assert deleted == 1
    left = {item.item_key for item in await items.list_for_project(project.id)}
    assert {"problem", "overview"} <= left
    assert "method" not in left