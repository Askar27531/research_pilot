"""V13 decision-desk provenance: evidence_reviews source/session columns,
human-source persistence and legacy backfill mapping."""

import pytest

from app.db import Database, ResearchDataRepository
from app.db.repositories import utc_now

BACKFILL_SQL = """
UPDATE evidence_reviews SET source = CASE
    WHEN note LIKE '自动复核：%' THEN 'auto_visual_verifier'
    WHEN note LIKE '图文一致：%' THEN 'auto_consistency'
    ELSE 'human'
END WHERE source IS NULL;
"""


async def _columns(database: Database, table: str) -> set[str]:
    async with database.connect() as connection:
        rows = await (await connection.execute(f"PRAGMA table_info({table})")).fetchall()
    return {row["name"] for row in rows}


async def _schema_version(database: Database) -> int:
    async with database.connect() as connection:
        row = await (await connection.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations"
        )).fetchone()
    return int(row["n"])


async def _seed_evidence(database: Database, project_id: str) -> None:
    """Insert one evidence row directly (FK chain skipped for the unit test)."""
    async with database.connect() as connection:
        await connection.execute("PRAGMA foreign_keys=OFF")
        await connection.execute(
            "INSERT INTO evidence(id,project_id,paper_id,document_id,evidence_type,"
            "locator_key,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("evt-1", project_id, "paper-1", "doc-1", "figure", "fig:1",
             '{"claim":"图 1 示例","confidence":0.8}', utc_now()),
        )
        await connection.commit()


@pytest.mark.asyncio
async def test_v13_adds_provenance_columns_and_version(tmp_path) -> None:
    database = Database(tmp_path / "desk.db")
    await database.initialize()

    assert await _schema_version(database) == 17
    columns = await _columns(database, "evidence_reviews")
    assert {"source", "review_session_id"} <= columns


@pytest.mark.asyncio
async def test_review_evidence_persists_source_and_session(tmp_path) -> None:
    database = Database(tmp_path / "provenance.db")
    await database.initialize()
    from app.db import ProjectRepository
    from app.schemas import ResearchRequest

    project = await ProjectRepository(database).create(
        "Provenance", ResearchRequest(research_question="Who decided what")
    )
    await _seed_evidence(database, project.id)
    repository = ResearchDataRepository(database)

    await repository.review_evidence(
        project.id, "evt-1", "doubted", "图文一致：存疑：图与正文不符",
        source="auto_consistency", review_session_id=None,
    )
    rows = await repository.list_reviews(project.id)
    assert rows == [{
        "evidence_id": "evt-1", "status": "doubted",
        "note": "图文一致：存疑：图与正文不符",
        "source": "auto_consistency", "review_session_id": None,
        "updated_at": rows[0]["updated_at"],
    }]

    # A later human override keeps provenance of the *latest* decision.
    await repository.review_evidence(
        project.id, "evt-1", "excluded", None, source="human",
        review_session_id="session-1",
    )
    rows = await repository.list_reviews(project.id)
    assert rows[0]["status"] == "excluded"
    assert rows[0]["source"] == "human"
    assert rows[0]["review_session_id"] == "session-1"


@pytest.mark.asyncio
async def test_legacy_rows_backfill_source_from_note(tmp_path) -> None:
    """Rows written before V13 (no source) map via the same note-prefix rules."""
    database = Database(tmp_path / "legacy.db")
    await database.initialize()
    from app.db import ProjectRepository
    from app.schemas import ResearchRequest

    project = await ProjectRepository(database).create(
        "Legacy", ResearchRequest(research_question="Backfill me")
    )
    await _seed_evidence(database, project.id)
    async with database.connect() as connection:
        await connection.execute("PRAGMA foreign_keys=OFF")
        await connection.executemany(
            "INSERT INTO evidence_reviews(evidence_id,project_id,status,note,updated_at) "
            "VALUES(?,?,?,?,?)",
            [
                ("evt-1", project.id, "confirmed", "自动复核：图支持该结论", utc_now()),
                ("legacy-e2", project.id, "doubted", "图文一致：存疑：存在矛盾", utc_now()),
                ("legacy-e3", project.id, "excluded", "人工判断无关", utc_now()),
            ],
        )
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.executescript(BACKFILL_SQL)
        await connection.commit()

    rows = await ResearchDataRepository(database).list_reviews(project.id)
    by_id = {row["evidence_id"]: row for row in rows}
    assert by_id["evt-1"]["source"] == "auto_visual_verifier"
    assert by_id["legacy-e2"]["source"] == "auto_consistency"
    assert by_id["legacy-e3"]["source"] == "human"